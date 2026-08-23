"""[2l. localize] 단계 계약 (ADR-0056 결정 5, specs/01 「번안 대본」, specs/05 `[2l]`).

세션은 페이크다 — 여기서 검증하는 것은 배선과 기계 검사다: 정본·줄 수가 세션에
실리는가, 줄 1:1이 깨진 언어만 막히는가, 있는 파일을 덮어쓰지 않는가, 기록이 남는가.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from shorts_factory.runstate import RunState
from shorts_factory.schemas import vocab
from shorts_factory.schemas.timed_scenes import LANGUAGES, PRIMARY_LANGUAGE
from shorts_factory.stages import localize as localize_mod
from shorts_factory.stages.draft import run_draft_stage
from shorts_factory.stages.factcheck import run_factcheck_stage
from shorts_factory.stages.localize import (
    EXISTING,
    FAILED,
    MISSING,
    WRITTEN,
    LocalizeStageError,
    run_localize_stage,
)
from shorts_factory.stages.scriptmd import localized_mark, script_md_name
from shorts_factory.stages.session import PROMPTS_DIR
from shorts_factory.stages.topic import run_topic_stage

from test_scriptmd import make_lines, make_script

TODAY = date(2026, 8, 22)
SEED = "https://ko.wikipedia.org/wiki/각자성석"

#: ADR-0050이 끊은 레퍼런스 채널 문구 — 프롬프트 어디에도 없어야 한다.
REFERENCE_PHRASES = ("환장할 노릇", "발상을 뒤집습니다", "가장 쉬운 방법은")


class FakeLLM:
    def __init__(self, *texts: str):
        self.texts = list(texts)
        self.prompts: list[str] = []
        self.tools: list[tuple[str, ...]] = []

    def run(self, prompt, *, allowed_tools=(), timeout=None, label="", **_kw):
        self.prompts.append(prompt)
        self.tools.append(tuple(allowed_tools))
        if not self.texts:
            raise AssertionError("세션이 불리면 안 되는 자리다")
        return SimpleNamespace(text=self.texts.pop(0))


# --- 픽스처 -------------------------------------------------------------------


def make_localized(lang: str, lines: list[str], *, claims: bool = True) -> str:
    """번안 대본 — 정본과 같은 포맷, 줄만 그 언어."""
    titles = {"ja": "テスト題材", "en": "Test Topic"}
    parts = [
        f"# {titles.get(lang, lang)}",
        "",
        "- 시드: https://example.org/a",
        "- 매체 적합성: 適合 — 構造を見せる" if lang == "ja" else "- 매체 적합성: fit — shows the structure",
        "",
        "## 대본",
        "",
        *lines,
        "",
    ]
    if claims:
        parts += ["## 주장", "", "- [줄 1] Hoover Dam — https://example.org/a", ""]
    return "\n".join(parts)


def lines_for(lang: str, count: int) -> list[str]:
    if lang == "ja":
        return [f"フーバーダムのコンクリートは{i}番目の秘密を隠しています。" for i in range(1, count + 1)]
    return [f"Hoover Dam's concrete hides secret number {i}." for i in range(1, count + 1)]


def session_output(sections: dict[str, str]) -> str:
    return "\n\n".join(f"{localized_mark(lang)}\n\n{text}" for lang, text in sections.items())


@pytest.fixture
def ko_lines() -> list[str]:
    return make_lines()


@pytest.fixture
def checked(paths, ko_lines):
    """[0]+[1]+[2]가 끝난 토픽 — [2l]의 전제다."""
    topic = run_topic_stage("한양도성 각자성석", paths=paths, today=TODAY, seed_url=SEED)
    script = make_script(ko_lines)
    run_draft_stage(topic.slug, llm=FakeLLM(script), paths=paths, run_id=topic.run_id)
    run_factcheck_stage(
        topic.slug,
        llm=FakeLLM(f"=== FACTCHECK ===\n# 팩트체크\n| 1 | 후버댐 | 확인 | url | — |\n=== SCRIPT ===\n{script}"),
        paths=paths, run_id=topic.run_id,
    )
    return topic


def good_output(count: int, langs=("ja", "en")) -> str:
    return session_output({lang: make_localized(lang, lines_for(lang, count)) for lang in langs})


def target_path(paths, slug, lang):
    return paths.topic_dir(slug) / script_md_name(lang)


# --- 프롬프트 -----------------------------------------------------------------


def test_prompt_carries_script_line_count_factcheck_and_no_reference_phrases(paths, checked, ko_lines):
    llm = FakeLLM(good_output(len(ko_lines)))
    run_localize_stage(checked.slug, llm=llm, paths=paths, run_id=checked.run_id)

    prompt = llm.prompts[0]
    assert "## 대본" in prompt and ko_lines[0] in prompt          # 정본 전문
    assert f"**{len(ko_lines)}**" in prompt                       # 줄 수
    assert "# 팩트체크" in prompt                                  # factcheck.md
    assert localized_mark("ja") in prompt and localized_mark("en") in prompt
    assert llm.tools[0] == ()                                     # 도구 없음 (ADR-0011)
    for phrase in REFERENCE_PHRASES:
        assert phrase not in prompt


def test_prompt_does_not_hand_copy_the_measured_ratios():
    """ADR-0056 맥락 4의 ×1.13·×0.78은 실측이지 계약이 아니다 — 프롬프트에 숫자로 적지 않는다."""
    text = (PROMPTS_DIR / localize_mod.PROMPT_FILE).read_text(encoding="utf-8")
    assert "1.13" not in text and "0.78" not in text
    for phrase in REFERENCE_PHRASES:
        assert phrase not in text


# --- 정상 경로 ----------------------------------------------------------------


def test_writes_both_files_record_state_and_checklist(paths, checked, ko_lines):
    result = run_localize_stage(
        checked.slug, llm=FakeLLM(good_output(len(ko_lines))), paths=paths, run_id=checked.run_id,
    )

    assert result.passed
    for lang in ("ja", "en"):
        path = target_path(paths, checked.slug, lang)
        assert path.is_file() and path.read_text(encoding="utf-8").startswith("#")
        assert result.outcome(lang).status == WRITTEN
        assert result.outcome(lang).line_count == len(ko_lines)

    import json
    record = json.loads(result.record_path.read_text(encoding="utf-8"))
    assert record["source_lines"] == len(ko_lines)
    assert record["languages"]["ja"]["status"] == WRITTEN
    assert record["languages"]["en"]["status"] == WRITTEN
    assert record["session_called"] is True

    state = RunState.load_or_create(paths.run_dir(checked.run_id), checked.run_id)
    assert state.is_done(localize_mod.STAGE)
    status_text = (paths.topic_dir(checked.slug) / "STATUS.md").read_text(encoding="utf-8")
    assert "- [x] 2l. localize" in status_text
    assert status_text.startswith("# STATUS: 보류")  # 판정 줄은 건드리지 않는다


def test_ko_primary_script_is_untouched(paths, checked, ko_lines):
    before = target_path(paths, checked.slug, PRIMARY_LANGUAGE).read_text(encoding="utf-8")
    run_localize_stage(
        checked.slug, llm=FakeLLM(good_output(len(ko_lines))), paths=paths, run_id=checked.run_id,
    )
    assert target_path(paths, checked.slug, PRIMARY_LANGUAGE).read_text(encoding="utf-8") == before


def test_seconds_are_not_estimated_without_a_locale_block(paths, checked, ko_lines):
    """로케일 블록이 없으면 줄 수 일치만 — 초 상한은 [3]이 실측으로 본다 (경고로 남긴다).

    en에는 블록이 없다 (ADR-0057 — ja는 임시 초 상한 블록이 있다)."""
    assert vocab.locale_limits("en") == {}
    result = run_localize_stage(
        checked.slug, llm=FakeLLM(good_output(len(ko_lines))), paths=paths, run_id=checked.run_id,
    )
    assert result.passed
    assert any("en:" in w and "실측" in w and "블록 없음" in w for w in result.warnings)


# --- 줄 1:1 -------------------------------------------------------------------


def test_line_mismatch_fails_only_that_language_and_rerun_fills_it(paths, checked, ko_lines):
    n = len(ko_lines)
    broken = session_output({
        "ja": make_localized("ja", lines_for("ja", n)),
        "en": make_localized("en", lines_for("en", n - 1)),
    })
    result = run_localize_stage(checked.slug, llm=FakeLLM(broken), paths=paths, run_id=checked.run_id)

    assert not result.passed
    assert result.outcome("ja").status == WRITTEN and target_path(paths, checked.slug, "ja").is_file()
    assert result.outcome("en").status == FAILED and not target_path(paths, checked.slug, "en").exists()
    assert any("en:" in e and f"{n - 1}줄" in e for e in result.errors)
    state = RunState.load_or_create(paths.run_dir(checked.run_id), checked.run_id)
    assert state.status_of(localize_mod.STAGE) == "failed"

    # 다시 돌리면 en만 만든다 — ja는 있으므로 세션에 싣지 않는다
    llm = FakeLLM(good_output(n, langs=("en",)))
    result = run_localize_stage(checked.slug, llm=llm, paths=paths, run_id=checked.run_id)

    assert result.passed
    assert result.outcome("ja").status == EXISTING
    assert result.outcome("en").status == WRITTEN and target_path(paths, checked.slug, "en").is_file()
    assert localized_mark("en") in llm.prompts[0] and localized_mark("ja") not in llm.prompts[0]


def test_blank_line_inside_script_section_fails(paths, checked, ko_lines):
    n = len(ko_lines)
    en_lines = lines_for("en", n)
    en_lines.insert(3, "")  # 비어 있지 않은 줄 수는 맞되 중간에 빈 줄
    broken = session_output({
        "ja": make_localized("ja", lines_for("ja", n)),
        "en": make_localized("en", en_lines),
    })
    result = run_localize_stage(checked.slug, llm=FakeLLM(broken), paths=paths, run_id=checked.run_id)

    assert result.outcome("en").status == FAILED
    assert any("빈 줄" in e for e in result.errors)
    assert not target_path(paths, checked.slug, "en").exists()


def test_session_missing_a_language_section(paths, checked, ko_lines):
    only_ja = good_output(len(ko_lines), langs=("ja",))
    result = run_localize_stage(checked.slug, llm=FakeLLM(only_ja), paths=paths, run_id=checked.run_id)

    assert not result.passed
    assert result.outcome("ja").status == WRITTEN
    assert result.outcome("en").status == MISSING
    assert any(localized_mark("en") in e for e in result.errors)


# --- 있는 파일은 건드리지 않는다 ---------------------------------------------


def test_existing_file_is_never_overwritten(paths, checked, ko_lines):
    ja_path = target_path(paths, checked.slug, "ja")
    human = make_localized("ja", lines_for("ja", len(ko_lines))).replace("テスト題材", "人が直した題材")
    ja_path.write_text(human, encoding="utf-8")

    llm = FakeLLM(good_output(len(ko_lines), langs=("en",)))
    result = run_localize_stage(checked.slug, llm=llm, paths=paths, run_id=checked.run_id)

    assert result.passed and result.outcome("ja").status == EXISTING
    assert ja_path.read_text(encoding="utf-8") == human
    assert localized_mark("ja") not in llm.prompts[0]

    # --force도 넘지 않는다 — 지우는 것이 재생성 요청이다
    result = run_localize_stage(
        checked.slug, llm=FakeLLM(), paths=paths, run_id=checked.run_id, force=True,
    )
    assert ja_path.read_text(encoding="utf-8") == human
    assert result.outcome("ja").status == EXISTING and result.outcome("en").status == EXISTING


def test_existing_file_with_wrong_line_count_is_kept_with_a_warning(paths, checked, ko_lines):
    ja_path = target_path(paths, checked.slug, "ja")
    ja_path.write_text(make_localized("ja", lines_for("ja", 3)), encoding="utf-8")

    result = run_localize_stage(
        checked.slug, llm=FakeLLM(good_output(len(ko_lines), langs=("en",))),
        paths=paths, run_id=checked.run_id,
    )
    assert result.passed  # 사람 파일은 막지 않는다 — [3]이 줄 수를 대조한다
    assert any("ja:" in w and "3줄" in w for w in result.warnings)


def test_all_files_present_skips_the_session(paths, checked, ko_lines):
    for lang in ("ja", "en"):
        target_path(paths, checked.slug, lang).write_text(
            make_localized(lang, lines_for(lang, len(ko_lines))), encoding="utf-8"
        )
    result = run_localize_stage(checked.slug, llm=FakeLLM(), paths=paths, run_id=checked.run_id)

    assert result.passed and not result.skipped
    assert all(o.status == EXISTING for o in result.outcomes)
    import json
    record = json.loads(result.record_path.read_text(encoding="utf-8"))
    assert record["session_called"] is False
    state = RunState.load_or_create(paths.run_dir(checked.run_id), checked.run_id)
    assert state.is_done(localize_mod.STAGE)


def test_done_stage_is_skipped_on_rerun(paths, checked, ko_lines):
    run_localize_stage(
        checked.slug, llm=FakeLLM(good_output(len(ko_lines))), paths=paths, run_id=checked.run_id,
    )
    result = run_localize_stage(checked.slug, llm=FakeLLM(), paths=paths, run_id=checked.run_id)
    assert result.skipped and result.passed


# --- 언어 선택 ----------------------------------------------------------------


def test_lang_filter_limits_the_session_and_the_files(paths, checked, ko_lines):
    llm = FakeLLM(good_output(len(ko_lines), langs=("ja",)))
    result = run_localize_stage(
        checked.slug, llm=llm, paths=paths, run_id=checked.run_id, langs=["ja"],
    )
    assert result.passed and [o.lang for o in result.outcomes] == ["ja"]
    assert target_path(paths, checked.slug, "ja").is_file()
    assert not target_path(paths, checked.slug, "en").exists()
    assert localized_mark("en") not in llm.prompts[0]


def test_primary_language_is_not_a_target(paths, checked):
    with pytest.raises(LocalizeStageError):
        run_localize_stage(
            checked.slug, llm=FakeLLM(), paths=paths, run_id=checked.run_id, langs=[PRIMARY_LANGUAGE],
        )


def test_target_languages_come_from_the_shared_list():
    """언어 목록은 schemas/timed_scenes.LANGUAGES 하나다 — [2l]이 따로 적지 않는다."""
    assert set(localize_mod.TARGET_LANGUAGES) == set(LANGUAGES) - {PRIMARY_LANGUAGE}


# --- 전제 ---------------------------------------------------------------------


def test_requires_factcheck_done(paths):
    topic = run_topic_stage("한양도성 각자성석", paths=paths, today=TODAY, seed_url=SEED)
    run_draft_stage(topic.slug, llm=FakeLLM(make_script(make_lines())), paths=paths, run_id=topic.run_id)
    with pytest.raises(LocalizeStageError, match="factcheck"):
        run_localize_stage(topic.slug, llm=FakeLLM(), paths=paths, run_id=topic.run_id)


def test_requires_script(paths):
    topic = run_topic_stage("한양도성 각자성석", paths=paths, today=TODAY, seed_url=SEED)
    with pytest.raises(LocalizeStageError, match="대본이 없다"):
        run_localize_stage(topic.slug, llm=FakeLLM(), paths=paths, run_id=topic.run_id)


def test_missing_factcheck_md_is_a_warning_not_a_stop(paths, checked, ko_lines):
    (paths.topic_dir(checked.slug) / "factcheck.md").unlink()
    llm = FakeLLM(good_output(len(ko_lines)))
    result = run_localize_stage(checked.slug, llm=llm, paths=paths, run_id=checked.run_id)

    assert result.passed
    assert any("factcheck.md" in w for w in result.warnings)
    assert "factcheck.md 없음" in llm.prompts[0]


# --- 로케일 엔벨로프 ----------------------------------------------------------


def test_locale_limits_are_applied_when_present(paths, checked, ko_lines, monkeypatch):
    """`locales.ja.limits`가 있으면 그 엔벨로프 — 계약 파일은 건드리지 않고 로드본만 바꾼다."""
    monkeypatch.setitem(vocab.SCRIPT_RULES["locales"], "ja", {"limits": {"total_chars": [1, 5]}})
    assert vocab.locale_limits("ja") == {"total_chars": [1, 5]}

    result = run_localize_stage(
        checked.slug, llm=FakeLLM(good_output(len(ko_lines))), paths=paths, run_id=checked.run_id,
    )
    assert result.outcome("ja").status == FAILED
    assert any("ja:" in e and "total_chars" in e for e in result.errors)
    assert result.outcome("en").status == WRITTEN
    # 블록이 있어도 초는 추정하지 않는다 — 경고는 남되 "블록 없음"은 아니다
    ja_warnings = result.outcome("ja").warnings
    assert any("실측" in w for w in ja_warnings) and not any("블록 없음" in w for w in ja_warnings)


def test_locale_limits_reach_the_prompt(paths, checked, ko_lines, monkeypatch):
    monkeypatch.setitem(
        vocab.SCRIPT_RULES["locales"], "en", {"limits": {"total_chars": [100, 9000]}}
    )
    llm = FakeLLM(good_output(len(ko_lines)))
    run_localize_stage(checked.slug, llm=llm, paths=paths, run_id=checked.run_id)
    assert "locales.en.limits" in llm.prompts[0] and "100~9000" in llm.prompts[0]
