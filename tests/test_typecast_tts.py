"""타입캐스트 실물 어댑터 (`tts/typecast.py`) — URL·본문·응답 파싱·오류 매핑.

**실제 호출은 하지 않는다.** HTTP 경계(`transport`)를 페이크로 갈아끼워 전 경로를
검증한다 (`test_elevenlabs_tts.py`와 같은 모양).

확인 대상:
- ADR-0081 — 기본 제공자, `granularity=char` 고정, 언어별 voice_id, 2,000자 상한
- ADR-0081 결정 4 — wav로 받아 **컨테이너를 벗겨** 원시 PCM으로 넘긴다
- ADR-0081 결정 6 개정 — **배속은 엔진이 건다**(`output.audio_tempo`)고 어댑터가
  `Narration.tempo`로 알린다. 결정 8 — 감정은 담지 않는다
- `base.py` 정렬 계약 — 정렬은 보낸 텍스트 그대로여야 한다
"""

from __future__ import annotations

import base64
import json

import pytest

from shorts_factory.tts.audio import pcm_to_wav
from shorts_factory.tts.base import (
    PCM_S16LE,
    TTSError,
    TTSNotConfigured,
    TTSRateLimited,
)
from shorts_factory.tts.typecast import (
    API_KEY_ENV,
    GRANULARITY,
    LANGUAGE_CODES,
    MAX_TEXT_CHARS,
    MODEL_ID,
    VOICE_ID_ENV,
    VOICE_ID_ENVS,
    TypecastClient,
    build_body,
    build_url,
    parse_response,
    unwrap_wav,
    voice_envs_for,
)

TEXT = "그래서 발상을 뒤집습니다."

#: 실물과 같은 44.1kHz 16bit 모노 0.5초. 값은 무음이면 충분하다 — 여기서 재는 것은 길이다.
SAMPLE_RATE = 44100
PCM = b"\x00\x00" * (SAMPLE_RATE // 2)


def characters_payload(text: str, *, duration: float = 0.5) -> list[dict]:
    """글자마다 같은 시간을 준 정렬. 마지막 end가 `duration`이 된다."""
    step = duration / len(text)
    return [
        {"text": char, "start": round(i * step, 6), "end": round((i + 1) * step, 6)}
        for i, char in enumerate(text)
    ]


def response_payload(text: str = TEXT, *, pcm: bytes = PCM, **extra) -> dict:
    """실제 응답 모양 — 오디오는 **wav 컨테이너**의 base64다."""
    wav = pcm_to_wav(pcm, sample_rate=SAMPLE_RATE)
    payload = {
        "audio": base64.b64encode(wav).decode("ascii"),
        "audio_format": "wav",
        "audio_duration": 0.5,
        "characters": characters_payload(text),
    }
    payload.update(extra)
    return payload


class FakeTransport:
    """호출 인자를 잡아 두고 정해진 응답을 돌려준다."""

    def __init__(self, status: int = 200, payload: dict | bytes | None = None):
        self.status = status
        self.payload = response_payload() if payload is None else payload
        self.calls: list[tuple] = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, headers, body, timeout))
        if isinstance(self.payload, bytes):
            return self.status, self.payload
        return self.status, json.dumps(self.payload).encode("utf-8")


@pytest.fixture(autouse=True)
def _credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, "typecast-test-key")
    monkeypatch.setenv(VOICE_ID_ENVS["ko"], "tc_test")
    monkeypatch.delenv(VOICE_ID_ENV, raising=False)


# --- URL·본문 -----------------------------------------------------------------


def test_granularity는_쿼리_파라미터로_간다():
    """word로 부르면 ja 정렬이 문장 하나로 뭉친다 (ADR-0081 결정 3)."""
    assert build_url().endswith(f"?granularity={GRANULARITY}")
    assert GRANULARITY == "char"


def test_본문은_감정을_담지_않는다():
    """톤은 목소리 선택의 문제다 (ADR-0081 결정 8) — 보이스에 저장된 값이 쓰인다."""
    body = build_body(TEXT, voice_id="tc_test", language="kor")
    assert body["voice_id"] == "tc_test"
    assert body["model"] == MODEL_ID
    assert body["language"] == "kor"
    assert "prompt" not in body
    assert "seed" not in body


