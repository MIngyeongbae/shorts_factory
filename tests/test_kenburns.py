"""Ken Burns 필터 조립 (specs/03 "카메라 워크 파라미터", ADR-0006/0024).

이 모듈의 값은 전부 스펙 03의 표에서 온다. 여기 테스트가 지키는 것은 두 가지다.

- **표의 숫자가 실제 필터 문자열에 그대로 나타나는가** — 줌 끝점이 정확히 1.10인지는
  식을 실제로 계산해서 본다. 문자열 포함 검사로는 `1+0.1*on/115`가 마지막 프레임에서
  무엇이 되는지 알 수 없다
- **`format=gbrp`가 zoompan 앞에 반드시 붙는가** — 이게 빠지면 크롭 원점이 2픽셀로
  스냅되어 느린 팬의 40%가 정지 프레임이 된다 (kenburns.py docstring의 실측표).
  눈으로만 보이는 회귀라 테스트로 못박는다
"""

import pytest

from shorts_factory.schemas.scenes import CAMERAS
from shorts_factory.video.kenburns import (
    REVERSE_CAMERA,
    STATIC,
    TRAVEL,
    WORKING_FORMAT,
    KenBurnsError,
    build_command,
    build_filter,
    frame_count,
    resolve_camera,
    zoompan_expressions,
)

MOVING = tuple(c for c in CAMERAS if c != STATIC)


def evaluate(expression: str, *, on: int, iw: float, ih: float, zoom: float) -> float:
    """zoompan 식을 파이썬으로 계산한다. FFmpeg가 쓰는 변수만 넣는다."""
    return float(
        eval(expression, {"__builtins__": {}}, {"on": on, "iw": iw, "ih": ih, "zoom": zoom})
    )


# --- 프레임 수 ---------------------------------------------------------------


def test_frame_count_rounds_up_so_the_clip_is_never_short():
    # 계약 3.842초는 30fps 격자에 안 떨어진다. 115프레임(3.833초)이면 [9]의 xfade가
    # 꼬리 끝에서 그림을 못 받는다.
    assert frame_count(3.842) == 116


def test_frame_count_keeps_exact_grid_lengths_exact():
    assert frame_count(4.0) == 120


@pytest.mark.parametrize("length", [0, -1.0])
def test_frame_count_rejects_empty_clips(length):
    with pytest.raises(KenBurnsError):
        frame_count(length)


# --- 스펙 03 표의 숫자 -------------------------------------------------------


@pytest.mark.parametrize("camera", ["slow_zoom_in", "slow_zoom_out"])
def test_zoom_runs_between_1_00_and_the_table_value(camera):
    frames = 116
    zoom_expr, _, _ = zoompan_expressions(camera, frames)
    first = evaluate(zoom_expr, on=0, iw=1536, ih=2730, zoom=1.0)
    last = evaluate(zoom_expr, on=frames - 1, iw=1536, ih=2730, zoom=1.0)

    assert sorted((first, last)) == pytest.approx([1.0, 1 + TRAVEL])
    # 방향까지 확인한다 — in은 커지고 out은 작아진다
    assert (last > first) == (camera == "slow_zoom_in")


@pytest.mark.parametrize("camera", ["pan_left", "pan_right", "tilt_down", "tilt_up"])
def test_pan_and_tilt_hold_the_zoom_fixed(camera):
    zoom_expr, _, _ = zoompan_expressions(camera, 116)
    assert evaluate(zoom_expr, on=0, iw=1536, ih=2730, zoom=1.0) == pytest.approx(1 + TRAVEL)
    assert evaluate(zoom_expr, on=115, iw=1536, ih=2730, zoom=1.0) == pytest.approx(1 + TRAVEL)


@pytest.mark.parametrize(
    "camera,axis,forward",
    [
        ("pan_right", "x", True),
        ("pan_left", "x", False),
        ("tilt_down", "y", True),
        ("tilt_up", "y", False),
    ],
)
def test_pan_and_tilt_travel_the_whole_margin(camera, axis, forward):
    """이동 폭은 크롭이 움직일 수 있는 여백 전부다 (스펙 03 "여백 전부")."""
    frames, iw, ih = 116, 1536.0, 2730.0
    zoom = 1 + TRAVEL
    _, x_expr, y_expr = zoompan_expressions(camera, frames)
    expression = x_expr if axis == "x" else y_expr
    size = iw if axis == "x" else ih
    margin = size - size / zoom

    start = evaluate(expression, on=0, iw=iw, ih=ih, zoom=zoom)
    end = evaluate(expression, on=frames - 1, iw=iw, ih=ih, zoom=zoom)
    assert sorted((start, end)) == pytest.approx([0.0, margin])
    assert (end > start) == forward


