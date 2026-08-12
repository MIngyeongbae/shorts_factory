"""Midjourney 어댑터 — mjopen 프록시 REST (ADR-0025).

ADR-0025가 프로바이더를, ADR-0027이 프롬프트 방언을 정했다.

- 제출 `POST {base}/mj-relax/mj/submit/imagine`, 인증 헤더 `mj-api-secret`
- 폴링 `GET {base}/mj/task/{id}/fetch` — 잡이 끝날 때까지
- 결과 4장 중 **`imageUrls[0]` 고정** (ADR-0025, 계약 공백 #1)

## Relax는 프롬프트가 아니라 URL 프리픽스로 고른다

`--relax` 플래그를 붙이지 않는다. 프록시가 `/mj-relax/` 엔드포인트에 붙인다 (G1 실측).
**프리픽스를 틀리면 잘못된 프롬프트가 아니라 아예 다른 엔드포인트가 되므로** 조용히
Fast로 새지 않는다 — 이미지 27잡이 Fast로 가면 GPU 27분을 태워 월 산출이 반토막 난다.

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

AUTH_HEADER = "mj-api-secret"

#: 이미지는 relax, 영상은 fast (ADR-0025). 이 어댑터는 이미지 전용이다.
SUBMIT_PATH = "/mj-relax/mj/submit/imagine"
FETCH_PATH = "/mj/task/{task_id}/fetch"

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


def result_image(payload: dict[str, Any]) -> tuple[str, bool]:
    """`(내려받을 URL, 4분할이 필요한가)`. 계정 모드마다 응답 모양이 다르다 (실측).

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
        first = items[0]
        url = first.get("url") if isinstance(first, dict) else first
        if not isinstance(url, str) or not url:
            raise ImageGenError(f"imageUrls[0]에서 url을 읽을 수 없다: {first!r}")
        return url, False

    url = payload.get("imageUrl")
    if not isinstance(url, str) or not url:
        raise ImageGenError(
            f"태스크가 SUCCESS인데 이미지 URL이 없다 (키: {sorted(payload)})"
        )
    return url, True


def crop_first_quadrant(
    data: bytes, *, suffix: str = ".webp", ffmpeg: str = FFMPEG
) -> bytes:
    """2×2 그리드에서 왼쪽 위 한 장을 PNG로 잘라 낸다.

    FFmpeg를 쓰는 이유는 **이미 이 프로젝트의 필수 의존이고**(조립·자막 번인) webp를
    읽을 수 있는 유일한 수단이기 때문이다. Pillow를 새로 들이지 않는다.

    `crop=iw/2:ih/2:0:0`은 정수 나눗셈이 아니라 FFmpeg 표현식이라 홀수 픽셀에서도
    안전하다. 실측 그리드는 1632×2912라 나머지가 없다.
    """
    with tempfile.TemporaryDirectory(prefix="mj-grid-") as tmp:
        source = Path(tmp) / f"grid{suffix}"
        target = Path(tmp) / "quadrant.png"
        source.write_bytes(data)
        result = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(source),
             "-vf", "crop=iw/2:ih/2:0:0", "-frames:v", "1", str(target)],
            capture_output=True, timeout=CROP_TIMEOUT,
        )
        if result.returncode != 0 or not target.exists():
            detail = result.stderr.decode("utf-8", "replace")[:300]
            raise ImageGenError(f"그리드 4분할에 실패했다 (ffmpeg): {detail}")
        return target.read_bytes()


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

        url, is_grid = result_image(payload)
        data = self.download(url, timeout=budget)
        if is_grid:
            suffix = ".webp" if url.lower().split("?")[0].endswith(".webp") else ".png"
            data = crop_first_quadrant(data, suffix=suffix, ffmpeg=self.ffmpeg)

        return GeneratedImage(
            data=data,
            mime_type="image/png",
            request_id=task_id,
            model_id=self.name,
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
