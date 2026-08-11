"""스펙 03 시각 룰 테이블 (ADR-0001: 연출은 룰에서만 나온다).

여기서 지키는 것은 하나다 — **룰 테이블이 스펙 02·03과 ADR-0002/0018/0019에서
기계적으로 유도 가능한 형태를 유지하는가.** 프롬프트 문구의 미학은 시험 대상이 아니다.
"""

import pytest

from shorts_factory.schemas.scenes import BEATS, CAMERAS, NUMBER_BEATS
from shorts_factory.schemas.visual_rules import (
    BEAT_RULES,
    ECHO_HOOK,
    FRAMING_TABLE,
    FRAMINGS,
    GLOBAL_NEGATIVES,
    INHERIT_PREV,
    OVERLAYS,
    REFERENCE_FALLBACK,
    SUBJECT_SCALES,
    build_negative,
    build_prompt,
    resolve_framing,
)

CROSS_SCENE = (INHERIT_PREV, ECHO_HOOK)

CELLS = [(beat, scale) for beat in BEATS for scale in SUBJECT_SCALES]


# --- 스펙 02 ↔ 스펙 03 정합 --------------------------------------------------


def test_every_beat_has_a_visual_rule():
    """스펙 02의 비트 12개와 스펙 03 룰 테이블 12행이 1:1이어야 한다."""
    assert set(BEAT_RULES) == set(BEATS)
    assert set(FRAMING_TABLE) == set(BEATS)


@pytest.mark.parametrize("beat", BEATS)
def test_framing_table_covers_every_scale(beat):
    """구도 표는 (beat × subject_scale) 전 칸이 채워져 있어야 한다 (ADR-0018).

    한 칸이라도 비면 그 조합의 대본이 왔을 때 [5]가 값을 정하지 못한다.
    """
    assert set(FRAMING_TABLE[beat]) == set(SUBJECT_SCALES)


@pytest.mark.parametrize("beat,scale", CELLS)
def test_every_cell_resolves_to_a_real_framing(beat, scale):
    token = FRAMING_TABLE[beat][scale]
    assert token in FRAMINGS or token in CROSS_SCENE


@pytest.mark.parametrize("token", sorted(CROSS_SCENE))
def test_cross_scene_reference_has_a_real_fallback(token):
    """참조가 성립하지 않을 때 쓸 값이 실재해야 한다 (specs/03)."""
    assert REFERENCE_FALLBACK[token] in FRAMINGS


@pytest.mark.parametrize("beat", BEATS)
def test_rule_overlays_are_registered(beat):
    for name in BEAT_RULES[beat].overlays:
        assert name in OVERLAYS


@pytest.mark.parametrize("beat", BEATS)
def test_rule_cameras_are_spec02_values(beat):
    """스펙 03 카메라 기본값은 스펙 02가 허용한 워크여야 한다 (복합 카메라 금지)."""
    assert set(BEAT_RULES[beat].cameras) <= set(CAMERAS)
    assert BEAT_RULES[beat].cameras


@pytest.mark.parametrize("beat", NUMBER_BEATS)
def test_number_beats_carry_big_red_text(beat):
    """스펙 03: 숫자 비트의 오버레이는 대형 빨간 숫자 텍스트다."""
    assert "big_red_text" in BEAT_RULES[beat].overlays


# --- 구도 참조 풀기 (ADR-0018) -----------------------------------------------


def test_plain_cell_comes_straight_from_the_beat_rule():
    token, source, reference = resolve_framing("hook_fact", "diagram")
    assert (token, source, reference) == ("section_diagram", "beat_rule", None)


def test_inherits_previous_framing_when_the_scale_matches():
    token, source, reference = resolve_framing(
        "hook_twist", "wide", prev=("aerial_diorama", "wide", 4)
    )
    assert (token, source, reference) == ("aerial_diorama", "prev_scene", 4)


def test_does_not_inherit_across_a_different_scale():
    """후버댐 2번 씬이 이 경우다 — 1번이 diagram, 2번이 wide.

    스케일이 다른 씬의 구도를 그대로 이으면 이 축을 도입한 이유가 무너진다.
    """
    token, source, reference = resolve_framing(
        "hook_twist", "wide", prev=("section_diagram", "diagram", 1)
    )
    assert (token, source, reference) == ("drone_wide", "scale_fallback", None)


def test_falls_back_when_there_is_no_scene_to_point_at():
    token, source, _ = resolve_framing("hook_twist", "wide", prev=None)
    assert (token, source) == ("drone_wide", "scale_fallback")

    token, source, _ = resolve_framing("ending_echo", "wide", hook=None)
    assert (token, source) == ("present_wide", "scale_fallback")


def test_echo_reuses_the_hook_framing():
    token, source, reference = resolve_framing(
        "ending_echo", "wide", hook=("drone_wide", "wide", 1)
    )
    assert (token, source, reference) == ("drone_wide", "hook_echo", 1)


@pytest.mark.parametrize("scale", ["close", "diagram"])
def test_non_wide_cells_never_reference_another_scene(scale):
    """참조는 wide 열에만 있다. 사람이 승인한 표가 그렇다."""
    for beat in BEATS:
        assert FRAMING_TABLE[beat][scale] not in CROSS_SCENE


# --- 오버레이 (ADR-0002 2계층, ADR-0019 레이어 A 폐기) -----------------------


def test_layer_a_is_not_used_at_all():
    """ADR-0019 — 빨간 어노테이션을 전부 버렸다. 남는 것은 후처리 합성뿐이다."""
    assert {overlay.layer for overlay in OVERLAYS.values()} == {"B"}


def test_no_beat_asks_for_a_red_annotation():
    for beat in BEATS:
        for name in BEAT_RULES[beat].overlays:
            assert OVERLAYS[name].layer == "B"


@pytest.mark.parametrize("name", sorted(OVERLAYS))
def test_text_overlays_live_in_layer_b(name):
    """읽어야 하는 값이 들어가는 오버레이는 전부 후처리 합성이다."""
    overlay = OVERLAYS[name]
    if overlay.needs_value:
        assert overlay.layer == "B"


@pytest.mark.parametrize("name", sorted(OVERLAYS))
def test_every_overlay_can_be_excluded_from_the_base_image(name):
    """베이스는 클린 이미지다 (ADR-0005·0019). 배제 문구가 없으면 그걸 보장 못 한다."""
    assert OVERLAYS[name].negative


# --- 프롬프트 조립 -----------------------------------------------------------


def test_prompt_forbids_rendering_the_korean_subject_as_text():
    """피사체는 한국어 그대로 넣되 글자로 그리지 말라고 못박는다 (ADR-0002)."""
    prompt = build_prompt("wide shot", "기울어진 탑에 겹쳐진 각도 눈금")
    assert "기울어진 탑에 겹쳐진 각도 눈금" in prompt
    assert "do not write these words in the image" in prompt


def test_negative_always_excludes_subtitles_and_particles():
    negative = build_negative(())
    for phrase in GLOBAL_NEGATIVES:
        assert phrase in negative


def test_negative_excludes_this_scene_overlays_too():
    negative = build_negative(("big_red_text",))
    assert OVERLAYS["big_red_text"].negative in negative
