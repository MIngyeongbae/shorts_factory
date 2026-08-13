"""씬 계약 검증. CLAUDE.md 최소 기준: 픽스처 JSON → 스키마 검증 통과.

`scenes_pass.json`은 **스펙 02 스키마 픽스처**다. 12개 비트를 모두 한 번씩 쓰도록
짠 15씬짜리라 스펙 01의 분량 규칙(23~28문장, 90~100초)은 만족하지 않고, 숫자도
팩트시트에 그라운딩되지 않은 더미다. 그 두 축은 각각 별도 검증기가 맡는다.
"""

import pytest
from jsonschema import Draft202012Validator

from conftest import load_fixture
from shorts_factory.schemas import vocab
from shorts_factory.schemas.scenes import (
    visual_goal_overlap,
    BEATS,
    MAX_VIDEO_SCENES,
    SCENE_SCHEMA,
    VIDEO_MOTIONS,
    validate_scenes,
)


def test_pass_fixture_is_valid():
    errors, warnings = validate_scenes(load_fixture("scenes_pass.json"))
    assert errors == []
    assert warnings == []


def test_fixture_covers_every_beat():
    """비트 enum이 늘거나 줄면 픽스처도 같이 움직여야 한다."""
    data = load_fixture("scenes_pass.json")
    assert {s["beat"] for s in data["scenes"]} == set(BEATS)


def test_spec_inline_example_scene_is_valid():
    """specs/02 본문의 씬 예시가 스키마를 그대로 통과하는지 고정한다."""
    example = {
        "scene_id": 14,
        "beat": "turning_point",
        "text": "그래서 발상을 뒤집습니다.",
        "est_start": 44.0,
        "est_end": 45.8,
        "emphasis": {"type": "big_red_text", "value": "발상"},
        "visual_goal": "축조 현장의 규모 — 자막이 말하지 않는 것",
        "subject": "성벽 축조 현장",
        "subject_scale": "wide",
        "framing": "frontal_symmetric",
        "transition": "hard_cut",
        "camera": "slow_zoom_in",
        "motion": "kenburns",
        "notes": "",
    }
    validator = Draft202012Validator(SCENE_SCHEMA, registry=vocab.REGISTRY)
    assert list(validator.iter_errors(example)) == []


def test_missing_required_field_is_rejected():
    data = load_fixture("scenes_pass.json")
    del data["scenes"][0]["subject"]
    errors, _ = validate_scenes(data)
    assert any("subject" in e for e in errors)


def test_unknown_field_is_rejected():
    data = load_fixture("scenes_pass.json")
    data["scenes"][0]["shot_type"] = "dolly"
    errors, _ = validate_scenes(data)
    assert errors


def test_empty_scenes_is_rejected():
    data = load_fixture("scenes_pass.json")
    data["scenes"] = []
    errors, _ = validate_scenes(data)
    assert errors


def test_unknown_beat_is_rejected():
    data = load_fixture("scenes_pass.json")
    data["scenes"][0]["beat"] = "hook"
    errors, _ = validate_scenes(data)
    assert any("beat" in e for e in errors)


@pytest.mark.parametrize("bad_camera", ["zoom_in", "dolly_zoom", "pan_left+tilt_up", "STATIC"])
def test_compound_or_unknown_camera_is_rejected(bad_camera):
    """specs/02: 복합 카메라 워크 금지 (AI 영상 왜곡 방지)."""
    data = load_fixture("scenes_pass.json")
    data["scenes"][0]["camera"] = bad_camera
    errors, _ = validate_scenes(data)
    assert any("camera" in e for e in errors)


def test_unknown_motion_is_rejected():
    data = load_fixture("scenes_pass.json")
    data["scenes"][0]["motion"] = "runway"
    errors, _ = validate_scenes(data)
    assert any("motion" in e for e in errors)


def test_scene_ids_must_be_sequential_from_one():
    """specs/02: scene_id는 1부터 연번, 대본 문장 순서와 일치."""
    data = load_fixture("scenes_pass.json")
    data["scenes"][2]["scene_id"] = 99
    errors, _ = validate_scenes(data)
    assert any("연번" in e for e in errors)


def test_reordered_scenes_are_rejected():
    data = load_fixture("scenes_pass.json")
    data["scenes"][0], data["scenes"][1] = data["scenes"][1], data["scenes"][0]
    errors, _ = validate_scenes(data)
    assert any("연번" in e for e in errors)


def test_zero_length_scene_is_rejected():
    data = load_fixture("scenes_pass.json")
    data["scenes"][0]["est_end"] = data["scenes"][0]["est_start"]
    errors, _ = validate_scenes(data)
    assert any("est_end" in e for e in errors)


def test_overlapping_scenes_are_rejected():
    data = load_fixture("scenes_pass.json")
    data["scenes"][1]["est_start"] = data["scenes"][0]["est_start"]
    errors, _ = validate_scenes(data)
    assert any("이르다" in e for e in errors)


@pytest.mark.parametrize("beat", ["context_number", "solution_number"])
def test_number_beat_without_emphasis_is_a_warning(beat):
    """오버레이를 고르는 것은 이제 [1s]다 (ADR-0033 §3). 막지 않고 알린다."""
    data = load_fixture("scenes_pass.json")
    scene = next(s for s in data["scenes"] if s["beat"] == beat)
    del scene["emphasis"]
    errors, warnings = validate_scenes(data)
    assert errors == []
    assert any("emphasis" in w for w in warnings)


