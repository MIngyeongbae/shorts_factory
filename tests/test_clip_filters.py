"""`[7]`의 클립 정규화·프레임 추출 필터와 프롬프트 보조 함수 (ADR-0056).

- 정규화: 1080×1920 · 30fps · 무음 · **정확히 N초** (짧으면 마지막 프레임 복제, 길면 자른다)
- 프레임: 시작·중간·끝 3장. 끝은 마지막으로 렌더된 프레임이다
- `fill_seconds`·`demote_info`: `[5]`의 프롬프트에 길이를 채우고 RED 절을 뺀다 — 문구는 어휘의 것
"""

import pytest

from shorts_factory.schemas import vocab
from shorts_factory.schemas.visual_rules import (
    SECONDS_PLACEHOLDER,
    build_video_prompt,
    demote_info,
    fill_seconds,
)
from shorts_factory.video.clips import (
    ClipError,
    frame_command,
    frame_times,
    normalize_command,
    normalize_filter,
)


def test_normalize_filter_pads_then_trims_to_the_exact_length():
    chain = normalize_filter(6)
    assert "scale=1080:1920" in chain and "fps=30" in chain
    assert chain.index("tpad=stop_mode=clone:stop_duration=6.000") < chain.index("trim=end=6.000")
    assert "setpts=PTS-STARTPTS" in chain


def test_normalize_command_drops_audio_and_matches_the_clip_contract():
    cmd = normalize_command("raw.mp4", "clips/3.mp4", seconds=6)
    assert "-an" in cmd
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-r") + 1] == "30"
    assert cmd[-1] == "clips/3.mp4" and cmd[cmd.index("-i") + 1] == "raw.mp4"


@pytest.mark.parametrize("seconds", [0, -1])
def test_non_positive_length_is_refused(seconds):
    with pytest.raises(ClipError):
        normalize_filter(seconds)


def test_frame_times_are_start_mid_and_last_rendered_frame():
    times = frame_times(6)
    assert times["start"] == 0.0 and times["mid"] == 3.0
    # 마지막 프레임(5.9667)보다 앞, 그 전 프레임(5.9333)보다 뒤 — `-ss`가 마지막 프레임을 고른다
    assert 6 - 2 / 30 < times["end"] < 6 - 1 / 30


def test_frame_command_seeks_before_input():
    cmd = frame_command("clips/3.mp4", "clip_review/3-1-end.png", at=5.967)
    assert cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-frames:v") + 1] == "1"
    assert cmd[-1] == "clip_review/3-1-end.png"


# --- 프롬프트 보조 ------------------------------------------------------------------


def info_prompt():
    return build_video_prompt(
        subject="종의 구멍", shot="close-up", staging="studio", camera="static",
        info={"labels": ["4 mm"], "target": "the diameter of the hole", "annotation": "dimension"},
    )


def test_fill_seconds_replaces_the_placeholder_only():
    prompt, _negative = info_prompt()
    assert SECONDS_PLACEHOLDER in prompt
    filled = fill_seconds(prompt, 6)
    assert ", 6 seconds long," in filled and SECONDS_PLACEHOLDER not in filled


def test_fill_seconds_refuses_a_prompt_without_the_placeholder():
    with pytest.raises(ValueError, match="자리"):
        fill_seconds("no placeholder here", 6)


def test_demote_info_removes_red_and_adds_the_text_ban():
    prompt, negative = info_prompt()
    assert "RED:" in prompt and "numbers" not in negative

    demoted, demoted_negative = demote_info(prompt, negative)

    assert "RED:" not in demoted
    assert '"4 mm"' not in demoted
    for item in vocab.negatives("no_text"):
        assert item in demoted_negative
    assert demoted.count("NEGATIVE:") == 1
    assert vocab.negatives("audio") in demoted


def test_demote_info_keeps_the_other_sections():
    prompt, negative = info_prompt()
    demoted, _ = demote_info(prompt, negative)
    for section in ("FORMAT:", "STAGING:", "SUBJECT:", "CAMERA:"):
        assert section in demoted


def test_demote_info_refuses_a_plain_scene():
    prompt, negative = build_video_prompt(
        subject="댐", shot="wide", staging="location", camera="static",
    )
    with pytest.raises(ValueError, match="RED"):
        demote_info(prompt, negative)
