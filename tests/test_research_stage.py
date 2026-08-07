"""[0b. research] 단계 계약 검증.

핵심 확인 대상:
- 4개 독립 세션의 실행 순서와 산출물 (specs/05)
- ADR-0009 오염 방지: 비판(03)에는 검증(02)의 결론이 아니라 조사(01)의 원자료가 간다
- verdict: fail → 백로그 반려 (specs/05 단계별 규칙)
- 팩트시트 계약 위반 시 재생성 루프
"""

import json
from datetime import date

import pytest

from conftest import load_fixture
from shorts_factory.backlog import parse_backlog
from shorts_factory.llm.base import LLMError
from shorts_factory.llm.fake import FakeLLMClient
from shorts_factory.stages import status as status_mod
from shorts_factory.stages.research import ResearchStageError, run_research_stage
from shorts_factory.stages.topic import run_topic_stage

TODAY = date(2026, 8, 7)
SLUG = "hanyangdoseong-gakjaseongseok"

RESEARCH_MD = "# 조사: 한양도성 각자성석\n\nRESEARCH_MARKER 수집한 원자료 본문"
VERIFY_MD = "# 검증: 한양도성 각자성석\n\nVERIFY_MARKER 검증관의 판정 결론"
CRITIQUE_MD = "# 비판: 한양도성 각자성석\n\nCRITIQUE_MARKER 비평가의 판정 결론"


def _sheet(name: str, **overrides) -> str:
    data = load_fixture(name)
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


def _responses(sheet: str | None = None) -> list[str]:
    return [RESEARCH_MD, VERIFY_MD, CRITIQUE_MD, sheet or _sheet("factsheet_pass.json")]


@pytest.fixture
def prepared(paths):
    """[0a]까지 끝난 상태."""
    run_topic_stage("한양도성 각자성석", paths=paths, today=TODAY)
    return paths


# --- 산출물 -------------------------------------------------------------


def test_writes_all_four_artifacts(prepared):
    llm = FakeLLMClient(_responses())
    result = run_research_stage(SLUG, llm=llm, paths=prepared)

    topic_dir = result.topic_dir
    assert (topic_dir / "01-research.md").read_text(encoding="utf-8").strip() == RESEARCH_MD
    assert (topic_dir / "02-verify.md").read_text(encoding="utf-8").strip() == VERIFY_MD
    assert (topic_dir / "03-critique.md").read_text(encoding="utf-8").strip() == CRITIQUE_MD
    assert (topic_dir / "04-factsheet.json").is_file()
    assert result.passed


def test_factsheet_is_preserved_in_run_dir(prepared):
    """specs/06: 팩트시트는 runs/{run_id}/research.json으로 보존."""
    llm = FakeLLMClient(_responses())
    result = run_research_stage(SLUG, llm=llm, paths=prepared)

    preserved = json.loads((result.run_dir / "research.json").read_text(encoding="utf-8"))
    package = json.loads((result.topic_dir / "04-factsheet.json").read_text(encoding="utf-8"))
    assert preserved == package


def test_sessions_run_in_spec_order(prepared):
    llm = FakeLLMClient(_responses())
    run_research_stage(SLUG, llm=llm, paths=prepared)
    assert [c["label"] for c in llm.calls] == [
        "01-research", "02-verify", "03-critique", "04-factsheet.try1",
    ]


# --- ADR-0009 오염 방지 --------------------------------------------------


def test_critique_receives_raw_research_not_verify_conclusions(prepared):
    """비판 세션에 검증의 결론이 새어 들어가면 자기 검증 오염이다 (ADR-0009)."""
    llm = FakeLLMClient(_responses())
    run_research_stage(SLUG, llm=llm, paths=prepared)

    critique_prompt = llm.calls[2]["prompt"]
    assert "RESEARCH_MARKER" in critique_prompt
    assert "VERIFY_MARKER" not in critique_prompt


