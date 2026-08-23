"""[2. factcheck] — 대본이 실제로 쓴 주장만 검증·정정한다 (ADR-0049).

specs/05-pipeline.md:
    [2. factcheck] → topics/{slug}/factcheck.md + script.md 갱신
    (헤드리스 1회 — 대본이 실제로 쓴 주장만 검증·정정. 스펙 06)

전수 조사로 돌아가지 않는다 — 대본에 없는 주제는 검증 대상이 아니다 (호르무즈 편
실측 113:1이 폐기 근거다). 통념↔사실이 어긋나면 버그가 아니라 반전 소재로 명기한다.

세션 출력은 `=== FACTCHECK ===` / `=== SCRIPT ===` 두 절이다 (scriptmd.py 마커 계약).
SCRIPT 절이 없으면 대본 무변경으로 취급하고 경고만 단다. 정정된 대본도 같은 기계
검사를 다시 통과해야 한다 — 실패해도 산출물은 남긴다 (ADR-0044).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Paths, write_text
from ..llm.base import LLMClient
from ..runstate import RunState, find_run_for_slug
from . import status as status_mod
from .draft import SCRIPT_FILE
from .scriptmd import (
    WEB_TOOLS,
    check_script_md,
    format_limits,
    load_prompt,
    split_factcheck_output,
)

log = logging.getLogger(__name__)

STAGE = "2-factcheck"
FACTCHECK_FILE = "factcheck.md"
PROMPT_FILE = "02-factcheck.md"

TIMEOUT = 1500


class FactcheckStageError(Exception):
    pass


@dataclass
class FactcheckResult:
    topic: str
    slug: str
    run_id: str
    factcheck_path: Path | None = None
    script_changed: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False

    @property
    def passed(self) -> bool:
        return not self.errors

    @property
    def summary(self) -> str:
        tail = " (스킵)" if self.skipped else ""
        changed = "대본 정정 반영" if self.script_changed else "대본 무변경"
        if self.passed:
            return f"[2] {self.topic} — 팩트체크 → {FACTCHECK_FILE} ({changed}){tail}"
        return (
            f"[2] {self.topic} — 정정본이 기계 검사 실패 {len(self.errors)}건 ({changed}). "
            f"산출물은 남겼다 — 다시 돌릴지는 사람이 정한다 (ADR-0044){tail}"
        )


def run_factcheck_stage(
    slug: str,
    *,
    llm: LLMClient,
    paths: Paths | None = None,
    run_id: str | None = None,
    force: bool = False,
    timeout: int = TIMEOUT,
) -> FactcheckResult:
    paths = paths or Paths.from_env()

    if run_id:
        import json

        contract_path = paths.run_dir(run_id) / "topic.json"
        if not contract_path.exists():
            raise FactcheckStageError(f"topic.json이 없다: {contract_path}")
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    else:
        run_id, contract = find_run_for_slug(paths, slug)

    topic = contract["topic"]
    seed_url = contract.get("seed_url", "")
    topic_dir = paths.topic_dir(slug)
    run_dir = paths.run_dir(run_id)
    state = RunState.load_or_create(run_dir, run_id, topic=topic, slug=slug)

    factcheck_path = topic_dir / FACTCHECK_FILE
    script_path = topic_dir / SCRIPT_FILE
    if state.is_done(STAGE) and not force and factcheck_path.exists():
        log.info("[%s] 이미 완료된 단계라 스킵한다 (run_id=%s)", STAGE, run_id)
        return FactcheckResult(topic=topic, slug=slug, run_id=run_id,
                               factcheck_path=factcheck_path, skipped=True)

    if not script_path.exists():
        raise FactcheckStageError(
            f"대본이 없다: {script_path}. [1. draft]를 먼저 실행하라."
        )
    script_text = script_path.read_text(encoding="utf-8")

    state.mark_running(STAGE)
    try:
        prompt = load_prompt(PROMPT_FILE).substitute(
            topic=topic,
            seed_url=seed_url or "(시드 URL 미기재 — script.md 머리의 '시드' 참조)",
            script=script_text,
            limits=format_limits("total_chars", "line_count", "line_chars_max"),
        )
        result = llm.run(prompt, allowed_tools=WEB_TOOLS, timeout=timeout, label=STAGE)
        fact_text, new_script = split_factcheck_output(result.text)
        write_text(factcheck_path, fact_text)
        if new_script is not None:
            write_text(script_path, new_script.rstrip() + "\n")
    except Exception as exc:
        state.mark_failed(STAGE, f"{type(exc).__name__}: {exc}")
        raise

    warnings: list[str] = []
    if new_script is None:
        warnings.append("세션이 SCRIPT 절을 내지 않았다 — 대본 무변경으로 취급한다")

    errors, check_warnings = check_script_md(script_path.read_text(encoding="utf-8"))
    warnings.extend(check_warnings)
    info = {
        "output": factcheck_path.relative_to(paths.root).as_posix(),
        "script_changed": new_script is not None,
        "validation_errors": errors,
        "validation_warnings": warnings,
    }

    if errors:
        state.mark_failed(STAGE, f"정정본 기계 검사 실패 {len(errors)}건", **info)
        return FactcheckResult(topic=topic, slug=slug, run_id=run_id,
                               factcheck_path=factcheck_path,
                               script_changed=new_script is not None,
                               errors=errors, warnings=warnings)

    state.mark_done(STAGE, **info)
    status_mod.sync_checklist(topic_dir, state)
    return FactcheckResult(topic=topic, slug=slug, run_id=run_id,
                           factcheck_path=factcheck_path,
                           script_changed=new_script is not None,
                           warnings=warnings)
