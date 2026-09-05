"""판정 블록 계약 (ADR-0094, 스펙 01·07) — `script.md` 맨 위 주석, 사람은 주석만 푼다.

- 꼴: `//reject` · `[` · `//ko,` · `//ja,` · `//en` · `]`. `[1]`이 붙이고 파서가 뗀다
- 뜻: `reject`가 풀리면 반려(언어보다 세다), 언어가 풀리면 그 언어, 아무것도 없으면 보류
- 2부: `[3]`이 반려·보류에서 멈추고 언어는 ko + 고른 것. `[9]`·`[10]`은 고른 것만.
  `--lang`은 판정보다 세다. 블록이 없는 옛 편은 있는 언어 전부
- `[2]`는 정정본에 블록을 되붙이고, 세션에는 블록을 떼서 준다
"""

from __future__ import annotations

import pytest
from conftest import PISA
from test_draft_stage import TODAY, SEED, FakeLLM, seeded  # noqa: F401 — 픽스처
from test_scriptmd import make_lines, make_script
from test_tts_stage import install_md, run as run_tts

from shorts_factory.config import write_text
from shorts_factory.gate import (
    Gate,
    GateError,
    attach_gate_block,
    parse_gate,
    render_gate_block,
    split_gate_block,
    strip_gate_block,
)
from shorts_factory.judgment import JudgmentError, languages_for, read_gate
from shorts_factory.schemas.timed_scenes import LANGUAGES
from shorts_factory.stages.draft import run_draft_stage
from shorts_factory.stages.factcheck import run_factcheck_stage
from shorts_factory.stages.scriptmd import parse_script_md
from shorts_factory.stages.tts import TTSStageError

BLOCK = "//reject\n[\n//ko,\n//ja,\n//en\n]\n"


def with_block(text: str, *uncomment: str) -> str:
    """전부 주석인 블록을 앞에 붙이고 `uncomment`의 토큰만 주석을 푼다."""
    block = BLOCK
    for token in uncomment:
        block = block.replace(f"//{token}", token, 1)
    return block + text


# --- 꼴 ------------------------------------------------------------------------


def test_the_rendered_block_is_exactly_the_human_chosen_shape():
    assert render_gate_block() == BLOCK


def test_a_script_without_a_block_is_a_legacy_episode():
    gate = parse_gate(make_script(make_lines()))
    assert gate == Gate()
    assert not gate.present and gate.status == "보류"


def test_all_commented_is_pending():
    gate = parse_gate(with_block(make_script(make_lines())))
    assert gate.present and gate.pending and gate.languages == () and gate.status == "보류"


@pytest.mark.parametrize("chosen", [("ko",), ("ja",), ("ko", "en"), ("ko", "ja", "en")])
def test_uncommented_languages_are_the_selection_in_contract_order(chosen):
    gate = parse_gate(with_block(make_script(make_lines()), *reversed(chosen)))
    assert gate.languages == tuple(l for l in LANGUAGES if l in chosen)
    assert gate.status == "go" and not gate.rejected


def test_reject_wins_over_languages():
    gate = parse_gate(with_block(make_script(make_lines()), "reject", "ko", "ja"))
    assert gate.rejected and gate.status == "no-go" and not gate.pending


def test_spaces_and_a_trailing_comma_are_tolerated():
    text = "// reject\n[\n //ko ,\n ja,\n//en,\n]\n" + make_script(make_lines())
    assert parse_gate(text).languages == ("ja",)


def test_an_unknown_token_is_refused():
    with pytest.raises(GateError, match="모르는 값"):
        parse_gate("//reject\n[\n//ko,\n//jp,\n//en\n]\n" + make_script(make_lines()))


def test_an_unclosed_block_is_refused():
    with pytest.raises(GateError, match="닫히지"):
        parse_gate("//reject\n[\n//ko,\n# 제목\n")


# --- 떼고 붙이기 ---------------------------------------------------------------


def test_split_round_trips_and_strip_leaves_the_script_untouched():
    script = make_script(make_lines())
    text = with_block(script, "ja")
    block, rest = split_gate_block(text)
    assert block + rest == text
    assert rest == script
    assert strip_gate_block(text) == script
    assert attach_gate_block(script, block) == text


