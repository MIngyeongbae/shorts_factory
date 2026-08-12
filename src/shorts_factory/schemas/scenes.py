"""씬 계약(scenes.json) 스키마 및 검증. specs/02-beat-schema.md + specs/05-pipeline.md.

[1. script]가 대본과 비트 태그를 한 번에 출력하므로(ADR-0003) 이 계약이 대본 단계의
첫 기계 검증 지점이다.

씬 1개 = 자막 줄(SRT 큐) 1개다 (ADR-0013). 문장이 아니라서 한 씬의 `text`에 문장이
1~3개 들어갈 수 있다.

이 모듈은 **스펙 02의 씬 스키마와 스펙 05의 봉투(run_id/topic/total_duration/scenes)만**
본다. 스펙 01의 대본 규칙(글자 수, 자막 줄 수, 시그니처 문구, 수미상관)과 ADR-0007
그라운딩은 이 모듈이 손대지 않는다 — 별도 검증기가 맡는다.

`est_start`/`est_end`는 TTS 이전 추정치다. TTS 후 `start`/`end`로 바뀐 형태(스펙 02)는
[3. tts+sync] 시점의 계약이라 여기서 다루지 않는다.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from .visual_rules import OVERLAY_TYPES, SUBJECT_SCALES

#: specs/02-beat-schema.md 비트 타입 테이블
BEATS = (
    "hook_fact",
    "hook_twist",
    "context",
    "context_number",
    "failed_solution",
    "failure_reason",
    "dilemma_peak",
    "turning_point",
    "solution_step",
    "solution_number",
    "present_link",
    "ending_echo",
)

#: specs/02 — "숫자 비트는 필수, 그 외 옵션". emphasis를 반드시 달아야 하는 비트.
NUMBER_BEATS = ("context_number", "solution_number")

#: specs/02 — 복합 카메라 워크 금지 (AI 영상 왜곡 방지)
CAMERAS = (
    "slow_zoom_in",
    "slow_zoom_out",
    "tilt_down",
    "tilt_up",
    "pan_left",
    "pan_right",
    "static",
)

MOTIONS = ("kenburns", "kling")

#: specs/02 + ADR-0006 — kling은 편당 최대 10씬
MAX_KLING_SCENES = 10

#: total_duration과 마지막 씬 est_end의 허용 오차(초). 경고 판정에만 쓴다.
DURATION_TOLERANCE = 0.5

SCENE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "scene_id",
        "beat",
        "text",
        "est_start",
        "est_end",
        "visual_goal",
        "subject",
        "subject_scale",
        "camera",
        "motion",
    ],
    "additionalProperties": False,
    "properties": {
        "scene_id": {"type": "integer", "minimum": 1},
        "beat": {"enum": list(BEATS)},
        "text": {"type": "string", "minLength": 1},
        "est_start": {"type": "number", "minimum": 0},
        "est_end": {"type": "number", "minimum": 0},
        "emphasis": {
            "type": "object",
            "required": ["type", "value"],
            "additionalProperties": False,
            "properties": {
                # specs/02 — "specs/03의 오버레이 타입 enum". 그 표가 ADR-0019로
                # 생겼으므로 문자열이 아니라 enum으로 조인다.
                "type": {"enum": list(OVERLAY_TYPES)},
                "value": {"type": "string", "minLength": 1},
            },
        },
        # specs/02 + ADR-0022 — 이 그림이 지는 설명. subject보다 먼저 정해진다.
        # 그림이 시간을 채우는 것이 아니라 설명을 대신하게 하는 필드다.
        "visual_goal": {"type": "string", "minLength": 1},
        "subject": {"type": "string", "minLength": 1},
        # specs/02 + ADR-0018 — beat와 함께 구도를 결정한다. 연출이 아니라 피사체 서술이라
        # [1. script]가 subject와 함께 쓴다.
        "subject_scale": {"enum": list(SUBJECT_SCALES)},
        # specs/02 + ADR-0028 — 대상을 고정하는 짧은 한국어 명사구 목록.
        # **required가 아니다.** 재질도 정체도 무의미한 씬이 있고(도해), 옛 대본 3편이
        # 그대로 이 계약을 만족해야 한다 — 백필 단계를 또 만들지 않기 위한 선택이다.
        # 없다고 실패시키지도 경고하지도 않는다. 있을 때 타입만 본다.
        "subject_anchor": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "camera": {"enum": list(CAMERAS)},
        "motion": {"enum": list(MOTIONS)},
        "notes": {"type": "string"},
    },
}

SCENES_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "scenes.json (씬 계약)",
    "type": "object",
    "required": ["run_id", "topic", "total_duration", "scenes"],
    "additionalProperties": False,
    "properties": {
        "run_id": {"type": "string", "minLength": 1},
        "topic": {"type": "string", "minLength": 1},
        "total_duration": {"type": "number", "exclusiveMinimum": 0},
        "scenes": {"type": "array", "minItems": 1, "items": SCENE_SCHEMA},
    },
}

_VALIDATOR = Draft202012Validator(SCENES_SCHEMA)


def schema_errors(data: Any) -> list[str]:
    """JSON Schema 위반 목록."""
    errors = []
    for err in sorted(_VALIDATOR.iter_errors(data), key=lambda e: list(e.absolute_path)):
        location = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{location}: {err.message}")
    return errors


#: `visual_goal`이 `text`를 되풀이했다고 볼 겹침 비율 (ADR-0022).
#: 문자 바이그램 기준이다 — 한국어라 형태소 분석기 없이 재려면 이게 가장 단순하다.
#: 0.7은 "본문을 살짝 바꿔 쓴 문장"은 걸고 "본문이 안 한 말"은 통과시키는 선이다.
VISUAL_GOAL_OVERLAP_LIMIT = 0.7


def _bigrams(text: str) -> set[str]:
    """공백·문장부호를 뺀 문자 바이그램 집합."""
    core = "".join(ch for ch in text if ch.isalnum())
    return {core[i : i + 2] for i in range(len(core) - 1)}


def visual_goal_overlap(text: str, visual_goal: str) -> float:
    """`visual_goal`이 `text` 안에 얼마나 들어 있는가 (0~1).

    **그림이 새로 지는 설명이 있는지를 재는 값이다** (ADR-0022). 1에 가까우면
    본문이 이미 한 말을 그림 지시가 되풀이한 것이고, 그 그림은 시간만 채운다.
    """
    goal = _bigrams(visual_goal)
    if not goal:
        return 1.0
    return len(goal & _bigrams(text)) / len(goal)


def semantic_errors(data: dict[str, Any]) -> list[str]:
    """스펙 02의 교차 규칙 검사 (스키마로 표현 불가한 부분)."""
    errors: list[str] = []
    scenes: list[dict[str, Any]] = data.get("scenes", [])

    # 규칙: scene_id는 1부터 연번이며 자막 줄 순서와 일치 (specs/02, ADR-0013)
    expected = list(range(1, len(scenes) + 1))
    actual = [s.get("scene_id") for s in scenes]
    if actual != expected:
        errors.append(
            f"scenes: scene_id가 1부터 연번이 아니다 (기대 {expected[:3]}…, 실제 {actual[:3]}…)"
        )

    prev_end: float | None = None
    for scene in scenes:
        sid = scene.get("scene_id", "?")
        start = scene.get("est_start")
        end = scene.get("est_end")

        # ADR-0022 — 그림이 본문을 되풀이하면 그 씬의 그림은 하는 일이 없다.
        # 시간만 채우는 그림을 여기서 거른다.
        overlap = visual_goal_overlap(scene.get("text", ""), scene.get("visual_goal", ""))
        if overlap >= VISUAL_GOAL_OVERLAP_LIMIT:
            errors.append(
                f"scenes/{sid}: visual_goal이 text와 {overlap:.0%} 겹친다 "
                f"(상한 {VISUAL_GOAL_OVERLAP_LIMIT:.0%}). 그림이 본문을 되풀이하면 "
                "설명을 지지 않는다 — 본문이 말하지 않고 넘어가는 것을 적어라 (ADR-0022)"
            )

        if start >= end:
            errors.append(f"scenes/{sid}: est_start({start}) >= est_end({end})")
        # 씬은 자막 줄 순서를 그대로 따르므로 시간이 역행하거나 겹칠 수 없다.
        # (스펙 02에 명시된 문장은 아니고 "scene_id는 자막 줄 순서와 일치"에서 파생한 규칙)
        elif prev_end is not None and start < prev_end:
            errors.append(
                f"scenes/{sid}: est_start({start})가 앞 씬의 est_end({prev_end})보다 이르다"
            )
        prev_end = end

        # 규칙: 숫자 비트는 emphasis 필수 (specs/02)
        if scene.get("beat") in NUMBER_BEATS and not scene.get("emphasis"):
            errors.append(
                f"scenes/{sid}: 숫자 비트({scene.get('beat')})인데 emphasis가 없다"
            )

    # 규칙: kling은 편당 최대 10씬 (specs/02, ADR-0006)
    kling = [s.get("scene_id") for s in scenes if s.get("motion") == "kling"]
    if len(kling) > MAX_KLING_SCENES:
        errors.append(
            f"scenes: motion=kling 씬이 {len(kling)}개다 (편당 최대 {MAX_KLING_SCENES}개)"
        )

    return errors


def semantic_warnings(data: dict[str, Any]) -> list[str]:
    """차단하지는 않지만 하류 단계가 알아야 할 사항."""
    warnings: list[str] = []
    scenes: list[dict[str, Any]] = data.get("scenes", [])
    if not scenes:
        return warnings

    last_end = scenes[-1].get("est_end")
    total = data.get("total_duration")
    if abs(float(total) - float(last_end)) > DURATION_TOLERANCE:
        warnings.append(
            f"total_duration({total})과 마지막 씬 est_end({last_end})의 차이가 "
            f"{DURATION_TOLERANCE}초를 넘는다"
        )
    return warnings


def validate_scenes(data: Any) -> tuple[list[str], list[str]]:
    """(errors, warnings)를 돌려준다. errors가 비어야 계약 통과."""
    errors = schema_errors(data)
    if errors:
        # 스키마가 깨졌으면 semantic 검사는 의미가 없다
        return errors, []
    return semantic_errors(data), semantic_warnings(data)
