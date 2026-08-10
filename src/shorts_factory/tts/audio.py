"""나레이션 오디오 후처리 — PCM → wav 컨테이너 → atempo. ADR-0004.

specs/05 `[3. tts+sync]`: "atempo 1.1 적용 후 타임스탬프도 1/1.1 스케일 보정."
ADR-0004: "원속 생성 후 FFmpeg atempo 1.1 후처리를 기본값으로."

## 왜 PCM만 받는가

ElevenLabs는 wav 컨테이너를 내주지 않는다 (`mp3_*` 또는 원시 PCM `pcm_*`). 산출물
계약은 `narration.wav`이므로 둘 중 하나를 골라야 하는데, PCM을 받으면 컨테이너 씌우기가
표준 라이브러리 `wave`로 끝난다 — 새 의존성도, 디코더도 필요 없다. mp3를 받으면
FFmpeg 없이는 길이조차 잴 수 없다.

샘플레이트는 어댑터가 정해 `Narration.sample_rate`로 알려준다. 이 모듈은 값을 그대로
헤더에 쓴다 (계정 등급에 따라 `pcm_44100`이 막히고 `pcm_24000`만 되는 경우가 있다).

## FFmpeg 호출 지점

`atempo` 하나뿐이다. `runner`를 주입할 수 있어 FFmpeg 없이도 단계 전체를 테스트한다.
`tempo == 1.0`이면 아예 부르지 않는다.
"""

from __future__ import annotations

import io
import logging
import subprocess
import wave
from pathlib import Path
from typing import Any, Callable

from .base import PCM_S16LE, Narration

log = logging.getLogger(__name__)

#: ADR-0004 — 원속 생성 후 후처리 배속 기본값
DEFAULT_TEMPO = 1.1

#: FFmpeg `atempo` 필터 1개가 커버하는 범위. 밖으로 나가면 필터를 체인해야 하는데,
#: 그건 배속을 스펙 밖으로 끌고 가는 결정이라 여기서 임의로 하지 않는다.
TEMPO_RANGE = (0.5, 2.0)

DEFAULT_FFMPEG = "ffmpeg"
FFMPEG_TIMEOUT = 300

Runner = Callable[..., Any]


class AudioError(Exception):
    """오디오 후처리 실패."""


class FFmpegError(AudioError):
    """FFmpeg 실행 실패."""


def pcm_to_wav(
    pcm: bytes, *, sample_rate: int, channels: int = 1, sample_width: int = 2
) -> bytes:
    """원시 PCM에 wav(RIFF) 헤더를 씌운다."""
    frame = channels * sample_width
    if frame <= 0:
        raise AudioError(f"채널/샘플폭이 잘못됐다 (channels={channels}, width={sample_width})")
    if len(pcm) % frame:
        raise AudioError(
            f"PCM 길이({len(pcm)}바이트)가 프레임 크기({frame}바이트)의 배수가 아니다"
        )

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(sample_width)
        out.setframerate(sample_rate)
        out.writeframes(pcm)
    return buffer.getvalue()


def wav_duration(path: Path) -> float:
    """wav 파일의 실제 재생 길이(초)."""
    try:
        with wave.open(str(path), "rb") as src:
            rate = src.getframerate()
            if rate <= 0:
                raise AudioError(f"샘플레이트가 0이다: {path}")
            return src.getnframes() / rate
    except wave.Error as exc:
        raise AudioError(f"wav로 읽을 수 없다: {path} — {exc}") from exc


def atempo_command(src: Path, dst: Path, tempo: float, *, executable: str) -> list[str]:
    """FFmpeg 명령. 테스트가 명령 자체를 검사할 수 있게 따로 뺐다."""
    return [
        executable,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-i", str(src),
        "-filter:a", f"atempo={tempo}",
        str(dst),
    ]


def apply_atempo(
    src: Path,
    dst: Path,
    *,
    tempo: float,
    executable: str = DEFAULT_FFMPEG,
    runner: Runner = subprocess.run,
    timeout: int = FFMPEG_TIMEOUT,
) -> None:
    """`src`를 `tempo`배로 빠르게 만들어 `dst`에 쓴다 (음정 유지)."""
    low, high = TEMPO_RANGE
    if not low <= tempo <= high:
        raise AudioError(f"배속 {tempo}는 atempo 단일 필터 범위({low}~{high}) 밖이다")

    cmd = atempo_command(src, dst, tempo, executable=executable)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise FFmpegError(
            f"FFmpeg를 찾을 수 없다: {executable!r}. 설치와 PATH를 확인하라."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"FFmpeg가 {timeout}초 안에 끝나지 않았다") from exc

    if getattr(proc, "returncode", 1) != 0:
        stderr = (getattr(proc, "stderr", "") or "").strip()[:500]
        raise FFmpegError(f"FFmpeg 실패 (exit={proc.returncode}): {stderr}")
    if not dst.exists():
        raise FFmpegError(f"FFmpeg가 산출물을 쓰지 않았다: {dst}")


def write_narration(
    narration: Narration,
    dest: Path,
    *,
    tempo: float = DEFAULT_TEMPO,
    executable: str = DEFAULT_FFMPEG,
    runner: Runner = subprocess.run,
) -> float:
    """나레이션을 `dest`(wav)에 쓰고 실제 길이(초)를 돌려준다.

    원속 wav는 `dest.raw.wav`에 잠깐 두었다가 성공 시 지운다. **실패하면 남긴다** —
    TTS 호출은 편당 과금이라 후처리 실패로 원본까지 날리면 다시 사야 한다.
    """
    if narration.encoding != PCM_S16LE:
        raise AudioError(
            f"지원하지 않는 인코딩이다: {narration.encoding!r}. "
            "어댑터에서 output_format=pcm_* 로 요청하라 (모듈 독스트링 참고)."
        )

    data = pcm_to_wav(
        narration.audio,
        sample_rate=narration.sample_rate,
        channels=narration.channels,
        sample_width=narration.sample_width,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)

    if tempo == 1.0:
        dest.write_bytes(data)
        return wav_duration(dest)

    raw_path = dest.with_suffix(".raw.wav")
    raw_path.write_bytes(data)
    try:
        apply_atempo(
            raw_path, dest, tempo=tempo, executable=executable, runner=runner
        )
    except AudioError as exc:
        raise FFmpegError(f"{exc} 원속 오디오는 {raw_path}에 남겼다") from exc

    raw_path.unlink(missing_ok=True)
    return wav_duration(dest)
