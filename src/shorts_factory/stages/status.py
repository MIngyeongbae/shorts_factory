"""topics/{slug}/STATUS.md — 인간 게이트 파일 (ADR-0009).

STATUS.md가 `go`가 아니면 비주얼 파이프라인(2부) 진입이 금지된다.
`go`는 **사람만** 기록한다. 파이프라인은 `보류`를 만들고, 스펙상 명백한
반려(판별 기준 미달)일 때만 `no-go`를 쓴다.

이 파일이 담는 것은 둘이고 주인이 다르다.

- **판정** (`# STATUS:` · 사유 · 판정자) — 사람의 것이다. `[0a]`·`[0b]`가 초기값만 놓는다
- **진행 체크리스트** — `runs/{run_id}/state.json`의 투영이다. 사실은 저쪽에 있고 여기
  있는 것은 사람이 읽을 사본이라, 단계가 끝날 때마다 `sync_checklist()`가 다시 그린다

둘을 한 함수로 쓰면 진행 기록이 사람의 `go`를 덮어쓴다. 그래서 갈라 뒀다.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from ..config import write_text
from ..runstate import RunState

GO = "go"
NO_GO = "no-go"
PENDING = "보류"
VALID_STATUSES = (GO, NO_GO, PENDING)

#: 토픽 패키지 안에서의 파일명 (specs/06-topic-research.md 패키지 구조)
FILENAME = "STATUS.md"

_STATUS_RE = re.compile(r"^#\s*STATUS:\s*(\S+)", re.MULTILINE)

#: 체크리스트 항목 = RunState의 단계 키다 (`stages/*.py`의 `STAGE` 상수와 같은 값).
#: 라벨을 따로 적어 두지 않는 이유는 두 벌이 되면 갈라지기 때문이고, 키에서 만든다.
#: 1부는 네 단계다 (ADR-0049 + ADR-0056의 `[2l]`).
_CHECKLIST: tuple[str, ...] = (
    "0-seed",
    "1-draft",
    "2-factcheck",
    "2l-localize",
)

_CHECKLIST_HEADING = "## 1부 진행 상황"

#: 체크리스트 절 전체(제목 + 항목 줄들). `sync_checklist`가 이 덩어리만 갈아 끼운다.
_CHECKLIST_BLOCK_RE = re.compile(
    rf"^{re.escape(_CHECKLIST_HEADING)}\n\n(?:- \[[ x]\] .*\n)+", re.MULTILINE
)
_STAMP_RE = re.compile(r"^- 갱신: .*$", re.MULTILINE)


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _label(stage_key: str) -> str:
    """RunState 단계 키 → 사람이 읽는 라벨. `1s-sceneplan` → `1s. sceneplan`."""
    return stage_key.replace("-", ". ", 1)


def _render_checklist(done_stages: tuple[str, ...]) -> str:
    done = set(done_stages)
    return "\n".join(
        f"- [{'x' if key in done else ' '}] {_label(key)}" for key in _CHECKLIST
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

    return f"""# STATUS: {status}

- 소재: {topic}
- slug: `{slug}`
- run_id: `{run_id}`
- 갱신: {_stamp()}
- 판정자: {decided_by}

## 사유

{reason}

{_CHECKLIST_HEADING}

{_render_checklist(tuple(done_stages))}

## 게이트 규칙 (ADR-0009)

이 파일이 `go`가 되기 전에는 2부(영상 생산)로 진입할 수 없다.
`go` / `no-go`는 script.md(대본)와 factcheck.md(검증)를 사람이 읽은 뒤 직접 기록한다 (ADR-0049).
"""


def write_status(path: Path, **kwargs) -> None:
    write_text(path, render_status(**kwargs))


def sync_checklist(topic_dir: Path, state: RunState) -> bool:
    """STATUS.md의 진행 체크리스트를 run 상태에 맞춰 다시 그린다.

    **판정에는 손대지 않는다.** `# STATUS:` 줄·사유·판정자는 읽지도 고치지도 않고
    체크리스트 절과 `- 갱신:` 시각만 갈아 끼운다. 사람이 적은 `go`가 파이프라인의
    진행 기록에 지워지면 ADR-0009의 게이트가 그 순간 무의미해지기 때문이다.

    파일이 없거나 체크리스트 절이 없으면 아무것도 하지 않고 False를 준다
    (D-3 — 선택적 입력의 부재는 경고가 아니다). 사람이 손으로 줄여 쓴 STATUS.md를
    이 함수가 제 모양으로 재구성하지 않는다.

    체크리스트는 **state.json 전체의 투영**이라, 늦게 부르는 단계 하나가 앞 단계의
    빈칸까지 함께 메운다. 그래서 각 단계는 자기 완료 직후 한 번만 부르면 된다.
    """
    path = topic_dir / FILENAME
    if not path.exists():
        return False

    text = path.read_text(encoding="utf-8")
    if not _CHECKLIST_BLOCK_RE.search(text):
        return False

    done = tuple(key for key in _CHECKLIST if state.is_done(key))
    block = f"{_CHECKLIST_HEADING}\n\n{_render_checklist(done)}\n"
    updated = _CHECKLIST_BLOCK_RE.sub(lambda _match: block, text, count=1)
    updated = _STAMP_RE.sub(lambda _match: f"- 갱신: {_stamp()}", updated, count=1)

    if updated == text:
        return False
    write_text(path, updated)
    return True
