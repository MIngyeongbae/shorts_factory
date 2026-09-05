"""scenes.timed.{lang}.json — 실측 타임스탬프가 붙은 씬 계약. specs/02·05, ADR-0017·0056.

specs/02:
    `[3. tts+sync]`가 실측값을 `runs/{run_id}/scenes.timed.{lang}.json`에 **새로 쓰며**,
    그 파일에서 필드명은 `start`/`end`다. 추정(`est_*`)과 실측(`start`/`end`)은
    파일 단위로 분리된다.

**언어당 파일 하나다** (ADR-0056 결정 5) — `ko` 필수, `ja`·`en`은 대본이 있으면.
세 파일의 씬 수는 같다(`[2l]`의 줄 1:1 정렬). 스키마는 언어와 무관하게 하나이고
경로만 `timed_scenes_path`가 만든다.

그래서 이 스키마는 `scenes.py`의 씬 스키마를 손으로 옮겨 적지 않고 **필드명만 바꿔
파생**시킨다. 스펙이 "같은 스키마, 이름만 다름"이라고 말하므로 코드도 그래야 한다 —
씬 스키마가 늘거나 줄면 이쪽이 자동으로 따라간다.

씬 계약은 읽기 전용이다 (ADR-0017). 이 모듈은 원본 문서를 손대지 않고
새 문서를 만들어 돌려준다.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Sequence

from jsonschema import Draft202012Validator

from . import vocab
from .scenes import DURATION_TOLERANCE, SCENES_SCHEMA, scene_schema_copy

#: 언어별 실측 파일 이름 (specs/05 계약 표). `{lang}` ∈ ko·ja·en.
TIMED_SCENES_PATTERN = "scenes.timed.{lang}.json"

#: 토픽 하나의 언어 (specs/05 — ADR-0056 결정 5). **ko는 필수이고 ja·en은 선택이다.**
#: `[3]`·`[7]`·`[9]`가 같은 목록을 본다 — 세 단계가 각자 적으면 갈라진다.
LANGUAGES: tuple[str, ...] = ("ko", "ja", "en")
PRIMARY_LANGUAGE = "ko"


def timed_scenes_path(run_dir: Path, lang: str) -> Path:
    """`runs/{run_id}/scenes.timed.{lang}.json`. 시각을 읽는 곳은 언어당 이 파일 하나다 (ADR-0020)."""
    lang = lang.strip().lower()
    if not lang.isalpha():
        raise ValueError(f"언어 코드가 아니다: {lang!r}")
    return Path(run_dir) / TIMED_SCENES_PATTERN.format(lang=lang)


def present_languages(run_dir: Path) -> list[str]:
    """실측 파일이 있는 언어, `LANGUAGES` 순서. 없는 언어는 그 언어의 쇼츠가 없을 뿐이다 (D-3)."""
    return [lang for lang in LANGUAGES if timed_scenes_path(run_dir, lang).exists()]


#: 추정 → 실측 필드명 대응 (specs/02)
RENAMED = {"est_start": "start", "est_end": "end"}

#: 그 언어 영상의 제목 훅 (ADR-0065). **선택 필드다** — 없으면 제목 없이 돈다 (D-3).
#: `[3]`이 쓰는 줄 경계 스키마와 `[7]`·`[9]`가 읽는 병합본 스키마 **둘 다** 이 값을 쓴다.
#: 한 곳에만 적는다 — 갈리면 `[3]`이 쓴 문서를 `[9]`가 못 읽는다 (ADR-0034 §3).
TITLE_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "선택. 그 언어 script.{lang}.md의 `# 제목`. [9]가 첫 씬 구간 동안 화면 "
        "상단에 굽는다 (ADR-0065)."
    ),
}

#: **폐기된 필드 (ADR-0096).** ADR-0090의 엔딩 여운 대사였다. `[3]`은 더 쓰지 않고 읽는
#: 곳도 없다 — 스키마에 남는 이유는 이미 만든 편의 실측 파일에 이 값이 있고
#: `additionalProperties: false`라 지우면 보관함 편의 `[9]` 재실행이 깨지기 때문이다
#: (ADR-0088 되돌릴 조건 1). 그 파일들이 다 폐기되면 같이 지운다.
OUTRO_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "폐기 (ADR-0096). 옛 편의 파일 호환으로만 허용한다 — 읽는 곳이 없다."
    ),
}

#: 이 파일에 싣지 않는 대본 필드. **이미지 지시는 여기 올 이유가 없다** (ADR-0020·0022).
#: 실측 파일을 읽는 곳은 `[7]`(클립 길이)과 `[9]`(전환·자막)뿐이고 둘 다 그림이 무엇을
#: 설명하는지도, 어떤 구도로 잡는지도 알 필요가 없다. 그림 쪽 소비자는 `prompts.json`을
#: 읽는다 — 같은 값이 두 파일에 있으면 갈라진다.
#:
#: `transition`은 빠지지 않는다. `[9]`가 그 값으로 전환을 놓는다 (ADR-0033 §3).
DROPPED: tuple[str, ...] = ("visual_goal", "framing")


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


TIMED_SCENE_SCHEMA: dict[str, Any] = _rename(scene_schema_copy())

TIMED_SCENES_SCHEMA: dict[str, Any] = copy.deepcopy(SCENES_SCHEMA)
# 파생 스키마는 자기 $id를 갖는다. 원본과 같은 $id를 달면 `#/...` 참조가 레지스트리에
# 등록된 원본으로 풀려 이름만 바꾼 이 스키마를 비껴간다.
TIMED_SCENES_SCHEMA["$id"] = "scenes.timed.schema.json"
TIMED_SCENES_SCHEMA["title"] = "scenes.timed.{lang}.json (실측 씬 계약)"
TIMED_SCENES_SCHEMA.pop("$defs", None)
TIMED_SCENES_SCHEMA["properties"]["scenes"]["items"] = TIMED_SCENE_SCHEMA
#: `title`은 언어별 제목 훅이라 **파생 쌀마에만** 넣는다 (ADR-0065). 원본 `scenes.json`은
#: 언어 중립이라 그쪽에 제목이 있을 자리가 없다. `merge_scene_direction`은 `dict(timed)`로
#: 시작하므로 실측 문서의 `title`이 병합본에 그대로 살아남는다.
TIMED_SCENES_SCHEMA["properties"]["title"] = copy.deepcopy(TITLE_PROPERTY)
TIMED_SCENES_SCHEMA["properties"]["outro"] = copy.deepcopy(OUTRO_PROPERTY)

_VALIDATOR = Draft202012Validator(TIMED_SCENES_SCHEMA, registry=vocab.REGISTRY)


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


#: 새 편(script.md) 모드 — `[3]`이 **줄 경계만** 담는다 (specs/05 계약 표, ADR-0049).
#: 대본 속성(beat·subject·camera·…)은 `[3s]`의 `scenes.json` 소관이라 여기 없고,
#: 소비자(`[7]`·`[9]`)는 `stages.contract.merge_scene_direction`으로 합쳐 읽는다.
LINE_TIMED_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "scenes.timed.line.schema.json",
    "title": "scenes.timed.{lang}.json (줄 경계 실측)",
    "type": "object",
    "required": ["run_id", "topic", "total_duration", "scenes"],
    "additionalProperties": False,
    "properties": {
        "run_id": {"type": "string", "minLength": 1},
        "topic": {"type": "string", "minLength": 1},
        "total_duration": {"type": "number", "exclusiveMinimum": 0},
        "title": copy.deepcopy(TITLE_PROPERTY),
        "outro": copy.deepcopy(OUTRO_PROPERTY),
        "scenes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["scene_id", "text", "start", "end"],
                "additionalProperties": False,
                "properties": {
                    "scene_id": {"type": "integer", "minimum": 1},
                    "text": {"type": "string", "minLength": 1},
                    "start": {"type": "number", "minimum": 0},
                    "end": {"type": "number", "minimum": 0},
                },
            },
        },
    },
}

_LINE_VALIDATOR = Draft202012Validator(LINE_TIMED_SCHEMA)


def build_line_timed_scenes(
    run_id: str,
    topic: str,
    lines: Sequence[str],
    boundaries: Sequence[tuple[float, float]],
    *,
    title: str = "",
) -> dict[str, Any]:
    """대본 줄 + 실측 경계 → 그 언어의 scenes.timed.{lang}.json 문서 (한 줄 = 한 씬, ADR-0013).

    `title`은 그 언어 대본의 `# 제목`이고 **선택이다** — 빈 문자열이면 필드 자체를 쓰지
    않는다. 스키마가 `minLength: 1`이라 빈 값을 실으면 계약 위반이고, 없는 것은 위반이
    아니다 (ADR-0065, D-3).

    `outro`는 쓰지 않는다 — ADR-0096이 폐기했고 스키마가 옛 파일을 위해 허용만 한다.
    """
    if len(lines) != len(boundaries):
        raise ValueError(f"줄 {len(lines)}개에 경계 {len(boundaries)}개가 왔다")
    scenes = [
        {"scene_id": index, "text": text, "start": start, "end": end}
        for index, (text, (start, end)) in enumerate(zip(lines, boundaries), start=1)
    ]
    document: dict[str, Any] = {
        "run_id": run_id,
        "topic": topic,
        "total_duration": scenes[-1]["end"] if scenes else 0.0,
        "scenes": scenes,
    }
    if title.strip():
        document["title"] = title.strip()
    return document


def validate_line_timed_scenes(data: Any) -> tuple[list[str], list[str]]:
    """(errors, warnings) — 줄 경계 모드. 교차 규칙(연번·경계 순서·빈틈)은 공용이다."""
    errors = []
    for err in sorted(
        _LINE_VALIDATOR.iter_errors(data), key=lambda e: list(e.absolute_path)
    ):
        location = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{location}: {err.message}")
    if errors:
        return errors, []
    return semantic_errors(data), semantic_warnings(data)


def build_timed_scenes(
    source: dict[str, Any], boundaries: Sequence[tuple[float, float]]
) -> dict[str, Any]:
    """씬 계약(`scenes.json` 모양) + 실측 경계 → `[7]`·`[9]`가 읽는 병합본 문서.

    실운영에서는 `stages.contract.merge_scene_direction`이 실측 파일과 계약 파일에서
    같은 병합을 한다 — 이 빌더는 병합본 **모양의 정의**이고 테스트 픽스처가 쓴다.
    `visual_goal`은 뺀다 — 이미지 지시라 이 파일의 소비자(`[7]`·`[9]`)가 읽을 일이
    없다 (`DROPPED`).
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
