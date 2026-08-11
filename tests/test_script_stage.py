"""[1. script] 단계 계약 검증.

핵심 확인 대상:
- 세션은 beat/text/subject만 내고 나머지는 룰 테이블이 채운다 (ADR-0001, specs/03)
- 팩트시트 verdict가 pass가 아니면 진입 금지 (specs/06)
- confidence=low 사실은 프롬프트에 아예 주입되지 않는다 (specs/06)
- 검증 실패해도 후보는 남긴다 — 재생성은 [2. validate] 소관 (specs/05)
"""

import json
from datetime import date

import pytest

from conftest import load_fixture
from shorts_factory.llm.fake import FakeLLMClient
from shorts_factory.stages.research import run_research_stage
from shorts_factory.stages.script import (
    NOMINAL_SPEED,
    ScriptStageError,
    build_scenes,
    groundable_factsheet,
    run_script_stage,
)
from shorts_factory.stages.topic import run_topic_stage

TODAY = date(2026, 8, 7)
SLUG = "hanyangdoseong-gakjaseongseok"

RESEARCH_MD = "# 조사\n\n원자료"
VERIFY_MD = "# 검증\n\n판정"
CRITIQUE_MD = "# 비판\n\n판정"


def session_output(**overrides) -> str:
    """헤드리스 세션이 낼 JSON 텍스트."""
    data = load_fixture("script_session_output.json")
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


@pytest.fixture
def prepared(paths):
    """[0a]+[0b]까지 끝나 팩트시트가 pass인 상태."""
    run_topic_stage("한양도성 각자성석", paths=paths, today=TODAY)
    sheet = json.dumps(load_fixture("factsheet_pass.json"), ensure_ascii=False)
    run_research_stage(
        SLUG,
        llm=FakeLLMClient([RESEARCH_MD, VERIFY_MD, CRITIQUE_MD, sheet]),
        paths=paths,
    )
    return paths


# --- 정상 경로 ---------------------------------------------------------------


def test_candidate_passes_all_three_validators(prepared):
    """생성물이 스키마·대본규칙·그라운딩을 모두 통과한다."""
    result = run_script_stage(SLUG, llm=FakeLLMClient([session_output()]), paths=prepared)
    assert result.errors == []
    assert result.warnings == []
    assert result.valid


def test_writes_candidate_file(prepared):
    result = run_script_stage(SLUG, llm=FakeLLMClient([session_output()]), paths=prepared)
    assert result.candidate_path == prepared.topic_dir(SLUG) / "05-candidates" / "01.json"
    written = json.loads(result.candidate_path.read_text(encoding="utf-8"))
    assert written == result.scenes


def test_session_gets_no_tools(prepared):
    """팩트시트는 프롬프트에 주입되므로 세션이 읽을 것이 없다 (ADR-0011)."""
    llm = FakeLLMClient([session_output()])
    run_script_stage(SLUG, llm=llm, paths=prepared)
    assert llm.calls[0]["allowed_tools"] == ()
    assert llm.calls[0]["add_dirs"] == ()


def test_prompt_carries_factsheet_numbers(prepared):
    llm = FakeLLMClient([session_output()])
    run_script_stage(SLUG, llm=llm, paths=prepared)
    prompt = llm.calls[0]["prompt"]
    assert "18.6km" in prompt
    assert "11만 명" in prompt


# --- 룰 테이블이 채우는 필드 (ADR-0001) --------------------------------------


def test_camera_comes_from_beat_rule_table(prepared):
    """세션은 카메라를 정하지 않는다 (CLAUDE.md 원칙 3, specs/03)."""
    result = run_script_stage(SLUG, llm=FakeLLMClient([session_output()]), paths=prepared)
    by_beat = {s["beat"]: s["camera"] for s in result.scenes["scenes"]}
    assert by_beat["hook_fact"] == "slow_zoom_in"
    assert by_beat["turning_point"] == "slow_zoom_in"
    assert by_beat["ending_echo"] == "slow_zoom_out"
    assert by_beat["dilemma_peak"] == "static"


