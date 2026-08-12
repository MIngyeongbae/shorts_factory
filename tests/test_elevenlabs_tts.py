"""ElevenLabs 실물 어댑터 (`tts/elevenlabs.py`) — URL·본문·응답 파싱·오류 매핑.

**실제 호출은 하지 않는다.** HTTP 경계(`transport`)를 페이크로 갈아끼워 전 경로를
검증한다 (`test_image_client.py`와 같은 모양).

확인 대상:
- ADR-0004 — 대본 전체 단일 호출, `voice_id`는 환경변수, 원속 생성(배속은 `[3]` 몫)
- `base.py` 정렬 계약 — `normalized_alignment`가 아니라 `alignment`를 쓴다
- 실호출로 확인한 두 함정 — `output_format`은 쿼리 파라미터, PCM만 받는다
"""

from __future__ import annotations

import base64
import json
import math

import pytest

from shorts_factory.tts.audio import pcm_to_wav
from shorts_factory.tts.base import (
    PCM_S16LE,
    TTSError,
    TTSNotConfigured,
    TTSRateLimited,
)
from shorts_factory.tts.elevenlabs import (
    API_KEY_ENV,
    DEFAULT_OUTPUT_FORMAT,
    MODEL_ID,
    VOICE_ID_ENV,
    ElevenLabsClient,
    build_body,
    build_url,
    parse_response,
    sample_rate_of,
)

TEXT = "그래서 발상을 뒤집습니다."

