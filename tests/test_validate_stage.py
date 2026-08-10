"""[2. validate] 재생성 루프 계약 검증.

핵심 확인 대상 (specs/05-pipeline.md):
- 후보가 통과하면 재생성 없이 06-script.json으로 승격
- 실패하면 실패 사유를 프롬프트에 피드백해 재생성, 최대 3회
- 상한을 넘으면 중단하고 리포트 (후보 파일은 전부 남긴다)
- 스펙 07의 fix_directives revise 루프(상한 2회)와 섞지 않는다
"""

import json
from datetime import date

import pytest

from conftest import load_fixture
from shorts_factory.llm.base import LLMError
from shorts_factory.llm.fake import FakeLLMClient
from shorts_factory.stages.research import run_research_stage
from shorts_factory.stages.script import run_script_stage
from shorts_factory.stages.topic import run_topic_stage
from shorts_factory.stages.validate import (
    MAX_REGENERATIONS,
    ValidateStageError,
    build_feedback,
    run_validate_stage,
)

TODAY = date(2026, 8, 7)
SLUG = "hanyangdoseong-gakjaseongseok"


def session_output(**overrides) -> str:
    data = load_fixture("script_session_output.json")
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


def broken_output() -> str:
    """씬 배열이 빠진 출력. 검증 실패가 아니라 생성 실패다."""
    return json.dumps({"note": "scenes가 없다"}, ensure_ascii=False)


def short_output() -> str:
    """스키마는 맞지만 분량이 모자라 대본 규칙에서 걸리는 후보."""
    data = load_fixture("script_session_output.json")
    data["scenes"] = data["scenes"][:3]
    return json.dumps(data, ensure_ascii=False)


@pytest.fixture
def with_candidate(paths):
    """[0a]~[1]까지 끝나 05-candidates/01.json이 통과 상태로 있는 프로젝트."""
    run_topic_stage("한양도성 각자성석", paths=paths, today=TODAY)
    sheet = json.dumps(load_fixture("factsheet_pass.json"), ensure_ascii=False)
    run_research_stage(
        SLUG,
        llm=FakeLLMClient(["# 조사", "# 검증", "# 비판", sheet]),
        paths=paths,
    )
    run_script_stage(SLUG, llm=FakeLLMClient([session_output()]), paths=paths)
    return paths


@pytest.fixture
def with_failing_candidate(with_candidate):
    """01.json을 대본 규칙에서 걸리도록 잘라 둔 상태."""
    path = with_candidate.topic_dir(SLUG) / "05-candidates" / "01.json"
    scenes = json.loads(path.read_text(encoding="utf-8"))
    scenes["scenes"] = scenes["scenes"][:3]
    path.write_text(json.dumps(scenes, ensure_ascii=False), encoding="utf-8")
    return with_candidate


# --- 통과 경로 ---------------------------------------------------------------


def test_passing_candidate_is_promoted_without_regenerating(with_candidate):
    llm = FakeLLMClient([])  # 세션을 한 번도 부르면 안 된다
    result = run_validate_stage(SLUG, llm=llm, paths=with_candidate)

    assert result.passed
    assert result.regenerations == 0
    assert llm.calls == []


def test_promoted_script_is_written_to_06_script_json(with_candidate):
    result = run_validate_stage(SLUG, llm=FakeLLMClient([]), paths=with_candidate)

    assert result.script_path == with_candidate.topic_dir(SLUG) / "06-script.json"
    written = json.loads(result.script_path.read_text(encoding="utf-8"))
    assert written == result.scenes
    assert written["scenes"], "06-script.json은 그 자체로 scenes.json이어야 한다"


def test_second_run_skips(with_candidate):
    run_validate_stage(SLUG, llm=FakeLLMClient([]), paths=with_candidate)
    again = run_validate_stage(SLUG, llm=FakeLLMClient([]), paths=with_candidate)
    assert again.skipped and again.passed


# --- 재생성 루프 -------------------------------------------------------------


