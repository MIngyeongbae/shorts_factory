"""[1. draft]·[2. factcheck] 단계 계약 (ADR-0049).

세션은 페이크다 — 여기서 검증하는 것은 배선이다: 프롬프트에 시드가 실리는가,
산출물이 파일로 남는가, 기계 검사가 실패를 막되 파일은 남기는가 (ADR-0044).
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from shorts_factory.stages.draft import DraftStageError, run_draft_stage
from shorts_factory.stages.factcheck import FactcheckStageError, run_factcheck_stage
from shorts_factory.stages.topic import run_topic_stage

from test_scriptmd import make_lines, make_script

TODAY = date(2026, 8, 7)
SEED = "https://ko.wikipedia.org/wiki/각자성석"


class FakeLLM:
    def __init__(self, text: str):
        self.text = text
        self.prompts: list[str] = []
        self.tools: list[tuple[str, ...]] = []

    def run(self, prompt, *, allowed_tools=(), timeout=None, label="", **_kw):
        self.prompts.append(prompt)
        self.tools.append(tuple(allowed_tools))
        return SimpleNamespace(text=self.text)


@pytest.fixture
def seeded(paths):
    result = run_topic_stage("한양도성 각자성석", paths=paths, today=TODAY, seed_url=SEED)
    return result


def test_draft_writes_script_and_passes(paths, seeded):
    llm = FakeLLM(make_script(make_lines()))
    result = run_draft_stage(seeded.slug, llm=llm, paths=paths, run_id=seeded.run_id)

    assert result.passed and result.script_path.is_file()
    assert result.script_path.read_text(encoding="utf-8").startswith("# 테스트 소재")
    # 시드와 웹 도구가 세션에 실렸다
    assert SEED in llm.prompts[0]
    assert "WebSearch" in llm.tools[0] and "WebFetch" in llm.tools[0]


def test_draft_failure_reports_but_keeps_the_file(paths, seeded):
    """검증 실패는 보고·중단이되 산출물은 남긴다 — 사람이 읽는다 (ADR-0044·0049)."""
    llm = FakeLLM(make_script(make_lines(count=5)))
    result = run_draft_stage(seeded.slug, llm=llm, paths=paths, run_id=seeded.run_id)

    assert not result.passed and result.errors
    assert result.script_path.is_file()


def test_draft_unfit_rejection(paths, seeded):
    text = "# 소재\n\n- 시드: x\n- 매체 적합성: 부적합 — 추상 개념\n\n## 반려\n\n삽화뿐.\n"
    result = run_draft_stage(seeded.slug, llm=FakeLLM(text), paths=paths, run_id=seeded.run_id)

    assert result.unfit and not result.passed


def test_draft_without_seed_url_raises(paths):
    topic = run_topic_stage("미완성 소재", paths=paths, today=TODAY)
    with pytest.raises(DraftStageError):
        run_draft_stage(topic.slug, llm=FakeLLM("# x\n"), paths=paths, run_id=topic.run_id)


def test_draft_rejects_non_markdown_output(paths, seeded):
    with pytest.raises(DraftStageError):
        run_draft_stage(seeded.slug, llm=FakeLLM("대본이 아니라 잡담"),
                        paths=paths, run_id=seeded.run_id)


def _drafted(paths, seeded) -> None:
    run_draft_stage(seeded.slug, llm=FakeLLM(make_script(make_lines())),
                    paths=paths, run_id=seeded.run_id)


def test_factcheck_writes_report_and_updates_script(paths, seeded):
    _drafted(paths, seeded)
    corrected = make_script(make_lines()).replace("# 테스트 소재", "# 테스트 소재 (정정)")
    llm = FakeLLM(f"=== FACTCHECK ===\n# 팩트체크\n표\n=== SCRIPT ===\n{corrected}")
    result = run_factcheck_stage(seeded.slug, llm=llm, paths=paths, run_id=seeded.run_id)

    assert result.passed and result.script_changed
    assert result.factcheck_path.read_text(encoding="utf-8").startswith("# 팩트체크")
    script = (paths.topic_dir(seeded.slug) / "script.md").read_text(encoding="utf-8")
    assert "(정정)" in script
    # 대본 전문이 세션에 실렸다
    assert "## 대본" in llm.prompts[0]


def test_factcheck_without_script_section_keeps_script(paths, seeded):
    _drafted(paths, seeded)
    before = (paths.topic_dir(seeded.slug) / "script.md").read_text(encoding="utf-8")
    llm = FakeLLM("=== FACTCHECK ===\n# 팩트체크\n표만 있다\n")
    result = run_factcheck_stage(seeded.slug, llm=llm, paths=paths, run_id=seeded.run_id)

    assert result.passed and not result.script_changed
    assert any("무변경" in w for w in result.warnings)
    after = (paths.topic_dir(seeded.slug) / "script.md").read_text(encoding="utf-8")
    assert after == before


def test_factcheck_correction_must_keep_the_envelope(paths, seeded):
    """정정본도 같은 기계 검사를 통과해야 한다. 실패해도 파일은 남는다."""
    _drafted(paths, seeded)
    broken = make_script(make_lines(count=5))
    llm = FakeLLM(f"=== FACTCHECK ===\n# 팩트체크\n표\n=== SCRIPT ===\n{broken}")
    result = run_factcheck_stage(seeded.slug, llm=llm, paths=paths, run_id=seeded.run_id)

    assert not result.passed and result.errors
    assert result.factcheck_path.is_file()


def test_factcheck_requires_script(paths, seeded):
    with pytest.raises(FactcheckStageError):
        run_factcheck_stage(seeded.slug, llm=FakeLLM("x"), paths=paths, run_id=seeded.run_id)
