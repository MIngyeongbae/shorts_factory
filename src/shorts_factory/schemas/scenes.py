"""씬 계약(`06-script.json`) 검증. specs/02-beat-schema.md + specs/schema/scene.schema.json.

**스키마도 어휘도 이 파일에 없다.** `specs/schema/`에서 로드한다 (ADR-0034 §3).
여기 있는 것은 스키마로 표현할 수 없는 교차 규칙뿐이다.

씬 1개 = 자막 줄(SRT 큐) 1개다 (ADR-0013). 문장이 아니라서 한 씬의 `text`에 문장이
1~3개 들어갈 수 있다.

스펙 01의 대본 규칙(글자 수, 자막 줄 수, 수미상관)과 ADR-0007 그라운딩은 이 모듈이
손대지 않는다 — 별도 검증기가 맡는다.

`est_start`/`est_end`는 TTS 이전 추정치다. TTS 후 `start`/`end`로 바뀐 형태는
`timed_scenes.py`가 이 스키마에서 파생시킨다.
"""

from __future__ import annotations

import copy
from typing import Any

from jsonschema import Draft202012Validator

from . import vocab

#: specs/schema/vocab.json — 손으로 옮겨 적지 않는다 (ADR-0034 §3).
BEATS: tuple[str, ...] = vocab.values("beat")
CAMERAS: tuple[str, ...] = vocab.values("camera")
MOTIONS: tuple[str, ...] = vocab.values("motion")
SUBJECT_SCALES: tuple[str, ...] = vocab.values("subject_scale")
FRAMING_TOKENS: tuple[str, ...] = vocab.values("framing")
TRANSITIONS: tuple[str, ...] = vocab.values("transition")

#: 숫자를 세우는 비트. 이름에서 뽑는다 — 목록을 또 적으면 어휘가 늘 때 갈라진다.
NUMBER_BEATS: tuple[str, ...] = tuple(b for b in BEATS if b.endswith("_number"))

#: 편당 영상 씬 상한 (ADR-0006). 어느 모션이 영상인지도 어휘가 안다.
VIDEO_MOTIONS: tuple[str, ...] = tuple(
    name for name, item in vocab.meta("motion").items() if item.get("max_scenes")
)
MAX_VIDEO_SCENES: int = min(
    (item["max_scenes"] for item in vocab.meta("motion").values() if item.get("max_scenes")),
    default=0,
)

#: `visual_goal`이 `text`를 되풀이했다고 볼 겹침 비율 (ADR-0022).
VISUAL_GOAL_OVERLAP_LIMIT: float = vocab.checks()["visual_goal_overlap_limit"]

#: total_duration과 마지막 씬 est_end의 허용 오차(초). 경고 판정에만 쓴다.
DURATION_TOLERANCE = 0.5

SCENES_SCHEMA: dict[str, Any] = vocab.SCENE_SCHEMA_DOC
SCENE_SCHEMA: dict[str, Any] = SCENES_SCHEMA["$defs"]["scene"]

_VALIDATOR = Draft202012Validator(SCENES_SCHEMA, registry=vocab.REGISTRY)


def schema_errors(data: Any) -> list[str]:
    """JSON Schema 위반 목록."""
    errors = []
    for err in sorted(_VALIDATOR.iter_errors(data), key=lambda e: list(e.absolute_path)):
        location = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{location}: {err.message}")
    return errors


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
    """스키마로 표현 불가한 교차 규칙 (스펙 02).

    **구조 검증은 없다** (ADR-0033 §4). 비트 순서·개수 제약은 폐기됐고, 서사가
    성립하는지는 `[2b] judge`가 본다.
    """
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
        elif prev_end is not None and start < prev_end:
            errors.append(
                f"scenes/{sid}: est_start({start})가 앞 씬의 est_end({prev_end})보다 이르다"
            )
        prev_end = end

    # 규칙: 영상 모션은 편당 상한이 있다 (specs/02, ADR-0006). fast 엔드포인트라
    # GPU 시간을 쓰는 mj_video도 같은 상한을 쓴다 (ADR-0025 §3).
    video = [s.get("scene_id") for s in scenes if s.get("motion") in VIDEO_MOTIONS]
    if MAX_VIDEO_SCENES and len(video) > MAX_VIDEO_SCENES:
        errors.append(
            f"scenes: 영상 모션({'/'.join(VIDEO_MOTIONS)}) 씬이 {len(video)}개다 "
            f"(편당 최대 {MAX_VIDEO_SCENES}개)"
        )

    return errors


def semantic_warnings(data: dict[str, Any]) -> list[str]:
    """차단하지는 않지만 하류 단계가 알아야 할 사항."""
    warnings: list[str] = []
    scenes: list[dict[str, Any]] = data.get("scenes", [])
    if not scenes:
        return warnings

    # ADR-0033 — 오버레이를 고르는 것은 이제 비트 표가 아니라 [1s]다. 숫자 비트에
    # 강조가 없는 것이 틀린 것은 아니지만(그 숫자를 화면에 안 세울 수 있다) 대개는
    # 빠뜨린 것이라 알린다. 막지는 않는다.
    missing = [
        s.get("scene_id")
        for s in scenes
        if s.get("beat") in NUMBER_BEATS and not s.get("emphasis")
    ]
    if missing:
        warnings.append(
            f"숫자 비트인데 emphasis가 없는 씬: {', '.join(str(i) for i in missing)} — "
            "그 숫자를 화면에 세우지 않을 생각이면 그대로 두어도 된다"
        )

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


def scene_schema_copy() -> dict[str, Any]:
    """씬 스키마의 깊은 복사본. 파생 스키마(`timed_scenes`)가 손대도 원본이 안 바뀐다."""
    return copy.deepcopy(SCENE_SCHEMA)
