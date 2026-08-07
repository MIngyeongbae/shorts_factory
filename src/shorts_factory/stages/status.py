"""topics/{slug}/STATUS.md — 인간 게이트 파일 (ADR-0009).

STATUS.md가 `go`가 아니면 비주얼 파이프라인(2부) 진입이 금지된다.
`go`는 **사람만** 기록한다. 파이프라인은 `보류`를 만들고, 스펙상 명백한
반려(4조건 불충족)일 때만 `no-go`를 쓴다.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from ..config import write_text

GO = "go"
NO_GO = "no-go"
PENDING = "보류"
VALID_STATUSES = (GO, NO_GO, PENDING)

_STATUS_RE = re.compile(r"^#\s*STATUS:\s*(\S+)", re.MULTILINE)

_CHECKLIST = (
    ("0a. topic", "topic"),
    ("0b. research", "research"),
    ("1. script", "script"),
    ("1b. score", "score"),
    ("2. validate", "validate"),
    ("2b. judge", "judge"),
)


def read_status(path: Path) -> str | None:
    if not path.exists():
        return None
    match = _STATUS_RE.search(path.read_text(encoding="utf-8"))
    return match.group(1) if match else None


def render_status(
    *,
    status: str,
    topic: str,
    slug: str,
    run_id: str,
    reason: str,
    decided_by: str = "(미정 — 인간 게이트)",
    done_stages: tuple[str, ...] = (),
) -> str:
    if status not in VALID_STATUSES:
        raise ValueError(f"'{status}'는 허용된 STATUS가 아니다: {VALID_STATUSES}")

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    checklist = "\n".join(
        f"- [{'x' if key in done_stages else ' '}] {label}" for label, key in _CHECKLIST
    )

    return f"""# STATUS: {status}

- 소재: {topic}
- slug: `{slug}`
- run_id: `{run_id}`
- 갱신: {stamp}
- 판정자: {decided_by}

## 사유

{reason}

## 1부 진행 상황

{checklist}

## 게이트 규칙 (ADR-0009)

이 파일이 `go`가 되기 전에는 2부(영상 생산, 편당 ~$7)로 진입할 수 없다.
`go` / `no-go`는 패키지(조사~대본 선발본)를 사람이 검토한 뒤 직접 기록한다.
"""


def write_status(path: Path, **kwargs) -> None:
    write_text(path, render_status(**kwargs))
