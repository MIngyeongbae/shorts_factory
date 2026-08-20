"""`[1b] score` 산출(`09-score.json`) 검증. specs/05-pipeline.md + specs/schema/score.schema.json.

**축 이름도 만점도 임계값도 이 파일에 없다.** `specs/schema/script-rules.json`의 `score`
절에서 로드한다 (ADR-0034 §3, ADR-0040). 여기 있는 것은 스키마로 표현할 수 없는 교차
규칙뿐이다.

| 무엇 | 왜 여기서 보나 |
|---|---|
| 축 목록 일치 | 스키마는 `axes`를 자유 키 맵으로 둔다. 축 이름이 계약과 같은지는 값 파일을 든 코드만 안다 |
| 축당 점수 상한 | `max_per_axis`가 값 파일에 있으므로 스키마에 상한을 못 박지 않았다 |
| `total` 합산 | 세션이 더한 값이라 틀릴 수 있다. 합이 안 맞으면 채점 전체를 신뢰할 수 없다 |
| `chosen` 실재 | 채점하지 않은 후보를 선발하면 `[2]`가 없는 파일을 읽는다 |
| `verdict` 정합 | `reject`인데 `chosen`이 있거나 그 반대면 하류가 무엇을 믿을지 모른다 |

**임계값 판정은 검증이 아니다.** `min_total`이 `None`이면(캘리브레이션 전) 미달을 낼 수
없고, 그것은 오류가 아니라 상태다 — `below_threshold()`가 그 구분을 진다.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from . import vocab

SCORE_SCHEMA: dict[str, Any] = vocab.SCORE_SCHEMA_DOC

_VALIDATOR = Draft202012Validator(SCORE_SCHEMA, registry=vocab.REGISTRY)


def axes() -> tuple[str, ...]:
    """채점 축 이름. specs/05가 부른 그대로다."""
    return tuple(vocab.score_rules()["axes"])


def max_per_axis() -> int:
    return int(vocab.score_rules()["max_per_axis"])


def thresholds() -> tuple[int | None, int | None]:
    """`(min_total, min_per_axis)`. 캘리브레이션 전이면 `(None, None)`이다."""
    rules = vocab.score_rules()
    total = rules.get("min_total")
    per_axis = rules.get("min_per_axis")
    return (
        int(total) if total is not None else None,
        int(per_axis) if per_axis is not None else None,
    )


def calibrated() -> bool:
    """임계값이 채워졌는가. 아니면 `[1b]`는 채점만 하고 반려하지 않는다."""
    return thresholds()[0] is not None


def schema_errors(data: Any) -> list[str]:
    """JSON Schema 위반 목록."""
    errors = []
    for err in sorted(_VALIDATOR.iter_errors(data), key=lambda e: list(e.absolute_path)):
        location = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{location}: {err.message}")
    return errors


def below_threshold(scored: dict[str, Any]) -> list[str]:
    """이 후보가 기준에 미달한 이유. 캘리브레이션 전이면 항상 빈 목록이다.

    미달은 **오류가 아니다** — 채점이 정상적으로 끝난 결과이므로 `schema_errors`와
    섞지 않는다. 섞으면 "채점기가 고장났다"와 "후보가 약하다"를 하류가 못 가른다.
    """
    min_total, min_per_axis = thresholds()
    if min_total is None:
        return []

    reasons: list[str] = []
    total = int(scored.get("total", 0))
    if total < min_total:
        reasons.append(f"총점 {total} < 기준 {min_total}")
    if min_per_axis is not None:
        for name in axes():
            got = int((scored.get("axes", {}).get(name) or {}).get("score", 0))
            if got < min_per_axis:
                reasons.append(f"{name} {got} < 축 기준 {min_per_axis}")
    return reasons


def semantic_errors(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    """스키마로 못 보는 교차 규칙. `(errors, warnings)`."""
    errors: list[str] = []
    warnings: list[str] = []

    expected = set(axes())
    limit = max_per_axis()
    names = [str(c.get("candidate")) for c in data.get("candidates", [])]

    for scored in data.get("candidates", []):
        name = scored.get("candidate")
        got = set(scored.get("axes", {}))
        if got != expected:
            missing = ", ".join(sorted(expected - got)) or "없음"
            extra = ", ".join(sorted(got - expected)) or "없음"
            errors.append(
                f"candidates/{name}: 축이 계약과 다르다 (빠짐: {missing} / 더함: {extra})"
            )
        over = [
            f"{axis}={item.get('score')}"
            for axis, item in scored.get("axes", {}).items()
            if isinstance(item, dict) and int(item.get("score", 0)) > limit
        ]
        if over:
            errors.append(
                f"candidates/{name}: 축당 만점 {limit}을 넘는다 ({', '.join(over)})"
            )
        summed = sum(
            int(item.get("score", 0))
            for item in scored.get("axes", {}).values()
            if isinstance(item, dict)
        )
        if summed != int(scored.get("total", -1)):
            errors.append(
                f"candidates/{name}: total {scored.get('total')}이 축 합 {summed}과 다르다"
            )
        if not scored.get("critique_reflected"):
            warnings.append(
                f"candidates/{name}: critique_reflected가 비었다 — "
                "비판 반영 채점(specs/05 [1b])이 실제로 일어났는지 알 수 없다"
            )

    if len(names) != len(set(names)):
        errors.append("candidates: 같은 후보를 두 번 채점했다")

    chosen = data.get("chosen")
    verdict = data.get("verdict")
    if verdict == "pass":
        if chosen is None:
            errors.append("verdict가 pass인데 chosen이 없다")
        elif chosen not in names:
            errors.append(f"chosen '{chosen}'이 채점한 후보에 없다")
    elif verdict == "reject" and chosen is not None:
        errors.append("verdict가 reject인데 chosen이 있다 — 미달이면 선발하지 않는다")

    if not calibrated():
        warnings.append(
            "score.min_total이 비어 있다 (ADR-0040 캘리브레이션 전) — "
            "채점만 하고 기준 미달 반려는 하지 않는다"
        )

    return errors, warnings


def validate_score(data: Any) -> tuple[list[str], list[str]]:
    """`(errors, warnings)`. errors가 비어야 계약 통과."""
    errors = schema_errors(data)
    if errors:
        return errors, []
    return semantic_errors(data)
