"""Veo 영상 어댑터 — Gemini API `predictLongRunning`을 REST로 직접 친다. ADR-0043.

인포씬 전용이다: 시작 프레임(CLEAN) + 끝 프레임(INFO)을 **base64 인라인**으로 넣어
그래픽이 조립되는 클립을 만든다. MJ 영상의 "닿는 URL" 왕복(UPSCALE 사슬)이 이 경로에는
없다 — 로컬 파일이 곧 입력이다.

- 제출 `POST /v1beta/models/{model}:predictLongRunning`, 인증 헤더 `x-goog-api-key`
- 폴링 `GET /v1beta/{operation_name}` — `done: true`까지
- 결과는 URI 또는 base64. URI면 같은 키로 내려받는다

## SDK를 쓰지 않는다

ADR-0021과 같은 판단이다 — 엔드포인트 둘에 의존성을 들이지 않고, HTTP 경계를 공용
`transport.py` 하나로 좁혀 테스트가 실제 호출 없이 전 경로를 검증한다. `omni.py`(A2)가
같은 경계를 쓴다 (ADR-0056 결정 2).

## 응답 파싱은 관용적으로 읽고 요란하게 실패한다

Veo 응답의 영상 위치는 버전에 따라 `generateVideoResponse.generatedSamples[].video` /
`generatedVideos[].video` 꼴이 오간다. 아는 모양을 전부 훑되, **없으면 응답의 최상위
키를 담아 실패한다** — 형태가 바뀌었을 때 추측이 아니라 한 줄짜리 오류로 드러나야 한다
(응답 모양을 박제하지 않는 태도 — ADR-0021).

## 첫 실호출로 확정된 것 (2026-08-20 프로브, ADR-0043 G2·G3)

- **순차 조립은 실재한다** — 앵커 → 지시선 → 라벨 순으로 약 5초에 수렴한다. 모핑이
  아니다. 다만 그 순서를 만드는 것은 `ASSEMBLY_PROMPT`다: 맨 프롬프트로 부르면 같은
  first/last여도 마지막 0.5초에 한꺼번에 뜬다
- **클립 규격** 720×1280 / 24fps / h264+AAC / 요청 길이 정확히. 오디오는 버린다(`[10]` 소관)
- **`lastFrame`은 8초 전용** — `INTERPOLATION_DURATION` 참조
- ⚠ **끝 0.3초는 믿지 않는다** — 끝 프레임이 INFO에 안착하지 못하고 글자가 뭉개지는
  것을 봤다(n=1). `[9]`의 trim이 씬 길이로 자르므로 실사용 구간엔 안 들어오지만,
  **씬이 길어 8초에 가까워지면 그 꼬리가 화면에 남는다**
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from ..config import MissingCredential, require_env
from ..transport import Transport, TransportError, TransportTimeout, urllib_transport
from .base import (
    GeneratedClip,
    VideoClient,
    VideoGenError,
    VideoGenRateLimited,
    VideoGenTimeout,
    VideoProviderNotConfigured,
    VideoRequest,
)

log = logging.getLogger(__name__)

API_KEY_ENV = "GEMINI_API_KEY"
#: 티어 선택. 실단가 미확정이라(ADR-0043 G3) 환경변수로 바꿔 끼울 수 있게 둔다.
MODEL_ENV = "VEO_MODEL"

#: 기본 티어. fast가 확인된 것 중 가장 싸다 — lite는 품질 미확인이라 기본이 아니다.
#: 2026-08-20 조회 실측: veo-3.1-generate / veo-3.1-fast-generate / veo-3.1-lite-generate.
DEFAULT_MODEL = "veo-3.1-fast-generate-preview"

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

#: Veo가 받는 클립 길이(초). 씬 실측 길이 + 0.6초를 덮는 최솟값을 고른다 (ADR-0043).
DURATIONS = (4, 6, 8)

#: **`lastFrame`을 넣으면 8초 하나뿐이다** (2026-08-20 실측).
#:
#: 4초·6초는 `Your use case is currently not supported`로 400이고, 화면비·해상도·티어
#: (fast·lite·standard)를 바꿔도 같다 — 길이만이 변수다. 공식 문서의 "8초여야 한다"
#: 목록(extension·reference images·1080p·4k)에 **interpolation이 빠져 있어서** 첫 구현이
#: 씬 길이에 맞춰 4·6을 골랐고, 그 결과 인포씬이 한 편도 빠짐없이 400으로 죽었다.
#:
#: 남는 꼬리는 `[9]`가 `trim`으로 자른다 — 8초는 **호출 단위이지 클립 길이가 아니다**.
INTERPOLATION_DURATION = 8

#: 세로 쇼츠 고정 (specs/00).
ASPECT_RATIO = "9:16"

#: 프레임 파일 확장자 → MIME. CLEAN은 MJ의 PNG, INFO는 NB2의 JPEG다 (ADR-0021).
FRAME_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

#: 폴링 간격·기본 상한(초). 상한의 주인은 어댑터다 (ADR-0035) — 단계가 선언하지 않는다.
POLL_INTERVAL = 10.0
DEFAULT_TIMEOUT = 900

#: 조립 지시. 카메라 구절(`{camera}`)은 씬 계약에서 오고, 나머지는 목표물의 클립 구조다
#: (ADR-0043 — 공학 쇼츠 워크플로우 11절). 프로바이더 프롬프팅은 어댑터 소관이라 여기
#: 있다 — MJ 어댑터가 `--bs`를 아는 것과 같은 자리다.
ASSEMBLY_PROMPT = (
    "Start exactly on the first frame. {camera} "
    "Technical annotation graphics assemble in sequence on top of the live scene: "
    "anchor points light up first, then leader lines and arrows draw themselves, "
    "then the labels and figures appear, converging precisely into the final frame. "
    "The graphics stay locked to the scene in 3D. No morphing, no full-frame "
    "cross-fade, no new text beyond the final frame."
)


def frame_part(path: Path) -> dict[str, str]:
    """프레임 파일 → 인라인 이미지 블록."""
    mime = FRAME_MIME.get(path.suffix.lower())
    if mime is None:
        raise VideoGenError(f"프레임 확장자를 모른다: {path.name}")
    return {
        "bytesBase64Encoded": base64.b64encode(path.read_bytes()).decode("ascii"),
        "mimeType": mime,
    }


def pick_duration(needed: float, *, interpolation: bool = False) -> int:
    """요청할 클립 길이(초).

    **first/last 보간이면 길이를 고르지 않는다 — 8초 하나뿐이다**
    (`INTERPOLATION_DURATION`의 실측 근거 참조). 씬이 4초여도 8초를 사고 `[9]`가 자른다.

    보간이 아니면(첫 프레임만 주는 i2v) 씬 실측 길이 + 0.6초를 덮는 최소 길이를 고른다.
    넘치면 8초로 눌러 담고 호출자가 경고한다.
    """
    if interpolation:
        return INTERPOLATION_DURATION
    for duration in DURATIONS:
        if duration >= needed:
            return duration
    return DURATIONS[-1]


def build_body(request: VideoRequest) -> dict[str, Any]:
    """요청 → `predictLongRunning` 본문."""
    if request.first_frame is None:
        raise VideoGenError(
            f"씬 {request.scene_id}: Veo는 첫 프레임 파일이 필요하다 (ADR-0043) — "
            "잡 id 입력은 MJ 어댑터의 계약이다"
        )

    prompt = ASSEMBLY_PROMPT.format(camera=request.motion_prompt or "The camera holds steady.")
    instance: dict[str, Any] = {
        "prompt": prompt,
        "image": frame_part(Path(request.first_frame)),
    }
    interpolation = request.last_frame is not None
    if interpolation:
        instance["lastFrame"] = frame_part(Path(request.last_frame))

    return {
        "instances": [instance],
        "parameters": {
            "aspectRatio": ASPECT_RATIO,
            "durationSeconds": pick_duration(
                request.duration or DURATIONS[0], interpolation=interpolation
            ),
            "sampleCount": 1,
        },
    }


def _find_videos(node: Any) -> list[dict[str, Any]]:
    """응답 어디에 있든 `video` 블록(uri 또는 base64)을 모은다.

    스키마를 박제하지 않는 이유는 모듈 독스트링에 있다 — preview 모델이라 필드 이름이
    움직이고, 우리가 원하는 것은 정확히 하나(영상 바이트에 닿는 길)다.
    """
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("bytesBase64Encoded") or node.get("uri"):
            found.append(node)
        for value in node.values():
            found.extend(_find_videos(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_find_videos(item))
    return found


def _fail(status: int, body: bytes, *, what: str) -> VideoGenError:
    detail = body.decode("utf-8", "replace")[:400]
    if status == 429:
        return VideoGenRateLimited(f"{what}: 사용 한도 도달 (429): {detail}")
    if status in (401, 403):
        return VideoProviderNotConfigured(
            f"{what}: 인증·권한 거부 ({status}): {detail}. "
            f"{API_KEY_ENV}가 맞는지, **유료 티어인지** 확인하라 — Veo는 유료 전용이다"
        )
    if status == 400 and b"not supported" in body.lower():
        # 파라미터 거부(길이·화면비·lastFrame 미지원 등)는 씬이 아니라 어댑터 전체의
        # 문제다 — 같은 본문으로 씬 수만큼 실패하는 것은 결과가 아니라 소음이다.
        return VideoProviderNotConfigured(f"{what}: 파라미터 거부 (400): {detail}")
    return VideoGenError(f"{what}: HTTP {status}: {detail}")


class VeoClient(VideoClient):
    """ADR-0043의 인포씬 영상 경로. 목표물(공학 쇼츠 워크플로우)이 쓴 그 계열이다."""

    name = "veo"
    output_suffix = ".mp4"
    #: 입력이 로컬 파일이라 `image_source.json`을 읽지 않는다 (ADR-0043).
    source_provider = ""

    def __init__(
        self,
        *,
        model_id: str | None = None,
        api_key: str | None = None,
        base_url: str = BASE_URL,
        transport: Transport = urllib_transport,
        poll_interval: float = POLL_INTERVAL,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.model_id = model_id or os.environ.get(MODEL_ENV) or DEFAULT_MODEL
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.poll_interval = poll_interval
        self._sleep = sleep
        self._clock = clock
        self._api_key = api_key

    @property
    def api_key(self) -> str:
        """키는 첫 호출에서 읽는다 — 어댑터를 만드는 것만으로(`--help`) 실패하지 않는다."""
        if self._api_key:
            return self._api_key
        try:
            self._api_key = require_env(API_KEY_ENV, purpose="Veo 인포씬 영상 생성")
        except MissingCredential as exc:
            raise VideoProviderNotConfigured(str(exc)) from exc
        return self._api_key

    def concurrency(self) -> int:
        """동시 잡 수. API가 한도를 노출하지 않으므로 보수적으로 2다 —
        인포씬은 편당 소수라(ADR-0043) 병렬이 벽시계를 정하지 않는다.
        """
        return 2

    # --- HTTP -------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

    def _send(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None,
        *, timeout: int, what: str,
    ) -> tuple[int, bytes]:
        """transport 호출 — 연결·타임아웃 오류를 이 어댑터의 예외로 바꾼다 (ADR-0056)."""
        try:
            return self.transport(method, url, headers, body, timeout)
        except TransportTimeout as exc:
            raise VideoGenTimeout(f"{what}: {exc}") from exc
        except TransportError as exc:
            raise VideoGenError(f"{what}: {exc}") from exc

    def _request(
        self, method: str, url: str, body: dict[str, Any] | None,
        *, timeout: int, what: str,
    ) -> dict[str, Any]:
        raw_body = json.dumps(body).encode("utf-8") if body is not None else None
        status, raw = self._send(
            method, url, self._headers(), raw_body, timeout=timeout, what=what
        )
        if status != 200:
            raise _fail(status, raw, what=what)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise VideoGenError(f"{what}: JSON이 아닌 200 응답이다: {exc}") from exc

    def _download(self, uri: str, *, timeout: int) -> bytes:
        status, raw = self._send(
            "GET", uri, {"x-goog-api-key": self.api_key}, None,
            timeout=timeout, what="영상 내려받기",
        )
        if status != 200:
            raise _fail(status, raw, what="영상 내려받기")
        return raw

    # --- 공개 API ---------------------------------------------------------

    def generate(
        self, request: VideoRequest, *, timeout: int | None = None
    ) -> GeneratedClip:
        budget = timeout or DEFAULT_TIMEOUT
        started = self._clock()
        what = f"씬 {request.scene_id} Veo 제출"

        submit_url = f"{self.base_url}/models/{self.model_id}:predictLongRunning"
        body = build_body(request)
        chosen_duration = float(body["parameters"]["durationSeconds"])
        operation = self._request(
            "POST", submit_url, body, timeout=budget, what=what,
        )
        name = operation.get("name")
        if not name:
            raise VideoGenError(
                f"{what}: 응답에 operation name이 없다 (최상위 키: {sorted(operation)})"
            )

        # 폴링 — `done: true`까지. 남은 예산 안에서만 돈다 (ADR-0035).
        while not operation.get("done"):
            elapsed = self._clock() - started
            if elapsed >= budget:
                raise VideoGenTimeout(
                    f"씬 {request.scene_id}: Veo 잡이 {budget}초 안에 끝나지 않았다 ({name})"
                )
            self._sleep(self.poll_interval)
            operation = self._request(
                "GET", f"{self.base_url}/{name}", None,
                timeout=budget, what=f"씬 {request.scene_id} Veo 폴링",
            )

        error = operation.get("error")
        if error:
            raise VideoGenError(
                f"씬 {request.scene_id}: Veo 잡 실패 — {error.get('message') or error}"
            )

        response = operation.get("response") or {}
        videos = _find_videos(response)
        if not videos:
            raise VideoGenError(
                f"씬 {request.scene_id}: 응답에 영상이 없다 "
                f"(response 최상위 키: {sorted(response)})"
            )

        video = videos[0]
        if video.get("bytesBase64Encoded"):
            data = base64.b64decode(video["bytesBase64Encoded"])
        else:
            remaining = max(int(budget - (self._clock() - started)), 30)
            data = self._download(str(video["uri"]), timeout=remaining)

        return GeneratedClip(
            data=data,
            request_id=name,
            model_id=self.model_id,
            duration=chosen_duration,
            raw={"operation": name},
        )
