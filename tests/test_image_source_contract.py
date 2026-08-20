"""`image_source.json` 계약 — `[7]`의 영상 입력 (ADR-0041, specs/05 2부 계약 표).

이 파일이 답하는 질문은 하나다: **씬 N의 화면에 지금 있는 그림은 어느 잡의 몇 번째
장인가.** 확인 대상은 그 계약이 지켜지는가와, 그 값을 만드는 `[6]`·`[6r]`이 화면과
어긋나지 않는가다.

`images.json`을 계약으로 승격하지 않는다는 것이 ADR-0024 §2이고, 이 파일의 존재
이유가 그것이다.
"""

import json

import pytest
from conftest import PISA

from shorts_factory.schemas.image_source import (
    IMAGE_SOURCE_SCHEMA,
    build_document,
    by_scene,
    validate_image_source,
)


def doc(*scenes, run_id="20260819-pisa", provider="midjourney"):
    """문서를 **손으로** 짓는다 — 계약 위반도 만들어야 하므로 빌더를 쓰지 않는다."""
    return {"run_id": run_id, "provider": provider, "scenes": list(scenes)}


def entry(scene_id, task_id="task-1", quadrant=0, set_by="6-imagegen"):
    return {
        "scene_id": scene_id,
        "task_id": task_id,
        "quadrant": quadrant,
        "set_by": set_by,
    }


# --- 계약 -------------------------------------------------------------------


def test_the_schema_is_loaded_not_declared():
    """값은 `specs/schema/`에 한 번만 적는다 (ADR-0034 §3)."""
    assert IMAGE_SOURCE_SCHEMA["$id"] == "image-source.schema.json"


def test_a_minimal_document_passes():
    assert validate_image_source(doc(entry(1), entry(2))) == []


def test_an_empty_scene_list_passes():
    """영상 입력이 한 씬도 없을 수 있다 — `[7]`은 전 씬 Ken Burns로 돈다 (D-3)."""
    assert validate_image_source(doc()) == []


@pytest.mark.parametrize("missing", ["scene_id", "task_id", "quadrant"])
def test_the_three_required_fields_are_required(missing):
    bad = entry(1)
    bad.pop(missing)
    assert any(missing in e for e in validate_image_source(doc(bad)))


def test_a_null_task_id_is_not_a_contract():
    """널 필드를 두지 않는다 (ADR-0041 결정 2). 입력이 없으면 **항목이 없다**.

    널을 허용하면 `[7]`의 규칙이 "항목이 없으면 강등" 한 줄로 끝나지 않는다.
    """
    assert validate_image_source(doc(entry(1, task_id=None)))


def test_unknown_fields_are_rejected():
    """계약이 값 둘짜리로 남아야 `[7]`이 `[6r]`의 어휘를 모른 채로 있다 (D-2)."""
    assert validate_image_source(doc({**entry(1), "verdict": "pick"}))


def test_the_same_scene_cannot_appear_twice():
    assert any("두 번" in e for e in validate_image_source(doc(entry(1), entry(1))))


def test_provider_is_required():
    """잡 id는 그것을 만든 곳에서만 통한다 — 누구 것인지 없으면 계약이 아니다."""
    document = doc(entry(1))
    document.pop("provider")
    assert validate_image_source(document)


# --- 도우미 -----------------------------------------------------------------


def test_the_builder_sorts_scenes_by_scene_id():
    """실행 순서가 달라도 파일이 같아야 눈으로 비교할 수 있다."""
    document = build_document("r", "midjourney", [entry(3), entry(1), entry(2)])
    assert [s["scene_id"] for s in document["scenes"]] == [1, 2, 3]
    assert validate_image_source(document) == []


def test_by_scene_indexes_on_int_keys():
    assert by_scene(doc(entry(2, quadrant=3)))[2]["quadrant"] == 3
