"""[5. prompt] — 씬 계약 → prompts.json.

픽스처는 검증을 통과한 실물 대본 2개다 (ADR-0017: "픽스처가 실물이라 별도 제작이
필요 없다"). 성격이 달라서 스펙 03 룰의 편향이 드러난다.
- 피사의 사탑: 이탈리아 석조탑, 야외 25씬
- 후버댐: 미국 콘크리트 댐, 단면·내부 묘사가 많은 27씬

확인 대상:
- ADR-0017 경계 — 입력은 06-script.json 하나, 읽기 전용, 산출물은 runs/{run_id}/ 아래
- ADR-0001 — 연출은 룰 테이블에서만 나온다 (룰에 없는 값이 오면 멈춘다)
- ADR-0018 — 구도는 (beat × subject_scale)에서 나오고, 스케일이 다르면 구도를 잇지 않는다
- ADR-0019 — 레이어 A 폐기. 베이스 이미지는 전 씬 클린이다
"""

import json
from pathlib import Path

import pytest

from shorts_factory.config import write_text
from shorts_factory.jsonio import dump_json
from shorts_factory.schemas.visual_rules import FRAMING_TABLE, schema_errors
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


def test_hook_twist_holds_the_previous_framing(paths, install):
    """스펙 03 '전경 유지' — 앞 씬과 스케일이 같을 때만 구도를 잇는다 (ADR-0018).

    피사 3번 씬이 이 경우다: 2번(hook_fact, wide) → 3번(hook_twist, wide).
    """
    script = install(PISA)
    scenes = run_prompt_stage(PISA, paths=paths).prompts["scenes"]
    twist = next(
        s for s in scenes
        if s["beat"] == "hook_twist" and s["framing_source"] == "prev_scene"
    )
    previous = scenes[twist["scene_id"] - 2]

    assert twist["framing"] == previous["framing"]
    assert twist["subject_scale"] == previous["subject_scale"]
    assert twist["framing_reuse_of"] == previous["scene_id"]
    assert script["scenes"][twist["scene_id"] - 1]["subject"] in twist["prompt"]


def test_framing_is_not_inherited_across_a_different_scale(paths, install):
    """후버댐 2번 씬 — 1번이 diagram, 2번이 wide다. 이으면 단면 구도를 전경에 쓴다."""
    install(HOOVER)
    scenes = run_prompt_stage(HOOVER, paths=paths).prompts["scenes"]
    first, second = scenes[0], scenes[1]

    assert first["subject_scale"] != second["subject_scale"]
    assert second["framing_source"] == "scale_fallback"
    assert second["framing"] != first["framing"]
    assert "framing_reuse_of" not in second


def test_ending_echo_reuses_the_hook_framing_when_the_scale_matches(paths, install):
    install(PISA)
    scenes = run_prompt_stage(PISA, paths=paths).prompts["scenes"]
    hook = next(s for s in scenes if s["beat"] == "hook_fact")
    echo = next(s for s in scenes if s["beat"] == "ending_echo")

    assert echo["subject_scale"] == hook["subject_scale"]
    assert echo["framing"] == hook["framing"]
    assert echo["framing_source"] == "hook_echo"
    assert echo["framing_reuse_of"] == hook["scene_id"]


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
    install(slug)
    result = run_prompt_stage(slug, paths=paths)
    for entry in result.prompts["scenes"]:
        assert "burned-in subtitles or caption bars" in entry["negative_prompt"]
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


def test_camera_off_the_rule_default_warns_but_proceeds(paths, install):
    """camera는 씬 계약이 정한 값을 쓴다 (ADR-0014). 룰 기본값과 다르면 알리기만 한다."""

    def odd_camera(script):
        script["scenes"][0]["camera"] = "pan_left"  # hook_fact 기본값은 slow_zoom_in

    install(PISA, mutate=odd_camera)
    result = run_prompt_stage(PISA, paths=paths)

    assert result.prompts["scenes"][0]["camera"] == "pan_left"
    assert any("씬 1" in w and "slow_zoom_in" in w for w in result.warnings)


def test_missing_style_anchors_warn(paths, install):
    """앵커가 없으면 [6]이 룩 일관성 수단 없이 돈다 (ADR-0005)."""
    install(PISA)
    result = run_prompt_stage(PISA, paths=paths)
    assert any("style_anchors" in w for w in result.warnings)


# --- 구도가 피사체를 따라간다 (ADR-0018) -------------------------------------


@pytest.mark.parametrize("slug", REAL_SLUGS)
def test_framing_comes_from_the_scale_the_script_declared(paths, install, slug):
    """구도는 (beat × subject_scale) 표에서만 나온다. 씬의 스케일과 어긋날 수 없다."""
    install(slug)
    result = run_prompt_stage(slug, paths=paths)

    for entry in result.prompts["scenes"]:
        expected = FRAMING_TABLE[entry["beat"]][entry["subject_scale"]]
        if entry["framing_source"] == "beat_rule":
            assert entry["framing"] == expected
        else:
            # 참조를 푼 경우에도 표가 참조를 지시한 칸이어야 한다
            assert expected in ("@prev", "@hook")


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
