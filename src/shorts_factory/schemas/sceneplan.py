"""`[3s. scenetable]` 세션 산출(씬 연출표) 검증. specs/02·03 + specs/schema/sceneplan.schema.json.

**스키마도 어휘도 값도 이 파일에 없다.** `specs/schema/`에서 로드한다 (ADR-0034 §3).

## 이 표가 지는 것 (ADR-0049 §5, ADR-0033 §3)

TTS 실측 줄 경계 위에서 씬마다 그림 필드(`visual_goal`·`subject`·`subject_anchor`·
`subject_scale`)와 연출 필드(`framing`·`transition`·`camera`·`staging`), 계측 표시
(`info` — ASCII 라벨·대상·표시 방식, ADR-0056), 인물(`characters`/`cast`)을 정하는 것이다. **문장도 시각도 여기 없다** — 씬 경계는
`[3]`의 실측이 정했고, 세션은 씬을 만들거나 합칠 수 없다 (그 대조는 `stages/scenetable.py`
가 실측 파일을 들고 한다).

옛 씬 계획 계약(`act`·`says`·`char_budget`)이 이 자리에
있었다. 그 축은 ADR-0049가 대본 3단계와 함께 지웠다 — 문장이 이미 있으므로 요지도
글자 예산도 필요 없다. 옛 산출물은 재검증하지 않는다 (ADR-0036).

| 무엇 | 왜 |
|---|---|
| 스키마 (어휘 enum, 필드 타입) | 연출은 닫힌 어휘에서만 고른다 (ADR-0033 §3). 필드 정의는 `scene.schema.json`을 `$ref`한다 |
| `scene_id` 연번 | 실측 줄과의 일대일 대응이 이 번호에 걸려 있다 (ADR-0013) |
| 연출 공백 | `framing`·`transition`·`staging`이 빈 씬 수를 **센다.** 막지 않는다 — 그 수가 ADR-0033의 되돌릴 조건을 관측하는 자리다 |

병합본(`scenes.json`)의 교차 규칙 — `visual_goal` 겹침, 라벨 숫자 에코, `cast`↔
`characters` 대조 — 는 `text`가 있어야 재지므로 `scenes.validate_scenes`가 맡는다.

**구조 검증은 없다** (ADR-0033 §4). 비트 순서·필수 비트는 폐기됐고, 서사가 성립하는지는
사람이 대본을 읽고 봤다 (ADR-0049 게이트).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from jsonschema import Draft202012Validator

from . import vocab

SCENEPLAN_SCHEMA: dict[str, Any] = vocab.SCENEPLAN_SCHEMA_DOC
PLANNED_SCENE_SCHEMA: dict[str, Any] = SCENEPLAN_SCHEMA["$defs"]["planned_scene"]

_VALIDATOR = Draft202012Validator(SCENEPLAN_SCHEMA, registry=vocab.REGISTRY)


def carried_fields() -> tuple[str, ...]:
    """`[3s]` 오케스트레이터가 `scenes.json`으로 **그대로 복사**하는 씬 필드.

    두 스키마의 교집합이다. 목록을 손으로 적으면 한쪽에 필드가 늘 때 병합에서 조용히
    빠진다 — `subject_anchor`가 실제로 그렇게 새던 자리다 (ADR-0028·0029).
    """
    plan = set(PLANNED_SCENE_SCHEMA["properties"])
    scene = set(vocab.SCENE_SCHEMA_DOC["$defs"]["scene"]["properties"])
    return tuple(sorted(plan & scene))


def schema_errors(data: Any) -> list[str]:
    """JSON Schema 위반 목록."""
    errors = []
    for err in sorted(_VALIDATOR.iter_errors(data), key=lambda e: list(e.absolute_path)):
        location = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{location}: {err.message}")
    return errors


def semantic_errors(data: dict[str, Any]) -> list[str]:
    """스키마로 표현 불가한 교차 규칙.

    **연번·누락은 여기서 재지 않는다.** scene_id의 정답은 연출표 안이 아니라
    `scenes.timed.ko.json`의 실측 줄 목록이고, 그 대조(누락 = 오류, 날조 = 경고)는
    `stages/scenetable.py`의 병합이 한다. 여기서 잡는 것은 실측 대조로도 조용히
    넘어가는 **중복**뿐이다 — 같은 id가 두 번 오면 뒤엣것이 앞엣것을 덮는다.
    """
    errors: list[str] = []
    scenes: list[dict[str, Any]] = data.get("scenes", [])

    counts = Counter(s.get("scene_id") for s in scenes)
    for sid, count in sorted(counts.items(), key=lambda kv: str(kv[0])):
        if count > 1:
            errors.append(f"scenes: scene_id {sid}가 {count}번 나온다")

    return errors


def semantic_warnings(data: dict[str, Any]) -> list[str]:
    """막지 않고 알리는 것. 연출 공백은 **관측이지 판정이 아니다** (ADR-0033)."""
    warnings: list[str] = []
    scenes: list[dict[str, Any]] = data.get("scenes", [])
    if not scenes:
        return warnings

    # staging도 센다 (ADR-0056 결정 4 — specs/05 [3s]: 빈 framing·transition·staging 씬 수).
    for field in ("framing", "transition", "staging"):
        blank = [s.get("scene_id") for s in scenes if not s.get(field)]
        if blank:
            warnings.append(
                f"{field}이 빈 씬 {len(blank)}/{len(scenes)}개 — [5]/[9]가 "
                "beat-defaults.json의 기본값으로 떨어뜨린다. 전 씬이 비면 연출을 "
                "고르지 않은 것이고 ADR-0033의 되돌릴 조건이다"
            )

    missing_anchor = [s.get("scene_id") for s in scenes if not s.get("subject_anchor")]
    if len(missing_anchor) == len(scenes):
        warnings.append(
            "subject_anchor가 전 씬에서 비었다 — 부재 자체는 정상이지만(ADR-0028) "
            "전멸은 후버댐 편의 실패 모양 그대로다 (ADR-0029 근거)"
        )
    return warnings


def direction_summary(data: dict[str, Any]) -> dict[str, Any]:
    """연출 선택의 분포. **판정이 아니라 관측이다** (ADR-0033 되돌릴 조건의 관측 수단).

    한 값에 쏠렸는지를 사람이 보라고 세어만 둔다 — 어디부터 편중인지는 실측이 쌓이기
    전에는 정할 수 없고, 임계값을 지금 지어내면 ADR-0001이 폐기한 그 표를 이름만 바꿔
    되살리는 것이다.
    """
    scenes: list[dict[str, Any]] = data.get("scenes", [])
    summary: dict[str, Any] = {"scene_count": len(scenes)}
    for field in ("beat", "framing", "transition", "camera", "staging", "subject_scale"):
        counts = Counter(s.get(field) or "(없음)" for s in scenes)
        summary[field] = dict(counts.most_common())
    summary["info"] = sum(1 for s in scenes if s.get("info"))
    summary["cast"] = sum(1 for s in scenes if s.get("cast"))
    return summary


def validate_sceneplan(data: Any) -> tuple[list[str], list[str]]:
    """(errors, warnings)를 돌려준다. errors가 비어야 계약 통과."""
    errors = schema_errors(data)
    if errors:
        return errors, []
    return semantic_errors(data), semantic_warnings(data)
