"""[5. prompt] — 씬 계약 위에서 **헤드리스 세션 1회**가 씬별 샷 서술을 쓰고, 코드가 골격에 얹는다. ADR-0060.

specs/05-pipeline.md:
    [5. prompt] → prompts.json (씬별 영상 프롬프트 — 어휘 골격 + 세션 단락, 스펙 03)

## 경계 (ADR-0017 — ADR-0052 — ADR-0060)

입력은 넷이고 전부 **읽기 전용**이다:

- `runs/{run_id}/scenes.json` — 씬 계약 (`[3s]`). 연출·무대·라벨·인물은 여기서 정해졌다
- `topics/{slug}/script.md` — 대본 전문. 머리(핵심 질문·반전·보이는 것)가 설계도다
- `topics/{slug}/factcheck.md` — 형태·치수·연도·재질의 근거. 없으면 "(없음)"으로 간다 (D-3)
- `runs/{run_id}/refs.json` — `[4]`의 실사 서술. 없으면 그냥 간다 (ADR-0030)

산출물은 `runs/{run_id}/prompts.json`뿐이고 출력 계약은 ADR-0056의 것 그대로다 — `[7]`은 이
ADR을 모른다 (단계 독립 D-2). `topics/` 아래에는 아무것도 쓰지 않는다.

## 세션이 쓰는 것과 코드가 쓰는 것

세션(`prompts/05-prompt.md`)은 씬마다 `subject_prompt`(SUBJECT 단락)·`camera_target`(착지)·
`red_prompt`(info 씬의 계측 기하)를 **영어**로 쓴다. 계약은 `specs/schema/promptplan.schema.json`
(`schemas/promptplan.py`) — ASCII·길이·라벨 포함·착지에 워크 단어 금지·씬 id 일치. 코드는 FORMAT·
STAGING·CAMERA 워크·NEGATIVE를 어휘에서 조립해 앞뒤에 붙인다 (`visual_rules.build_video_prompt`).
**연출은 여전히 씬 계약의 것이다** (ADR-0033 §3) — 세션은 그것을 서술로 구현할 뿐 바꾸지 못한다.

## 실패는 보고·중단이다

세션 산출이 계약을 어기면 `prompts.json`을 쓰지 않고 멈춘다 (ADR-0044 — 재생성 루프 없음).
세션 출력 원본은 `logs/`에 남는다. 지난 실행의 `prompts.json`이 남아 있으면 지운다 — 깨진 계약
파일이 남으면 `[7]`이 그대로 진행해 돈을 쓴다.

## 길이를 쓰지 않는다

클립 길이는 `[7]`이 세 언어의 실측에서 정한다 (ADR-0056 결정 2). FORMAT 절의 초 수는
`{seconds}` 자리표시자로 남고 `[7]`이 그때 채운다.
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
from ..schemas import promptplan
from ..schemas import refs as refs_schema
from ..schemas import vocab
from ..schemas.scenes import validate_scenes
from ..schemas.visual_rules import (
    ANNOTATIONS,
    ASPECT_RATIO,
    BASE_STYLE,
    CAMERA_PROMPTS,
    COMPOSITION,
    FRAMINGS,
    FROM_DEFAULT,
    RESOLUTION,
    STAGINGS,
    build_video_prompt,
    resolve_framing,
    resolve_staging,
    schema_errors,
)
from .contract import SceneContractNotFound, load_scene_contract
from .session import ScriptSessionError, ask_json, load_prompt

log = logging.getLogger(__name__)

STAGE = "5-prompt"
PROMPT_FILE = "05-prompt.md"
PROMPTS_FILE = "prompts.json"
REFS_FILE = refs_schema.RECORD_FILE
SCRIPT_FILE = "script.md"
FACTCHECK_FILE = "factcheck.md"

#: 세션 상한(초). 씬 16개 × 영어 단락이라 `[3s]`보다 길다.
TIMEOUT = 900


class PromptStageError(Exception):
    """입력 부재·계약 위반 — 보고·중단."""


@dataclass
class PromptResult:
    topic: str
    slug: str
    run_id: str
    prompts_path: Path | None = None
    prompts: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: bool = False
    #: `subject_anchor`가 비어 있지 않은 씬 수 (ADR-0028 관측). 앵커는 이제 프롬프트에
    #: 실리지 않고(ADR-0060 결정 5) 세션이 영어로 푼다 — 씬 계약에서 센다.
    anchored_scenes: int = 0
    #: `refs.json`의 서술을 세션에 준 씬 수 (ADR-0030 관측).
    described_scenes: int = 0
    #: 세션 실행 메타 (session_id·turns·시간).
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.errors and self.prompts_path is not None

    @property
    def scene_count(self) -> int:
        return len(self.prompts["scenes"]) if self.prompts else 0

    @property
    def scale_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for scene in self.prompts["scenes"] if self.prompts else []:
            counts[scene["subject_scale"]] = counts.get(scene["subject_scale"], 0) + 1
        return counts

    def _defaulted(self, key: str) -> int:
        if not self.prompts:
            return 0
        return sum(1 for s in self.prompts["scenes"] if s[key] == FROM_DEFAULT)

    @property
    def default_framed_scenes(self) -> int:
        """구도를 씬이 고르지 않아 기본값으로 떨어진 씬 수 (ADR-0033 되돌릴 조건의 관측)."""
        return self._defaulted("framing_source")

    @property
    def default_staged_scenes(self) -> int:
        return self._defaulted("staging_source")

    @property
    def info_scenes(self) -> int:
        if not self.prompts:
            return 0
        return sum(1 for s in self.prompts["scenes"] if s["has_info"])

    @property
    def summary(self) -> str:
        if self.errors:
            return f"[5] {self.topic} — 샷 서술 계약 위반 {len(self.errors)}건, {PROMPTS_FILE}을 쓰지 않았다"
        tail = " (스킵)" if self.skipped else ""
        scales = " / ".join(
            f"{scale} {count}" for scale, count in sorted(self.scale_counts.items())
        )
        return (
            f"[5] {self.topic} — {self.scene_count}씬 ({scales}) / "
            f"인포 {self.info_scenes}씬 / "
            f"대상 앵커 {self.anchored_scenes}씬 / "
            f"참조 서술 {self.described_scenes}씬 / "
            f"구도 기본값 {self.default_framed_scenes}씬 / "
            f"무대 기본값 {self.default_staged_scenes}씬 "
            f"→ {PROMPTS_FILE}{tail}"
        )


def _load_json(path: Path, what: str) -> dict[str, Any]:
    if not path.exists():
        raise PromptStageError(f"{what}이 없다: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PromptStageError(f"{what}을 읽을 수 없다: {path} — {exc}") from exc


# --- 세션 입력 -----------------------------------------------------------------


def scene_brief(scene: dict[str, Any]) -> dict[str, Any]:
    """씬 하나를 세션에 보여 줄 모양 — 계약 값 + 그 값의 **어휘 문구**(세션이 구현할 연출).

    구도·카메라·무대·주석 문구는 어휘에서 온다 (ADR-0034). 세션은 이 문구가 말하는 연출을
    서술로 구현하고, 값을 바꾸지 못한다.
    """
    token, framing_source = resolve_framing(scene)
    staging, staging_source = resolve_staging(scene)
    brief: dict[str, Any] = {
        "scene_id": scene["scene_id"],
        "text": scene.get("text", ""),
        "beat": scene.get("beat"),
        "visual_goal": scene.get("visual_goal", ""),
        "subject": scene.get("subject", ""),
        "subject_anchor": list(scene.get("subject_anchor") or []),
        "subject_scale": scene.get("subject_scale"),
        "staging": {"value": staging, "phrase": STAGINGS[staging], "source": staging_source},
        "framing": {"value": token, "shot": FRAMINGS[token].shot, "source": framing_source},
        "camera": {"value": scene["camera"], "phrase": CAMERA_PROMPTS[scene["camera"]]},
    }
    info = scene.get("info") or None
    if info:
        brief["info"] = {
            "labels": list(info.get("labels", [])),
            "target": info.get("target", ""),
            "annotation": {
                "value": info.get("annotation"),
                "phrase": ANNOTATIONS.get(str(info.get("annotation")), ""),
            },
        }
    shot2 = scene.get("shot2") or None
    if shot2:
        brief["shot2"] = {
            "framing": {"value": shot2["framing"], "shot": FRAMINGS[shot2["framing"]].shot},
            "camera": {"value": shot2["camera"], "phrase": CAMERA_PROMPTS[shot2["camera"]]},
        }
    if scene.get("cast"):
        brief["cast"] = list(scene["cast"])
    return brief


def format_scenes(contract: dict[str, Any]) -> str:
    return "\n".join(
        json.dumps(scene_brief(scene), ensure_ascii=False) for scene in contract["scenes"]
    )


def format_characters(contract: dict[str, Any]) -> str:
    characters = contract.get("characters") or []
    if not characters:
        return ""
    body = "\n".join(json.dumps(c, ensure_ascii=False) for c in characters)
    return (
        "## 인물 (characters — `cast` 씬의 SUBJECT에 `appearance`를 그대로 싣는다, ADR-0051)\n\n"
        + body
    )


def format_refs(refs: dict[str, Any] | None, contract: dict[str, Any]) -> tuple[str, int]:
    """`(절 텍스트, 서술이 있는 씬 수)`. 없으면 빈 절."""
    if not refs:
        return "", 0
    lines = []
    for scene in contract["scenes"]:
        sid = scene["scene_id"]
        description = refs_schema.description_of(refs, sid)
        if description:
            lines.append(f"- 씬 {sid}: {description}")
    if not lines:
        return "", 0
    return (
        "## 실사 참조 서술 (refs.json — [4]가 실물 사진을 보고 적은 형태. SUBJECT의 형태 근거로 쓴다)\n\n"
        + "\n".join(lines),
        len(lines),
    )


def build_session_prompt(
    *,
    topic: str,
    script_text: str,
    factcheck: str | None,
    contract: dict[str, Any],
    refs: dict[str, Any] | None,
) -> tuple[str, int]:
    """세션 프롬프트 전문. `(prompt, 참조 서술 씬 수)`."""
    refs_block, described = format_refs(refs, contract)
    subject_min, subject_max = promptplan.length_limits(promptplan.SUBJECT_FIELD)
    target_min, target_max = promptplan.length_limits(promptplan.CAMERA_TARGET_FIELD)
    red_min, red_max = promptplan.length_limits(promptplan.RED_FIELD)
    prompt = load_prompt(PROMPT_FILE).substitute(
        topic=topic,
        script=script_text.strip(),
        factcheck=(factcheck or "(없음 — 팩트체크가 없어 형태 근거는 대본 머리뿐이다)").strip(),
        scenes=format_scenes(contract),
        characters=format_characters(contract),
        refs=refs_block,
        subject_min=subject_min, subject_max=subject_max,
        target_min=target_min, target_max=target_max,
        red_min=red_min, red_max=red_max,
    )
    return prompt, described


# --- 세션 산출 → prompts.json ---------------------------------------------------


def build_prompts(
    contract: dict[str, Any],
    plan: dict[str, Any],
    *,
    source_script: str,
) -> dict[str, Any]:
    """씬 계약 + 세션 단락 → prompts.json 문서. `plan`은 `promptplan.validate_promptplan`을 통과한 것."""
    planned = {int(e["scene_id"]): e for e in plan["scenes"]}
    out_scenes: list[dict[str, Any]] = []
    for scene in contract["scenes"]:
        sid = int(scene["scene_id"])
        entry = planned[sid]
        token, framing_source = resolve_framing(scene)
        staging, staging_source = resolve_staging(scene)
        info = scene.get("info") or None
        try:
            prompt_text, negative_text = build_video_prompt(
                subject_prompt=str(entry[promptplan.SUBJECT_FIELD]),
                staging=staging,
                camera=scene["camera"],
                camera_target=str(entry.get(promptplan.CAMERA_TARGET_FIELD, "")),
                red_prompt=str(entry[promptplan.RED_FIELD]) if info else None,
            )
        except ValueError as exc:
            raise PromptStageError(f"씬 {sid}: {exc}") from exc
        record: dict[str, Any] = {
            "scene_id": sid,
            "beat": scene["beat"],
            "subject_scale": scene["subject_scale"],
            "camera": scene["camera"],
            "staging": staging,
            "staging_source": staging_source,
            "framing": token,
            "framing_source": framing_source,
            "has_info": info is not None,
            "prompt": prompt_text,
            "negative_prompt": negative_text,
        }
        if scene.get("cast"):
            record["cast"] = list(scene["cast"])
        out_scenes.append(record)
    return {
        "run_id": contract["run_id"],
        "topic": contract["topic"],
        "source_script": source_script,
        "style": {
            "base_style": BASE_STYLE,
            "composition": COMPOSITION,
            "aspect_ratio": ASPECT_RATIO,
            "resolution": RESOLUTION,
        },
        "scenes": out_scenes,
    }


def _anchored_scenes(contract: dict[str, Any]) -> int:
    return sum(
        1 for s in contract["scenes"]
        if any(str(a).strip() for a in (s.get("subject_anchor") or []))
    )


def _load_refs(run_dir: Path, run_id: str) -> tuple[dict[str, Any] | None, list[str]]:
    path = run_dir / REFS_FILE
    if not path.exists():
        return None, []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, [f"{REFS_FILE}을 읽을 수 없어 서술 없이 간다: {exc}"]
    errors = refs_schema.schema_errors(document)
    if errors:
        return None, [f"{REFS_FILE}이 계약을 어겨 서술 없이 간다: {errors[0]}"]
    if document.get("run_id") != run_id:
        return None, [f"{REFS_FILE}의 run_id({document.get('run_id')})가 다르다 — 서술 없이 간다"]
    return document, []


def run_prompt_stage(
    slug: str,
    *,
    llm: LLMClient,
    paths: Paths | None = None,
    force: bool = False,
    timeout: int = TIMEOUT,
) -> PromptResult:
    paths = paths or Paths.from_env()

    try:
        contract, contract_path = load_scene_contract(paths, slug)
    except SceneContractNotFound as exc:
        raise PromptStageError(str(exc)) from exc

    errors, scene_warnings = validate_scenes(contract)
    if errors:
        raise PromptStageError(
            f"{contract_path}가 씬 계약을 어겼다 ({len(errors)}건): " + "; ".join(errors)
        )

    run_id = contract["run_id"]
    topic = contract["topic"]
    run_dir = paths.run_dir(run_id)
    prompts_path = run_dir / PROMPTS_FILE
    state = RunState.load_or_create(run_dir, run_id, topic=topic, slug=slug)

    if state.is_done(STAGE) and not force and prompts_path.exists():
        previous = _load_json(prompts_path, PROMPTS_FILE)
        log.info("[%s] 이미 완료된 단계라 스킵한다 (run_id=%s)", STAGE, run_id)
        return PromptResult(
            topic=topic, slug=slug, run_id=run_id, skipped=True,
            prompts_path=prompts_path, prompts=previous,
            anchored_scenes=_anchored_scenes(contract),
        )

    script_path = paths.topic_dir(slug) / SCRIPT_FILE
    if not script_path.exists():
        raise PromptStageError(f"대본이 없다: {script_path}")
    script_text = script_path.read_text(encoding="utf-8")
    factcheck_path = paths.topic_dir(slug) / FACTCHECK_FILE
    factcheck = factcheck_path.read_text(encoding="utf-8") if factcheck_path.exists() else None

    state.mark_running(STAGE)
    if prompts_path.exists():
        prompts_path.unlink()

    refs_doc, refs_warnings = _load_refs(run_dir, run_id)
    warnings = list(scene_warnings) + refs_warnings
    if factcheck is None:
        warnings.append(f"{FACTCHECK_FILE}이 없어 형태 근거 없이 돈다 — 세션은 대본 머리만 본다 (D-3)")

    prompt, described = build_session_prompt(
        topic=topic, script_text=script_text, factcheck=factcheck,
        contract=contract, refs=refs_doc,
    )
    try:
        plan, meta = ask_json(llm, prompt, label=STAGE, tools=(), timeout=timeout)
    except ScriptSessionError as exc:
        state.mark_failed(STAGE, str(exc))
        raise PromptStageError(str(exc)) from exc

    plan_errors = promptplan.validate_promptplan(plan, contract)
    if plan_errors:
        for error in plan_errors:
            log.error("[%s] %s", STAGE, error)
        state.mark_failed(STAGE, f"샷 서술 계약 위반 {len(plan_errors)}건", errors=plan_errors, **meta)
        return PromptResult(
            topic=topic, slug=slug, run_id=run_id, errors=plan_errors, warnings=warnings, meta=meta,
            anchored_scenes=_anchored_scenes(contract), described_scenes=described,
        )

    try:
        document = build_prompts(
            contract, plan, source_script=contract_path.relative_to(paths.root).as_posix(),
        )
    except PromptStageError as exc:
        state.mark_failed(STAGE, str(exc))
        raise

    output_errors = schema_errors(document)
    if output_errors:
        message = f"{PROMPTS_FILE} 스키마 위반: " + "; ".join(output_errors)
        state.mark_failed(STAGE, message)
        raise PromptStageError(message)

    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)

    write_text(prompts_path, dump_json(document))
    result = PromptResult(
        topic=topic, slug=slug, run_id=run_id,
        prompts_path=prompts_path, prompts=document, warnings=warnings, meta=meta,
        anchored_scenes=_anchored_scenes(contract), described_scenes=described,
    )
    state.mark_done(
        STAGE,
        output=prompts_path.relative_to(paths.root).as_posix(),
        source_script=document["source_script"],
        scenes=len(document["scenes"]),
        info_scenes=result.info_scenes,
        anchored_scenes=result.anchored_scenes,
        described_scenes=described,
        default_framed_scenes=result.default_framed_scenes,
        default_staged_scenes=result.default_staged_scenes,
        warnings=warnings,
        **meta,
    )
    return result
