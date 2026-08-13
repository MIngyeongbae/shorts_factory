"""[2. validate] 재생성 루프 계약 검증.

핵심 확인 대상 (specs/05-pipeline.md, ADR-0029):
- 후보가 통과하면 재생성 없이 06-script.json으로 승격
- 실패하면 실패 사유를 프롬프트에 피드백해 재생성, 최대 3회
- **재진입 지점이 실패 종류를 따라간다** — 분량이 틀렸다고 그림 계획까지 버리지 않는다
- 상한을 넘으면 중단하고 리포트 (후보 파일은 전부 남긴다)
- 스펙 07의 fix_directives revise 루프(상한 2회)와 섞지 않는다
"""

import json
from datetime import date

import pytest

from conftest import load_fixture
from shorts_factory.config import write_text
from shorts_factory.jsonio import dump_json
from shorts_factory.llm.base import LLMError
from shorts_factory.llm.fake import FakeLLMClient
from shorts_factory.stages.research import run_research_stage
from shorts_factory.stages.topic import run_topic_stage
from shorts_factory.stages.validate import (
    MAX_REGENERATIONS,
    OUTLINE,
    SCENEPLAN,
    WRITE,
    ValidateStageError,
    build_feedback,
    reentry_for,
    run_validate_stage,
)
from shorts_factory.stages.write import build_scenes

TODAY = date(2026, 8, 13)
TOPIC = "후버댐 콘크리트 냉각"
SLUG = "hubeodaem-konkeuriteu-naenggak"
RUN_ID = f"20260813-{SLUG}"


def outline_output() -> str:
    return json.dumps(load_fixture("outline_pass.json"), ensure_ascii=False)


def sceneplan_output() -> str:
    return json.dumps(load_fixture("sceneplan_pass.json"), ensure_ascii=False)


def write_output(**overrides) -> str:
    data = load_fixture("write_session.json")
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


def short_write_output() -> str:
    """씬은 다 채우되 문장을 짧게 — 분량에서만 걸리는 후보 (= `[1w]` 재진입)."""
    data = load_fixture("write_session.json")
    for scene in data["scenes"]:
        scene["text"] = "짧은 줄입니다."
    return json.dumps(data, ensure_ascii=False)


def broken_output() -> str:
    """씬 배열이 빠진 출력. 검증 실패가 아니라 생성 실패다."""
    return json.dumps({"note": "scenes가 없다"}, ensure_ascii=False)


def candidate(overrides: dict | None = None) -> dict:
    """계획 + 세션 문장으로 만든 대본 후보."""
    plan = load_fixture("sceneplan_pass.json")
    texts = {s["scene_id"]: s["text"] for s in load_fixture("write_session.json")["scenes"]}
    texts.update(overrides or {})
    return build_scenes(plan, texts, run_id=RUN_ID, topic=TOPIC)


@pytest.fixture
def with_candidate(paths):
    """[0a]~[1w]까지 끝나 05-candidates/01.json이 통과 상태로 있는 프로젝트."""
    backlog = paths.root / "topics" / "backlog.md"
    backlog.write_text(
        backlog.read_text(encoding="utf-8")
        + f"| {TOPIC} | ✅ | ✅ | ✅ | ✅ | 개척국 기술보고서 | 후보 |\n",
        encoding="utf-8",
    )
    run_topic_stage(TOPIC, paths=paths, today=TODAY)
    sheet = json.dumps(load_fixture("factsheet_hoover.json"), ensure_ascii=False)
    run_research_stage(
        SLUG,
        llm=FakeLLMClient(["# 조사", "# 검증", "# 비판", sheet]),
        paths=paths,
    )
    topic_dir = paths.topic_dir(SLUG)
    write_text(topic_dir / "07-outline.json", outline_output())
    write_text(topic_dir / "08-sceneplan.json", sceneplan_output())
    write_text(topic_dir / "05-candidates" / "01.json", dump_json(candidate()))
    return paths


@pytest.fixture
def with_failing_candidate(with_candidate):
    """01.json을 분량에서 걸리도록 줄여 둔 상태 — `[1w]`만 다시 돌면 되는 실패다."""
    path = with_candidate.topic_dir(SLUG) / "05-candidates" / "01.json"
    scenes = json.loads(path.read_text(encoding="utf-8"))
    for scene in scenes["scenes"]:
        scene["text"] = "짧은 줄입니다."
    path.write_text(json.dumps(scenes, ensure_ascii=False), encoding="utf-8")
    return with_candidate


