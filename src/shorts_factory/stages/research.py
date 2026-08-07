"""[0b. research] — 조사 → 검증 → 비판 → 팩트시트.

specs/05-pipeline.md:
    [0b. research] → 01-research.md → 02-verify.md → 03-critique.md → 04-factsheet.json
                     (각각 독립 헤드리스 세션, ADR-0009)
    verdict: fail 시 백로그 반려하고 종료.

ADR-0009의 오염 방지 규칙: 비판(03) 세션에는 검증(02)의 결론이 아니라 조사(01)의
**원자료**를 준다. 앞 세션의 판단이 뒤 세션의 판단을 물들이지 않게 한다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any

from .. import backlog as backlog_mod
from .. import runstate
from ..backlog import STATUS_REJECTED
from ..config import Paths, write_text
from ..jsonio import JSONExtractionError, dump_json, extract_json_object
from ..knowledge import KnowledgeStore, extract_contract
from ..llm.base import LLMClient
from ..runstate import RunState
from ..schemas.factsheet import validate_factsheet
from . import status as status_mod

log = logging.getLogger(__name__)

STAGE = "0b-research"
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

WEB_TOOLS = ("WebSearch", "WebFetch")
#: 소스 카드를 읽어야 하는 세션 (ADR-0012). Read는 열어준 디렉터리에만 닿는다.
SOURCE_TOOLS = WEB_TOOLS + ("Read",)

#: 팩트시트가 스키마·규칙 검증을 통과할 때까지의 재생성 상한
MAX_FACTSHEET_ATTEMPTS = 3

#: 웹 도구를 준 세션이 이 턴 수 이하로 끝나면 검색 없이 기억으로 답했을 가능성이 높다.
#: ADR-0007 그라운딩의 전제가 깨지는 신호이므로 경고로 올린다.
MIN_TOOL_TURNS = 2


class ResearchStageError(Exception):
    pass


@dataclass(frozen=True)
class Substep:
    key: str
    output: str
    prompt: str
    tools: tuple[str, ...]
    timeout: int
    #: 프롬프트에 주입할 선행 산출물 (ADR-0009 오염 방지: 03은 01만 받는다)
    needs: tuple[str, ...] = ()
    #: 소스 카드 인덱스를 주입하고 `## 참조 소스`를 수확할지 (ADR-0012).
    #: 03은 콘텐츠 가치만 판정하므로 라이브러리에 닿지 않는다.
    uses_knowledge: bool = False

    @property
    def stage_key(self) -> str:
        return f"0b-{self.key}"


SUBSTEPS: tuple[Substep, ...] = (
    Substep("01-research", "01-research.md", "01-research.md", SOURCE_TOOLS, 900,
            uses_knowledge=True),
    Substep("02-verify", "02-verify.md", "02-verify.md", SOURCE_TOOLS, 900, ("research",),
            uses_knowledge=True),
    Substep("03-critique", "03-critique.md", "03-critique.md", WEB_TOOLS, 900, ("research",)),
    Substep(
        "04-factsheet",
        "04-factsheet.json",
        "04-factsheet.md",
        (),
        600,
        ("research", "verify", "critique"),
    ),
)

#: needs 키 → 산출물 파일명
_SOURCE_FILES = {
    "research": "01-research.md",
    "verify": "02-verify.md",
    "critique": "03-critique.md",
}


@dataclass
class ResearchResult:
    topic: str
    slug: str
    run_id: str
    verdict: str | None
    factsheet: dict[str, Any] | None
    topic_dir: Path
    run_dir: Path
    warnings: list[str] = field(default_factory=list)
    executed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    @property
    def summary(self) -> str:
        head = f"[0b] {self.topic} — verdict={self.verdict}"
        detail = f"실행 {len(self.executed)}건 / 스킵 {len(self.skipped)}건"
        if self.passed:
            return f"{head} ({detail}) → 사람 게이트 대기 (STATUS.md 보류)"
        return f"{head} ({detail}) → 백로그 반려"


def _load_prompt(name: str) -> Template:
    path = PROMPTS_DIR / name
    if not path.exists():
        raise ResearchStageError(f"프롬프트 파일이 없다: {path}")
    return Template(path.read_text(encoding="utf-8"))


def find_run_for_slug(paths: Paths, slug: str) -> tuple[str, dict[str, Any]]:
    """해당 슬러그의 가장 최근 run을 찾는다 (run_id가 날짜 프리픽스라 사전순=시간순)."""
    matches: list[tuple[str, dict[str, Any]]] = []
    for contract_path in sorted(paths.runs.glob("*/topic.json")):
        try:
            data = json.loads(contract_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("slug") == slug:
            matches.append((contract_path.parent.name, data))

    if not matches:
        raise ResearchStageError(
            f"슬러그 '{slug}'에 해당하는 run이 없다. [0a. topic]을 먼저 실행하라."
        )
    return matches[-1]


def _build_feedback(problems: list[str]) -> str:
    bullets = "\n".join(f"- {p}" for p in problems)
    return (
        "\n\n# 재생성 지시 — 직전 출력이 계약 검증에 실패했다\n\n"
        f"{bullets}\n\n"
        "위 문제를 모두 고쳐 JSON 객체 하나만 다시 출력하라. "
        "특히 4조건과 verdict의 일치 규칙을 다시 확인하라.\n"
    )


def _harvest_sources(
    step: Substep,
    text: str,
    *,
    slug: str,
    store: KnowledgeStore,
    warnings: list[str],
) -> None:
    """산출물 끝의 `## 참조 소스`를 카드로 반영한다 (ADR-0012).

    계약 위반은 경고로만 올린다. 조사 본문은 유효한데 부록 파싱 실패로
    900초짜리 세션을 버리는 것은 손해다.
    """
    contract = extract_contract(text)
    if contract is None:
        warnings.append(
            f"{step.key}: 산출물에 `## 참조 소스` JSON이 없어 소스 카드를 만들지 못했다"
        )
        return

    created, updated, issues = store.apply(contract, slug=slug)
    warnings.extend(f"{step.key}: {issue}" for issue in issues)
    if created or updated:
        store.reindex()
        log.info("[%s] 소스 카드 신규 %d건 / 갱신 %d건", step.key, created, updated)


def _run_substep(
    step: Substep,
    *,
    topic: str,
    slug: str,
    topic_dir: Path,
    state: RunState,
    llm: LLMClient,
    paths: Paths,
    store: KnowledgeStore,
    force: bool,
    executed: list[str],
    skipped: list[str],
    warnings: list[str],
) -> str:
    """마크다운 산출 서브스텝 1개를 실행하고 산출물 텍스트를 돌려준다."""
    out_path = topic_dir / step.output

    if not force and out_path.exists() and out_path.read_text(encoding="utf-8").strip():
        log.info("[%s] 산출물이 이미 있어 스킵한다: %s", step.key, out_path.name)
        skipped.append(step.key)
        state.mark_done(step.stage_key, skipped=True,
                        output=out_path.relative_to(paths.root).as_posix())
        return out_path.read_text(encoding="utf-8")

    context = {"topic": topic, "knowledge": ""}
    for need in step.needs:
        source = topic_dir / _SOURCE_FILES[need]
        if not source.exists():
            raise ResearchStageError(
                f"[{step.key}] 선행 산출물이 없다: {source.name}"
            )
        context[need] = source.read_text(encoding="utf-8")

    add_dirs: tuple[Path, ...] = ()
    if step.uses_knowledge:
        # 카드가 0장이면 주입문이 빈 문자열이라 첫 실행은 현행과 동일하게 돈다
        context["knowledge"] = store.injection()
        if context["knowledge"]:
            add_dirs = (store.root,)

    prompt = _load_prompt(step.prompt).safe_substitute(**context, feedback="")

    state.mark_running(step.stage_key)
    log.info("[%s] 독립 헤드리스 세션 시작 (tools=%s)", step.key, ",".join(step.tools) or "없음")
    result = llm.run(
        prompt,
        allowed_tools=step.tools,
        timeout=step.timeout,
        label=step.key,
        add_dirs=add_dirs,
    )

    if step.tools and (result.num_turns or 0) < MIN_TOOL_TURNS:
        warnings.append(
            f"{step.key}: 세션이 {result.num_turns}턴만에 끝났다. 웹 검색 없이 "
            "기억으로 작성했을 수 있으니 출처를 육안 확인하라 (ADR-0007)"
        )

    write_text(out_path, result.text.strip() + "\n")
    executed.append(step.key)

    if step.uses_knowledge:
        _harvest_sources(step, result.text, slug=slug, store=store, warnings=warnings)

    state.mark_done(
        step.stage_key,
        output=out_path.relative_to(paths.root).as_posix(),
        **result.meta,
    )
    return result.text


def _run_factsheet(
    step: Substep,
    *,
    topic: str,
    topic_dir: Path,
    state: RunState,
    llm: LLMClient,
    paths: Paths,
    force: bool,
    executed: list[str],
    skipped: list[str],
) -> tuple[dict[str, Any], list[str]]:
    """팩트시트를 생성하고 스키마·스펙 규칙 검증을 통과할 때까지 재생성한다."""
    out_path = topic_dir / step.output

    if not force and out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            errors, warnings = validate_factsheet(existing)
            if not errors:
                log.info("[%s] 검증을 통과한 산출물이 이미 있어 스킵한다", step.key)
                skipped.append(step.key)
                state.mark_done(step.stage_key, skipped=True,
                                output=out_path.relative_to(paths.root).as_posix())
                return existing, warnings
            log.warning("[%s] 기존 산출물이 검증에 실패해 재생성한다: %s", step.key, errors)
        except (json.JSONDecodeError, OSError):
            log.warning("[%s] 기존 산출물을 읽을 수 없어 재생성한다", step.key)

    context = {"topic": topic}
    for need in step.needs:
        source = topic_dir / _SOURCE_FILES[need]
        if not source.exists():
            raise ResearchStageError(f"[{step.key}] 선행 산출물이 없다: {source.name}")
        context[need] = source.read_text(encoding="utf-8")

    template = _load_prompt(step.prompt)
    state.mark_running(step.stage_key)

    feedback = ""
    problems: list[str] = []

    for attempt in range(1, MAX_FACTSHEET_ATTEMPTS + 1):
        prompt = template.safe_substitute(**context, feedback=feedback)
        log.info("[%s] 팩트시트 생성 시도 %d/%d", step.key, attempt, MAX_FACTSHEET_ATTEMPTS)
        result = llm.run(
            prompt,
            allowed_tools=step.tools,
            timeout=step.timeout,
            label=f"{step.key}.try{attempt}",
        )

        try:
            data = extract_json_object(result.text)
        except JSONExtractionError as exc:
            problems = [f"출력이 JSON 객체가 아니다: {exc}"]
            feedback = _build_feedback(problems)
            continue

        # 소재명은 계약상 입력과 같아야 한다. 표기 흔들림은 여기서 고정한다.
        if str(data.get("topic", "")).strip() != topic:
            log.warning("[%s] topic 필드가 입력과 달라 교정한다: %r", step.key, data.get("topic"))
            data["topic"] = topic

        errors, warnings = validate_factsheet(data)
        if not errors:
            write_text(out_path, dump_json(data))
            executed.append(step.key)
            state.mark_done(
                step.stage_key,
                output=out_path.relative_to(paths.root).as_posix(),
                attempts=attempt,
                **result.meta,
            )
            return data, warnings

        problems = errors
        log.warning("[%s] 계약 검증 실패 (%d건): %s", step.key, len(errors), errors)
        feedback = _build_feedback(errors)

    message = (
        f"팩트시트가 {MAX_FACTSHEET_ATTEMPTS}회 재생성 후에도 계약을 만족하지 못했다: "
        + "; ".join(problems)
    )
    state.mark_failed(step.stage_key, message)
    raise ResearchStageError(message)


def run_research_stage(
    slug: str,
    *,
    llm: LLMClient,
    paths: Paths | None = None,
    run_id: str | None = None,
    force: bool = False,
    only: str | None = None,
) -> ResearchResult:
    paths = paths or Paths.from_env()

    if run_id:
        contract_path = paths.run_dir(run_id) / "topic.json"
        if not contract_path.exists():
            raise ResearchStageError(f"topic.json이 없다: {contract_path}")
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    else:
        run_id, contract = find_run_for_slug(paths, slug)

    topic = contract["topic"]
    topic_dir = paths.topic_dir(slug)
    run_dir = paths.run_dir(run_id)
    state = RunState.load_or_create(run_dir, run_id, topic=topic, slug=slug)
    store = KnowledgeStore(paths.knowledge)

    if state.is_done(STAGE) and not force and not only:
        existing_path = topic_dir / "04-factsheet.json"
        try:
            existing = json.loads(existing_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # 상태는 done인데 산출물이 사라졌다면 상태를 믿지 않고 다시 만든다
            log.warning("[%s] 완료 상태이나 %s를 읽을 수 없어 재실행한다",
                        STAGE, existing_path.name)
        else:
            log.info("[%s] 이미 완료된 단계라 스킵한다 (run_id=%s)", STAGE, run_id)
            return ResearchResult(
                topic=topic, slug=slug, run_id=run_id, verdict=existing.get("verdict"),
                factsheet=existing, topic_dir=topic_dir, run_dir=run_dir,
                skipped=[s.key for s in SUBSTEPS],
            )

    state.mark_running(STAGE)
    executed: list[str] = []
    skipped: list[str] = []
    warnings: list[str] = []  # 사람이 봐야 할 신호. 차단하지는 않는다
    factsheet: dict[str, Any] | None = None

    steps = [s for s in SUBSTEPS if only in (None, s.key)]
    if only and not steps:
        raise ResearchStageError(
            f"'{only}'는 없는 서브스텝이다. 가능한 값: {', '.join(s.key for s in SUBSTEPS)}"
        )

    try:
        for step in steps:
            if step.key == "04-factsheet":
                factsheet, sheet_warnings = _run_factsheet(
                    step, topic=topic, topic_dir=topic_dir, state=state, llm=llm,
                    paths=paths, force=force, executed=executed, skipped=skipped,
                )
                warnings.extend(sheet_warnings)
            else:
                _run_substep(
                    step, topic=topic, slug=slug, topic_dir=topic_dir, state=state,
                    llm=llm, paths=paths, store=store, force=force,
                    executed=executed, skipped=skipped, warnings=warnings,
                )
    except Exception as exc:
        state.mark_failed(STAGE, f"{type(exc).__name__}: {exc}")
        raise

    if only and factsheet is None:
        # 부분 실행: verdict 분기는 전체 실행에서만 처리한다
        state.stage(STAGE)["status"] = runstate.PENDING
        state.save()
        return ResearchResult(
            topic=topic, slug=slug, run_id=run_id, verdict=None, factsheet=None,
            topic_dir=topic_dir, run_dir=run_dir, executed=executed, skipped=skipped,
        )

    assert factsheet is not None
    verdict = factsheet["verdict"]

    # 팩트시트는 runs/{run_id}/research.json으로 보존 (specs/06 — 사실 검증 추적성)
    write_text(run_dir / "research.json", dump_json(factsheet))

    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)

    if verdict == "fail":
        reason = factsheet.get("reject_reason") or "소재 4조건 중 불충족 항목이 있다"
        log.warning("[%s] verdict=fail → 백로그 반려: %s", STAGE, reason)

        status_mod.write_status(
            topic_dir / "STATUS.md",
            status=status_mod.NO_GO,
            topic=topic,
            slug=slug,
            run_id=run_id,
            reason=(
                f"[0b. research] 자동 반려 — {reason}\n\n"
                "소재 4조건 중 하나 이상이 사료 근거로 불충족 판정됐다 "
                "(specs/06-topic-research.md). 대본 생성으로 진입하지 않는다."
            ),
            decided_by="파이프라인 (자동 반려)",
            done_stages=("topic", "research"),
        )

        entries = backlog_mod.parse_backlog(paths.backlog)
        try:
            entry = backlog_mod.find_entry(entries, topic)
            backlog_mod.update_status(paths.backlog, entry, STATUS_REJECTED)
        except backlog_mod.BacklogError as exc:
            log.warning("백로그 상태를 갱신하지 못했다: %s", exc)

        state.mark_blocked(
            STAGE,
            reason=reason,
            verdict=verdict,
            outputs=[(run_dir / "research.json").relative_to(paths.root).as_posix()],
        )
    else:
        status_mod.write_status(
            topic_dir / "STATUS.md",
            status=status_mod.PENDING,
            topic=topic,
            slug=slug,
            run_id=run_id,
            reason=(
                "[0b. research] 통과 — 소재 4조건이 사료 근거로 충족됐다.\n\n"
                "대본 후보 생성([1. script]) 이후 패키지 전체를 보고 "
                "사람이 go / no-go를 기록한다 (ADR-0009)."
            ),
            done_stages=("topic", "research"),
        )
        state.mark_done(
            STAGE,
            verdict=verdict,
            warnings=warnings,
            outputs=[
                (topic_dir / "04-factsheet.json").relative_to(paths.root).as_posix(),
                (run_dir / "research.json").relative_to(paths.root).as_posix(),
            ],
        )

    return ResearchResult(
        topic=topic, slug=slug, run_id=run_id, verdict=verdict, factsheet=factsheet,
        topic_dir=topic_dir, run_dir=run_dir, warnings=warnings,
        executed=executed, skipped=skipped,
    )
