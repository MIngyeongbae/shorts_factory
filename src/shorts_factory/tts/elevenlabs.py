"""ElevenLabs `with-timestamps` 어댑터 — 대본 한 편을 단일 호출로 읽는다 (ADR-0004).

ADR-0004가 엔진과 호출 방식을, `base.py`가 정렬 계약을 정했다. 여기는 그 둘을 HTTP로
옮긴 것뿐이다.

- 엔드포인트 `POST /v1/text-to-speech/{voice_id}/with-timestamps`, 헤더 `xi-api-key`
- 모델 `eleven_multilingual_v2` (한국어 + 클로닝 품질)
- `voice_id`는 환경변수 `ELEVEN_VOICE_ID` (specs/04, ADR-0004 — IVC→PVC 교체 시 코드 무변경)
- 오디오는 원시 PCM으로 받는다. 이유는 `audio.py` 독스트링 참고

SDK(`elevenlabs`)를 쓰지 않는 이유는 `imagegen/nano_banana.py`와 같다 — 엔드포인트가
하나고 현재 의존성이 두 줄이다. HTTP 경계를 `transport` 하나로 좁혀 테스트가 과금 호출
없이 전 경로를 검증한다.

## 실호출로 확인한 두 가지 (2026-08-12, Starter 플랜)

1. **`output_format`은 쿼리 파라미터다.** JSON 본문에 넣으면 400도, 경고도 없이 무시되고
   기본값 `mp3_44100_128`이 온다. 그러면 `write_narration`이 mp3 바이트에 wav 헤더를
   씌우게 되므로 — 길이가 5배 어긋난 재생 불가 파일이 나온다. 조용히 틀리는 경로라
   URL 조립을 `build_url()` 한 곳에 몰아 두고 테스트가 지킨다
2. **`pcm_44100`은 Pro 티어 이상 전용이다.** Starter로 요청하면 403
   `subscription_required`가 온다. 그래서 기본값이 `pcm_24000`이다 (24kHz 16bit 모노).
   나레이션 음성이라 24kHz로 충분하고, 플랜을 올리면 `output_format`만 바꾸면 된다

## 목소리가 다르게 들리면 `voice_id`를 의심하지 말 것 (2026-08-13)

`ELEVEN_VOICE_ID`는 맞다. **다르게 들리는 이유는 속도다** — ADR-0004가 "원속 생성 후
FFmpeg atempo 1.1 후처리"를 기본으로 정했고(specs/04는 1.1~1.2배속), 그래서 원속 출력만
들어 보면 기억보다 느리다. 클론을 다시 만들거나 `voice_id`를 바꾸는 것으로 답이 나오지
않는다. 배속 값 자체의 확정은 별도 ADR로 남아 있다.

## 정렬은 `alignment`만 쓴다

응답의 `normalized_alignment`는 숫자·단위를 읽는 대로 풀어 쓴 배열이라 문자 수가 원문과
다르다 (`base.py` 참고). 여기서 그것을 넘기면 `[3]`의 대조 검사가 즉시 실패한다.
"""

from __future__ import annotations

import base64
import binascii
import json
import urllib.error
import urllib.request
from typing import Any, Callable

from ..config import MissingCredential, require_env
from .base import (
    PCM_S16LE,
    Alignment,
    Narration,
    TTSClient,
    TTSError,
    TTSNotConfigured,
    TTSRateLimited,
    TTSTimeout,
)

#: ADR-0004 — 한국어 클로닝 음성. v3는 문자 타임스탬프를 주지 않아 쓸 수 없다.
MODEL_ID = "eleven_multilingual_v2"

ENDPOINT_TEMPLATE = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps"

API_KEY_ENV = "ELEVENLABS_API_KEY"
VOICE_ID_ENV = "ELEVEN_VOICE_ID"

#: 모듈 독스트링 2번 — Starter에서 되는 가장 높은 PCM 레이트.
DEFAULT_OUTPUT_FORMAT = "pcm_24000"

