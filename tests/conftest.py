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


#: 씬 계약 픽스처 2편 — 실물 대본에서 동결한 사본이다 (ADR-0052 — 리포의 옛
#: `topics/` 산출물은 폐기 대상이라 테스트가 그쪽을 읽으면 안 된다).
PISA = "pisaui-satap-jiban-bogang"
HOOVER = "hubeodaem-konkeuriteu-naenggak"

_CONTRACT_FIXTURES = {
    HOOVER: "contract_hoover.json",
    PISA: "contract_pisa.json",
}


def load_script(slug: str) -> dict:
    """씬 계약 픽스처 (`tests/fixtures/contract_*.json`)."""
    return load_fixture(_CONTRACT_FIXTURES[slug])


def install_script(paths: Paths, slug: str) -> Path:
    """격리된 루트에 씬 계약을 새 경로(`runs/{run_id}/scenes.json`)로 놓는다 (ADR-0052).

    slug→run 해석이 `runs/*/topic.json` 하나이므로 (stages/contract.py) topic.json도
    함께 놓는다 — 이 둘이 2부 소비자 단계가 아는 전부다.
    """
    from shorts_factory.config import write_text
    from shorts_factory.jsonio import dump_json

    document = load_script(slug)
    run_id = document["run_id"]
    run_dir = paths.run_dir(run_id)
    write_text(
        run_dir / "topic.json",
        dump_json({"run_id": run_id, "slug": slug, "topic": document["topic"]}),
    )
    dest = run_dir / "scenes.json"
    write_text(dest, dump_json(document))
    return dest
