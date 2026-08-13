"""`[1s] sceneplan` 산출(`08-sceneplan.json`) 검증. specs/01·02 + specs/schema/sceneplan.schema.json.

**스키마도 어휘도 값도 이 파일에 없다.** `specs/schema/`에서 로드한다 (ADR-0034 §3).

## 이 단계가 지는 것 (ADR-0029, ADR-0033 §3)

씬 분할, 글과 그림의 설명 분담, **연출 선택**이다. 문장은 쓰지 않는다. 후버댐 편에서
그림 필드가 27/27 비었던 것이 이 단계를 독립시킨 이유이므로, 검사도 그 실패를 겨눈다.

| 무엇 | 왜 |
|---|---|
| `says`가 문장인가 | 종결어미·수사 의문문·시그니처가 여기 들어오면 `[1w]`가 할 일이 없어진다. **경계를 기계로 지킨다** |
| 씬 수·예산 합계 | `[1a]`가 한 분량 산수를 씬으로 쪼갤 때 깨지는 자리다 |
| 단별 예산 | 씬 합이 그 단의 예산과 맞는가 (`act_budget_errors`) |
| 연출 공백 | `framing`·`transition`이 빈 씬 수를 **센다.** 막지 않는다 — 그 수가 ADR-0033의 되돌릴 조건을 관측하는 자리다 |

**구조 검증은 없다** (ADR-0033 §4). 비트 순서·필수 비트·`failed_solution` 2회는 폐기됐고,
서사가 성립하는지는 `[2b] judge`가 본다.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from jsonschema import Draft202012Validator

from . import vocab
from .outline import act_budgets
from .script_rules import SIGNATURES

SCENEPLAN_SCHEMA: dict[str, Any] = vocab.SCENEPLAN_SCHEMA_DOC
PLANNED_SCENE_SCHEMA: dict[str, Any] = SCENEPLAN_SCHEMA["$defs"]["planned_scene"]

_LIMITS = vocab.limits()
LINE_COUNT = tuple(_LIMITS["line_count"])
TOTAL_CHARS = tuple(_LIMITS["total_chars"])
LINE_CHARS_MAX = _LIMITS["line_chars_max"]
LINE_CHARS_TARGET = tuple(_LIMITS["line_chars_target"])

#: 씬 합과 단 예산의 허용 오차. 줄 하나를 통째로 옮긴 것은 걸리도록 잡혀 있다.
ACT_BUDGET_TOLERANCE = vocab.checks()["act_budget_tolerance"]

_VALIDATOR = Draft202012Validator(SCENEPLAN_SCHEMA, registry=vocab.REGISTRY)


def carried_fields() -> tuple[str, ...]:
    """`[1w]`가 `06-script.json`으로 **그대로 복사**하는 필드.

    두 스키마의 교집합이다. 목록을 손으로 적으면 한쪽에 필드가 늘 때 대조에서 조용히
    빠진다 — `subject_anchor`가 실제로 그렇게 새던 자리다 (ADR-0028·0029).
    """
    plan = set(PLANNED_SCENE_SCHEMA["properties"])
    scene = set(vocab.SCENE_SCHEMA_DOC["$defs"]["scene"]["properties"])
    return tuple(sorted(plan & scene))


def plan_only_fields() -> tuple[str, ...]:
    """계획에만 있고 대본에는 없는 필드 (`act`·`says`·`char_budget`)."""
    plan = set(PLANNED_SCENE_SCHEMA["properties"])
    scene = set(vocab.SCENE_SCHEMA_DOC["$defs"]["scene"]["properties"])
    return tuple(sorted(plan - scene))


def schema_errors(data: Any) -> list[str]:
    """JSON Schema 위반 목록."""
    errors = []
    for err in sorted(_VALIDATOR.iter_errors(data), key=lambda e: list(e.absolute_path)):
        location = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{location}: {err.message}")
    return errors


#: 존댓말 종결어미. `says`는 요지라 여기서 끝나면 안 된다 — 그건 이미 문장이다.
#:
#: `니다` 하나로 습니다·입니다·합니다·아닙니다를 다 받는다. 종성 ㅂ은 앞 음절에
#: 합쳐져 있어서(`아닙`) `ㅂ니다`로는 영영 안 걸린다 — 자모를 문자열로 적는 함정이다.
#: 맨 '요'는 뺐다: '수요'·'필요'처럼 요로 끝나는 명사가 요지에 흔하다.
_POLITE_ENDINGS = (
    "니다", "죠",
    "네요", "세요", "어요", "아요", "여요", "예요", "에요", "해요", "데요",
    "군요", "거든요", "잖아요",
)
_TRAILING = " \t.!?…"


def says_intrusions(says: str) -> list[str]:
    """`says`가 `[1w]`의 몫을 침범했는가 (스펙 05 §[1s]).

    셋만 본다 — **구어체 존댓말, 수사 의문문, 시그니처 문구.** 스펙이 `[1w]`에 준
    것이 정확히 그 셋이다. 평서 '한다'체 요지는 침범이 아니라서 걸리지 않는다.
    """
    found: list[str] = []
    core = (says or "").rstrip(_TRAILING)
    for ending in _POLITE_ENDINGS:
        if core.endswith(ending) and len(core) > len(ending):
            found.append(f"존댓말 종결어미 '{ending}'")
            break
    if "?" in (says or ""):
        found.append("수사 의문문")
    for item in SIGNATURES:
        phrase = str(item.get("phrase", "")).rstrip(_TRAILING)
        if phrase and phrase in (says or ""):
            found.append(f"시그니처 문구 '{phrase}'")
    return found


def semantic_errors(data: dict[str, Any]) -> list[str]:
    """스키마로 표현 불가한 교차 규칙."""
    errors: list[str] = []
    scenes: list[dict[str, Any]] = data.get("scenes", [])

    expected = list(range(1, len(scenes) + 1))
    actual = [s.get("scene_id") for s in scenes]
    if actual != expected:
        errors.append(
            f"scenes: scene_id가 1부터 연번이 아니다 (기대 {expected[:3]}…, 실제 {actual[:3]}…)"
        )

    if not LINE_COUNT[0] <= len(scenes) <= LINE_COUNT[1]:
        errors.append(
            f"scenes: 씬 {len(scenes)}개 (범위 {LINE_COUNT[0]}~{LINE_COUNT[1]}개). "
            "씬 1개 = 자막 줄 1개다 (ADR-0013)"
        )

    total = sum(int(s.get("char_budget", 0)) for s in scenes)
    if not TOTAL_CHARS[0] <= total <= TOTAL_CHARS[1]:
        errors.append(
            f"scenes: char_budget 합계가 {total}자다 (범위 {TOTAL_CHARS[0]}~{TOTAL_CHARS[1]}자)"
        )

    for scene in scenes:
        sid = scene.get("scene_id", "?")
        budget = int(scene.get("char_budget", 0))
        if budget > LINE_CHARS_MAX:
            errors.append(
                f"scenes/{sid}: char_budget {budget}자 (줄당 최대 {LINE_CHARS_MAX}자). "
                "한 줄에 안 들어가면 씬을 나눠야 한다"
            )
        intrusions = says_intrusions(str(scene.get("says", "")))
        if intrusions:
            errors.append(
                f"scenes/{sid}: says에 {', '.join(intrusions)} — says는 요지지 문장이 아니다. "
                "이걸 여기서 쓰면 [1w]가 할 일이 없어진다 (ADR-0029)"
            )

    return errors


def semantic_warnings(data: dict[str, Any]) -> list[str]:
    """막지 않고 알리는 것. 연출 공백은 **관측이지 판정이 아니다.**"""
    warnings: list[str] = []
    scenes: list[dict[str, Any]] = data.get("scenes", [])
    if not scenes:
        return warnings

    off_target = [
        s.get("scene_id")
        for s in scenes
        if not LINE_CHARS_TARGET[0] <= int(s.get("char_budget", 0)) <= LINE_CHARS_TARGET[1]
    ]
    if off_target:
        warnings.append(
            f"char_budget이 줄당 목표({LINE_CHARS_TARGET[0]}~{LINE_CHARS_TARGET[1]}자) 밖인 씬: "
            f"{', '.join(str(i) for i in off_target)}"
        )

    for field in ("framing", "transition"):
        blank = [s.get("scene_id") for s in scenes if not s.get(field)]
        if blank:
            warnings.append(
                f"{field}이 빈 씬 {len(blank)}/{len(scenes)}개 — [5]가 beat-defaults.json의 "
                "기본값으로 떨어뜨린다. 전 씬이 비면 연출을 고르지 않은 것이고 "
                "ADR-0033의 되돌릴 조건이다"
            )

    missing_anchor = [s.get("scene_id") for s in scenes if not s.get("subject_anchor")]
    if len(missing_anchor) == len(scenes):
        warnings.append(
            "subject_anchor가 전 씬에서 비었다 — 부재 자체는 정상이지만(ADR-0028) "
            "전멸은 후버댐 편의 실패 모양 그대로다 (ADR-0029 근거)"
        )
    return warnings


def act_budget_errors(data: dict[str, Any], outline: dict[str, Any]) -> list[str]:
    """씬 합이 `[1a]`의 단 예산과 맞는가 (ADR-0029).

    맞추지 못하는 것이 반복되면 예산을 act 단위로만 주는 것이 되돌릴 조건이다 —
    그때 지울 곳이 이 함수 하나다.
    """
    errors: list[str] = []
    budgets = act_budgets(outline)
    planned: Counter[int] = Counter()
    for scene in data.get("scenes", []):
        act = scene.get("act")
        if act not in budgets:
            errors.append(
                f"scenes/{scene.get('scene_id', '?')}: act {act}가 07-outline.json에 없다 "
                f"(있는 단: {sorted(budgets)})"
            )
            continue
        planned[act] += int(scene.get("char_budget", 0))

    for act, budget in sorted(budgets.items()):
        got = planned.get(act, 0)
        if not got:
            errors.append(f"acts/{act}: 씬이 하나도 배정되지 않았다 (예산 {budget}자)")
        elif abs(got - budget) > ACT_BUDGET_TOLERANCE:
            errors.append(
                f"acts/{act}: 씬 합 {got}자 vs 단 예산 {budget}자 "
                f"(허용 ±{ACT_BUDGET_TOLERANCE}자)"
            )
    return errors


def carry_errors(data: dict[str, Any], scenes: dict[str, Any]) -> list[str]:
    """`[1w]`가 계획을 그대로 옮겼는가 (ADR-0029).

    `[1x]`·`[1y]` 백필이 쓴 패턴이다 — 하나라도 다르면 파일을 쓰지 않고 실패한다.
    대조 대상은 `carried_fields()`가 계산하므로 스키마에 필드가 늘면 저절로 따라온다.
    """
    errors: list[str] = []
    fields = carried_fields()
    planned = {s.get("scene_id"): s for s in data.get("scenes", [])}
    written = {s.get("scene_id"): s for s in scenes.get("scenes", [])}

    if set(planned) != set(written):
        return [
            f"scenes: 씬 구성이 계획과 다르다 (계획 {len(planned)}개, 대본 {len(written)}개). "
            "[1w]는 씬을 만들거나 합칠 수 없다"
        ]

    for sid in sorted(planned):
        for field in fields:
            want = planned[sid].get(field)
            got = written[sid].get(field)
            if want != got:
                errors.append(
                    f"scenes/{sid}/{field}: 계획은 {want!r}인데 대본은 {got!r}다 — "
                    "[1w]는 text만 쓴다"
                )
    return errors


def direction_summary(data: dict[str, Any]) -> dict[str, Any]:
    """연출 선택의 분포. **판정이 아니라 관측이다** (ADR-0033 되돌릴 조건의 관측 수단).

    한 값에 쏠렸는지를 사람이 보라고 세어만 둔다 — 어디부터 편중인지는 실측이 쌓이기
    전에는 정할 수 없고, 임계값을 지금 지어내면 ADR-0001이 폐기한 그 표를 이름만 바꿔
    되살리는 것이다.
    """
    scenes: list[dict[str, Any]] = data.get("scenes", [])
    summary: dict[str, Any] = {"scene_count": len(scenes)}
    for field in ("beat", "framing", "transition", "camera", "motion", "subject_scale"):
        counts = Counter(s.get(field) or "(없음)" for s in scenes)
        summary[field] = dict(counts.most_common())
    summary["emphasis"] = sum(1 for s in scenes if s.get("emphasis"))
    return summary


def validate_sceneplan(
    data: Any, outline: dict[str, Any] | None = None
) -> tuple[list[str], list[str]]:
    """(errors, warnings)를 돌려준다. `outline`을 주면 단 예산까지 대조한다.

    없어도 돈다 — 선택적 입력의 부재는 경고가 아니다 (ADR-0034 D-3).
    """
    errors = schema_errors(data)
    if errors:
        return errors, []
    errors = semantic_errors(data)
    if outline:
        errors += act_budget_errors(data, outline)
    return errors, semantic_warnings(data)
