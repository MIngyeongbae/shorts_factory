"""Nano Banana 2 어댑터 — Gemini Interactions API를 REST로 직접 친다 (ADR-0021).

ADR-0005가 모델을, ADR-0021이 호출 경로를 정했다.

- 엔드포인트 `POST /v1beta/interactions`, 인증 헤더 `x-goog-api-key`
- 모델 `gemini-3.1-flash-image` (= Nano Banana 2)
- 스타일 앵커는 base64 인라인, **상한 3장**
- 종횡비·해상도는 `prompts.json`의 계약값을 그대로 넘긴다 — `9:16`·`2K`가 API 허용값과
  문자열까지 같아서 변환 테이블이 없다 (ADR-0021)

## SDK를 쓰지 않는다

`google-genai`는 `pydantic`·`google-auth`·`httpx`를 끌고 오는데 우리가 쓰는
엔드포인트는 **하나**다. 현재 의존성은 두 줄이고, ADR-0008이 LLM 쪽에서 같은 선택을
이미 했다. 대신 **HTTP 경계를 `transport` 하나로 좁혀** 테스트가 실제 호출 없이
전 경로를 검증한다 (`[9]`의 `run_ffmpeg`와 같은 모양이다).

## 응답 파싱을 여기 한 곳에 몰아 둔다

Interactions API의 응답은 편의 필드(`output_image`)와 단계 배열(`steps[].content[]`)
두 경로로 이미지를 노출한다. 어느 쪽이 오든 읽되, **둘 다 없으면 응답의 최상위 키를
담아 실패한다** — 형태가 바뀌었을 때 추측이 아니라 한 줄짜리 오류로 드러나야 한다.
ADR-0021의 되돌릴 조건("파싱이 두 번 이상 깨지면 SDK로 간다")이 여기에 걸려 있다.
"""

from __future__ import annotations

import base64
import binascii
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from ..config import MissingCredential, require_env
from .base import (
    GeneratedImage,
    ImageClient,
    ImageGenError,
    ImageGenRateLimited,
    ImageGenTimeout,
    ImageRequest,
    ProviderNotConfigured,
)

#: ADR-0005가 고른 모델. preview 접미사 없는 안정 ID다 (ADR-0021).
MODEL_NAME = "gemini-3.1-flash-image"

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"

API_KEY_ENV = "GEMINI_API_KEY"

#: `gemini-3.1-flash-image`의 스타일 레퍼런스 상한 (ADR-0021).
MAX_STYLE_ANCHORS = 3

#: **API가 JPEG만 준다.** `image/png`을 넣으면 400으로 거절한다:
#:   "The value 'image/png' is not supported for 'response_format.mime_type'.
#:    Supported values: 'image/jpeg'."
#: 실호출로 확인했다. 그래서 산출물이 `images/{scene_id}.jpg`가 됐다 (specs/05 갱신).
#: PNG로 변환해 봐야 손실 압축은 이미 일어난 뒤라 화질이 돌아오지 않고 의존성만 는다.
OUTPUT_MIME = "image/jpeg"

#: 앵커 확장자 → MIME. `base.ANCHOR_SUFFIXES`와 짝이다.
ANCHOR_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

DEFAULT_TIMEOUT = 180

#: `(url, headers, body) -> (status, response_bytes)`. 이 프로젝트의 유일한 HTTP 경계다.
Transport = Callable[[str, dict[str, str], bytes, int], "tuple[int, bytes]"]


