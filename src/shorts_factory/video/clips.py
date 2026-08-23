"""씬 클립 정규화 + 검수 프레임 추출 — `[7. videogen]`의 FFmpeg 필터. ADR-0056.

`timeline.py`·`ending.py`와 같은 자리다 — 전부 순수 함수이고 프로세스는
`ffmpeg.run_ffmpeg`가 부른다.

## 왜 정규화하는가

프로바이더가 내는 클립은 720×1280 / 24fps / h264 + **aac**이고(Omni 프로브 실측) 길이가
요청과 몇 프레임 어긋날 수 있다. `[9]`의 기하는 "클립 길이 ≥ 씬 길이 + 0.6초"를 전제로
`trim`한다 (ADR-0024) — 클립이 그보다 짧으면 xfade가 소진할 프레임이 없다. 그래서 `[7]`이
자기 계약을 **파일로** 보장한다 (D-6의 반대 방향이 아니다 — 하류 편의가 아니라 자기
산출물의 규격이다):

    scale=1080:1920, setsar=1, fps=30           → specs/00 규격, `[9]`의 xfade·concat 입력 통일
    tpad=stop_mode=clone:stop_duration=N        → 짧으면 마지막 프레임을 복제해 N초까지
    trim=end=N, setpts                          → 길면 N초에서 자른다
    -an                                         → 오디오는 버린다 (스펙 05 — `[10]` 소관)

## 검수 프레임

시작·중간·끝 3장 (스펙 05 `[7]` ②). 끝 프레임은 OCR 게이트의 입력이기도 하다 (결정 6) —
`N - 1/fps`에서 뽑아 마지막으로 렌더된 프레임을 본다.
"""

from __future__ import annotations

from .ffmpeg import CRF, FPS, HEIGHT, PIXEL_FORMAT, PRESET, VIDEO_CODEC, WIDTH

#: 검수 프레임의 위치 이름. 순서대로 시작·중간·끝이다.
FRAME_POSITIONS: tuple[str, ...] = ("start", "mid", "end")


class ClipError(Exception):
    """클립을 필터로 옮길 수 없음."""


def normalize_filter(seconds: float, *, width: int = WIDTH, height: int = HEIGHT, fps: int = FPS) -> str:
    """프로바이더 클립 → 규격 클립의 `-vf` 문자열. 길이는 정확히 `seconds`가 된다."""
    if seconds <= 0:
        raise ClipError(f"클립 길이는 0보다 커야 한다: {seconds}")
    return (
        f"scale={width}:{height},setsar=1,fps={fps},"
        f"tpad=stop_mode=clone:stop_duration={seconds:.3f},"
        f"trim=end={seconds:.3f},setpts=PTS-STARTPTS,"
        f"format={PIXEL_FORMAT}"
    )


def normalize_command(
    source: str,
    output: str,
    *,
    seconds: float,
    executable: str = "ffmpeg",
    fps: int = FPS,
    crf: int = CRF,
    preset: str = PRESET,
) -> list[str]:
    """프로바이더 mp4 → `clips/{scene_id}.mp4` 규격 (1080×1920 · 30fps · h264 · 무음 · 정확한 길이)."""
    return [
        executable, "-y", "-hide_banner", "-loglevel", "error",
        "-i", source,
        "-vf", normalize_filter(seconds, fps=fps),
        "-an",
        "-c:v", VIDEO_CODEC,
        "-preset", preset,
        "-crf", str(crf),
        "-pix_fmt", PIXEL_FORMAT,
        "-r", str(fps),
        "-movflags", "+faststart",
        output,
    ]


def frame_times(seconds: float, *, fps: int = FPS) -> dict[str, float]:
    """검수 프레임 3장의 시각. 끝은 마지막으로 렌더된 프레임이다.

    `-ss`는 그 시각 **이상**의 첫 프레임을 고르므로 마지막 프레임(PTS = N − 1/fps)을 잡으려면
    그보다 반 프레임 앞을 가리킨다 — 정확히 N − 1/fps를 주면 반올림에 따라 빈 출력이 된다
    (2026-08-23 오프라인 관통 실측: 5.967초 → 프레임 없음).
    """
    if seconds <= 0:
        raise ClipError(f"클립 길이는 0보다 커야 한다: {seconds}")
    last = max(0.0, seconds - 1.5 / fps)
    return {"start": 0.0, "mid": round(seconds / 2, 3), "end": round(last, 3)}


#: 검수 프레임 형식. PNG다 — FFmpeg 9의 mjpeg 인코더가 yuv420p(제한 범위)를 거부한다
#: (`Non full-range YUV is non-standard`, 2026-08-23 오프라인 관통 실측). PNG는 무손실이라
#: OCR에도 낫다.
FRAME_SUFFIX = ".png"


def frame_command(source: str, output: str, *, at: float, executable: str = "ffmpeg") -> list[str]:
    """클립의 `at`초 프레임 한 장 → 이미지 파일. `-ss`를 입력 앞에 두어 빠르게 찾는다."""
    if at < 0:
        raise ClipError(f"프레임 시각은 음수일 수 없다: {at}")
    return [
        executable, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{at:.3f}",
        "-i", source,
        "-frames:v", "1",
        output,
    ]
