"""팩트시트 계약 검증. CLAUDE.md 최소 기준: 픽스처 JSON → 스키마 검증 통과."""

import copy

import pytest

from conftest import load_fixture
from shorts_factory.schemas.factsheet import MIN_NUMBERS, validate_factsheet


def test_pass_fixture_is_valid():
    errors, _ = validate_factsheet(load_fixture("factsheet_pass.json"))
    assert errors == []


def test_fail_fixture_is_valid():
    """verdict=fail도 계약을 만족해야 한다 (반려 경로도 스키마를 탄다)."""
    errors, _ = validate_factsheet(load_fixture("factsheet_fail.json"))
    assert errors == []


def test_low_confidence_raises_warning_not_error():
    errors, warnings = validate_factsheet(load_fixture("factsheet_fail.json"))
    assert errors == []
    assert any("low" in w for w in warnings)


def test_missing_required_field_is_rejected():
    data = load_fixture("factsheet_pass.json")
    del data["present_link"]
    errors, _ = validate_factsheet(data)
    assert any("present_link" in e for e in errors)


def test_unknown_field_is_rejected():
    data = load_fixture("factsheet_pass.json")
    data["extra_field"] = "스키마에 없는 키"
    errors, _ = validate_factsheet(data)
    assert errors


def test_bad_confidence_value_is_rejected():
    data = load_fixture("factsheet_pass.json")
    data["facts"][0]["confidence"] = "확실함"
    errors, _ = validate_factsheet(data)
    assert any("confidence" in e for e in errors)


def test_conditions_do_not_decide_the_verdict():
    """`conditions`는 관측이지 판정이 아니다 (ADR-0033 §1).

    지표가 다 차 있어도 매체 적합성에서 fail일 수 있고, 비어 있어도 pass일 수 있다.
    둘을 묶으면 지표가 다시 게이트가 된다.
    """
    data = load_fixture("factsheet_pass.json")
    data["verdict"] = "fail"
    errors, _ = validate_factsheet(data)
    assert errors == []


def test_an_empty_indicator_does_not_reject_the_topic():
    data = load_fixture("factsheet_pass.json")
    data["conditions"]["numbers"] = False
    errors, _ = validate_factsheet(data)
    assert errors == []


def test_numbers_condition_requires_minimum_count():
    """specs/06 조건 3: 구체적 숫자 최소 3개."""
    data = load_fixture("factsheet_pass.json")
    data["facts"] = data["facts"][:1]
    data["facts"][0]["numbers"] = ["1396년"]
    errors, _ = validate_factsheet(data)
    assert any(str(MIN_NUMBERS) in e for e in errors)


def test_spec_inline_example_is_abbreviated():
    """specs/06 본문 예시는 fact 1건짜리 축약형이라 그대로는 계약을 통과하지 못한다.

    스펙의 예시가 완성본이 아니라는 사실을 고정해 둔다. 완성본은 픽스처가 기준이다.
    """
    abbreviated = {
        "topic": "한양도성 각자성석",
        "verdict": "pass",
        "conditions": {
            "twist": True, "failed_alternative": True,
            "numbers": True, "present_link": True,
        },
        "facts": [
            {
                "id": "f01",
                "claim": "1396년 태조가 한양 둘레 18.6km 성벽 공사를 시작했다",
                "numbers": ["1396년", "18.6km"],
                "source": "실록 태조 5년 1월",
                "confidence": "high",
            }
        ],
        "twist": "성돌의 이름은 낙서가 아니라 국가가 새기게 한 책임 표기다",
        "failed_alternatives": ["감독관 배치", "포상제"],
        "present_link": "흥인지문 옆·낙산 구간에서 각자성석 실견 가능",
    }
    errors, _ = validate_factsheet(abbreviated)
    assert any(str(MIN_NUMBERS) in e for e in errors)


def test_empty_source_is_rejected():
    data = load_fixture("factsheet_pass.json")
    data["facts"][0]["source"] = "   "
    errors, _ = validate_factsheet(data)
    assert any("출처" in e for e in errors)


def test_duplicate_fact_ids_are_rejected():
    data = load_fixture("factsheet_pass.json")
    data["facts"][1]["id"] = data["facts"][0]["id"]
    errors, _ = validate_factsheet(data)
    assert any("중복" in e for e in errors)


@pytest.mark.parametrize("bad_id", ["1", "fact01", "F01", "f1"])
def test_fact_id_format(bad_id):
    data = copy.deepcopy(load_fixture("factsheet_pass.json"))
    data["facts"][0]["id"] = bad_id
    errors, _ = validate_factsheet(data)
    assert errors