def test_verify_receives_only_research(prepared):
    llm = FakeLLMClient(_responses())
    run_research_stage(SLUG, llm=llm, paths=prepared)

    verify_prompt = llm.calls[1]["prompt"]
    assert "RESEARCH_MARKER" in verify_prompt
    assert "CRITIQUE_MARKER" not in verify_prompt


def test_factsheet_receives_all_three(prepared):
    llm = FakeLLMClient(_responses())
    run_research_stage(SLUG, llm=llm, paths=prepared)

    prompt = llm.calls[3]["prompt"]
    assert "RESEARCH_MARKER" in prompt
    assert "VERIFY_MARKER" in prompt
    assert "CRITIQUE_MARKER" in prompt


def test_web_tools_granted_only_to_collecting_sessions(prepared):
    llm = FakeLLMClient(_responses())
    run_research_stage(SLUG, llm=llm, paths=prepared)

    assert llm.calls[0]["allowed_tools"] == ("WebSearch", "WebFetch")
    assert llm.calls[1]["allowed_tools"] == ("WebSearch", "WebFetch")
    assert llm.calls[2]["allowed_tools"] == ("WebSearch", "WebFetch")
    # 종합 단계는 새 사실을 찾으면 안 되므로 도구를 주지 않는다
    assert llm.calls[3]["allowed_tools"] == ()


# --- verdict 분기 --------------------------------------------------------


def test_pass_leaves_status_pending_for_human(prepared):
    """[0b] 통과는 go가 아니다. go는 사람만 기록한다 (ADR-0009)."""
    llm = FakeLLMClient(_responses())
    result = run_research_stage(SLUG, llm=llm, paths=prepared)

    assert status_mod.read_status(result.topic_dir / "STATUS.md") == status_mod.PENDING
    assert parse_backlog(prepared.backlog)[0].status == "리서치중"


