"""`[1s] sceneplan` 계약 검증. CLAUDE.md 최소 기준: 픽스처 JSON → 스키마 검증 통과.

**계약 테스트다** (ADR-0034 §4). `stages/`에 `[1s]`가 아직 없어도 돈다.

이 파일이 지키는 것은 셋이다 — `says`와 `text`의 경계(`[1s]`↔`[1w]`), 단 예산의
연결(`[1a]`↔`[1s]`), 그리고 계획이 대본으로 **그대로** 건너가는가(`[1w]`).
"""

import copy

import pytest

from conftest import load_fixture
from shorts_factory.schemas import vocab
from shorts_factory.schemas.sceneplan import (
    ACT_BUDGET_TOLERANCE,
    PLANNED_SCENE_SCHEMA,
    act_budget_errors,
    carried_fields,
    carry_errors,
    direction_summary,
    plan_only_fields,
    says_intrusions,
    validate_sceneplan,
)


@pytest.fixture
def plan() -> dict:
    return load_fixture("sceneplan_pass.json")


@pytest.fixture
def outline() -> dict:
    return load_fixture("outline_pass.json")


def as_script(plan: dict) -> dict:
    """계획을 `[1w]`가 규칙대로 옮긴 `06-script.json`."""
    scenes = []
    start = 0.0
    for planned in plan["scenes"]:
        scene = {f: planned[f] for f in carried_fields() if f in planned}
        scene["text"] = f"{planned['says']}입니다."
        scene["est_start"] = start
        scene["est_end"] = start = start + 3.8
        scenes.append(scene)
    return {
        "run_id": "20260813-hubeodaem",
        "topic": plan["topic"],
        "total_duration": start,
        "scenes": scenes,
    }


def test_pass_fixture_is_valid(plan):
    assert validate_sceneplan(plan) == ([], [])


def test_pass_fixture_matches_its_outline(plan, outline):
    assert validate_sceneplan(plan, outline) == ([], [])


def test_outline_is_optional(plan):
    """선택적 입력의 부재는 경고가 아니다 (ADR-0034 D-3)."""
    errors, warnings = validate_sceneplan(plan, None)
    assert errors == [] and warnings == []


# --- says와 text의 경계 (ADR-0029) ---------------------------------------


@pytest.mark.parametrize(
    "says, expect",
    [
        ("후버댐이 한 덩어리가 아니라는 사실", False),
        ("블록마다 따로 붓고 따로 식힌다", False),
        ("230개 블록으로 나눈 이유", False),
        ("후버댐은 한 덩어리가 아닙니다", True),
        ("따로 붓고 따로 식혔죠", True),
        ("그럼 어떻게 식혔을까?", True),
        ("그래서 발상을 뒤집습니다", True),
        ("환장할 노릇이죠", True),
    ],
)
def test_says_is_a_gist_not_a_sentence(says, expect):
    """존댓말 종결어미·수사 의문문·시그니처 셋만 본다. 평서 '한다'체는 요지다."""
    assert bool(says_intrusions(says)) is expect


def test_noun_ending_in_yo_is_not_flagged():
    """'수요'·'필요'처럼 요로 끝나는 명사가 요지에 흔하다 — 오탐을 고정한다."""
    assert says_intrusions("냉각 설비에 들어간 전력 수요") == []


def test_sentence_in_says_is_an_error(plan):
    plan["scenes"][3]["says"] = "통으로 부으려고 했습니다"
    errors, _ = validate_sceneplan(plan)
    assert any("says" in e and "[1w]" in e for e in errors)


# --- 분량과 씬 수 ---------------------------------------------------------


def test_scene_count_out_of_range(plan):
    plan["scenes"] = plan["scenes"][:8]
    errors, _ = validate_sceneplan(plan)
    assert any("씬 8개" in e for e in errors)


def test_scene_ids_must_be_sequential(plan):
    plan["scenes"][4]["scene_id"] = 99
    errors, _ = validate_sceneplan(plan)
    assert any("연번" in e for e in errors)


def test_char_budget_over_line_max_is_an_error(plan):
    plan["scenes"][0]["char_budget"] = 60
    errors, _ = validate_sceneplan(plan)
    assert any("한 줄에 안 들어가면" in e for e in errors)


# --- 단 예산의 연결 (ADR-0029) --------------------------------------------


def test_act_budget_within_tolerance_passes(plan, outline):
    plan["scenes"][0]["char_budget"] += ACT_BUDGET_TOLERANCE
    plan["scenes"][1]["char_budget"] -= ACT_BUDGET_TOLERANCE
    assert act_budget_errors(plan, outline) == []


