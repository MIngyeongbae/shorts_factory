"""[5. prompt] — 씬 계약 → prompts.json (씬별 영상 프롬프트, ADR-0056).

픽스처는 검증을 통과한 실물 대본 2개다 (ADR-0017: "픽스처가 실물이라 별도 제작이
필요 없다"). 성격이 달라서 스펙 03 룰의 편향이 드러난다.
- 피사의 사탑: 이탈리아 석조탑, 야외 25씬
- 후버댐: 미국 콘크리트 댐, 단면·내부 묘사가 많은 28씬

확인 대상:
- ADR-0017 경계 — 입력은 씬 계약(scenes.json) 하나, 읽기 전용, 산출물은 runs/{run_id}/ 아래
- ADR-0033 — 연출은 씬 계약에서 온다. 비었을 때만 기본값으로 떨어지고 그 수를 센다
- ADR-0018 — 기본값으로 떨어질 때도 씬의 subject_scale과 어긋나지 않는다
- ADR-0056 — 골격(FORMAT·STAGING·SUBJECT·CAMERA·RED·NEGATIVE), RED는 info 씬만,
  길이는 쓰지 않는다, 방언은 없다
"""

import json
from pathlib import Path

import pytest

from conftest import PISA, install_script, load_script

from shorts_factory.config import write_text
from shorts_factory.jsonio import dump_json
from shorts_factory.schemas import vocab
from shorts_factory.schemas.visual_rules import (
    FRAMINGS,
    FROM_DEFAULT,
    FROM_SCENE,
    SECONDS_PLACEHOLDER,
    SECTIONS,
    schema_errors,
)
from shorts_factory.stages.prompt import (
    PROMPTS_FILE,
    PromptStageError,
    build_prompts,
    run_prompt_stage,
)

PISA = "pisaui-satap-jiban-bogang"
HOOVER = "hubeodaem-konkeuriteu-naenggak"
REAL_SLUGS = (PISA, HOOVER)

INFO = {"labels": ["221 m"], "target": "the full height of the tower", "annotation": "dimension"}


@pytest.fixture
def install(paths):
    """씬 계약(수정본도 가능)을 새 경로 `runs/{run_id}/scenes.json`에 놓는다 (ADR-0052)."""

    def _install(slug: str, mutate=None) -> dict:
        script = load_script(slug)
        if mutate:
            mutate(script)
        run_id = script["run_id"]
        run_dir = paths.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        write_text(
            run_dir / "topic.json",
            dump_json({"run_id": run_id, "slug": slug, "topic": script["topic"]}),
        )
        write_text(run_dir / "scenes.json", dump_json(script))
        return script

    return _install


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


def section_names(prompt: str) -> list[str]:
    return [line.split(":", 1)[0] for line in prompt.splitlines()]


def line_of(prompt: str, section: str) -> str:
    return next(l for l in prompt.splitlines() if l.startswith(f"{section}:"))


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
def test_every_scene_prompt_carries_its_subject_untranslated(paths, install, slug):
    script = install(slug)
    result = run_prompt_stage(slug, paths=paths)
    for scene, entry in zip(script["scenes"], result.prompts["scenes"]):
        assert scene["subject"] in line_of(entry["prompt"], "SUBJECT")


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
    """run_id는 씬 계약이 들고 있다 (계보 = run_id, ADR-0017)."""
    script = install(PISA, mutate=lambda s: s.update(run_id="20991231-somewhere-else"))

    result = run_prompt_stage(PISA, paths=paths)

    assert result.run_id == "20991231-somewhere-else"
    assert result.prompts["run_id"] == script["run_id"]
    assert (
        result.prompts["source_script"] == "runs/20991231-somewhere-else/scenes.json"
    )


def test_missing_script_is_a_clear_error(paths):
    with pytest.raises(PromptStageError, match="run이 없다"):
        run_prompt_stage("없는-슬러그", paths=paths)


def test_broken_scene_contract_stops_before_writing(paths, install):
    """깨진 계약으로 만든 프롬프트는 [7]에서 돈만 쓰고 실패한다."""

    def break_order(script):
        script["scenes"][0]["scene_id"] = 99

    script = install(PISA, mutate=break_order)
    with pytest.raises(PromptStageError, match="씬 계약"):
        run_prompt_stage(PISA, paths=paths)
    assert not (paths.run_dir(script["run_id"]) / PROMPTS_FILE).exists()