def test_fail_rejects_backlog_and_writes_no_go(prepared):
    """specs/05: verdict fail 시 백로그 반려하고 종료."""
    llm = FakeLLMClient(_responses(_sheet("factsheet_fail.json")))
    result = run_research_stage(SLUG, llm=llm, paths=prepared)

    assert result.verdict == "fail"
    assert not result.passed
    assert status_mod.read_status(result.topic_dir / "STATUS.md") == status_mod.NO_GO
    assert parse_backlog(prepared.backlog)[0].status == "반려"

    state = json.loads((result.run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["stages"]["0b-research"]["status"] == "blocked"


def test_fail_reason_is_recorded(prepared):
    llm = FakeLLMClient(_responses(_sheet("factsheet_fail.json")))
    result = run_research_stage(SLUG, llm=llm, paths=prepared)
    status_text = (result.topic_dir / "STATUS.md").read_text(encoding="utf-8")
    assert "실패한 대안" in status_text


def test_single_turn_session_warns_about_missing_search(prepared):
    """웹 도구를 줬는데 1턴에 끝났으면 검색 없이 기억으로 답했다는 신호다 (ADR-0007)."""
    llm = FakeLLMClient(_responses(), num_turns=1)
    result = run_research_stage(SLUG, llm=llm, paths=prepared)

    assert result.passed  # 차단하지는 않는다
    assert any("01-research" in w and "검색" in w for w in result.warnings)


def test_normal_session_does_not_warn_about_search(prepared):
    llm = FakeLLMClient(_responses(), num_turns=6)
    result = run_research_stage(SLUG, llm=llm, paths=prepared)
    assert not any("검색" in w for w in result.warnings)


def test_low_confidence_surfaces_as_warning(prepared):
    llm = FakeLLMClient(_responses(_sheet("factsheet_fail.json")))
    result = run_research_stage(SLUG, llm=llm, paths=prepared)
    assert any("low" in w for w in result.warnings)


# --- 팩트시트 재생성 루프 -------------------------------------------------


def test_retries_until_contract_is_satisfied(prepared):
    broken_json = "JSON이 아닌 설명 문장입니다."
    # 4조건 충족인데 verdict가 fail → semantic 위반
    contract_violation = _sheet("factsheet_pass.json", verdict="fail")

    llm = FakeLLMClient(
        [RESEARCH_MD, VERIFY_MD, CRITIQUE_MD,
         broken_json, contract_violation, _sheet("factsheet_pass.json")]
    )
    result = run_research_stage(SLUG, llm=llm, paths=prepared)

    assert result.passed
    assert [c["label"] for c in llm.calls[3:]] == [
        "04-factsheet.try1", "04-factsheet.try2", "04-factsheet.try3",
    ]
    # 재시도 프롬프트에 실패 사유가 주입돼야 한다
    assert "재생성 지시" in llm.calls[5]["prompt"]


def test_gives_up_after_max_attempts(prepared):
    violation = _sheet("factsheet_pass.json", verdict="fail")
    llm = FakeLLMClient([RESEARCH_MD, VERIFY_MD, CRITIQUE_MD] + [violation] * 3)

    with pytest.raises(ResearchStageError, match="계약"):
        run_research_stage(SLUG, llm=llm, paths=prepared)

    state = json.loads(
        (prepared.run_dir(f"20260807-{SLUG}") / "state.json").read_text(encoding="utf-8")
    )
    assert state["stages"]["0b-research"]["status"] == "failed"


def test_topic_field_is_pinned_to_contract(prepared):
    """세션이 소재명을 흔들어도 계약 값으로 고정한다."""
    llm = FakeLLMClient(_responses(_sheet("factsheet_pass.json", topic="각자성석(한양도성)")))
    result = run_research_stage(SLUG, llm=llm, paths=prepared)
    assert result.factsheet["topic"] == "한양도성 각자성석"


# --- 재시작 -------------------------------------------------------------


def test_rerun_skips_completed_stage_without_calling_llm(prepared):
    run_research_stage(SLUG, llm=FakeLLMClient(_responses()), paths=prepared)

    exhausted = FakeLLMClient([])  # 호출되면 예외가 난다
    result = run_research_stage(SLUG, llm=exhausted, paths=prepared)

    assert result.passed
    assert exhausted.calls == []


def test_partial_resume_reuses_existing_artifacts(prepared):
    """중간에 끊긴 run은 남은 서브스텝만 이어서 실행한다 (specs/05 실패 정책)."""
    # 03까지 끝난 뒤 04에서 세션이 죽은 상황을 만든다
    with pytest.raises(LLMError):
        run_research_stage(
            SLUG,
            llm=FakeLLMClient([RESEARCH_MD, VERIFY_MD, CRITIQUE_MD]),
            paths=prepared,
        )

    state_path = prepared.run_dir(f"20260807-{SLUG}") / "state.json"
    assert json.loads(state_path.read_text(encoding="utf-8"))["stages"]["0b-research"][
        "status"
    ] == "failed"

    resumed = FakeLLMClient([_sheet("factsheet_pass.json")])
    result = run_research_stage(SLUG, llm=resumed, paths=prepared)

    assert result.passed
    assert result.skipped == ["01-research", "02-verify", "03-critique"]
    assert len(resumed.calls) == 1


def test_only_runs_single_substep(prepared):
    llm = FakeLLMClient([RESEARCH_MD])
    result = run_research_stage(SLUG, llm=llm, paths=prepared, only="01-research")

    assert result.verdict is None
    assert result.executed == ["01-research"]
    assert (result.topic_dir / "01-research.md").is_file()
    assert not (result.topic_dir / "02-verify.md").exists()


def test_unknown_substep_raises(prepared):
    with pytest.raises(ResearchStageError, match="서브스텝"):
        run_research_stage(SLUG, llm=FakeLLMClient([]), paths=prepared, only="99-nope")


def test_missing_run_raises(paths):
    with pytest.raises(ResearchStageError, match="0a"):
        run_research_stage("없는-슬러그", llm=FakeLLMClient([]), paths=paths)
