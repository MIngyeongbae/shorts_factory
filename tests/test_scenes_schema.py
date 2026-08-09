"""씬 계약 검증. CLAUDE.md 최소 기준: 픽스처 JSON → 스키마 검증 통과.

`scenes_pass.json`은 **스펙 02 스키마 픽스처**다. 12개 비트를 모두 한 번씩 쓰도록
짠 15씬짜리라 스펙 01의 분량 규칙(23~28문장, 90~100초)은 만족하지 않고, 숫자도
팩트시트에 그라운딩되지 않은 더미다. 그 두 축은 각각 별도 검증기가 맡는다.
"""

import pytest
from jsonschema import Draft202012Validator

from conftest import load_fixture
from shorts_factory.schemas.scenes import (
    BEATS,
    MAX_KLING_SCENES,
    SCENE_SCHEMA,
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
        "subject": "성벽 축조 현장",
        "camera": "slow_zoom_in",
        "motion": "kenburns",
        "notes": "",
    }
    assert list(Draft202012Validator(SCENE_SCHEMA).iter_errors(example)) == []


def test_missing_required_field_is_rejected():
    data = load_fixture("scenes_pass.json")
    del data["scenes"][0]["subject"]
    errors, _ = validate_scenes(data)
    assert any("subject" in e for e in errors)


def test_unknown_field_is_rejected():
    data = load_fixture("scenes_pass.json")
    data["scenes"][0]["transition"] = "dissolve"
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
def test_number_beat_requires_emphasis(beat):
    """specs/02: emphasis는 숫자 비트에 필수, 그 외 옵션."""
    data = load_fixture("scenes_pass.json")
    scene = next(s for s in data["scenes"] if s["beat"] == beat)
    del scene["emphasis"]
    errors, _ = validate_scenes(data)
    assert any("emphasis" in e for e in errors)


def test_non_number_beat_may_omit_emphasis():
    data = load_fixture("scenes_pass.json")
    scene = next(s for s in data["scenes"] if s["beat"] == "hook_twist")
    del scene["emphasis"]
    errors, _ = validate_scenes(data)
    assert errors == []


def _with_kling_count(data: dict, count: int) -> dict:
    """픽스처에 이미 kling 씬이 있으므로 전부 되돌린 뒤 정확히 count개만 켠다."""
    for idx, scene in enumerate(data["scenes"]):
        scene["motion"] = "kling" if idx < count else "kenburns"
    return data


def test_kling_at_limit_is_allowed():
    data = _with_kling_count(load_fixture("scenes_pass.json"), MAX_KLING_SCENES)
    errors, _ = validate_scenes(data)
    assert errors == []


def test_kling_over_limit_is_rejected():
    """specs/02 + ADR-0006: kling은 편당 최대 10씬."""
    data = _with_kling_count(load_fixture("scenes_pass.json"), MAX_KLING_SCENES + 1)
    errors, _ = validate_scenes(data)
    assert any(str(MAX_KLING_SCENES) in e for e in errors)


def test_duration_mismatch_is_warning_not_error():
    data = load_fixture("scenes_pass.json")
    data["total_duration"] = data["scenes"][-1]["est_end"] + 12.0
    errors, warnings = validate_scenes(data)
    assert errors == []
    assert any("total_duration" in w for w in warnings)


def test_emphasis_type_is_not_yet_an_enum():
    """specs/02는 emphasis.type을 specs/03의 오버레이 enum이라고 하지만 그 enum이 없다.

    스펙 공백을 고정해 둔다. specs/03에 enum이 추가되면 이 테스트가 뒤집혀야 한다.
    """
    data = load_fixture("scenes_pass.json")
    data["scenes"][1]["emphasis"]["type"] = "아직_정의되지_않은_오버레이"
    errors, _ = validate_scenes(data)
    assert errors == []