def test_배속은_엔진이_건다():
    """실측: `audio_tempo`를 주면 오디오와 정렬이 같이 당겨진다 (결정 6 개정)."""
    assert build_body(TEXT, voice_id="tc_test", tempo=1.1)["output"] == {
        "audio_format": "wav", "audio_tempo": 1.1,
    }
    # 1.0은 안 보낸다 — "이 편에 배속을 안 걸었다"가 요청에 남는다.
    assert build_body(TEXT, voice_id="tc_test")["output"] == {"audio_format": "wav"}


def test_배속_범위_밖은_호출_전에_막는다():
    for bad in (0.4, 2.1):
        with pytest.raises(TTSError, match="배속"):
            build_body(TEXT, voice_id="tc_test", tempo=bad)


def test_어댑터는_건_배속을_알린다():
    """`[3]`이 `tempo / narration.tempo`만 후처리하므로 배속이 겹치지 않는다."""
    transport = FakeTransport()
    client = TypecastClient(voice_id="tc_test", api_key="k", transport=transport)

    narration = client.synthesize(TEXT, tempo=1.1)
    assert json.loads(transport.calls[0][2])["output"]["audio_tempo"] == 1.1
    assert narration.tempo == 1.1 and narration.meta["tempo"] == 1.1


def test_생성자_배속이_호출_인자를_이긴다():
    """`[3]` 밖에서 값을 못 박아 쓰는 경로 (실험·프로브)."""
    transport = FakeTransport()
    client = TypecastClient(
        voice_id="tc_test", api_key="k", tempo=1.0, transport=transport
    )

    narration = client.synthesize(TEXT, tempo=1.2)
    assert "audio_tempo" not in json.loads(transport.calls[0][2])["output"]
    assert narration.tempo == 1.0


def test_언어는_ISO_639_3로_간다():
    assert LANGUAGE_CODES == {"ko": "kor", "ja": "jpn", "en": "eng"}
    client = TypecastClient(lang="ja", voice_id="tc_ja", api_key="k")
    assert client.language == "jpn"


def test_빈_텍스트는_호출_전에_막는다():
    transport = FakeTransport()
    client = TypecastClient(voice_id="tc_test", api_key="k", transport=transport)
    with pytest.raises(TTSError):
        client.synthesize("   ")
    assert transport.calls == []


def test_상한을_넘는_텍스트는_호출_전에_막는다():
    """넘으면 400이 오는데 그건 과금 뒤의 설정 오류다 (ADR-0081 결정 7)."""
    transport = FakeTransport()
    client = TypecastClient(voice_id="tc_test", api_key="k", transport=transport)
    with pytest.raises(TTSError, match=str(MAX_TEXT_CHARS)):
        client.synthesize("가" * (MAX_TEXT_CHARS + 1))
    assert transport.calls == []


# --- 오디오: 컨테이너를 벗긴다 (ADR-0081 결정 4) --------------------------------


def test_wav_컨테이너를_벗겨_PCM으로_넘긴다():
    narration = parse_response(response_payload(), voice_id="tc_test", model_id=MODEL_ID)
    assert narration.encoding == PCM_S16LE
    assert narration.audio == PCM  # 헤더가 아니라 프레임만 온다
    assert (narration.sample_rate, narration.channels, narration.sample_width) == (
        SAMPLE_RATE, 1, 2,
    )
    # 벗긴 PCM의 길이가 0.5초여야 `write_narration`의 wav 길이와 정렬이 맞는다.
    frames = len(narration.audio) / (narration.channels * narration.sample_width)
    assert frames / narration.sample_rate == pytest.approx(narration.raw_duration, abs=0.01)


def test_16bit가_아니면_거절한다():
    """`write_narration`은 pcm_s16le만 받는다 (tts/audio.py)."""
    wav = pcm_to_wav(b"\x00" * 3000, sample_rate=SAMPLE_RATE, sample_width=1)
    with pytest.raises(TTSError, match="16bit"):
        unwrap_wav(wav)


def test_wav가_아닌_오디오는_거절한다():
    payload = response_payload()
    payload["audio_format"] = "mp3"
    with pytest.raises(TTSError, match="wav"):
        parse_response(payload, voice_id="tc_test", model_id=MODEL_ID)


# --- 정렬 ---------------------------------------------------------------------


def test_정렬은_보낸_텍스트_그대로다():
    """씬 구간을 짚는 근거다 — 어긋나면 `[3]`의 대조 검사가 실패한다."""
    narration = parse_response(response_payload(), voice_id="tc_test", model_id=MODEL_ID)
    assert narration.alignment.text == TEXT
    assert len(narration.alignment.characters) == len(TEXT)
    assert narration.raw_duration == pytest.approx(0.5)