@pytest.mark.parametrize("camera", ["slow_zoom_in", "slow_zoom_out"])
def test_zoom_stays_centred(camera):
    frames, iw, ih = 116, 1536.0, 2730.0
    zoom = 1 + TRAVEL
    _, x_expr, y_expr = zoompan_expressions(camera, frames)
    assert evaluate(x_expr, on=0, iw=iw, ih=ih, zoom=zoom) == pytest.approx(
        (iw - iw / zoom) / 2
    )
    assert evaluate(y_expr, on=0, iw=iw, ih=ih, zoom=zoom) == pytest.approx(
        (ih - ih / zoom) / 2
    )


def test_unknown_camera_stops_instead_of_guessing():
    # CLAUDE.md 원칙 3 — 룰에 없는 케이스는 지어내지 않는다
    with pytest.raises(KenBurnsError, match="specs/03"):
        zoompan_expressions("dolly_zoom", 116)


# --- 필터 문자열 -------------------------------------------------------------


@pytest.mark.parametrize("camera", CAMERAS)
def test_every_camera_crops_to_aspect_before_anything_else(camera):
    # 원본이 9:16이 아니면 zoompan의 s=1080x1920이 그림을 눌러 늘린다
    assert build_filter(camera, frames=116).startswith("crop=")


@pytest.mark.parametrize("camera", MOVING)
def test_moving_cameras_drop_chroma_subsampling_before_zoompan(camera):
    """`format=gbrp`가 빠지면 느린 팬이 정지·점프를 반복한다 (실측 변동계수 81%)."""
    chain = build_filter(camera, frames=116)
    assert f"format={WORKING_FORMAT}" in chain
    assert chain.index(f"format={WORKING_FORMAT}") < chain.index("zoompan")


@pytest.mark.parametrize("camera", MOVING)
def test_moving_cameras_render_at_the_target_size(camera):
    assert "s=1080x1920" in build_filter(camera, frames=116)
    assert "fps=30" in build_filter(camera, frames=116)


def test_static_does_not_go_through_zoompan():
    chain = build_filter(STATIC, frames=116)
    assert "zoompan" not in chain
    assert "scale=1080:1920" in chain


def test_single_frame_clips_fall_back_to_a_still():
    # 진행도의 분모가 0이 되는 자리다
    assert "zoompan" not in build_filter("pan_right", frames=1)


def test_build_filter_rejects_unknown_cameras_on_the_static_path():
    with pytest.raises(KenBurnsError, match="specs/03"):
        build_filter("dolly_zoom", frames=1)


# --- 명령 -------------------------------------------------------------------


def test_command_loops_one_image_and_cuts_by_frame_count():
    cmd = build_command("images/3.jpg", "clips/3.mp4", camera="pan_right", frames=116)

    assert cmd[:2] == ["ffmpeg", "-y"]
    assert "-loop" in cmd and cmd[cmd.index("-loop") + 1] == "1"
    # 초(-t)로 끊으면 격자에 걸려 한 프레임 모자랄 수 있다
    assert "-t" not in cmd
    assert cmd[cmd.index("-frames:v") + 1] == "116"
    assert cmd[-1] == "clips/3.mp4"


def test_command_carries_no_audio():
    # 나레이션·SFX·BGM은 [10. mix] 소관이다 (specs/05)
    assert "-an" in build_command("a.jpg", "b.mp4", camera=STATIC, frames=30)


# --- 역방향 워크 (스펙 03) ---------------------------------------------------


def test_reverse_table_covers_every_camera_in_the_scene_schema():
    # 스펙 02의 enum이 늘면 스펙 03의 역방향 표도 함께 늘어야 한다
    assert set(REVERSE_CAMERA) == set(CAMERAS)


@pytest.mark.parametrize("camera", MOVING)
def test_reversing_twice_returns_the_original(camera):
    assert REVERSE_CAMERA[REVERSE_CAMERA[camera]] == camera


def test_static_has_no_opposite_so_it_gains_movement():
    # 정지 화면이 두 번 지나가는 것이 가장 눈에 띈다 (스펙 03)
    assert REVERSE_CAMERA[STATIC] == "slow_zoom_in"


@pytest.mark.parametrize("camera", CAMERAS)
def test_first_use_keeps_the_contracted_camera(camera):
    assert resolve_camera(camera, 1) == camera


@pytest.mark.parametrize("camera", CAMERAS)
def test_later_uses_take_the_reverse(camera):
    assert resolve_camera(camera, 2) == REVERSE_CAMERA[camera]
    assert resolve_camera(camera, 3) == REVERSE_CAMERA[camera]
