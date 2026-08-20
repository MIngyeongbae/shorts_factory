"""`[4] refpack` 산출(`refs.json`) 검증. specs/05-pipeline.md + specs/schema/refs.schema.json.

**첨부 가능 라이선스 목록이 이 파일에 없다.** `refs.schema.json`의 `meta`에서 로드한다
(ADR-0034 §3).

## 이 파일이 답하는 질문 하나

**"씬 N의 대상은 실제로 어떻게 생겼고, 그 근거 사진을 붙여도 되는가."**

`subject`·`subject_anchor`가 **무엇을 그릴지**를 정하고(ADR-0028), `description`은
**그것이 실제로 어떻게 생겼는지**를 더한다. 값의 출처는 여전히 하나다 — 겹치면
`subject`가 이긴다 (ADR-0020).

## 비어 있는 것은 오류가 아니다

도해 씬(`subject_scale: diagram`)과 실물이 존재하지 않는 개념 씬에는 참조가 없다.
씬 항목이 없어도, 있고 `images`가 비어도 계약을 지킨 것이다 (specs/05 D-3).
**부재를 경고로 만들면 편마다 경고가 쏟아져 진짜 경고가 묻힌다.**

## 첨부 여부는 판단이 아니라 대조다

`attachable`은 세션이 고르는 값이 아니라 라이선스가 정하는 값이다. 세션은 라이선스를
**보고하고**, 대조는 `enforce_attachable()`이 한다 — 세션에 맡기면 편마다 기준이
흔들리고, 그것은 저작권이 걸린 축이라 흔들리면 안 되는 쪽이다.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from . import vocab

#: specs/schema/refs.schema.json — 손으로 옮겨 적지 않는다 (ADR-0034 §3).
REFS_SCHEMA: dict[str, Any] = vocab.REFS_SCHEMA_DOC

RECORD_FILE = "refs.json"

_VALIDATOR = Draft202012Validator(REFS_SCHEMA, registry=vocab.REGISTRY)


def attachable_licenses() -> tuple[str, ...]:
    """첨부해도 되는 라이선스. `refs.schema.json`의 `meta`가 정본이다."""
    return tuple(REFS_SCHEMA["meta"]["attachable_licenses"])


def max_images_per_scene() -> int:
    """씬 하나가 들 수 있는 참조 사진 수. 프롬프트에 주입하는 값이다."""
    return int(REFS_SCHEMA["meta"]["max_images_per_scene"])


def is_attachable(license_name: str) -> bool:
    """라이선스 하나가 첨부 가능한가. 불명·저작권 있음은 서술 경로만 탄다."""
    return license_name in attachable_licenses()


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
    seen: set[int] = set()

    for entry in data.get("scenes", []):
        sid = entry.get("scene_id")
        if sid in seen:
            errors.append(f"scenes/{sid}: 같은 씬이 두 번 있다")
        seen.add(sid)

        for index, image in enumerate(entry.get("images", [])):
            where = f"scenes/{sid}/images/{index}"
            license_name = str(image.get("license", ""))
            if image.get("attachable") and not is_attachable(license_name):
                errors.append(
                    f"{where}: 라이선스가 '{license_name}'인데 attachable이다 "
                    f"(첨부 가능: {', '.join(attachable_licenses())})"
                )
            # 첨부하겠다고 해 놓고 파일이 없으면 `[6]`이 붙일 것이 없다. 반대는 정상이다
            # — 서술만 쓰는 사진은 내려받지 않는다.
            if image.get("attachable") and not image.get("file"):
                errors.append(f"{where}: attachable인데 내려받은 파일이 없다")

    return errors


def validate_refs(data: Any) -> list[str]:
    """errors가 비어야 계약 통과. **경고는 없다** — 부재가 이 계약의 정상 상태다."""
    errors = schema_errors(data)
    return errors or semantic_errors(data)


def build_document(
    run_id: str, scenes: list[dict[str, Any]], *, source_script: str = ""
) -> dict[str, Any]:
    """문서 하나. `scenes`는 `scene_id` 오름차순으로 담는다."""
    document: dict[str, Any] = {"run_id": run_id}
    if source_script:
        document["source_script"] = source_script
    document["scenes"] = sorted(scenes, key=lambda s: s["scene_id"])
    return document


def by_scene(data: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """`scene_id` → 항목. 소비 단계가 씬마다 조회하는 모양이다."""
    return {int(entry["scene_id"]): entry for entry in data.get("scenes", [])}


def description_of(data: dict[str, Any], scene_id: int) -> str:
    """씬 하나의 서술. 없으면 빈 문자열이고 그것은 오류가 아니다.

    `[5] prompt`가 쓰는 표면이다 — 강등 사다리의 **서술** 칸이고, 이 값이 비면
    그 씬은 참조 없이 그냥 간다.
    """
    entry = by_scene(data).get(scene_id)
    return str((entry or {}).get("description") or "").strip()


def attachments_of(data: dict[str, Any], scene_id: int) -> list[str]:
    """씬 하나에 붙일 수 있는 파일 경로. 강등 사다리의 **첨부** 칸이다."""
    entry = by_scene(data).get(scene_id) or {}
    return [
        str(image["file"])
        for image in entry.get("images", [])
        if image.get("attachable") and image.get("file")
    ]
