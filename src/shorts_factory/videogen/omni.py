"""Gemini Omni Flash 텍스트→영상 어댑터 — Google Interactions API를 REST로 직접 친다. ADR-0056 결정 2.

`[7. videogen]`의 주 경로다. 씬당 프롬프트 하나로 클립 하나를 만든다 — 참조 프레임도
잡 id도 없다. `veo.py`와 같은 경계(공용 `transport.py`, SDK 없음, 관용적 파싱 + 요란한
실패)이고, 호출 규약은 2026-08-22 문서 확인 + 프로브 실측(`runs/20260822-omni-api-probe/`)이다.

- 제출 `POST {BASE_URL}/interactions`, 헤더 `x-goog-api-key`
- 본문 `{"model", "input": "<prompt>", "response_format": {"type": "video", "aspect_ratio",
  "delivery": "uri", "duration": "{N}s"}}`
- 응답은 **동기**다 (프로브 28초). 영상은 `steps[*].content[*]`의 `uri`(또는 인라인 `data`,
  4MB 이하). uri는 `…/v1beta/files/{id}:download?alt=media` 꼴이고 **같은 키로 GET하면 곧
  mp4**다 — 폴링이 필요 없었다. 혹시 파일 리소스(JSON, `state`)가 오면 `ACTIVE`까지 기다린다

## 길이 필드 — 프로브로 확정한 것과 아닌 것 (2026-08-22)

`response_format.duration_seconds`·최상위 `duration_seconds`는 `Unknown parameter`(400)다.
**`response_format.duration`은 이름을 안다** — 정수 `3`은 `Invalid input`이고, protobuf
Duration 문자열 **`"3s"`**(또는 `{"seconds": 3}`)이면 본문 검증을 통과한다(잘못된 모델명으로
404를 받아 본문 검증만 무료로 확인했다). **그 필드가 길이를 실제로 바꾸는지는 재지
않았다** (n=0 — 유료 호출은 1회로 제한했다). 유료 1회는 필드 없이 프롬프트의
"A 3-second …"만으로 **3.008초**가 왔다 (n=1).

그래서 어댑터는 **둘 다 보낸다**: `duration: "{N}s"` + FORMAT 절의 "An N-second". 첫 실편에서
`clips.json`의 요청 초 수와 `provider_seconds`(ffprobe)를 대조하면 필드의 효과가 확정된다.
필드가 런타임에 거절되면(`Invalid input`) 파라미터 거부라 프로바이더 전체 문제로 승격한다
— 그때는 `send_duration=False`로 만들어 프롬프트만으로 간다.

## 클립 규격 (프로브 실측)

720×1280 / 24fps / h264 + **aac 오디오**. 오디오는 버린다 (스펙 05 `[7]` — `[10]` 소관).
규격 맞추기(1080×1920·30fps·무음·정확한 길이)는 `[7]`의 정규화가 한다 (`video/clips.py`).

## 응답 파싱은 관용적으로 읽고 요란하게 실패한다

preview 모델이라 필드 이름이 움직일 수 있다. 아는 모양(`uri`·`data`를 가진 content 블록)을
전부 훑되, 없으면 응답의 **최상위 키**를 담아 실패한다 — 추측하지 않는다 (ADR-0021 태도).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any, Callable

from ..config import MissingCredential, require_env
from ..schemas import vocab
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
#: 모델 id 덮어쓰기. preview 모델이라 이름이 바뀔 수 있다 (ADR-0056 되돌릴 조건 2).
MODEL_ENV = "OMNI_MODEL"
DEFAULT_MODEL = "gemini-omni-flash-preview"

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
INTERACTIONS = "interactions"

#: 영상을 파일 uri로 받는다 (4MB 넘는 클립은 인라인이 안 된다).
DELIVERY = "uri"
RESPONSE_TYPE = "video"

#: 세로 쇼츠. 값은 어휘의 `style.aspect_ratio`다 — 손으로 적지 않는다 (ADR-0034).
ASPECT_RATIO: str = str(vocab.style("aspect_ratio"))

#: Omni가 받는 클립 길이(초) 범위 (ADR-0043이 잰 3~10초 자유 길이, 스펙 05 `[7]`).
MIN_SECONDS = 3
MAX_SECONDS = 10

#: 동기 호출 1회의 상한(초). 프로브 28~40초 — 여유를 둔다. 주인은 어댑터다 (ADR-0035).
DEFAULT_TIMEOUT = 300
#: 파일 리소스(JSON)가 왔을 때의 폴링 간격(초).
POLL_INTERVAL = 5.0

#: 동시 제출 기본값. API가 한도를 노출하지 않는다 — 429가 오면 `[7]`이 1로 줄인다 (스펙 05).
DEFAULT_CONCURRENCY = 4


def duration_value(seconds: int) -> str:
    """`response_format.duration`의 값 — protobuf Duration 문자열 `"{N}s"` (모듈 독스트링)."""
    return f"{int(seconds)}s"


def build_body(
    request: VideoRequest, *, model: str, send_duration: bool = True
) -> dict[str, Any]:
    """요청 → Interactions 본문. 프롬프트는 `[5]`의 것 그대로다 — 여기서 문장을 더하지 않는다."""
    if not request.prompt.strip():
        raise VideoGenError(f"씬 {request.scene_id}: 프롬프트가 비어 있다")
    if request.seconds and not MIN_SECONDS <= request.seconds <= MAX_SECONDS:
        raise VideoGenError(
            f"씬 {request.scene_id}: 클립 길이 {request.seconds}초는 Omni 범위"
            f"({MIN_SECONDS}~{MAX_SECONDS}) 밖이다 — [7]이 클램프했어야 한다"
        )
    response_format: dict[str, Any] = {
        "type": RESPONSE_TYPE,
        "aspect_ratio": ASPECT_RATIO,
        "delivery": DELIVERY,
    }
    if send_duration and request.seconds:
        response_format["duration"] = duration_value(request.seconds)
    return {"model": model, "input": request.prompt, "response_format": response_format}


def find_videos(node: Any) -> list[dict[str, Any]]:
    """응답 어디에 있든 영상 블록(`uri` 또는 인라인 `data`)을 모은다.

    문서의 자리는 `steps[*].content[*]`지만 스키마를 박제하지 않는다 — 원하는 것은 정확히
    하나(영상 바이트에 닿는 길)다. `type`/`mime_type`이 있으면 영상인 것만 고른다.
    """
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        has_payload = bool(node.get("uri") or node.get("data"))
        kind = str(node.get("type") or "")
        mime = str(node.get("mime_type") or node.get("mimeType") or "")
        looks_video = (kind == RESPONSE_TYPE) or mime.startswith("video/") or (not kind and not mime)
        if has_payload and looks_video:
            found.append(node)
        for value in node.values():
            found.extend(find_videos(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(find_videos(item))
    return found


def _fail(status: int, body: bytes, *, what: str) -> VideoGenError:
    detail = body.decode("utf-8", "replace")[:400]
    if status == 429:
        return VideoGenRateLimited(f"{what}: 사용 한도 도달 (429): {detail}")
    if status in (401, 403):
        return VideoProviderNotConfigured(
            f"{what}: 인증·권한 거부 ({status}): {detail}. "
            f"{API_KEY_ENV}가 맞는지, **유료 티어인지** 확인하라"
        )
    if status == 404 and b"model" in body.lower():
        return VideoProviderNotConfigured(
            f"{what}: 모델을 찾을 수 없다 (404): {detail}. {MODEL_ENV}로 바꿔 끼워라 — "
            "preview 모델이라 이름이 움직인다 (ADR-0056 되돌릴 조건 2)"
        )
    if status == 400 and (
        b"unknown parameter" in body.lower() or b"invalid input" in body.lower()
    ):
        # 본문 스키마 거부는 씬이 아니라 어댑터 전체의 문제다 — 같은 본문으로 씬 수만큼
        # 실패하는 것은 결과가 아니라 소음이다 (veo.py와 같은 판단).
        return VideoProviderNotConfigured(f"{what}: 파라미터 거부 (400): {detail}")
    return VideoGenError(f"{what}: HTTP {status}: {detail}")


class OmniClient(VideoClient):
    """ADR-0056의 주 영상 경로. 씬당 프롬프트 1개 → 클립 1개."""

    name = "omni"
    output_suffix = ".mp4"
    source_provider = ""
    min_seconds = MIN_SECONDS
    max_seconds = MAX_SECONDS

    def __init__(
        self,
        *,
        model_id: str | None = None,
        api_key: str | None = None,
        base_url: str = BASE_URL,
        transport: Transport = urllib_transport,
        send_duration: bool = True,
        concurrency: int = DEFAULT_CONCURRENCY,
        poll_interval: float = POLL_INTERVAL,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.model_id = model_id or os.environ.get(MODEL_ENV) or DEFAULT_MODEL
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.send_duration = send_duration
        self._concurrency = max(1, int(concurrency))
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
            self._api_key = require_env(API_KEY_ENV, purpose="Omni Flash 영상 생성 (ADR-0056)")
        except MissingCredential as exc:
            raise VideoProviderNotConfigured(str(exc)) from exc
        return self._api_key

    def concurrency(self) -> int:
        return self._concurrency

    # --- HTTP -------------------------------------------------------------

    def _send(
        self, method: str, url: str, body: bytes | None, *, timeout: int, what: str,
    ) -> tuple[int, bytes]:
        headers = {"x-goog-api-key": self.api_key}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            return self.transport(method, url, headers, body, timeout)
        except TransportTimeout as exc:
            raise VideoGenTimeout(f"{what}: {exc}") from exc
        except TransportError as exc:
            raise VideoGenError(f"{what}: {exc}") from exc

    def _request_json(
        self, method: str, url: str, body: dict[str, Any] | None, *, timeout: int, what: str,
    ) -> dict[str, Any]:
        raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        status, raw = self._send(method, url, raw_body, timeout=timeout, what=what)
        if status != 200:
            raise _fail(status, raw, what=what)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise VideoGenError(f"{what}: JSON이 아닌 200 응답이다: {exc}") from exc

    def _download(self, uri: str, *, budget_until: float, what: str) -> bytes:
        """uri → mp4 바이트. 바로 mp4면 끝이고, 파일 리소스(JSON)면 ACTIVE까지 기다린다."""
        seen_uri = uri
        while True:
            remaining = max(int(budget_until - self._clock()), 30)
            status, raw = self._send("GET", seen_uri, None, timeout=remaining, what=what)
            if status != 200:
                raise _fail(status, raw, what=what)
            if GeneratedClip.looks_like_mp4(raw):
                return raw
            try:
                resource = json.loads(raw)
            except json.JSONDecodeError:
                raise VideoGenError(f"{what}: mp4도 JSON도 아닌 응답이다 ({len(raw)}바이트)")
            state = str(resource.get("state") or (resource.get("file") or {}).get("state") or "")
            if state and state != "ACTIVE":
                if self._clock() >= budget_until:
                    raise VideoGenTimeout(f"{what}: 파일이 ACTIVE가 되지 않았다 (state={state})")
                self._sleep(self.poll_interval)
                continue
            download = (
                resource.get("downloadUri") or resource.get("download_uri")
                or (resource.get("file") or {}).get("downloadUri")
            )
            if not download:
                download = uri + ("&" if "?" in uri else "?") + "alt=media"
            if download == seen_uri:
                raise VideoGenError(
                    f"{what}: 파일 리소스에서 내려받을 주소를 찾지 못했다 (키: {sorted(resource)})"
                )
            seen_uri = download

    # --- 공개 API ---------------------------------------------------------

    def generate(
        self, request: VideoRequest, *, timeout: int | None = None
    ) -> GeneratedClip:
        budget = timeout or DEFAULT_TIMEOUT
        started = self._clock()
        what = f"씬 {request.scene_id} Omni 제출"

        body = build_body(request, model=self.model_id, send_duration=self.send_duration)
        url = f"{self.base_url}/{INTERACTIONS}"
        payload = self._request_json("POST", url, body, timeout=budget, what=what)

        interaction_id = str(payload.get("id") or "")
        status = str(payload.get("status") or "")
        videos = find_videos(payload.get("steps", payload))

        # 동기 응답이 원칙이지만, 미완료 상태로 오면 id로 폴링한다 (budget 안에서).
        while not videos and interaction_id and status and status not in ("completed", "failed", "cancelled"):
            if self._clock() - started >= budget:
                raise VideoGenTimeout(
                    f"씬 {request.scene_id}: Omni 호출이 {budget}초 안에 끝나지 않았다 ({interaction_id})"
                )
            self._sleep(self.poll_interval)
            payload = self._request_json(
                "GET", f"{url}/{interaction_id}", None,
                timeout=budget, what=f"씬 {request.scene_id} Omni 폴링",
            )
            status = str(payload.get("status") or "")
            videos = find_videos(payload.get("steps", payload))

        if status in ("failed", "cancelled"):
            error = payload.get("error") or {}
            raise VideoGenError(
                f"씬 {request.scene_id}: Omni 호출 {status} — "
                f"{error.get('message') if isinstance(error, dict) else error or '사유 없음'}"
            )
        if not videos:
            raise VideoGenError(
                f"씬 {request.scene_id}: 응답에 영상이 없다 (최상위 키: {sorted(payload)}, status={status!r})"
            )

        video = videos[0]
        if video.get("data"):
            try:
                data = base64.b64decode(video["data"])
            except (ValueError, TypeError) as exc:
                raise VideoGenError(f"씬 {request.scene_id}: 인라인 영상 base64를 디코드할 수 없다: {exc}") from exc
        else:
            data = self._download(
                str(video["uri"]), budget_until=started + budget,
                what=f"씬 {request.scene_id} 영상 내려받기",
            )

        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        return GeneratedClip(
            data=data,
            request_id=interaction_id or None,
            model_id=str(payload.get("model") or self.model_id),
            duration=float(request.seconds) if request.seconds else None,
            raw={
                "interaction_id": interaction_id,
                "status": status,
                "total_output_tokens": usage.get("total_output_tokens"),
                "uri": video.get("uri"),
            },
        )
