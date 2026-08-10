"""스펙 03 시각 룰 테이블 (ADR-0001: 연출은 룰에서만 나온다).

여기서 지키는 것은 하나다 — **룰 테이블이 스펙 02·03과 ADR-0002/0005/0006에서
기계적으로 유도 가능한 형태를 유지하는가.** 프롬프트 문구의 미학은 시험 대상이 아니다.
"""

import pytest

from shorts_factory.schemas.scenes import BEATS, CAMERAS, NUMBER_BEATS
from shorts_factory.schemas.visual_rules import (
    BEAT_RULES,
    ECHO_HOOK,
    FRAMINGS,
    GLOBAL_NEGATIVES,
    HOOK_TWIST_FALLBACK,
    INHERIT_PREV,
    OVERLAYS,
    build_annotation,
    build_negative,
    build_prompt,
    framing_conflicts,
)

CROSS_SCENE = (INHERIT_PREV, ECHO_HOOK)


# --- 스펙 02 ↔ 스펙 03 정합 --------------------------------------------------


def test_every_beat_has_a_visual_rule():
    """스펙 02의 비트 12개와 스펙 03 룰 테이블 12행이 1:1이어야 한다."""
    assert set(BEAT_RULES) == set(BEATS)


@pytest.mark.parametrize("beat", BEATS)
def test_rule_framing_resolves(beat):
    framing = BEAT_RULES[beat].framing
    assert framing in FRAMINGS or framing in CROSS_SCENE


@pytest.mark.parametrize("beat", BEATS)
def test_rule_overlays_are_registered(beat):
    for name in BEAT_RULES[beat].overlays:
        assert name in OVERLAYS


@pytest.mark.parametrize("beat", BEATS)
def test_rule_cameras_are_spec02_values(beat):
    """스펙 03 카메라 기본값은 스펙 02가 허용한 워크여야 한다 (복합 카메라 금지)."""
    assert set(BEAT_RULES[beat].cameras) <= set(CAMERAS)
    assert BEAT_RULES[beat].cameras


def test_cross_scene_fallback_is_a_real_framing():
    assert HOOK_TWIST_FALLBACK in FRAMINGS


@pytest.mark.parametrize("beat", NUMBER_BEATS)
def test_number_beats_carry_big_red_text(beat):
    """스펙 03: 숫자 비트의 오버레이는 대형 빨간 숫자 텍스트다."""
    assert "big_red_text" in BEAT_RULES[beat].overlays


# --- ADR-0002 텍스트 2계층 ---------------------------------------------------


@pytest.mark.parametrize("name", sorted(OVERLAYS))
def test_text_overlays_live_in_layer_b(name):
    """읽어야 하는 값이 들어가는 오버레이는 전부 후처리 합성이다."""
    overlay = OVERLAYS[name]
    if overlay.needs_value:
        assert overlay.layer == "B"


@pytest.mark.parametrize("name", sorted(OVERLAYS))
def test_only_layer_a_has_annotation_instructions(name):
    """레이어 A만 이미지 편집 지시를 갖는다. 레이어 B는 [8]이 얹는다."""
    overlay = OVERLAYS[name]
    assert (overlay.annotation is not None) == (overlay.layer == "A")


@pytest.mark.parametrize("name", sorted(OVERLAYS))
def test_every_overlay_can_be_excluded_from_the_base_image(name):
    """베이스는 클린 이미지다 (ADR-0005·0006). 배제 문구가 없으면 그걸 보장 못 한다."""
    assert OVERLAYS[name].negative


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
    negative = build_negative(("big_red_text", "red_crayon_x"))
    assert OVERLAYS["big_red_text"].negative in negative
    assert OVERLAYS["red_crayon_x"].negative in negative


# --- 어노테이션 2-pass (ADR-0005) --------------------------------------------


def test_annotation_targets_the_scene_subject():
    text = build_annotation(("red_crayon_x",), "파비아의 시민탑")
    assert "파비아의 시민탑" in text
    assert "does not need to be legible" in text  # 레이어 A는 글자 정확도를 안 본다


def test_no_annotation_pass_without_layer_a_overlays():
    assert build_annotation((), "무언가") is None
    assert build_annotation(("big_red_text",), "무언가") is None


# --- 편향 검출 (조언용) ------------------------------------------------------


def test_wide_framing_with_close_subject_is_flagged():
    scenes = [
        {"scene_id": 1, "subject": "콘크리트 단면 속에 박힌 강철 파이프"},
        {"scene_id": 2, "subject": "협곡을 가득 메운 거대한 콘크리트 댐"},
    ]
    assert framing_conflicts(scenes, ["drone_wide", "drone_wide"]) == [1]


def test_close_framing_is_never_flagged():
    scenes = [{"scene_id": 1, "subject": "콘크리트 단면 속에 박힌 강철 파이프"}]
    assert framing_conflicts(scenes, ["detail_closeup"]) == []
