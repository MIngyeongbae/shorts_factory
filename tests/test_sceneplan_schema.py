"""`[3s. scenetable]` 세션 산출(씬 연출표) 계약 검증. CLAUDE.md 최소 기준: 픽스처 JSON → 스키마 검증 통과.

**계약 테스트다** (ADR-0034 §4). 옛 씬 계획 계약(`act`·`says`·`char_budget`)의 테스트는
그 단계와 함께 죽었다 (ADR-0049) — 여기 남은 것은 새 계약이 지키는 것들이다:

- 연출은 닫힌 어휘에서만 고른다 (ADR-0033 §3)
- 필드 정의는 `scene.schema.json`의 `$ref`다 — 두 스키마가 갈라질 수 없다 (ADR-0034)
- 어느 필드가 `scenes.json`으로 건너가는지는 교집합(`carried_fields`)이 계산한다
- 연출 공백은 관측이지 판정이 아니다 (ADR-0033 되돌릴 조건의 관측 수단)
"""

from __future__ import annotations

import pytest

from conftest import HOOVER, load_script
from shorts_factory.schemas import vocab
from shorts_factory.schemas.sceneplan import (
    PLANNED_SCENE_SCHEMA,
    carried_fields,
    direction_summary,
    validate_sceneplan,
)


def table_of(slug: str = HOOVER) -> dict:
    """실물 씬 계약 → `[3s]` 세션이 냈을 모양의 연출표.

    `text`·`est_*`는 실측 파일의 몫이라 뺀다.
    """
    script = load_script(slug)
    fields = set(carried_fields())
    return {
        "scenes": [
            {key: value for key, value in scene.items() if key in fields}
            for scene in script["scenes"]
        ]
    }


@pytest.fixture
def plan() -> dict:
    return table_of()


def test_real_script_as_a_table_is_valid(plan):
    """실물 씬 계약에서 [3s] 몫만 추리면 연출표 계약을 그대로 통과해야 한다."""
    errors, _warnings = validate_sceneplan(plan)
    assert errors == []


def test_topic_key_is_optional(plan):
    plan["topic"] = "후버댐"
    errors, _ = validate_sceneplan(plan)
    assert errors == []


# --- 씬 경계는 [3]의 실측이다 ---------------------------------------------


def test_duplicate_scene_ids_are_rejected(plan):
    """중복만 여기서 잡는다 — 누락·날조는 [3s]가 실측 줄과 대조해 잡는다."""
    plan["scenes"][4]["scene_id"] = plan["scenes"][3]["scene_id"]
    errors, _ = validate_sceneplan(plan)
    assert any("2번 나온다" in e for e in errors)


def test_text_is_not_the_sessions_to_write(plan):
    """대본 문장은 실측 파일의 값이다 (ADR-0020). 세션이 내면 계약 위반이다."""
    plan["scenes"][0]["text"] = "세션이 지어낸 문장"
    errors, _ = validate_sceneplan(plan)
    assert errors


def test_measured_time_is_not_the_sessions_to_write(plan):
    plan["scenes"][0]["est_start"] = 0.0
    errors, _ = validate_sceneplan(plan)
    assert errors


def test_motion_is_not_a_field_any_more(plan):
    """전 씬이 영상 클립이다 (ADR-0056). 세션이 motion을 내면 위반이다."""
    plan["scenes"][0]["motion"] = "kenburns"
    errors, _ = validate_sceneplan(plan)
    assert errors


def test_staging_is_the_sessions_to_choose(plan):
    """무대는 [3s]가 어휘에서 고른다 (ADR-0056 결정 4) — 어휘 밖은 위반, 비면 기본값이다."""
    plan["scenes"][0]["staging"] = "location"
    assert validate_sceneplan(plan)[0] == []
    plan["scenes"][0]["staging"] = "underwater"
    assert any("staging" in e for e in validate_sceneplan(plan)[0])


def test_info_takes_ascii_labels_target_and_annotation(plan):
    """계측 표시 (ADR-0056 결정 3) — 라벨은 ASCII, 대상은 영어 서술, 방식은 어휘."""
    plan["scenes"][0]["info"] = {
        "labels": ["221 m"], "target": "the full height of the dam", "annotation": "dimension",
    }
    assert validate_sceneplan(plan)[0] == []
    plan["scenes"][0]["info"]["labels"] = ["높이 221m"]
    assert any("labels" in e for e in validate_sceneplan(plan)[0])


# --- 닫힌 어휘 (ADR-0033 §3) -----------------------------------------------


def test_unknown_framing_is_rejected(plan):
    plan["scenes"][0]["framing"] = "dutch_angle"
    errors, _ = validate_sceneplan(plan)
    assert any("framing" in e for e in errors)