def test_the_parser_ignores_the_block():
    """블록은 대본이 아니다 — 머리 항목·줄 수가 블록 없는 대본과 같다."""
    script = make_script(make_lines())
    plain, gated = parse_script_md(script), parse_script_md(with_block(script, "reject"))
    assert gated.lines == plain.lines and gated.header == plain.header and gated.title == plain.title


# --- 읽기 — `judgment.read_gate` · `languages_for` ------------------------------


def test_read_gate_without_a_script_is_legacy(paths):
    assert read_gate(paths, "nowhere") == Gate()


def test_an_explicit_lang_list_beats_the_gate(paths):
    write_text(paths.topic_dir(PISA) / "script.md", with_block(make_script(make_lines()), "reject"))
    assert languages_for(paths, "20260810-" + PISA, langs=["en"], present=["ko", "en"]) == ["en"]


def test_a_legacy_episode_takes_every_present_language(paths):
    write_text(paths.topic_dir(PISA) / "script.md", make_script(make_lines()))
    assert languages_for(paths, "20260810-" + PISA, langs=None, present=["ko", "ja"]) == ["ko", "ja"]


def test_the_selection_is_filtered_by_what_exists(paths):
    write_text(paths.topic_dir(PISA) / "script.md", with_block(make_script(make_lines()), "ko", "en"))
    assert languages_for(paths, "20260810-" + PISA, langs=None, present=["ko", "ja"]) == ["ko"]


@pytest.mark.parametrize("tokens, message", [(("reject",), "반려"), ((), "보류")])
def test_rejected_and_pending_stop_the_stage(paths, tokens, message):
    write_text(paths.topic_dir(PISA) / "script.md", with_block(make_script(make_lines()), *tokens))
    with pytest.raises(JudgmentError, match=message):
        languages_for(paths, "20260810-" + PISA, langs=None, present=["ko"])


# --- [1]·[2] — 붙이고 보존한다 -----------------------------------------------------


def test_draft_prepends_a_fully_commented_block(paths, seeded):
    run_draft_stage(seeded.slug, llm=FakeLLM(make_script(make_lines())), paths=paths, run_id=seeded.run_id)
    text = (paths.topic_dir(seeded.slug) / "script.md").read_text(encoding="utf-8")
    assert text.startswith(BLOCK)
    assert parse_gate(text).pending


def test_factcheck_keeps_the_human_decision_and_hides_it_from_the_session(paths, seeded):
    run_draft_stage(seeded.slug, llm=FakeLLM(make_script(make_lines())), paths=paths, run_id=seeded.run_id)
    path = paths.topic_dir(seeded.slug) / "script.md"
    write_text(path, with_block(strip_gate_block(path.read_text(encoding="utf-8")), "ko", "ja"))

    corrected = make_script(make_lines()).replace("# 테스트 소재", "# 테스트 소재 (정정)")
    llm = FakeLLM(f"=== FACTCHECK ===\n# 팩트체크\n표\n=== SCRIPT ===\n{corrected}")
    result = run_factcheck_stage(seeded.slug, llm=llm, paths=paths, run_id=seeded.run_id)

    assert result.passed and result.script_changed
    after = path.read_text(encoding="utf-8")
    assert "(정정)" in after
    assert parse_gate(after).languages == ("ko", "ja")      # 판정이 살아 있다
    assert "//reject" not in llm.prompts[0]                  # 세션은 블록을 못 본다


# --- [3] — 2부의 첫 단계가 게이트를 읽는다 -----------------------------------------


def _gate_pisa(paths, *tokens):
    install_md(paths)
    path = paths.topic_dir(PISA) / "script.md"
    write_text(path, with_block(path.read_text(encoding="utf-8"), *tokens))


def test_tts_refuses_a_rejected_episode(paths):
    _gate_pisa(paths, "reject")
    with pytest.raises(TTSStageError, match="반려"):
        run_tts(paths)


def test_tts_refuses_a_pending_episode_unless_a_lang_is_named(paths):
    _gate_pisa(paths)
    with pytest.raises(TTSStageError, match="보류"):
        run_tts(paths)
    assert run_tts(paths, langs=["ko"]).passed


def test_tts_always_measures_korean_even_when_only_another_language_is_chosen(paths):
    """ko는 `[3s]`의 줄 경계와 `[7]` 기본 풀의 입력이라 고르지 않아도 든다 (ADR-0094 결정 4)."""
    _gate_pisa(paths, "ja")
    result = run_tts(paths)
    assert "ko" in result.languages
