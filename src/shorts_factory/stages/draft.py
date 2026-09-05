"""[1. draft] — 시드 기사를 읽고 통짜 내레이션을 쓴다 (ADR-0049).

specs/05-pipeline.md:
    [1. draft] → topics/{slug}/script.md  (헤드리스 1회 — 시드 기사를 읽고 통짜 내레이션.
    직후 오케스트레이터가 분량 엔벨로프를 기계 검사한다 — LLM 0회)

씬 계획·글자 예산·사실 배분을 먼저 정하고 빈칸을 채우는 옛 방식은
폐기됐다 — 대본은 통짜로 먼저 쓰고, 씬 분할·연출은 TTS 실측 뒤 `[3s]`가 한다.
서사 원칙·포맷의 정본은 specs/01이고 프롬프트(prompts/01-draft.md)가 그것을 나른다.

매체 적합성(그림이 이해를 보조하는가)도 이 세션이 판정한다 — 부적합이면 대본 대신
반려 사유를 낸다 (스펙 06. `[0. seed]`는 반려하지 않는다).

실패는 보고·중단이다 — 재생성 루프는 없다 (ADR-0044 원칙 유지). 검증에 실패해도
script.md는 남긴다 — 사람이 읽고 고칠지 다시 돌릴지 정한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Paths, write_text
from ..llm.base import LLMClient
from ..runstate import RunState, find_run_for_slug
from . import status as status_mod
from .scriptmd import (
    WEB_TOOLS,
    check_script_md,
    format_limits,
    load_prompt,
    parse_script_md,
    render_gate_block,
    strip_code_fence,
)
from .seedfetch import SEED_BODY_FILE  # 파일명 계약이다 — 단계를 부르지 않는다 (D-1)

log = logging.getLogger(__name__)

STAGE = "1-draft"
SCRIPT_FILE = "script.md"
PROMPT_FILE = "01-draft.md"

#: 프롬프트에 싣는 시드 본문의 상한. `[0f]`는 본문 추출을 안 하므로 내비게이션이
#: 섞여 들어온다 — 석빙고 문서가 46,874자였다 (ADR-0061). 세션 입력이 무한히
#: 커지지 않게만 자른다.
SEED_BODY_MAX_CHARS = 120_000

#: `seed-body.md`가 있을 때. 사다리의 첫 칸이다 (specs/05 `[1]` 규칙, ADR-0061).
SEED_BODY_PRESENT = """아래 「시드 기사 본문」이 그 기사를 헤드리스 브라우저로 렌더한 텍스트다 —
WebFetch로는 본문이 오지 않는 문서가 있어 `[0f]`가 미리 긁어 두었다. **이것을 먼저 읽어라.**
본문 추출을 하지 않았으므로 목차·분류·다른 문서 목록·푸터가 섞여 있다. 그 소재를 서술하는
문단만 읽고 나머지는 무시하라. 여기에 본문이 없거나 너무 얇으면 그때 WebFetch·WebSearch로
같은 소재의 위키백과·나무위키 문서를 찾아 본문을 확보하라.

