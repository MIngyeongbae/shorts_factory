"""[0f. seedfetch] — 시드 URL을 헤드리스 브라우저로 렌더해 본문 텍스트를 남긴다 (ADR-0061).

specs/05-pipeline.md:
    [0f. seedfetch] → topics/{slug}/seed-body.md (기계 단계, LLM 0회)

나무위키 같은 클라이언트 렌더 SPA는 WebFetch로 본문이 오지 않는다 — 서버가 주는 HTML에는
카테고리·내비게이션 템플릿만 있고 본문은 브라우저가 그린 뒤에야 존재한다 (실측: `/w/석빙고`는
내비게이션만, `/raw/석빙고`는 `Loading...` 한 줄). 그래서 **이미 설치된** Chrome/Edge를
`--headless=new --dump-dom`으로 불러 렌더된 DOM을 받는다 — 새 파이썬 의존성이 없다.

**본문 추출을 하지 않는다.** 사이트별 셀렉터는 사이트마다 깨지므로 내비게이션째로 넘기고,
본문을 고르는 일은 어차피 글을 읽는 `[1]`이 한다 (ADR-0061 검토한 대안 5).

**실패는 이 단계 안에서 끝난다** (D-5) — 브라우저가 없거나 렌더가 막히면 경고만 남기고
파일을 쓰지 않는다. `[1]`이 WebFetch → WebSearch 사다리로 내려간다.

**이미 있는 파일은 덮어쓰지 않는다** — 사람이 본문을 붙여넣었을 수 있고, 그것이 헤드리스가
막히는 사이트의 폴백이다 (ADR-0061 검토한 대안 1). 다시 긁으려면 `--force`.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import Paths, write_text
from ..runstate import RunState, find_run_for_slug
from . import status as status_mod

log = logging.getLogger(__name__)

STAGE = "0f-seedfetch"
SEED_BODY_FILE = "seed-body.md"

#: SPA는 하이드레이션이 끝나야 본문이 선다. 실측(ADR-0061): 위키백과 2.4초,
#: 나무위키 문서 61.5초. 가상 시각 예산은 렌더 대기이고 timeout은 그 바깥의 안전망이다.
VIRTUAL_TIME_BUDGET_MS = 20_000
TIMEOUT = 180

#: 파일에 남기는 글자 수 상한. 석빙고 문서가 46,874자였다 — 넉넉하되 무한은 아니다.
MAX_CHARS = 300_000

#: 브라우저를 손으로 지정하는 환경변수. 있으면 탐색을 건너뛴다.
BROWSER_ENV = "SEEDFETCH_BROWSER"

#: PATH에서 찾을 이름들 (win32 밖에서도 돌게).
_PATH_NAMES = ("chrome", "google-chrome", "chromium", "chromium-browser", "msedge")

#: Windows 설치 경로. Chrome을 먼저 본다 — Edge는 폴백이다.
_WINDOWS_SUFFIXES = (
    Path("Google/Chrome/Application/chrome.exe"),
    Path("Microsoft/Edge/Application/msedge.exe"),
)
_WINDOWS_ROOTS = ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")

_DROP_RE = re.compile(r"(?is)<(script|style|noscript|template)\b[^>]*>.*?</\1\s*>")
_BLOCK_RE = re.compile(
    r"(?i)</(p|div|li|tr|td|th|h[1-6]|section|article|blockquote|dt|dd|pre)\s*>"
    r"|<br\s*/?>|<hr\s*/?>"
)
_TAG_RE = re.compile(r"(?s)<[^>]+>")

HEADER = """# 시드 본문 — {topic}

- 출처: {url}
- 수집: {stamp} (headless {browser})
- 글자 수: {chars}

> `[0f] seedfetch`가 렌더한 텍스트다 (ADR-0061). **본문 추출을 하지 않으므로** 목차·분류·
> 다른 문서 목록·푸터가 섞여 있다 — 그 소재를 서술하는 문단만 읽고 나머지는 무시하라.
> 이 파일은 **시드**이지 근거가 아니다. 주장의 근거는 `[2]`가 다른 곳에서 확인한다
> (스펙 06 소스 지위 표).
> 사람이 직접 본문을 붙여넣어도 된다 — 헤드리스가 막히는 사이트의 폴백이다.

---

