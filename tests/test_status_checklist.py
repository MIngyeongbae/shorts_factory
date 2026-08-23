"""STATUS.md 진행 체크리스트 계약 (ADR-0009).

체크리스트는 `runs/{run_id}/state.json`의 투영이고, 판정(`# STATUS:`)은 사람의 것이다.
여기서 지키는 것은 그 경계다 — 진행 기록이 사람의 `go`를 지우면 게이트가 무의미해진다.
"""

from __future__ import annotations

from pathlib import Path

from shorts_factory.runstate import RunState
from shorts_factory.stages import status as status_mod


def _package(tmp_path: Path, *, done: tuple[str, ...] = ()) -> tuple[Path, RunState]:
    topic_dir = tmp_path / "topics" / "t"
    topic_dir.mkdir(parents=True)
    status_mod.write_status(
        topic_dir / status_mod.FILENAME,
        status=status_mod.PENDING,
        topic="소재",
        slug="t",
        run_id="20260820-t",
        reason="조사 통과",
        done_stages=("0-seed",),
    )
    state = RunState.load_or_create(tmp_path / "runs" / "20260820-t", "20260820-t")
    for key in done:
        state.mark_done(key)
    return topic_dir, state


def _checked(text: str) -> list[str]:
    return [
        line[len("- [x] "):]
        for line in text.splitlines()
        if line.startswith("- [x] ")
    ]


def test_checklist_follows_run_state(tmp_path):
    topic_dir, state = _package(
        tmp_path, done=("0-seed", "1-draft")
    )
    assert status_mod.sync_checklist(topic_dir, state) is True

    text = (topic_dir / status_mod.FILENAME).read_text(encoding="utf-8")
    assert _checked(text) == ["0. seed", "1. draft"]
    assert "- [ ] 2. factcheck" in text


def test_late_stage_fills_earlier_blanks(tmp_path):
    """체크리스트는 state.json 전체의 투영이라 늦은 단계 하나가 앞 빈칸도 메운다."""
    topic_dir, state = _package(
        tmp_path,
        done=("0-seed", "1-draft", "2-factcheck"),
    )
    status_mod.sync_checklist(topic_dir, state)

    text = (topic_dir / status_mod.FILENAME).read_text(encoding="utf-8")
    assert "- [x] 1. draft" in text
    assert "- [x] 2. factcheck" in text


def test_human_verdict_survives_sync(tmp_path):
    """사람이 적은 go·사유·판정자를 진행 기록이 덮지 않는다 (ADR-0009)."""
    topic_dir, state = _package(tmp_path, done=("0-seed", "1-draft"))
    path = topic_dir / status_mod.FILENAME
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace("# STATUS: 보류", "# STATUS: go")
        .replace("판정자: (미정 — 인간 게이트)", "판정자: 사람")
        .replace("조사 통과", "패키지 전체를 보고 승인함"),
        encoding="utf-8",
    )

    status_mod.sync_checklist(topic_dir, state)

    text = path.read_text(encoding="utf-8")
    assert status_mod.read_status(path) == status_mod.GO
    assert "판정자: 사람" in text
    assert "패키지 전체를 보고 승인함" in text
    assert "- [x] 1. draft" in text


def test_missing_file_is_not_an_error(tmp_path):
    """D-3 — 선택적 입력의 부재는 경고가 아니다."""
    _, state = _package(tmp_path, done=("0-seed",))
    empty = tmp_path / "topics" / "none"
    empty.mkdir()
    assert status_mod.sync_checklist(empty, state) is False
    assert not (empty / status_mod.FILENAME).exists()


def test_handwritten_file_without_checklist_is_left_alone(tmp_path):
    """사람이 줄여 쓴 STATUS.md를 제 모양으로 재구성하지 않는다."""
    topic_dir, state = _package(tmp_path, done=("0-seed", "1-draft"))
    path = topic_dir / status_mod.FILENAME
    original = "# STATUS: go\n\n사람이 승인함\n"
    path.write_text(original, encoding="utf-8")

    assert status_mod.sync_checklist(topic_dir, state) is False
    assert path.read_text(encoding="utf-8") == original


def test_checklist_has_the_localize_item(tmp_path):
    """[2l]은 1부 네 번째 칸이다 (ADR-0056 결정 5)."""
    topic_dir, state = _package(tmp_path, done=("0-seed", "1-draft", "2-factcheck"))
    status_mod.sync_checklist(topic_dir, state)
    text = (topic_dir / status_mod.FILENAME).read_text(encoding="utf-8")
    assert "- [ ] 2l. localize" in text

    state.mark_done("2l-localize")
    status_mod.sync_checklist(topic_dir, state)
    text = (topic_dir / status_mod.FILENAME).read_text(encoding="utf-8")
    assert _checked(text) == ["0. seed", "1. draft", "2. factcheck", "2l. localize"]


def test_checklist_labels_come_from_stage_keys(tmp_path):
    """라벨을 따로 적지 않는다 — 두 벌이 되면 갈라진다."""
    topic_dir, state = _package(tmp_path, done=("2-factcheck",))
    status_mod.sync_checklist(topic_dir, state)

    text = (topic_dir / status_mod.FILENAME).read_text(encoding="utf-8")
    assert "- [x] 2. factcheck" in text
    assert "- [ ] 1. draft" in text