def test_non_ascii_label_in_the_contract_stops_before_writing(paths, install):
    """화면 텍스트는 ASCII만이다 (ADR-0056 결정 3) — 계약 검증이 [5] 앞에서 막는다."""

    def korean_label(script):
        script["scenes"][0]["info"] = {**INFO, "labels": ["높이 221m"]}

    script = install(PISA, mutate=korean_label)
    with pytest.raises(PromptStageError, match="labels"):
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


def test_state_records_the_stage_and_the_observations(paths, install):
    script = install(PISA, mutate=lambda s: s["scenes"][2].update(info=INFO))
    run_prompt_stage(PISA, paths=paths)
    state = json.loads(
        (paths.run_dir(script["run_id"]) / "state.json").read_text(encoding="utf-8")
    )
    entry = state["stages"]["5-prompt"]
    assert entry["status"] == "done"
    assert entry["output"] == f"runs/{script['run_id']}/{PROMPTS_FILE}"
    assert entry["scenes"] == len(script["scenes"])
    # ADR-0033·0056 되돌릴 조건의 관측값이 run 상태에 남는다.
    assert entry["info_scenes"] == 1
    assert entry["default_framed_scenes"] == len(script["scenes"])
    assert entry["default_staged_scenes"] == len(script["scenes"])
    assert "dialect" not in entry


# --- 골격 (스펙 03 「프롬프트 골격」) ----------------------------------------


@pytest.mark.parametrize("slug", REAL_SLUGS)
def test_every_prompt_follows_the_skeleton_order(paths, install, slug):
    install(slug)
    result = run_prompt_stage(slug, paths=paths)
    plain = [s for s in SECTIONS if s != "RED"]
    for entry in result.prompts["scenes"]:
        expected = list(SECTIONS) if entry["has_info"] else plain
        assert section_names(entry["prompt"]) == expected


@pytest.mark.parametrize("slug", REAL_SLUGS)
def test_prompts_carry_no_length_only_the_placeholder(paths, install, slug):
    """길이는 [7]이 실측에서 정한다 (스펙 05) — prompts.json은 시간 정보를 담지 않는다."""
    install(slug)
    result = run_prompt_stage(slug, paths=paths)
    for entry in result.prompts["scenes"]:
        assert SECONDS_PLACEHOLDER in line_of(entry["prompt"], "FORMAT")
        assert "clip_length" not in entry and "duration" not in entry
        assert not any(key.startswith("est_") for key in entry)


def test_red_section_only_on_info_scenes_and_labels_verbatim(paths, install):
    def with_info(script):
        script["scenes"][4]["info"] = {
            "labels": ["221 m", "3x"], "target": "the full height", "annotation": "dimension",
        }

    install(PISA, mutate=with_info)
    result = run_prompt_stage(PISA, paths=paths)
    scenes = result.prompts["scenes"]

    assert [s["scene_id"] for s in scenes if s["has_info"]] == [5]
    assert result.info_scenes == 1 and "인포 1씬" in result.summary
    red = line_of(scenes[4]["prompt"], "RED")
    assert '"221 m"' in red and '"3x"' in red
    assert "the full height" in red
    assert vocab.annotation_closing() in red
    for entry in scenes:
        if entry["scene_id"] != 5:
            assert "RED:" not in entry["prompt"]


def test_no_text_negative_only_without_info(paths, install):
    """info 씬은 RED 절의 마무리가 글자 금지를 대신한다 (vocab negatives._role)."""
    install(PISA, mutate=lambda s: s["scenes"][0].update(info=INFO))
    scenes = run_prompt_stage(PISA, paths=paths).prompts["scenes"]
    no_text = vocab.negatives("no_text")

    info_negative = line_of(scenes[0]["prompt"], "NEGATIVE").lower()
    plain_negative = line_of(scenes[1]["prompt"], "NEGATIVE").lower()
    for item in no_text:
        assert f"no {item}" not in info_negative
        assert f"no {item}" in plain_negative
    assert all(item not in scenes[0]["negative_prompt"].split(", ") for item in no_text)
    assert all(item in scenes[1]["negative_prompt"].split(", ") for item in no_text)


@pytest.mark.parametrize("slug", REAL_SLUGS)
def test_phrases_are_the_vocab_values(paths, install, slug):
    """코드가 영어 문장을 들고 있지 않다 (ADR-0034) — 절마다 어휘 값이 그대로 들어간다."""
    install(slug)
    result = run_prompt_stage(slug, paths=paths)
    for entry in result.prompts["scenes"]:
        prompt = entry["prompt"]
        assert vocab.style("base_style") in line_of(prompt, "FORMAT")
        assert line_of(prompt, "STAGING") == f"STAGING: {vocab.phrase('staging', entry['staging'])}"
        assert vocab.video_prompt(entry["camera"]) in line_of(prompt, "CAMERA")
        assert FRAMINGS[entry["framing"]].shot in line_of(prompt, "SUBJECT")
        assert line_of(prompt, "NEGATIVE").endswith(vocab.negatives("audio"))


