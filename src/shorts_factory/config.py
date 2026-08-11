"""경로 및 실행 설정.

파이프라인 산출물 배치는 specs/05-pipeline.md, specs/06-topic-research.md를 따른다.
- topics/{slug}/  : 토픽 패키지 (사람이 읽는 산출물, ADR-0009)
- runs/{run_id}/  : 단계 간 JSON 계약 + 실행 상태 (재시작 가능성)
- knowledge/      : 소스 카드 라이브러리 (토픽 간 누적 자산, ADR-0012)
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


# --- API 키 (.env) -----------------------------------------------------------
#
# `python-dotenv`을 쓰지 않는다 (ADR-0021). 파싱이 아래 20줄이고, 현재 의존성은
# `jsonschema`·`pytest` 두 줄뿐이다. 키를 읽는 곳은 어댑터 생성자 하나다.


def parse_dotenv(text: str) -> dict[str, str]:
    """`.env` 본문 → 키/값. 주석·빈 줄은 건너뛰고 값의 감싼 따옴표만 벗긴다."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """`.env`를 읽어 **아직 없는 환경변수만** 채운다.

    이미 설정된 환경변수가 이긴다 — 테스트와 CI가 파일을 건드리지 않고 값을
    갈아끼울 수 있어야 한다. 파일이 없으면 조용히 아무것도 하지 않는다
    (키가 필요한 시점에 `require_env`가 무엇이 없는지 말해 준다).

    임포트 시점이 아니라 CLI 진입에서 한 번 부른다. 임포트만으로 프로세스
    환경이 바뀌면 테스트가 서로를 오염시킨다.
    """
    path = path or project_root() / ".env"
    if not path.is_file():
        return {}
    values = parse_dotenv(path.read_text(encoding="utf-8"))
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values


class MissingCredential(RuntimeError):
    """필요한 키가 환경에도 `.env`에도 없다."""


def require_env(name: str, *, purpose: str) -> str:
    """키를 읽거나, 무엇을 어디에 넣어야 하는지 말하고 실패한다.

    빈 문자열도 없는 것으로 친다 — `.env`에 `KEY=`만 적어 둔 자리를 유효한 키로
    읽으면 인증 오류가 나서야 원인을 찾게 된다.
    """
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise MissingCredential(
            f"{name}이(가) 비어 있다 ({purpose}). "
            f"프로젝트 루트의 .env에 {name}=... 을 넣어라 (형식은 .env.example 참고)."
        )
    return value


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

    @property
    def knowledge(self) -> Path:
        """소스 카드 라이브러리. run·토픽에 종속되지 않는 누적 자산 (ADR-0012)."""
        return self.root / "knowledge"

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
