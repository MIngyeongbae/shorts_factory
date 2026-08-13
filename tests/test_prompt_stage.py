"""[5. prompt] — 씬 계약 → prompts.json.

픽스처는 검증을 통과한 실물 대본 2개다 (ADR-0017: "픽스처가 실물이라 별도 제작이
필요 없다"). 성격이 달라서 스펙 03 룰의 편향이 드러난다.
- 피사의 사탑: 이탈리아 석조탑, 야외 25씬
- 후버댐: 미국 콘크리트 댐, 단면·내부 묘사가 많은 27씬

확인 대상:
- ADR-0017 경계 — 입력은 06-script.json 하나, 읽기 전용, 산출물은 runs/{run_id}/ 아래
- ADR-0033 — 연출은 씬 계약에서 온다. 비었을 때만 기본값으로 떨어지고 그 수를 센다
- ADR-0018 — 기본값으로 떨어질 때도 씬의 subject_scale과 어긋나지 않는다
- ADR-0019 — 레이어 A 폐기. 베이스 이미지는 전 씬 클린이다
"""

import json
from pathlib import Path

import pytest

from conftest import PISA, install_script, load_script

from shorts_factory.config import write_text
from shorts_factory.jsonio import dump_json
from shorts_factory.schemas import vocab
from shorts_factory.schemas.visual_rules import FRAMINGS, schema_errors
from shorts_factory.stages.prompt import (
    PROMPTS_FILE,
    SCRIPT_FILE,
    PromptStageError,
    build_prompts,
    run_prompt_stage,
)

REPO = Path(__file__).resolve().parents[1]

PISA = "pisaui-satap-jiban-bogang"
HOOVER = "hubeodaem-konkeuriteu-naenggak"
REAL_SLUGS = (PISA, HOOVER)


def real_script(slug: str) -> dict:
    """실물 대본 사본. 없으면 skip (1부가 만든 토픽 패키지에 딸린 파일이다)."""
    path = REPO / "topics" / slug / SCRIPT_FILE
    if not path.exists():
        pytest.skip(f"{path}가 없다 — 1부 토픽 패키지가 있어야 돈다")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def install(paths):
    """실물 대본을 격리된 루트의 topics/{slug}/에 놓고 (수정본도 가능) 돌려준다."""

    def _install(slug: str, mutate=None) -> dict:
        script = real_script(slug)
        if mutate:
            mutate(script)
        write_text(paths.topic_dir(slug) / SCRIPT_FILE, dump_json(script))
        return script

    return _install


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


# --- 실물 픽스처 2개 ---------------------------------------------------------


@pytest.mark.parametrize("slug", REAL_SLUGS)
def test_real_script_produces_one_prompt_per_scene(paths, install, slug):
    script = install(slug)
    result = run_prompt_stage(slug, paths=paths)

    assert result.prompts_path == paths.run_dir(script["run_id"]) / PROMPTS_FILE
    assert result.prompts_path.exists()
    assert [s["scene_id"] for s in result.prompts["scenes"]] == [
        s["scene_id"] for s in script["scenes"]
    ]


@pytest.mark.parametrize("slug", REAL_SLUGS)
def test_output_satisfies_its_own_schema(paths, install, slug):
    install(slug)
    result = run_prompt_stage(slug, paths=paths)
    assert schema_errors(result.prompts) == []


@pytest.mark.parametrize("slug", REAL_SLUGS)
def test_every_scene_prompt_carries_its_subject(paths, install, slug):
    script = install(slug)
    result = run_prompt_stage(slug, paths=paths)
    for scene, entry in zip(script["scenes"], result.prompts["scenes"]):
        assert scene["subject"] in entry["prompt"]


@pytest.mark.parametrize("slug", REAL_SLUGS)
def test_emphasis_value_reaches_a_layer_b_overlay(paths, install, slug):
    """대형 숫자는 후처리 합성이다 (ADR-0002). 값이 계약에 실려 [8]까지 가야 한다."""
    script = install(slug)
    result = run_prompt_stage(slug, paths=paths)

    for scene, entry in zip(script["scenes"], result.prompts["scenes"]):
        emphasis = scene.get("emphasis")
        if not emphasis:
            continue
        match = [o for o in entry["overlays"] if o["type"] == emphasis["type"]]
        assert match, f"씬 {scene['scene_id']}: emphasis가 오버레이로 옮겨지지 않았다"
        assert match[0]["value"] == emphasis["value"]
        assert match[0]["layer"] == "B"


