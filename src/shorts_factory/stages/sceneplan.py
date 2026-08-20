"""[1s. sceneplan] — 구성안 + 팩트시트 → 씬 분할 · 글/그림 분담 · 연출.

specs/05-pipeline.md:
    [1s. sceneplan] → 08-sceneplan.json (씬 분할 + 글·그림 분담 + 연출 선택)

## 이 단계가 지는 판단

씬 분할, **그림 필드 넷**(`visual_goal`·`subject`·`subject_anchor`·`subject_scale`),
**연출 필드 다섯**(`framing`·`transition`·`camera`·`motion`·`emphasis`)이다. 문장은
쓰지 않는다.

후버댐 편에서 그림 필드가 27/27 비었던 것이 `[1]`을 가른 이유다 (ADR-0029). 연출까지
이 단계가 지는 것은 ADR-0033 §3이고, **같은 실패가 반복되면 연출을 떼는 것**이 그
ADR의 되돌릴 조건이다. 관측 수단은 `[5]`의 `구도 기본값 N씬`이다.

## 연출은 어휘에서 고른다

자유 기술이 아니다. 선택지는 `specs/schema/vocab.json`이고 프롬프트에는 코드가
주입한다 — 어휘 목록을 프롬프트 마크다운에 적으면 갈라진다 (ADR-0034 §3).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Paths, write_text
from ..jsonio import dump_json, extract_json_object
from ..llm.base import LLMClient
from ..runstate import RunState
from ..schemas.grounding import validate_plan_grounding
from ..schemas.sceneplan import ACT_BUDGET_TOLERANCE, direction_summary, validate_sceneplan
from .outline import load_factsheet, groundable_factsheet, resolve_run
from .session import (
    ScriptSessionError,
    ask_json,
    format_limits,
    format_vocab,
    load_prompt,
)

log = logging.getLogger(__name__)

STAGE = "1s-sceneplan"
PROMPT = "09-sceneplan.md"
PLAN_FILE = "08-sceneplan.json"
OUTLINE_FILE = "07-outline.json"

#: 재청 상한 (ADR-0044). 산출 직후 검증에 걸리면 **같은 세션**을 이어 1회만 다시 청한다.
#: 그래도 실패면 중단·보고다 — 전체 재생성 루프로 돌아가지 않는다.
MAX_RETRIES = 1


@dataclass
class SceneplanResult:
    topic: str
    slug: str
    run_id: str
    path: Path | None
    plan: dict[str, Any] | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False

    @property
    def valid(self) -> bool:
        return self.plan is not None and not self.errors

    @property
    def summary(self) -> str:
        if self.plan is None:
            return f"[1s] {self.topic} — 씬 계획 생성 실패"
        scenes = self.plan.get("scenes", [])
        blank = sum(1 for s in scenes if not s.get("framing"))
        anchored = sum(1 for s in scenes if s.get("subject_anchor"))
        verdict = "검증 통과" if self.valid else f"검증 실패 {len(self.errors)}건"
        tail = " (스킵)" if self.skipped else ""
        return (
            f"[1s] {self.topic} — {len(scenes)}씬 / 대상 앵커 {anchored}씬 / "
            f"구도 미선택 {blank}씬 → {verdict}{tail}"
        )


def generate_sceneplan(
    *,
    llm: LLMClient,
    topic: str,
    outline: dict[str, Any],
    factsheet: dict[str, Any],
    feedback: str = "",
    label: str = STAGE,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """구성안 + 팩트시트 → 씬 계획 1개. 파일 쓰기·상태 기록은 호출자 몫이다."""
    prompt = load_prompt(PROMPT).safe_substitute(
        topic=topic,
        outline=dump_json(outline),
        factsheet=dump_json(groundable_factsheet(factsheet)),
        limits=format_limits("line_count", "line_chars_target", "line_chars_max"),
        act_tolerance=ACT_BUDGET_TOLERANCE,
        vocab=format_vocab(),
        feedback=feedback,
    )
    payload, meta = ask_json(llm, prompt, label=label)
    payload.setdefault("topic", topic)
    return payload, meta


def validate_plan(
    plan: dict[str, Any],
    outline: dict[str, Any],
    factsheet: dict[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    """계약 검증 + 그라운딩 (ADR-0044 fail-fast).

    `says`·`info.labels`의 숫자가 팩트시트 밖이면 **여기서** 잡는다 — `[1w]`가 그
    숫자를 문장으로 옮긴 뒤 잡으면 되감을 것이 한 단계 늘어난다.
    """
    errors, warnings = validate_sceneplan(plan, outline)
    if factsheet is not None:
        g_errors, g_warnings = validate_plan_grounding(plan, factsheet)
        errors += [f"[그라운딩] {e}" for e in g_errors]
        warnings += [f"[그라운딩] {w}" for w in g_warnings]
    return errors, warnings


def build_retry_prompt(errors: list[str]) -> str:
    """같은 세션에 이어 보낼 재청 지시 (ADR-0044). 오류는 검증기 문구 그대로다."""
    lines = "\n".join(f"{i}. {error}" for i, error in enumerate(errors, 1))
    return (
        "방금 낸 씬 계획이 아래 검증에 걸렸다. 위반만 고쳐서 **계획 전체 JSON을**\n"
        "같은 형식으로 다시 출력하라. 걸리지 않은 씬은 바꾸지 마라.\n\n"
        f"{lines}\n"
    )


def load_outline(topic_dir: Path) -> dict[str, Any]:
    path = topic_dir / OUTLINE_FILE
    if not path.exists():
        raise ScriptSessionError(
            f"구성안이 없다: {path}. [1a. outline]을 먼저 실행하라."
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScriptSessionError(f"구성안을 읽을 수 없다: {exc}") from exc


def run_sceneplan_stage(
    slug: str,
    *,
    llm: LLMClient,
    paths: Paths | None = None,
    run_id: str | None = None,
    force: bool = False,
) -> SceneplanResult:
    paths = paths or Paths.from_env()
    run_id, contract = resolve_run(paths, slug, run_id)

    topic = contract["topic"]
    topic_dir = paths.topic_dir(slug)
    run_dir = paths.run_dir(run_id)
    state = RunState.load_or_create(run_dir, run_id, topic=topic, slug=slug)

    factsheet = load_factsheet(topic_dir)
    outline = load_outline(topic_dir)
    path = topic_dir / PLAN_FILE

    if not force and path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("[%s] 기존 씬 계획을 읽을 수 없어 다시 생성한다", STAGE)
        else:
            log.info("[%s] 씬 계획이 이미 있어 스킵한다: %s", STAGE, PLAN_FILE)
            errors, warnings = validate_plan(existing, outline, factsheet)
            return SceneplanResult(
                topic=topic, slug=slug, run_id=run_id, path=path, plan=existing,
                errors=errors, warnings=warnings, skipped=True,
            )

    state.mark_running(STAGE)
    log.info("[%s] 독립 헤드리스 세션 시작 (도구 없음)", STAGE)

    try:
        plan, meta = generate_sceneplan(
            llm=llm, topic=topic, outline=outline, factsheet=factsheet,
        )
    except ScriptSessionError as exc:
        message = f"{exc} 원본은 {run_dir / 'logs'}에 있다."
        state.mark_failed(STAGE, message)
        raise ScriptSessionError(message) from exc

    # 산출 직후 검증 → 걸리면 **같은 세션**에 1회 재청한다 (ADR-0044).
    # 틀린 계획을 [1w]로 흘려보낸 뒤 사슬을 되감는 것이 편당 38분이었다.
    errors, warnings = validate_plan(plan, outline, factsheet)
    retries = 0
    while errors and retries < MAX_RETRIES and meta.get("session_id"):
        retries += 1
        log.info("[%s] 검증 실패 %d건 — 같은 세션에 재청 %d/%d",
                 STAGE, len(errors), retries, MAX_RETRIES)
        try:
            result = llm.run(
                build_retry_prompt(errors),
                resume=meta["session_id"],
                label=f"{STAGE}.retry{retries}",
            )
            retried = extract_json_object(result.text)
            retried.setdefault("topic", topic)
        except Exception as exc:  # noqa: BLE001 — 재청 실패는 원본 오류로 보고한다
            log.warning("[%s] 재청 실패: %s — 원본 검증 결과로 보고한다", STAGE, exc)
            break
        plan, meta = retried, {**meta, **result.meta}
        errors, warnings = validate_plan(plan, outline, factsheet)

    write_text(path, dump_json(plan))
    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)

    info = {
        "output": path.relative_to(paths.root).as_posix(),
        "scene_count": len(plan.get("scenes", [])),
        "retries": retries,
        # ADR-0033 되돌릴 조건의 관측 수단. 판정하지 않고 세어만 둔다.
        "direction": direction_summary(plan),
        "validation_errors": errors,
        "validation_warnings": warnings,
        **meta,
    }
    if errors:
        # 재청까지 실패했다 — 중단·보고 (ADR-0044). 재생성 루프로 돌아가지 않는다.
        log.warning("[%s] 재청 후에도 검증 실패 %d건 — 중단·보고", STAGE, len(errors))
        state.mark_failed(STAGE, f"검증 실패 {len(errors)}건", **info)
    else:
        state.mark_done(STAGE, **info)

    return SceneplanResult(
        topic=topic, slug=slug, run_id=run_id, path=path, plan=plan,
        errors=errors, warnings=warnings,
    )
