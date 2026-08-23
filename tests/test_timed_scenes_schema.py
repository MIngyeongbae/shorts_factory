"""scenes.timed.{lang}.json 계약 (specs/02·05, ADR-0017·0056).

specs/02는 이 파일을 "씬 스키마 그대로, `est_*`만 `start`/`end`로"라고 정의한다.
그래서 스키마는 손으로 옮겨 적지 않고 파생시킨다 — 이 테스트가 그 파생이 실제로
씬 스키마를 따라가는지를 본다.
"""

import pytest

from conftest import PISA, load_script
from shorts_factory.schemas.scenes import SCENE_SCHEMA
from shorts_factory.schemas.timed_scenes import (
    DROPPED,
    TIMED_SCENE_SCHEMA,
    TIMED_SCENES_PATTERN,
    build_timed_scenes,
    timed_scenes_path,
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
                    "subject_scale": "wide",
                    "camera": "static", "notes": "",
                },
                {
                    "scene_id": 2, "beat": "ending_echo", "text": "나.",
                    "est_start": 2.0, "est_end": 4.0, "subject": "나",
                    "subject_scale": "close",
                    "camera": "static", "notes": "",
                },
            ],
        },
        [(0.0, 2.5), (2.5, 5.0)],
    )
    doc.update(overrides)
    return doc


# --- 스키마 파생 --------------------------------------------------------------


def test_timed_scene_schema_renames_the_time_fields_and_drops_the_image_ones():
    assert set(TIMED_SCENE_SCHEMA["properties"]) - {"start", "end"} == (
        set(SCENE_SCHEMA["properties"]) - {"est_start", "est_end"} - set(DROPPED)
    )
    assert TIMED_SCENE_SCHEMA["required"].count("start") == 1
    assert "est_start" not in TIMED_SCENE_SCHEMA["required"]


def test_visual_goal_does_not_travel_into_the_measured_file():
    """이미지 지시는 prompts.json 몫이다 (ADR-0020·0022).

    이 파일을 읽는 곳은 [7](클립 길이·카메라·모션)과 [9](전환·자막)뿐이고 둘 다
    그림이 무엇을 설명하는지 알 필요가 없다. 같은 값이 두 파일에 있으면 갈라진다.
    """
    assert "visual_goal" in SCENE_SCHEMA["properties"]
    assert "visual_goal" not in TIMED_SCENE_SCHEMA["properties"]
    assert "visual_goal" not in TIMED_SCENE_SCHEMA["required"]

    doc = build_timed_scenes(
        load_script(PISA),
        [(s["est_start"], s["est_end"]) for s in load_script(PISA)["scenes"]],
    )
    assert all("visual_goal" not in scene for scene in doc["scenes"])


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
        carried = {
            k: v
            for k, v in before.items()
            if not k.startswith("est_") and k not in DROPPED
        }
        assert carried == {
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
    """씬 계약은 읽기 전용이다 (ADR-0017)."""
    source = load_script(PISA)
    before = load_script(PISA)

    build_timed_scenes(source, [(s["est_start"], s["est_end"]) for s in source["scenes"]])

    assert source == before


# --- 언어별 파일 (ADR-0056 결정 5) ---------------------------------------------


@pytest.mark.parametrize("lang", ["ko", "ja", "en"])
def test_timed_scenes_path_is_per_language(tmp_path, lang):
    """시각을 읽는 곳은 언어당 `scenes.timed.{lang}.json` 하나다 (specs/05 계약 표)."""
    path = timed_scenes_path(tmp_path, lang)
    assert path == tmp_path / TIMED_SCENES_PATTERN.format(lang=lang)
    assert path.name == f"scenes.timed.{lang}.json"


def test_timed_scenes_path_normalises_and_rejects_garbage(tmp_path):
    assert timed_scenes_path(tmp_path, " KO ").name == "scenes.timed.ko.json"
    with pytest.raises(ValueError):
        timed_scenes_path(tmp_path, "../x")
    with pytest.raises(ValueError):
        timed_scenes_path(tmp_path, "")


def test_present_languages_lists_existing_files_in_contract_order(tmp_path):
    """ko 필수 + ja·en 선택 — 파일이 있는 언어만, 늘 ko·ja·en 순서다 (specs/05)."""
    from shorts_factory.schemas.timed_scenes import LANGUAGES, PRIMARY_LANGUAGE, present_languages

    assert LANGUAGES == ("ko", "ja", "en") and PRIMARY_LANGUAGE == "ko"
    assert present_languages(tmp_path) == []
    timed_scenes_path(tmp_path, "en").write_text("{}", encoding="utf-8")
    timed_scenes_path(tmp_path, "ko").write_text("{}", encoding="utf-8")
    assert present_languages(tmp_path) == ["ko", "en"]