#: 570자 한 편 기준. 단계(`stages/tts.py`)가 자기 값을 넘겨준다.
DEFAULT_TIMEOUT = 300

#: 응답 헤더에만 오는 추적 ID. 본문에는 없다.
REQUEST_ID_HEADER = "request-id"

#: `(url, headers, body, timeout) -> (status, response_headers, response_bytes)`.
#: `imagegen`의 `Transport`에 응답 헤더가 하나 더 붙은 모양이다 — 위 추적 ID 때문이다.
Transport = Callable[[str, dict[str, str], bytes, int], "tuple[int, dict[str, str], bytes]"]


def urllib_transport(
    url: str, headers: dict[str, str], body: bytes, timeout: int
) -> tuple[int, dict[str, str], bytes]:
    """stdlib POST. 오류 응답도 본문을 살려 돌려준다 — 서버가 사유를 적어 준다."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()
    except TimeoutError as exc:
        raise TTSTimeout(f"{timeout}초 안에 응답이 오지 않았다") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise TTSTimeout(f"{timeout}초 안에 응답이 오지 않았다") from exc
        raise TTSError(f"연결 실패: {exc.reason}") from exc


def sample_rate_of(output_format: str) -> int:
    """`pcm_24000` → 24000. PCM이 아니면 거절한다.

    mp3를 받아 오면 `write_narration`이 인코딩 불일치로 멈추지만, 그때는 이미 편당
    과금이 끝난 뒤다. 호출 **전에** 여기서 막는다.
    """
    if not output_format.startswith("pcm_"):
        raise TTSNotConfigured(
            f"output_format이 PCM이 아니다: {output_format!r}. "
            "`[3]`의 산출물은 narration.wav이고 컨테이너를 표준 라이브러리로 씌운다 "
            "(tts/audio.py 참고). pcm_24000처럼 pcm_* 를 써라"
        )
    try:
        return int(output_format.split("_", 1)[1])
    except (IndexError, ValueError) as exc:
        raise TTSNotConfigured(
            f"output_format에서 샘플레이트를 읽을 수 없다: {output_format!r}"
        ) from exc


def build_url(voice_id: str, *, output_format: str, endpoint: str = ENDPOINT_TEMPLATE) -> str:
    """호출 URL. `output_format`은 **쿼리 파라미터여야 한다** (모듈 독스트링 1번)."""
    return f"{endpoint.format(voice_id=voice_id)}?output_format={output_format}"


def build_body(
    text: str, *, model_id: str, voice_settings: dict[str, Any] | None = None
) -> dict[str, Any]:
    """요청 본문. `voice_settings`는 주어질 때만 담는다 — 안 담으면 보이스에 저장된 값이 쓰인다."""
    if not text.strip():
        raise TTSError("읽을 텍스트가 비어 있다")

    body: dict[str, Any] = {"text": text, "model_id": model_id}
    if voice_settings:
        body["voice_settings"] = voice_settings
    return body


def parse_response(
    payload: dict[str, Any],
    *,
    sample_rate: int,
    voice_id: str,
    model_id: str,
    output_format: str,
    request_id: str | None = None,
) -> Narration:
    """응답 → `Narration`. 오디오나 정렬이 없으면 응답 모양을 담아 실패한다."""
    encoded = payload.get("audio_base64")
    if not encoded:
        raise TTSError(f"응답에 오디오가 없다 (최상위 키: {sorted(payload)})")

    try:
        audio = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TTSError(f"오디오 base64를 디코드할 수 없다: {exc}") from exc

    alignment = payload.get("alignment")
    if not isinstance(alignment, dict):
        raise TTSError(
            f"응답에 문자 정렬(alignment)이 없다 (최상위 키: {sorted(payload)}). "
            "with-timestamps 엔드포인트를 부른 것이 맞는지 확인하라"
        )

    return Narration(
        audio=audio,
        # normalized_alignment이 아니다 — 모듈 독스트링 참고.
        alignment=Alignment.from_api(alignment),
        sample_rate=sample_rate,
        channels=1,
        encoding=PCM_S16LE,
        request_id=request_id,
        voice_id=voice_id,
        model_id=model_id,
        raw={"output_format": output_format},
    )


def _fail(status: int, body: bytes) -> TTSError:
    """HTTP 상태 → 예외.

    401·403은 재시도할 것이 없는 설정 문제라 따로 가른다. 특히 403은 키가 아니라
    **플랜**이 사유인 경우가 많다 (`pcm_44100`은 Pro 이상, PVC는 Creator 이상).
    """
    detail = body.decode("utf-8", "replace")[:400]
    if status == 429:
        return TTSRateLimited(f"사용 한도 도달 (429): {detail}")
    if status == 401:
        return TTSNotConfigured(
            f"인증 거부 (401): {detail}. {API_KEY_ENV}가 `sk_`로 시작하는 값인지 확인하라 "
            "— 키 ID(하이픈 없는 64자 hex)로는 인증되지 않는다"
        )
    if status == 403:
        return TTSNotConfigured(
            f"권한 거부 (403): {detail}. 키보다 **플랜**을 먼저 의심하라 — "
            f"pcm_44100은 Pro 이상 전용이라 {DEFAULT_OUTPUT_FORMAT}로 내려야 한다"
        )
    if status == 422:
        return TTSError(
            f"요청이 거절됐다 (422): {detail}. voice_id·model_id가 계정에 있는 값인지 확인하라"
        )
    return TTSError(f"HTTP {status}: {detail}")


class ElevenLabsClient(TTSClient):
    """ADR-0004의 실물 어댑터. 대본 전체를 한 번에 읽는다."""

    name = "elevenlabs"

    def __init__(
        self,
        *,
        voice_id: str | None = None,
        model_id: str = MODEL_ID,
        api_key: str | None = None,
        output_format: str = DEFAULT_OUTPUT_FORMAT,
        voice_settings: dict[str, Any] | None = None,
        endpoint: str = ENDPOINT_TEMPLATE,
        transport: Transport = urllib_transport,
    ) -> None:
        self.model_id = model_id
        self.output_format = output_format
        self.sample_rate = sample_rate_of(output_format)
        self.voice_settings = voice_settings
        self.endpoint = endpoint
        self.transport = transport
        self._api_key = api_key
        self._voice_id = voice_id

    @property
    def api_key(self) -> str:
        """키와 voice_id는 **생성자가 아니라 첫 호출에서** 읽는다.

        `--help`나 다른 서브커맨드가 이 클래스를 만드는 것만으로 실패하면 안 된다.
        """
        if self._api_key:
            return self._api_key
        self._api_key = self._require(API_KEY_ENV, "ElevenLabs TTS 호출")
        return self._api_key

    @property
    def voice_id(self) -> str:
        if self._voice_id:
            return self._voice_id
        self._voice_id = self._require(VOICE_ID_ENV, "클로닝한 본인 목소리 (ADR-0004)")
        return self._voice_id

    @staticmethod
    def _require(name: str, purpose: str) -> str:
        try:
            return require_env(name, purpose=purpose)
        except MissingCredential as exc:
            raise TTSNotConfigured(str(exc)) from exc

    def synthesize(
        self, text: str, *, timeout: int | None = None, label: str = ""
    ) -> Narration:
        body = json.dumps(
            build_body(text, model_id=self.model_id, voice_settings=self.voice_settings),
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = build_url(
            self.voice_id, output_format=self.output_format, endpoint=self.endpoint
        )

        status, response_headers, raw = self.transport(
            url, headers, body, timeout or DEFAULT_TIMEOUT
        )
        if status != 200:
            raise _fail(status, raw)

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TTSError(f"JSON이 아닌 200 응답이다: {exc}") from exc

        return parse_response(
            payload,
            sample_rate=self.sample_rate,
            voice_id=self.voice_id,
            model_id=self.model_id,
            output_format=self.output_format,
            request_id=_header(response_headers, REQUEST_ID_HEADER),
        )


def _header(headers: dict[str, str], name: str) -> str | None:
    """헤더 이름은 대소문자를 가리지 않는다."""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None