@pytest.mark.parametrize("slug", REAL_SLUGS)
def test_same_input_gives_the_same_bytes(paths, install, slug):
    """산출물에 타임스탬프가 없다 — 재실행 결과를 diff로 비교할 수 있어야 한다."""
    install(slug)
    first = run_prompt_stage(slug, paths=paths).prompts_path.read_bytes()
    second = run_prompt_stage(slug, paths=paths, force=True).prompts_path.read_bytes()
    assert first == second


# --- 1부 ↔ 2부 경계 (ADR-0017) -----------------------------------------------


def test_topics_tree_is_untouched(paths, install):
    install(PISA)
    before = snapshot(paths.topics)
    run_prompt_stage(PISA, paths=paths)
    assert snapshot(paths.topics) == before


def test_output_lands_in_the_run_dir_named_by_the_script(paths, install):
    """run_id는 대본이 들고 있다. run 디렉터리가 없어도 만들어 쓴다 (계보 = run_id)."""
    script = install(PISA, mutate=lambda s: s.update(run_id="20991231-somewhere-else"))
    assert not paths.run_dir("20991231-somewhere-else").exists()

    result = run_prompt_stage(PISA, paths=paths)

    assert result.run_id == "20991231-somewhere-else"
    assert result.prompts["run_id"] == script["run_id"]
    assert result.prompts["source_script"] == f"topics/{PISA}/{SCRIPT_FILE}"


def test_missing_script_is_a_clear_error(paths):
    with pytest.raises(PromptStageError, match=SCRIPT_FILE):
        run_prompt_stage("없는-슬러그", paths=paths)


def test_broken_scene_contract_stops_before_writing(paths, install):
    """깨진 계약으로 만든 프롬프트는 [6]에서 돈만 쓰고 실패한다."""

    def break_order(script):
        script["scenes"][0]["scene_id"] = 99

    script = install(PISA, mutate=break_order)
    with pytest.raises(PromptStageError, match="씬 계약"):
        run_prompt_stage(PISA, paths=paths)
    assert not (paths.run_dir(script["run_id"]) / PROMPTS_FILE).exists()


# --- 상태 / 재실행 -----------------------------------------------------------


def test_rerun_skips_and_force_rebuilds(paths, install):
    install(PISA)
    first = run_prompt_stage(PISA, paths=paths)
    assert not first.skipped

    again = run_prompt_stage(PISA, paths=paths)
    assert again.skipped and again.prompts == first.prompts

    forced = run_prompt_stage(PISA, paths=paths, force=True)
    assert not forced.skipped


def test_state_records_the_stage(paths, install):
    script = install(PISA)
    run_prompt_stage(PISA, paths=paths)
    state = json.loads(
        (paths.run_dir(script["run_id"]) / "state.json").read_text(encoding="utf-8")
    )
    entry = state["stages"]["5-prompt"]
    assert entry["status"] == "done"
    assert entry["output"] == f"runs/{script['run_id']}/{PROMPTS_FILE}"
    assert entry["scenes"] == len(script["scenes"])


# --- 룰 적용 -----------------------------------------------------------------


def test_framing_falls_back_when_the_script_did_not_choose(paths, install):
    """옛 대본에는 `framing`이 없다. 막지 않고 기본값으로 떨어뜨린다 (D-3)."""
    script = install(PISA)
    assert all("framing" not in s for s in script["scenes"])

    result = run_prompt_stage(PISA, paths=paths)
    scenes = result.prompts["scenes"]

    assert all(s["framing_source"] == "beat_default" for s in scenes)
    assert result.default_framed_scenes == len(scenes)


def test_the_scene_choice_is_carried_through_untouched(paths, install):
    """연출을 고르는 것은 `[1s]`다. `[5]`는 그 값을 옮기기만 한다 (ADR-0033 §3)."""

    def choose(script):
        script["scenes"][0]["framing"] = "cross_section"

    install(PISA, mutate=choose)
    result = run_prompt_stage(PISA, paths=paths)
    first = result.prompts["scenes"][0]

    assert (first["framing"], first["framing_source"]) == ("cross_section", "scene")
    assert FRAMINGS["cross_section"].shot in first["prompt"]
    assert result.default_framed_scenes == len(result.prompts["scenes"]) - 1


