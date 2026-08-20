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

import json
import logging
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .base import (
    GeneratedImage,
    ImageClient,
    ImageGenError,
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
FETCH_PATH = "/mj/task/{task_id}/fetch"

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

#: 잡 하나를 기다리는 상한(초)과 폴링 간격(초).
#: **relax는 큐가 밀리면 분 단위다.** 짧게 잡으면 멀쩡히 돌고 있는 잡을 타임아웃으로
#: 죽이고 재시도해서 큐를 두 배로 만든다.
DEFAULT_TIMEOUT = 1800
POLL_INTERVAL = 10

#: 프록시 태스크 상태. `SUCCESS`만 성공이고 나머지 종결 상태는 씬 실패다.
STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = ("FAILURE", "CANCEL")

#: Discord 모드 그리드를 4분할할 때 쓴다. 이미 필수 의존이다 (조립·자막 번인).
FFMPEG = "ffmpeg"
CROP_TIMEOUT = 120

#: `(method, url, headers, body, timeout) -> (status, bytes)`. 유일한 HTTP 경계다.
Transport = Callable[[str, str, dict[str, str], bytes | None, int], "tuple[int, bytes]"]


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: int
) -> tuple[int, bytes]:
    """stdlib 요청. 오류 응답도 본문을 살려 돌려준다 — 프록시가 사유를 적어 준다."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except TimeoutError as exc:
        raise ImageGenTimeout(f"{timeout}초 안에 응답이 오지 않았다") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise ImageGenTimeout(f"{timeout}초 안에 응답이 오지 않았다") from exc
        raise ImageGenError(f"연결 실패: {exc.reason}") from exc


def build_prompt(request: ImageRequest) -> str:
    """MJ에 보낼 한 줄. `prompt` + 공백 + `negative_prompt`.

    `--ar`은 이미 `prompt` 끝에, `--no`는 `negative_prompt` 전체다 (ADR-0027).
    여기서 종횡비나 배제 항목을 다시 만들지 않는다 — 출처가 둘이 되면 갈린다.
    """
    negative = request.negative_prompt.strip()
    prompt = request.prompt.strip()
    return f"{prompt} {negative}" if negative else prompt


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
            status, raw = self.transport(
                "POST", f"{self.base_url}{ACCOUNTS_PATH}", self.headers,
                b"{}", ACCOUNTS_TIMEOUT,
            )
            if status != 200:
                raise ImageGenError(f"HTTP {status}")
            accounts = _load(raw, what="계정 목록").get("list")
            if not isinstance(accounts, list):
                raise ImageGenError("응답에 list가 없다")
            sizes = [
                int(a["coreSize"])
                for a in accounts
                if isinstance(a, dict) and a.get("enable") and a.get("coreSize")
            ]
            if not sizes:
                raise ImageGenError("활성 계정에 coreSize가 없다")
        except (ImageGenError, OSError, KeyError, TypeError, ValueError) as exc:
            # 비밀이 섞인 응답이라 본문을 로그에 싣지 않는다 (ADR-0032 §3).
            log.warning(
                "coreSize를 읽지 못해 워커 1로 간다 (%s: %s). "
                "--jobs로 직접 줄 수 있다", type(exc).__name__, exc,
            )
            return 1
        return max(1, min(sizes))

    def download(self, url: str, *, timeout: int) -> bytes:
        """산출 이미지 바이트. 인증 헤더를 붙이지 않는다 — CDN 주소다."""
        status, raw = self.transport("GET", url, {}, None, timeout)
        if status != 200:
            raise _fail(status, raw, what="이미지 내려받기")
        return raw

    def generate(
        self, request: ImageRequest, *, timeout: int | None = None
    ) -> GeneratedImage:
        budget = timeout or DEFAULT_TIMEOUT
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
                # 잡은 프록시에서 계속 돈다. 죽이지 않고 taskId를 남겨 둔다 —
                # relax 큐가 밀린 것뿐이면 같은 잡을 또 제출하는 것이 더 나쁘다.
                raise ImageGenTimeout(
                    f"태스크 {task_id}가 {budget}초 안에 끝나지 않았다 "
                    f"(마지막 status={status!r}). 프록시에서는 계속 돈다"
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


def _load(raw: bytes, *, what: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ImageGenError(f"{what}이 JSON이 아니다: {exc}") from exc
    if not isinstance(payload, dict):
        raise ImageGenError(f"{what}이 객체가 아니다: {type(payload).__name__}")
    return payload
