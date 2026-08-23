"""[0f. seedfetch] 단계 계약 (ADR-0061).

브라우저는 페이크다 — 여기서 검증하는 것은 계약이다: 렌더 텍스트가 파일로 남는가,
**사람이 붙여넣은 본문을 덮어쓰지 않는가**, 실패가 파이프라인을 세우지 않는가(D-5),
그리고 `[1]`이 그 파일을 프롬프트에 싣는가.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from shorts_factory.stages import seedfetch
from shorts_factory.stages.draft import SEED_BODY_ABSENT, run_draft_stage
from shorts_factory.stages.seedfetch import (
    SEED_BODY_FILE,
    RenderError,
    extract_text,
    run_seedfetch_stage,
)
from shorts_factory.stages.topic import run_topic_stage

from test_scriptmd import make_lines, make_script

TODAY = date(2026, 8, 7)
SEED = "https://namu.wiki/w/각자성석"

DOM = """<html><head><title>각자성석</title>
<style>.x{color:red}</style><script>var a = 1 < 2;</script></head>
<body><div id="nav">분류 목록</div>
<h2>1. 개요</h2><p>각자성석(刻字城石)은 성벽에 새긴 &lt;공사 기록&gt;이다.</p>
<p>구간마다   담당 군현을 새겼다.<br>책임을 묻기 위해서다.</p>
</body></html>"""


class FakeLLM:
    def __init__(self, text: str):
        self.text = text
        self.prompts: list[str] = []

    def run(self, prompt, *, allowed_tools=(), timeout=None, label="", **_kw):
        self.prompts.append(prompt)
        return SimpleNamespace(text=self.text)


@pytest.fixture
def seeded(paths):
    return run_topic_stage("한양도성 각자성석", paths=paths, today=TODAY, seed_url=SEED)


@pytest.fixture
def fake_browser(monkeypatch):
    """브라우저를 찾은 셈 치고 DOM을 돌려준다. 네트워크도 서브프로세스도 없다."""

    def install(dom: str = DOM):
        monkeypatch.setattr(seedfetch, "find_browser", lambda *_a, **_kw: Path("chrome.exe"))
        monkeypatch.setattr(seedfetch, "render_dom", lambda *_a, **_kw: dom)

    return install


# --- extract_text: 태그만 벗긴다. 본문 추출이 아니다 (ADR-0061) ---

def test_extract_drops_script_and_style_but_keeps_prose():
    text = extract_text(DOM)

    assert "var a" not in text and "color:red" not in text
    assert "각자성석(刻字城石)은 성벽에 새긴 <공사 기록>이다." in text  # 엔티티 복원
    assert "구간마다 담당 군현을 새겼다." in text  # 공백 정규화


def test_extract_keeps_navigation_noise():
    """추출하지 않는 것이 계약이다 — 본문 고르기는 `[1]`이 한다."""
    assert "분류 목록" in extract_text(DOM)


def test_extract_breaks_lines_on_block_tags():
    lines = extract_text(DOM).splitlines()

    assert "1. 개요" in lines
    assert "책임을 묻기 위해서다." in lines


# --- 단계 계약 ---

def test_writes_seed_body_with_header(paths, seeded, fake_browser):
    fake_browser()
    result = run_seedfetch_stage(seeded.slug, paths=paths, run_id=seeded.run_id)

    assert result.passed
    body = result.body_path.read_text(encoding="utf-8")
    assert result.body_path.name == SEED_BODY_FILE
    assert SEED in body  # 출처가 파일에 남는다
    assert "각자성석(刻字城石)은" in body
    assert result.chars > 0


def test_does_not_overwrite_a_human_written_body(paths, seeded, fake_browser):
    """사람이 붙여넣은 본문이 폴백이다 (ADR-0061 검토한 대안 1) — 덮어쓰면 폴백이 죽는다."""
    body_path = paths.topic_dir(seeded.slug) / SEED_BODY_FILE
    body_path.write_text("사람이 붙여넣은 본문", encoding="utf-8")

    fake_browser()
    result = run_seedfetch_stage(seeded.slug, paths=paths, run_id=seeded.run_id)

    assert result.skipped and result.passed
    assert body_path.read_text(encoding="utf-8") == "사람이 붙여넣은 본문"


def test_force_replaces_the_body(paths, seeded, fake_browser):
    body_path = paths.topic_dir(seeded.slug) / SEED_BODY_FILE
    body_path.write_text("낡은 본문", encoding="utf-8")

    fake_browser()
    result = run_seedfetch_stage(seeded.slug, paths=paths, run_id=seeded.run_id, force=True)

    assert result.passed and not result.skipped
    assert "각자성석(刻字城石)은" in body_path.read_text(encoding="utf-8")


def test_missing_browser_warns_and_does_not_raise(paths, seeded, monkeypatch):
    """D-5 — 실패는 단계 안에서 끝난다. `[1]`이 WebFetch로 내려간다."""
    monkeypatch.setattr(seedfetch, "find_browser", lambda *_a, **_kw: None)
    result = run_seedfetch_stage(seeded.slug, paths=paths, run_id=seeded.run_id)

    assert not result.passed and result.warnings
    assert not (paths.topic_dir(seeded.slug) / SEED_BODY_FILE).exists()


def test_render_failure_warns_and_writes_nothing(paths, seeded, monkeypatch):
    def boom(*_a, **_kw):
        raise RenderError("브라우저가 1로 끝났다")

    monkeypatch.setattr(seedfetch, "find_browser", lambda *_a, **_kw: Path("chrome.exe"))
    monkeypatch.setattr(seedfetch, "render_dom", boom)
    result = run_seedfetch_stage(seeded.slug, paths=paths, run_id=seeded.run_id)

    assert not result.passed and result.warnings
    assert not (paths.topic_dir(seeded.slug) / SEED_BODY_FILE).exists()


def test_empty_render_is_a_failure_not_an_empty_file(paths, seeded, fake_browser):
    """빈 파일을 남기면 `[1]`이 파일이 있다는 이유로 사다리를 안 내려간다."""
    fake_browser("<html><body><script>x</script></body></html>")
    result = run_seedfetch_stage(seeded.slug, paths=paths, run_id=seeded.run_id)

    assert not result.passed
    assert not (paths.topic_dir(seeded.slug) / SEED_BODY_FILE).exists()


# --- [1]과의 배선: 사다리 첫 칸 ---

def test_draft_loads_the_seed_body_into_the_prompt(paths, seeded, fake_browser):
    fake_browser()
    run_seedfetch_stage(seeded.slug, paths=paths, run_id=seeded.run_id)

    llm = FakeLLM(make_script(make_lines()))
    run_draft_stage(seeded.slug, llm=llm, paths=paths, run_id=seeded.run_id)

    assert "각자성석(刻字城石)은" in llm.prompts[0]
    assert "=== 시드 기사 본문 끝 ===" in llm.prompts[0]


def test_draft_falls_back_when_there_is_no_seed_body(paths, seeded):
    """부재는 경고가 아니다 (D-3) — 도구 사다리로 내려간다."""
    llm = FakeLLM(make_script(make_lines()))
    run_draft_stage(seeded.slug, llm=llm, paths=paths, run_id=seeded.run_id)

    assert SEED_BODY_ABSENT in llm.prompts[0]
    assert "=== 시드 기사 본문 시작 ===" not in llm.prompts[0]
