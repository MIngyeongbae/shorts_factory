"""[5. prompt] — 씬 계약 → prompts.json.

픽스처는 검증을 통과한 실물 대본 2개다 (ADR-0017: "픽스처가 실물이라 별도 제작이
필요 없다"). 성격이 달라서 스펙 03 룰의 편향이 드러난다.
- 피사의 사탑: 이탈리아 석조탑, 야외 25씬
- 후버댐: 미국 콘크리트 댐, 단면·내부 묘사가 많은 27씬

확인 대상:
- ADR-0017 경계 — 입력은 06-script.json 하나, 읽기 전용, 산출물은 runs/{run_id}/ 아래
- ADR-0001 — 연출은 룰 테이블에서만 나온다 (룰에 없는 값이 오면 멈춘다)
- ADR-0002 — 레이어 A/B 분리
- ADR-0006 — kling 씬에는 클린 이미지
"""

import json
from pathlib import Path

import pytest

from shorts_factory.config import write_text
from shorts_factory.jsonio import dump_json
from shorts_factory.schemas.visual_rules import schema_errors
from shorts_factory.stages.prompt import (
    PROMPTS_FILE,
    SCRIPT_FILE,
    PromptStageError,
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
    """스펙 03 '전경 유지' — 구도만 잇는다. 피사체는 씬 계약이 정한 것을 쓴다."""
    script = install(PISA)
    scenes = run_prompt_stage(PISA, paths=paths).prompts["scenes"]
    twist = next(s for s in scenes if s["beat"] == "hook_twist")
    previous = scenes[twist["scene_id"] - 2]

    assert twist["framing"] == previous["framing"]
    assert twist["framing_source"] == "prev_scene"
    assert twist["framing_reuse_of"] == previous["scene_id"]
    assert script["scenes"][twist["scene_id"] - 1]["subject"] in twist["prompt"]


def test_ending_echo_reuses_the_hook_framing(paths, install):
    install(PISA)
    scenes = run_prompt_stage(PISA, paths=paths).prompts["scenes"]
    hook = next(s for s in scenes if s["beat"] == "hook_fact")
    echo = next(s for s in scenes if s["beat"] == "ending_echo")

    assert echo["framing"] == hook["framing"]
    assert echo["framing_source"] == "hook_echo"
    assert echo["framing_reuse_of"] == hook["scene_id"]


def test_turning_point_x_is_composited_not_baked(paths, install):
    """'X → 사라짐'은 시간 변화라 정지 이미지에 못 담는다. 레이어 B로 간다."""
    install(PISA)
    scenes = run_prompt_stage(PISA, paths=paths).prompts["scenes"]
    turning = next(s for s in scenes if s["beat"] == "turning_point")

    assert turning["overlays"] == [
        {"type": "red_crayon_x_fadeout", "layer": "B", "value": None}
    ]
    assert turning["annotation_prompt"] is None
    assert "red crayon X mark" in turning["negative_prompt"]


def test_kenburns_scene_with_layer_a_gets_a_second_pass(paths, install):
    install(PISA)
    scenes = run_prompt_stage(PISA, paths=paths).prompts["scenes"]
    context = next(s for s in scenes if s["beat"] == "context")

    assert context["motion"] == "kenburns"
    assert context["annotation_prompt"]
    assert [o["layer"] for o in context["overlays"]] == ["A"]


def test_kling_scene_gets_a_clean_image(paths, install):
    """ADR-0006: kling 입력은 어노테이션 없는 클린 이미지, 어노테이션은 클립 위로."""

    def make_kling(script):
        script["scenes"][3]["motion"] = "kling"  # context 씬 (빨간 측정선)

    install(PISA, mutate=make_kling)
    scenes = run_prompt_stage(PISA, paths=paths).prompts["scenes"]
    kling = scenes[3]

    assert kling["motion"] == "kling"
    assert kling["annotation_prompt"] is None
    assert kling["overlays"] == [
        {
            "type": "red_measure_line",
            "layer": "B",
            "value": None,
            "layer_note": "kling_clean_input",
        }
    ]
    assert "red measurement lines or outlined areas" in kling["negative_prompt"]


def test_unknown_emphasis_type_stops_the_stage(paths, install):
    """스펙 03에 없는 오버레이 타입이 오면 연출을 지어내지 않고 멈춘다 (ADR-0001)."""

    def odd_emphasis(script):
        script["scenes"][0]["emphasis"] = {"type": "blue_circle", "value": "?"}

    script = install(PISA, mutate=odd_emphasis)
    with pytest.raises(PromptStageError, match="blue_circle"):
        run_prompt_stage(PISA, paths=paths)

    # specs/05 실패 정책: 실패는 run 디렉터리에 기록하고 종료한다
    state = json.loads(
        (paths.run_dir(script["run_id"]) / "state.json").read_text(encoding="utf-8")
    )
    assert state["stages"]["5-prompt"]["status"] == "failed"


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


# --- 룰 공백 보고 -------------------------------------------------------------


def test_rule_gaps_only_list_beats_this_script_actually_uses(paths, install):
    install(PISA)
    prompts = run_prompt_stage(PISA, paths=paths).prompts
    beats = {s["beat"] for s in prompts["scenes"]}
    codes = {gap["code"] for gap in prompts["rule_gaps"]}

    assert "turning_point_overlay_temporal" in codes
    assert all(gap["scene_ids"] for gap in prompts["rule_gaps"])
    if "hook_twist" not in beats:  # 방어: 픽스처가 바뀌어도 뜻이 유지되도록
        assert "hook_twist_framing_choice" not in codes


def test_hoover_hits_more_framing_conflicts_than_pisa(paths, install):
    """스펙 03 구도 열은 한국 건축 3편 실측 편향이 있다. 단면·내부 소재에서 더 부딪힌다."""

    def conflicts(slug):
        install(slug)
        prompts = run_prompt_stage(slug, paths=paths).prompts
        found = [
            gap for gap in prompts["rule_gaps"] if gap["code"] == "framing_subject_conflict"
        ]
        return found[0]["scene_ids"] if found else []

    assert len(conflicts(HOOVER)) > len(conflicts(PISA))
