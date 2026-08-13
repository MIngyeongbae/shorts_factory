"""`[1a] outline` 산출(`07-outline.json`) 검증. specs/01-script-template.md + specs/schema/outline.schema.json.

**스키마도 값도 이 파일에 없다.** `specs/schema/`에서 로드한다 (ADR-0034 §3).
여기 있는 것은 스키마로 표현할 수 없는 교차 규칙뿐이다.

## 이 단계가 지는 것 (ADR-0029)

무엇이 흥미로운가(각도)와 그것을 어떤 순서로 말하는가(단 구성)다. **씬도 문장도 만들지
않는다.** 단 구성은 소재마다 다르므로 개수·이름·순서를 검사하지 않는다 (ADR-0033 §2) —
기계가 볼 수 있는 것은 **결과**뿐이다.

| 무엇 | 왜 여기서 보나 |
|---|---|
| 글자 예산 합계 | 분량 산수를 파이프라인에서 **한 번만** 하는 자리다. 여기서 틀리면 `[1w]`가 아무리 잘 써도 분량이 안 맞는다 |
| `chosen_hook` 범위 | 실재하지 않는 후보를 가리키면 `[1s]` 프롬프트에 실을 각도가 없다 |
| 훅이 앞·수미상관 | 구조에 거는 유일한 제약 둘이다 (스펙 01). 다만 **경고다** — 라벨이 아니라 근거 겹침으로 재는 근사값이라 막을 근거가 못 된다 |

그라운딩(ADR-0007)은 팩트시트를 함께 읽어야 볼 수 있어 `unknown_fact_ids()`로 뺐다.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from . import vocab
from .script_rules import noun_stems

OUTLINE_SCHEMA: dict[str, Any] = vocab.OUTLINE_SCHEMA_DOC

#: specs/schema/script-rules.json — 단 예산의 합이 맞아야 하는 범위.
TOTAL_CHARS = tuple(vocab.limits()["total_chars"])

_VALIDATOR = Draft202012Validator(OUTLINE_SCHEMA, registry=vocab.REGISTRY)


def schema_errors(data: Any) -> list[str]:
    """JSON Schema 위반 목록."""
    errors = []
    for err in sorted(_VALIDATOR.iter_errors(data), key=lambda e: list(e.absolute_path)):
        location = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{location}: {err.message}")
    return errors


def chosen(data: dict[str, Any]) -> dict[str, Any]:
    """고른 훅 후보. 인덱스가 범위 밖이면 빈 dict다 (검사는 `semantic_errors`)."""
    candidates = data.get("hook_candidates") or []
    index = data.get("chosen_hook")
    if isinstance(index, int) and 0 <= index < len(candidates):
        return candidates[index]
    return {}


def budget_total(data: dict[str, Any]) -> int:
    """단 예산의 합."""
    return sum(int(a.get("char_budget", 0)) for a in data.get("acts", []))


def act_budgets(data: dict[str, Any]) -> dict[int, int]:
    """`{act 번호: 글자 예산}`. `[1s]`의 씬별 합과 대조하는 쪽이다."""
    return {int(a["act"]): int(a["char_budget"]) for a in data.get("acts", []) if "act" in a}


def unknown_fact_ids(data: dict[str, Any], factsheet: dict[str, Any]) -> list[str]:
    """팩트시트에 없는 `grounded_in` id (ADR-0007).

    **형식이 아니라 실재를 본다.** id 패턴을 이 파일에 적으면 팩트시트 스키마와 갈라진다 —
    실물 팩트시트가 유일한 기준이다.
    """
    known = {f.get("id") for f in factsheet.get("facts", [])}
    used: list[str] = []
    for hook in data.get("hook_candidates", []):
        used += list(hook.get("grounded_in") or [])
    for act in data.get("acts", []):
        used += list(act.get("grounded_in") or [])
    return sorted({fid for fid in used if fid not in known})


def semantic_errors(data: dict[str, Any]) -> list[str]:
    """스키마로 표현 불가한 교차 규칙.

    **구조 검증은 없다** (ADR-0033 §2). 단 개수·이름·순서는 소재가 정하므로 여기서
    볼 것이 아니고, 서사가 성립하는지는 `[2b] judge`가 본다.
    """
    errors: list[str] = []

    candidates = data.get("hook_candidates") or []
    index = data.get("chosen_hook")
    if not isinstance(index, int) or not 0 <= index < len(candidates):
        errors.append(
            f"chosen_hook: {index}번 후보가 없다 (후보 {len(candidates)}개). "
            "[1s]에 실을 각도가 정해지지 않았다"
        )

    acts = data.get("acts") or []
    expected = list(range(1, len(acts) + 1))
    actual = [a.get("act") for a in acts]
    if actual != expected:
        errors.append(f"acts: act가 1부터 연번이 아니다 (기대 {expected}, 실제 {actual})")

    total = budget_total(data)
    if not TOTAL_CHARS[0] <= total <= TOTAL_CHARS[1]:
        errors.append(
            f"acts: 글자 예산 합계가 {total}자다 (범위 {TOTAL_CHARS[0]}~{TOTAL_CHARS[1]}자). "
            "분량 산수는 이 단계에서 한 번만 한다 — 여기서 틀리면 [1w]가 맞출 수 없다"
        )

    return errors


def _act_terms(act: dict[str, Any]) -> set[str]:
    """단 하나가 다루는 것 — 근거 id + `must_convey`의 명사."""
    terms = set(act.get("grounded_in") or [])
    for item in act.get("must_convey") or []:
        terms |= noun_stems(str(item))
    return terms


def semantic_warnings(data: dict[str, Any]) -> list[str]:
    """구조에 거는 제약 둘 — 훅이 앞, 수미상관 (스펙 01).

    **막지 않는다.** 근거 id와 명사 겹침으로 재는 근사값이고, 실제 판정은 텍스트가 나온
    뒤 `[2] validate`(수미상관)와 `[1b] score`(훅)가 한다. 여기서 알리는 것은 **설계
    단계에서 이미 어긋난 것**을 문장 쓰기 전에 잡기 위해서다.
    """
    warnings: list[str] = []
    acts = data.get("acts") or []
    hook = chosen(data)
    if not acts or not hook:
        return warnings

    hook_terms = set(hook.get("grounded_in") or []) | noun_stems(str(hook.get("claim", "")))

    if not hook_terms & _act_terms(acts[0]):
        warnings.append(
            "acts/1: 고른 훅의 근거도 명사도 첫 단에 없다 — 훅이 앞에 있어야 한다 (스펙 01)"
        )
    if len(acts) > 1 and not hook_terms & _act_terms(acts[-1]):
        warnings.append(
            f"acts/{len(acts)}: 마지막 단이 훅의 소재로 돌아오지 않는다 (수미상관). "
            "텍스트가 나오면 [2] validate가 같은 것을 오류로 잡는다"
        )
    return warnings


def validate_outline(data: Any) -> tuple[list[str], list[str]]:
    """(errors, warnings)를 돌려준다. errors가 비어야 계약 통과."""
    errors = schema_errors(data)
    if errors:
        return errors, []
    return semantic_errors(data), semantic_warnings(data)
