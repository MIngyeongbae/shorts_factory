"""script.md 파싱·기계 검사 계약 (ADR-0049, specs/01 「기계 검사」).

값은 specs/schema/script-rules.json에서 온다 — 테스트도 손으로 옮겨 적지 않고
vocab.limits()로 읽어 픽스처를 만든다 (ADR-0034).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from shorts_factory.schemas import vocab
from shorts_factory.schemas.timed_scenes import PRIMARY_LANGUAGE
from shorts_factory.stages.scriptmd import (
    check_localized_script_md,
    check_script_md,
    localized_mark,
    parse_script_md,
    script_md_name,
    script_section_inner_blank_lines,
    split_factcheck_output,
    split_localized_output,
    strip_code_fence,
)

LIMITS = vocab.limits()


def make_lines(count: int | None = None, *, chars: int | None = None) -> list[str]:
    """엔벨로프 정중앙을 겨냥한 대본 줄. 첫·끝 줄이 명사를 공유한다 (수미상관)."""
    if count is None:
        count = (LIMITS["line_count"][0] + LIMITS["line_count"][1]) // 2
    if chars is None:
        total_target = (LIMITS["total_chars"][0] + LIMITS["total_chars"][1]) // 2
        chars = round(total_target / count)
    # '후버댐' 명사를 앞뒤에 심어 수미상관을 만족시킨다
    body = "가나다라마바사아자차카타파하거너더러머버서어저처커터퍼허"
    line = "후버댐은 " + body[: max(0, chars - 4)]  # core_chars == chars
    return [line] * count


def make_script(lines: list[str], *, claims: bool = True, fitness: str = "적합 — 구조를 보인다") -> str:
    parts = [
        "# 테스트 소재",
        "",
        "- 시드: https://example.org/a",
        f"- 매체 적합성: {fitness}",
        "- 핵심 질문: 왜?",
        "",
        "## 대본",
        "",
        *lines,
        "",
    ]
    if claims:
        parts += ["## 주장", "", "- [줄 1] 후버댐 이야기 — https://example.org/a", ""]
    return "\n".join(parts)


def test_parse_reads_title_header_lines_claims():
    doc = parse_script_md(make_script(make_lines()))
    assert doc.title == "테스트 소재"
    assert doc.seed_url == "https://example.org/a"
    assert not doc.unfit
    assert len(doc.lines) == len(make_lines())
    assert doc.claims and doc.claims[0].startswith("[줄 1]")


def test_valid_script_passes():
    errors, _warnings = check_script_md(make_script(make_lines()))
    assert errors == []


def test_line_count_out_of_range_is_an_error():
    errors, _ = check_script_md(make_script(make_lines(count=LIMITS["line_count"][0] - 3)))
    assert any("줄" in e and "범위" in e for e in errors)


def test_total_chars_out_of_range_is_an_error():
    lines = make_lines(chars=5)  # 줄 수는 맞고 글자 수만 모자라게
    errors, _ = check_script_md(make_script(lines))
    assert any("total_chars" in e for e in errors)


def test_ring_composition_failure_is_an_error():
    lines = make_lines()
    # 뒤 15% 구간(EDGE)을 전부 앞과 명사가 겹치지 않는 줄로 바꾼다
    for i in range(-5, 0):
        lines[i] = "끝자락 문장인데 앞과 겹치는 명사구 하나 없이 끝나요"
    errors, _ = check_script_md(make_script(lines))
    assert any("수미상관" in e for e in errors)


def test_unfit_returns_single_rejection_error():
    text = "\n".join([
        "# 소재", "", "- 시드: https://example.org",
        "- 매체 적합성: 부적합 — 추상 개념이라 그릴 것이 없다", "",
        "## 반려", "", "삽화만 나온다.", "",
    ])
    errors, warnings = check_script_md(text)
    assert len(errors) == 1 and "부적합" in errors[0]
    assert warnings == []


def test_missing_script_section_is_an_error():
    errors, _ = check_script_md("# 소재\n\n- 매체 적합성: 적합 — ok\n")
    assert any("대본" in e for e in errors)


def test_long_line_is_a_warning_not_an_error():
    lines = make_lines()
    lines[1] = "후버댐" + "가" * (LIMITS["line_chars_max"] + 5)
    errors, warnings = check_script_md(make_script(lines))
    assert any("줄당 최대" in w for w in warnings)
    # 줄 하나가 길다고 막지 않는다 — 총량이 엔벨로프를 벗어나는 것과는 별개다
    assert not any("줄당" in e for e in errors)


def test_missing_claims_is_a_warning():
    _, warnings = check_script_md(make_script(make_lines(), claims=False))
    assert any("주장" in w for w in warnings)


def test_strip_code_fence():
    assert strip_code_fence("```markdown\n# a\n```") == "# a"
    assert strip_code_fence("# a\n```py\nx\n```\nb").startswith("# a")  # 부분 펜스 보존


def test_split_factcheck_output_both_sections():
    out = "=== FACTCHECK ===\n# 팩트체크\n표\n=== SCRIPT ===\n# 소재\n\n## 대본\n\n줄\n"
    fact, script = split_factcheck_output(out)
    assert fact.startswith("# 팩트체크")
    assert script is not None and script.startswith("# 소재")


def test_split_factcheck_output_without_script_section():
    fact, script = split_factcheck_output("=== FACTCHECK ===\n# 팩트체크\n표\n")
    assert fact.startswith("# 팩트체크")
    assert script is None


# --- 번안 대본 (`[2l]`, ADR-0056) --------------------------------------------


def test_script_md_name_follows_the_boundary_contract():
    """ko는 script.md, 나머지는 script.{lang}.md (specs/05 1부↔2부 경계 절)."""
    assert script_md_name(PRIMARY_LANGUAGE) == "script.md"
    assert script_md_name("ja") == "script.ja.md"
    assert script_md_name("en") == "script.en.md"


def test_split_localized_output_two_sections():
    out = (
        f"{localized_mark('ja')}\n\n# 題材\n\n## 대본\n\n行\n\n"
        f"{localized_mark('en')}\n\n# Topic\n\n## 대본\n\nline\n"
    )
    sections = split_localized_output(out)
    assert set(sections) == {"ja", "en"}
    assert sections["ja"].startswith("# 題材") and sections["en"].startswith("# Topic")
    assert "# Topic" not in sections["ja"]


def test_split_localized_output_ignores_sections_that_are_not_markdown():
    out = f"{localized_mark('ja')}\n\n잡담\n\n{localized_mark('en')}\n\n# Topic\n"
    assert set(split_localized_output(out)) == {"en"}
    assert split_localized_output("마커 없는 출력") == {}


def test_split_localized_output_strips_fences_per_section():
    out = f"{localized_mark('en')}\n```markdown\n# Topic\n\n## 대본\n\nline\n```\n"
    assert split_localized_output(out)["en"].startswith("# Topic")


def _localized(lines: list[str], *, claims: bool = True) -> str:
    parts = ["# Topic", "", "- 시드: https://example.org/a", "", "## 대본", "", *lines, ""]
    if claims:
        parts += ["## 주장", "", "- [줄 1] claim — url", ""]
    return "\n".join(parts)


def test_inner_blank_lines_count_only_between_first_and_last_line():
    text = _localized(["a", "b", "", "c"])
    assert script_section_inner_blank_lines(text) == 1
    assert script_section_inner_blank_lines(_localized(["a", "b", "c"])) == 0


def test_localized_check_passes_on_matching_line_count():
    errors, warnings = check_localized_script_md(
        _localized(["a", "b", "c"]), lang="en", expected_lines=3
    )
    assert errors == []
    assert any("실측" in w for w in warnings)  # 초 상한은 [3]이 본다는 경고


def test_localized_check_line_mismatch_and_blank_line_are_errors():
    errors, _ = check_localized_script_md(_localized(["a", "b"]), lang="en", expected_lines=3)
    assert any("줄 1:1" in e for e in errors)
    errors, _ = check_localized_script_md(
        _localized(["a", "", "b", "c"]), lang="en", expected_lines=3
    )
    assert any("빈 줄" in e for e in errors)


def test_localized_check_has_no_korean_heuristics():
    """수미상관·어미 검사는 정본 몫이다 — 번안은 줄 1:1만 본다."""
    lines = ["Why does it stand?", "Because of the base.", "Water did the rest."]
    errors, _ = check_localized_script_md(_localized(lines), lang="en", expected_lines=3)
    assert errors == []


def test_localized_check_missing_claims_is_a_warning():
    _, warnings = check_localized_script_md(
        _localized(["a", "b"], claims=False), lang="ja", expected_lines=2
    )
    assert any("주장" in w for w in warnings)


def test_localized_check_applies_locale_limits(monkeypatch):
    monkeypatch.setitem(
        vocab.SCRIPT_RULES["locales"], "en",
        {"limits": {"total_chars": [100, 200], "line_chars_max": 3}},
    )
    errors, warnings = check_localized_script_md(
        _localized(["abcd", "ef"]), lang="en", expected_lines=2
    )
    assert any("total_chars" in e for e in errors)
    assert any("줄당 최대" in w for w in warnings)
