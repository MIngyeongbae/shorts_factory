"""`topics/{slug}/judgment/human.json` — 사람의 판정 파일에서 2부가 읽는 값. ADR-0059.

스펙 07이 파일의 스키마를 들고 있다 (JSON 예시가 정본 — 별도 schema 파일은 없다). 2부가
여기서 읽는 것은 **영상 라인(`video_line`)** 하나다 — `decision: go` 게이트 자체는 현재
`STATUS.md`(`stages/status.py`, ADR-0009)가 맡고 있어 이 모듈은 건드리지 않는다.

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

from .config import Paths
from .schemas import vocab

#: 판정 디렉터리·파일 (스펙 07).
JUDGMENT_DIR = "judgment"
HUMAN_FILE = "human.json"
#: human.json의 라인 필드 (ADR-0059 결정 2).
VIDEO_LINE_FIELD = "video_line"

_RUN_ID_RE = re.compile(r"^\d{8}-(?P<slug>.+)$")


class JudgmentError(Exception):
    """human.json을 읽을 수 없거나 값이 계약 밖이다."""


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
