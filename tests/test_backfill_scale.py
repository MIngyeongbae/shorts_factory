"""[1x. backfill-scale] — 확정 대본에 subject_scale만 채운다 (ADR-0018).

이 단계가 지켜야 하는 것은 사실상 하나다: **승인된 대본을 바꾸지 않는다.** 나머지
검사는 전부 그것을 우회할 수 있는 경로를 막는다.

픽스처는 실물 대본 2편이다. 다만 이미 백필이 끝나 `subject_scale`을 들고 있으므로,
"필드가 없던 시절"을 재현하려면 테스트가 직접 걷어내야 한다.
"""

import json

import pytest

from conftest import HOOVER, PISA, load_script
from shorts_factory.config import write_text
from shorts_factory.jsonio import dump_json
from shorts_factory.llm.fake import FakeLLMClient
from shorts_factory.schemas.scenes import validate_scenes
from shorts_factory.stages.backfill_scale import (
    ADDED_FIELD,
    SCRIPT_FILE,
    BackfillStageError,
    apply_scales,
    assert_only_scale_changed,
    parse_scales,
    render_scenes,
    run_backfill_scale_stage,
)

REAL_SLUGS = (PISA, HOOVER)


def stripped_script(slug: str) -> dict:
    """백필 이전 상태 — subject_scale이 없는 대본."""
    script = load_script(slug)
    for scene in script["scenes"]:
        scene.pop(ADDED_FIELD, None)
    return script


@pytest.fixture
def install(paths):
    def _install(slug: str, script: dict | None = None) -> dict:
        script = script or stripped_script(slug)
        write_text(paths.topic_dir(slug) / SCRIPT_FILE, dump_json(script))
        return script

    return _install


def answer_for(script: dict, scale: str = "wide"):
    """세션이 낼 법한 응답. 씬을 전부 같은 값으로 판정한다."""
    return json.dumps(
        {
            "scales": [
                {"scene_id": s["scene_id"], ADDED_FIELD: scale}
                for s in script["scenes"]
            ]
        },
        ensure_ascii=False,
    )


# --- 대본을 바꾸지 않는다 -----------------------------------------------------


@pytest.mark.parametrize("slug", REAL_SLUGS)
def test_only_the_new_field_is_added(paths, install, slug):
    script = install(slug)
    llm = FakeLLMClient([answer_for(script, "close")])
    run_backfill_scale_stage(slug, llm=llm, paths=paths)

    after = json.loads(
        (paths.topic_dir(slug) / SCRIPT_FILE).read_text(encoding="utf-8")
    )
    assert len(after["scenes"]) == len(script["scenes"])
    for before, now in zip(script["scenes"], after["scenes"]):
        assert now.pop(ADDED_FIELD) == "close"
        assert now == before


def test_the_script_text_survives_verbatim(paths, install):
    script = install(PISA)
    llm = FakeLLMClient([answer_for(script, "diagram")])
    run_backfill_scale_stage(PISA, llm=llm, paths=paths)

    after = json.loads((paths.topic_dir(PISA) / SCRIPT_FILE).read_text(encoding="utf-8"))
    assert [s["text"] for s in after["scenes"]] == [s["text"] for s in script["scenes"]]
    assert after["total_duration"] == script["total_duration"]
    assert after["run_id"] == script["run_id"]


def test_a_session_that_rewrites_the_script_is_caught():
    """세션이 대본을 고쳐 보내도 파일에 닿지 못한다."""
    before = load_script(PISA)
    after = json.loads(json.dumps(before))
    after["scenes"][0]["text"] = "다른 문장입니다."
    with pytest.raises(BackfillStageError, match="다른 필드가 달라졌다"):
        assert_only_scale_changed(before, after)


def test_a_dropped_scene_is_caught():
    before = load_script(PISA)
    after = json.loads(json.dumps(before))
    del after["scenes"][3]
    with pytest.raises(BackfillStageError, match="씬 수가"):
        assert_only_scale_changed(before, after)


def test_a_changed_envelope_is_caught():
    before = load_script(PISA)
    after = json.loads(json.dumps(before))
    after["total_duration"] = 1.0
    with pytest.raises(BackfillStageError, match="total_duration"):
        assert_only_scale_changed(before, after)


# --- 세션 출력 파싱 -----------------------------------------------------------


def test_field_lands_right_after_subject(paths):
    """스펙 02의 씬 스키마 순서를 지킨다 — diff가 읽히게."""
    script = load_script(PISA)
    scales = {s["scene_id"]: "close" for s in script["scenes"]}
    keys = list(apply_scales(script, scales)["scenes"][0])
    assert keys[keys.index("subject") + 1] == ADDED_FIELD