def test_act_budget_drift_is_an_error(plan, outline):
    plan["scenes"][0]["char_budget"] += ACT_BUDGET_TOLERANCE + 1
    errors = act_budget_errors(plan, outline)
    assert any("acts/1" in e for e in errors)


def test_scene_pointing_at_a_missing_act(plan, outline):
    plan["scenes"][0]["act"] = 7
    errors = act_budget_errors(plan, outline)
    assert any("07-outline.json에 없다" in e for e in errors)


def test_act_with_no_scenes(plan, outline):
    """단을 배분해 놓고 씬을 안 만들면 그 단은 대본에서 사라진다."""
    plan["scenes"] = [s for s in plan["scenes"] if s["act"] != 3]
    for index, scene in enumerate(plan["scenes"], start=1):
        scene["scene_id"] = index
    errors = act_budget_errors(plan, outline)
    assert any("acts/3" in e and "씬이 하나도" in e for e in errors)


# --- 연출 공백은 관측이지 판정이 아니다 (ADR-0033) --------------------------


def test_blank_framing_warns_but_does_not_block(plan):
    for scene in plan["scenes"]:
        scene.pop("framing", None)
    errors, warnings = validate_sceneplan(plan)
    assert errors == []
    assert any("framing이 빈 씬 25/25개" in w for w in warnings)


def test_direction_summary_counts_without_judging(plan):
    summary = direction_summary(plan)
    assert summary["scene_count"] == 25
    assert sum(summary["framing"].values()) == 25
    assert summary["emphasis"] == 5
    plan["scenes"][0].pop("transition")
    assert direction_summary(plan)["transition"]["(없음)"] == 1


# --- 계획 → 대본 복사 (ADR-0029의 [1w] 계약) ------------------------------


def test_carried_fields_is_the_intersection_of_two_schemas():
    """목록을 손으로 적지 않는다. 스키마에 필드가 늘면 대조가 저절로 따라온다."""
    plan_props = set(PLANNED_SCENE_SCHEMA["properties"])
    scene_props = set(vocab.SCENE_SCHEMA_DOC["$defs"]["scene"]["properties"])
    assert set(carried_fields()) == plan_props & scene_props
    assert set(plan_only_fields()) == {"act", "says", "char_budget"}
    assert scene_props - plan_props == {"text", "est_start", "est_end"}


def _without_descriptions(node):
    if isinstance(node, dict):
        return {k: _without_descriptions(v) for k, v in node.items() if k != "description"}
    if isinstance(node, list):
        return [_without_descriptions(v) for v in node]
    return node


def test_carried_fields_are_defined_identically_in_both_schemas():
    """복사되는 필드의 **정의**가 두 스키마에서 갈리면 [1w]가 통과시킨 값이 [2]에서 걸린다."""
    plan_props = PLANNED_SCENE_SCHEMA["properties"]
    scene_props = vocab.SCENE_SCHEMA_DOC["$defs"]["scene"]["properties"]
    for field in carried_fields():
        assert _without_descriptions(plan_props[field]) == _without_descriptions(
            scene_props[field]
        ), f"{field}: 두 스키마의 정의가 다르다"


def test_faithful_copy_has_no_carry_errors(plan):
    assert carry_errors(plan, as_script(plan)) == []


def test_changed_field_is_caught(plan):
    script = as_script(plan)
    script["scenes"][6]["subject"] = "다른 피사체"
    errors = carry_errors(plan, script)
    assert any("scenes/7/subject" in e for e in errors)


def test_merged_scene_is_caught(plan):
    """[1w]는 씬을 합칠 수 없다 — 합치면 그 씬의 그림 계획 하나가 버려진다."""
    script = as_script(plan)
    del script["scenes"][10]
    errors = carry_errors(plan, script)
    assert any("씬을 만들거나 합칠 수 없다" in e for e in errors)


def test_dropped_optional_field_is_caught(plan):
    """`subject_anchor`가 조용히 새던 자리다 (ADR-0028)."""
    script = as_script(plan)
    script["scenes"][0].pop("subject_anchor")
    errors = carry_errors(plan, script)
    assert any("subject_anchor" in e for e in errors)


def test_carried_scenes_satisfy_the_scene_contract(plan):
    """계획을 그대로 옮긴 대본이 씬 계약을 통과하는가 — 두 스키마가 실제로 이어지는지."""
    from shorts_factory.schemas.scenes import schema_errors

    assert schema_errors(as_script(copy.deepcopy(plan))) == []