def test_failing_candidate_triggers_regeneration(with_failing_candidate):
    llm = FakeLLMClient([session_output()])
    result = run_validate_stage(SLUG, llm=llm, paths=with_failing_candidate)

    assert result.passed
    assert result.regenerations == 1
    assert len(llm.calls) == 1


def test_regenerated_candidate_is_written_next_to_the_first(with_failing_candidate):
    run_validate_stage(
        SLUG, llm=FakeLLMClient([session_output()]), paths=with_failing_candidate
    )
    candidates = with_failing_candidate.topic_dir(SLUG) / "05-candidates"
    assert (candidates / "01.json").exists(), "실패한 후보도 지우지 않는다"
    assert (candidates / "02.json").exists()


def test_failure_reasons_are_fed_back_into_the_prompt(with_failing_candidate):
    llm = FakeLLMClient([session_output()])
    result = run_validate_stage(SLUG, llm=llm, paths=with_failing_candidate)

    prompt = llm.calls[0]["prompt"]
    assert "# 재생성 지시" in prompt
    first_attempt_errors = result.attempts[0].errors
    assert first_attempt_errors
    for error in first_attempt_errors:
        assert error in prompt


def test_regeneration_stops_at_the_spec_limit(with_failing_candidate):
    """specs/05 '최대 3회, 초과 시 중단·리포트'."""
    llm = FakeLLMClient([short_output()] * 10)
    result = run_validate_stage(SLUG, llm=llm, paths=with_failing_candidate)

    assert not result.passed
    assert result.regenerations == MAX_REGENERATIONS == 3
    assert len(llm.calls) == 3
    assert not (with_failing_candidate.topic_dir(SLUG) / "06-script.json").exists()


def test_exhausted_loop_keeps_every_candidate_and_reports(with_failing_candidate):
    llm = FakeLLMClient([short_output()] * 10)
    result = run_validate_stage(SLUG, llm=llm, paths=with_failing_candidate)

    candidates = with_failing_candidate.topic_dir(SLUG) / "05-candidates"
    assert sorted(p.name for p in candidates.glob("*.json")) == [
        "01.json", "02.json", "03.json", "04.json",
    ]

    state = json.loads(
        (with_failing_candidate.run_dir(f"20260807-{SLUG}") / "state.json").read_text(
            encoding="utf-8"
        )
    )
    stage = state["stages"]["2-validate"]
    assert stage["status"] == "failed"
    assert stage["regenerations"] == 3
    assert len(stage["report"]) == 4, "첫 후보 + 재생성 3회가 리포트에 남는다"
    assert result.errors


def test_generation_failure_does_not_burn_the_whole_budget(with_failing_candidate):
    """세션이 씬을 못 내놓는 것은 검증 실패와 다르다. 남은 횟수로 계속 간다."""
    llm = FakeLLMClient([broken_output(), session_output()])
    result = run_validate_stage(SLUG, llm=llm, paths=with_failing_candidate)

    assert result.passed
    assert result.regenerations == 2
    assert result.attempts[1].generation_error
    assert result.attempts[1].errors == result.attempts[0].errors


def test_llm_error_propagates(with_failing_candidate):
    """전송 실패는 어댑터가 재시도할 몫이지 이 루프가 삼킬 것이 아니다."""
    with pytest.raises(LLMError):
        run_validate_stage(
            SLUG,
            llm=FakeLLMClient([LLMError("한도 초과")]),
            paths=with_failing_candidate,
        )


# --- 입력 계약 ---------------------------------------------------------------


def test_missing_candidate_is_an_error(with_candidate):
    (with_candidate.topic_dir(SLUG) / "05-candidates" / "01.json").unlink()
    with pytest.raises(ValidateStageError, match=r"\[1\. script\]"):
        run_validate_stage(SLUG, llm=FakeLLMClient([]), paths=with_candidate)


def test_feedback_carries_reasons_only():
    """직전 대본은 넣지 않는다 — revise 루프(스펙 07)와 구분되는 지점이다."""
    text = build_feedback(["[대본규칙] text: 총 594자 (범위 545~575자)"])
    assert "594자" in text
    assert "재생성" in text