def test_non_number_beat_may_omit_emphasis():
    data = load_fixture("scenes_pass.json")
    scene = next(s for s in data["scenes"] if s["beat"] == "hook_twist")
    scene.pop("emphasis", None)
    errors, warnings = validate_scenes(data)
    assert errors == []
    assert warnings == []


def _with_video_count(data: dict, count: int, motion: str = "kling") -> dict:
    """픽스처에 이미 영상 씬이 있으므로 전부 되돌린 뒤 정확히 count개만 켠다."""
    for idx, scene in enumerate(data["scenes"]):
        scene["motion"] = motion if idx < count else "kenburns"
    return data


@pytest.mark.parametrize("motion", VIDEO_MOTIONS)
def test_video_motion_at_limit_is_allowed(motion):
    data = _with_video_count(load_fixture("scenes_pass.json"), MAX_VIDEO_SCENES, motion)
    errors, _ = validate_scenes(data)
    assert errors == []


@pytest.mark.parametrize("motion", VIDEO_MOTIONS)
def test_video_motion_over_limit_is_rejected(motion):
    """specs/02 + ADR-0006: 영상은 편당 최대 10씬. mj_video도 같은 상한이다 (ADR-0025 §3)."""
    data = _with_video_count(load_fixture("scenes_pass.json"), MAX_VIDEO_SCENES + 1, motion)
    errors, _ = validate_scenes(data)
    assert any(str(MAX_VIDEO_SCENES) in e for e in errors)


def test_duration_mismatch_is_warning_not_error():
    data = load_fixture("scenes_pass.json")
    data["total_duration"] = data["scenes"][-1]["est_end"] + 12.0
    errors, warnings = validate_scenes(data)
    assert errors == []
    assert any("total_duration" in w for w in warnings)


def test_emphasis_type_is_an_enum_from_spec_03():
    """specs/02의 emphasis.type = specs/03의 오버레이 타입 enum (ADR-0019로 그 표가 생겼다)."""
    data = load_fixture("scenes_pass.json")
    scene = next(s for s in data["scenes"] if "emphasis" in s)
    scene["emphasis"]["type"] = "red_crayon_x"  # ADR-0019로 폐기된 타입
    errors, _ = validate_scenes(data)
    assert any("emphasis" in e for e in errors)


# --- visual_goal (ADR-0022) ---------------------------------------------------


def test_visual_goal_is_required():
    """비어 있으면 그 씬의 그림은 할 일이 없다는 뜻이다."""
    doc = load_fixture("scenes_pass.json")
    del doc["scenes"][0]["visual_goal"]

    errors, _ = validate_scenes(doc)
    assert any("visual_goal" in e for e in errors)


def test_visual_goal_that_restates_the_text_is_rejected():
    """그림이 본문을 되풀이하면 설명을 지지 않는다 — 시간만 채운다."""
    doc = load_fixture("scenes_pass.json")
    doc["scenes"][0]["visual_goal"] = doc["scenes"][0]["text"]

    errors, _ = validate_scenes(doc)
    assert any("겹친다" in e for e in errors)


def test_visual_goal_that_adds_something_passes():
    doc = load_fixture("scenes_pass.json")
    doc["scenes"][0]["visual_goal"] = "무너진 뒤 남은 잔해의 부피감"

    errors, _ = validate_scenes(doc)
    assert not any("겹친다" in e for e in errors)


def test_overlap_is_measured_on_content_not_punctuation():
    """공백·문장부호만 다른 문장은 되풀이다."""
    text = "4명이 숨졌고 잔해는 8,000 m³였죠."
    assert visual_goal_overlap(text, "4명이 숨졌고 잔해는 8,000 m³였다") >= 0.7
    assert visual_goal_overlap(text, "8,000 m³가 광장을 덮는 부피감") < 0.7


def test_empty_goal_counts_as_fully_overlapping():
    """빈 값에 관대하면 필드가 조용히 죽는다."""
    assert visual_goal_overlap("아무 문장.", "") == 1.0


# --- 연출 필드는 선택이다 (ADR-0033 §3) ---------------------------------------


def test_framing_and_transition_are_optional():
    """옛 대본은 두 필드가 없다. 부재는 경고가 아니다 (단계 독립 D-3)."""
    data = load_fixture("scenes_pass.json")
    assert all("framing" not in s for s in data["scenes"])
    errors, warnings = validate_scenes(data)
    assert errors == []
    assert warnings == []


def test_chosen_framing_must_be_in_the_vocabulary():
    """자유 기술이 아니라 닫힌 어휘에서의 선택이다."""
    data = load_fixture("scenes_pass.json")
    data["scenes"][0]["framing"] = "cinematic_dolly_zoom"
    errors, _ = validate_scenes(data)
    assert any("framing" in e for e in errors)


def test_chosen_transition_must_be_in_the_vocabulary():
    data = load_fixture("scenes_pass.json")
    data["scenes"][0]["transition"] = "wipe"
    errors, _ = validate_scenes(data)
    assert any("transition" in e for e in errors)


@pytest.mark.parametrize("token", ["drone_wide", "cross_section", "present_closeup"])
def test_vocabulary_framing_tokens_are_accepted(token):
    data = load_fixture("scenes_pass.json")
    data["scenes"][0]["framing"] = token
    errors, _ = validate_scenes(data)
    assert errors == []