"""


class SeedfetchStageError(Exception):
    """단계를 시작조차 못 하는 오류 (run 계약 부재 등). 렌더 실패는 여기 오지 않는다."""


class RenderError(Exception):
    """렌더가 실패했다. D-5에 따라 경고로 끝나고 파이프라인을 세우지 않는다."""


@dataclass
class SeedfetchResult:
    topic: str
    slug: str
    run_id: str
    body_path: Path | None = None
    chars: int = 0
    browser: str = ""
    skipped: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.body_path is not None

    @property
    def summary(self) -> str:
        if self.skipped:
            return (
                f"[0f] {self.topic} — {SEED_BODY_FILE}가 이미 있다 ({self.chars:,}자). "
                "덮어쓰지 않는다 — 다시 긁으려면 --force"
            )
        if self.passed:
            return (
                f"[0f] {self.topic} — 시드 본문 {self.chars:,}자 → {SEED_BODY_FILE} "
                f"({self.browser})"
            )
        return (
            f"[0f] {self.topic} — 시드 본문을 못 얻었다. "
            "[1]이 WebFetch → WebSearch로 내려간다 (ADR-0061)"
        )


def find_browser(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """헤드리스로 부를 브라우저. 못 찾으면 None — 부재는 이 단계의 실패지 오류가 아니다."""
    for candidate in (explicit, os.environ.get(BROWSER_ENV)):
        if candidate:
            path = Path(candidate)
            if path.exists():
                return path
            log.warning("[%s] 지정한 브라우저가 없다: %s", STAGE, path)

    for name in _PATH_NAMES:
        found = shutil.which(name)
        if found:
            return Path(found)

    for root in _WINDOWS_ROOTS:
        base = os.environ.get(root)
        if not base:
            continue
        for suffix in _WINDOWS_SUFFIXES:
            path = Path(base) / suffix
            if path.exists():
                return path
    return None


def render_dom(
    url: str,
    browser: Path,
    *,
    timeout: int = TIMEOUT,
    budget_ms: int = VIRTUAL_TIME_BUDGET_MS,
) -> str:
    """헤드리스로 URL을 렌더해 DOM 문자열을 돌려준다.

    프로필은 임시 디렉터리다 — 사람이 쓰는 브라우저 프로필을 건드리면 실행 중인
    창과 충돌한다.
    """
    profile = Path(tempfile.mkdtemp(prefix="seedfetch-"))
    cmd = [
        str(browser),
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        f"--user-data-dir={profile}",
        f"--virtual-time-budget={budget_ms}",
        "--dump-dom",
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"렌더가 {timeout}초 안에 안 끝났다: {url}") from exc
    except OSError as exc:
        raise RenderError(f"브라우저를 실행하지 못했다: {exc}") from exc
    finally:
        shutil.rmtree(profile, ignore_errors=True)

    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()[-1:]
        detail = f" — {tail[0]}" if tail else ""
        raise RenderError(f"브라우저가 {proc.returncode}로 끝났다{detail}")
    return proc.stdout.decode("utf-8", errors="replace")


def extract_text(dom: str) -> str:
    """DOM → 사람이 읽는 텍스트. **본문 추출이 아니다** — 태그만 벗긴다 (ADR-0061)."""
    text = _DROP_RE.sub(" ", dom)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    lines = []
    for raw in text.split("\n"):
        line = re.sub(r"\s+", " ", raw).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _read_chars(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8"))
    except OSError:
        return 0


def run_seedfetch_stage(
    slug: str,
    *,
    paths: Paths | None = None,
    run_id: str | None = None,
    force: bool = False,
    browser: str | os.PathLike[str] | None = None,
    timeout: int = TIMEOUT,
) -> SeedfetchResult:
    paths = paths or Paths.from_env()

    if run_id:
        contract_path = paths.run_dir(run_id) / "topic.json"
        if not contract_path.exists():
            raise SeedfetchStageError(f"topic.json이 없다: {contract_path}")
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    else:
        run_id, contract = find_run_for_slug(paths, slug)

    topic = contract["topic"]
    seed_url = contract.get("seed_url", "")
    topic_dir = paths.topic_dir(slug)
    body_path = topic_dir / SEED_BODY_FILE
    state = RunState.load_or_create(paths.run_dir(run_id), run_id, topic=topic, slug=slug)

    result = SeedfetchResult(topic=topic, slug=slug, run_id=run_id)

    if body_path.exists() and not force:
        # 사람이 붙여넣었을 수 있다 (ADR-0061 검토한 대안 1) — 덮어쓰지 않는다.
        result.body_path = body_path
        result.chars = _read_chars(body_path)
        result.skipped = True
        state.mark_done(STAGE, output=body_path.relative_to(paths.root).as_posix(),
                        chars=result.chars, source="existing")
        status_mod.sync_checklist(topic_dir, state)
        return result

    if not seed_url:
        # 시드 URL이 없는 것은 `[1]`이 잡는 오류다. 여기서는 할 일이 없을 뿐이다.
        result.warnings.append("시드 기사 URL이 없다 — 렌더할 것이 없다")
        state.mark_failed(STAGE, "seed_url 없음")
        return result

    exe = find_browser(browser)
    if exe is None:
        result.warnings.append(
            "헤드리스로 부를 브라우저를 못 찾았다 — Chrome/Edge를 깔거나 "
            f"{BROWSER_ENV}에 실행 파일 경로를 지정하라"
        )
        state.mark_failed(STAGE, "브라우저 없음")
        return result

    state.mark_running(STAGE)
    try:
        dom = render_dom(seed_url, exe, timeout=timeout)
        text = extract_text(dom)
    except RenderError as exc:
        result.warnings.append(f"{exc} — 사람이 {SEED_BODY_FILE}에 본문을 붙여넣어도 된다")
        state.mark_failed(STAGE, str(exc))
        return result

    if not text.strip():
        result.warnings.append(
            f"렌더는 됐는데 텍스트가 비었다 ({seed_url}) — "
            f"사람이 {SEED_BODY_FILE}에 본문을 붙여넣어도 된다"
        )
        state.mark_failed(STAGE, "렌더 텍스트 없음")
        return result

    truncated = len(text) > MAX_CHARS
    if truncated:
        text = text[:MAX_CHARS]
        result.warnings.append(f"{MAX_CHARS:,}자에서 잘랐다")

    header = HEADER.format(
        topic=topic,
        url=seed_url,
        stamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        browser=exe.name,
        chars=f"{len(text):,}" + ("(잘림)" if truncated else ""),
    )
    write_text(body_path, header + text.rstrip() + "\n")

    result.body_path = body_path
    result.chars = len(text)
    result.browser = exe.name
    state.mark_done(
        STAGE,
        output=body_path.relative_to(paths.root).as_posix(),
        chars=result.chars,
        browser=exe.name,
        url=seed_url,
        truncated=truncated,
    )
    status_mod.sync_checklist(topic_dir, state)
    return result
