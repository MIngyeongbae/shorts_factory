"""나레이션 오디오 후처리 (ADR-0004의 atempo 1.1).

FFmpeg 없이 도는 부분(wav 컨테이너, 길이 계산, 명령 조립)과 FFmpeg에 넘기는 부분을
갈라 본다. 실제 FFmpeg는 부르지 않는다 — `runner` 주입 지점이 그래서 있다.
"""

import subprocess
import wave

import pytest

from shorts_factory.tts.audio import (
    AudioError,
    FFmpegError,
    apply_atempo,
    atempo_command,
    pcm_to_wav,
    wav_duration,
    write_narration,
)
from shorts_factory.tts.fake import FakeFFmpeg, fake_narration, silence_pcm

SAMPLE_RATE = 8000


def narration(text: str = "한 줄입니다.", **kwargs):
    return fake_narration(text, sample_rate=SAMPLE_RATE, **kwargs)


# --- wav 컨테이너 -------------------------------------------------------------


def test_pcm_to_wav_keeps_frames_and_rate(tmp_path):
    pcm = silence_pcm(2.0, sample_rate=SAMPLE_RATE)
    path = tmp_path / "a.wav"
    path.write_bytes(pcm_to_wav(pcm, sample_rate=SAMPLE_RATE))

    with wave.open(str(path)) as src:
        assert src.getframerate() == SAMPLE_RATE
        assert src.getnchannels() == 1
        assert src.getsampwidth() == 2
    assert wav_duration(path) == pytest.approx(2.0)


def test_pcm_length_must_be_a_whole_number_of_frames():
    with pytest.raises(AudioError, match="배수가 아니다"):
        pcm_to_wav(b"\x00\x00\x00", sample_rate=SAMPLE_RATE)


def test_wav_duration_rejects_non_wav(tmp_path):
    path = tmp_path / "b.wav"
    path.write_bytes(b"not a wav")
    with pytest.raises(AudioError, match="wav로 읽을 수 없다"):
        wav_duration(path)


# --- atempo 명령 --------------------------------------------------------------


def test_atempo_command_uses_the_audio_filter(tmp_path):
    cmd = atempo_command(tmp_path / "in.wav", tmp_path / "out.wav", 1.1, executable="ffmpeg")
    assert "-filter:a" in cmd
    assert "atempo=1.1" in cmd
    assert cmd[-1] == str(tmp_path / "out.wav")


def test_tempo_outside_the_single_filter_range_is_refused(tmp_path):
    with pytest.raises(AudioError, match="범위"):
        apply_atempo(tmp_path / "in.wav", tmp_path / "out.wav", tempo=3.0)


def test_ffmpeg_failure_surfaces_stderr(tmp_path):
    src = tmp_path / "in.wav"
    src.write_bytes(pcm_to_wav(silence_pcm(1.0, sample_rate=SAMPLE_RATE), sample_rate=SAMPLE_RATE))

    with pytest.raises(FFmpegError, match="atempo 필터 실패"):
        apply_atempo(
            src, tmp_path / "out.wav", tempo=1.1,
            runner=FakeFFmpeg(returncode=1, stderr="atempo 필터 실패"),
        )


def test_missing_ffmpeg_is_reported_as_a_path_problem(tmp_path):
    def missing(*_args, **_kwargs):
        raise FileNotFoundError("ffmpeg")

    with pytest.raises(FFmpegError, match="PATH"):
        apply_atempo(tmp_path / "in.wav", tmp_path / "out.wav", tempo=1.1, runner=missing)


def test_ffmpeg_timeout_is_reported(tmp_path):
    def slow(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("ffmpeg", 300)

    with pytest.raises(FFmpegError, match="끝나지 않았다"):
        apply_atempo(tmp_path / "in.wav", tmp_path / "out.wav", tempo=1.1, runner=slow)


# --- write_narration ----------------------------------------------------------


def test_write_narration_shortens_audio_by_the_tempo(tmp_path):
    speech = narration("이 줄은 배속 대상입니다.")
    dest = tmp_path / "narration.wav"

    duration = write_narration(speech, dest, tempo=1.1, runner=FakeFFmpeg())

    assert duration == pytest.approx(speech.raw_duration / 1.1, abs=0.01)
    assert wav_duration(dest) == pytest.approx(duration)


def test_tempo_one_skips_ffmpeg_entirely(tmp_path):
    runner = FakeFFmpeg()
    speech = narration()

    duration = write_narration(speech, tmp_path / "n.wav", tempo=1.0, runner=runner)

    assert runner.calls == []
    assert duration == pytest.approx(speech.raw_duration, abs=0.01)


def test_raw_audio_is_kept_when_ffmpeg_fails(tmp_path):
    """호출은 편당 과금이다. 후처리가 깨졌다고 원본을 버리지 않는다."""
    dest = tmp_path / "narration.wav"

    with pytest.raises(FFmpegError, match="원속 오디오는"):
        write_narration(narration(), dest, tempo=1.1, runner=FakeFFmpeg(returncode=1))

    assert dest.with_suffix(".raw.wav").exists()


def test_raw_audio_is_removed_on_success(tmp_path):
    dest = tmp_path / "narration.wav"
    write_narration(narration(), dest, tempo=1.1, runner=FakeFFmpeg())

    assert not dest.with_suffix(".raw.wav").exists()


def test_non_pcm_encoding_is_refused(tmp_path):
    speech = narration()
    speech.encoding = "mp3_44100_128"

    with pytest.raises(AudioError, match="output_format=pcm"):
        write_narration(speech, tmp_path / "n.wav", tempo=1.1, runner=FakeFFmpeg())
