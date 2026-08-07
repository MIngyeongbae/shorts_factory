"""topics/backlog.md 파싱 및 상태 갱신.

백로그 항목은 specs/06-topic-research.md의 `[0a. topic] 소재 백로그` 정의를 따른다:
소재명 / 4조건 체크 / 출처 후보 / 상태(후보·리서치중·제작완료·반려).

컬럼 순서에 의존하지 않고 헤더 이름으로 매핑한다. `slug` 컬럼이 있으면
자동 로마자 변환 대신 그 값을 쓴다(수동 오버라이드).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .slug import slugify

#: specs/06-topic-research.md가 정의한 상태 값
STATUS_CANDIDATE = "후보"
STATUS_RESEARCHING = "리서치중"
STATUS_DONE = "제작완료"
STATUS_REJECTED = "반려"
CANONICAL_STATUSES = (STATUS_CANDIDATE, STATUS_RESEARCHING, STATUS_DONE, STATUS_REJECTED)

#: specs/06-topic-research.md의 소재 4조건 → 팩트시트 conditions 키
CONDITION_KEYS = ("twist", "failed_alternative", "numbers", "present_link")

_HEADER_ALIASES: dict[str, str] = {
    "소재": "topic",
    "소재명": "topic",
    "주제": "topic",
    "slug": "slug",
    "슬러그": "slug",
    "뒤집기": "twist",
    "통념뒤집기": "twist",
    "실패대안": "failed_alternative",
    "실패한대안": "failed_alternative",
    "실패한대안의기록": "failed_alternative",
    "숫자": "numbers",
    "구체적숫자": "numbers",
    "현재접점": "present_link",
    "출처후보": "sources",
    "출처": "sources",
    "상태": "status",
    "비고": "notes",
}

_TRUE_MARKS = {"✅", "✔", "✔️", "⭕", "o", "O", "ㅇ", "y", "Y", "yes", "true", "1", "예"}
_FALSE_MARKS = {"❌", "✖", "✗", "x", "X", "n", "N", "no", "false", "0", "-", "아니오", ""}


class BacklogError(Exception):
    """백로그 파일이 계약을 벗어났을 때."""


@dataclass
class BacklogEntry:
    topic: str
    slug: str
    conditions: dict[str, bool]
    status: str
    sources: str = ""
    notes: str = ""
    line_no: int = -1  # 0-based, 상태 갱신용
    raw_cells: dict[str, str] = field(default_factory=dict)

    @property
    def all_conditions_met(self) -> bool:
        return all(self.conditions.get(key, False) for key in CONDITION_KEYS)

    @property
    def unmet_conditions(self) -> list[str]:
        return [key for key in CONDITION_KEYS if not self.conditions.get(key, False)]


def _normalize_header(cell: str) -> str:
    return re.sub(r"\s+", "", cell).strip().lower()


def _split_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c)


def _parse_mark(value: str) -> bool:
    token = value.strip()
    if token in _TRUE_MARKS:
        return True
    if token in _FALSE_MARKS:
        return False
    # "✅ (실록 확인)" 처럼 주석이 붙은 경우 첫 글자로 판단
    if token and token[0] in _TRUE_MARKS:
        return True
    return False


def parse_backlog(path: Path) -> list[BacklogEntry]:
    """백로그의 첫 번째 마크다운 표를 파싱한다."""
    if not path.exists():
        raise BacklogError(f"백로그 파일이 없다: {path}")

    lines = path.read_text(encoding="utf-8").splitlines()

    header_idx = -1
    header_map: dict[int, str] = {}
    for idx, line in enumerate(lines):
        if not line.strip().startswith("|"):
            continue
        cells = _split_row(line)
        mapped = {
            i: _HEADER_ALIASES[_normalize_header(c)]
            for i, c in enumerate(cells)
            if _normalize_header(c) in _HEADER_ALIASES
        }
        if "topic" in mapped.values():
            header_idx, header_map = idx, mapped
            break

    if header_idx < 0:
        raise BacklogError(f"소재명 컬럼이 있는 표를 찾지 못했다: {path}")

    entries: list[BacklogEntry] = []
    for idx in range(header_idx + 1, len(lines)):
        line = lines[idx]
        if not line.strip().startswith("|"):
            break  # 표 종료
        cells = _split_row(line)
        if _is_separator(cells):
            continue

        row = {header_map[i]: cells[i] for i in header_map if i < len(cells)}
        topic = row.get("topic", "").strip()
        if not topic:
            continue

        explicit_slug = row.get("slug", "").strip()
        entries.append(
            BacklogEntry(
                topic=topic,
                slug=explicit_slug or slugify(topic),
                conditions={key: _parse_mark(row.get(key, "")) for key in CONDITION_KEYS},
                status=row.get("status", "").strip(),
                sources=row.get("sources", "").strip(),
                notes=row.get("notes", "").strip(),
                line_no=idx,
                raw_cells=row,
            )
        )

    return entries


def find_entry(entries: list[BacklogEntry], needle: str) -> BacklogEntry:
    """소재명 또는 슬러그로 항목을 찾는다."""
    needle = needle.strip()
    for entry in entries:
        if entry.topic == needle or entry.slug == needle:
            return entry
    # 부분 일치 폴백 (유일할 때만 허용)
    partial = [e for e in entries if needle in e.topic]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        names = ", ".join(e.topic for e in partial)
        raise BacklogError(f"소재명 '{needle}'이 여러 항목과 일치한다: {names}")
    raise BacklogError(f"백로그에 '{needle}' 항목이 없다")


def update_status(path: Path, entry: BacklogEntry, new_status: str) -> None:
    """해당 소재의 상태 셀만 교체한다. 표의 나머지는 건드리지 않는다."""
    if new_status not in CANONICAL_STATUSES:
        raise BacklogError(
            f"'{new_status}'는 정의된 상태가 아니다. 허용: {', '.join(CANONICAL_STATUSES)}"
        )

    lines = path.read_text(encoding="utf-8").splitlines()
    if not 0 <= entry.line_no < len(lines):
        raise BacklogError(f"백로그 행 위치가 어긋났다 (line {entry.line_no})")

    target = lines[entry.line_no]
    cells = _split_row(target)

    # 상태 컬럼 인덱스를 헤더에서 다시 찾는다 (파싱 시점 이후 편집 방어)
    status_idx = -1
    for idx in range(entry.line_no - 1, -1, -1):
        if not lines[idx].strip().startswith("|"):
            break
        header_cells = _split_row(lines[idx])
        for i, cell in enumerate(header_cells):
            if _HEADER_ALIASES.get(_normalize_header(cell)) == "status":
                status_idx = i
                break
        if status_idx >= 0:
            break

    if status_idx < 0 or status_idx >= len(cells):
        raise BacklogError("백로그에서 상태 컬럼을 찾지 못했다")

    cells[status_idx] = new_status
    lines[entry.line_no] = "| " + " | ".join(cells) + " |"

    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")

    entry.status = new_status
