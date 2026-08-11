"""[1y. backfill-visual-goal] — 확정 대본에 visual_goal만 채운다 (ADR-0022).

이 단계가 낼 수 있는 최악의 결과는 **승인된 대본을 조용히 바꾸는 것**이다. 그래서
테스트의 무게는 "채웠는가"가 아니라 "그 밖의 것을 안 건드렸는가"에 실린다.
"""

import json

import pytest

from conftest import PISA, install_script, load_script
from shorts_factory.llm.fake import FakeLLMClient
from shorts_factory.stages.backfill_scale import BackfillStageError
from shorts_factory.stages.backfill_visual_goal import (
    ADDED_FIELD,
    apply_goals,
    parse_goals,
    render_scenes,
    run_backfill_visual_goal_stage,
)


def goals_for(scenes, *, goal="자막이 말하지 않고 넘어가는 규모감"):
    return {"goals": [{"scene_id": s["scene_id"], ADDED_FIELD: goal} for s in scenes]}


def stripped_script(slug=PISA):
    """visual_goal이 없던 시절의 대본."""
    doc = load_script(slug)
    for scene in doc["scenes"]:
        scene.pop(ADDED_FIELD, None)
    return doc


def run(paths, payload, *, slug=PISA, force=False):
    return run_backfill_visual_goal_stage(
        slug, llm=FakeLLMClient([json.dumps(payload, ensure_ascii=False)]),
        paths=paths, force=force,
    )


@pytest.fixture
def prepared(paths):
    """visual_goal이 빠진 대본을 격리된 루트에 놓는다."""
    def _prepare(slug=PISA):
        install_script(paths, slug)
        path = paths.topic_dir(slug) / "06-script.json"
        doc = stripped_script(slug)
        path.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return path, doc
    return _prepare


# --- 세션에 보여주는 것 --------------------------------------------------------


def test_session_sees_the_subtitle_and_the_subject():
    """[1x]와 다르다 — 본문이 무엇을 말하는지 알아야 그림이 질 몫이 나온다."""
    scenes = load_script(PISA)["scenes"][:2]
    rendered = render_scenes(scenes)

    assert scenes[0]["text"] in rendered
    assert scenes[0]["subject"] in rendered


# --- 세션 출력 파싱 ------------------------------------------------------------


def test_missing_scene_is_refused():
    scenes = load_script(PISA)["scenes"]
    payload = goals_for(scenes)
    payload["goals"].pop()

    with pytest.raises(BackfillStageError, match="빠진 씬"):
        parse_goals(payload, scenes)


def test_unknown_scene_id_is_refused():
    scenes = load_script(PISA)["scenes"]
    payload = goals_for(scenes)
    payload["goals"][0]["scene_id"] = 999

    with pytest.raises(BackfillStageError, match="대본에 없다"):
        parse_goals(payload, scenes)


def test_empty_goal_is_refused():
    scenes = load_script(PISA)["scenes"]
    payload = goals_for(scenes)
    payload["goals"][0][ADDED_FIELD] = "   "

    with pytest.raises(BackfillStageError, match="비어 있다"):
        parse_goals(payload, scenes)


def test_goal_that_restates_the_subtitle_is_refused():
    """자막을 바꿔 쓴 값은 그림이 지는 설명이 아니다 (ADR-0022)."""
    scenes = load_script(PISA)["scenes"]
    payload = goals_for(scenes)
    payload["goals"][0][ADDED_FIELD] = scenes[0]["text"]

    with pytest.raises(BackfillStageError, match="겹친다"):
        parse_goals(payload, scenes)


# --- 끼우는 자리 ---------------------------------------------------------------


def test_goal_lands_right_before_the_subject():
    """목표가 먼저고 피사체가 그다음이다 (ADR-0022). 파일에서도 그 순서로 읽힌다."""
    script = stripped_script()
    updated = apply_goals(script, {s["scene_id"]: "설명" for s in script["scenes"]})

    keys = list(updated["scenes"][0])
    assert keys.index(ADDED_FIELD) == keys.index("subject") - 1


# --- 대본을 건드리지 않는다 -----------------------------------------------------


def test_only_the_new_field_is_added(paths, prepared):
    path, before = prepared()
    run(paths, goals_for(before["scenes"]))

    after = json.loads(path.read_text(encoding="utf-8"))
    assert len(after["scenes"]) == len(before["scenes"])
    for old, new in zip(before["scenes"], after["scenes"]):
        assert new[ADDED_FIELD]
        assert {k: v for k, v in new.items() if k != ADDED_FIELD} == old


def test_a_changed_subtitle_stops_the_write(paths, prepared):
    """세션이 대본을 고쳐 보내도 파일은 그대로여야 한다."""
    path, before = prepared()
    original = path.read_text(encoding="utf-8")

    payload = goals_for(before["scenes"])
    payload["goals"][0]["text"] = "세션이 몰래 바꾼 자막"  # 스키마 밖 키는 무시된다

    run(paths, payload)
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["scenes"][0]["text"] == before["scenes"][0]["text"]
    assert original != path.read_text(encoding="utf-8"), "필드는 실제로 채워졌다"


def test_second_run_skips_without_calling_the_session(paths, prepared):
    path, before = prepared()
    run(paths, goals_for(before["scenes"]))

    # 세션 응답을 주지 않는다 — 부르면 그 자리에서 터진다
    result = run_backfill_visual_goal_stage(
        PISA, llm=FakeLLMClient([]), paths=paths,
    )
    assert result.skipped
    assert ADDED_FIELD in json.loads(path.read_text(encoding="utf-8"))["scenes"][0]


def test_result_reports_the_scene_count(paths, prepared):
    _path, before = prepared()
    result = run(paths, goals_for(before["scenes"]))

    assert result.scene_count == len(before["scenes"])
    assert str(len(before["scenes"])) in result.summary
