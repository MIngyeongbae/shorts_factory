"""Midjourney 어댑터 — mjopen 프록시 REST (ADR-0025).

ADR-0025가 프로바이더를, ADR-0027이 프롬프트 방언을 정했다.

- 제출 `POST {base}/mj-fast/mj/submit/imagine`, 인증 헤더 `mj-api-secret`
- 폴링 `GET {base}/mj/task/{id}/fetch` — 잡이 끝날 때까지
- 결과 **4장을 전부 돌려준다** (`GeneratedImage.variants`, ADR-0031 §2).
  `data`는 그중 첫 장이라 하류 계약은 그대로다 — 고르는 것은 `[6r]` 몫이다.
  ADR-0025의 `imageUrls[0]` 고정(계약 공백 #1)은 "고를 근거가 없어서"였고,
  근거를 만드는 단계가 생겨 풀렸다

## 모드는 프롬프트가 아니라 URL 프리픽스로 고른다 — 이미지는 fast다

`--relax`·`--fast` 플래그를 붙이지 않는다. 프록시가 엔드포인트 프리픽스를 보고 붙인다
(G1 실측). **이미지는 `/mj-fast/`다** — ADR-0039이 ADR-0025 §2("이미지 relax, 영상
fast")를 뒤집었다. 1잡이 relax 180.4초에서 **7.0초**가 되고 GPU는 0.6~0.8분뿐이라
Pro 30시간이면 월 80편이다. GPU를 쓰는 쪽이 이제 이미지고, **영상이 relax로 빠져
GPU 0**이다 (`videogen/midjourney.py`) — 전 씬 영상을 가능하게 한 것이 그 교환이다.

**프리픽스를 프록시의 자동 변환에 맡기지 않는다.** 같은 영상 호출이 2026-08-19 오전에는
`/mj-relax/`로 던졌는데 조용히 fast로 바뀌어 성공했고 오후에는 거절됐다 (ADR-0039 §4).
프리픽스를 틀리면 잘못된 프롬프트가 아니라 **아예 다른 엔드포인트**가 되므로, 모드가
어긋나는 경로는 이 한 줄뿐이다.

## 보내는 문자열 = `prompt` + 공백 + `negative_prompt`

MJ 방언은 `--ar`·`--no`까지 한 줄에 담긴다 (ADR-0027). 이 조합이 relax 탐침에서 통한
그 문자열이라 `[6]`이 재조립하지 않고 그대로 잇는다.

## 앵커를 첨부하지 않는다

`--sref`가 `BASE_STYLE`을 이겨 마감을 망가뜨렸다 (ADR-0025 G3). 그래서
`requires_style_anchors = False`다 — 이 경로에서 앵커 0장은 정상이다.

## 응답 파싱 주의

`imageUrls`는 **URL 문자열 배열이 아니라 `{url, thumbnail}` 객체 배열**이다 (실측).
`url`은 `cdn.midjourney.com/<uuid>/0_0.png`(PNG 원본)이고, 최상위 `imageUrl`은 4장을
합친 로컬 그리드(`attachments/merges/...webp`, 1632×2912)라 **씬 이미지가 아니다.**
둘을 헷갈리면 씬마다 2×2 그리드가 들어간다.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from ..transport import CDN_HEADERS, Transport, TransportError, TransportTimeout
from ..transport import urllib_transport as _urllib_transport
from .base import (
    GeneratedImage,
    ImageClient,
    ImageGenError,
    CharacterReference,
    ImageGenRateLimited,
    ImageGenTimeout,
    ImageRequest,
    ProviderNotConfigured,
)

#: 프록시 주소와 토큰. 기본값은 이 개발 머신의 mjopen 컨테이너다 (ADR-0025).
BASE_URL_ENV = "MJ_PROXY_URL"
SECRET_ENV = "MJ_API_SECRET"
DEFAULT_BASE_URL = "http://127.0.0.1:8086"
DEFAULT_SECRET = "admin"

log = logging.getLogger(__name__)

AUTH_HEADER = "mj-api-secret"

#: **이미지는 fast, 영상은 relax** (ADR-0039이 ADR-0025 §2를 뒤집었다).
#: 이 어댑터는 이미지 전용이고, 영상은 `videogen/midjourney.py`가 relax로 던진다.
SUBMIT_PATH = "/mj-fast/mj/submit/imagine"

#: 우리 이미지를 **공개 주소로** 올리는 자리 (ADR-0070·0071). 저장 위치는 프록시 설정이
#: 정한다(`imageStorageType`) — `LOCAL`이면 `http://localhost:…`가 돌아오고 MJ가 그것을
#: `Invalid link`로 거절한다. R2/S3여야 한다.
UPLOAD_PATH = "/mj/submit/upload-discord-images"
FETCH_PATH = "/mj/task/{task_id}/fetch"

#: 포기한 잡을 죽인다 (ADR-0045). 제출과 같은 모드 프리픽스를 쓴다 — 취소도 모드별
#: 라우팅을 탄다. 버려두면 프록시가 끝까지 돌려서 GPU를 태운다: 26장이 필요한 편에서
#: MJ가 73장을 구웠다 (실측 2026-08-20).
CANCEL_PATH = "/mj-fast/mj/task/{task_id}/cancel"
CANCEL_TIMEOUT = 15

#: 캐릭터 시트 U1 업스케일 (ADR-0051). 제출과 같은 fast 프리픽스다 — 모드는 엔드포인트
#: 프리픽스가 정한다 (ADR-0039 §4).
ACTION_PATH = "/mj-fast/mj/submit/action"

#: 완료된 업스케일을 찾을 때 쓴다 (ADR-0051 — 같은 업스케일을 짧은 간격에 재요청하면
#: MJ가 `Slow Down!`으로 거절한다, 실측 2026-08-21).
TASK_LIST_PATH = "/mj/task/list"

#: U{n} 버튼의 customId 표식. 전체 형식은 `MJ::JOB::upsample::{n}::{jobId}`이고 태스크의
#: buttons에서 읽는다 — 손으로 조립하지 않는다. `{n}`은 1부터다.
UPSCALE_MARKER_FORMAT = "::upsample::{n}::"
#: 캐릭터 시트(ADR-0051)가 쓰는 칸. `[6]`은 사분면을 골라 부른다 (ADR-0071).
UPSCALE_MARKER = UPSCALE_MARKER_FORMAT.format(n=1)

#: 캐릭터 참조 문법 (ADR-0051 G3 실측 2026-08-21, `--ow`는 개정 2026-08-22). 계정 기본
#: v8.2에서 `--cref`(v8 폐기)·`--oref` 둘 다 `not compatible with --version 8.2`로
#: 거절된다 — oref는 v7 실행 전용이라 `--v 7`을 명시해야 돈다. **가중 기본(100)은
#: 시트의 구도·흰 배경까지 복제해 씬을 잃는다** — `--ow 25`에서 동작·배경 요소가
#: 돌아오고 정체는 유지됐다 (G4 프로브 실측). 문법이 또 바뀌면 여기 한 자리만 고친다.
REFERENCE_SUFFIX = "--oref {url} --ow 25 --v 7"

#: 계정 목록. `[6]`의 워커 수를 여기서 읽는다 (ADR-0031 G3). 응답은
#: `{"list": [...], "pagination": ...}`이고 계정 오브젝트에 `coreSize`(fast 동시
#: 한도)·`relaxCoreSize`(relax 동시 한도)가 있다 (실측).
#: **응답에는 `userToken`·`cookie` 같은 비밀이 함께 온다** — 이 어댑터는 숫자 한 개만
#: 꺼내고 나머지는 어디에도 남기지 않는다 (ADR-0032 §3).
ACCOUNTS_PATH = "/mj/admin/accounts"
ACCOUNTS_TIMEOUT = 15

#: 제출 성공으로 치는 응답 코드. 1=제출됨, 21=같은 잡이 이미 큐에 있음(둘 다 `result`에
#: taskId를 준다), 22=큐 대기. 그 외는 실패다.
SUBMIT_OK = (1, 21, 22)

#: 잡 하나를 기다리는 상한을 **프록시 계정에서 읽는다** (ADR-0045). 어댑터가 상수를
#: 들면 프록시가 더 짧을 때 그 상수는 한 번도 안 쓰이는 죽은 값이 된다 — 실측이
#: 어댑터 1800초 vs 프록시 `timeoutMinutes` 15분(900초)이었고, 잡을 죽인 것은 언제나
#: 프록시였다. `coreSize`를 읽는 것과 같은 이유·같은 경로다 (ADR-0031 G3, ADR-0032).
#:
#: 아래 값은 **계정을 못 읽었을 때만** 쓴다. 넉넉해도 폭주하지 않는다 — 프록시가
#: 자기 상한을 그대로 집행하므로 이 숫자는 상한이 아니라 상한의 상한이다.
FALLBACK_TIMEOUT = 1800
POLL_INTERVAL = 10

#: 프록시 태스크 상태. `SUCCESS`만 성공이고 나머지 종결 상태는 씬 실패다.
STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = ("FAILURE", "CANCEL")

#: Discord 모드 그리드를 4분할할 때 쓴다. 이미 필수 의존이다 (조립·자막 번인).
FFMPEG = "ffmpeg"
CROP_TIMEOUT = 120

#: HTTP 경계는 공용 `transport.py`다 (ADR-0056 — 휴면 어댑터가 공용 모듈에 기댄다).
#: `Transport`는 거기서 import한 이름이고 이 어댑터의 생성자 시그니처 그대로다.


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: int
) -> tuple[int, bytes]:
    """stdlib 요청 — 오류를 이 어댑터의 예외 계층으로 바꾼다. 호출부가 보는 것은 전과 같다."""
    try:
        return _urllib_transport(method, url, headers, body, timeout)
    except TransportTimeout as exc:
        raise ImageGenTimeout(str(exc)) from exc
    except TransportError as exc:
        raise ImageGenError(str(exc)) from exc


def build_prompt(request: ImageRequest) -> str:
    """MJ에 보낼 한 줄. `prompt` + 공백 + `negative_prompt` (+ 캐릭터 참조).

    `--ar`은 이미 `prompt` 끝에, `--no`는 `negative_prompt` 전체다 (ADR-0027).
    여기서 종횡비나 배제 항목을 다시 만들지 않는다 — 출처가 둘이 되면 갈린다.

    `reference_url`이 있으면 참조 플래그를 맨 끝에 잇는다 (ADR-0051). 문법이
    프로바이더 소관이라 이 어댑터가 든다 (G3) — `[6]`은 URL만 넘긴다.
    """
    negative = request.negative_prompt.strip()
    prompt = request.prompt.strip()
    line = f"{prompt} {negative}" if negative else prompt
    if request.reference_url:
        line = f"{line} {REFERENCE_SUFFIX.format(url=request.reference_url)}"
    return line


def result_images(payload: dict[str, Any]) -> tuple[tuple[str, ...], bool]:
    """`(내려받을 URL들, 4분할이 필요한가)`. 계정 모드마다 응답 모양이 다르다 (실측).

    **네 장을 전부 돌려준다** (ADR-0031 §2). 잡 하나가 네 장을 내는데 지금까지 한 장만
    쓰고 셋을 버렸다 — 이미 산 것이라 고르는 데 추가 과금이 0이고, 실측에서 `q0`이
    넷 중 제일 약했다(ADR-0031 사분면 실측). 고르는 것은 `[6r]` 몫이고 어댑터는
    버리지만 않는다.

    **공식 웹 모드**는 `imageUrls`에 4장을 개별 URL로 준다
    (`{url, thumbnail}` 객체 배열, `url`은 `cdn.midjourney.com/<uuid>/0_0.png`).
    `imageUrls[0]` 고정이면 되고 자를 것이 없다 (ADR-0025 계약 공백 #1).

    **Discord 모드**는 `imageUrls`가 `null`이고 `imageUrl` 하나만 온다. 그것은
    **2×2 그리드**(1632×2912 = 816×1456의 4배)다 — 개별 URL이 없으므로 왼쪽 위
    사분면을 잘라 쓴다. ADR-0025가 "개별 URL이 막히면 4분할하는 경로가 남아 있다"고
    적어 둔 그 경로다. U1 업스케일 잡을 따로 던지지 않는다: v6 이후 U 버튼은 확대가
    아니라 사분면 분리라서 같은 픽셀을 얻자고 잡 수를 27 → 54로 늘리게 된다.

    `thumbnail`은 어느 모드에서도 쓰지 않는다 — 640px 축소본이다.
    """
    items = payload.get("imageUrls") or []
    if items:
        urls: list[str] = []
        for index, item in enumerate(items):
            url = item.get("url") if isinstance(item, dict) else item
            if not isinstance(url, str) or not url:
                raise ImageGenError(f"imageUrls[{index}]에서 url을 읽을 수 없다: {item!r}")
            urls.append(url)
        return tuple(urls), False

    url = payload.get("imageUrl")
    if not isinstance(url, str) or not url:
        raise ImageGenError(
            f"태스크가 SUCCESS인데 이미지 URL이 없다 (키: {sorted(payload)})"
        )
    return (url,), True


#: 2×2 그리드의 사분면 → `crop` 표현식. 순서는 읽는 순서다 (좌상·우상·좌하·우하).
#: `iw/2`는 정수 나눗셈이 아니라 FFmpeg 표현식이라 홀수 픽셀에서도 안전하다.
#: 실측 그리드는 1632×2912라 나머지가 없다.
QUADRANT_CROPS = (
    "crop=iw/2:ih/2:0:0",
    "crop=iw/2:ih/2:iw/2:0",
    "crop=iw/2:ih/2:0:ih/2",
    "crop=iw/2:ih/2:iw/2:ih/2",
)


def crop_quadrants(
    data: bytes, *, suffix: str = ".webp", ffmpeg: str = FFMPEG
) -> tuple[bytes, ...]:
    """2×2 그리드를 네 장의 PNG로 나눈다. 순서는 `q0`~`q3`이다 (ADR-0031 §2).

    FFmpeg를 쓰는 이유는 **이미 이 프로젝트의 필수 의존이고**(조립·자막 번인) webp를
    읽을 수 있는 유일한 수단이기 때문이다. Pillow를 새로 들이지 않는다.

    한 번에 네 번 자른다. U 버튼(사분면 분리) 잡을 던지지 않는 이유는 그대로다 —
    같은 픽셀을 얻자고 잡 수를 27 → 54로 늘리게 된다 (`result_images` 독스트링).
    여기는 로컬 FFmpeg라 잡도 과금도 늘지 않는다.
    """
    with tempfile.TemporaryDirectory(prefix="mj-grid-") as tmp:
        source = Path(tmp) / f"grid{suffix}"
        source.write_bytes(data)
        quadrants: list[bytes] = []
        for index, crop in enumerate(QUADRANT_CROPS):
            target = Path(tmp) / f"q{index}.png"
            result = subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", str(source),
                 "-vf", crop, "-frames:v", "1", str(target)],
                capture_output=True, timeout=CROP_TIMEOUT,
            )
            if result.returncode != 0 or not target.exists():
                detail = result.stderr.decode("utf-8", "replace")[:300]
                raise ImageGenError(
                    f"그리드 4분할에 실패했다 (ffmpeg, q{index}): {detail}"
                )
            quadrants.append(target.read_bytes())
        return tuple(quadrants)


def _fail(status: int, body: bytes, *, what: str) -> ImageGenError:
    """HTTP 상태 → 예외. 프로바이더 전체의 문제와 씬 하나의 실패를 가른다."""
    detail = body.decode("utf-8", "replace")[:400]
    if status == 429:
        return ImageGenRateLimited(f"{what}: 사용 한도 도달 (429): {detail}")
    if status in (401, 403):
        return ProviderNotConfigured(
            f"{what}: 인증 거부 ({status}): {detail}. "
            f"{SECRET_ENV}가 프록시의 mj-api-secret과 같은지 확인하라"
        )
    return ImageGenError(f"{what}: HTTP {status}: {detail}")


class MidjourneyClient(ImageClient):
    """ADR-0025가 고른 실물 이미지 경로. 프록시 REST를 직접 친다."""

    #: MJ 경로는 스타일 앵커를 쓰지 않는다 (ADR-0025 G3 — `--sref`가 마감을 망가뜨렸다).
    #: 앵커 0장이 정상이므로 단계가 앵커 없음으로 막으면 안 된다.
    requires_style_anchors = False
    name = "midjourney"
    #: **필수 선언.** 이 값이 없으면 `[6]`의 방언 대조가 무력해지고, MJ 문법이 아닌
    #: 문자열이 편당 과금을 쓴 뒤에야 실패한다 (ADR-0027).
    dialect = "mj"
    #: `imageUrls[0]`이 주는 `cdn.midjourney.com/.../0_0.png`가 PNG다 (실측).
    output_suffix = ".png"
    #: 캐릭터 시트 참조를 만들 수 있다 (ADR-0051 — U1 업스케일의 Discord CDN 서명 URL).
    supports_character_reference = True

    def __init__(
        self,
        *,
        base_url: str | None = None,
        secret: str | None = None,
        transport: Transport = urllib_transport,
        poll_interval: float = POLL_INTERVAL,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        ffmpeg: str = FFMPEG,
    ) -> None:
        self.ffmpeg = ffmpeg
        self.base_url = (base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL).rstrip("/")
        self.secret = secret or os.environ.get(SECRET_ENV) or DEFAULT_SECRET
        self.transport = transport
        self.poll_interval = poll_interval
        self.sleep = sleep
        self.clock = clock
        #: 계정 응답 메모. `coreSize`와 `timeoutMinutes`가 같은 응답에 있어 씬마다
        #: 다시 물을 이유가 없다 — 26씬이면 관리 호출이 26번이 된다.
        self._accounts: list[Any] | None = None

    @property
    def headers(self) -> dict[str, str]:
        return {AUTH_HEADER: self.secret, "Content-Type": "application/json"}

    def submit(self, request: ImageRequest, *, timeout: int) -> str:
        """잡을 제출하고 taskId를 돌려준다."""
        body = json.dumps(
            {"prompt": build_prompt(request)}, ensure_ascii=False
        ).encode("utf-8")
        status, raw = self.transport(
            "POST", f"{self.base_url}{SUBMIT_PATH}", self.headers, body, timeout
        )
        if status != 200:
            raise _fail(status, raw, what="제출")

        payload = _load(raw, what="제출 응답")
        code = payload.get("code")
        task_id = payload.get("result")
        if code not in SUBMIT_OK or not task_id:
            raise ImageGenError(
                f"제출이 거절됐다 (code={code}): {payload.get('description')!r}"
            )
        return str(task_id)

    def upload(self, data: bytes, *, mime_type: str, timeout: int | None = None) -> str:
        """우리 이미지 한 장 → **MJ가 닿는 공개 https 주소** (ADR-0071).

        `[6]`의 INFO 이미지가 이 길로 나간다. MJ 영상 엔드포인트는 자기가 내려받을 수
        있는 주소만 받고 base64는 거절하므로(ADR-0070 실측) 업로드가 유일한 길이다.
        저장 위치는 프록시 설정의 몫이라 여기서는 돌려받은 주소만 본다 — https가
        아니면 **여기서** 실패한다. 잡을 사기 전에 걸러야 사유가 타임아웃으로 오지 않는다.
        """
        budget = timeout or self.wait_budget()
        payload = base64.b64encode(data).decode("ascii")
        body = json.dumps(
            {"base64Array": [f"data:{mime_type};base64,{payload}"]}, ensure_ascii=False
        ).encode("utf-8")
        status, raw = self.transport(
            "POST", f"{self.base_url}{UPLOAD_PATH}", self.headers, body, budget
        )
        if status != 200:
            raise _fail(status, raw, what="이미지 업로드")
        result = _load(raw, what="업로드 응답")
        urls = result.get("result")
        if result.get("code") not in SUBMIT_OK or not isinstance(urls, list) or not urls:
            raise ImageGenError(
                f"업로드가 거절됐다 (code={result.get('code')}): {result.get('description')!r}"
            )
        url = str(urls[0])
        if not url.startswith("https://"):
            raise ProviderNotConfigured(
                f"업로드 주소가 https가 아니다: {url!r}. MJ는 자기가 닿는 공개 주소만 받는다 "
                "— 프록시 imageStorageType을 R2/S3로 두어야 한다 (ADR-0070)"
            )
        return url

    def fetch(self, task_id: str, *, timeout: int) -> dict[str, Any]:
        """태스크 1회 조회."""
        url = f"{self.base_url}{FETCH_PATH.format(task_id=task_id)}"
        status, raw = self.transport("GET", url, self.headers, None, timeout)
        if status != 200:
            raise _fail(status, raw, what=f"태스크 {task_id} 조회")
        return _load(raw, what="태스크 응답")

    def concurrency(self) -> int:
        """활성 계정의 `coreSize`. `[6]`의 워커 수 기본값이다 (ADR-0031 §4·G3).

        이미지는 fast로 제출한다 (ADR-0039 §2) — 그 큐의 동시 한도가 `coreSize`다.
        relax 한도인 `relaxCoreSize`는 relax로 가는 `[7]` 영상이 쓴다.

        **한 번도 예외를 올리지 않는다.** 이 값은 얼마나 빨리 돌릴지를 정할 뿐이고,
        못 읽었다고 그림을 못 만드는 것이 아니다. 못 읽으면 1로 떨어져 지금까지의
        순차 동작이 된다 — 느려질 뿐 틀리지 않는다.

        활성 계정이 여럿이면 **가장 작은 값**을 쓴다. 잡이 어느 계정으로 갈지는
        프록시가 정하므로, 큰 쪽에 맞추면 작은 쪽 계정이 429를 낸다.
        """
        try:
            sizes = self._account_numbers("coreSize")
        except (ImageGenError, OSError, KeyError, TypeError, ValueError) as exc:
            # 비밀이 섞인 응답이라 본문을 로그에 싣지 않는다 (ADR-0032 §3).
            log.warning(
                "coreSize를 읽지 못해 워커 1로 간다 (%s: %s). "
                "--jobs로 직접 줄 수 있다", type(exc).__name__, exc,
            )
            return 1
        return max(1, min(sizes))

    def wait_budget(self) -> int:
        """활성 계정의 `timeoutMinutes`를 초로. 잡 하나를 기다리는 상한이다 (ADR-0045).

        **한 번도 예외를 올리지 않는다.** `concurrency()`와 같은 규칙이다 — 못 읽으면
        `FALLBACK_TIMEOUT`으로 떨어진다. 넉넉한 폴백이 위험하지 않은 이유는 프록시가
        자기 `timeoutMinutes`를 그대로 집행하기 때문이다: 우리가 더 오래 기다려도
        잡은 프록시가 먼저 죽인다. 우리 숫자는 상한이 아니라 **상한의 상한**이다.

        활성 계정이 여럿이면 **가장 작은 값**을 쓴다. 잡이 어느 계정으로 갈지는
        프록시가 정하므로, 큰 쪽에 맞추면 작은 쪽 계정의 잡을 산 채로 기다리게 된다.
        """
        try:
            minutes = self._account_numbers("timeoutMinutes")
        except (ImageGenError, OSError, KeyError, TypeError, ValueError) as exc:
            log.warning(
                "timeoutMinutes를 읽지 못해 %d초로 간다 (%s: %s). "
                "--timeout으로 직접 줄 수 있다",
                FALLBACK_TIMEOUT, type(exc).__name__, exc,
            )
            return FALLBACK_TIMEOUT
        return max(1, min(minutes) * 60)

    def cancel(self, task_id: str) -> bool:
        """포기한 잡을 프록시에서 죽인다 (ADR-0045).

        **실패를 삼킨다.** 취소는 자원 회수이지 이 씬의 성패가 아니다 — 여기서 예외를
        올리면 호출부의 폴백 사다리가 끊긴다. 이미 끝난 잡에 걸어도 무해하다.
        """
        url = f"{self.base_url}{CANCEL_PATH.format(task_id=task_id)}"
        try:
            status, _ = self.transport("POST", url, self.headers, None, CANCEL_TIMEOUT)
        except Exception as exc:  # 취소 실패로 씬을 죽이지 않는다
            log.warning("태스크 %s 취소를 보내지 못했다 (%s)", task_id, type(exc).__name__)
            return False
        if status != 200:
            log.warning("태스크 %s 취소가 거절됐다 (HTTP %s)", task_id, status)
            return False
        log.info("태스크 %s를 취소했다 — 버려두면 프록시가 끝까지 돈다", task_id)
        return True

    def _account_numbers(self, field: str) -> list[int]:
        """활성 계정에서 숫자 필드 하나를 모은다. 계정 응답은 요청당 1회만 읽는다."""
        if self._accounts is None:
            status, raw = self.transport(
                "POST", f"{self.base_url}{ACCOUNTS_PATH}", self.headers,
                b"{}", ACCOUNTS_TIMEOUT,
            )
            if status != 200:
                raise ImageGenError(f"HTTP {status}")
            accounts = _load(raw, what="계정 목록").get("list")
            if not isinstance(accounts, list):
                raise ImageGenError("응답에 list가 없다")
            self._accounts = accounts
        values = [
            int(a[field])
            for a in self._accounts
            if isinstance(a, dict) and a.get("enable") and a.get(field)
        ]
        if not values:
            raise ImageGenError(f"활성 계정에 {field}가 없다")
        return values

    def download(self, url: str, *, timeout: int) -> bytes:
        """산출 이미지 바이트. 인증 헤더 대신 **CDN 신원**을 붙인다 (`transport.CDN_HEADERS`) —
        R2 저장(ADR-0070)에서는 기본 UA가 Cloudflare 403으로 막힌다."""
        status, raw = self.transport("GET", url, dict(CDN_HEADERS), None, timeout)
        if status != 200:
            raise _fail(status, raw, what="이미지 내려받기")
        return raw

    def character_reference(
        self, task_id: str, *, timeout: int | None = None
    ) -> CharacterReference:
        """시트 잡 → U1 업스케일 → 단일 이미지의 CDN URL (ADR-0051 G1·G3)."""
        return self.upscale(task_id, quadrant=0, timeout=timeout)

    def upscale(
        self, task_id: str, *, quadrant: int = 0, timeout: int | None = None
    ) -> CharacterReference:
        """그리드 잡 → U{quadrant+1} → **낱장 이미지의 공개 URL** (ADR-0051 G3, ADR-0071).

        `[6]`이 CLEAN을 만들 때 부른다 — MJ imagine이 주는 것은 2×2 그리드이고, 영상
        엔드포인트도 NB2 편집도 낱장을 받아야 한다. 그리드를 그대로 넘기면 MJ가
        "네 컷 콜라주"로 읽는다 (ADR-0070).

        **멱등이다.** 완료된 업스케일이 있으면 그것을 재사용한다 — 같은 업스케일을 짧은
        간격에 재요청하면 MJ가 `Slow Down!`으로 거절한다 (실측 2026-08-21). URL은 부를
        때마다 태스크에서 새로 읽는다 — 서명 URL이라 만료가 있다.
        """
        budget = timeout or self.wait_budget()
        marker = UPSCALE_MARKER_FORMAT.format(n=quadrant + 1)
        existing = self._find_completed_upscale(
            task_id, marker=marker, allow_unknown=quadrant == 0, timeout=budget
        )
        if existing is not None:
            return existing

        parent = self.fetch(task_id, timeout=budget)
        custom_id = next(
            (
                str(button.get("customId"))
                for button in (parent.get("buttons") or [])
                if isinstance(button, dict)
                and marker in str(button.get("customId"))
            ),
            None,
        )
        if not custom_id:
            available = [
                b.get("customId")
                for b in (parent.get("buttons") or [])
                if isinstance(b, dict) and "upsample" in str(b.get("customId"))
            ]
            raise ImageGenError(
                f"태스크 {task_id}에 U{quadrant + 1} 버튼이 없다 (있는 것: {available})"
            )

        body = json.dumps(
            {"taskId": task_id, "customId": custom_id}, ensure_ascii=False
        ).encode("utf-8")
        status, raw = self.transport(
            "POST", f"{self.base_url}{ACTION_PATH}", self.headers, body, budget
        )
        if status != 200:
            raise _fail(status, raw, what="업스케일 제출")
        payload = _load(raw, what="업스케일 제출 응답")
        code = payload.get("code")
        upscale_id = payload.get("result")
        if code not in SUBMIT_OK or not upscale_id:
            raise ImageGenError(
                f"업스케일이 거절됐다 (code={code}): {payload.get('description')!r}"
            )

        deadline = self.clock() + budget
        while True:
            task = self.fetch(str(upscale_id), timeout=budget)
            state = str(task.get("status") or "")
            if state == STATUS_SUCCESS:
                break
            if state in STATUS_FAILED:
                raise ImageGenError(
                    f"업스케일 {upscale_id}가 {state}로 끝났다: "
                    f"{task.get('failReason') or '사유 없음'}"
                )
            if self.clock() >= deadline:
                # 버려두면 프록시가 끝까지 돈다 (ADR-0045 — generate와 같은 규칙).
                self.cancel(str(upscale_id))
                raise ImageGenTimeout(
                    f"업스케일 {upscale_id}가 {budget}초 안에 끝나지 않았다"
                )
            self.sleep(self.poll_interval)
        return self._reference_from_task(str(upscale_id), task)

    def _find_completed_upscale(
        self, task_id: str, *, marker: str, allow_unknown: bool, timeout: int
    ) -> CharacterReference | None:
        """이미 성공한 **그 사분면의** 업스케일 재사용. 못 읽으면 None — 재사용은
        최적화지 성패가 아니다.

        부모가 같아도 사분면이 다르면 다른 그림이다 (ADR-0031 §2). 그래서 `customId`의
        `upsample::{n}::`까지 맞춰 본다 — 안 맞추면 `[6]`의 재시도로 바꾼 칸이 첫 칸으로
        되돌아온다.

        `allow_unknown`은 **U1을 찾을 때만** 참이다. 응답에 `custom_id`가 없는 프록시가
        있고(옛 응답), 그때 "부모가 같은 완료 업스케일은 U1이다"라는 ADR-0051의 전제가
        남아 있기 때문이다. 다른 칸에는 그 전제가 없으므로 확인되지 않으면 재사용하지
        않는다 — 조용히 다른 그림을 쓰는 것보다 잡 하나를 더 던지는 편이 싸다.
        """
        try:
            status, raw = self.transport(
                "GET", f"{self.base_url}{TASK_LIST_PATH}", self.headers, None, timeout
            )
            if status != 200:
                return None
            tasks: Any = json.loads(raw)
        except Exception:  # 목록 조회 실패로 참조 생성을 죽이지 않는다
            return None
        if isinstance(tasks, dict):
            tasks = tasks.get("list") or []
        if not isinstance(tasks, list):
            return None
        for task in tasks:
            if not isinstance(task, dict):
                continue
            if (
                task.get("action") == "UPSCALE"
                and str(task.get("status")) == STATUS_SUCCESS
                and str(task.get("parentId")) == str(task_id)
                and _quadrant_matches(task, marker, allow_unknown=allow_unknown)
            ):
                try:
                    return self._reference_from_task(str(task.get("id")), task)
                except ImageGenError:
                    continue
        return None

    @staticmethod
    def _reference_from_task(upscale_id: str, task: dict[str, Any]) -> CharacterReference:
        """업스케일 태스크 → 참조. **저장소 주소가 https면 그것을 먼저 쓴다.**

        두 주소가 온다 (실측 2026-08-25):

        | 필드 | 저장 설정 `LOCAL` | 저장 설정 `R2` |
        |---|---|---|
        | `url` | Discord CDN **서명** URL (`?ex=`로 만료) | 같음 |
        | `imageUrl` | `http://localhost:8086/…` — MJ가 못 가져간다 (ADR-0046) | `https://pub-….r2.dev/…` — **우리 버킷, 만료 없음** |

        `[6]`이 적어 둔 주소를 `[7]`이 나중에 쓰므로(단계가 갈려 있다) 만료되는 주소를
        적으면 시간이 지난 재실행에서 죽는다. 그래서 https인 저장소 주소가 있으면 그쪽,
        없으면 서명 URL로 떨어진다 — `http://localhost`는 어느 쪽으로도 채택되지 않는다.
        """
        for field in ("imageUrl", "url"):
            candidate = task.get(field)
            if isinstance(candidate, str) and candidate.startswith("https://"):
                return CharacterReference(upscale_task_id=upscale_id, url=candidate)
        raise ImageGenError(
            f"업스케일 {upscale_id}에 MJ가 닿는 https 주소가 없다 "
            f"(url={task.get('url')!r}, imageUrl={task.get('imageUrl')!r}) — "
            "프록시 imageStorageType을 R2/S3로 두어야 한다 (ADR-0070)"
        )

    def generate(
        self, request: ImageRequest, *, timeout: int | None = None
    ) -> GeneratedImage:
        budget = timeout or self.wait_budget()
        deadline = self.clock() + budget

        task_id = self.submit(request, timeout=budget)

        while True:
            payload = self.fetch(task_id, timeout=budget)
            status = str(payload.get("status") or "")
            if status == STATUS_SUCCESS:
                break
            if status in STATUS_FAILED:
                raise ImageGenError(
                    f"태스크 {task_id}가 {status}로 끝났다: "
                    f"{payload.get('failReason') or '사유 없음'}"
                )
            if self.clock() >= deadline:
                # 버려두면 프록시가 끝까지 돌려 GPU를 태운다 (ADR-0045가 ADR-0035의
                # "죽이지 않는다"를 뒤집었다). fast 생성 실측이 7~12초라, 상한까지 간
                # 잡은 느린 것이 아니라 멈춘 것이다 — 26장이 필요한 편에서 버려둔 잡들
                # 때문에 MJ가 73장을 구웠다.
                cancelled = self.cancel(task_id)
                tail = "취소했다" if cancelled else "취소하지 못했다 — 프록시에서 계속 돈다"
                raise ImageGenTimeout(
                    f"태스크 {task_id}가 {budget}초 안에 끝나지 않았다 "
                    f"(마지막 status={status!r}). {tail}"
                )
            self.sleep(self.poll_interval)

        urls, is_grid = result_images(payload)
        if is_grid:
            # 그리드 하나를 받아 로컬에서 넷으로 나눈다. 내려받기는 여전히 1회다.
            url = urls[0]
            suffix = ".webp" if url.lower().split("?")[0].endswith(".webp") else ".png"
            variants = crop_quadrants(
                self.download(url, timeout=budget), suffix=suffix, ffmpeg=self.ffmpeg
            )
        else:
            # 개별 URL 모드는 네 장이 따로 온다. 넷을 다 받는다 — 잡은 이미 샀고
            # 남는 비용은 내려받기뿐인데, 그것을 아끼려고 셋을 버려 온 것이 ADR-0031이
            # 되돌린 결정이다.
            variants = tuple(self.download(u, timeout=budget) for u in urls)

        return GeneratedImage(
            data=variants[0],
            mime_type="image/png",
            request_id=task_id,
            model_id=self.name,
            variants=variants,
            raw={
                "progress": payload.get("progress"),
                "status": status,
                "mode": payload.get("mode"),
                "grid": is_grid,
            },
        )


def _task_custom_id(task: dict[str, Any]) -> str:
    """태스크가 눌린 버튼의 customId. 프록시는 **`properties.custom_id`**에 담는다
    (실측 2026-08-25 — 최상위 `customId`는 `null`이다). 못 읽으면 빈 문자열이고,
    그러면 재사용 대조가 실패해 새로 제출한다 — **조용히 다른 사분면을 쓰지 않는다.**
    """
    properties = task.get("properties")
    if not isinstance(properties, dict):
        return ""
    return str(properties.get("custom_id") or "")


def _quadrant_matches(
    task: dict[str, Any], marker: str, *, allow_unknown: bool
) -> bool:
    """완료된 업스케일이 **우리가 원하는 사분면**인가."""
    custom_id = _task_custom_id(task)
    if custom_id:
        return marker in custom_id
    return allow_unknown


def _load(raw: bytes, *, what: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ImageGenError(f"{what}이 JSON이 아니다: {exc}") from exc
    if not isinstance(payload, dict):
        raise ImageGenError(f"{what}이 객체가 아니다: {type(payload).__name__}")
    return payload
