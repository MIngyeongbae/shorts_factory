"""MJ 방언 (ADR-0027) — 표기 변환만 하고 룰은 건드리지 않는다.

확인 대상:
- 실측으로 확정한 문자열 모양 (`runs/20260812-mj-lang-probe/`의 9잡, G3 20잡, 단면 7잡)
- 방언이 바뀌어도 **룰은 같다** — 구도·네거티브 항목·한국어 subject
- `style.dialect`가 산출물에 남고, 스키마가 그것을 강제한다
"""

from __future__ import annotations

import pytest

from shorts_factory.schemas.visual_rules import (
    ASPECT_RATIO,
    BASE_STYLE,
    COMPOSITION,
    DEFAULT_DIALECT,
    DIALECTS,
    MJ_COMPOSITION,
    MJ_GLOBAL_NEGATIVES,
    build_mj_negative,
    build_mj_prompt,
    build_scene_prompt,
    schema_errors,
)
from shorts_factory.stages.prompt import build_prompts

from conftest import HOOVER, load_script

SUBJECT = "홈이 파인 블록 접합면 클로즈업"
GOAL = "맞물린 홈이 어떤 모양인지"
SHOT = "tight detail close-up of the solution"


# --- 문법 -------------------------------------------------------------------


def test_mj_prompt_is_one_line_with_the_aspect_flag():
    prompt = build_mj_prompt(SHOT, SUBJECT, GOAL)

    assert "\n" not in prompt
    assert prompt.endswith(f"--ar {ASPECT_RATIO}")
    # 라벨을 붙이지 않는다 — MJ는 그것을 구분자가 아니라 그릴 대상으로 읽는다.
    assert "Subject" not in prompt and "Shot:" not in prompt


def test_mj_prompt_keeps_the_korean_subject_untranslated():
    """실측: 한국어가 영어와 품질 차가 없었다 (ADR-0027 맥락). 번역 경로는 없다."""
    prompt = build_mj_prompt(SHOT, SUBJECT, GOAL)

    assert SUBJECT in prompt
    assert GOAL in prompt


def test_mj_prompt_drops_the_subtitle_rationale():
    """양성 프롬프트의 `for subtitles`가 `--no burned-in subtitles`와 싸운다."""
    assert "for subtitles" in COMPOSITION
    assert "for subtitles" not in MJ_COMPOSITION
    assert "bottom third left empty" in MJ_COMPOSITION
    # 종횡비는 --ar가 정한다. 본문에 두 번 말하지 않는다.
    assert "vertical 9:16" not in MJ_COMPOSITION


def test_mj_prompt_flattens_the_style_clauses():
    """MJ는 `:`·`;`를 구분자로 읽지 않는다."""
    prompt = build_mj_prompt(SHOT, SUBJECT)

    assert "mixed media, the focal subject" in prompt
    assert ";" not in prompt
    # 콜론이 남는 자리는 종횡비 하나뿐이다 (`--ar 9:16`).
    assert prompt.count(":") == 1


def test_mj_negative_is_a_list_not_a_sentence():
    negative = build_mj_negative(())

    assert negative.startswith("--no ")
    assert "Do not include" not in negative
    for item in MJ_GLOBAL_NEGATIVES:
        assert item in negative


def test_mj_negative_appends_scene_overlays_without_duplicates():
    negative = build_mj_negative(("big_red_text", "sparkle_particles"))

    assert "large red number text" in negative
    assert negative.count("sparkle particle overlay") == 1


def test_mj_prompt_omits_the_goal_line_when_absent():
    """필드가 없던 옛 대본도 있다. 빈 항목을 남기지 않는다."""
    prompt = build_mj_prompt(SHOT, SUBJECT, "   ")

    assert ", , " not in prompt
    assert prompt.split(", ")[1] == SHOT


def test_the_shipped_string_matches_what_was_verified():
    """`[6]`이 보내는 것은 prompt + ' ' + negative_prompt다.

    이 조합이 relax 9잡에서 정상 산출을 낸 그 문자열이다. 조립 순서가 바뀌면
    검증되지 않은 프롬프트를 과금 경로로 보내게 된다.
    """
    prompt, negative = build_scene_prompt(
        "mj", shot=SHOT, subject=SUBJECT, visual_goal="", overlay_types=()
    )

    line = f"{prompt} {negative}"
    assert line.startswith(SUBJECT)
    assert " --ar 9:16 --no burned-in subtitles, caption bars," in line
    assert "bottom third left empty --ar" in line


# --- 룰은 방언과 무관하다 ----------------------------------------------------


@pytest.mark.parametrize("dialect", DIALECTS)
def test_both_dialects_carry_the_same_rules(dialect):
    prompt, negative = build_scene_prompt(
        dialect, shot=SHOT, subject=SUBJECT, visual_goal=GOAL, overlay_types=()
    )

    assert SUBJECT in prompt and GOAL in prompt and SHOT in prompt
    assert "realism dominant over painterly looseness" in prompt
    assert "sparkle particle overlay" in negative


def test_unknown_dialect_is_refused():
    with pytest.raises(ValueError, match="방언"):
        build_scene_prompt(
            "sdxl", shot=SHOT, subject=SUBJECT, visual_goal="", overlay_types=()
        )


def test_base_style_is_the_same_text_in_both_dialects():
    """방언은 표기 변환이지 다른 룰이 아니다. BASE_STYLE의 절 순서까지 같다."""
    mj = build_mj_prompt(SHOT, SUBJECT)

    flattened = BASE_STYLE.replace(": ", ", ").replace("; ", ", ")
    assert flattened in mj


# --- 산출물 계약 -------------------------------------------------------------


@pytest.mark.parametrize("dialect", DIALECTS)
def test_document_records_its_dialect(dialect):
    script = load_script(HOOVER)

    document, _warnings = build_prompts(
        script, source_script="topics/x/06-script.json", dialect=dialect
    )

    assert document["style"]["dialect"] == dialect
    assert schema_errors(document) == []


def test_schema_rejects_a_document_without_a_dialect():
    """옛 prompts.json이 위반이 되는 지점이다. [5]는 무료라 다시 돌리면 된다."""
    script = load_script(HOOVER)
    document, _ = build_prompts(script, source_script="topics/x/06-script.json")

    del document["style"]["dialect"]

    assert any("dialect" in err for err in schema_errors(document))


def test_default_dialect_is_the_shipping_path():
    """ADR-0025가 실물 경로를 MJ로 정했다."""
    assert DEFAULT_DIALECT == "mj"
    script = load_script(HOOVER)

    document, _ = build_prompts(script, source_script="topics/x/06-script.json")

    assert document["style"]["dialect"] == "mj"
