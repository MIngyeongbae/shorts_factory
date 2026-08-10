"""[5. prompt] — 씬 계약을 씬별 이미지 프롬프트로 옮긴다.

specs/05-pipeline.md:
    [5. prompt] → prompts.json (씬별 이미지 프롬프트, 스펙 03 룰 적용)

## 경계 (ADR-0017)

입력은 `topics/{slug}/06-script.json` 하나다. **읽기 전용**이다. 산출물은
`runs/{run_id}/prompts.json`뿐이고, `run_id`는 대본 파일에 적힌 값을 그대로 쓴다 —
계보를 run_id로 잇는다는 ADR-0017 그대로다. 이 단계는 `topics/` 아래에 아무것도 쓰지 않는다.

## 이 단계가 판단하지 않는 것

연출은 전부 `schemas/visual_rules.py`의 룰 테이블에서 나온다 (ADR-0001). 이 모듈이
하는 일은 룰 적용 순서를 정하고, 씬을 가로지르는 참조(`전경 유지`, `훅 구도 재사용`)를
풀고, 결과를 계약 형태로 쓰는 것뿐이다. 룰에 없는 연출을 만들지 않는다.

## 외부 의존 없음

순수 텍스트 변환이다. 네트워크도 API 키도 LLM 세션도 쓰지 않는다. 같은 입력이면 항상
같은 바이트가 나오도록 산출물에 타임스탬프를 넣지 않는다 (재현성).

## 게이트는 여기가 아니다

ADR-0017의 `judgment/human.json` 게이트는 2부 **진입점**인 `[3. tts+sync]`가 본다.
`[5]`는 이미 진입한 run 안에서 도는 단계라 게이트를 다시 검사하지 않는다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Paths, write_text
from ..jsonio import dump_json
from ..runstate import RunState
from ..schemas.scenes import validate_scenes
from ..schemas.visual_rules import (
    ASPECT_RATIO,
    BASE_STYLE,
    BEAT_RULES,
    COMPOSITION,
    ECHO_HOOK,
    FRAMINGS,
    GLOBAL_OVERLAYS,
    HOOK_TWIST_FALLBACK,
    INHERIT_PREV,
    OVERLAYS,
    RESOLUTION,
    RULE_GAPS,
    STYLE_ANCHOR_DIR,
    build_annotation,
    build_negative,
    build_prompt,
    framing_conflicts,
    schema_errors,
)

log = logging.getLogger(__name__)

STAGE = "5-prompt"

SCRIPT_FILE = "06-script.json"
PROMPTS_FILE = "prompts.json"

#: 스타일 앵커로 인정하는 확장자 (ADR-0005 / .gitignore 예외 목록과 같다)
ANCHOR_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


class PromptStageError(Exception):
    pass


@dataclass
class PromptResult:
    topic: str
    slug: str
    run_id: str
    prompts_path: Path | None = None
    prompts: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False

    @property
    def scene_count(self) -> int:
        return len(self.prompts["scenes"]) if self.prompts else 0

    @property
    def annotation_count(self) -> int:
        """레이어 A 2-pass 편집이 붙는 씬 수 (= 추가 이미지 호출 수, ADR-0005 비용)."""
        if not self.prompts:
            return 0
        return sum(1 for s in self.prompts["scenes"] if s["annotation_prompt"])

    @property
    def overlay_count(self) -> int:
        """[8. overlay]가 합성해야 하는 레이어 B 항목 수."""
        if not self.prompts:
            return 0
        return sum(
            1 for s in self.prompts["scenes"] for o in s["overlays"] if o["layer"] == "B"
        )

    @property
    def summary(self) -> str:
        tail = " (스킵)" if self.skipped else ""
        return (
            f"[5] {self.topic} — {self.scene_count}씬 / "
            f"레이어A 어노테이션 {self.annotation_count}씬 / "
            f"레이어B 오버레이 {self.overlay_count}건 → {PROMPTS_FILE}{tail}"
        )


def _load_json(path: Path, what: str) -> dict[str, Any]:
    if not path.exists():
        raise PromptStageError(f"{what}이(가) 없다: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PromptStageError(f"{what}을(를) 읽을 수 없다: {path} — {exc}") from exc


def _resolve_framing(
    beat: str,
    prev: tuple[str, int] | None,
    hook: tuple[str, int] | None,
) -> tuple[str, str, int | None]:
    """(구도 토큰, 출처, 참조 씬) — 씬을 가로지르는 구도 참조를 푼다.

    스펙 03의 구도 열에는 다른 씬을 가리키는 값이 둘 있다.
    - hook_twist `전경 유지` → 앞 씬 구도를 잇는다 (이미지가 아니라 구도만)
    - ending_echo `훅과 동일/유사 구도 재사용` → 첫 hook_fact 씬 구도를 다시 쓴다
    """
    token = BEAT_RULES[beat].framing
    if token == INHERIT_PREV:
        if prev is None:
            return HOOK_TWIST_FALLBACK, "beat_rule", None
        return prev[0], "prev_scene", prev[1]
    if token == ECHO_HOOK:
        reference = hook or prev
        if reference is None:
            return HOOK_TWIST_FALLBACK, "beat_rule", None
        return reference[0], "hook_echo", reference[1]
    return token, "beat_rule", None


def _scene_overlays(
    scene: dict[str, Any], beat: str, sid: int
) -> list[dict[str, Any]]:
    """비트 룰 오버레이 + 씬의 emphasis. 레이어는 ADR-0002/0006이 정한다."""
    names = list(BEAT_RULES[beat].overlays)

    emphasis = scene.get("emphasis")
    value_of: dict[str, str] = {}
    if emphasis:
        etype = emphasis["type"]
        if etype not in OVERLAYS:
            # specs/02는 emphasis.type을 "specs/03의 오버레이 타입 enum"이라고 했다.
            # 목록에 없는 값이 오면 연출을 지어내지 않고 멈춘다 (ADR-0001).
            raise PromptStageError(
                f"씬 {sid}: 스펙 03 룰 테이블에 없는 emphasis.type '{etype}'. "
                f"허용: {', '.join(sorted(OVERLAYS))}"
            )
        value_of[etype] = emphasis["value"]
        if etype not in names:
            # 숫자 비트가 아닌데 emphasis가 달린 경우 (specs/02 "그 외 옵션").
            names.append(etype)

    items: list[dict[str, Any]] = []
    for name in names:
        overlay = OVERLAYS[name]
        item: dict[str, Any] = {
            "type": name,
            "layer": overlay.layer,
            "value": value_of.get(name),
        }
        if overlay.layer == "A" and scene["motion"] == "kling":
            # ADR-0006: kling 씬에는 클린 이미지를 넣고 어노테이션은 클립 위에 얹는다
            item["layer"] = "B"
            item["layer_note"] = "kling_clean_input"
        items.append(item)
    return items


def build_prompts(
    script: dict[str, Any], *, source_script: str
) -> tuple[dict[str, Any], list[str]]:
    """씬 계약 → prompts.json 문서. (문서, 경고) 를 돌려준다."""
    scenes: list[dict[str, Any]] = script["scenes"]
    warnings: list[str] = []
    out_scenes: list[dict[str, Any]] = []
    framing_tokens: list[str] = []

    prev: tuple[str, int] | None = None
    hook: tuple[str, int] | None = None
    missing_values: dict[str, list[int]] = {}

    for scene in scenes:
        sid = scene["scene_id"]
        beat = scene["beat"]
        if beat not in BEAT_RULES:
            raise PromptStageError(
                f"씬 {sid}: 스펙 03 룰 테이블에 없는 비트 '{beat}'"
            )
        rule = BEAT_RULES[beat]

        token, source, reference = _resolve_framing(beat, prev, hook)
        framing_tokens.append(token)

        overlays = _scene_overlays(scene, beat, sid)
        overlay_names = tuple(item["type"] for item in overlays)
        layer_a = tuple(item["type"] for item in overlays if item["layer"] == "A")
        for item in overlays:
            if OVERLAYS[item["type"]].needs_value and item["value"] is None:
                missing_values.setdefault(item["type"], []).append(sid)

        if scene["camera"] not in rule.cameras:
            warnings.append(
                f"씬 {sid}: camera={scene['camera']}가 비트 {beat}의 스펙 03 기본값"
                f"({'/'.join(rule.cameras)})과 다르다"
            )

        entry: dict[str, Any] = {
            "scene_id": sid,
            "beat": beat,
            "camera": scene["camera"],
            "motion": scene["motion"],
            "framing": token,
            "framing_source": source,
            "prompt": build_prompt(FRAMINGS[token].shot, scene["subject"]),
            "negative_prompt": build_negative(overlay_names),
            # ADR-0006: kenburns만 2-pass 어노테이션. kling 씬은 위에서 레이어 A가
            # 전부 B로 옮겨가므로 layer_a가 비고 자연히 None이 된다.
            "annotation_prompt": build_annotation(layer_a, scene["subject"]),
            "overlays": overlays,
        }
        if reference is not None:
            entry["framing_reuse_of"] = reference
        out_scenes.append(entry)

        prev = (token, sid)
        if hook is None and beat == "hook_fact":
            hook = (token, sid)

    for overlay_type, ids in sorted(missing_values.items()):
        warnings.append(
            f"레이어 B 텍스트 오버레이 '{overlay_type}'에 넣을 값이 없다 "
            f"(씬 {', '.join(str(i) for i in ids)}). 계약에 출처가 없어 value=null로 뒀다"
        )

    conflicts = framing_conflicts(scenes, framing_tokens)
    if conflicts:
        warnings.append(
            f"스펙 03 구도 룰과 피사체가 부딪히는 씬 {len(conflicts)}개 "
            f"(씬 {', '.join(str(i) for i in conflicts)}). 룰대로 두고 프롬프트는 바꾸지 않았다"
        )

    document = {
        "run_id": script["run_id"],
        "topic": script["topic"],
        "source_script": source_script,
        "style": {
            "base_style": BASE_STYLE,
            "composition": COMPOSITION,
            "aspect_ratio": ASPECT_RATIO,
            "resolution": RESOLUTION,
            "style_anchors": STYLE_ANCHOR_DIR,
            "global_overlays": [dict(o) for o in GLOBAL_OVERLAYS],
        },
        "scenes": out_scenes,
        "rule_gaps": _rule_gaps(scenes, conflicts),
    }
    return document, warnings


def _rule_gaps(
    scenes: list[dict[str, Any]], conflicts: list[int]
) -> list[dict[str, Any]]:
    """이 대본에서 실제로 마주친 '스펙 03이 결정을 남겨 둔 자리'만 싣는다."""
    gaps: list[dict[str, Any]] = []
    for gap in RULE_GAPS:
        ids = [s["scene_id"] for s in scenes if s["beat"] in gap.beats]
        if ids:
            gaps.append(
                {
                    "code": gap.code,
                    "scene_ids": ids,
                    "issue": gap.issue,
                    "resolution": gap.resolution,
                }
            )
    if conflicts:
        gaps.append(
            {
                "code": "framing_subject_conflict",
                "scene_ids": conflicts,
                "issue": "스펙 03의 구도 열은 한국 건축 3편 실측에서 나와 드론 뷰·조감 "
                         "디오라마로 기운다. 이 씬들은 피사체가 근접·내부·도해인데 룰이 "
                         "야외 광각/조감을 지시한다",
                "resolution": "룰대로 광각/조감을 지시했다. 프롬프트는 바꾸지 않았다 — "
                              "메우려면 스펙 03 구도 열에 근접·도해 케이스 행이 필요하다",
            }
        )
    return gaps


def _anchor_warning(paths: Paths) -> str | None:
    """스타일 앵커가 하나도 없으면 [6]이 룩 일관성 수단 없이 돈다 (ADR-0005)."""
    anchor_dir = paths.root / STYLE_ANCHOR_DIR
    if anchor_dir.is_dir() and any(
        p.suffix.lower() in ANCHOR_SUFFIXES for p in anchor_dir.iterdir()
    ):
        return None
    return (
        f"{STYLE_ANCHOR_DIR}/에 스타일 앵커 이미지가 없다. "
        "[6. imagegen]이 룩 일관성 레퍼런스 없이 돌게 된다 (ADR-0005)"
    )


def run_prompt_stage(
    slug: str,
    *,
    paths: Paths | None = None,
    force: bool = False,
) -> PromptResult:
    paths = paths or Paths.from_env()

    script_path = paths.topic_dir(slug) / SCRIPT_FILE
    script = _load_json(script_path, f"씬 계약({SCRIPT_FILE})")

    # 읽기 전용 입력이지만 계약 위반은 여기서 막는다. 깨진 씬으로 만든 프롬프트는
    # [6]에서 돈만 쓰고 실패한다.
    errors, scene_warnings = validate_scenes(script)
    if errors:
        raise PromptStageError(
            f"{script_path}가 씬 계약을 어겼다 ({len(errors)}건): " + "; ".join(errors)
        )

    run_id = script["run_id"]
    topic = script["topic"]
    run_dir = paths.run_dir(run_id)
    prompts_path = run_dir / PROMPTS_FILE
    state = RunState.load_or_create(run_dir, run_id, topic=topic, slug=slug)

    if state.is_done(STAGE) and not force and prompts_path.exists():
        log.info("[%s] 이미 완료된 단계라 스킵한다 (run_id=%s)", STAGE, run_id)
        return PromptResult(
            topic=topic, slug=slug, run_id=run_id, skipped=True,
            prompts_path=prompts_path,
            prompts=_load_json(prompts_path, PROMPTS_FILE),
        )

    state.mark_running(STAGE)

    try:
        document, warnings = build_prompts(
            script, source_script=script_path.relative_to(paths.root).as_posix()
        )
    except PromptStageError as exc:
        # specs/05 실패 정책: 단계 실패는 run 디렉터리에 기록하고 종료
        state.mark_failed(STAGE, str(exc))
        raise
    warnings = list(scene_warnings) + warnings

    output_errors = schema_errors(document)
    if output_errors:
        message = f"{PROMPTS_FILE} 스키마 위반: " + "; ".join(output_errors)
        state.mark_failed(STAGE, message)
        raise PromptStageError(message)

    anchor_warning = _anchor_warning(paths)
    if anchor_warning:
        warnings.append(anchor_warning)

    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)

    write_text(prompts_path, dump_json(document))
    state.mark_done(
        STAGE,
        output=prompts_path.relative_to(paths.root).as_posix(),
        source_script=document["source_script"],
        scenes=len(document["scenes"]),
        rule_gaps=[gap["code"] for gap in document["rule_gaps"]],
        warnings=warnings,
    )

    return PromptResult(
        topic=topic, slug=slug, run_id=run_id,
        prompts_path=prompts_path, prompts=document, warnings=warnings,
    )