=== 시드 기사 본문 시작 ===
{body}
=== 시드 기사 본문 끝 ==="""

#: `seed-body.md`가 없을 때 — 부재는 경고가 아니다 (D-3). 사다리의 나머지 칸.
SEED_BODY_ABSENT = """WebFetch로 시드 기사를 읽어라. 열리지 않으면 WebSearch로 같은 소재의
위키백과·나무위키 문서를 찾아 본문을 확보하라."""


def load_seed_body(topic_dir: Path) -> str:
    """`[0f]`가 남긴 시드 본문을 프롬프트에 실을 꼴로. 없으면 도구 사다리 안내다."""
    path = topic_dir / SEED_BODY_FILE
    if not path.exists():
        return SEED_BODY_ABSENT
    body = path.read_text(encoding="utf-8").strip()
    if not body:
        return SEED_BODY_ABSENT
    if len(body) > SEED_BODY_MAX_CHARS:
        body = body[:SEED_BODY_MAX_CHARS] + "\n…(잘림)"
    return SEED_BODY_PRESENT.format(body=body)

#: 기사 열람 + 집필을 한 세션에 하므로 생성 전용 단계보다 길게 잡는다.
TIMEOUT = 1500


class DraftStageError(Exception):
    pass


@dataclass
class DraftResult:
    topic: str
    slug: str
    run_id: str
    script_path: Path | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unfit: bool = False
    skipped: bool = False

    @property
    def passed(self) -> bool:
        return not self.errors

    @property
    def summary(self) -> str:
        tail = " (스킵)" if self.skipped else ""
        if self.unfit:
            return f"[1] {self.topic} — 매체 부적합 반려. 사유는 {SCRIPT_FILE}에 있다{tail}"
        if self.passed:
            return f"[1] {self.topic} — 통짜 대본 → {SCRIPT_FILE} (기계 검사 통과){tail}"
        return (
            f"[1] {self.topic} — 기계 검사 실패 {len(self.errors)}건. "
            f"{SCRIPT_FILE}은 남겼다 — 다시 돌릴지는 사람이 정한다 (ADR-0044){tail}"
        )


def run_draft_stage(
    slug: str,
    *,
    llm: LLMClient,
    paths: Paths | None = None,
    run_id: str | None = None,
    force: bool = False,
    timeout: int = TIMEOUT,
) -> DraftResult:
    paths = paths or Paths.from_env()

    if run_id:
        import json

        contract_path = paths.run_dir(run_id) / "topic.json"
        if not contract_path.exists():
            raise DraftStageError(f"topic.json이 없다: {contract_path}")
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    else:
        run_id, contract = find_run_for_slug(paths, slug)

    topic = contract["topic"]
    seed_url = contract.get("seed_url", "")
    topic_dir = paths.topic_dir(slug)
    run_dir = paths.run_dir(run_id)
    state = RunState.load_or_create(run_dir, run_id, topic=topic, slug=slug)

    script_path = topic_dir / SCRIPT_FILE
    if state.is_done(STAGE) and not force and script_path.exists():
        log.info("[%s] 이미 완료된 단계라 스킵한다 (run_id=%s)", STAGE, run_id)
        return DraftResult(topic=topic, slug=slug, run_id=run_id,
                           script_path=script_path, skipped=True)

    if not seed_url:
        raise DraftStageError(
            "시드 기사 URL이 없다 — 백로그의 '시드' 컬럼 또는 "
            "`topic --seed-url`로 지정하고 [0. seed]를 다시 돌려라 (specs/06)."
        )

    state.mark_running(STAGE)
    try:
        prompt = load_prompt(PROMPT_FILE).substitute(
            topic=topic,
            seed_url=seed_url,
            seed_body=load_seed_body(topic_dir),
            limits=format_limits(
                "total_chars", "line_count", "line_chars_target",
                "line_chars_max", "total_seconds",
            ),
        )
        result = llm.run(prompt, allowed_tools=WEB_TOOLS, timeout=timeout, label=STAGE)
        text = strip_code_fence(result.text)
        if not text.startswith("#"):
            raise DraftStageError(
                f"세션 출력이 script.md 모양이 아니다 (첫 글자 {text[:20]!r})"
            )
        # 판정 블록을 전부 주석인 채로 앞에 붙인다 (ADR-0094) — 사람은 주석만 푼다.
        write_text(script_path, render_gate_block() + text.rstrip() + "\n")
    except DraftStageError as exc:
        state.mark_failed(STAGE, str(exc))
        raise
    except Exception as exc:  # 세션 오류 등 — run 디렉터리에 기록하고 전파 (specs/05)
        state.mark_failed(STAGE, f"{type(exc).__name__}: {exc}")
        raise

    errors, warnings = check_script_md(script_path.read_text(encoding="utf-8"))
    unfit = parse_script_md(script_path.read_text(encoding="utf-8")).unfit
    info = {
        "output": script_path.relative_to(paths.root).as_posix(),
        "validation_errors": errors,
        "validation_warnings": warnings,
    }

    if errors:
        # 보고·중단 (ADR-0044). 산출물은 남긴다 — 사람이 읽는다.
        message = (
            "매체 부적합 반려" if unfit else f"기계 검사 실패 {len(errors)}건"
        )
        state.mark_failed(STAGE, message, **info)
        return DraftResult(topic=topic, slug=slug, run_id=run_id,
                           script_path=script_path, errors=errors,
                           warnings=warnings, unfit=unfit)

    state.mark_done(STAGE, **info)
    status_mod.sync_checklist(topic_dir, state)
    return DraftResult(topic=topic, slug=slug, run_id=run_id,
                       script_path=script_path, warnings=warnings)