# --- 재진입 판단 (ADR-0029) ---------------------------------------------------


@pytest.mark.parametrize(
    "error, expected",
    [
        ("[대본규칙] text: 총 463자 (범위 545~575자)", WRITE),
        ("[대본규칙] scenes/3: 51자 (줄당 최대 43자)", WRITE),
        ("[스키마] scenes/2: 'text' is a required property", WRITE),
        ("[대본규칙] scenes: 자막 12줄 (범위 23~28줄)", SCENEPLAN),
        ("[스키마] scenes/4: visual_goal이 text와 82% 겹친다", SCENEPLAN),
        ("[스키마] scenes/1/camera: 'zoom' is not one of [...]", SCENEPLAN),
        ("[그라운딩] 팩트시트에 없는 숫자: 400미터", OUTLINE),
        ("[대본규칙] text: 엔딩이 훅의 명사를 하나도 재사용하지 않는다 (수미상관 실패)", OUTLINE),
        ("[알수없음] 처음 보는 오류", OUTLINE),
    ],
)
def test_reentry_point_follows_the_failure(error, expected):
    assert reentry_for([error]) == expected


def test_the_earliest_reentry_point_wins():
    """하나라도 [1a]짜리면 전부 다시 돈다 — 뒤 단계만 고쳐서는 답이 안 나온다."""
    assert reentry_for(
        ["[대본규칙] text: 총 463자 (범위 545~575자)", "[그라운딩] 팩트시트에 없는 숫자: 400미터"]
    ) == OUTLINE


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


def test_length_failure_only_rewrites_the_sentences(with_failing_candidate):
    """분량 실패는 `[1w]`만 다시 돈다. **그림 계획은 세션을 거치지 않는다.**"""
    llm = FakeLLMClient([write_output()])
    result = run_validate_stage(SLUG, llm=llm, paths=with_failing_candidate)

    assert result.passed
    assert result.regenerations == 1
    assert len(llm.calls) == 1, "[1a]·[1s]는 부르지 않는다"
    assert result.attempts[1].reentry == WRITE

    plan = json.loads(
        (with_failing_candidate.topic_dir(SLUG) / "08-sceneplan.json").read_text(
            encoding="utf-8"
        )
    )
    assert plan == load_fixture("sceneplan_pass.json"), "씬 계획은 손대지 않는다"


def test_grounding_failure_rewinds_to_the_outline(with_candidate):
    """그라운딩 위반은 사실 선택이 틀린 것이라 각도부터 다시 고른다."""
    path = with_candidate.topic_dir(SLUG) / "05-candidates" / "01.json"
    broken = candidate({3: "높이는 999미터, 바닥 두께가 200미터입니다."})
    path.write_text(dump_json(broken), encoding="utf-8")

    llm = FakeLLMClient([outline_output(), sceneplan_output(), write_output()])
    result = run_validate_stage(SLUG, llm=llm, paths=with_candidate)

    assert result.passed
    assert result.attempts[1].reentry == OUTLINE
    assert [call["label"] for call in llm.calls] == [
        "2-validate.regen1.1a", "2-validate.regen1.1s", "2-validate.regen1.1w",
    ]


def test_regenerated_candidate_is_written_next_to_the_first(with_failing_candidate):
    run_validate_stage(
        SLUG, llm=FakeLLMClient([write_output()]), paths=with_failing_candidate
    )
    candidates = with_failing_candidate.topic_dir(SLUG) / "05-candidates"
    assert (candidates / "01.json").exists(), "실패한 후보도 지우지 않는다"
    assert (candidates / "02.json").exists()


def test_failure_reasons_are_fed_back_into_the_prompt(with_failing_candidate):
    llm = FakeLLMClient([write_output()])
    result = run_validate_stage(SLUG, llm=llm, paths=with_failing_candidate)

    prompt = llm.calls[0]["prompt"]
    assert "# 재생성 지시" in prompt
    first_attempt_errors = result.attempts[0].errors
    assert first_attempt_errors
    for error in first_attempt_errors:
        assert error in prompt


