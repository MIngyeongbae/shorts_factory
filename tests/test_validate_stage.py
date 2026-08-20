"""[2. validate] 최종 게이트 계약 검증 (ADR-0044).

핵심 확인 대상 (specs/05-pipeline.md):
- 선발본이 통과하면 06-script.json으로 확정
- **재생성하지 않는다** — 실패는 보고·중단이고 LLM 세션이 아예 없다
- 실패해도 선발본을 덮지 않고, 사유를 state에 남긴다
- 오류는 각 단계 직후 검증(`[1s]`·`[1w]`)이 잡는 것이 전제다 — 그 계약은
  test_script_stages.py가 본다
"""

import json
from datetime import date

import pytest

from conftest import load_fixture
from shorts_factory.config import write_text
from shorts_factory.jsonio import dump_json
from shorts_factory.llm.fake import FakeLLMClient
from shorts_factory.stages.research import run_research_stage
from shorts_factory.stages.topic import run_topic_stage
from shorts_factory.stages.validate import (
    ValidateStageError,
    run_validate_stage,
)
from shorts_factory.stages.write import build_scenes

TODAY = date(2026, 8, 13)
TOPIC = "후버댐 콘크리트 냉각"
SLUG = "hubeodaem-konkeuriteu-naenggak"
RUN_ID = f"20260813-{SLUG}"


def candidate(overrides: dict | None = None) -> dict:
    """계획 + 세션 문장으로 만든 대본 후보."""
    plan = load_fixture("sceneplan_pass.json")
    texts = {s["scene_id"]: s["text"] for s in load_fixture("write_session.json")["scenes"]}
    texts.update(overrides or {})
    return build_scenes(plan, texts, run_id=RUN_ID, topic=TOPIC)


@pytest.fixture
def with_candidate(paths):
    """[0a]~[1b]까지 끝나 06-script.json이 통과 상태로 선발돼 있는 프로젝트."""
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
    write_text(topic_dir / "05-candidates" / "01.json", dump_json(candidate()))
    write_text(topic_dir / "06-script.json", dump_json(candidate()))
    return paths


@pytest.fixture
def with_failing_candidate(with_candidate):
    """선발본을 분량에서 걸리도록 줄여 둔 상태."""
    path = with_candidate.topic_dir(SLUG) / "06-script.json"
    scenes = json.loads(path.read_text(encoding="utf-8"))
    for scene in scenes["scenes"]:
        scene["text"] = "짧은 줄입니다."
    path.write_text(json.dumps(scenes, ensure_ascii=False), encoding="utf-8")
    return with_candidate


# --- 통과 경로 ---------------------------------------------------------------


def test_passing_candidate_is_promoted(with_candidate):
    result = run_validate_stage(SLUG, paths=with_candidate)
    assert result.passed
    assert not result.errors


def test_promoted_script_is_written_to_06_script_json(with_candidate):
    result = run_validate_stage(SLUG, paths=with_candidate)

    assert result.script_path == with_candidate.topic_dir(SLUG) / "06-script.json"
    written = json.loads(result.script_path.read_text(encoding="utf-8"))
    assert written == result.scenes
    assert written["scenes"], "06-script.json은 그 자체로 scenes.json이어야 한다"


def test_second_run_skips(with_candidate):
    run_validate_stage(SLUG, paths=with_candidate)
    again = run_validate_stage(SLUG, paths=with_candidate)
    assert again.skipped and again.passed


# --- 실패 = 보고·중단 (ADR-0044) ----------------------------------------------


def test_failure_stops_without_regenerating(with_failing_candidate):
    """재생성 루프는 없다 — run_validate_stage는 LLM 인자 자체를 받지 않는다."""
    result = run_validate_stage(SLUG, paths=with_failing_candidate)

    assert not result.passed
    assert result.errors, "실패 사유가 보고에 남아야 한다"


def test_failure_does_not_touch_the_selected_script(with_failing_candidate):
    """실패해도 선발본을 덮지 않는다 — 다시 만들지는 사람이 정한다."""
    selected = with_failing_candidate.topic_dir(SLUG) / "06-script.json"
    before = selected.read_text(encoding="utf-8")

    run_validate_stage(SLUG, paths=with_failing_candidate)

    assert selected.read_text(encoding="utf-8") == before


def test_failure_is_recorded_in_state(with_failing_candidate):
    result = run_validate_stage(SLUG, paths=with_failing_candidate)

    state = json.loads(
        (with_failing_candidate.run_dir(RUN_ID) / "state.json").read_text(encoding="utf-8")
    )
    stage = state["stages"]["2-validate"]
    assert stage["status"] == "failed"
    assert stage["validation_errors"] == result.errors
    assert "재생성하지 않는다" in stage["error"]


def test_grounding_violation_fails_the_gate(with_candidate):
    """팩트시트 밖 숫자는 최종 게이트에서도 잡힌다 (ADR-0007)."""
    path = with_candidate.topic_dir(SLUG) / "06-script.json"
    broken = candidate({3: "높이는 999미터, 바닥 두께가 200미터입니다."})
    path.write_text(dump_json(broken), encoding="utf-8")

    result = run_validate_stage(SLUG, paths=with_candidate)

    assert not result.passed
    assert any("[그라운딩]" in error for error in result.errors)


# --- 입력 계약 ---------------------------------------------------------------


def test_missing_selection_is_an_error(with_candidate):
    """[2]의 입력은 `[1b]`가 선발한 것이다 (ADR-0040)."""
    (with_candidate.topic_dir(SLUG) / "06-script.json").unlink()
    with pytest.raises(ValidateStageError, match=r"\[1b\. score\]"):
        run_validate_stage(SLUG, paths=with_candidate)


def test_the_gate_does_not_need_the_plan_files(with_candidate):
    """재생성이 없으므로 구성안·씬 계획 없이도 돈다 — 검증에 그 파일이 필요 없다."""
    topic_dir = with_candidate.topic_dir(SLUG)
    for name in ("07-outline.json", "08-sceneplan.json"):
        target = topic_dir / name
        if target.exists():
            target.unlink()

    result = run_validate_stage(SLUG, paths=with_candidate)
    assert result.passed