def test_missing_scenes_are_rejected():
    script = load_script(PISA)
    payload = {"scales": [{"scene_id": 1, ADDED_FIELD: "wide"}]}
    with pytest.raises(BackfillStageError, match="판정이 빠진 씬"):
        parse_scales(payload, script["scenes"])


def test_unknown_scene_id_is_rejected():
    script = load_script(PISA)
    payload = {"scales": [{"scene_id": 999, ADDED_FIELD: "wide"}]}
    with pytest.raises(BackfillStageError, match="대본에 없다"):
        parse_scales(payload, script["scenes"])


def test_duplicate_scene_id_is_rejected():
    script = load_script(PISA)
    payload = {
        "scales": [
            {"scene_id": 1, ADDED_FIELD: "wide"},
            {"scene_id": 1, ADDED_FIELD: "close"},
        ]
    }
    with pytest.raises(BackfillStageError, match="두 번 나왔다"):
        parse_scales(payload, script["scenes"])


@pytest.mark.parametrize("bad", ["medium", "WIDE", "", None])
def test_value_outside_the_enum_is_rejected(bad):
    script = load_script(PISA)
    payload = {"scales": [{"scene_id": s["scene_id"], ADDED_FIELD: bad}
                          for s in script["scenes"]]}
    with pytest.raises(BackfillStageError, match=ADDED_FIELD):
        parse_scales(payload, script["scenes"])


def test_output_without_the_array_is_rejected():
    script = load_script(PISA)
    with pytest.raises(BackfillStageError, match="scales 배열"):
        parse_scales({"result": "ok"}, script["scenes"])


def test_non_json_output_is_rejected(paths, install):
    install(PISA)
    llm = FakeLLMClient(["판정을 마쳤습니다."])
    with pytest.raises(BackfillStageError, match="JSON"):
        run_backfill_scale_stage(PISA, llm=llm, paths=paths)


# --- 세션에 주는 것 -----------------------------------------------------------


def test_session_sees_subjects_but_not_the_script_text(paths, install):
    """분류에 필요한 것은 피사체뿐이다. 대본 본문을 보여주면 고치고 싶어진다."""
    script = install(PISA)
    llm = FakeLLMClient([answer_for(script)])
    run_backfill_scale_stage(PISA, llm=llm, paths=paths)

    prompt = llm.calls[0]["prompt"]
    assert script["scenes"][0]["subject"] in prompt
    assert script["scenes"][0]["text"] not in prompt
    assert llm.calls[0]["allowed_tools"] == ()


def test_rendered_scenes_carry_every_id():
    script = load_script(HOOVER)
    rendered = render_scenes(script["scenes"])
    for scene in script["scenes"]:
        assert f"scene_id {scene['scene_id']}:" in rendered


# --- 재실행 -------------------------------------------------------------------


def test_already_backfilled_script_does_not_call_the_session(paths, install):
    install(PISA, script=load_script(PISA))  # 실물 = 이미 채워져 있다
    llm = FakeLLMClient([])
    result = run_backfill_scale_stage(PISA, llm=llm, paths=paths)

    assert result.skipped
    assert llm.calls == []


def test_force_reclassifies(paths, install):
    script = install(PISA, script=load_script(PISA))
    llm = FakeLLMClient([answer_for(script, "diagram")])
    result = run_backfill_scale_stage(PISA, llm=llm, paths=paths, force=True)

    assert not result.skipped
    assert result.scale_counts == {"diagram": len(script["scenes"])}


def test_result_reports_the_distribution(paths, install):
    script = install(PISA)
    llm = FakeLLMClient([answer_for(script, "wide")])
    result = run_backfill_scale_stage(PISA, llm=llm, paths=paths)
    assert result.scale_counts == {"wide": len(script["scenes"])}
    assert str(len(script["scenes"])) in result.summary


# --- 산출물이 계약을 만족한다 -------------------------------------------------


@pytest.mark.parametrize("slug", REAL_SLUGS)
def test_backfilled_script_passes_the_scene_contract(paths, install, slug):
    script = install(slug)
    llm = FakeLLMClient([answer_for(script, "wide")])
    run_backfill_scale_stage(slug, llm=llm, paths=paths)

    after = json.loads(
        (paths.topic_dir(slug) / SCRIPT_FILE).read_text(encoding="utf-8")
    )
    assert validate_scenes(after)[0] == []


def test_stripped_script_fails_the_contract_before_backfill():
    """이 단계가 필요한 이유 — 옛 대본은 지금 계약을 통과하지 못한다."""
    errors, _ = validate_scenes(stripped_script(PISA))
    assert any(ADDED_FIELD in e for e in errors)


def test_missing_script_is_a_clear_error(paths):
    with pytest.raises(BackfillStageError, match=SCRIPT_FILE):
        run_backfill_scale_stage("없는-슬러그", llm=FakeLLMClient([]), paths=paths)