def test_style_block_has_no_dialect_and_no_anchors(paths, install):
    """방언(ADR-0027)·스타일 앵커(ADR-0005)는 ADR-0056이 접었다."""
    install(PISA)
    style = run_prompt_stage(PISA, paths=paths).prompts["style"]
    assert set(style) == {"base_style", "composition", "aspect_ratio", "resolution"}
    assert style["base_style"] == vocab.style("base_style")


# --- 룰 적용 -----------------------------------------------------------------


def test_framing_falls_back_when_the_script_did_not_choose(paths, install):
    """옛 대본에는 `framing`이 없다. 막지 않고 기본값으로 떨어뜨린다 (D-3)."""
    script = install(PISA)
    assert all("framing" not in s for s in script["scenes"])

    result = run_prompt_stage(PISA, paths=paths)
    scenes = result.prompts["scenes"]

    assert all(s["framing_source"] == FROM_DEFAULT for s in scenes)
    assert result.default_framed_scenes == len(scenes)


def test_staging_falls_back_to_studio_and_is_counted(paths, install):
    """무대를 비우면 기본값(studio)이다 (ADR-0056 결정 4) — 그 씬 수를 요약에 낸다."""

    def choose_one(script):
        script["scenes"][0]["staging"] = "location"

    install(PISA, mutate=choose_one)
    result = run_prompt_stage(PISA, paths=paths)
    scenes = result.prompts["scenes"]

    assert (scenes[0]["staging"], scenes[0]["staging_source"]) == ("location", FROM_SCENE)
    assert all(s["staging"] == vocab.default_staging(s["beat"]) for s in scenes[1:])
    assert all(s["staging_source"] == FROM_DEFAULT for s in scenes[1:])
    assert result.default_staged_scenes == len(scenes) - 1
    assert f"무대 기본값 {len(scenes) - 1}씬" in result.summary


def test_the_scene_choice_is_carried_through_untouched(paths, install):
    """연출을 고르는 것은 `[3s]`다. `[5]`는 그 값을 옮기기만 한다 (ADR-0033 §3)."""

    def choose(script):
        script["scenes"][0]["framing"] = "cross_section"

    install(PISA, mutate=choose)
    result = run_prompt_stage(PISA, paths=paths)
    first = result.prompts["scenes"][0]

    assert (first["framing"], first["framing_source"]) == ("cross_section", FROM_SCENE)
    assert FRAMINGS["cross_section"].shot in first["prompt"]
    assert result.default_framed_scenes == len(result.prompts["scenes"]) - 1


def test_motion_in_the_contract_is_rejected(paths, install):
    """ADR-0056 — motion 필드가 사라졌다. 오면 계약 위반이다."""

    def old_motion(script):
        script["scenes"][3]["motion"] = "kenburns"

    script = install(PISA, mutate=old_motion)
    with pytest.raises(PromptStageError, match="씬 계약"):
        run_prompt_stage(PISA, paths=paths)
    assert not (paths.run_dir(script["run_id"]) / PROMPTS_FILE).exists()


def test_emphasis_in_the_contract_is_rejected(paths, install):
    """ADR-0054 — 오버레이 합성이 삭제돼 emphasis 자체가 계약 위반이다."""

    def odd_emphasis(script):
        script["scenes"][0]["emphasis"] = {"type": "big_red_text", "value": "?"}

    script = install(PISA, mutate=odd_emphasis)
    with pytest.raises(PromptStageError, match="씬 계약"):
        run_prompt_stage(PISA, paths=paths)
    assert not (paths.run_dir(script["run_id"]) / PROMPTS_FILE).exists()


def test_camera_off_the_beat_default_is_not_a_warning(paths, install):
    """카메라도 `[3s]`가 어휘에서 고른다 (ADR-0033 §3)."""

    def odd_camera(script):
        script["scenes"][0]["camera"] = "pan_left"  # hook_fact 기본값은 slow_zoom_in

    install(PISA, mutate=odd_camera)
    result = run_prompt_stage(PISA, paths=paths)

    assert result.prompts["scenes"][0]["camera"] == "pan_left"
    assert vocab.video_prompt("pan_left") in result.prompts["scenes"][0]["prompt"]
    assert all("camera" not in w for w in result.warnings)


