"""scenes.timed.json 계약 (specs/02, ADR-0017).

specs/02는 이 파일을 "씬 스키마 그대로, `est_*`만 `start`/`end`로"라고 정의한다.
그래서 스키마는 손으로 옮겨 적지 않고 파생시킨다 — 이 테스트가 그 파생이 실제로
씬 스키마를 따라가는지를 본다.
"""

import pytest

from conftest import PISA, load_script
from shorts_factory.schemas.scenes import SCENE_SCHEMA
from shorts_factory.schemas.timed_scenes import (
    TIMED_SCENE_SCHEMA,
    build_timed_scenes,
    validate_timed_scenes,
)


def timed(**overrides):
    doc = build_timed_scenes(
        {
            "run_id": "20260810-x",
            "topic": "테스트",
            "scenes": [
                {
                    "scene_id": 1, "beat": "hook_fact", "text": "가.",
                    "est_start": 0.0, "est_end": 2.0, "subject": "가",
                    "camera": "static", "motion": "kenburns", "notes": "",
                },
                {
                    "scene_id": 2, "beat": "ending_echo", "text": "나.",
                    "est_start": 2.0, "est_end": 4.0, "subject": "나",
                    "camera": "static", "motion": "kenburns", "notes": "",
                },
            ],
        },
        [(0.0, 2.5), (2.5, 5.0)],
    )
    doc.update(overrides)
    return doc


# --- 스키마 파생 --------------------------------------------------------------


def test_timed_scene_schema_renames_only_the_time_fields():
    assert set(TIMED_SCENE_SCHEMA["properties"]) - {"start", "end"} == (
        set(SCENE_SCHEMA["properties"]) - {"est_start", "est_end"}
    )
    assert TIMED_SCENE_SCHEMA["required"].count("start") == 1
    assert "est_start" not in TIMED_SCENE_SCHEMA["required"]


def test_estimates_are_not_allowed_in_the_measured_file():
    """추정과 실측은 파일 단위로 갈린다 (ADR-0017)."""
    doc = timed()
    doc["scenes"][0]["est_start"] = 0.0

    errors, _ = validate_timed_scenes(doc)
    assert any("est_start" in e for e in errors)


def test_measured_document_passes_its_own_contract():
    errors, warnings = validate_timed_scenes(timed())
    assert errors == []
    assert warnings == []


# --- 교차 규칙 ----------------------------------------------------------------


def test_overlapping_scenes_are_an_error():
    doc = timed()
    doc["scenes"][1]["start"] = 1.0

    errors, _ = validate_timed_scenes(doc)
    assert any("앞 씬의 end" in e for e in errors)


def test_zero_length_scene_is_an_error():
    doc = timed()
    doc["scenes"][0]["end"] = 0.0

    errors, _ = validate_timed_scenes(doc)
    assert any("start(0.0) >= end(0.0)" in e for e in errors)


def test_gap_between_scenes_is_a_warning():
    doc = timed()
    doc["scenes"][1]["start"] = 3.0

    errors, warnings = validate_timed_scenes(doc)
    assert errors == []
    assert any("떨어져 있다" in w for w in warnings)


def test_total_duration_must_track_the_last_scene():
    doc = timed(total_duration=90.0)
    _, warnings = validate_timed_scenes(doc)
    assert any("total_duration" in w for w in warnings)


# --- 변환 --------------------------------------------------------------------


def test_build_keeps_every_other_field_untouched():
    source = load_script(PISA)
    boundaries = [(s["est_start"], s["est_end"]) for s in source["scenes"]]

    doc = build_timed_scenes(source, boundaries)

    for before, after in zip(source["scenes"], doc["scenes"]):
        assert {k: v for k, v in before.items() if not k.startswith("est_")} == {
            k: v for k, v in after.items() if k not in ("start", "end")
        }
    assert doc["run_id"] == source["run_id"], "계보는 run_id로 잇는다 (ADR-0017)"
    assert doc["total_duration"] == doc["scenes"][-1]["end"]


def test_build_puts_start_end_where_est_fields_were():
    source = load_script(PISA)
    doc = build_timed_scenes(
        source, [(s["est_start"], s["est_end"]) for s in source["scenes"]]
    )
    keys = list(doc["scenes"][0])
    assert keys.index("start") == list(source["scenes"][0]).index("est_start")


def test_build_refuses_a_boundary_count_mismatch():
    with pytest.raises(ValueError, match="경계"):
        build_timed_scenes(load_script(PISA), [(0.0, 1.0)])


def test_build_does_not_mutate_the_source_script():
    """06-script.json은 읽기 전용이다 (ADR-0017)."""
    source = load_script(PISA)
    before = load_script(PISA)

    build_timed_scenes(source, [(s["est_start"], s["est_end"]) for s in source["scenes"]])

    assert source == before
