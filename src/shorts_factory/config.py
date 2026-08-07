"""경로 및 실행 설정.

파이프라인 산출물 배치는 specs/05-pipeline.md, specs/06-topic-research.md를 따른다.
- topics/{slug}/  : 토픽 패키지 (사람이 읽는 산출물, ADR-0009)
- runs/{run_id}/  : 단계 간 JSON 계약 + 실행 상태 (재시작 가능성)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

#: 헤드리스 세션 1회의 기본 타임아웃(초). 웹 검색 다회를 감안한 값.
DEFAULT_LLM_TIMEOUT = 900

#: 사용 한도(rate limit) 도달 시 지수 백오프 기본 대기(초). ADR-0008.
DEFAULT_BACKOFF_BASE = 60

#: 한도 재시도 최대 횟수. 초과 시 run 상태에 기록하고 종료.
DEFAULT_MAX_RETRIES = 5


def project_root() -> Path:
    """리포지토리 루트. 테스트는 SHORTS_FACTORY_ROOT로 격리한다."""
    env = os.environ.get("SHORTS_FACTORY_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Paths:
    root: Path

    @classmethod
    def from_env(cls) -> "Paths":
        return cls(project_root())

    @property
    def topics(self) -> Path:
        return self.root / "topics"

    @property
    def backlog(self) -> Path:
        return self.topics / "backlog.md"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    def topic_dir(self, slug: str) -> Path:
        return self.topics / slug

    def run_dir(self, run_id: str) -> Path:
        return self.runs / run_id


def make_run_id(slug: str, when: date | None = None) -> str:
    """run_id = YYYYMMDD-{slug} (specs/05-pipeline.md의 `20260807-baekak` 형식)."""
    when = when or date.today()
    return f"{when:%Y%m%d}-{slug}"


def write_text(path: Path, text: str) -> None:
    """LF 고정으로 텍스트를 쓴다 (.gitattributes 정책과 일치)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
