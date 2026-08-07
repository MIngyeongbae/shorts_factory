import pytest

from shorts_factory import backlog as backlog_mod
from shorts_factory.backlog import (
    STATUS_REJECTED,
    STATUS_RESEARCHING,
    BacklogError,
    find_entry,
    parse_backlog,
    update_status,
)


def test_parse_reads_conditions_and_status(paths):
    entries = parse_backlog(paths.backlog)
    assert [e.topic for e in entries] == ["한양도성 각자성석", "미완성 소재"]

    first = entries[0]
    assert first.slug == "hanyangdoseong-gakjaseongseok"
    assert first.conditions == {
        "twist": True,
        "failed_alternative": True,
        "numbers": True,
        "present_link": True,
    }
    assert first.all_conditions_met
    assert first.sources == "실록, 국가유산포털"


def test_parse_marks_unmet_conditions(paths):
    entry = parse_backlog(paths.backlog)[1]
    assert not entry.all_conditions_met
    # ❌ 와 빈 칸 모두 미충족으로 읽는다
    assert set(entry.unmet_conditions) == {"failed_alternative", "present_link"}


def test_find_entry_by_topic_and_slug(paths):
    entries = parse_backlog(paths.backlog)
    assert find_entry(entries, "한양도성 각자성석").slug == "hanyangdoseong-gakjaseongseok"
    assert find_entry(entries, "hanyangdoseong-gakjaseongseok").topic == "한양도성 각자성석"


def test_find_entry_partial_match(paths):
    entries = parse_backlog(paths.backlog)
    assert find_entry(entries, "각자성석").topic == "한양도성 각자성석"


def test_find_entry_missing_raises(paths):
    entries = parse_backlog(paths.backlog)
    with pytest.raises(BacklogError):
        find_entry(entries, "없는 소재")


def test_update_status_rewrites_only_that_cell(paths):
    entries = parse_backlog(paths.backlog)
    target = entries[0]
    update_status(paths.backlog, target, STATUS_RESEARCHING)

    reparsed = parse_backlog(paths.backlog)
    assert reparsed[0].status == STATUS_RESEARCHING
    assert reparsed[0].conditions == target.conditions
    assert reparsed[0].sources == "실록, 국가유산포털"
    # 다른 행은 그대로
    assert reparsed[1].status == "후보"


def test_update_status_rejects_undefined_status(paths):
    entries = parse_backlog(paths.backlog)
    with pytest.raises(BacklogError):
        update_status(paths.backlog, entries[0], "대충진행중")


def test_update_status_supports_rejection(paths):
    entries = parse_backlog(paths.backlog)
    update_status(paths.backlog, entries[1], STATUS_REJECTED)
    assert parse_backlog(paths.backlog)[1].status == STATUS_REJECTED


def test_column_order_is_irrelevant(tmp_path):
    """헤더 이름으로 매핑하므로 컬럼 순서를 바꿔도 읽혀야 한다."""
    path = tmp_path / "backlog.md"
    path.write_text(
        "| 상태 | 숫자 | 소재명 | 현재접점 | 뒤집기 | 실패대안 |\n"
        "|---|---|---|---|---|---|\n"
        "| 후보 | ✅ | 창덕궁 인정전 | ✅ | ✅ | ✅ |\n",
        encoding="utf-8",
    )
    entry = parse_backlog(path)[0]
    assert entry.topic == "창덕궁 인정전"
    assert entry.all_conditions_met
    assert entry.status == "후보"


def test_explicit_slug_column_overrides_romanization(tmp_path):
    path = tmp_path / "backlog.md"
    path.write_text(
        "| 소재 | slug | 뒤집기 | 실패대안 | 숫자 | 현재접점 | 상태 |\n"
        "|---|---|---|---|---|---|---|\n"
        "| 경복궁 뒷산 벌목 금지 | baekak | ✅ | ✅ | ✅ | ✅ | 후보 |\n",
        encoding="utf-8",
    )
    assert parse_backlog(path)[0].slug == "baekak"


def test_missing_file_raises(tmp_path):
    with pytest.raises(BacklogError):
        parse_backlog(tmp_path / "없음.md")


def test_table_without_topic_column_raises(tmp_path):
    path = tmp_path / "backlog.md"
    path.write_text("| 상태 | 숫자 |\n|---|---|\n| 후보 | ✅ |\n", encoding="utf-8")
    with pytest.raises(BacklogError):
        parse_backlog(path)