def urllib_transport(
    url: str, headers: dict[str, str], body: bytes, timeout: int
) -> tuple[int, bytes]:
    """stdlib POST. 오류 응답도 본문을 살려 돌려준다 — 서버가 사유를 적어 준다."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
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


def anchor_part(path: Path) -> dict[str, str]:
    """앵커 파일 → `input` 배열의 이미지 블록."""
    mime = ANCHOR_MIME.get(path.suffix.lower())
    if mime is None:
        raise ProviderNotConfigured(f"앵커 확장자를 모른다: {path.name}")
    return {
        "type": "image",
        "mime_type": mime,
        "data": base64.b64encode(path.read_bytes()).decode("ascii"),
    }


def build_body(request: ImageRequest, *, model_id: str) -> dict[str, Any]:
    """요청 → Interactions API 본문.

    `negative_prompt`를 담을 자리가 API에 따로 없어서 프롬프트 본문에 붙인다.
    비어 있으면 붙이지 않는다 — 빈 "피할 것:" 줄이 프롬프트에 남으면 모델이
    그것도 지시로 읽는다.
    """
    if len(request.style_anchors) > MAX_STYLE_ANCHORS:
        raise ProviderNotConfigured(
            f"스타일 앵커가 {len(request.style_anchors)}장이다. "
            f"{model_id}의 스타일 레퍼런스 상한은 {MAX_STYLE_ANCHORS}장이다 (ADR-0021). "
            "앵커를 골라 assets/style_anchors/에 3장만 두어라 — "
            "여기서 앞 3장만 쓰면 파일 이름 순서가 룩을 정하게 된다"
        )

    text = request.prompt
    if request.negative_prompt.strip():
        text = f"{text}\n\nAvoid: {request.negative_prompt}"

    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    parts.extend(anchor_part(p) for p in request.style_anchors)

    return {
        "model": model_id,
        "input": parts,
        "response_format": {
            "type": "image",
            "mime_type": OUTPUT_MIME,
            "aspect_ratio": request.aspect_ratio,
            "image_size": request.resolution,
        },
    }


def _image_blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """응답에서 이미지 블록을 모은다. 편의 필드와 단계 배열을 모두 본다."""
    blocks: list[dict[str, Any]] = []

    output = payload.get("output_image")
    if isinstance(output, dict) and output.get("data"):
        blocks.append(output)

    for step in payload.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for block in step.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "image" and block.get("data"):
                blocks.append(block)

    return blocks


def parse_response(payload: dict[str, Any], *, model_id: str) -> GeneratedImage:
    """응답 → `GeneratedImage`. 이미지가 없으면 응답 모양을 담아 실패한다."""
    blocks = _image_blocks(payload)
    if not blocks:
        raise ImageGenError(
            "응답에 이미지 블록이 없다 "
            f"(최상위 키: {sorted(payload)}). ADR-0021의 되돌릴 조건 — "
            "파싱이 두 번 이상 깨지면 SDK로 간다"
        )

    block = blocks[0]
    try:
        data = base64.b64decode(block["data"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageGenError(f"이미지 base64를 디코드할 수 없다: {exc}") from exc

    return GeneratedImage(
        data=data,
        mime_type=block.get("mime_type", OUTPUT_MIME),
        request_id=payload.get("id") or payload.get("interaction_id"),
        model_id=payload.get("model") or model_id,
        raw={"finish_reason": payload.get("finish_reason")},
    )


def _fail(status: int, body: bytes) -> ImageGenError:
    """HTTP 상태 → 예외 (ADR-0021의 오류 매핑).

    프로바이더 전체의 문제(401·403)와 씬 하나의 실패를 가른다. 전자는 `[6]`이
    재시도하지 않고 즉시 멈춘다 — 씬마다 같은 인증 오류로 두 번씩 실패하며 전 씬을
    폴백으로 채우는 것은 결과가 아니라 소음이다 (`base.ProviderNotConfigured`).
    """
    detail = body.decode("utf-8", "replace")[:400]
    if status == 429:
        return ImageGenRateLimited(f"사용 한도 도달 (429): {detail}")
    if status in (401, 403):
        return ProviderNotConfigured(
            f"인증·권한 거부 ({status}): {detail}. "
            f"{API_KEY_ENV}가 맞는지, 그리고 **유료 티어인지** 확인하라 — "
            "이미지 생성은 유료 티어 전용이라 모델 목록 조회가 되더라도 막힐 수 있다"
        )
    return ImageGenError(f"HTTP {status}: {detail}")


class NanoBananaClient(ImageClient):
    """ADR-0005의 기본 모델. 호출 경로는 ADR-0021."""

    #: 앵커 없이 과금 호출을 돌리면 씬 간 룩이 갈리고 어차피 다시 만들게 된다.
    requires_style_anchors = True
    name = MODEL_NAME
    #: 여러 줄 자연어 프롬프트를 읽는다 (ADR-0027). MJ 방언 문자열이 들어오면
    #: `--ar`·`--no`가 그릴 대상으로 섞이므로 단계가 호출 전에 막는다.
    dialect = "nb2"

    def __init__(
        self,
        *,
        model_id: str = MODEL_NAME,
        api_key: str | None = None,
        endpoint: str = ENDPOINT,
        transport: Transport = urllib_transport,
    ) -> None:
        self.model_id = model_id
        self.endpoint = endpoint
        self.transport = transport
        self._api_key = api_key

    @property
    def api_key(self) -> str:
        """키는 **생성자가 아니라 첫 호출에서** 읽는다.

        `--help`나 다른 서브커맨드가 이 클래스를 만드는 것만으로 실패하면 안 된다.
        """
        if self._api_key:
            return self._api_key
        try:
            self._api_key = require_env(API_KEY_ENV, purpose="Nano Banana 2 이미지 생성")
        except MissingCredential as exc:
            raise ProviderNotConfigured(str(exc)) from exc
        return self._api_key

    def generate(
        self, request: ImageRequest, *, timeout: int | None = None
    ) -> GeneratedImage:
        body = json.dumps(build_body(request, model_id=self.model_id)).encode("utf-8")
        headers = {
            "x-goog-api-key": self.api_key,
            "Content-Type": "application/json",
        }

        status, raw = self.transport(
            self.endpoint, headers, body, timeout or DEFAULT_TIMEOUT
        )
        if status != 200:
            raise _fail(status, raw)

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ImageGenError(f"JSON이 아닌 200 응답이다: {exc}") from exc

        return parse_response(payload, model_id=self.model_id)
