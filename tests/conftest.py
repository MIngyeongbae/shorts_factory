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


#: 실제 호출이 돈을 쓰는 어댑터의 키. 테스트에서는 **항상** 지운다.
PAID_CREDENTIALS = ("GEMINI_API_KEY", "ELEVENLABS_API_KEY", "KLING_API_KEY")


@pytest.fixture(autouse=True)
def _no_paid_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """개발 머신의 API 키가 테스트로 새지 않게 막는다.

    실물 어댑터는 키가 없으면 호출 전에 `ProviderNotConfigured`로 멈춘다. 그래서
    키를 지워 두면 **테스트가 실수로 과금 호출을 하는 경로 자체가 사라진다** —
    편당 ~$2.7이 걸린 문제라 개별 테스트의 규율에 맡기지 않고 여기서 못박는다.
    호출 경로를 검증하는 테스트는 페이크 transport를 명시적으로 주입한다.
    """
    for name in PAID_CREDENTIALS:
        monkeypatch.delenv(name, raising=False)


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


#: 실제 토픽 패키지. 2부 테스트는 여기서 06-script.json만 가져다 쓴다 (ADR-0017).
REPO_TOPICS = Path(__file__).resolve().parents[1] / "topics"

#: 1부가 검증까지 끝낸 대본 2편. 2부의 입력 픽스처다.
PISA = "pisaui-satap-jiban-bogang"
HOOVER = "hubeodaem-konkeuriteu-naenggak"


def load_script(slug: str) -> dict:
    """`topics/{slug}/06-script.json` 원본."""
    return json.loads(
        (REPO_TOPICS / slug / "06-script.json").read_text(encoding="utf-8")
    )


def install_script(paths: Paths, slug: str) -> Path:
    """격리된 루트에 경계면 파일 **하나만** 놓는다.

    2부는 `06-script.json` 말고 1부 산출물에 의존하지 않는다 (ADR-0017). 팩트시트도
    후보도 topic.json도 없는 상태에서 단계가 끝까지 도는지가 이 헬퍼의 요점이다.
    """
    source = REPO_TOPICS / slug / "06-script.json"
    dest = paths.topic_dir(slug) / "06-script.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(source.read_bytes())
    return dest