def test_unknown_beat_is_rejected(plan):
    plan["scenes"][0]["beat"] = "cliffhanger"
    errors, _ = validate_sceneplan(plan)
    assert any("beat" in e for e in errors)


def test_figure_framing_is_available():
    """인물 framing 4종은 어휘에 있다 (ADR-0051) — 연출표가 고를 수 있어야 한다."""
    assert "figure_back" in vocab.values("framing")


# --- 인물 블록 (ADR-0051) ---------------------------------------------------


def test_characters_block_is_optional(plan):
    """없으면 인물 경로 전체가 스킵된다 — 부재는 경고가 아니다 (D-3)."""
    errors, warnings = validate_sceneplan(plan)
    assert errors == []
    assert not any("characters" in w for w in warnings)


def test_characters_block_validates(plan):
    plan["characters"] = [
        {"id": "wonhyo", "name": "원효", "anchor": "元曉", "appearance": "잿빛 승복의 승려"}
    ]
    plan["scenes"][0]["cast"] = ["wonhyo"]
    errors, _ = validate_sceneplan(plan)
    assert errors == []


def test_character_without_appearance_is_rejected(plan):
    """appearance가 프롬프트에 그대로 들어간다 (ADR-0027) — 없으면 시트를 만들 수 없다."""
    plan["characters"] = [{"id": "wonhyo", "name": "원효"}]
    errors, _ = validate_sceneplan(plan)
    assert any("appearance" in e for e in errors)


def test_cast_id_format_is_checked(plan):
    plan["scenes"][0]["cast"] = ["원효"]
    errors, _ = validate_sceneplan(plan)
    assert any("cast" in e for e in errors)


# --- 연출 공백은 관측이지 판정이 아니다 (ADR-0033) --------------------------


def test_blank_framing_warns_but_does_not_block(plan):
    total = len(plan["scenes"])
    for scene in plan["scenes"]:
        scene.pop("framing", None)
    errors, warnings = validate_sceneplan(plan)
    assert errors == []
    assert any(f"framing이 빈 씬 {total}/{total}개" in w for w in warnings)


def test_all_blank_anchors_warn(plan):
    for scene in plan["scenes"]:
        scene.pop("subject_anchor", None)
    _errors, warnings = validate_sceneplan(plan)
    assert any("subject_anchor가 전 씬에서" in w for w in warnings)


def test_direction_summary_counts_without_judging(plan):
    total = len(plan["scenes"])
    summary = direction_summary(plan)
    assert summary["scene_count"] == total
    assert sum(summary["framing"].values()) == total
    plan["scenes"][0]["info"] = {
        "labels": ["221 m"], "target": "the full height of the dam", "annotation": "dimension",
    }
    assert direction_summary(plan)["info"] == 1
    assert "staging" in summary


def test_blank_staging_is_counted_not_blocked(plan):
    """specs/05 [3s] — 빈 framing·transition·staging 씬 수를 기록에 남긴다."""
    total = len(plan["scenes"])
    for scene in plan["scenes"]:
        scene.pop("staging", None)
    errors, warnings = validate_sceneplan(plan)
    assert errors == []
    assert any(f"staging이 빈 씬 {total}/{total}개" in w for w in warnings)


# --- 두 스키마는 갈라질 수 없다 (ADR-0034) ---------------------------------


def test_carried_fields_is_the_intersection_of_two_schemas():
    """목록을 손으로 적지 않는다. 스키마에 필드가 늘면 병합이 저절로 따라온다."""
    plan_props = set(PLANNED_SCENE_SCHEMA["properties"])
    scene_props = set(vocab.SCENE_SCHEMA_DOC["$defs"]["scene"]["properties"])
    assert set(carried_fields()) == plan_props & scene_props
    # 씬 계약에만 있는 것 = 실측 파일의 몫(text·시각)뿐이다 — motion은 ADR-0056이 지웠다
    assert scene_props - plan_props == {"text", "est_start", "est_end"}
    # 연출표에만 있는 필드는 없다 — 전부 scenes.json으로 건너간다
    assert plan_props - scene_props == set()


def test_every_planned_field_is_a_ref_into_the_scene_contract():
    """필드 정의를 복사하지 않고 `$ref`로 가리킨다 — 정의가 갈라질 자리 자체가 없다."""
    for name, prop in PLANNED_SCENE_SCHEMA["properties"].items():
        assert set(prop) == {"$ref"}, f"{name}: $ref가 아니라 복사다"
        assert prop["$ref"].startswith("scene.schema.json#/"), name


def test_characters_block_is_a_ref_into_the_scene_contract():
    prop = vocab.SCENEPLAN_SCHEMA_DOC["properties"]["characters"]
    assert prop == {"$ref": "scene.schema.json#/properties/characters"}
