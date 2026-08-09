"""대본 ↔ 팩트시트 그라운딩 검증 (ADR-0007).

`scenes_pass.json`의 숫자는 `factsheet_pass.json`(1396년 / 18.6km / 11만 명 / 97개)에
그라운딩되어 있다. 둘 중 하나를 고치면 다른 하나도 같이 움직여야 한다.
"""

from decimal import Decimal

import pytest

from conftest import load_fixture
from shorts_factory.schemas.grounding import (
    extract_values,
    factsheet_values,
    validate_grounding,
)


def values(text: str) -> list[Decimal]:
    return [v for _, v in extract_values(text)]


# --- 숫자 추출 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1396년", ["1396"]),
        ("18.6km", ["18.6"]),
        ("118,070명", ["118070"]),
        ("35.3%", ["35.3"]),
        ("11만 명", ["110000"]),
        ("11만 8천 명", ["118000"]),
        ("2천 명", ["2000"]),
        ("숫자가 없는 문장입니다.", []),
        ("", []),
    ],
)
def test_extract_single_expression(text, expected):
    assert values(text) == [Decimal(e) for e in expected]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 자릿수 없이 나열된 숫자는 각각 별개 값이다
        ("1422년 2월 23일", ["1422", "2", "23"]),
        ("40~45cm", ["40", "45"]),
        ("성벽 3 4", ["3", "4"]),
    ],
)
def test_adjacent_numbers_stay_separate(text, expected):
    assert values(text) == [Decimal(e) for e in expected]


def test_korean_spelled_numeral_is_not_extracted():
    """한글 수사는 잡지 못한다 — 모듈 docstring에 적힌 사각지대를 고정한다."""
    assert values("아흔일곱 개 구간") == []


# --- 팩트시트 허용 집합 -------------------------------------------------------


def test_factsheet_values_from_pass_fixture():
    allowed, low_only, unparsable = factsheet_values(load_fixture("factsheet_pass.json"))
    assert allowed == {Decimal("1396"), Decimal("18.6"), Decimal("110000"), Decimal("97")}
    assert low_only == set()
    assert unparsable == []


def test_low_confidence_numbers_are_separated():
    """specs/06: confidence=low 사실은 대본에 사용 금지."""
    allowed, low_only, _ = factsheet_values(load_fixture("factsheet_fail.json"))
    assert allowed == {Decimal("1520")}
    assert low_only == {Decimal("4"), Decimal("3000")}


def test_unparsable_factsheet_number_is_reported():
    data = load_fixture("factsheet_pass.json")
    data["facts"][0]["numbers"].append("네 시기")
    _, _, unparsable = factsheet_values(data)
    assert unparsable == ["네 시기"]


# --- 그라운딩 판정 ------------------------------------------------------------


def test_pass_fixture_is_grounded():
    errors, warnings = validate_grounding(
        load_fixture("scenes_pass.json"), load_fixture("factsheet_pass.json")
    )
    assert errors == []
    assert warnings == []


def test_ungrounded_number_in_text_is_rejected():
    scenes = load_fixture("scenes_pass.json")
    scenes["scenes"][0]["text"] = "1398년에 쌓은 한양도성 성벽입니다."
    errors, _ = validate_grounding(scenes, load_fixture("factsheet_pass.json"))
    assert any("1398" in e and "scenes/1/text" in e for e in errors)


def test_ungrounded_number_in_emphasis_is_rejected():
    """emphasis는 화면에 합성되는 대형 숫자라 본문과 같이 대조한다 (ADR-0002)."""
    scenes = load_fixture("scenes_pass.json")
    scenes["scenes"][11]["emphasis"]["value"] = "120"
    errors, _ = validate_grounding(scenes, load_fixture("factsheet_pass.json"))
    assert any("120" in e and "scenes/12/emphasis" in e for e in errors)


def test_unit_is_ignored_when_matching():
    """값만 비교한다 — 팩트시트의 '18.6km'가 대본의 '18.6%'를 통과시킨다 (알려진 한계)."""
    scenes = load_fixture("scenes_pass.json")
    scenes["scenes"][5]["text"] = "하지만 18.6%는 감시할 수 없었죠."
    errors, _ = validate_grounding(scenes, load_fixture("factsheet_pass.json"))
    assert errors == []


def test_magnitude_form_matches_factsheet_digits():
    """대본이 '11만'으로 써도 팩트시트 '11만 명'과 같은 값으로 붙는다."""
    scenes = load_fixture("scenes_pass.json")
    scenes["scenes"][3]["text"] = "동원된 인원만 110,000명이었죠."
    errors, _ = validate_grounding(scenes, load_fixture("factsheet_pass.json"))
    assert errors == []


def test_low_confidence_number_gets_its_own_message():
    factsheet = load_fixture("factsheet_pass.json")
    low = next(f for f in factsheet["facts"] if f["numbers"] == ["97개"])
    low["confidence"] = "low"
    errors, _ = validate_grounding(load_fixture("scenes_pass.json"), factsheet)
    assert any("low" in e for e in errors)
    assert all("팩트시트 numbers에 없다" not in e for e in errors)


def test_every_ungrounded_number_is_reported():
    """ADR-0007: 전수 추출. 하나만 걸리고 멈추면 안 된다."""
    scenes = load_fixture("scenes_pass.json")
    scenes["scenes"][0]["text"] = "1398년입니다."
    scenes["scenes"][2]["text"] = "동원 인원은 7만 명이었죠."
    errors, _ = validate_grounding(scenes, load_fixture("factsheet_pass.json"))
    assert len(errors) == 2


def test_empty_factsheet_rejects_every_number():
    """픽스처가 쓰는 숫자 6곳: 1396 / 11만(text·emphasis) / 18.6 / 97(text·emphasis)."""
    scenes = load_fixture("scenes_pass.json")
    errors, _ = validate_grounding(scenes, {"facts": []})
    assert len(errors) == 6
