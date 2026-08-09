"""대본 ↔ 팩트시트 그라운딩 검증. ADR-0007 + specs/05-pipeline.md + specs/06-topic-research.md.

ADR-0007: "대본의 모든 숫자를 추출해 팩트시트 numbers와 대조, 미출처 숫자 발견 시 재생성."

## 이 모듈이 보는 것

씬의 `text`와 `emphasis.value`. emphasis는 화면에 합성되는 대형 숫자(레이어 B, ADR-0002)라
대본 본문만큼 노출되는 사실 주장이므로 같이 대조한다.

## 이 모듈이 보지 않는 것 (의도적)

- **인명·지명.** ADR-0007 본문은 이것도 금지하지만, 검증 절차로 명시된 것은 숫자 대조뿐이고
  (specs/05) 인명·지명 추출은 형태소 분석기가 필요하다. 여기서 하지 않는다.
- **단위.** 값만 비교한다. 팩트시트의 단위 표기가 열려 있어서("척", "자호", "구간", "km", "%")
  단위까지 맞추면 오탐이 더 커진다. 이 모듈에서 "18.6km"와 "18.6%"는 같은 값이다.
- **한글로 풀어 쓴 수사.** "십일만", "아흔일곱"은 잡지 못한다. 숫자 음절('이','사','만','조')이
  일상 어휘와 겹쳐 정규식으로 가르면 오탐이 폭발한다. 대본은 숫자를 아라비아 숫자로 쓴다
  (specs/01의 예: "11만 8천 명"). 팩트시트 쪽에 이런 항목이 있으면 경고로 알린다.

스펙 01의 대본 규칙(분량·시그니처·수미상관)과 스펙 02 씬 스키마는 각각 다른 검증기가 맡는다.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

#: 한글 자릿수. 만 이상은 앞의 누적값 전체에 곱하고, 천 이하는 조각 단위로 더한다.
MAGNITUDES: dict[str, int] = {
    "조": 10**12,
    "억": 10**8,
    "만": 10**4,
    "천": 10**3,
    "백": 10**2,
    "십": 10,
}

#: specs/06 — confidence: low 사실은 대본에 사용 금지
EXCLUDED_CONFIDENCE = "low"

_DIGITS = r"\d[\d,]*(?:\.\d+)?"
_MAG = "".join(MAGNITUDES)

#: "11만 8천"처럼 자릿수로 이어지는 표현만 하나로 묶는다.
#: 자릿수 없이 공백으로 나열된 숫자("2월 23일")는 각각 별개 값이다.
_NUMBER_EXPR = re.compile(
    rf"{_DIGITS}(?:\s*[{_MAG}]\s*{_DIGITS})*(?:\s*[{_MAG}])?"
)
_TOKEN = re.compile(rf"({_DIGITS})|([{_MAG}])")


def _parse_expr(expr: str) -> Decimal | None:
    """숫자 표현 하나를 값으로 접는다. "11만 8천" → 118000."""
    total = Decimal(0)
    small = Decimal(0)
    pending: Decimal | None = None

    for digits, mag in _TOKEN.findall(expr):
        if digits:
            try:
                pending = Decimal(digits.replace(",", ""))
            except InvalidOperation:
                return None
            continue

        factor = MAGNITUDES[mag]
        head = pending if pending is not None else Decimal(1)
        if factor >= MAGNITUDES["만"]:
            # 만·억·조는 지금까지 쌓인 조각 전체를 묶어서 곱한다
            total += (small + (pending or Decimal(0))) * factor
            small = Decimal(0)
        else:
            small += head * factor
        pending = None

    if pending is not None:
        small += pending
    return total + small


def extract_values(text: str) -> list[tuple[str, Decimal]]:
    """텍스트에서 (원문 조각, 값) 쌍을 순서대로 뽑는다."""
    found: list[tuple[str, Decimal]] = []
    for match in _NUMBER_EXPR.finditer(text or ""):
        raw = match.group().strip()
        value = _parse_expr(raw)
        if value is not None:
            found.append((raw, value))
    return found


def factsheet_values(factsheet: dict[str, Any]) -> tuple[set[Decimal], set[Decimal], list[str]]:
    """팩트시트 numbers를 (허용 값, low 전용 값, 파싱 불가 항목)으로 가른다."""
    allowed: set[Decimal] = set()
    low: set[Decimal] = set()
    unparsable: list[str] = []

    for fact in factsheet.get("facts", []):
        if not isinstance(fact, dict):
            continue
        bucket = low if fact.get("confidence") == EXCLUDED_CONFIDENCE else allowed
        for raw in fact.get("numbers", []):
            values = extract_values(str(raw))
            if not values:
                unparsable.append(str(raw))
                continue
            bucket.update(value for _, value in values)

    return allowed, low - allowed, unparsable


def _scene_sources(scene: dict[str, Any]) -> list[tuple[str, str]]:
    """(필드명, 검사할 문자열) 목록."""
    sources = [("text", str(scene.get("text", "")))]
    emphasis = scene.get("emphasis")
    if isinstance(emphasis, dict) and emphasis.get("value"):
        sources.append(("emphasis", str(emphasis["value"])))
    return sources


def validate_grounding(
    scenes: dict[str, Any], factsheet: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """(errors, warnings)를 돌려준다. errors가 비어야 그라운딩 통과 (ADR-0007)."""
    errors: list[str] = []
    warnings: list[str] = []

    allowed, low_only, unparsable = factsheet_values(factsheet)

    for scene in scenes.get("scenes", []):
        if not isinstance(scene, dict):
            continue
        sid = scene.get("scene_id", "?")
        for field, text in _scene_sources(scene):
            for raw, value in extract_values(text):
                if value in allowed:
                    continue
                if value in low_only:
                    errors.append(
                        f"scenes/{sid}/{field}: '{raw}'는 confidence=low 사실의 숫자다 "
                        f"(specs/06 — 대본 사용 금지)"
                    )
                else:
                    errors.append(
                        f"scenes/{sid}/{field}: '{raw}'가 팩트시트 numbers에 없다 (ADR-0007)"
                    )

    if unparsable:
        warnings.append(
            f"팩트시트 numbers 중 값을 못 읽은 항목 {len(unparsable)}건"
            f"({', '.join(unparsable[:5])}): 대본이 이 숫자를 쓰면 미출처로 잡힌다"
        )

    return errors, warnings
