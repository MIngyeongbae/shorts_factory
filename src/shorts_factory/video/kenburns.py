"""Ken Burns — 정지 이미지 한 장 + `camera` → 클립 하나의 FFmpeg 필터.

specs/03 "카메라 워크 파라미터" 표를 그대로 옮긴 것이다. 값을 여기서 정하지 않는다
(CLAUDE.md 원칙 3) — 표에 없는 카메라가 오면 기본값으로 흘려보내지 않고 멈춘다.

## 왜 조립과 실행을 나누는가

`ffmpeg.py`와 같은 이유다. 필터 문자열이 맞는지는 FFmpeg를 돌리지 않고 검사할 수
있고, 돌려서 확인하려면 클립 27개와 몇 분이 든다. 이 모듈은 전부 순수 함수이고
프로세스는 `ffmpeg.run_ffmpeg`가 부른다.

## 체인 모양

    -loop 1 -framerate 30 -i {image}
      crop      → 원본을 9:16으로 먼저 자른다
      zoompan   → 그 안에서 창을 움직이며 1080×1920으로 뽑는다
      setsar=1
    -frames:v N

**`crop`이 먼저다.** 원본은 9:16(0.5625)보다 살짝 세로로 길다 (MJ 사분면 816×1456,
NB2 1536×2752 둘 다). 비율을 안 맞추고 zoompan에 넣으면 `s=1080x1920`이 그림을 눌러
늘린다. MJ 경로에서는 크롭 뒤 **1.32~1.45배 업스케일**이 붙는다 (스펙 03).

**`static`은 zoompan을 타지 않는다.** 움직임이 없으면 창도 없다 — 크롭 후 스케일
한 번이 픽셀 단위로 정확하고, zoompan의 정수 반올림도 타지 않는다.

## zoompan 앞에 `format=gbrp`가 반드시 붙는다

zoompan은 잘라 낼 창의 원점을 **크로마 정렬에 맞춰 버림**한다
(`px = (int)x & ~(chroma_w-1)`). 입력이 yuv420이면 그 정렬이 2픽셀이라, 초당 1.2픽셀로
움직이는 느린 팬에서 **창이 몇 프레임씩 멈췄다가 2픽셀을 뛴다.**

탐침(후버댐 27번, 116프레임 `pan_right`)으로 실측했다. 프레임 간 차이가
`0.02 → 14.2 → 0.02`로 진동한다 — 40%의 프레임이 완전한 정지다.

| 입력 포맷 | 프레임차 변동계수 | 클립당 시간 |
|---|---|---|
| yuv420 (기본) | **81.2%** | 3.5초 |
| **gbrp** | **22.9%** | 3.9초 |
| gbrp + 2배 업스케일 | 16.2% | 5.2초 |

크로마 서브샘플링이 없으면 정렬이 1픽셀이 되어 정지 프레임이 사라진다. 2배 업스케일은
거기서 더 얻는 것이 적고 시간을 49% 더 쓴다. 펜선·작도선이 많은 ADR-0023 룩에서
이 떨림은 눈에 띄는 종류라 비용 11%는 싸다.

## 프레임 수는 올림이다

`N = ceil(clip_length × 30)`. 계약 길이가 30fps 격자에 안 떨어질 때 **모자라는 쪽으로
깎으면 안 된다** — `[9]`의 xfade는 앞 클립이 `offset + 0.6초`까지 그림을 주고 있어야
하고, 한 프레임이라도 비면 그 자리가 언다. 넘치는 몫은 `[9]`의 `trim`이 자른다
(`ffmpeg.clip_filter`).

## 화질 — 중간 산출물이라 `[9]`보다 좋게 뽑는다

이 클립은 `[9]`에서 한 번 더 인코딩된다. 두 세대를 같은 CRF로 태우면 손실이 겹치므로
여기서만 CRF를 낮춘다. 편당 27클립 × 4초라 용량은 문제되지 않는다.
"""

from __future__ import annotations

import math

from .ffmpeg import FPS, HEIGHT, PIXEL_FORMAT, VIDEO_CODEC, WIDTH

#: specs/03 "이동량 = 프레임의 10%". 줌 깊이와 팬·틸트 이동 폭을 함께 정하는 상수다 —
#: 1.10으로 크롭하면 그 크롭이 움직일 수 있는 여백이 곧 이동 폭이 된다.
TRAVEL = 0.10

#: 중간 산출물의 CRF. `[9]`의 18보다 낮다 (위 docstring "화질" 참고).
CLIP_CRF = 14
CLIP_PRESET = "medium"

#: specs/03 "역방향 워크" — 같은 그림이 두 번째로 쓰일 때 갈아 끼우는 값.
#: `static`만 역이 없어 움직임을 준다.
REVERSE_CAMERA: dict[str, str] = {
    "slow_zoom_in": "slow_zoom_out",
    "slow_zoom_out": "slow_zoom_in",
    "pan_left": "pan_right",
    "pan_right": "pan_left",
    "tilt_down": "tilt_up",
    "tilt_up": "tilt_down",
    "static": "slow_zoom_in",
}

STATIC = "static"

#: zoompan에 물리기 전에 크로마 서브샘플링을 없앤다 (위 docstring "format=gbrp").
#: 이 한 줄이 느린 팬의 정지 프레임을 없앤다. `static`에도 같이 걸어 두 경로의
#: 색 처리를 맞춘다 — 크로마 축소는 인코딩 한 번에서만 일어난다.
WORKING_FORMAT = "gbrp"


class KenBurnsError(Exception):
    """카메라 워크를 필터로 옮길 수 없음."""


