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
    LABEL_NUMBERS_FROM_LINE,
    labels_not_in_line,
    line_numbers,
    visual_goal_overlap,
    ANNOTATIONS,
    BEATS,
    SCENE_SCHEMA,
    STAGINGS,
    validate_scenes,
)


def info(*labels: str, target: str = "the measured part", annotation: str = "dimension") -> dict:
    """계측 표시 블록 (ADR-0056 결정 3) — 라벨은 ASCII, 대상은 영어 서술, 방식은 어휘."""
    return {"labels": list(labels), "target": target, "annotation": annotation}


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
        "visual_goal": "축조 현장의 규모 — 자막이 말하지 않는 것",
        "subject": "성벽 축조 현장",
        "subject_scale": "wide",
        "framing": "frontal_symmetric",
        "transition": "hard_cut",
        "camera": "slow_zoom_in",
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


def test_motion_is_no_longer_in_the_contract():
    """ADR-0056 — 전 씬이 영상 클립이다. motion이 오면 계약 위반이다."""
    data = load_fixture("scenes_pass.json")
    data["scenes"][0]["motion"] = "kenburns"
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


def test_emphasis_없는_숫자_씬은_경고가_아니다():
    """"숫자 비트인데 emphasis가 없다" 경고는 *_number 비트와 함께 죽었다 (ADR-0047).

    숫자를 화면에 세우라고 미는 장치였고 방향이 반대다 — 어느 씬이든 emphasis
    없이 조용히 통과한다.
    """
    data = load_fixture("scenes_pass.json")
    for scene in data["scenes"]:
        scene.pop("emphasis", None)
    errors, warnings = validate_scenes(data)
    assert errors == []
    assert not any("emphasis" in w for w in warnings)


def test_라벨_숫자는_그_줄이_말하는_숫자면_통과한다():
    """ADR-0060 결정 4 — "11만"을 말하는 줄 위의 `110,000`은 맞는 라벨이다."""
    data = load_fixture("scenes_pass.json")
    scene = next(s for s in data["scenes"] if "11만" in s["text"])
    scene["info"] = info("110,000 workers")
    errors, _ = validate_scenes(data)
    assert errors == []


def test_줄이_말하지_않는_숫자_라벨은_반려된다():
    """반려 여부는 계약의 스위치가 정한다 (`script-rules.json` `checks.label_numbers_from_line`)."""
    data = load_fixture("scenes_pass.json")
    scene = next(s for s in data["scenes"] if s["beat"] == "hook_twist")
    assert not any(ch.isdigit() for ch in scene["text"])
    scene["info"] = info("110000 workers", "12 years")
    errors, _ = validate_scenes(data)
    caught = [e for e in errors if "말하지 않는다" in e and "ADR-0060" in e]
    assert bool(caught) is LABEL_NUMBERS_FROM_LINE


def test_숫자_없는_라벨은_에코를_재지_않는다():
    data = load_fixture("scenes_pass.json")
    scene = next(s for s in data["scenes"] if "11만" in s["text"])
    scene["info"] = info("Fortress wall", annotation="leader")
    errors, _ = validate_scenes(data)
    assert errors == []


def test_라벨_숫자는_그_줄이_말하는_숫자다():
    """ADR-0060 결정 4 — 표기 차이는 허용하고, 줄이 말하지 않는 숫자는 잡는다."""
    assert labels_not_in_line("전체 길이가 1568km였죠.", ["1,568 km"]) == []
    assert labels_not_in_line("한강이 12센티 넘게 얼면", ["12 cm"]) == []
    assert labels_not_in_line("서빙고 한 곳에만 13만 덩이가", ["130,000"]) == []
    assert labels_not_in_line("바닥 배수로는 5도 기울여", ["5 deg"]) == []
    assert labels_not_in_line("여섯 달 뒤에도 99%가 남았습니다", ["6 months", "99%"]) == []
    assert labels_not_in_line("녹는 양이 0.4%뿐이에요", ["No straw 38.4%", "50% fill"]) == ["No straw 38.4%", "50% fill"]
    assert labels_not_in_line("더운 공기는 위로 뜨니까", ["Hot air"]) == []
    assert line_numbers("13만") == {13.0, 130000.0}


def test_scene_with_unspoken_label_number_is_rejected():
    data = load_fixture("scenes_pass.json")
    scene = data["scenes"][0]
    scene["text"] = "돌은 아무 말도 하지 않습니다."
    scene["info"] = info("221 m")
    errors, _ = validate_scenes(data)
    assert any("말하지 않는다" in e for e in errors) is LABEL_NUMBERS_FROM_LINE


# --- 계측 표시 info (ADR-0056 결정 3) -----------------------------------------


def test_info_needs_labels_target_and_annotation():
    """존재가 곧 계측 씬이다 — 셋 중 하나라도 없으면 [5]가 RED 절을 채울 수 없다."""
    for missing in ("labels", "target", "annotation"):
        data = load_fixture("scenes_pass.json")
        block = info("221 m")
        del block[missing]
        data["scenes"][0]["info"] = block
        errors, _ = validate_scenes(data)
        assert any(missing in e for e in errors), missing


@pytest.mark.parametrize("label", ["높이 221m", "２２１ m", "4 ㎜", "22°C", "사망 22,000+"])
def test_non_ascii_labels_are_rejected(label):
    """화면 텍스트는 영어(ASCII)만이다 — 한글·가나·전각·°는 자막이 진다 (ADR-0002·0056)."""
    data = load_fixture("scenes_pass.json")
    data["scenes"][0]["info"] = info(label)
    errors, _ = validate_scenes(data)
    assert any("labels" in e for e in errors), label


