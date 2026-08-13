"""[1w. write] — 씬 계획 → 자막 문장. 대본 후보를 쓴다.

specs/05-pipeline.md:
    [1w. write] → 05-candidates/*.json (요지 → 자막 문장. 씬 구조를 바꾸지 않는다)

## 이 단계가 지는 판단

문체 하나다. 구어체 존댓말, 수사 의문문, 시그니처 문구, 줄당 리듬. **씬 수·`scene_id`·
`beat`·그림 필드·연출 필드는 `08-sceneplan.json`에서 그대로 복사한다** (ADR-0029).

세션에는 `scene_id`와 `text`만 내놓게 한다. 씬을 합치거나 나눌 수단을 아예 주지 않는
것이 프롬프트로 이르는 것보다 확실하다 — 씬을 합치면 그 씬의 `visual_goal`·`subject`
중 하나가 버려지고, 누가 버릴지 정할 근거가 이 단계에 없다.

복사가 실제로 일어났는지는 쓰기 전에 `sceneplan.carry_errors()`로 전수 대조한다.
**대조할 필드 목록은 손으로 적지 않는다** — 두 스키마의 교집합이다.

## 팩트시트를 읽지 않는다

숫자는 `says`에 이미 있고, 없는 숫자를 쓰면 그라운딩 위반이다 (ADR-0007). 검증은
`validate_candidate`가 팩트시트와 대조해 잡는다.

## 이 단계가 하지 않는 것

검증 실패 시 **재생성하지 않는다.** 재진입 지점은 `[2. validate]`가 정한다.
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
from ..schemas import vocab
from ..schemas.grounding import validate_grounding
from ..schemas.scenes import validate_scenes
from ..schemas.sceneplan import carried_fields, carry_errors
from ..schemas.script_rules import core_chars, validate_script
from .outline import load_factsheet, resolve_run
from .sceneplan import load_outline
from .session import (
    ScriptSessionError,
    ask_json,
    format_limits,
    format_signatures,
    load_prompt,
)

log = logging.getLogger(__name__)

STAGE = "1w-write"
PROMPT = "10-write.md"
PLAN_FILE = "08-sceneplan.json"
CANDIDATES_DIR = "05-candidates"
FIRST_CANDIDATE = "01.json"

#: est_* 계산용 명목 발화 속도(자/초). `script-rules.json`의 속도 범위 한가운데다 —
#: 값을 손으로 적으면 엔벨로프가 바뀔 때 추정 시간만 옛 기준에 남는다 (ADR-0034 §3).
NOMINAL_SPEED = sum(vocab.limits()["speed_cps"]) / 2


@dataclass
class WriteResult:
    topic: str
    slug: str
    run_id: str
    candidate_path: Path | None
    scenes: dict[str, Any] | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False

    @property
    def valid(self) -> bool:
        return self.scenes is not None and not self.errors

    @property
    def summary(self) -> str:
        if self.scenes is None:
            return f"[1w] {self.topic} — 집필 실패"
        count = len(self.scenes["scenes"])
        chars = len(core_chars(" ".join(s["text"] for s in self.scenes["scenes"])))
        verdict = "검증 통과" if self.valid else f"검증 실패 {len(self.errors)}건"
        tail = " (스킵)" if self.skipped else ""
        return (
            f"[1w] {self.topic} — 후보 1개 / {count}줄 / {chars}자 / "
            f"{self.scenes['total_duration']:.1f}초 → {verdict}{tail}"
        )


def format_angle(outline: dict[str, Any]) -> str:
    """고른 각도와 단 구성을 집필 세션이 읽을 모양으로.

    `why_chosen`을 싣는 것은 단계 간 의도 손실을 막기 위해서다 (ADR-0029 되돌릴 조건).
    """
    candidates = outline.get("hook_candidates") or []
    index = outline.get("chosen_hook")
    lines: list[str] = []
    if isinstance(index, int) and 0 <= index < len(candidates):
        hook = candidates[index]
        lines.append(f"- **훅**: {hook.get('claim', '')}")
        lines.append(f"- **왜 놀라운가**: {hook.get('why_surprising', '')}")
    if outline.get("why_chosen"):
        lines.append(f"- **이 각도를 고른 이유**: {outline['why_chosen']}")
    acts = outline.get("acts") or []
    if acts:
        lines.append("- **단 구성**: " + " → ".join(str(a.get("name", "")) for a in acts))
    return "\n".join(lines)


def format_scenes(plan: dict[str, Any]) -> str:
    """씬 계획을 집필 세션이 읽을 목록으로. **문장에 필요한 것만 준다.**

    그림 필드와 연출 필드는 빼고 준다 — 이 세션이 손댈 수 없는 값이고, 보여 주면
    자막에 그림 지시를 섞어 쓰게 만든다.
    """
    rows = [
        {
            "scene_id": scene.get("scene_id"),
            "act": scene.get("act"),
            "beat": scene.get("beat"),
            "says": scene.get("says"),
            "budget": scene.get("char_budget"),
        }
        for scene in plan.get("scenes", [])
    ]
    return dump_json({"scenes": rows})


def build_scenes(
    plan: dict[str, Any],
    texts: dict[int, str],
    *,
    run_id: str,
    topic: str,
) -> dict[str, Any]:
    """씬 계획 + 세션이 쓴 문장 → `06-script.json` 모양.

    타임스탬프는 코드가 계산한다 (ADR-0014: 산술을 세션에 맡기지 않는다). TTS 이후
    실측으로 갱신되는 추정치다 (specs/05).
    """
    planned = plan.get("scenes", [])
    if not planned:
        raise ScriptSessionError("씬 계획에 씬이 없다")

    fields = carried_fields()
    scenes: list[dict[str, Any]] = []
    cursor = 0.0

    for item in planned:
        sid = item.get("scene_id")
        text = (texts.get(sid) or "").strip()
        if not text:
            raise ScriptSessionError(f"{sid}번 씬의 text가 비어 있다 (세션이 빠뜨렸다)")

        span = round(len(core_chars(text)) / NOMINAL_SPEED, 3)
        start, cursor = cursor, round(cursor + span, 3)

        scene: dict[str, Any] = {"scene_id": sid, "text": text,
                                 "est_start": start, "est_end": cursor}
        # 계획에 있는 것만 옮긴다. 없는 필드를 기본값으로 채우면 그 순간 이 단계가
        # 연출을 판단하는 것이 되고, [5]의 "구도 기본값 N씬"이 0으로 죽는다.
        for field_name in fields:
            if field_name in item and field_name != "scene_id":
                scene[field_name] = item[field_name]
        scenes.append(scene)

    built = {
        "run_id": run_id,
        "topic": topic,
        "total_duration": scenes[-1]["est_end"],
        "scenes": scenes,
    }

    # ADR-0029 — 쓰기 전 전수 대조. 계획에서 만들었으므로 실패하면 세션이 아니라
    # 이 함수가 틀린 것이다. 조용히 넘기면 그림 계획이 소리 없이 바뀐다.
    drift = carry_errors(plan, built)
    if drift:
        raise ScriptSessionError(
            "계획과 대본이 어긋난다 (파일을 쓰지 않는다): " + "; ".join(drift[:3])
        )
    return built


def validate_candidate(
    scenes: dict[str, Any], factsheet: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """검증기 3개를 한 번에 돌린다. 각 오류에 출처 검증기를 붙여 돌려준다.

    `[2. validate]`가 같은 함수를 쓴다 — 검증 기준이 단계마다 다르면 `[1w]`가 통과
    시킨 것이 `[2]`에서 떨어지고 그 차이를 아무도 못 본다.
    """
    errors: list[str] = []
    warnings: list[str] = []

    for label, (errs, warns) in (
        ("스키마", validate_scenes(scenes)),
        ("대본규칙", validate_script(scenes)),
        ("그라운딩", validate_grounding(scenes, factsheet)),
    ):
        errors.extend(f"[{label}] {e}" for e in errs)
        warnings.extend(f"[{label}] {w}" for w in warns)

    return errors, warnings


def generate_candidate(
    *,
    llm: LLMClient,
    topic: str,
    run_id: str,
    outline: dict[str, Any],
    plan: dict[str, Any],
    feedback: str = "",
    label: str = STAGE,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """씬 계획 → 대본 후보 1개. 파일 쓰기·상태 기록은 호출자 몫이다."""
    prompt = load_prompt(PROMPT).safe_substitute(
        topic=topic,
        angle=format_angle(outline),
        scenes=format_scenes(plan),
        signatures=format_signatures(),
        limits=format_limits("total_chars", "line_chars_max"),
        feedback=feedback,
    )
    payload, meta = ask_json(llm, prompt, label=label)

    raw = payload.get("scenes")
    if not isinstance(raw, list):
        raise ScriptSessionError("세션 출력에 scenes 배열이 없다")

    texts: dict[int, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ScriptSessionError(f"씬 항목이 객체가 아니다: {item!r}")
        sid = item.get("scene_id")
        if not isinstance(sid, int):
            raise ScriptSessionError(f"scene_id가 정수가 아니다: {sid!r}")
        # 세션이 다른 키를 얹어도 무시한다 — 씬 구조는 계획에서만 온다. 알리기는 한다:
        # 프롬프트를 못 읽었다는 신호이고, 다음 편에서 프롬프트를 손볼 근거가 된다.
        extra = set(item) - {"scene_id", "text"}
        if extra:
            log.warning("[%s] %s번 씬에 계약 밖 키: %s (무시한다)",
                        label, sid, ", ".join(sorted(extra)))
        texts[sid] = str(item.get("text", ""))

    return build_scenes(plan, texts, run_id=run_id, topic=topic), meta


def load_plan(topic_dir: Path) -> dict[str, Any]:
    path = topic_dir / PLAN_FILE
    if not path.exists():
        raise ScriptSessionError(
            f"씬 계획이 없다: {path}. [1s. sceneplan]을 먼저 실행하라."
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScriptSessionError(f"씬 계획을 읽을 수 없다: {exc}") from exc


def run_write_stage(
    slug: str,
    *,
    llm: LLMClient,
    paths: Paths | None = None,
    run_id: str | None = None,
    force: bool = False,
) -> WriteResult:
    paths = paths or Paths.from_env()
    run_id, contract = resolve_run(paths, slug, run_id)

    topic = contract["topic"]
    topic_dir = paths.topic_dir(slug)
    run_dir = paths.run_dir(run_id)
    state = RunState.load_or_create(run_dir, run_id, topic=topic, slug=slug)

    factsheet = load_factsheet(topic_dir)
    outline = load_outline(topic_dir)
    plan = load_plan(topic_dir)
    candidate_path = topic_dir / CANDIDATES_DIR / FIRST_CANDIDATE

    if not force and candidate_path.exists():
        try:
            existing = json.loads(candidate_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("[%s] 기존 후보를 읽을 수 없어 다시 생성한다", STAGE)
        else:
            log.info("[%s] 후보가 이미 있어 스킵한다: %s", STAGE, candidate_path.name)
            errors, warnings = validate_candidate(existing, factsheet)
            return WriteResult(
                topic=topic, slug=slug, run_id=run_id, candidate_path=candidate_path,
                scenes=existing, errors=errors, warnings=warnings, skipped=True,
            )

    state.mark_running(STAGE)
    log.info("[%s] 독립 헤드리스 세션 시작 (도구 없음)", STAGE)

    try:
        scenes, meta = generate_candidate(
            llm=llm, topic=topic, run_id=run_id, outline=outline, plan=plan,
        )
    except ScriptSessionError as exc:
        message = f"{exc} 원본은 {run_dir / 'logs'}에 있다. 재생성은 [2. validate] 소관이다."
        state.mark_failed(STAGE, message)
        raise ScriptSessionError(message) from exc

    write_text(candidate_path, dump_json(scenes))
    errors, warnings = validate_candidate(scenes, factsheet)
    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)

    info = {
        "output": candidate_path.relative_to(paths.root).as_posix(),
        "scene_count": len(scenes["scenes"]),
        "est_duration": scenes["total_duration"],
        "validation_errors": errors,
        "validation_warnings": warnings,
        **meta,
    }
    if errors:
        log.warning("[%s] 검증 실패 %d건 — 후보는 남긴다 ([2]가 판정)", STAGE, len(errors))
        state.mark_failed(STAGE, f"검증 실패 {len(errors)}건", **info)
    else:
        state.mark_done(STAGE, **info)

    return WriteResult(
        topic=topic, slug=slug, run_id=run_id, candidate_path=candidate_path,
        scenes=scenes, errors=errors, warnings=warnings,
    )
