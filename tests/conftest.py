from __future__ import annotations

import json
from pathlib import Path

import pytest

from shorts_factory.config import Paths

FIXTURES = Path(__file__).parent / "fixtures"

BACKLOG_TEMPLATE = """# 소재 백로그

| 소재 | 뒤집기 | 실패대안 | 숫자 | 현재접점 | 출처 후보 | 상태 |
|---|---|---|---|---|---|---|
| 한양도성 각자성석 | ✅ | ✅ | ✅ | ✅ | 실록, 국가유산포털 | 후보 |
| 미완성 소재 | ✅ | ❌ | ✅ |  | | 후보 |
"""


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    """격리된 프로젝트 루트."""
    (tmp_path / "topics").mkdir()
    (tmp_path / "runs").mkdir()
    (tmp_path / "topics" / "backlog.md").write_text(BACKLOG_TEMPLATE, encoding="utf-8")
    return Paths(tmp_path)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")