@pytest.mark.parametrize(
    "payload",
    [
        {"audio_format": "wav", "characters": characters_payload(TEXT)},  # 오디오 없음
        {"audio": base64.b64encode(pcm_to_wav(PCM, sample_rate=SAMPLE_RATE)).decode(),
         "audio_format": "wav"},  # 정렬 없음
        {"audio": base64.b64encode(pcm_to_wav(PCM, sample_rate=SAMPLE_RATE)).decode(),
         "audio_format": "wav", "characters": []},  # 빈 정렬
    ],
)
def test_오디오나_정렬이_없으면_실패한다(payload):
    with pytest.raises(TTSError):
        parse_response(payload, voice_id="tc_test", model_id=MODEL_ID)


def test_정렬_원소에_키가_없으면_실패한다():
    payload = response_payload()
    payload["characters"] = [{"text": "가", "start": 0.0}]
    with pytest.raises(TTSError, match="end"):
        parse_response(payload, voice_id="tc_test", model_id=MODEL_ID)


# --- 호출 전체 ----------------------------------------------------------------


def test_한_번의_호출로_한_편을_읽는다():
    transport = FakeTransport()
    client = TypecastClient(voice_id="tc_test", api_key="key", transport=transport)
    narration = client.synthesize(TEXT, timeout=42)

    assert len(transport.calls) == 1
    url, headers, body, timeout = transport.calls[0]
    assert f"granularity={GRANULARITY}" in url
    assert headers["X-API-KEY"] == "key"
    assert timeout == 42
    assert json.loads(body)["text"] == TEXT

    assert narration.provider == "typecast"
    assert narration.model_id == MODEL_ID
    assert narration.request_id is None  # 응답에 추적 ID가 없다
    assert narration.meta["provider"] == "typecast"


def test_voice_id는_언어별_환경변수에서_읽는다(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(VOICE_ID_ENVS["ja"], "tc_ja")
    assert TypecastClient(lang="ja").voice_id == "tc_ja"
    assert TypecastClient(lang="ko").voice_id == "tc_test"


def test_ko는_접미사_없는_옛_이름으로도_떨어진다(monkeypatch: pytest.MonkeyPatch):
    """엔진을 붙이기 전부터 `.env`에 있던 이름이다 (ADR-0081 결정 5)."""
    monkeypatch.delenv(VOICE_ID_ENVS["ko"], raising=False)
    monkeypatch.setenv(VOICE_ID_ENV, "tc_old")
    assert TypecastClient(lang="ko").voice_id == "tc_old"
    assert voice_envs_for("ko") == (VOICE_ID_ENVS["ko"], VOICE_ID_ENV)
    assert voice_envs_for("en") == (VOICE_ID_ENVS["en"],)


def test_id가_없으면_호출_전에_멈춘다(monkeypatch: pytest.MonkeyPatch):
    """세 언어를 도는데 둘째 언어의 id가 비면 첫 언어의 과금이 헛되다 (스펙 05 [3])."""
    monkeypatch.delenv(VOICE_ID_ENVS["en"], raising=False)
    with pytest.raises(TTSNotConfigured, match=VOICE_ID_ENVS["en"]):
        TypecastClient(lang="en").check_configured()


def test_키가_없어도_생성은_된다(monkeypatch: pytest.MonkeyPatch):
    """`--help`가 이 클래스를 만드는 것만으로 실패하면 안 된다."""
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    client = TypecastClient()
    with pytest.raises(TTSNotConfigured, match=API_KEY_ENV):
        client.check_configured()


@pytest.mark.parametrize(
    "status, expected",
    [
        (400, TTSNotConfigured),  # voice_id가 계정에 없다
        (401, TTSNotConfigured),
        (402, TTSRateLimited),  # 크레딧 소진 — 재시도해도 같다
        (404, TTSNotConfigured),
        (422, TTSError),
        (429, TTSRateLimited),
        (500, TTSError),
    ],
)
def test_상태코드를_예외로_가른다(status, expected):
    transport = FakeTransport(status=status, payload=b'{"detail": "nope"}')
    client = TypecastClient(voice_id="tc_test", api_key="k", transport=transport)
    with pytest.raises(expected):
        client.synthesize(TEXT)


def test_JSON이_아닌_200은_실패한다():
    transport = FakeTransport(payload=b"<html>maintenance</html>")
    client = TypecastClient(voice_id="tc_test", api_key="k", transport=transport)
    with pytest.raises(TTSError):
        client.synthesize(TEXT)
