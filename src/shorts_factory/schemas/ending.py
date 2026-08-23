"""`[8] ending` 산출(`ending.json`) 검증. specs/05-pipeline.md + specs/schema/ending.schema.json.

**게시 가능 라이선스도 장수도 초 수도 이 파일에 없다.** `ending.schema.json`의 `meta`에서
로드한다 (ADR-0034 §3).

## 이 파일이 답하는 질문 하나

**"이 사진을 완성 영상에 실어 배포해도 되는가."**

`refs.py`의 `attachable`과 **다른 질문이다.** 그쪽은 "이미지 프롬프트에 입력으로 붙여도
되는가"이고, 붙인 사진은 화면에 나가지 않는다 (ADR-0030). 이쪽은 사진이 그대로 화면에
게시된다 — 오늘 두 목록의 값이 같아도 축이 다르므로 따로 둔다. 한쪽이 넓어졌다고 다른
쪽이 따라가면 안 되는 자리다 (ADR-0055).

## 게시 여부도 판단이 아니라 대조다

`refs.py`가 `attachable`을 세션에서 뺏어 온 것과 같은 이유다. 라이선스 대조와 **표시
의무 확인**(`cc-by`·`cc-by-sa`에 `credit`이 있는가)은 기계가 하고, 세션은 그림에만
답한다 — 실사인가, 대상이 보이는가, **인물이 있는가**, 사고가 없는가.

**인물 판정만은 세션 몫이다.** 그것은 라이선스가 답할 수 없는 축이고(자유 라이선스
사진에도 초상권은 남는다) 파일을 봐야 알 수 있다.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from . import vocab

#: specs/schema/ending.schema.json — 손으로 옮겨 적지 않는다 (ADR-0034 §3).
ENDING_SCHEMA: dict[str, Any] = vocab.ENDING_SCHEMA_DOC

#: 계약 파일과 클립이 놓이는 자리 (specs/05 계약 표).
RECORD_FILE = "ending.json"
ENDING_DIR = "ending"
CREDITS_FILE = "credits.txt"

#: 기각 주체. 되돌릴 조건을 볼 때 두 축을 갈라 봐야 한다 (ADR-0055).
BY_MACHINE = "machine"
BY_REVIEW = "review"

_VALIDATOR = Draft202012Validator(ENDING_SCHEMA, registry=vocab.REGISTRY)


def publishable_licenses() -> tuple[str, ...]:
    """화면에 게시해도 되는 라이선스. `ending.schema.json`의 `meta`가 정본이다."""
    return tuple(ENDING_SCHEMA["meta"]["publishable_licenses"])


def credit_required_licenses() -> tuple[str, ...]:
    """저작자 표시가 의무인 라이선스."""
    return tuple(ENDING_SCHEMA["meta"]["credit_required_licenses"])


def max_photos() -> int:
    """엔딩에 세울 사진 수의 상한."""
    return int(ENDING_SCHEMA["meta"]["max_photos"])


def photo_seconds() -> float:
    """사진 한 장이 화면에 서 있는 시간(초)."""
    return float(ENDING_SCHEMA["meta"]["photo_seconds"])


def is_publishable(license_name: str) -> bool:
    """라이선스 하나가 게시 가능한가. 불명·저작권 있음은 애초에 내려받히지도 않았다."""
    return license_name in publishable_licenses()


def needs_credit(license_name: str) -> bool:
    """저작자 표시가 의무인가."""
    return license_name in credit_required_licenses()


def credit_ok(license_name: str, credit: str) -> bool:
    """표시 의무를 지킬 수 있는가.

    의무가 있는데 표시할 이름이 없으면 **쓸 수 없는 사진이다.** 라이선스를 어기는
    쪽보다 후보에서 빼는 쪽이 안전하고, 이것은 판단이 아니라 대조다.
    """
    return bool(credit.strip()) if needs_credit(license_name) else True


def schema_errors(data: Any) -> list[str]:
    """JSON Schema 위반 목록."""
    errors = []
    for err in sorted(_VALIDATOR.iter_errors(data), key=lambda e: list(e.absolute_path)):
        location = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{location}: {err.message}")
    return errors


def semantic_errors(data: dict[str, Any]) -> list[str]:
    """스키마로 표현되지 않는 교차 규칙."""
    errors: list[str] = []
    photos = data.get("photos", [])

    limit = max_photos()
    if len(photos) > limit:
        errors.append(f"photos: {len(photos)}장인데 상한은 {limit}장이다")

    for position, photo in enumerate(photos):
        where = f"photos/{position}"
        if photo.get("index") != position + 1:
            errors.append(
                f"{where}: index가 {photo.get('index')}인데 {position + 1}번째다 — "
                "표시 순서와 어긋난다"
            )

        license_name = str(photo.get("license", ""))
        if not is_publishable(license_name):
            errors.append(
                f"{where}: 라이선스가 '{license_name}'인데 화면에 나간다 "
                f"(게시 가능: {', '.join(publishable_licenses())})"
            )
        if not credit_ok(license_name, str(photo.get("credit", ""))):
            errors.append(
                f"{where}: '{license_name}'은 저작자 표시가 의무인데 credit이 비어 있다"
            )

    return errors


def validate_ending(data: Any) -> list[str]:
    """errors가 비어야 계약 통과. **경고는 없다.**"""
    errors = schema_errors(data)
    return errors or semantic_errors(data)


def build_document(
    run_id: str,
    photos: list[dict[str, Any]],
    *,
    topic: str = "",
    source_refs: str = "",
    rejected: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """문서 하나. `photos`는 이미 표시 순서로 정렬된 것을 받는다."""
    document: dict[str, Any] = {"run_id": run_id}
    if topic:
        document["topic"] = topic
    if source_refs:
        document["source_refs"] = source_refs
    document["photos"] = photos
    document["rejected"] = rejected or []
    document["warnings"] = warnings or []
    return document