def test_regeneration_stops_at_the_spec_limit(with_failing_candidate):
    """specs/05 '최대 3회, 초과 시 중단·리포트'."""
    llm = FakeLLMClient([short_write_output()] * 10)
    result = run_validate_stage(SLUG, llm=llm, paths=with_failing_candidate)

    assert not result.passed
    assert result.regenerations == MAX_REGENERATIONS == 3
    assert len(llm.calls) == 3
    assert not (with_failing_candidate.topic_dir(SLUG) / "06-script.json").exists()


def test_exhausted_loop_keeps_every_candidate_and_reports(with_failing_candidate):
    llm = FakeLLMClient([short_write_output()] * 10)
    result = run_validate_stage(SLUG, llm=llm, paths=with_failing_candidate)

    candidates = with_failing_candidate.topic_dir(SLUG) / "05-candidates"
    assert sorted(p.name for p in candidates.glob("*.json")) == [
        "01.json", "02.json", "03.json", "04.json",
    ]

    state = json.loads(
        (with_failing_candidate.run_dir(RUN_ID) / "state.json").read_text(encoding="utf-8")
    )
    stage = state["stages"]["2-validate"]
    assert stage["status"] == "failed"
    assert stage["regenerations"] == 3
    assert len(stage["report"]) == 4, "첫 후보 + 재생성 3회가 리포트에 남는다"
    assert all(item["reentry"] == WRITE for item in stage["report"][1:])
    assert result.errors


def test_generation_failure_does_not_burn_the_whole_budget(with_failing_candidate):
    """세션이 씬을 못 내놓는 것은 검증 실패와 다르다. 남은 횟수로 계속 간다."""
    llm = FakeLLMClient([broken_output(), write_output()])
    result = run_validate_stage(SLUG, llm=llm, paths=with_failing_candidate)

    assert result.passed
    assert result.regenerations == 2
    assert result.attempts[1].generation_error
    assert result.attempts[1].errors == result.attempts[0].errors


def test_a_broken_regenerated_plan_is_thrown_away(with_candidate):
    """되돌아가서 만든 씬 계획이 제 계약을 어기면 그 시도를 버린다."""
    path = with_candidate.topic_dir(SLUG) / "05-candidates" / "01.json"
    path.write_text(
        dump_json(candidate({3: "높이는 999미터, 바닥 두께가 200미터입니다."})),
        encoding="utf-8",
    )
    plan = load_fixture("sceneplan_pass.json")
    plan["scenes"] = plan["scenes"][:5]  # 씬 수가 범위 밖이다

    llm = FakeLLMClient([
        outline_output(), json.dumps(plan, ensure_ascii=False),
        outline_output(), sceneplan_output(), write_output(),
    ])
    result = run_validate_stage(SLUG, llm=llm, paths=with_candidate)

    assert result.passed
    assert result.attempts[1].generation_error
    assert "08-sceneplan.json" in result.attempts[1].generation_error


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
    with pytest.raises(ValidateStageError, match=r"\[1w\. write\]"):
        run_validate_stage(SLUG, llm=FakeLLMClient([]), paths=with_candidate)


def test_a_passing_candidate_does_not_need_the_plan_files(with_candidate):
    """[1a]·[1s] 이전에 만든 대본이 아직 있다 — 백필하지 않기로 했다 (ADR-0029)."""
    topic_dir = with_candidate.topic_dir(SLUG)
    (topic_dir / "07-outline.json").unlink()
    (topic_dir / "08-sceneplan.json").unlink()

    result = run_validate_stage(SLUG, llm=FakeLLMClient([]), paths=with_candidate)
    assert result.passed


def test_regeneration_without_the_plan_files_is_an_error(with_failing_candidate):
    (with_failing_candidate.topic_dir(SLUG) / "08-sceneplan.json").unlink()
    with pytest.raises(ValidateStageError, match="08-sceneplan.json"):
        run_validate_stage(
            SLUG, llm=FakeLLMClient([write_output()]), paths=with_failing_candidate
        )


def test_feedback_carries_reasons_only():
    """직전 대본은 넣지 않는다 — revise 루프(스펙 07)와 구분되는 지점이다."""
    text = build_feedback(["[대본규칙] text: 총 594자 (범위 545~575자)"])
    assert "594자" in text
    assert "재생성" in text