@pytest.mark.parametrize("slug", REAL_SLUGS)
def test_no_scene_asks_for_a_baked_annotation(paths, install, slug):
    """ADR-0019 — 레이어 A 폐기. 모든 베이스 이미지는 클린이다."""
    install(slug)
    result = run_prompt_stage(slug, paths=paths)

    for entry in result.prompts["scenes"]:
        assert "annotation_prompt" not in entry
        assert all(o["layer"] == "B" for o in entry["overlays"])


@pytest.mark.parametrize("slug", REAL_SLUGS)
def test_base_image_never_carries_subtitles_or_particles(paths, install, slug):
    """방언이 바뀌어도 배제 대상은 같다 — 표기만 다르다 (ADR-0027)."""
    install(slug)
    for dialect, subtitles in (("nb2", "burned-in subtitles or caption bars"),
                               ("mj", "burned-in subtitles")):
        result = run_prompt_stage(slug, paths=paths, dialect=dialect, force=True)
        for entry in result.prompts["scenes"]:
            assert subtitles in entry["negative_prompt"]
            assert "sparkle particle overlay" in entry["negative_prompt"]


def test_turning_point_has_no_overlay(paths, install):
    """ADR-0019 — '빨간 크레용 X → 사라짐'은 룰 테이블에서 사라졌다."""
    install(PISA)
    scenes = run_prompt_stage(PISA, paths=paths).prompts["scenes"]
    turning = next(s for s in scenes if s["beat"] == "turning_point")
    assert turning["overlays"] == []


def test_kling_scene_needs_no_special_handling(paths, install):
    """ADR-0006의 클린 입력 조항은 대상이 사라졌다 — 전 씬이 이미 클린이다.

    motion 말고는 아무것도 달라지지 않아야 한다. 예전에는 레이어 A가 B로 옮겨가고
    어노테이션 2-pass가 빠지면서 씬 항목이 통째로 바뀌었다.
    """
    install(PISA)
    before = run_prompt_stage(PISA, paths=paths).prompts["scenes"][3]

    def make_kling(script):
        script["scenes"][3]["motion"] = "kling"

    install(PISA, mutate=make_kling)
    after = run_prompt_stage(PISA, paths=paths, force=True).prompts["scenes"][3]

    assert before["motion"] == "kenburns"
    assert after["motion"] == "kling"
    assert {k: v for k, v in before.items() if k != "motion"} == {
        k: v for k, v in after.items() if k != "motion"
    }


def test_unknown_emphasis_type_never_reaches_the_prompt(paths, install):
    """스펙 03에 없는 오버레이 타입은 씬 계약 단계에서 걸린다 (ADR-0019로 enum이 생겼다)."""

    def odd_emphasis(script):
        script["scenes"][0]["emphasis"] = {"type": "blue_circle", "value": "?"}

    script = install(PISA, mutate=odd_emphasis)
    with pytest.raises(PromptStageError, match="씬 계약"):
        run_prompt_stage(PISA, paths=paths)
    assert not (paths.run_dir(script["run_id"]) / PROMPTS_FILE).exists()


def test_stage_still_refuses_to_invent_an_overlay(paths, install):
    """계약을 우회해 들어와도 연출을 지어내지 않는다 (ADR-0001). 계약 검사 뒤의 이중 방어다."""
    script = real_script(PISA)
    script["scenes"][0]["emphasis"] = {"type": "blue_circle", "value": "?"}
    with pytest.raises(PromptStageError, match="blue_circle"):
        build_prompts(script, source_script="x")


def test_camera_off_the_beat_default_is_not_a_warning(paths, install):
    """카메라도 `[1s]`가 어휘에서 고른다 (ADR-0033 §3).

    예전에는 비트 기본값과 다르면 경고했다. 기본값이 지시가 아니게 된 뒤로 그 경고는
    "고를 수 있는 값을 골랐다"는 말이라 편마다 뜨기만 한다.
    """

    def odd_camera(script):
        script["scenes"][0]["camera"] = "pan_left"  # hook_fact 기본값은 slow_zoom_in

    install(PISA, mutate=odd_camera)
    result = run_prompt_stage(PISA, paths=paths)

    assert result.prompts["scenes"][0]["camera"] == "pan_left"
    assert all("camera" not in w for w in result.warnings)