#: 24kHz 16bit 모노 0.5초. 값은 무음이면 충분하다 — 여기서 재는 것은 길이다.
SAMPLE_RATE = 24000
PCM = b"\x00\x00" * (SAMPLE_RATE // 2)


def alignment_payload(text: str, *, duration: float = 0.5) -> dict:
    """문자마다 같은 시간을 준 정렬. 마지막 end가 `duration`이 된다."""
    step = duration / len(text)
    return {
        "characters": list(text),
        "character_start_times_seconds": [round(i * step, 6) for i in range(len(text))],
        "character_end_times_seconds": [round((i + 1) * step, 6) for i in range(len(text))],
    }


def response_payload(text: str = TEXT, *, pcm: bytes = PCM) -> dict:
    """실제 응답 모양. `normalized_alignment`가 **글자 수가 다르게** 함께 온다."""
    return {
        "audio_base64": base64.b64encode(pcm).decode("ascii"),
        "alignment": alignment_payload(text),
        "normalized_alignment": alignment_payload(text + "요"),
    }


class FakeTransport:
    """호출을 기록하고 준비된 응답을 돌려준다."""

    def __init__(self, *, status: int = 200, payload: dict | None = None, body: bytes | None = None,
                 headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.body = body if body is not None else json.dumps(payload or response_payload()).encode("utf-8")
        self.headers = headers if headers is not None else {"request-id": "req-abc"}
        self.calls: list[dict] = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append(
            {"url": url, "headers": headers, "body": json.loads(body), "timeout": timeout}
        )
        return self.status, self.headers, self.body


@pytest.fixture(autouse=True)
def _credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """conftest가 지운 키를 이 파일에서만 가짜 값으로 되돌린다 (호출은 페이크로 간다)."""
    monkeypatch.setenv(API_KEY_ENV, "sk_test")
    monkeypatch.setenv(VOICE_ID_ENV, "voice-test")


# --- URL·본문 ---------------------------------------------------------------


def test_output_format은_쿼리_파라미터로_간다():
    """본문에 넣으면 무시되고 mp3가 온다 — 조용히 틀리는 경로라 여기서 못박는다."""
    url = build_url("voice-test", output_format="pcm_24000")

    assert url.endswith("/v1/text-to-speech/voice-test/with-timestamps?output_format=pcm_24000")


def test_본문에는_output_format이_없다():
    body = build_body(TEXT, model_id=MODEL_ID)

    assert body == {"text": TEXT, "model_id": MODEL_ID}
    assert "output_format" not in body


def test_voice_settings는_줄_때만_담는다():
    assert "voice_settings" not in build_body(TEXT, model_id=MODEL_ID)
    assert build_body(TEXT, model_id=MODEL_ID, voice_settings={"speed": 0.9})["voice_settings"] == {
        "speed": 0.9
    }


def test_빈_텍스트는_호출_전에_막는다():
    with pytest.raises(TTSError):
        build_body("   ", model_id=MODEL_ID)


# --- 오디오 포맷 -------------------------------------------------------------


def test_pcm이_아니면_거절한다():
    """mp3는 wav 컨테이너를 씌울 수 없다. 과금 호출 **전에** 막는다."""
    assert sample_rate_of("pcm_24000") == 24000
    assert sample_rate_of(DEFAULT_OUTPUT_FORMAT) == 24000

    with pytest.raises(TTSNotConfigured):
        sample_rate_of("mp3_44100_128")
    with pytest.raises(TTSNotConfigured):
        ElevenLabsClient(output_format="mp3_44100_128")


def test_받은_PCM의_길이가_정렬과_맞는다():
    """샘플레이트를 잘못 신고하면 `[3]`의 오디오 길이 대조가 흔들린다."""
    narration = parse_response(
        response_payload(),
        sample_rate=SAMPLE_RATE,
        voice_id="voice-test",
        model_id=MODEL_ID,
        output_format=DEFAULT_OUTPUT_FORMAT,
    )

    wav = pcm_to_wav(narration.audio, sample_rate=narration.sample_rate)
    seconds = len(narration.audio) / 2 / narration.sample_rate

    assert narration.encoding == PCM_S16LE
    assert narration.channels == 1
    assert math.isclose(seconds, narration.raw_duration, abs_tol=0.01)
    assert wav.startswith(b"RIFF")


# --- 응답 파싱 ---------------------------------------------------------------


def test_정렬은_normalized가_아니라_alignment다():
    """`normalized_alignment`는 글자 수가 원문과 달라 씬 경계를 짚을 수 없다 (base.py)."""
    narration = parse_response(
        response_payload(),
        sample_rate=SAMPLE_RATE,
        voice_id="voice-test",
        model_id=MODEL_ID,
        output_format=DEFAULT_OUTPUT_FORMAT,
    )

    assert narration.alignment.text == TEXT


@pytest.mark.parametrize(
    "payload",
    [
        {"alignment": alignment_payload(TEXT)},
        {"audio_base64": base64.b64encode(PCM).decode("ascii")},
    ],
    ids=["오디오_없음", "정렬_없음"],
)
def test_오디오나_정렬이_없으면_실패한다(payload):
    with pytest.raises(TTSError):
        parse_response(
            payload,
            sample_rate=SAMPLE_RATE,
            voice_id="voice-test",
            model_id=MODEL_ID,
            output_format=DEFAULT_OUTPUT_FORMAT,
        )


# --- 호출 경로 ---------------------------------------------------------------


def test_한_번의_호출로_한_편을_읽는다():
    """ADR-0004 — 문장별 분할 호출은 톤 불연속으로 탈락한 방식이다."""
    transport = FakeTransport()
    client = ElevenLabsClient(transport=transport)

    narration = client.synthesize(TEXT, timeout=120, label="3-tts-sync")

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["body"]["text"] == TEXT
    assert call["body"]["model_id"] == MODEL_ID
    assert call["headers"]["xi-api-key"] == "sk_test"
    assert call["timeout"] == 120
    assert f"output_format={DEFAULT_OUTPUT_FORMAT}" in call["url"]

    assert narration.request_id == "req-abc"
    assert narration.meta["voice_id"] == "voice-test"
    assert narration.meta["model_id"] == MODEL_ID
    assert narration.meta["characters"] == len(TEXT)


def test_voice_id는_환경변수에서_읽는다(monkeypatch: pytest.MonkeyPatch):
    """ADR-0004 — IVC→PVC 교체 시 코드는 안 바뀐다."""
    monkeypatch.setenv(VOICE_ID_ENV, "voice-pvc")
    transport = FakeTransport()

    ElevenLabsClient(transport=transport).synthesize(TEXT)

    assert "/text-to-speech/voice-pvc/" in transport.calls[0]["url"]


def test_키가_없어도_생성은_된다(monkeypatch: pytest.MonkeyPatch):
    """`--help`가 어댑터를 만드는 것만으로 실패하면 안 된다."""
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    client = ElevenLabsClient(transport=FakeTransport())

    with pytest.raises(TTSNotConfigured):
        client.synthesize(TEXT)


# --- 오류 매핑 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "status, expected",
    [
        (401, TTSNotConfigured),
        (403, TTSNotConfigured),
        (422, TTSError),
        (429, TTSRateLimited),
        (500, TTSError),
    ],
)
def test_상태코드를_예외로_가른다(status, expected):
    transport = FakeTransport(status=status, body=b'{"detail":"nope"}')
    client = ElevenLabsClient(transport=transport)

    with pytest.raises(expected):
        client.synthesize(TEXT)


def test_JSON이_아닌_200은_실패한다():
    client = ElevenLabsClient(transport=FakeTransport(body=b"<html/>"))

    with pytest.raises(TTSError):
        client.synthesize(TEXT)