@pytest.mark.parametrize("label", ["221 m", "4 mm", "22 C", "660,000 t", "3x", "Short sag", "22,000+"])
def test_ascii_labels_are_accepted(label):
    data = load_fixture("scenes_pass.json")
    scene = next(s for s in data["scenes"] if not any(ch.isdigit() for ch in s["text"]))
    # 라벨 숫자는 그 줄이 말해야 한다 (ADR-0060) — 라벨의 숫자를 줄에 넣어 둔다.
    scene["text"] = scene["text"] + " 숫자는 221, 4, 22, 66만, 3, 2만 2천입니다."
    scene["info"] = info(label)
    errors, _ = validate_scenes(data)
    assert errors == []


def test_target_must_be_ascii_too():
    data = load_fixture("scenes_pass.json")
    data["scenes"][0]["info"] = info("221 m", target="댐의 높이")
    errors, _ = validate_scenes(data)
    assert any("target" in e for e in errors)


@pytest.mark.parametrize("annotation", ANNOTATIONS)
def test_every_annotation_in_the_vocabulary_is_accepted(annotation):
    data = load_fixture("scenes_pass.json")
    scene = next(s for s in data["scenes"] if not any(ch.isdigit() for ch in s["text"]))
    scene["info"] = info("Fortress wall", annotation=annotation)
    errors, _ = validate_scenes(data)
    assert errors == []


def test_annotation_outside_the_vocabulary_is_rejected():
    data = load_fixture("scenes_pass.json")
    data["scenes"][0]["info"] = info("221 m", annotation="circle")
    errors, _ = validate_scenes(data)
    assert any("annotation" in e for e in errors)


def test_more_than_four_labels_are_rejected():
    data = load_fixture("scenes_pass.json")
    data["scenes"][0]["info"] = info("1 m", "2 m", "3 m", "4 m", "5 m")
    errors, _ = validate_scenes(data)
    assert any("labels" in e for e in errors)


# --- 무대 staging (ADR-0056 결정 4) -------------------------------------------


@pytest.mark.parametrize("staging", STAGINGS)
def test_staging_from_the_vocabulary_is_accepted(staging):
    data = load_fixture("scenes_pass.json")
    data["scenes"][0]["staging"] = staging
    errors, _ = validate_scenes(data)
    assert errors == []


def test_staging_is_optional():
    data = load_fixture("scenes_pass.json")
    assert all("staging" not in s for s in data["scenes"])
    errors, warnings = validate_scenes(data)
    assert errors == [] and warnings == []


def test_staging_outside_the_vocabulary_is_rejected():
    data = load_fixture("scenes_pass.json")
    data["scenes"][0]["staging"] = "underwater"
    errors, _ = validate_scenes(data)
    assert any("staging" in e for e in errors)


def test_duration_mismatch_is_warning_not_error():
    data = load_fixture("scenes_pass.json")
    data["total_duration"] = data["scenes"][-1]["est_end"] + 12.0
    errors, warnings = validate_scenes(data)
    assert errors == []
    assert any("total_duration" in w for w in warnings)


def test_emphasis_is_no_longer_in_the_contract():
    """ADR-0054 — 오버레이 합성이 삭제됐다. emphasis가 오면 계약 위반이다."""
    data = load_fixture("scenes_pass.json")
    data["scenes"][0]["emphasis"] = {"type": "big_red_text", "value": "18.6"}
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


# --- 인물 블록: cast ↔ characters (ADR-0051) ----------------------------------


def _with_character(data: dict, *, cast_scenes: int = 2) -> dict:
    data["characters"] = [
        {"id": "wonhyo", "name": "원효", "anchor": "元曉", "appearance": "잿빛 승복의 승려"}
    ]
    for scene in data["scenes"][:cast_scenes]:
        scene["cast"] = ["wonhyo"]
    return data


def test_cast_pointing_at_a_defined_character_passes():
    data = _with_character(load_fixture("scenes_pass.json"))
    errors, warnings = validate_scenes(data)
    assert errors == []
    assert warnings == []


def test_cast_pointing_at_a_missing_character_is_rejected():
    """characters에 없는 id는 계약 위반이다 (specs/02, ADR-0051)."""
    data = _with_character(load_fixture("scenes_pass.json"))
    data["scenes"][0]["cast"] = ["dokkaebi"]
    errors, _ = validate_scenes(data)
    assert any("dokkaebi" in e and "ADR-0051" in e for e in errors)


def test_cast_without_a_characters_block_is_rejected():
    data = load_fixture("scenes_pass.json")
    data["scenes"][0]["cast"] = ["wonhyo"]
    errors, _ = validate_scenes(data)
    assert any("characters에 없다" in e for e in errors)


def test_duplicate_character_ids_are_rejected():
    data = _with_character(load_fixture("scenes_pass.json"))
    data["characters"].append(dict(data["characters"][0]))
    errors, _ = validate_scenes(data)
    assert any("2번 정의" in e for e in errors)


def test_character_cast_in_fewer_than_two_scenes_warns():
    """존재 기준은 '2씬 이상 반복 등장'이다 (ADR-0051) — 위반이 아니라 관측이다."""
    data = _with_character(load_fixture("scenes_pass.json"), cast_scenes=1)
    errors, warnings = validate_scenes(data)
    assert errors == []
    assert any("2씬 이상" in w for w in warnings)


def test_no_characters_block_is_not_even_a_warning():
    """블록이 없으면 인물 경로 전체가 스킵된다 — 부재는 경고가 아니다 (D-3)."""
    data = load_fixture("scenes_pass.json")
    _errors, warnings = validate_scenes(data)
    assert not any("characters" in w for w in warnings)
