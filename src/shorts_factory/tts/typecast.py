"""타입캐스트 `with-timestamps` 어댑터 — 대본 한 편을 단일 호출로 읽는다 (ADR-0081).

`[3]`의 **기본 제공자**다. ADR-0004가 엔진과 호출 방식을, ADR-0081이 벤더를,
`base.py`가 정렬 계약을 정했다. 여기는 그것을 HTTP로 옮긴 것뿐이고, `elevenlabs.py`와
같은 자리에 같은 모양으로 선다 — 단계 코드는 어느 쪽인지 모른다.

- 엔드포인트 `POST /v1/text-to-speech/with-timestamps`, 헤더 `X-API-KEY`
- 모델 `ssfm-v30` (37개 언어, 세 언어 공통)
- **`granularity=char` 고정** (아래)
- `voice_id`는 **언어별 환경변수**다: ko `TYPECAST_VOICE_ID_KO`, ja `TYPECAST_VOICE_ID_JA`,
  en `TYPECAST_VOICE_ID_EN`. **ko는 접미사 없는 `TYPECAST_VOICE_ID`로도 떨어진다** —
  엔진을 붙이기 전부터 `.env`에 있던 이름이라 깨지 않는다. 비어 있으면
  `check_configured()`가 호출 전에 거절한다
- 오디오는 wav로 받아 **컨테이너를 벗겨** 원시 PCM으로 넘긴다 (ADR-0081 결정 4)
- **배속은 엔진이 건다** (`output.audio_tempo`, 기본 1.1 — ADR-0081 결정 6 개정).
  정렬도 같은 시간축으로 당겨져 오므로 `[3]`은 더 손대지 않는다

SDK(`typecast-python`)를 쓰지 않는다 (ADR-0021과 같은 판단) — 엔드포인트가 하나고 현재
의존성이 두 줄이다. HTTP 경계를 공용 `transport.py` 하나로 좁혀 테스트가 과금 호출 없이
전 경로를 검증한다.

## `granularity=char`를 고정하는 이유

공식 문서: 공백으로 단어를 가르지 않는 언어(`jpn`·`zho`)는 **word 정렬이 문장 전체를
원소 하나로 뭉친다.** 우리는 ja를 세 언어 중 하나로 쓰므로 word는 애초에 선택지가
아니고, ko·en도 씬 경계는 문자 인덱스로 잡는다 (ADR-0013). 파라미터를 생략하면 `words`와
`characters`가 둘 다 오는데, 안 쓰는 배열을 받을 이유가 없다.

## 실호출로 확인한 것 (2026-08-27, ko 38자)

| 확인 | 결과 |
|---|---|
| `characters` 개수 | 38 — 보낸 글자 수와 같다 |
| **정렬 텍스트 == 보낸 텍스트** | **True** — `12cm`·`3.5kg`도 원문 그대로다 |
| 오디오 | wav(RIFF) base64, 모노 16bit 44,100Hz |
| `audio_duration` | 정렬의 마지막 `end`와 같다 |
| 추적 ID | **없다** — 응답 헤더에 `request-id`가 없어 `Narration.request_id`는 None이다 |

첫 글자의 `start`가 0이 아니다(실측 0.18초, 앞 묵음). 씬 경계는 첫 씬만 0.0으로
시작하므로(`sync.scene_boundaries`) 그 묵음은 첫 씬이 먹는다 — 계약은 바뀌지 않는다.

## 배속: 정렬이 같이 당겨진다 (2026-08-28, 같은 seed로 2회)

| `output.audio_tempo` | wav 길이 | 정렬 마지막 `end` |
|---|---|---|
| 안 보냄 | 6.000초 | 6.000 |
| **1.1** | **5.409초** (÷1.109) | **5.409** |

**타임스탬프가 배속 후 시간축을 따라온다** — `end`가 새 오디오 길이와 같고, 정렬 텍스트는
여전히 원문과 일치한다. 그래서 여기서 배속을 걸면 `[3]`은 스케일 보정도 FFmpeg atempo도
하지 않는다 (`Narration.tempo`로 "이만큼 이미 걸었다"를 알린다). 시간 늘이기(atempo)가
아니라 **엔진이 그 속도로 다시 읽은 것**이라 운율이 더 자연스럽다 — 그것이 ADR-0081
결정 6을 뒤집은 이유다.

## 엔진이 무엇을 더 읽었는지는 볼 수 없다

ElevenLabs의 `normalized_alignment`에 해당하는 것이 없다. 그래서 이 제공자로 돌면
`timing.{lang}.json`의 `spoken.engine_normalized`가 **없다** — 우리가 편 발화형
(`spoken.lines`, ADR-0063)은 그대로 남지만, 엔진이 그 위에 무엇을 더 했는지는 오디오로만
확인된다. ADR-0063 되돌릴 조건 1의 관측 창이 이 제공자에서는 닫혀 있다 (ADR-0081).
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import wave
from typing import Any, Callable

from ..config import MissingCredential, require_env
from ..transport import TransportError, TransportTimeout, urllib_request
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

#: ADR-0081 — 최신 모델. ja·ko를 포함한 37개 언어를 덮는다.
MODEL_ID = "ssfm-v30"

ENDPOINT = "https://api.typecast.ai/v1/text-to-speech/with-timestamps"

#: 모듈 독스트링 참고. word로 부르면 ja 정렬이 문장 하나로 뭉친다.
GRANULARITY = "char"

API_KEY_ENV = "TYPECAST_API_KEY"

#: 접미사 없는 옛 이름. ko의 하위 호환 자리다 (`.env.example`).
VOICE_ID_ENV = "TYPECAST_VOICE_ID"

#: 언어별 voice_id 환경변수 (ADR-0081 결정 5).
VOICE_ID_ENVS: dict[str, str] = {
    "ko": "TYPECAST_VOICE_ID_KO",
    "ja": "TYPECAST_VOICE_ID_JA",
    "en": "TYPECAST_VOICE_ID_EN",
}

#: 그 언어의 id가 비어 있을 때 물러날 옛 이름. ko에만 있다.
VOICE_ID_FALLBACKS: dict[str, str] = {"ko": VOICE_ID_ENV}

#: 우리 언어 코드 → 타입캐스트가 받는 ISO 639-3. 생략하면 자동 판별인데, 대본이 숫자·
#: 로마자를 섞고 있어 짧은 줄에서 흔들릴 수 있다. 우리는 어느 언어인지 알고 있으므로 준다.
LANGUAGE_CODES: dict[str, str] = {"ko": "kor", "ja": "jpn", "en": "eng"}

DEFAULT_LANG = "ko"

#: 한 호출의 텍스트 상한. 넘으면 400을 사고 나서야 알게 되므로 호출 **전에** 본다.
MAX_TEXT_CHARS = 2000

#: 받을 오디오 컨테이너. mp3는 디코더 없이 길이도 못 잰다 (`audio.py` 독스트링).
AUDIO_FORMAT = "wav"

#: `output.audio_tempo`가 받는 범위. 밖으로 나가면 422가 오므로 호출 전에 막는다.
TEMPO_RANGE = (0.5, 2.0)

#: 570자 한 편 기준. 단계(`stages/tts.py`)가 자기 값을 넘겨준다.
DEFAULT_TIMEOUT = 300

#: `(url, headers, body, timeout) -> (status, response_bytes)`.
#: 응답 헤더에 쓸 것이 없어(추적 ID가 없다) ElevenLabs 쪽보다 한 칸 좁다.
Transport = Callable[[str, dict[str, str], bytes, int], "tuple[int, bytes]"]


def urllib_transport(
    url: str, headers: dict[str, str], body: bytes, timeout: int
) -> tuple[int, bytes]:
    """stdlib POST. 오류 응답도 본문을 살려 돌려준다 — 서버가 사유를 적어 준다.

    HTTP는 공용 `transport.urllib_request`가 치고, 여기서는 그 오류를 TTS 예외 계층으로
    바꾼다 (`elevenlabs.py`와 같은 모양).
    """
    try:
        status, _headers, raw = urllib_request("POST", url, headers, body, timeout)
    except TransportTimeout as exc:
        raise TTSTimeout(str(exc)) from exc
    except TransportError as exc:
        raise TTSError(str(exc)) from exc
    return status, raw


def voice_envs_for(lang: str) -> tuple[str, ...]:
    """언어 코드 → 볼 환경변수 이름들. 앞의 것이 이긴다. 모르는 언어는 설정 오류다."""
    try:
        primary = VOICE_ID_ENVS[lang]
    except KeyError as exc:
        raise TTSNotConfigured(
            f"언어 '{lang}'의 voice_id 환경변수가 정해져 있지 않다 "
            f"(있는 언어: {', '.join(VOICE_ID_ENVS)})"
        ) from exc
    fallback = VOICE_ID_FALLBACKS.get(lang)
    return (primary, fallback) if fallback else (primary,)


def language_code(lang: str) -> str:
    """우리 언어 코드 → ISO 639-3. 모르는 언어는 호출 전에 거절한다."""
    try:
        return LANGUAGE_CODES[lang]
    except KeyError as exc:
        raise TTSNotConfigured(
            f"언어 '{lang}'의 타입캐스트 언어 코드가 없다 "
            f"(있는 언어: {', '.join(LANGUAGE_CODES)})"
        ) from exc


def build_url(*, endpoint: str = ENDPOINT, granularity: str = GRANULARITY) -> str:
    """호출 URL. `granularity`는 쿼리 파라미터다 (모듈 독스트링)."""
    return f"{endpoint}?granularity={granularity}"


def build_body(
    text: str,
    *,
    voice_id: str,
    model_id: str = MODEL_ID,
    language: str | None = None,
    audio_format: str = AUDIO_FORMAT,
    tempo: float = 1.0,
    output: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """요청 본문.

    감정(`prompt`)은 담지 않는다 (ADR-0081 결정 8) — 보이스에 저장된 톤이 쓰인다.

    **배속은 담는다** (`output.audio_tempo`, ADR-0081 결정 6 개정) — 정렬이 같은 시간축
    으로 따라오는 것이 실측됐고(모듈 독스트링), 엔진이 그 속도로 다시 읽는 편이
    FFmpeg의 시간 늘이기보다 낫다. 1.0이면 키를 넣지 않는다 — 기본값이 1.0이라
    보내나 마나지만, 안 보내는 쪽이 "이 편에 배속을 안 걸었다"를 요청에 남긴다.
    """
    if not text.strip():
        raise TTSError("읽을 텍스트가 비어 있다")
    check_text_length(text)
    check_tempo(tempo)

    settings: dict[str, Any] = {"audio_format": audio_format}
    if tempo != 1.0:
        settings["audio_tempo"] = tempo
    settings.update(output or {})

    body: dict[str, Any] = {
        "voice_id": voice_id,
        "text": text,
        "model": model_id,
        "output": settings,
    }
    if language:
        body["language"] = language
    return body


def check_tempo(tempo: float) -> None:
    """상한·하한을 호출 **전에** 본다. 밖으로 나가면 422가 오는데 그건 과금 뒤다."""
    low, high = TEMPO_RANGE
    if not low <= tempo <= high:
        raise TTSError(
            f"배속 {tempo}는 타입캐스트 audio_tempo 범위({low}~{high}) 밖이다"
        )


def check_text_length(text: str) -> None:
    """상한을 호출 **전에** 본다. 넘으면 400이 오는데 그건 과금 뒤의 설정 오류다."""
    if len(text) > MAX_TEXT_CHARS:
        raise TTSError(
            f"보낼 텍스트가 {len(text)}자로 타입캐스트 상한 {MAX_TEXT_CHARS}자를 넘는다. "
            "대본 축약은 1부 소관이다 (ADR-0017) — 여기서 잘라 보내면 씬 경계가 어긋난다"
        )


def unwrap_wav(data: bytes) -> tuple[bytes, int, int, int]:
    """wav(RIFF) → `(pcm, sample_rate, channels, sample_width)`.

    `audio.py`의 계약은 원시 PCM이다 — 컨테이너 씌우기는 그쪽이 표준 라이브러리로 하고,
    이 어댑터는 벤더가 씌워 준 것을 벗겨서 넘긴다 (ADR-0081 결정 4). 16bit가 아니면
    `write_narration`의 인코딩 계약을 만족할 수 없으므로 여기서 거절한다.
    """
    try:
        with wave.open(io.BytesIO(data), "rb") as src:
            channels = src.getnchannels()
            width = src.getsampwidth()
            rate = src.getframerate()
            pcm = src.readframes(src.getnframes())
    except wave.Error as exc:
        raise TTSError(f"wav로 읽을 수 없는 오디오다: {exc}") from exc

    if width != 2:
        raise TTSError(
            f"16bit PCM이 아니다 (샘플 폭 {width}바이트). "
            f"{PCM_S16LE}만 `[3]`의 후처리를 탄다 (tts/audio.py 참고)"
        )
    if rate <= 0:
        raise TTSError("샘플레이트가 0이다 (응답 wav 헤더가 깨졌다)")
    return pcm, rate, channels, width


def parse_response(
    payload: dict[str, Any],
    *,
    voice_id: str,
    model_id: str,
    language: str | None = None,
    provider: str = "typecast",
    tempo: float = 1.0,
) -> Narration:
    """응답 → `Narration`. 오디오나 정렬이 없으면 응답 모양을 담아 실패한다."""
    encoded = payload.get("audio")
    if not encoded:
        raise TTSError(f"응답에 오디오가 없다 (최상위 키: {sorted(payload)})")

    audio_format = str(payload.get("audio_format") or AUDIO_FORMAT)
    if audio_format != AUDIO_FORMAT:
        raise TTSError(
            f"wav가 아닌 오디오가 왔다: {audio_format!r}. "
            f"본문의 output.audio_format을 {AUDIO_FORMAT!r}로 보냈는지 확인하라"
        )

    try:
        container = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TTSError(f"오디오 base64를 디코드할 수 없다: {exc}") from exc

    characters = payload.get("characters")
    if not isinstance(characters, list) or not characters:
        raise TTSError(
            f"응답에 문자 정렬(characters)이 없다 (최상위 키: {sorted(payload)}). "
            f"with-timestamps를 granularity={GRANULARITY}로 불렀는지 확인하라"
        )

    pcm, rate, channels, width = unwrap_wav(container)

    raw: dict[str, Any] = {"audio_format": audio_format}
    duration = payload.get("audio_duration")
    if duration is not None:
        raw["audio_duration"] = duration
    if language:
        raw["language"] = language

    return Narration(
        audio=pcm,
        alignment=Alignment.from_segments(characters),
        sample_rate=rate,
        channels=channels,
        sample_width=width,
        encoding=PCM_S16LE,
        request_id=None,  # 응답 헤더에 추적 ID가 없다 (모듈 독스트링)
        voice_id=voice_id,
        model_id=model_id,
        provider=provider,
        # 엔진이 이미 건 배속. `[3]`은 이 값을 빼고 남은 몫만 후처리한다.
        tempo=tempo,
        raw=raw,
    )


def _fail(status: int, body: bytes) -> TTSError:
    """HTTP 상태 → 예외.

    400·401·404는 재시도할 것이 없는 설정 문제라 따로 가른다 — 특히 400은 대개
    `voice_id`가 그 계정에 없는 값이다. 402(크레딧 소진)는 한도와 같은 자리로 본다.
    """
    detail = body.decode("utf-8", "replace")[:400]
    if status == 429:
        return TTSRateLimited(f"사용 한도 도달 (429): {detail}")
    if status == 402:
        return TTSRateLimited(
            f"크레딧이 부족하다 (402): {detail}. 플랜·잔량을 확인하라 "
            "— 재시도해도 같은 응답이 온다"
        )
    if status == 401:
        return TTSNotConfigured(
            f"인증 거부 (401): {detail}. {API_KEY_ENV}가 studio.typecast.ai에서 발급한 "
            "API 키인지 확인하라"
        )
    if status in (400, 404):
        return TTSNotConfigured(
            f"요청이 거절됐다 ({status}): {detail}. voice_id를 먼저 의심하라 — "
            f"내장 목소리는 `tc_`, 클론은 `uc_`로 시작하고 소문자다 "
            f"(목록: studio.typecast.ai/developers/api/voices)"
        )
    if status == 422:
        return TTSError(
            f"읽을 수 없는 텍스트다 (422): {detail}. "
            "발화형이 만든 문자열에 엔진이 못 읽는 글자가 섞였는지 확인하라"
        )
    return TTSError(f"HTTP {status}: {detail}")


class TypecastClient(TTSClient):
    """ADR-0081의 실물 어댑터. 대본 전체를 한 번에 읽는다."""

    name = "typecast"

    def __init__(
        self,
        *,
        lang: str = DEFAULT_LANG,
        voice_id: str | None = None,
        model_id: str = MODEL_ID,
        api_key: str | None = None,
        language: str | None = None,
        audio_format: str = AUDIO_FORMAT,
        tempo: float | None = None,
        output: dict[str, Any] | None = None,
        endpoint: str = ENDPOINT,
        granularity: str = GRANULARITY,
        transport: Transport = urllib_transport,
    ) -> None:
        self.lang = lang
        self.model_id = model_id
        self.audio_format = audio_format
        #: 생성자로 못 박으면 그것이 이긴다. None이면 `synthesize(tempo=…)`가 정하고,
        #: 그것도 없으면 원속이다 — `[3]`은 언제나 요청 배속을 넘겨준다.
        self.tempo = tempo
        self.output = output
        self.endpoint = endpoint
        self.granularity = granularity
        self.transport = transport
        self._api_key = api_key
        self._voice_id = voice_id
        self._language = language

    @property
    def api_key(self) -> str:
        """키와 voice_id는 **생성자가 아니라 첫 호출에서** 읽는다.

        `--help`나 다른 서브커맨드가 이 클래스를 만드는 것만으로 실패하면 안 된다.
        """
        if self._api_key:
            return self._api_key
        self._api_key = self._require(
            (API_KEY_ENV,), "타입캐스트 TTS 호출 (studio.typecast.ai에서 발급)"
        )
        return self._api_key

    @property
    def voice_id(self) -> str:
        """이 언어의 목소리. 비어 있으면 `TTSNotConfigured` — 호출 전에 멈춘다."""
        if self._voice_id:
            return self._voice_id
        self._voice_id = self._require(
            voice_envs_for(self.lang), f"{self.lang} 목소리 (ADR-0081 결정 5)"
        )
        return self._voice_id

    @property
    def language(self) -> str:
        """본문에 실을 ISO 639-3 코드."""
        if self._language:
            return self._language
        self._language = language_code(self.lang)
        return self._language

    def check_configured(self) -> None:
        """키·voice_id·언어 코드를 호출 전에 읽어 본다 — 비어 있으면 여기서 멈춘다."""
        self.api_key
        self.voice_id
        self.language

    @staticmethod
    def _require(names: tuple[str, ...], purpose: str) -> str:
        """앞의 이름이 이긴다. 다 비어 있으면 **첫 이름**을 사유에 적어 실패한다."""
        first: MissingCredential | None = None
        for name in names:
            try:
                return require_env(name, purpose=purpose)
            except MissingCredential as exc:
                first = first or exc
        assert first is not None  # names는 비어 있지 않다 (voice_envs_for)
        raise TTSNotConfigured(str(first))

    def synthesize(
        self,
        text: str,
        *,
        timeout: int | None = None,
        label: str = "",
        tempo: float = 1.0,
    ) -> Narration:
        """**요청 배속을 엔진이 건다** (ADR-0081 결정 6 개정).

        정렬이 같은 시간축으로 따라오므로 `Narration.tempo`에 건 값을 실어 보내고,
        `[3]`은 그만큼을 빼고 남은 몫(대개 없다)만 FFmpeg로 건다.
        """
        applied = self.tempo if self.tempo is not None else tempo
        body = json.dumps(
            build_body(
                text,
                voice_id=self.voice_id,
                model_id=self.model_id,
                language=self.language,
                audio_format=self.audio_format,
                tempo=applied,
                output=self.output,
            ),
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {
            "X-API-KEY": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = build_url(endpoint=self.endpoint, granularity=self.granularity)

        status, raw = self.transport(url, headers, body, timeout or DEFAULT_TIMEOUT)
        if status != 200:
            raise _fail(status, raw)

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TTSError(f"JSON이 아닌 200 응답이다: {exc}") from exc

        return parse_response(
            payload,
            voice_id=self.voice_id,
            model_id=self.model_id,
            language=self.language,
            provider=self.name,
            tempo=applied,
        )
