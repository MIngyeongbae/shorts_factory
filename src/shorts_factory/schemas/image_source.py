"""`image_source.json` 검증 — `[6]`·`[6r]` 산출, `[7]` 입력 (ADR-0041).

**스키마도 값도 이 파일에 없다.** `specs/schema/image-source.schema.json`에서
로드한다 (ADR-0034 §3).

## 이 파일이 답하는 질문 하나

**"씬 N의 화면에 지금 있는 그림은 어느 잡의 몇 번째 장인가."**

`[7]`이 MJ 영상을 만들려면 잡 id와 사분면이 필요한데 둘 다 **이미지 파일 내용에서
복원되지 않는다** — `images/{scene_id}.png`는 바이트일 뿐이다. 그 값을 들고 있는
`images.json`·`image_review.json`은 계약이 아니라 **기록**이라 `[7]`이 열지 않는다
(ADR-0020, ADR-0024 §2). 기록을 계약으로 승격하는 대신 사이드카를 둔 것이 ADR-0041이다.

## 영상 입력이 없는 씬은 항목이 없다

널 필드를 두지 않는다. `[7]`의 규칙이 **"항목이 없으면 강등"** 한 줄이 된다.
항목이 없는 경우는 `[6]`의 인접 씬 폴백 씬과 잡 id 개념이 없는 프로바이더다.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from . import vocab

#: specs/schema/image-source.schema.json — 손으로 옮겨 적지 않는다 (ADR-0034 §3).
IMAGE_SOURCE_SCHEMA: dict[str, Any] = vocab.load("image-source.schema.json")

RECORD_FILE = "image_source.json"

_VALIDATOR = Draft202012Validator(IMAGE_SOURCE_SCHEMA)


def schema_errors(data: Any) -> list[str]:
    errors = []
    for err in sorted(_VALIDATOR.iter_errors(data), key=lambda e: list(e.absolute_path)):
        location = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{location}: {err.message}")
    return errors


def semantic_errors(data: dict[str, Any]) -> list[str]:
    """스키마로 표현되지 않는 교차 규칙 — 씬 하나에 입력 하나다."""
    errors: list[str] = []
    seen: set[int] = set()
    for entry in data.get("scenes", []):
        sid = entry.get("scene_id")
        if sid in seen:
            errors.append(f"scenes/{sid}: 같은 씬이 두 번 있다")
        seen.add(sid)
    return errors


def validate_image_source(data: Any) -> list[str]:
    """errors가 비어야 계약 통과. 경고는 없다 — 이 파일은 값 둘짜리 계약이다."""
    errors = schema_errors(data)
    return errors or semantic_errors(data)


def build_document(
    run_id: str, provider: str, scenes: list[dict[str, Any]]
) -> dict[str, Any]:
    """문서 하나. `scenes`는 `scene_id` 오름차순으로 정렬해 담는다."""
    return {
        "run_id": run_id,
        "provider": provider,
        "scenes": sorted(scenes, key=lambda s: s["scene_id"]),
    }


def by_scene(data: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """`scene_id` → 항목. `[7]`이 씬마다 조회하는 모양이다."""
    return {int(entry["scene_id"]): entry for entry in data.get("scenes", [])}
