"""[0. seed] 단계 계약 검증 (ADR-0049)."""

import json
from datetime import date

import pytest

from shorts_factory.backlog import parse_backlog, update_status
from shorts_factory.stages import status as status_mod
from shorts_factory.stages.topic import TopicStageError, run_topic_stage

TODAY = date(2026, 8, 7)


def test_creates_topic_package(paths):
    result = run_topic_stage("한양도성 각자성석", paths=paths, today=TODAY)

    assert result.accepted
    assert result.slug == "hanyangdoseong-gakjaseongseok"
    assert result.run_id == "20260807-hanyangdoseong-gakjaseongseok"
    assert result.topic_dir.is_dir()
    # specs/06 토픽 패키지 구조
    assert (result.topic_dir / "seed.md").is_file()
    assert (result.topic_dir / "STATUS.md").is_file()


def test_writes_stage_contract(paths):
    result = run_topic_stage("한양도성 각자성석", paths=paths, today=TODAY)
    contract = json.loads((result.run_dir / "topic.json").read_text(encoding="utf-8"))

    assert contract["topic"] == "한양도성 각자성석"
    assert contract["slug"] == "hanyangdoseong-gakjaseongseok"
    assert contract["run_id"] == result.run_id
    assert contract["backlog_conditions"]["twist"] is True
    assert contract["topic_dir"] == "topics/hanyangdoseong-gakjaseongseok"


def test_status_starts_as_pending_not_go(paths):
    """go는 사람만 기록한다 (ADR-0009)."""
    result = run_topic_stage("한양도성 각자성석", paths=paths, today=TODAY)
    assert status_mod.read_status(result.topic_dir / "STATUS.md") == status_mod.PENDING


def test_backlog_moves_to_researching(paths):
    run_topic_stage("한양도성 각자성석", paths=paths, today=TODAY)
    entry = parse_backlog(paths.backlog)[0]
    assert entry.status == "리서치중"


def test_run_state_records_stage(paths):
    result = run_topic_stage("한양도성 각자성석", paths=paths, today=TODAY)
    state = json.loads((result.run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["stages"]["0-seed"]["status"] == "done"
    assert state["topic"] == "한양도성 각자성석"


def test_empty_indicators_do_not_reject_the_topic(paths):
    """지표가 비어도 패키지를 만든다 (ADR-0033 §1).

    네 지표는 전부 폐기된 고정 구조를 채우기 위한 것이었다. 지금 게이트는 매체
    적합성 하나이고, 지표는 관측값으로만 남는다 (ADR-0049).
    """
    result = run_topic_stage("미완성 소재", paths=paths, today=TODAY)

    assert result.accepted
    assert result.topic_dir.exists()
    # 관측은 남는다 — 반려하지 않을 뿐 세지 않는 것은 아니다
    assert set(result.unmet_conditions) == {"failed_alternative", "present_link"}
    assert "관측" in result.summary


def test_rerun_skips_completed_stage(paths):
    """같은 run_id 재실행 시 완료 단계는 스킵 (specs/05 실패 정책)."""
    run_topic_stage("한양도성 각자성석", paths=paths, today=TODAY)
    second = run_topic_stage("한양도성 각자성석", paths=paths, today=TODAY)
    assert second.skipped and second.accepted


def test_rerun_does_not_overwrite_human_decision(paths):
    """사람이 기록한 go/no-go를 파이프라인이 덮어쓰지 않는다."""
    first = run_topic_stage("한양도성 각자성석", paths=paths, today=TODAY)
    status_path = first.topic_dir / "STATUS.md"
    status_path.write_text("# STATUS: go\n\n사람이 승인함\n", encoding="utf-8")

    run_topic_stage("한양도성 각자성석", paths=paths, today=TODAY, force=True)
    assert status_mod.read_status(status_path) == "go"


def test_defaults_to_first_candidate(paths):
    result = run_topic_stage(None, paths=paths, today=TODAY)
    assert result.topic == "한양도성 각자성석"


def test_no_candidate_raises(paths):
    """'후보' 상태 항목이 하나도 없으면 자동 선택은 실패해야 한다."""
    run_topic_stage("한양도성 각자성석", paths=paths, today=TODAY)  # → 리서치중
    entries = parse_backlog(paths.backlog)
    update_status(paths.backlog, entries[1], "반려")

    with pytest.raises(TopicStageError):
        run_topic_stage(None, paths=paths, today=TODAY)


def test_seed_url_reaches_contract_and_seed_md(paths):
    """시드 기사 URL이 topic.json과 seed.md에 실린다 (ADR-0049)."""
    result = run_topic_stage(
        "한양도성 각자성석", paths=paths, today=TODAY,
        seed_url="https://ko.wikipedia.org/wiki/각자성석",
    )
    contract = json.loads((result.run_dir / "topic.json").read_text(encoding="utf-8"))
    assert contract["seed_url"] == "https://ko.wikipedia.org/wiki/각자성석"
    seed = (result.topic_dir / "seed.md").read_text(encoding="utf-8")
    assert "https://ko.wikipedia.org/wiki/각자성석" in seed
