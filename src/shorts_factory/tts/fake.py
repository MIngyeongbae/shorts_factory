"""테스트용 TTS 어댑터 + FFmpeg 대역. `llm/fake.py`와 같은 자리다.

`[3. tts+sync]`의 판단은 전부 문자 정렬 위에서 일어나므로(경계 추출, 배속 보정,
오차 경고, 길이 초과 중단) API 키 없이 단계 전체를 돌릴 수 있어야 한다. 이 모듈이
그 대역을 선다.

## 페이크가 흉내 내는 것

문자마다 시간을 배분해 정렬을 만든다. 본문 글자는 `1/speed`초, 문장부호는 `pause`초,
공백은 `gap`초다. 기본 `speed`는 **원속** 기준이라 atempo 1.1을 거치면 1부가 `est_*`를
계산할 때 쓴 명목 5.85자/초에 떨어진다 — 그래야 페이크로 돌린 결과가 추정과 맞고,
오차 경고를 테스트할 때는 `speed`를 일부러 틀리게 주면 된다.

발음 품질·톤은 흉내 내지 않는다. 그건 페이크로 검증할 수 있는 것이 아니다.
"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path
from typing import Any, Callable, Sequence

from .base import PCM_S16LE, Alignment, Narration, TTSClient, TTSError
from .audio import DEFAULT_TEMPO

#: 1부가 `est_*`를 만들 때 쓰는 명목 발화 속도(자/초). specs/01의 5.3~6.3 중앙 부근.
NOMINAL_FINAL_SPEED = 5.85

#: 페이크의 기본 원속. atempo 후 위 명목 속도가 되도록 잡는다.
DEFAULT_RAW_SPEED = NOMINAL_FINAL_SPEED / DEFAULT_TEMPO

#: 시간을 따로 배분하는 문자. 문장부호는 운율상 쉼이 붙는 자리다 (specs/04).
PUNCTUATION = frozenset(".?!…,·;:")

#: 페이크 오디오 샘플레이트. 실물(pcm_24000/44100)보다 낮게 잡아 테스트가 몇 MB짜리
#: 바이트열을 들고 다니지 않게 한다. 컨테이너 헤더에 값이 그대로 실리므로 길이 계산은
#: 실물과 같은 경로를 탄다.
FAKE_SAMPLE_RATE = 8000


def fake_alignment(
    text: str, *, speed: float = DEFAULT_RAW_SPEED, pause: float = 0.0, gap: float = 0.0
) -> Alignment:
    """텍스트에서 그럴듯한 문자 정렬을 만든다. 시각은 원속 기준이다."""
    if speed <= 0:
        raise TTSError(f"speed는 0보다 커야 한다: {speed}")

    characters: list[str] = []
    starts: list[float] = []
    ends: list[float] = []

    cursor = 0.0
    for char in text:
        if char.isspace():
            span = gap
        elif char in PUNCTUATION:
            span = pause
        else:
            span = 1.0 / speed
        characters.append(char)
        starts.append(round(cursor, 6))
        cursor += span
        ends.append(round(cursor, 6))

    return Alignment(characters=characters, starts=starts, ends=ends)


def silence_pcm(duration: float, *, sample_rate: int, channels: int = 1) -> bytes:
    """길이만 맞춘 무음 16bit PCM."""
    frames = max(1, round(duration * sample_rate))
    return b"\x00\x00" * frames * channels


def fake_narration(
    text: str,
    *,
    speed: float = DEFAULT_RAW_SPEED,
    pause: float = 0.0,
    gap: float = 0.0,
    sample_rate: int = FAKE_SAMPLE_RATE,
    voice_id: str = "fake-voice",
    model_id: str = "fake-model",
) -> Narration:
    alignment = fake_alignment(text, speed=speed, pause=pause, gap=gap)
    return Narration(
        audio=silence_pcm(alignment.duration, sample_rate=sample_rate),
        alignment=alignment,
        sample_rate=sample_rate,
        encoding=PCM_S16LE,
        request_id="fake-request",
        voice_id=voice_id,
        model_id=model_id,
    )


class FakeTTSClient(TTSClient):
    """`responses`를 주면 순서대로 돌려주고, 안 주면 텍스트에서 만들어 낸다."""

    def __init__(
        self,
        responses: Sequence[Narration | Callable[[str], Narration] | Exception] | None = None,
        *,
        speed: float = DEFAULT_RAW_SPEED,
        pause: float = 0.0,
        gap: float = 0.0,
        sample_rate: int = FAKE_SAMPLE_RATE,
    ) -> None:
        self._responses = None if responses is None else list(responses)
        self.speed = speed
        self.pause = pause
        self.gap = gap
        self.sample_rate = sample_rate
        self.calls: list[dict[str, Any]] = []

    def synthesize(
        self, text: str, *, timeout: int | None = None, label: str = ""
    ) -> Narration:
        self.calls.append({"text": text, "timeout": timeout, "label": label})

        if self._responses is None:
            return fake_narration(
                text,
                speed=self.speed,
                pause=self.pause,
                gap=self.gap,
                sample_rate=self.sample_rate,
            )

        if not self._responses:
            raise TTSError(f"준비된 픽스처 응답이 없다 (label={label})")

        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item(text) if callable(item) else item


def _parse_atempo(cmd: Sequence[str]) -> tuple[Path, Path, float]:
    """FFmpeg 명령에서 (입력, 출력, 배속)을 뽑는다."""
    args = list(cmd)
    try:
        src = Path(args[args.index("-i") + 1])
    except (ValueError, IndexError) as exc:
        raise AssertionError(f"입력(-i)이 없는 FFmpeg 명령이다: {args}") from exc

    tempo = 1.0
    for arg in args:
        if arg.startswith("atempo="):
            tempo = float(arg.split("=", 1)[1])
            break
    return src, Path(args[-1]), tempo


class FakeFFmpeg:
    """`apply_atempo`가 부르는 subprocess.run 대역. 프레임 수만 배속대로 줄인다.

    음정 보존은 흉내 내지 않는다 — 이 단계가 FFmpeg에 기대는 것은 "길이가 1/tempo로
    줄어든 wav" 하나뿐이고, 그것만 맞으면 타임스탬프 검증이 성립한다.
    """

    def __init__(self, *, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, cmd: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append(list(cmd))
        if self.returncode != 0:
            return subprocess.CompletedProcess(list(cmd), self.returncode, "", self.stderr)

        src, dst, tempo = _parse_atempo(cmd)
        with wave.open(str(src), "rb") as source:
            params = source.getparams()
            frames = source.readframes(params.nframes)

        keep = max(1, int(params.nframes / tempo))
        width = params.sampwidth * params.nchannels
        with wave.open(str(dst), "wb") as out:
            out.setnchannels(params.nchannels)
            out.setsampwidth(params.sampwidth)
            out.setframerate(params.framerate)
            out.writeframes(frames[: keep * width])

        return subprocess.CompletedProcess(list(cmd), 0, "", "")
