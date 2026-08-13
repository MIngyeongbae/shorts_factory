"""`[1a] outline` 계약 검증. CLAUDE.md 최소 기준: 픽스처 JSON → 스키마 검증 통과.

**계약 테스트다** (ADR-0034 §4). `stages/`에 `[1a]`가 아직 없어도 이 파일은 돈다 —
검사 대상이 구현이 아니라 `specs/schema/outline.schema.json`이기 때문이다.
"""

import copy

import pytest

from conftest import load_fixture
from shorts_factory.schemas.outline import (
    act_budgets,
    budget_total,
    chosen,
    unknown_fact_ids,
    validate_outline,
)


@pytest.fixture
def outline() -> dict:
    return load_fixture("outline_pass.json")


def test_pass_fixture_is_valid(outline):
    assert validate_outline(outline) == ([], [])


def test_missing_required_field_is_rejected(outline):
    del outline["why_chosen"]
    errors, _ = validate_outline(outline)
    assert any("why_chosen" in e for e in errors)


def test_single_hook_candidate_is_rejected(outline):
    """후보가 하나면 고른 것이 아니다 — 여기서 고르는 것은 문장이 아니라 각도다."""
    outline["hook_candidates"] = outline["hook_candidates"][:1]
    outline["chosen_hook"] = 0
    errors, _ = validate_outline(outline)
    assert errors


def test_chosen_hook_out_of_range(outline):
    outline["chosen_hook"] = 9
    errors, _ = validate_outline(outline)
    assert any("chosen_hook" in e for e in errors)
    assert chosen(outline) == {}


def test_acts_must_be_numbered_from_one(outline):
    outline["acts"][2]["act"] = 9
    errors, _ = validate_outline(outline)
    assert any("연번" in e for e in errors)


def test_budget_sum_out_of_envelope_is_an_error(outline):
    """분량 산수는 이 단계에서 한 번만 한다 — 여기서 틀리면 [1w]가 맞출 수 없다."""
    outline["acts"][0]["char_budget"] += 200
    errors, _ = validate_outline(outline)
    assert any("글자 예산 합계" in e for e in errors)


def test_act_count_is_free(outline):
    """**7단 고정이 없어졌다** (ADR-0033 §2). 단 개수·이름은 소재가 정한다.

    이 테스트가 깨지면 고정 구조가 어디선가 되살아난 것이다.
    """
    merged = copy.deepcopy(outline)
    total = budget_total(outline)
    merged["acts"] = [
        {
            "act": 1,
            "name": "물이 얼면 벌어지는 일",
            "char_budget": total // 2,
            "must_convey": ["230개 블록의 조립체"],
            "grounded_in": ["f03"],
        },
        {
            "act": 2,
            "name": "그래서 230개로 쪼갰다",
            "char_budget": total - total // 2,
            "must_convey": ["230개 블록의 조립체", "지금도 그 방식"],
            "grounded_in": ["f03", "f12"],
        },
    ]
    assert validate_outline(merged) == ([], [])


def test_callback_gap_is_a_warning_not_an_error(outline):
    """수미상관은 설계 단계에서 근사로만 본다. 판정은 텍스트가 나온 뒤다."""
    last = outline["acts"][-1]
    last["grounded_in"] = ["f12"]
    last["must_convey"] = ["오늘날 댐 공사에 그대로 쓰인다"]
    errors, warnings = validate_outline(outline)
    assert errors == []
    assert any("수미상관" in w for w in warnings)


def test_hook_absent_from_first_act_is_a_warning(outline):
    first = outline["acts"][0]
    first["grounded_in"] = ["f02"]
    first["must_convey"] = ["시멘트가 굳는 원리"]
    errors, warnings = validate_outline(outline)
    assert errors == []
    assert any("훅이 앞에" in w for w in warnings)


def test_unknown_fact_ids_are_found_against_the_real_factsheet(outline):
    """그라운딩은 **형식이 아니라 실재**를 본다 (ADR-0007)."""
    factsheet = {"facts": [{"id": f"f{n:02d}"} for n in range(1, 13)]}
    assert unknown_fact_ids(outline, factsheet) == []

    outline["acts"][0]["grounded_in"] = ["f99"]
    assert unknown_fact_ids(outline, factsheet) == ["f99"]


def test_act_budgets_maps_number_to_budget(outline):
    budgets = act_budgets(outline)
    assert sorted(budgets) == [1, 2, 3, 4, 5]
    assert sum(budgets.values()) == budget_total(outline)
