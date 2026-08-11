"""scenes.timed.json — 실측 타임스탬프가 붙은 씬 계약. specs/02, ADR-0017.

specs/02:
    `[3. tts+sync]`가 실측값을 `runs/{run_id}/scenes.timed.json`에 **새로 쓰며**,
    그 파일에서 필드명은 `start`/`end`다. 추정(`est_*`)과 실측(`start`/`end`)은
    파일 단위로 분리된다.

그래서 이 스키마는 `scenes.py`의 씬 스키마를 손으로 옮겨 적지 않고 **필드명만 바꿔
파생**시킨다. 스펙이 "같은 스키마, 이름만 다름"이라고 말하므로 코드도 그래야 한다 —
씬 스키마가 늘거나 줄면 이쪽이 자동으로 따라간다.

`06-script.json`은 읽기 전용이다 (ADR-0017). 이 모듈은 원본 문서를 손대지 않고
새 문서를 만들어 돌려준다.
"""

from __future__ import annotations

import copy
from typing import Any, Sequence

from jsonschema import Draft202012Validator

from .scenes import DURATION_TOLERANCE, SCENE_SCHEMA, SCENES_SCHEMA

#: 추정 → 실측 필드명 대응 (specs/02)
RENAMED = {"est_start": "start", "est_end": "end"}

#: 이 파일에 싣지 않는 대본 필드. **`visual_goal`은 이미지 지시라 여기 올 이유가 없다**
#: (ADR-0020·0022). `scenes.timed.json`을 읽는 곳은 `[7]`(클립 길이·카메라·모션)과
#: `[9]`(전환·자막)뿐이고 둘 다 그림이 무엇을 설명하는지 알 필요가 없다. 그림 쪽
#: 소비자는 `prompts.json`을 읽는다 — 같은 값이 두 파일에 있으면 갈라진다.
DROPPED: tuple[str, ...] = ("visual_goal",)


def _rename(schema: dict[str, Any]) -> dict[str, Any]:
    timed = copy.deepcopy(schema)
    properties = timed["properties"]
    for name in DROPPED:
        properties.pop(name, None)
    for old, new in RENAMED.items():
        properties[new] = properties.pop(old)
    timed["required"] = [
        RENAMED.get(name, name) for name in timed["required"] if name not in DROPPED
    ]
    return timed


TIMED_SCENE_SCHEMA: dict[str, Any] = _rename(SCENE_SCHEMA)

TIMED_SCENES_SCHEMA: dict[str, Any] = copy.deepcopy(SCENES_SCHEMA)
TIMED_SCENES_SCHEMA["title"] = "scenes.timed.json (실측 씬 계약)"
TIMED_SCENES_SCHEMA["properties"]["scenes"]["items"] = TIMED_SCENE_SCHEMA

_VALIDATOR = Draft202012Validator(TIMED_SCENES_SCHEMA)


def schema_errors(data: Any) -> list[str]:
    errors = []
    for err in sorted(_VALIDATOR.iter_errors(data), key=lambda e: list(e.absolute_path)):
        location = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{location}: {err.message}")
    return errors


def semantic_errors(data: dict[str, Any]) -> list[str]:
    """스키마로 표현되지 않는 교차 규칙."""
    errors: list[str] = []
    scenes: list[dict[str, Any]] = data.get("scenes", [])

    expected = list(range(1, len(scenes) + 1))
    actual = [s.get("scene_id") for s in scenes]
    if actual != expected:
        errors.append(
            f"scenes: scene_id가 1부터 연번이 아니다 (기대 {expected[:3]}…, 실제 {actual[:3]}…)"
        )

    prev_end: float | None = None
    for scene in scenes:
        sid = scene.get("scene_id", "?")
        start = scene.get("start")
        end = scene.get("end")
        if start >= end:
            errors.append(f"scenes/{sid}: start({start}) >= end({end})")
        elif prev_end is not None and start < prev_end:
            errors.append(
                f"scenes/{sid}: start({start})가 앞 씬의 end({prev_end})보다 이르다"
            )
        prev_end = end

    return errors


def semantic_warnings(data: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    scenes: list[dict[str, Any]] = data.get("scenes", [])
    if not scenes:
        return warnings

    # 씬은 빈틈 없이 이어진다 (tts/sync.py). 구멍이 있으면 [7]의 클립 길이와
    # [9]의 자막이 덮지 못하는 구간이 생긴다.
    prev_end = None
    for scene in scenes:
        start = float(scene.get("start", 0.0))
        if prev_end is not None and start > prev_end:
            warnings.append(
                f"scenes/{scene.get('scene_id')}: 앞 씬과 {start - prev_end:.3f}초 떨어져 있다"
            )
        prev_end = float(scene.get("end", 0.0))

    last_end = float(scenes[-1].get("end", 0.0))
    total = float(data.get("total_duration", 0.0))
    if abs(total - last_end) > DURATION_TOLERANCE:
        warnings.append(
            f"total_duration({total})과 마지막 씬 end({last_end})의 차이가 "
            f"{DURATION_TOLERANCE}초를 넘는다"
        )
    return warnings


def validate_timed_scenes(data: Any) -> tuple[list[str], list[str]]:
    """(errors, warnings). errors가 비어야 계약 통과."""
    errors = schema_errors(data)
    if errors:
        return errors, []
    return semantic_errors(data), semantic_warnings(data)


def build_timed_scenes(
    source: dict[str, Any], boundaries: Sequence[tuple[float, float]]
) -> dict[str, Any]:
    """`06-script.json` + 실측 경계 → scenes.timed.json 문서.

    씬의 나머지 필드(beat·text·emphasis·subject·camera·motion·notes)는 그대로 옮긴다.
    2부는 대본을 고치지 않는다 (ADR-0017). `visual_goal`은 뺀다 — 이미지 지시라
    이 파일의 소비자(`[7]`·`[9]`)가 읽을 일이 없다 (`DROPPED`).
    """
    scenes = source.get("scenes", [])
    if len(scenes) != len(boundaries):
        raise ValueError(
            f"씬 {len(scenes)}개에 경계 {len(boundaries)}개가 왔다"
        )

    timed: list[dict[str, Any]] = []
    for scene, (start, end) in zip(scenes, boundaries):
        item = {
            key: copy.deepcopy(value)
            for key, value in scene.items()
            if key not in RENAMED and key not in DROPPED
        }
        item["start"] = start
        item["end"] = end
        # 필드 순서를 원본과 맞춘다 (start/end는 est_*가 있던 자리에).
        order = [RENAMED.get(key, key) for key in scene if key not in DROPPED]
        timed.append({key: item[key] for key in order})

    return {
        "run_id": source["run_id"],
        "topic": source["topic"],
        "total_duration": timed[-1]["end"] if timed else 0.0,
        "scenes": timed,
    }
