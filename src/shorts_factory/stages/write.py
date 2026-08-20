"""[1w. write] — 씬 계획 → 자막 문장. 대본 후보를 쓴다.

specs/05-pipeline.md:
    [1w. write] → 05-candidates/*.json (요지 → 자막 문장. 씬 구조를 바꾸지 않는다)

## 이 단계가 지는 판단

문체 하나다. 구어체 존댓말, 수사 의문문, 시그니처 문구, 줄당 리듬. 세션의 출력은
`{scene_id, text}`뿐이고 `06-script.json` 모양은 오케스트레이터가 `08-sceneplan.json`의
필드에 그 text를 얹어 **조립한다** (ADR-0044). 세션에 필드 복사를 시키지 않으므로
복사 실패라는 계급이 없다. `carry_errors()`는 조립 코드의 자기 검증으로만 남는다.

세션에는 `scene_id`와 `text`만 내놓게 한다. 씬을 합치거나 나눌 수단을 아예 주지 않는
것이 프롬프트로 이르는 것보다 확실하다 — 씬을 합치면 그 씬의 `visual_goal`·`subject`
중 하나가 버려지고, 누가 버릴지 정할 근거가 이 단계에 없다.

## 팩트시트를 읽지 않는다

숫자는 `says`에 이미 있고, 없는 숫자를 쓰면 그라운딩 위반이다 (ADR-0007). 검증은
`validate_candidate`가 팩트시트와 대조해 잡는다.

## 산출 직후 씬 단위로 검증하고, 실패 씬만 다시 청한다 (ADR-0044)

전체 재집필은 없다. 24씬 중 3씬이 틀렸으면 **그 3씬만** 같은 세션(resume)에 다시
청한다 (상한 1회). 그래도 남으면 중단·보고다 — 재생성 루프는 `[2]`에서도 없다.
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
from ..schemas import vocab
from ..schemas.grounding import extract_values, factsheet_values, validate_grounding
from ..schemas.scenes import validate_scenes
from ..schemas.sceneplan import carried_fields, carry_errors
from ..schemas.script_rules import LINE_CHARS_MAX, core_chars, validate_script
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

#: 세션 타임아웃(초). 24씬 전체 집필의 성공 시도가 583초였다 (다다미 실측, ADR-0044) —
#: 공통 기본값 600초는 마진이 17초뿐이라 타임아웃 재시도로 편당 ~20분을 태웠다.
WRITE_TIMEOUT = 1200

#: 씬 단위 재청 상한 (ADR-0044). 실패 씬만 같은 세션(resume)에 다시 청한다.
MAX_RETRIES = 1


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


def parse_texts(payload: dict[str, Any], *, label: str = STAGE) -> dict[int, str]:
    """세션 출력에서 `{scene_id: text}`만 취한다. 나머지 키는 계약 밖이다."""
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
    return texts


def generate_texts(
    *,
    llm: LLMClient,
    topic: str,
    outline: dict[str, Any],
    plan: dict[str, Any],
    feedback: str = "",
    label: str = STAGE,
) -> tuple[dict[int, str], dict[str, Any]]:
    """씬 계획 → 문장. 세션의 산출은 `{scene_id: text}`뿐이다 (ADR-0044)."""
    prompt = load_prompt(PROMPT).safe_substitute(
        topic=topic,
        angle=format_angle(outline),
        scenes=format_scenes(plan),
        signatures=format_signatures(),
        limits=format_limits("total_chars", "line_chars_max"),
        feedback=feedback,
    )
    payload, meta = ask_json(llm, prompt, label=label, timeout=WRITE_TIMEOUT)
    return parse_texts(payload, label=label), meta


def scene_text_errors(
    scenes: dict[str, Any], factsheet: dict[str, Any]
) -> dict[int, list[str]]:
    """씬 단위 검증 (ADR-0044). `{scene_id: [오류]}` — 재청 대상을 고르는 기준이다.

    씬에 귀속되는 것만 본다: 줄당 글자 상한과 그 씬 text의 그라운딩.
    전체 분량 엔벨로프·수미상관은 씬 하나를 다시 써서 고칠 수 없으므로
    `validate_candidate`(최종 기록)에 남긴다.
    """
    allowed, low_only, _unparsable = factsheet_values(factsheet)
    failures: dict[int, list[str]] = {}

    for scene in scenes.get("scenes", []):
        sid = scene.get("scene_id")
        text = str(scene.get("text", ""))
        errors: list[str] = []

        chars = len(core_chars(text))
        if chars > LINE_CHARS_MAX:
            errors.append(f"{chars}자 — 줄당 최대 {LINE_CHARS_MAX}자다. 문장을 줄여라")

        for raw, value in extract_values(text):
            if value in allowed:
                continue
            if value in low_only:
                errors.append(
                    f"'{raw}'는 confidence=low 사실의 숫자다 — 빼거나 says의 숫자만 써라"
                )
            else:
                errors.append(
                    f"'{raw}'가 팩트시트에 없다 — says에 없는 숫자를 만들지 마라 (ADR-0007)"
                )

        if errors:
            failures[int(sid)] = errors
    return failures


def build_scene_retry_prompt(
    plan: dict[str, Any], failures: dict[int, list[str]]
) -> str:
    """실패 씬만 다시 청하는 지시 (ADR-0044). 같은 세션에 이어 보낸다."""
    planned = {s.get("scene_id"): s for s in plan.get("scenes", [])}
    blocks: list[str] = []
    for sid in sorted(failures):
        item = planned.get(sid, {})
        errors = "\n".join(f"  - {error}" for error in failures[sid])
        blocks.append(
            f"- scene_id {sid} (요지: {item.get('says', '')} / "
            f"예산 {item.get('char_budget', '?')}자)\n{errors}"
        )
    listed = "\n".join(blocks)
    return (
        "방금 쓴 대본에서 아래 씬만 검증에 걸렸다. **이 씬들만** 다시 써라.\n"
        "다른 씬은 출력하지 마라. 출력 형식은 처음과 같다:\n"
        '{"scenes": [{"scene_id": N, "text": "..."}]}\n\n'
        f"{listed}\n"
    )


def retry_failed_scenes(
    *,
    llm: LLMClient,
    session_id: str,
    plan: dict[str, Any],
    failures: dict[int, list[str]],
    label: str,
) -> dict[int, str]:
    """실패 씬의 문장만 같은 세션에서 다시 받는다. `{scene_id: text}`.

    재청이 청하지 않은 씬을 내면 버린다 — 씬 구조는 계획에서만 온다.
    """
    result = llm.run(
        build_scene_retry_prompt(plan, failures),
        resume=session_id,
        label=label,
    )
    texts = parse_texts(extract_json_object(result.text), label=label)
    dropped = set(texts) - set(failures)
    if dropped:
        log.warning("[%s] 재청이 청하지 않은 씬을 냈다 (버린다): %s",
                    label, ", ".join(str(s) for s in sorted(dropped)))
    return {sid: text for sid, text in texts.items() if sid in failures}


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
        texts, meta = generate_texts(
            llm=llm, topic=topic, outline=outline, plan=plan,
        )
        scenes = build_scenes(plan, texts, run_id=run_id, topic=topic)
    except ScriptSessionError as exc:
        message = f"{exc} 원본은 {run_dir / 'logs'}에 있다."
        state.mark_failed(STAGE, message)
        raise ScriptSessionError(message) from exc

    # 산출 직후 씬 단위 검증 → 실패 씬만 같은 세션에 재청한다 (ADR-0044).
    # 전체 재집필이 회당 ~10분이던 것이 실패 씬 몇 개의 재청으로 준다.
    failures = scene_text_errors(scenes, factsheet)
    retries = 0
    while failures and retries < MAX_RETRIES and meta.get("session_id"):
        retries += 1
        log.info("[%s] 씬 검증 실패 %d씬 — 같은 세션에 재청 %d/%d: %s",
                 STAGE, len(failures), retries, MAX_RETRIES,
                 ", ".join(str(s) for s in sorted(failures)))
        try:
            fixed = retry_failed_scenes(
                llm=llm, session_id=meta["session_id"], plan=plan,
                failures=failures, label=f"{STAGE}.retry{retries}",
            )
            texts.update(fixed)
            scenes = build_scenes(plan, texts, run_id=run_id, topic=topic)
        except Exception as exc:  # noqa: BLE001 — 재청 실패는 원본 검증 결과로 보고한다
            log.warning("[%s] 재청 실패: %s — 지금 대본으로 보고한다", STAGE, exc)
            break
        failures = scene_text_errors(scenes, factsheet)

    write_text(candidate_path, dump_json(scenes))
    errors, warnings = validate_candidate(scenes, factsheet)
    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)

    info = {
        "output": candidate_path.relative_to(paths.root).as_posix(),
        "scene_count": len(scenes["scenes"]),
        "est_duration": scenes["total_duration"],
        "retries": retries,
        "validation_errors": errors,
        "validation_warnings": warnings,
        **meta,
    }
    if errors:
        # 재청까지 거친 결과다 — 중단·보고 (ADR-0044). [2]는 재생성하지 않는다.
        log.warning("[%s] 검증 실패 %d건 — 중단·보고", STAGE, len(errors))
        state.mark_failed(STAGE, f"검증 실패 {len(errors)}건", **info)
    else:
        state.mark_done(STAGE, **info)

    return WriteResult(
        topic=topic, slug=slug, run_id=run_id, candidate_path=candidate_path,
        scenes=scenes, errors=errors, warnings=warnings,
    )
