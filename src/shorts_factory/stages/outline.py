"""[1a. outline] — 팩트시트 → 훅 각도 + 서사 구조.

specs/05-pipeline.md:
    [1a. outline] → 07-outline.json (흥미 포인트 선정 + 서사 구조 설계 + 단별 사실·글자 배분)

## 이 단계가 지는 판단

**무엇이 흥미로운가**와 **어떤 순서로 말하는가** 둘뿐이다. 씬도 문장도 만들지 않는다.
한 세션이 아홉 가지 판단을 지면 뒤에 오는 것이 형식적으로 채워진다는 것이 이 분할의
근거다 (ADR-0029).

**단 구성은 소재가 정한다** (ADR-0033 §2). 개수도 이름도 순서도 고정이 아니므로 이
파일에 단 목록이 없고, 검증기도 개수를 세지 않는다. 기계가 보는 것은 결과뿐이다 —
예산 합계, 훅 인덱스, 그리고 경고로 내려간 훅 위치·수미상관.

## 이 단계가 하지 않는 것

검증 실패 시 **재생성하지 않는다.** 재진입 지점은 실패 종류가 정하고 그 판단은
`[2. validate]` 소관이다 (ADR-0029).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Paths, write_text
from ..jsonio import dump_json
from ..llm.base import LLMClient
from ..runstate import RunState
from ..schemas.outline import unknown_fact_ids, validate_outline
from .research import find_run_for_slug
from .session import ScriptSessionError, ask_json, format_limits, load_prompt

log = logging.getLogger(__name__)

STAGE = "1a-outline"
PROMPT = "08-outline.md"
OUTLINE_FILE = "07-outline.json"
FACTSHEET_FILE = "04-factsheet.json"

#: specs/06 — confidence: low 사실은 대본에 사용 금지. 주입 단계에서 아예 뺀다.
EXCLUDED_CONFIDENCE = "low"


@dataclass
class OutlineResult:
    topic: str
    slug: str
    run_id: str
    path: Path | None
    outline: dict[str, Any] | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False

    @property
    def valid(self) -> bool:
        return self.outline is not None and not self.errors

    @property
    def summary(self) -> str:
        if self.outline is None:
            return f"[1a] {self.topic} — 구성 생성 실패"
        acts = self.outline.get("acts", [])
        hooks = self.outline.get("hook_candidates", [])
        budget = sum(int(a.get("char_budget", 0)) for a in acts)
        verdict = "검증 통과" if self.valid else f"검증 실패 {len(self.errors)}건"
        tail = " (스킵)" if self.skipped else ""
        return (
            f"[1a] {self.topic} — 훅 후보 {len(hooks)}개 / {len(acts)}단 / "
            f"{budget}자 배분 → {verdict}{tail}"
        )


def groundable_factsheet(factsheet: dict[str, Any]) -> dict[str, Any]:
    """세션에 주입할 팩트시트. confidence=low 사실을 제거한다 (specs/06).

    프롬프트로 "쓰지 마라"라고 이르는 대신 아예 안 보여준다. 하류 그라운딩 검증도
    같은 기준으로 허용 집합을 만들므로 두 곳의 판단이 어긋나지 않는다.
    """
    trimmed = dict(factsheet)
    trimmed["facts"] = [
        fact
        for fact in factsheet.get("facts", [])
        if isinstance(fact, dict) and fact.get("confidence") != EXCLUDED_CONFIDENCE
    ]
    trimmed.pop("verdict", None)
    trimmed.pop("reject_reason", None)
    return trimmed


def validate_with_factsheet(
    outline: dict[str, Any], factsheet: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """계약 검증 + 그라운딩(ADR-0007). 오류에 출처 검증기를 붙여 돌려준다."""
    errors, warnings = validate_outline(outline)
    errors = [f"[구성] {e}" for e in errors]
    warnings = [f"[구성] {w}" for w in warnings]

    unknown = unknown_fact_ids(outline, groundable_factsheet(factsheet))
    if unknown:
        errors.append(
            f"[그라운딩] 팩트시트에 없는 근거 id: {', '.join(unknown)} — "
            "confidence가 low라 빠졌거나 지어낸 것이다 (ADR-0007)"
        )
    return errors, warnings


def generate_outline(
    *,
    llm: LLMClient,
    topic: str,
    factsheet: dict[str, Any],
    feedback: str = "",
    label: str = STAGE,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """팩트시트 → 구성안 1개. 파일 쓰기·상태 기록은 호출자 몫이다."""
    prompt = load_prompt(PROMPT).safe_substitute(
        topic=topic,
        factsheet=dump_json(groundable_factsheet(factsheet)),
        limits=format_limits("total_chars", "line_count", "total_seconds"),
        feedback=feedback,
    )
    payload, meta = ask_json(llm, prompt, label=label)
    payload.setdefault("topic", topic)
    return payload, meta


def load_factsheet(topic_dir: Path) -> dict[str, Any]:
    path = topic_dir / FACTSHEET_FILE
    if not path.exists():
        raise ScriptSessionError(
            f"팩트시트가 없다: {path}. [0b. research]를 먼저 실행하라."
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScriptSessionError(f"팩트시트를 읽을 수 없다: {exc}") from exc


def resolve_run(paths: Paths, slug: str, run_id: str | None) -> tuple[str, dict[str, Any]]:
    """run_id와 topic.json. 세 단계가 같은 방식으로 run을 찾는다."""
    if run_id:
        path = paths.run_dir(run_id) / "topic.json"
        if not path.exists():
            raise ScriptSessionError(f"topic.json이 없다: {path}")
        return run_id, json.loads(path.read_text(encoding="utf-8"))
    return find_run_for_slug(paths, slug)


def run_outline_stage(
    slug: str,
    *,
    llm: LLMClient,
    paths: Paths | None = None,
    run_id: str | None = None,
    force: bool = False,
) -> OutlineResult:
    paths = paths or Paths.from_env()
    run_id, contract = resolve_run(paths, slug, run_id)

    topic = contract["topic"]
    topic_dir = paths.topic_dir(slug)
    run_dir = paths.run_dir(run_id)
    state = RunState.load_or_create(run_dir, run_id, topic=topic, slug=slug)

    factsheet = load_factsheet(topic_dir)
    if factsheet.get("verdict") != "pass":
        reason = (
            f"팩트시트 verdict가 '{factsheet.get('verdict')}'다. "
            "판별 기준을 넘기지 못한 소재는 대본 생성에 진입하지 않는다 (specs/06)."
        )
        state.mark_blocked(STAGE, reason=reason)
        raise ScriptSessionError(reason)

    path = topic_dir / OUTLINE_FILE

    if not force and path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("[%s] 기존 구성안을 읽을 수 없어 다시 생성한다", STAGE)
        else:
            log.info("[%s] 구성안이 이미 있어 스킵한다: %s", STAGE, OUTLINE_FILE)
            errors, warnings = validate_with_factsheet(existing, factsheet)
            return OutlineResult(
                topic=topic, slug=slug, run_id=run_id, path=path, outline=existing,
                errors=errors, warnings=warnings, skipped=True,
            )

    state.mark_running(STAGE)
    log.info("[%s] 독립 헤드리스 세션 시작 (도구 없음)", STAGE)

    try:
        outline, meta = generate_outline(llm=llm, topic=topic, factsheet=factsheet)
    except ScriptSessionError as exc:
        message = f"{exc} 원본은 {run_dir / 'logs'}에 있다."
        state.mark_failed(STAGE, message)
        raise ScriptSessionError(message) from exc

    write_text(path, dump_json(outline))
    errors, warnings = validate_with_factsheet(outline, factsheet)
    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)

    info = {
        "output": path.relative_to(paths.root).as_posix(),
        "act_count": len(outline.get("acts", [])),
        "hook_candidates": len(outline.get("hook_candidates", [])),
        "validation_errors": errors,
        "validation_warnings": warnings,
        **meta,
    }
    if errors:
        log.warning("[%s] 검증 실패 %d건 — 구성안은 남긴다", STAGE, len(errors))
        state.mark_failed(STAGE, f"검증 실패 {len(errors)}건", **info)
    else:
        state.mark_done(STAGE, **info)

    return OutlineResult(
        topic=topic, slug=slug, run_id=run_id, path=path, outline=outline,
        errors=errors, warnings=warnings,
    )
