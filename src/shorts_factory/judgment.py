"""사람의 판정에서 2부가 읽는 값 — 판정 블록(ADR-0094)과 영상 라인(ADR-0059).

**판정은 `topics/{slug}/script.md` 맨 위 주석 블록이다** (`gate.py`, 스펙 07). `read_gate`가
그것을 읽고 `languages_for`가 2부 단계의 언어 목록을 정한다 — 명시(`--lang`) > 판정 >
옛 편(블록 없음)은 있는 언어 전부. `human.json`에서 읽는 것은 **영상 라인(`video_line`)**
하나다 — 옛 `decision`은 폐기됐다 (ADR-0094 결정 6).

- 파일이 없거나 필드가 비어 있으면 어휘의 기본 라인이다 (`vocab.video_line_default()`,
  ADR-0059 결정 2 "비우면 local")
- 값이 어휘 밖이면 **실패**한다 — 사람이 오타를 냈는데 기본 라인으로 조용히 내려가면
  돈이 나가는 쪽(`api`)과 안 나가는 쪽이 뒤바뀐다
- run_id만 아는 호출 경로(`[7] --run-id`)는 `slug_from_run_id()`로 슬러그를 되찾는다 —
  run_id는 `YYYYMMDD-{slug}`다 (`config.make_run_id`)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Sequence

from .config import Paths
from .gate import Gate, GateError, parse_gate
from .schemas import vocab
from .stages_script_name import SCRIPT_MD_FILE

#: 판정 디렉터리·파일 (스펙 07).
JUDGMENT_DIR = "judgment"
HUMAN_FILE = "human.json"
#: human.json의 라인 필드 (ADR-0059 결정 2).
VIDEO_LINE_FIELD = "video_line"

_RUN_ID_RE = re.compile(r"^\d{8}-(?P<slug>.+)$")


class JudgmentError(Exception):
    """판정을 읽을 수 없거나(블록·human.json) 값이 계약 밖이다, 또는 판정이 2부를 막는다."""


def read_gate(paths: Paths, slug: str) -> Gate:
    """`script.md` 맨 위 블록의 판정. 파일이나 블록이 없으면 옛 편(`present=False`)이다."""
    path = paths.topic_dir(slug) / SCRIPT_MD_FILE
    if not path.exists():
        return Gate()
    try:
        return parse_gate(path.read_text(encoding="utf-8"))
    except GateError as exc:
        raise JudgmentError(f"{path}: {exc}") from exc


def languages_for(
    paths: Paths, run_id: str, *, langs: Sequence[str] | None, present: Sequence[str],
    slug: str | None = None,
) -> list[str]:
    """2부 단계가 돌 언어 — **명시 > 판정 > 있는 것 전부** (ADR-0094 결정 4).

    `langs`가 오면 그대로다(`--lang`은 판정보다 세다 — 디버깅·재조립 자리). 없으면 판정
    블록이 고른 언어 중 `present`에 있는 것이고, 블록이 없는 옛 편은 `present` 전부다.
    반려·보류면 **멈춘다** — 조용히 전부를 돌리면 사람이 안 고른 언어를 산다.
    """
    if langs:
        return list(langs)
    if slug is None:
        try:
            slug = slug_from_run_id(run_id)
        except JudgmentError:
            return list(present)
    gate = read_gate(paths, slug)
    if gate.rejected:
        raise JudgmentError(
            f"{slug}: 반려된 편이다 — script.md 맨 위 블록의 reject가 풀려 있다 (ADR-0094)"
        )
    if gate.pending:
        raise JudgmentError(
            f"{slug}: 보류 — script.md 맨 위 블록에서 언어(ko·ja·en)의 주석을 풀어야 2부가 돈다 "
            "(ADR-0094). 명시하려면 --lang"
        )
    if not gate.present:
        return list(present)
    return [lang for lang in gate.languages if lang in present]


def slug_from_run_id(run_id: str) -> str:
    """`YYYYMMDD-{slug}` → slug. 모양이 다르면 실패한다 — 추측하지 않는다."""
    match = _RUN_ID_RE.match(run_id)
    if not match:
        raise JudgmentError(f"run_id가 YYYYMMDD-slug 꼴이 아니다: {run_id!r}")
    return match.group("slug")


def human_path(paths: Paths, slug: str) -> Path:
    return paths.topic_dir(slug) / JUDGMENT_DIR / HUMAN_FILE


def read_video_line(paths: Paths, slug: str) -> str:
    """사람이 고른 영상 라인. 없으면 기본 라인, 어휘 밖이면 `JudgmentError`."""
    path = human_path(paths, slug)
    if not path.exists():
        return vocab.video_line_default()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise JudgmentError(f"{path}를 읽을 수 없다: {exc}") from exc
    if not isinstance(document, dict):
        raise JudgmentError(f"{path}가 JSON 객체가 아니다")
    value = document.get(VIDEO_LINE_FIELD)
    if value in (None, ""):
        return vocab.video_line_default()
    if not isinstance(value, str) or value not in vocab.values("video_line"):
        raise JudgmentError(
            f"{path}의 {VIDEO_LINE_FIELD}={value!r}는 어휘 밖이다 "
            f"(있는 값: {', '.join(vocab.values('video_line'))}) — 기본 라인으로 내려가지 않는다"
        )
    return value