def frame_count(length: float, *, fps: int = FPS) -> int:
    """클립 길이(초) → 프레임 수. **올림이다** (위 docstring "프레임 수는 올림이다")."""
    if length <= 0:
        raise KenBurnsError(f"클립 길이는 0보다 커야 한다: {length}")
    return max(1, math.ceil(round(length * fps, 6)))


def crop_to_aspect(*, width: int = WIDTH, height: int = HEIGHT) -> str:
    """원본을 목표 비율로 먼저 자른다. 중앙 기준이며 원본보다 커지지 않는다.

    `min()`의 쉼표가 필터 그래프의 구분자와 겹치므로 작은따옴표로 묶는다.
    """
    return (
        f"crop='min(iw,ih*{width}/{height})':'min(ih,iw*{height}/{width})'"
    )


def _progress(frames: int) -> str:
    """0 → 1로 가는 진행도 식. 마지막 프레임에서 정확히 1이다.

    `on`(출력 프레임 번호)에서 매번 새로 계산한다. 앞 프레임 값에 더해 나가면
    반올림 오차가 쌓여 끝에서 목표 배율을 못 맞춘다.
    """
    return f"on/{frames - 1}"


def zoompan_expressions(camera: str, frames: int) -> tuple[str, str, str]:
    """`camera` → (z, x, y) 식. specs/03 "카메라 워크 파라미터" 표.

    `x`/`y`는 zoompan이 잘라 낼 창의 좌상단이고, 창 크기는 `iw/zoom × ih/zoom`이다.
    따라서 창이 움직일 수 있는 폭은 `iw - iw/zoom`이며, 이것이 표의 "여백 전부"다.
    """
    zoom = 1 + TRAVEL
    progress = _progress(frames)
    centered_x = "iw/2-(iw/zoom/2)"
    centered_y = "ih/2-(ih/zoom/2)"

    if camera == "slow_zoom_in":
        return f"1+{TRAVEL}*{progress}", centered_x, centered_y
    if camera == "slow_zoom_out":
        return f"{zoom}-{TRAVEL}*{progress}", centered_x, centered_y
    if camera == "pan_right":
        return f"{zoom}", f"(iw-iw/zoom)*{progress}", centered_y
    if camera == "pan_left":
        return f"{zoom}", f"(iw-iw/zoom)*(1-{progress})", centered_y
    if camera == "tilt_down":
        return f"{zoom}", centered_x, f"(ih-ih/zoom)*{progress}"
    if camera == "tilt_up":
        return f"{zoom}", centered_x, f"(ih-ih/zoom)*(1-{progress})"

    raise KenBurnsError(
        f"specs/03 카메라 워크 파라미터 표에 없는 camera '{camera}'. 움직임을 임의로 정하지 않는다"
    )


def build_filter(
    camera: str,
    *,
    frames: int,
    width: int = WIDTH,
    height: int = HEIGHT,
    fps: int = FPS,
) -> str:
    """정지 이미지 → 클립 한 개의 `-vf` 문자열.

    `static`과 1프레임짜리 클립은 zoompan을 타지 않는다. 후자는 진행도의 분모가
    0이 되는 자리이기도 하다.
    """
    crop = crop_to_aspect(width=width, height=height)
    if camera == STATIC or frames <= 1:
        if camera not in REVERSE_CAMERA:
            raise KenBurnsError(
                f"specs/03 카메라 워크 파라미터 표에 없는 camera '{camera}'. "
                "움직임을 임의로 정하지 않는다"
            )
        return f"{crop},format={WORKING_FORMAT},scale={width}:{height},setsar=1"

    zoom, x, y = zoompan_expressions(camera, frames)
    return (
        f"{crop},format={WORKING_FORMAT},"
        f"zoompan=z='{zoom}':x='{x}':y='{y}'"
        f":d=1:s={width}x{height}:fps={fps},"
        f"setsar=1"
    )


def build_command(
    image: str,
    output: str,
    *,
    camera: str,
    frames: int,
    executable: str = "ffmpeg",
    width: int = WIDTH,
    height: int = HEIGHT,
    fps: int = FPS,
    crf: int = CLIP_CRF,
    preset: str = CLIP_PRESET,
) -> list[str]:
    """클립 하나를 만드는 FFmpeg 인자 배열.

    `-loop 1`로 같은 그림을 계속 넣고 `-frames:v`로 끊는다. `-t`(초)로 끊으면 길이가
    프레임 격자에 걸려 한 프레임 모자랄 수 있다.

    소리는 넣지 않는다(`-an`). 나레이션·SFX·BGM은 `[10. mix]`가 붙인다 (specs/05).
    """
    return [
        executable, "-y", "-hide_banner", "-loglevel", "error",
        "-loop", "1", "-framerate", str(fps), "-i", image,
        "-vf", build_filter(camera, frames=frames, width=width, height=height, fps=fps),
        "-frames:v", str(frames),
        "-an",
        "-c:v", VIDEO_CODEC,
        "-preset", preset,
        "-crf", str(crf),
        "-pix_fmt", PIXEL_FORMAT,
        "-r", str(fps),
        "-movflags", "+faststart",
        output,
    ]


def resolve_camera(camera: str, occurrence: int) -> str:
    """같은 그림이 `occurrence`번째로 쓰일 때 실제로 적용할 카메라 (specs/03 역방향 워크).

    1회차는 계약된 값 그대로다. 2회차부터 역방향을 쓴다 — 씬 계약의 `camera`는
    고치지 않는다 (ADR-0020: 값의 출처는 `06-script.json` 하나).
    """
    if camera not in REVERSE_CAMERA:
        raise KenBurnsError(
            f"specs/03 카메라 워크 파라미터 표에 없는 camera '{camera}'"
        )
    return camera if occurrence <= 1 else REVERSE_CAMERA[camera]