# --- 구도가 피사체를 따라간다 (ADR-0018) -------------------------------------


@pytest.mark.parametrize("slug", REAL_SLUGS)
def test_fallback_framing_follows_the_scale_the_script_declared(paths, install, slug):
    """기본값으로 떨어질 때도 씬의 스케일과 어긋날 수 없다 (ADR-0018)."""
    install(slug)
    result = run_prompt_stage(slug, paths=paths)

    for entry in result.prompts["scenes"]:
        if entry["framing_source"] != FROM_DEFAULT:
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

    by_scale = {e["subject_scale"]: e["framing"] for e in scenes}
    assert set(by_scale) == {"wide", "close", "diagram"}
    assert len(set(by_scale.values())) == 3


@pytest.mark.parametrize("slug", REAL_SLUGS)
def test_every_scale_actually_gets_used(paths, install, slug):
    """ADR-0018 되돌릴 조건의 관측 지점 — 한 값으로 쏠리면 축이 판별력이 없다."""
    install(slug)
    result = run_prompt_stage(slug, paths=paths)
    assert set(result.scale_counts) == {"wide", "close", "diagram"}


def test_visual_goal_is_not_in_the_video_prompt(paths):
    """골격에 visual_goal 절은 없다 (스펙 03) — 그림 목표는 [7] 검수가 본다."""
    install_script(paths, PISA)
    script = load_script(PISA)
    doc = run_prompt_stage(PISA, paths=paths).prompts
    for scene, source in zip(doc["scenes"], script["scenes"]):
        assert source["visual_goal"] not in scene["prompt"]


# --- 참조 서술 경로 (ADR-0030 — 서술만 산다, 첨부 경로는 없다) ---------------


def _refs_doc(script: dict, scene_id: int, description: str) -> dict:
    return {
        "run_id": script["run_id"],
        "scenes": [
            {
                "scene_id": scene_id,
                "query": ["프로브"],
                "description": description,
                "images": [],
            }
        ],
    }


def test_refs_description_lands_after_anchors_inside_subject(paths, install):
    """스펙 03의 항목 순서 — subject, anchor…, refs.description, (appearance), shot."""
    script = install(PISA, mutate=lambda s: s["scenes"][0].update(subject_anchor=["Pisa"]))
    target = script["scenes"][0]
    desc = "사진에서 읽은 실사 서술 — 풍화된 백색 대리석과 8층 아케이드"
    write_text(
        paths.run_dir(script["run_id"]) / "refs.json",
        dump_json(_refs_doc(script, target["scene_id"], desc)),
    )

    result = run_prompt_stage(PISA, paths=paths)

    assert result.described_scenes == 1
    assert "참조 서술 1씬" in result.summary
    line = line_of(result.prompts["scenes"][0]["prompt"], "SUBJECT")
    assert desc in line
    assert line.index(target["subject"]) < line.index("Pisa") < line.index(desc)
    assert line.index(desc) < line.index(FRAMINGS[result.prompts["scenes"][0]["framing"]].shot)
    # 서술이 없는 나머지 씬은 그대로다
    assert all(desc not in s["prompt"] for s in result.prompts["scenes"][1:])


def test_without_refs_the_document_is_byte_identical(install):
    """부재는 경고가 아니다 (D-3) — refs가 없으면 도입 전과 같은 바이트가 나온다."""
    script = install(PISA)
    plain, _ = build_prompts(script, source_script="x")
    empty_desc, _ = build_prompts(
        script,
        source_script="x",
        refs=_refs_doc(script, script["scenes"][0]["scene_id"], ""),
    )
    assert plain == empty_desc


def test_refs_from_another_run_are_ignored_with_a_warning(paths, install):
    """계보는 run_id로 잇는다 (ADR-0017) — 다른 편의 서술을 싣지 않는다."""
    script = install(PISA)
    doc = _refs_doc(script, script["scenes"][0]["scene_id"], "다른 편의 서술")
    doc["run_id"] = "20990101-other-run"
    write_text(paths.run_dir(script["run_id"]) / "refs.json", dump_json(doc))

    result = run_prompt_stage(PISA, paths=paths)

    assert result.described_scenes == 0
    assert any("run_id" in w for w in result.warnings)
    assert all("다른 편의 서술" not in s["prompt"] for s in result.prompts["scenes"])