def test_number_beats_get_big_red_text_emphasis(prepared):
    """숫자 비트의 오버레이는 대형 빨간 숫자 (specs/03, ADR-0002)."""
    result = run_script_stage(SLUG, llm=FakeLLMClient([session_output()]), paths=prepared)
    numbered = [s for s in result.scenes["scenes"]
                if s["beat"] in ("context_number", "solution_number")]
    assert numbered
    for scene in numbered:
        assert scene["emphasis"]["type"] == "big_red_text"
        assert scene["emphasis"]["value"] in scene["text"]


def test_all_motion_is_kenburns(prepared):
    """kling 선택은 이 슬라이스에서 하지 않는다 (ADR-0006 판단은 하류로)."""
    result = run_script_stage(SLUG, llm=FakeLLMClient([session_output()]), paths=prepared)
    assert {s["motion"] for s in result.scenes["scenes"]} == {"kenburns"}


def test_timestamps_are_derived_from_char_count():
    """산술은 세션이 아니라 코드가 한다."""
    raw = [
        {"beat": "hook_fact", "text": "열두 글자짜리 문장.", "subject": "피사체",
         "subject_scale": "wide"},
        {"beat": "ending_echo", "text": "또 다른 문장입니다.", "subject": "피사체",
         "subject_scale": "wide"},
    ]
    doc = build_scenes(raw, run_id="r", topic="t")
    first, second = doc["scenes"]
    assert first["est_start"] == 0.0
    assert first["est_end"] == pytest.approx(len("열두글자짜리문장") / NOMINAL_SPEED, abs=1e-3)
    assert second["est_start"] == first["est_end"]
    assert doc["total_duration"] == second["est_end"]


def test_session_supplied_timestamps_are_ignored():
    """세션이 타임스탬프를 넣어 보내도 스키마에 새지 않는다."""
    raw = [{"beat": "hook_fact", "text": "문장.", "subject": "피사체",
            "subject_scale": "wide", "est_start": 999.0, "camera": "dolly_zoom"}]
    doc = build_scenes(raw, run_id="r", topic="t")
    assert doc["scenes"][0]["est_start"] == 0.0
    assert doc["scenes"][0]["camera"] == "slow_zoom_in"


@pytest.mark.parametrize("scale", [None, "", "medium", "WIDE"])
def test_unknown_subject_scale_is_rejected(scale):
    """ADR-0018 — 구도가 이 값에 걸려 있다. 없다고 wide로 때우지 않는다."""
    scene = {"beat": "hook_fact", "text": "문장.", "subject": "피사체"}
    if scale is not None:
        scene["subject_scale"] = scale
    with pytest.raises(ScriptStageError, match="subject_scale"):
        build_scenes([scene], run_id="r", topic="t")


def test_subject_scale_reaches_the_scene_contract():
    raw = [{"beat": "hook_fact", "text": "문장.", "subject": "댐 단면 일러스트",
            "subject_scale": "diagram"}]
    doc = build_scenes(raw, run_id="r", topic="t")
    assert doc["scenes"][0]["subject_scale"] == "diagram"


def test_unknown_beat_is_rejected():
    raw = [{"beat": "hook", "text": "문장.", "subject": "피사체",
            "subject_scale": "wide"}]
    with pytest.raises(ScriptStageError, match="비트 테이블"):
        build_scenes(raw, run_id="r", topic="t")


def test_number_beat_without_number_is_rejected():
    raw = [{"beat": "solution_number", "text": "숫자가 없는 문장.", "subject": "피사체",
            "subject_scale": "wide"}]
    with pytest.raises(ScriptStageError, match="숫자가 없다"):
        build_scenes(raw, run_id="r", topic="t")


# --- 팩트시트 주입 (specs/06) -------------------------------------------------


def test_low_confidence_facts_are_not_injected():
    """프롬프트로 금지하는 대신 아예 보여주지 않는다."""
    sheet = load_fixture("factsheet_fail.json")
    trimmed = groundable_factsheet(sheet)
    assert [f["confidence"] for f in trimmed["facts"]] == ["medium"]
    assert all(f["confidence"] != "low" for f in trimmed["facts"])


