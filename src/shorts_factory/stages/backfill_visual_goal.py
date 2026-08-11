"""[1y. backfill-visual-goal] — 확정된 대본에 `visual_goal`만 채운다. ADR-0022.

ADR-0022가 씬 계약에 `visual_goal`을 required로 추가했다. 그 전에 만들어져 이미 사람이
승인한 `06-script.json`에는 이 필드가 없다. `[1. script]`를 다시 돌리면 대본 자체가
달라지므로(피사 대본은 재생성 2회 만에 수렴한 것이다) **대본을 그대로 두고 필드만
채우는** 일회성 경로가 따로 필요하다. `[1x. backfill-scale]`(ADR-0018)과 같은 계열이고
쓰기 전 가드도 그 모듈의 것을 공유한다.

## 이 단계가 절대 하지 않는 것

`text`·`beat`·`subject`·`subject_scale`·`est_*`·`emphasis`·`camera`·`motion`·`notes`를
건드리지 않는다. 쓰기 전에 실제로 비교해서 확인하고, 하나라도 달라지면 파일을 쓰지 않고
실패한다 — 승인된 대본을 조용히 바꾸는 것이 이 단계가 낼 수 있는 최악의 결과다.

## 세션에 자막과 피사체를 함께 보여준다

`[1x]`는 `subject`만 보여줬다(분류에 본문이 필요 없었다). 여기서는 **본문이 무엇을
말하고 무엇을 말하지 않는지**를 알아야 그림이 질 몫이 나오므로 `text`도 함께 준다.
대신 대본을 고치라고는 하지 않는다 — 출력 스키마에 `text`가 없다.

## 새 토픽에는 쓰지 않는다

`[1]`이 `visual_goal`을 직접 출력하므로(ADR-0022) 이 단계는 필드가 없는 옛 대본에만
해당한다. 이미 전 씬에 값이 있으면 세션을 부르지 않고 그대로 끝낸다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any

from ..config import Paths, write_text
from ..jsonio import JSONExtractionError, dump_json, extract_json_object
from ..llm.base import LLMClient
from ..schemas.scenes import (
    VISUAL_GOAL_OVERLAP_LIMIT,
    validate_scenes,
    visual_goal_overlap,
)
from .backfill_scale import BackfillStageError, assert_only_scale_changed

log = logging.getLogger(__name__)

STAGE = "1y-backfill-visual-goal"
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
PROMPT = "07-visual-goal.md"

SCRIPT_FILE = "06-script.json"

#: 씬마다 한 구절씩 쓰는 세션이라 대본 생성보다 짧다.
TIMEOUT = 600

#: 팩트시트도 웹도 필요 없다 — 프롬프트에 자막과 피사체가 전부 들어간다 (ADR-0011).
TOOLS: tuple[str, ...] = ()

#: 이 단계가 채우는 필드. 이것 말고는 전부 그대로여야 한다.
ADDED_FIELD = "visual_goal"


@dataclass
class VisualGoalResult:
    topic: str
    slug: str
    script_path: Path
    scene_count: int = 0
    skipped: bool = False

    @property
    def summary(self) -> str:
        if self.skipped:
            return f"[1y] {self.topic} — 전 씬에 {ADDED_FIELD}가 이미 있다 (스킵)"
        return (
            f"[1y] {self.topic} — {self.scene_count}씬에 {ADDED_FIELD} 채움 "
            f"→ {SCRIPT_FILE}"
        )


def _load_prompt() -> Template:
    path = PROMPTS_DIR / PROMPT
    if not path.exists():
        raise BackfillStageError(f"프롬프트 파일이 없다: {path}")
    return Template(path.read_text(encoding="utf-8"))


def render_scenes(scenes: list[dict[str, Any]]) -> str:
    """세션에 보여줄 판정 대상. 자막과 피사체를 함께 준다."""
    return "\n".join(
        f"- scene_id {scene['scene_id']}\n"
        f"  자막: {scene['text']}\n"
        f"  피사체: {scene['subject']}"
        for scene in scenes
    )


def parse_goals(
    payload: dict[str, Any], scenes: list[dict[str, Any]]
) -> dict[int, str]:
    """세션 출력 → `{scene_id: visual_goal}`.

    빠진 씬·모르는 씬·빈 값은 전부 실패로 돌린다. 일부만 채워 놓으면 나머지가 왜
    비었는지 나중에 구분할 수 없다. **자막을 되풀이한 값도 여기서 건다** — 계약
    검증까지 가면 어느 씬이 왜 걸렸는지가 흐려진다.
    """
    raw = payload.get("goals")
    if not isinstance(raw, list):
        raise BackfillStageError("세션 출력에 goals 배열이 없다")

    by_id = {scene["scene_id"]: scene for scene in scenes}
    goals: dict[int, str] = {}
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise BackfillStageError(f"{index}번째 항목이 객체가 아니다: {item!r}")
        sid = item.get("scene_id")
        goal = str(item.get(ADDED_FIELD, "")).strip()
        if sid not in by_id:
            raise BackfillStageError(f"{index}번째 항목의 scene_id가 대본에 없다: {sid!r}")
        if not goal:
            raise BackfillStageError(f"씬 {sid}의 {ADDED_FIELD}가 비어 있다")
        if sid in goals:
            raise BackfillStageError(f"씬 {sid}가 두 번 나왔다")

        overlap = visual_goal_overlap(by_id[sid]["text"], goal)
        if overlap >= VISUAL_GOAL_OVERLAP_LIMIT:
            raise BackfillStageError(
                f"씬 {sid}의 {ADDED_FIELD}가 자막과 {overlap:.0%} 겹친다 "
                f"(상한 {VISUAL_GOAL_OVERLAP_LIMIT:.0%}). 자막을 바꿔 쓴 것은 "
                "그림이 지는 설명이 아니다 (ADR-0022)"
            )
        goals[sid] = goal

    missing = sorted(set(by_id) - set(goals))
    if missing:
        raise BackfillStageError(
            f"판정이 빠진 씬 {len(missing)}개: {', '.join(str(i) for i in missing)}"
        )
    return goals


def apply_goals(script: dict[str, Any], goals: dict[int, str]) -> dict[str, Any]:
    """`visual_goal`만 끼운 새 문서. 원본은 건드리지 않는다.

    필드는 `subject` **앞**에 놓는다 — 스펙 02의 씬 스키마 순서 그대로이고,
    "목표가 먼저고 피사체가 그다음"이라는 ADR-0022의 순서를 파일에서도 읽히게 한다.
    """
    scenes: list[dict[str, Any]] = []
    for scene in script["scenes"]:
        rebuilt: dict[str, Any] = {}
        for key, value in scene.items():
            if key == ADDED_FIELD:
                continue  # 이미 있으면 새 판정으로 덮어쓴다
            if key == "subject":
                rebuilt[ADDED_FIELD] = goals[scene["scene_id"]]
            rebuilt[key] = value
        scenes.append(rebuilt)

    updated = dict(script)
    updated["scenes"] = scenes
    return updated


def run_backfill_visual_goal_stage(
    slug: str,
    *,
    llm: LLMClient,
    paths: Paths | None = None,
    force: bool = False,
) -> VisualGoalResult:
    paths = paths or Paths.from_env()

    script_path = paths.topic_dir(slug) / SCRIPT_FILE
    if not script_path.exists():
        raise BackfillStageError(
            f"대본이 없다: {script_path}. [2. validate]가 끝난 토픽에만 쓴다."
        )
    try:
        script = json.loads(script_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BackfillStageError(f"{SCRIPT_FILE}을(를) 읽을 수 없다: {exc}") from exc

    scenes = script.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise BackfillStageError(f"{script_path}에 scenes 배열이 없다")

    topic = script.get("topic", slug)

    if not force and all(scene.get(ADDED_FIELD) for scene in scenes):
        log.info("[%s] 전 씬에 %s가 이미 있다 (slug=%s)", STAGE, ADDED_FIELD, slug)
        return VisualGoalResult(
            topic=topic, slug=slug, script_path=script_path,
            scene_count=len(scenes), skipped=True,
        )

    prompt = _load_prompt().safe_substitute(scenes=render_scenes(scenes))
    log.info("[%s] 세션 시작 — %d씬 (도구 없음)", STAGE, len(scenes))
    result = llm.run(prompt, allowed_tools=TOOLS, timeout=TIMEOUT, label=STAGE)

    try:
        payload = extract_json_object(result.text)
    except JSONExtractionError as exc:
        raise BackfillStageError(f"세션 출력이 JSON 객체가 아니다: {exc}") from exc

    goals = parse_goals(payload, scenes)
    updated = apply_goals(script, goals)
    assert_only_scale_changed(script, updated, field_name=ADDED_FIELD)

    errors, _warnings = validate_scenes(updated)
    if errors:
        listed = "\n".join(f"  - {e}" for e in errors[:5])
        raise BackfillStageError(
            f"{ADDED_FIELD}를 채운 대본이 씬 계약을 어긴다:\n{listed}"
        )

    write_text(script_path, dump_json(updated))

    return VisualGoalResult(
        topic=topic, slug=slug, script_path=script_path, scene_count=len(scenes),
    )
