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
        "subject": {"type": "string", "minLength": 1},
        # specs/02 + ADR-0018 — beat와 함께 구도를 결정한다. 연출이 아니라 피사체 서술이라
        # [1. script]가 subject와 함께 쓴다.
        "subject_scale": {"enum": list(SUBJECT_SCALES)},
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