def test_verdict_and_conditions_are_not_injected():
    """토픽 판정은 대본 작가가 알 필요 없다 — 사실만 준다."""
    trimmed = groundable_factsheet(load_fixture("factsheet_pass.json"))
    assert "verdict" not in trimmed
    assert "conditions" not in trimmed


# --- 진입 금지 / 실패 경로 ---------------------------------------------------


def test_failed_verdict_blocks_the_stage(paths):
    """4조건 불충족 소재는 대본 생성에 진입하지 않는다 (specs/06)."""
    run_topic_stage("한양도성 각자성석", paths=paths, today=TODAY)
    sheet = json.dumps(load_fixture("factsheet_fail.json"), ensure_ascii=False)
    run_research_stage(
        SLUG,
        llm=FakeLLMClient([RESEARCH_MD, VERIFY_MD, CRITIQUE_MD, sheet]),
        paths=paths,
    )
    with pytest.raises(ScriptStageError, match="verdict"):
        run_script_stage(SLUG, llm=FakeLLMClient([session_output()]), paths=paths)


def test_missing_factsheet_is_rejected(paths):
    run_topic_stage("한양도성 각자성석", paths=paths, today=TODAY)
    with pytest.raises(ScriptStageError, match="팩트시트가 없다"):
        run_script_stage(SLUG, llm=FakeLLMClient([session_output()]), paths=paths)


def test_non_json_output_fails_without_retry(prepared):
    """재생성 루프는 [2. validate] 소관이라 여기서 재시도하지 않는다."""
    llm = FakeLLMClient(["JSON이 아닌 산문 응답입니다."])
    with pytest.raises(ScriptStageError, match="JSON"):
        run_script_stage(SLUG, llm=llm, paths=prepared)
    assert len(llm.calls) == 1


def test_invalid_candidate_is_still_written(prepared):
    """검증에 실패해도 후보 파일은 남긴다 — 판정은 하류가 한다."""
    truncated = load_fixture("script_session_output.json")
    truncated["scenes"] = truncated["scenes"][:5]
    llm = FakeLLMClient([json.dumps(truncated, ensure_ascii=False)])
    result = run_script_stage(SLUG, llm=llm, paths=prepared)

    assert not result.valid
    assert result.candidate_path.is_file()
    assert any("대본규칙" in e for e in result.errors)


def test_ungrounded_number_is_reported(prepared):
    """팩트시트에 없는 숫자를 쓰면 그라운딩 검증이 잡는다 (ADR-0007)."""
    data = load_fixture("script_session_output.json")
    data["scenes"][2]["text"] = "1398년, 태조는 성벽 공사를 시작합니다."
    llm = FakeLLMClient([json.dumps(data, ensure_ascii=False)])
    result = run_script_stage(SLUG, llm=llm, paths=prepared)
    assert any("그라운딩" in e and "1398" in e for e in result.errors)


# --- 재시작 -------------------------------------------------------------------


def test_existing_candidate_is_skipped(prepared):
    run_script_stage(SLUG, llm=FakeLLMClient([session_output()]), paths=prepared)
    again = run_script_stage(SLUG, llm=FakeLLMClient([]), paths=prepared)
    assert again.skipped
    assert again.valid


def test_force_regenerates(prepared):
    run_script_stage(SLUG, llm=FakeLLMClient([session_output()]), paths=prepared)
    llm = FakeLLMClient([session_output()])
    again = run_script_stage(SLUG, llm=llm, paths=prepared, force=True)
    assert not again.skipped
    assert len(llm.calls) == 1


def test_state_records_validation_outcome(prepared):
    result = run_script_stage(SLUG, llm=FakeLLMClient([session_output()]), paths=prepared)
    state = json.loads((prepared.run_dir(result.run_id) / "state.json").read_text("utf-8"))
    entry = state["stages"]["1-script"]
    assert entry["status"] == "done"
    assert entry["scene_count"] == len(result.scenes["scenes"])
    assert entry["validation_errors"] == []
