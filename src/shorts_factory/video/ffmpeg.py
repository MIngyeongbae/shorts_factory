"""FFmpeg 명령 조립 + 실행 경계. specs/00 산출물 정의, specs/03 전환 규칙.

## 왜 조립과 실행을 나누는가

전환·자막은 전부 **필터 그래프 문자열 하나**로 표현된다. 그 문자열이 맞는지는 FFmpeg를
돌리지 않고도 검사할 수 있고, 반대로 FFmpeg를 돌려서 확인하려면 클립 27개와 몇 분이
필요하다. 그래서 이 모듈의 `build_*`는 전부 순수 함수이고, 프로세스를 부르는 것은
`run_ffmpeg` 하나뿐이다 — `tts/audio.py`의 `atempo_command`/`apply_atempo`와 같은 구조다.

## 그래프 모양

클립마다 앞부분을 다듬어(`trim`) 규격을 맞춘 뒤, 씬 순서대로 하나씩 접는다.

    [i:v] trim → setpts → scale → setsar → fps → format  ⇒ [c{i}]
    디졸브 junction: [acc][c{i}] xfade=fade:duration=0.6:offset={씬 start}
    하드컷 junction: [acc][c{i}] concat=n=2:v=1:a=0
    마지막: [acc] ass=filename=subtitles.ass ⇒ [vout]

`xfade`의 `offset`은 **첫 입력(=누적 스트림)의 시각**이고, 누적 스트림은 항상 0초에서
시작하는 절대 시간축이라 그 값이 곧 씬의 `start`다 (timeline.py의 불변식). 그래서
오프셋이 클립 길이의 누적합으로 계산되지 않는다 — 클립 하나가 계약보다 짧아도 그
뒤 씬들의 자막 싱크가 밀리지 않는다.

`concat`은 하드컷 자리에서 쓴다. `xfade`는 `duration=0`을 받지 않으므로 컷을 아주 짧은
디졸브로 흉내 내는 대신, 겹침이 없는 이어붙이기를 그대로 쓴다.

## 자막은 마지막에 얹는다

`ass` 필터를 그래프 맨 끝에 두면 자막이 전환에 섞이지 않는다. 디졸브 구간에서 앞
씬 자막이 함께 페이드되면 두 줄이 겹쳐 보인다 — 자막은 레이어 B이고 이미지와 다른
층이다 (ADR-0002).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from .timeline import DISSOLVE, HARD_CUT, Timeline

#: specs/00 "9:16 세로, 1080×1920, 30fps, h264 + aac"
WIDTH = 1080
HEIGHT = 1920
FPS = 30
VIDEO_CODEC = "libx264"
PIXEL_FORMAT = "yuv420p"
PRESET = "medium"
CRF = 18

DEFAULT_FFMPEG = "ffmpeg"

#: 클립 27개를 x264로 다시 인코딩하는 시간. atempo 한 번(300초)보다 넉넉해야 한다.
FFMPEG_TIMEOUT = 900

Runner = Callable[..., Any]


class FFmpegError(Exception):
    """FFmpeg 실행 실패."""


def relative_path(path: str | Path, base: Path | None = None) -> str:
    """`base`(= 실행 디렉터리) 기준 상대 경로. 구분자는 `/`로 통일한다.

    명령 인자(`-i`)에 그대로 쓰는 값이라 escape하지 않는다. FFmpeg는 argv를 셸을 거치지
    않고 받으므로 경로에 escape를 넣으면 오히려 파일을 못 찾는다.
    """
    text = str(path)
    if base is not None:
        try:
            text = os.path.relpath(text, str(base))
        except ValueError:  # 드라이브가 다르면 상대 경로가 없다 (Windows)
            pass
    return text.replace("\\", "/")


def escape_filter_path(path: str | Path, base: Path | None = None) -> str:
    """**필터 인자**에 넣을 경로.

    필터 그래프에서 `:`는 인자 구분자라 Windows 드라이브 문자(`C:`)가 그대로 들어가면
    그래프가 깨진다. `base`를 주면 상대 경로가 되어 escape할 것이 거의 남지 않는다.
    """
    return re.sub(r"([:'\\,;\[\]])", r"\\\1", relative_path(path, base))


def clip_filter(
    index: int,
    *,
    length: float,
    label: str,
    width: int = WIDTH,
    height: int = HEIGHT,
    fps: int = FPS,
) -> str:
    """입력 클립 하나를 규격에 맞춘다.

    `trim`이 자르는 길이는 timeline.py가 정한 `clip_length`다 — 디졸브가 뒤따르면
    겹침 0.6초를 포함하고, 하드컷이나 마지막 클립이면 씬 길이만 남는다. `xfade`와
    `concat`은 두 입력의 해상도·SAR·픽셀 포맷이 같아야 하므로 여기서 통일한다.
    """
    return (
        f"[{index}:v]"
        f"trim=end={length:.3f},setpts=PTS-STARTPTS,"
        f"scale={width}:{height},setsar=1,fps={fps},settb=1/{fps},"
        f"format={PIXEL_FORMAT}"
        f"[{label}]"
    )


def build_filter_graph(
    timeline: Timeline,
    *,
    subtitles: str,
    fontsdir: str | None = None,
    width: int = WIDTH,
    height: int = HEIGHT,
    fps: int = FPS,
    output_label: str = "vout",
) -> str:
    """전환 계획 + 자막 파일 → `-filter_complex` 문자열.

    `subtitles`/`fontsdir`은 **이미 escape된** 경로 문자열이다 (`escape_filter_path`).
    """
    segments = timeline.segments
    if not segments:
        raise FFmpegError("클립이 없다")

    steps = [
        clip_filter(
            index,
            length=segment.clip_length,
            label=f"c{index}",
            width=width,
            height=height,
            fps=fps,
        )
        for index, segment in enumerate(segments)
    ]

    accumulator = "c0"
    for index, segment in enumerate(segments[1:], start=1):
        merged = f"m{index}"
        if segment.transition_in == DISSOLVE:
            steps.append(
                f"[{accumulator}][c{index}]"
                f"xfade=transition=fade:duration={segment.dissolve:.3f}"
                f":offset={segment.start:.3f}[{merged}]"
            )
        elif segment.transition_in == HARD_CUT:
            # concat은 출력 타임베이스를 AV_TIME_BASE(1/1000000)로 바꾼다. 그대로 두면
            # 그 뒤 첫 xfade가 "timebase do not match"로 설정에 실패한다 — 실물
            # 6씬(피사, 3번이 하드컷) 첫 실행에서 드러났다. 격자를 즉시 되돌린다.
            steps.append(
                f"[{accumulator}][c{index}]concat=n=2:v=1:a=0,settb=1/{fps}[{merged}]"
            )
        else:
            raise FFmpegError(
                f"scenes/{segment.scene_id}: 알 수 없는 전환 '{segment.transition_in}'"
            )
        accumulator = merged

    burn = f"ass=filename={subtitles}"
    if fontsdir:
        burn += f":fontsdir={fontsdir}"
    steps.append(f"[{accumulator}]{burn}[{output_label}]")

    return ";".join(steps)


def build_command(
    timeline: Timeline,
    *,
    inputs: list[str],
    filter_graph: str,
    output: str,
    executable: str = DEFAULT_FFMPEG,
    fps: int = FPS,
    crf: int = CRF,
    preset: str = PRESET,
    output_label: str = "vout",
) -> list[str]:
    """FFmpeg 인자 배열. `inputs`는 씬 순서와 같아야 한다.

    소리는 넣지 않는다(`-an`). 나레이션·SFX·BGM은 `[10. mix]`가 붙인다 (specs/05).
    """
    if len(inputs) != len(timeline.segments):
        raise FFmpegError(
            f"입력 {len(inputs)}개 / 클립 {len(timeline.segments)}개 — 개수가 다르다"
        )

    cmd = [executable, "-y", "-hide_banner", "-loglevel", "error"]
    for source in inputs:
        cmd += ["-i", source]
    cmd += [
        "-filter_complex", filter_graph,
        "-map", f"[{output_label}]",
        "-an",
        "-c:v", VIDEO_CODEC,
        "-preset", preset,
        "-crf", str(crf),
        "-pix_fmt", PIXEL_FORMAT,
        "-r", str(fps),
        "-movflags", "+faststart",
        output,
    ]
    return cmd


def run_ffmpeg(
    cmd: list[str],
    *,
    cwd: Path,
    produces: Path,
    runner: Runner = subprocess.run,
    timeout: int = FFMPEG_TIMEOUT,
) -> None:
    """이 패키지에서 프로세스를 부르는 **유일한 지점**.

    `cwd`를 run 디렉터리로 두면 명령 안의 경로가 전부 상대 경로가 되어 필터 그래프
    escape가 거의 필요 없어지고, 로그에 남은 명령을 사람이 그대로 다시 실행할 수 있다.
    """
    try:
        proc = runner(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise FFmpegError(
            f"FFmpeg를 찾을 수 없다: {cmd[0]!r}. 설치와 PATH를 확인하라."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"FFmpeg가 {timeout}초 안에 끝나지 않았다") from exc

    if getattr(proc, "returncode", 1) != 0:
        stderr = (getattr(proc, "stderr", "") or "").strip()[:1000]
        raise FFmpegError(f"FFmpeg 실패 (exit={proc.returncode}): {stderr}")
    if not produces.exists():
        raise FFmpegError(f"FFmpeg가 산출물을 쓰지 않았다: {produces}")
