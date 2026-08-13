"""팩트시트(research.json) 검증. specs/06-topic-research.md + specs/schema/factsheet.schema.json.

**스키마도 값도 이 파일에 없다.** `specs/schema/`에서 로드한다 (ADR-0034 §3).
여기 있는 것은 JSON Schema로 표현되지 않는 교차 규칙뿐이다 — 관측 지표와 근거 필드의
일치, 고유 숫자 개수, 출처 유무, id 중복.

**`conditions`는 판정이 아니라 관측이다** (ADR-0033 §1). 비어 있다고 `verdict`가
`fail`이 되지 않는다 — `fail`은 매체 적합성이나 그라운딩을 못 넘길 때다. 여기서 보는
것은 "true라고 적었으면 그 근거가 있는가"뿐이다.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from . import vocab

FACTSHEET_SCHEMA: dict[str, Any] = vocab.FACTSHEET_SCHEMA_DOC

#: specs/schema/factsheet.schema.json — 손으로 옮겨 적지 않는다 (ADR-0034 §3).
CONFIDENCE_LEVELS: tuple[str, ...] = tuple(
    FACTSHEET_SCHEMA["$defs"]["fact"]["properties"]["confidence"]["enum"]
)

#: 관측 지표 3(구체적 숫자)이 true일 때 실제로 있어야 하는 고유 숫자 개수.
MIN_NUMBERS: int = FACTSHEET_SCHEMA["checks"]["min_numbers"]

_VALIDATOR = Draft202012Validator(FACTSHEET_SCHEMA, registry=vocab.REGISTRY)


def schema_errors(data: Any) -> list[str]:
    """JSON Schema 위반 목록."""
    errors = []
    for err in sorted(_VALIDATOR.iter_errors(data), key=lambda e: list(e.absolute_path)):
        location = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{location}: {err.message}")
    return errors


def semantic_errors(data: dict[str, Any]) -> list[str]:
    """스펙 06의 교차 규칙 검사 (스키마로 표현 불가한 부분)."""
    errors: list[str] = []

    conditions = data.get("conditions", {})
    facts = data.get("facts", [])

    # ADR-0033 §1 — conditions와 verdict를 묶던 규칙은 없앴다. 지표가 비어도 pass일
    # 수 있고, 네 지표가 다 차 있어도 매체 적합성에서 fail일 수 있다.

    # 규칙: fact마다 출처 필수 (스키마가 minLength로 막지만 공백 문자열 방어)
    seen_ids: set[str] = set()
    for idx, fact in enumerate(facts):
        if not isinstance(fact, dict):
            continue
        fid = fact.get("id", f"#{idx}")
        if not str(fact.get("source", "")).strip():
            errors.append(f"facts/{fid}: 출처(source)가 비어 있다")
        if fid in seen_ids:
            errors.append(f"facts/{fid}: id가 중복됐다")
        seen_ids.add(fid)

    # 규칙: numbers를 true라고 적었으면 그만큼의 숫자가 실제로 있어야 한다
    unique_numbers = {
        str(n).strip()
        for fact in facts
        if isinstance(fact, dict)
        for n in fact.get("numbers", [])
        if str(n).strip()
    }
    if conditions.get("numbers") and len(unique_numbers) < MIN_NUMBERS:
        errors.append(
            f"conditions/numbers: true인데 facts의 고유 숫자가 "
            f"{len(unique_numbers)}개다 (최소 {MIN_NUMBERS}개)"
        )

    # 규칙: 지표가 true면 대응 근거 필드가 채워져 있어야 한다
    if conditions.get("twist") and not str(data.get("twist", "")).strip():
        errors.append("twist: conditions.twist가 true인데 뒤집기 문장이 비어 있다")
    if conditions.get("failed_alternative") and not data.get("failed_alternatives"):
        errors.append(
            "failed_alternatives: conditions.failed_alternative가 true인데 목록이 비어 있다"
        )
    if conditions.get("present_link") and not str(data.get("present_link", "")).strip():
        errors.append("present_link: conditions.present_link가 true인데 비어 있다")

    return errors


def semantic_warnings(data: dict[str, Any]) -> list[str]:
    """차단하지는 않지만 하류 단계가 알아야 할 사항."""
    warnings: list[str] = []
    low = [
        f.get("id", "?")
        for f in data.get("facts", [])
        if isinstance(f, dict) and f.get("confidence") == "low"
    ]
    if low:
        warnings.append(
            f"confidence=low 사실 {len(low)}건({', '.join(low)})은 대본에 사용 금지 "
            "(specs/06 규칙, ADR-0007)"
        )
    return warnings


def validate_factsheet(data: Any) -> tuple[list[str], list[str]]:
    """(errors, warnings)를 돌려준다. errors가 비어야 계약 통과."""
    errors = schema_errors(data)
    if errors:
        # 스키마가 깨졌으면 semantic 검사는 의미가 없다
        return errors, []
    return semantic_errors(data), semantic_warnings(data)