def test_missing_style_anchors_warn(paths, install):
    """앵커가 없으면 [6]이 룩 일관성 수단 없이 돈다 (ADR-0005)."""
    install(PISA)
    result = run_prompt_stage(PISA, paths=paths, dialect="nb2")
    assert any("style_anchors" in w for w in result.warnings)


def test_mj_dialect_does_not_warn_about_anchors(paths, install):
    """MJ 경로는 앵커를 안 쓴다 (ADR-0025 G3) — 고칠 것이 없는 경고를 띄우지 않는다."""
    install(PISA)
    result = run_prompt_stage(PISA, paths=paths, dialect="mj")
    assert not any("style_anchors" in w for w in result.warnings)


# --- 구도가 피사체를 따라간다 (ADR-0018) -------------------------------------


@pytest.mark.parametrize("slug", REAL_SLUGS)
def test_fallback_framing_follows_the_scale_the_script_declared(paths, install, slug):
    """기본값으로 떨어질 때도 씬의 스케일과 어긋날 수 없다 (ADR-0018)."""
    install(slug)
    result = run_prompt_stage(slug, paths=paths)

    for entry in result.prompts["scenes"]:
        if entry["framing_source"] != "beat_default":
            continue
        expected = vocab.default_framing(entry["beat"], entry["subject_scale"])
        assert entry["framing"] == expected


def test_close_and_diagram_subjects_no_longer_get_a_drone_shot(paths, install):
    """이 축을 도입한 이유다 — 후버댐 1번 '콘크리트 단면 속 강철 파이프'가 대표 사례."""
    install(HOOVER)
    scenes = run_prompt_stage(HOOVER, paths=paths).prompts["scenes"]

    wide_only = {"drone_wide", "aerial_diorama", "problem_wide", "present_wide"}
    for entry in scenes:
        if entry["subject_scale"] in ("close", "diagram"):
            assert entry["framing"] not in wide_only

    assert scenes[0]["subject_scale"] == "close"
    assert scenes[0]["framing"] == "subject_closeup"


@pytest.mark.parametrize("slug", REAL_SLUGS)
def test_every_scale_actually_gets_used(paths, install, slug):
    """ADR-0018 되돌릴 조건의 관측 지점 — 한 값으로 쏠리면 축이 판별력이 없다."""
    install(slug)
    result = run_prompt_stage(slug, paths=paths)
    assert set(result.scale_counts) == {"wide", "close", "diagram"}


def test_prompt_tells_the_model_what_the_image_must_explain(paths):
    """visual_goal이 프롬프트로 실린다 (ADR-0022).

    피사체만 주면 모델은 그것을 그릴 뿐이고, 그 그림이 왜 거기 있는지는 모른다.
    """
    install_script(paths, PISA)
    script = load_script(PISA)

    # 방언과 무관하게 실린다. 라벨(`must explain`)은 NB2 문법이고, MJ는 라벨 없이
    # 콤마 항목으로 넣는다 (ADR-0027).
    for dialect in ("nb2", "mj"):
        result = run_prompt_stage(PISA, paths=paths, dialect=dialect, force=True)
        doc = json.loads(result.prompts_path.read_text(encoding="utf-8"))
        for scene, source in zip(doc["scenes"], script["scenes"]):
            assert source["visual_goal"] in scene["prompt"]
        if dialect == "nb2":
            assert all("must explain" in s["prompt"] for s in doc["scenes"])
        else:
            assert not any("must explain" in s["prompt"] for s in doc["scenes"])


def test_prompt_omits_the_line_when_the_field_is_absent():
    """필드가 없던 옛 대본도 있다. 빈 지시문을 남기지 않는다."""
    from shorts_factory.schemas.visual_rules import build_prompt

    assert "must explain" not in build_prompt("shot", "피사체")
    assert "must explain" not in build_prompt("shot", "피사체", "   ")
