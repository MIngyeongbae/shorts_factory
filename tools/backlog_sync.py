"""판정을 표와 폴더에 비춘다 — `topics/backlog.md`의 `상태`, 반려 편의 보관, `STATUS.md` 머리글.

    python tools/backlog_sync.py           # 어긋난 것을 고친다
    python tools/backlog_sync.py --check   # 고치지 않고 보고만 한다 (어긋나면 종료 1)

## 왜 있는가

`제작완료`·`반려`는 처음부터 스펙 06에 있던 값인데 **2026-09-03까지 표에 0건이었다.**
값이 없어서가 아니라 **쓰는 주체가 없어서**다 — `[0] seed`가 `후보 → 리서치중`까지만
바꾸고 그 뒤로는 아무도 손대지 않았다. 그래서 `리서치중` 60건 안에 이미 만들어 올린
편과 반려된 편이 다 섞였고, 사람이 읽는 표로는 죽어 있었다.

**고치는 방법은 표를 손으로 관리하는 것이 아니다** (ADR-0034: 값은 한 번만 적는다).
상태는 이미 다른 곳에 정본이 있는 **파생값**이다:

- `제작완료` ← `runs/{run_id}/upload.json`이 있다 (드라이브에 올라갔다)
- `반려`     ← `script.md` 맨 위 판정 블록의 `reject`가 풀렸다 (ADR-0094).
             옛 편의 `judgment/human.json` `decision: no_go`도 같은 뜻으로 읽는다

둘 다 **ADR-0088이 보관함 이동 기준으로 쓰는 신호**다. 이 도구는 그 두 신호를 표에
비출 뿐이고, 표를 정본으로 삼지 않는다.

## 반려 편은 여기가 옮긴다 (ADR-0094 결정 7)

제작완료분은 `[10] upload`가 보관함으로 옮기지만 반려는 어느 단계도 돌지 않으므로 옮길
단계가 없다. 판정을 표에 비추는 도구가 그 뒷정리도 진다 — 추적 중이면 `git mv`, 아니면
일반 이동이다 (ADR-0088 결정 5). 현역 편의 `STATUS.md` 머리글도 판정 블록의 투영으로
맞춘다 (`reject` → `no-go`, 언어 하나라도 → `go`, 아니면 `보류`).

## 무엇을 건드리지 않는가

- **`후보`·`리서치중`은 그대로 둔다.** 그 둘은 사람과 `[0]`의 것이고, 여기서 유도할
  근거가 없다 (판정 전이거나 2부 진행 중이라는 뜻이다)
- **행을 더하거나 지우지 않는다.** 백로그의 행 목록은 발굴이 쓰는 정본이다 —
  토픽 폴더가 없는 후보도 표에 남아야 한다 (스펙 06: 「고르지 않은 후보도 `반려`로 남긴다」)
- **`반려`를 되돌리지 않는다** — 사람이 손으로 적은 `반려`에는 판정이 없다.
  없는 것은 근거가 없는 것이므로 건드리지 않는다 (스펙 06: 「`반려`는 되살리지 않는다」)
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from shorts_factory.gate import Gate, GateError, parse_gate  # noqa: E402
from shorts_factory.stages_script_name import SCRIPT_MD_FILE  # noqa: E402

BACKLOG = ROOT / "topics" / "backlog.md"
TOPICS = ROOT / "topics"
ARCHIVE = TOPICS / "_archive"

#: 표에서 `상태`가 몇 번째 칸인가. `| 소재 | 슬러그 | … | 상태 | 비고 | 시드 |`을
#: `|`로 자르면 앞뒤에 빈 칸이 하나씩 생겨 소재가 1번이다.
SLUG_COL = 2
STATE_COL = 8
MIN_CELLS = 11

#: run 디렉터리 이름은 `{YYYYMMDD}-{slug}`다 (ADR-0011).
RUN_DIR = re.compile(r"^\d{8}-(?P<slug>.+)$")
_STATUS_HEAD = re.compile(r"^#\s*STATUS:\s*\S+", re.M)


def _gate_of(topic_dir: Path) -> Gate | None:
    path = topic_dir / SCRIPT_MD_FILE
    if not path.exists():
        return None
    try:
        return parse_gate(path.read_text(encoding="utf-8"))
    except GateError as exc:
        print(f"⚠ {path.relative_to(ROOT).as_posix()}: {exc}")
        return None


def _legacy_no_go(topic_dir: Path) -> bool:
    """옛 편의 `judgment/human.json` `decision: no_go` — 블록이 생기기 전의 반려 기록."""
    path = topic_dir / "judgment" / "human.json"
    if not path.exists():
        return False
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("decision") == "no_go"
    except json.JSONDecodeError as exc:
        print(f"⚠ {path.relative_to(ROOT).as_posix()}: 읽지 못했다 — {exc}")
        return False


def topic_dirs() -> list[Path]:
    active = [p for p in TOPICS.iterdir() if p.is_dir() and not p.name.startswith("_")]
    archived = [p for p in ARCHIVE.iterdir() if p.is_dir()] if ARCHIVE.is_dir() else []
    return sorted(active) + sorted(archived)


def derived_states() -> dict[str, str]:
    """슬러그 → 유도된 상태. 업로드가 판정을 이긴다 (올렸으면 만든 것이다)."""
    states: dict[str, str] = {}
    for topic in topic_dirs():
        gate = _gate_of(topic)
        if (gate is not None and gate.rejected) or _legacy_no_go(topic):
            states[topic.name] = "반려"

    runs = ROOT / "runs"
    if runs.is_dir():
        for run in sorted(runs.iterdir()):
            m = RUN_DIR.match(run.name)
            if m and (run / "upload.json").exists():
                states[m.group("slug")] = "제작완료"
    return states


def rejected_active() -> list[Path]:
    """반려됐는데 아직 현역 폴더에 있는 편 — 보관함으로 갈 것."""
    out = []
    for topic in TOPICS.iterdir():
        if not topic.is_dir() or topic.name.startswith("_"):
            continue
        gate = _gate_of(topic)
        if (gate is not None and gate.rejected) or _legacy_no_go(topic):
            out.append(topic)
    return sorted(out)


def _tracked(rel: str) -> bool:
    out = subprocess.run(["git", "ls-files", "--", rel], cwd=ROOT, capture_output=True, text=True)
    return bool(out.stdout.strip())


def archive(topic: Path) -> str:
    ARCHIVE.mkdir(exist_ok=True)
    target = ARCHIVE / topic.name
    if target.exists():
        raise SystemExit(f"{topic.name}: 보관함에 이미 있다 — 손으로 본다")
    rel = f"topics/{topic.name}"
    if _tracked(rel):
        subprocess.run(["git", "mv", rel, f"topics/_archive/{topic.name}"], cwd=ROOT, check=True)
        return "git mv"
    topic.rename(target)
    return "mv"


def status_drift() -> list[tuple[Path, str, str]]:
    """현역 편의 `STATUS.md` 머리글이 판정 블록과 어긋난 것 — `(파일, 지금, 되어야 할 것)`."""
    out = []
    for topic in TOPICS.iterdir():
        if not topic.is_dir() or topic.name.startswith("_"):
            continue
        gate = _gate_of(topic)
        status = topic / "STATUS.md"
        if gate is None or not gate.present or not status.exists():
            continue
        text = status.read_text(encoding="utf-8")
        m = _STATUS_HEAD.search(text)
        if not m:
            continue
        now = m.group(0).split(":", 1)[1].strip()
        if now != gate.status:
            out.append((status, now, gate.status))
    return out


def sync(*, check: bool) -> int:
    drift = 0

    # 1. 표
    states = derived_states()
    lines = BACKLOG.read_text(encoding="utf-8").splitlines(keepends=True)
    seen: set[str] = set()
    changes: list[tuple[str, str, str]] = []
    for i, line in enumerate(lines):
        if not line.startswith("|"):
            continue
        cells = line.rstrip("\n").split("|")
        if len(cells) < MIN_CELLS:
            continue
        slug = cells[SLUG_COL].strip()
        if not slug:
            continue
        seen.add(slug)
        want = states.get(slug)
        if want is None:
            continue
        now = cells[STATE_COL].strip()
        if now == want:
            continue
        changes.append((slug, now, want))
        cells[STATE_COL] = f" {want} "
        lines[i] = "|".join(cells) + "\n"
    for slug, now, want in changes:
        print(f"  표  {slug}: {now} → {want}")
    orphan = sorted(s for s in states if s not in seen)
    if orphan:
        print("\n⚠ 표에서 못 찾은 토픽 (슬러그 칸이 비었거나 행이 없다):")
        for slug in orphan:
            print(f"  {slug} — 유도된 상태 {states[slug]}")
    drift += len(changes) + len(orphan)
    if changes and not check:
        BACKLOG.write_text("".join(lines), encoding="utf-8")

    # 2. 현역 STATUS.md 머리글
    for status, now, want in status_drift():
        print(f"  머리글 {status.parent.name}: {now} → {want}")
        drift += 1
        if not check:
            text = status.read_text(encoding="utf-8")
            status.write_text(_STATUS_HEAD.sub(f"# STATUS: {want}", text, count=1), encoding="utf-8")

    # 3. 반려 편 보관
    for topic in rejected_active():
        drift += 1
        if check:
            print(f"  보관 {topic.name}: 반려됐는데 현역에 있다")
        else:
            print(f"  보관 {topic.name}: → topics/_archive/ ({archive(topic)})")

    if not drift:
        print("어긋난 것이 없다.")
        return 0
    if check:
        print(f"\n{drift}건이 어긋나 있다. 고치려면 --check 없이 돌린다.")
        return 1
    print(f"\n{drift}건을 맞췄다.")
    return 0


def main() -> int:
    return sync(check="--check" in sys.argv)


if __name__ == "__main__":
    sys.exit(main())
