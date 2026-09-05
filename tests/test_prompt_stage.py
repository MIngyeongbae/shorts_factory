"""[5. prompt] — 헤드리스 세션이 씬별 샷 서술을 쓰고 코드가 골격에 얹는다 (ADR-0060).

확인 대상:

- ADR-0060 결정 1 — 세션 입력에 대본 전문·팩트체크·씬 계약(값 + 어휘 문구)·참조 서술이 들어간다.
  도구는 없다 (ADR-0011)
- ADR-0060 결정 2 — 골격 순서(FORMAT·STAGING·SUBJECT·CAMERA·RED·NEGATIVE), RED는 info 씬만,
  마무리 문장은 어휘, `{seconds}`는 남는다, 착지는 CAMERA 문구 뒤
- ADR-0060 결정 3 — 세션 산출 계약: 영어·ASCII, 길이, 라벨 따옴표째, 워크 단어 금지, 씬 id 일치.
  위반은 보고·중단이고 prompts.json을 쓰지 않는다 (ADR-0044)
- ADR-0017 경계 — 산출물은 runs/{run_id}/prompts.json 하나, topics/에는 쓰지 않는다
- ADR-0033 — 연출은 씬 계약에서 온다. 비었을 때만 기본값으로 떨어지고 그 수를 센다
- **ADR-0098 — 계약 위반이면 고쳐쓰기 세션 1회로 위반한 씬만 다시 쓰고 재검사한다. 사다리는
  1칸이고 루프가 아니다. 금지어 목록은 어휘에서 주입하고 손으로 옮겨 적지 않는다**
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import HOOVER, PISA, fake_plan, load_script

from shorts_factory.config import write_text
from shorts_factory.jsonio import dump_json
from shorts_factory.schemas import promptplan, vocab
from shorts_factory.schemas.visual_rules import (
    FROM_DEFAULT,
    FROM_SCENE,
    SECONDS_PLACEHOLDER,
    SECTIONS,
    schema_errors,
)
from shorts_factory.stages.prompt import (
    PROMPTS_FILE,
    PromptStageError,
    build_fix_prompt,
    build_prompts,
    build_session_prompt,
    merge_fix,
    run_prompt_stage,
    scene_brief,
    violating_scenes,
)
from test_scenetable_stage import FakeLLM

INFO = {"labels": ["221 m"], "target": "the full height of the tower", "annotation": "dimension"}
SCRIPT_MD = "# 피사의 사탑\n\n- 핵심 질문: 왜 기울었나\n\n## 대본\n\n탑이 기울었습니다.\n"
FACTCHECK_MD = "# 팩트체크\n\n| 줄 | 주장 | 판정 |\n|---|---|---|\n| 1 | 높이 55.86 m | 확인 |\n"


@pytest.fixture
def install(paths):
    """씬 계약 + 대본 + 팩트체크를 격리 루트에 놓는다."""
    def _install(slug: str = PISA, mutate=None, *, factcheck: bool = True) -> dict:
        contract = load_script(slug)
        # 라벨 규칙(ADR-0060 결정 4)에 맞춰 info 씬의 줄이 라벨 숫자를 말하게 한다
        if mutate:
            mutate(contract)
        run_id = contract["run_id"]
        run_dir = paths.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        write_text(run_dir / "topic.json", dump_json({"run_id": run_id, "slug": slug, "topic": contract["topic"]}))
        write_text(run_dir / "scenes.json", dump_json(contract))
        write_text(paths.topic_dir(slug) / "script.md", SCRIPT_MD)
        if factcheck:
            write_text(paths.topic_dir(slug) / "factcheck.md", FACTCHECK_MD)
        return contract
    return _install


def with_info(contract: dict, scene_id: int = 1) -> None:
    scene = next(s for s in contract["scenes"] if s["scene_id"] == scene_id)
    scene["info"] = dict(INFO)
    scene["text"] = scene["text"] + " 높이는 221미터입니다."


def section_names(prompt: str) -> list[str]:
    return [line.split(":", 1)[0] for line in prompt.splitlines()]


def line_of(prompt: str, section: str) -> str:
    return next(l for l in prompt.splitlines() if l.startswith(f"{section}:"))


# --- 정상 경로 ----------------------------------------------------------------------


@pytest.mark.parametrize("slug", (PISA, HOOVER))
def test_session_plan_becomes_one_prompt_per_scene(paths, install, slug):
    contract = install(slug)
    llm = FakeLLM(fake_plan(contract))
    result = run_prompt_stage(slug, llm=llm, paths=paths)

    assert result.passed and not result.errors
    assert result.prompts_path == paths.run_dir(contract["run_id"]) / PROMPTS_FILE
    document = json.loads(result.prompts_path.read_text(encoding="utf-8"))
    assert schema_errors(document) == []
    assert [s["scene_id"] for s in document["scenes"]] == [s["scene_id"] for s in contract["scenes"]]
    assert document["run_id"] == contract["run_id"]
    # topics/ 아래에는 쓰지 않는다 (ADR-0017)
    assert sorted(p.name for p in paths.topic_dir(slug).iterdir()) == ["factcheck.md", "script.md"]


def test_session_gets_script_factcheck_and_scene_briefs_without_tools(paths, install):
    contract = install(PISA, mutate=with_info)
    llm = FakeLLM(fake_plan(contract))
    run_prompt_stage(PISA, llm=llm, paths=paths)

    assert llm.tools == [()]
    prompt = llm.prompts[0]
    assert "탑이 기울었습니다." in prompt            # 대본 전문
    assert "55.86 m" in prompt                      # 팩트체크
    assert '"scene_id": 1' in prompt                # 씬 계약
    assert vocab.phrase("staging", "studio") in prompt or vocab.phrase("staging", "location") in prompt
    assert '"221 m"' in prompt                      # 라벨 문자열
    assert "London Millennium Bridge" in prompt      # 원카랩 참조 프롬프트
    low, high = promptplan.length_limits(promptplan.SUBJECT_FIELD)
    assert f"{low}~{high}" in prompt


def test_scene_brief_carries_contract_values_and_vocab_phrases():
    contract = load_script(PISA)
    scene = dict(contract["scenes"][0])
    scene["info"] = dict(INFO)
    brief = scene_brief(scene)
    assert brief["camera"]["value"] == scene["camera"]
    assert brief["camera"]["phrase"] == vocab.video_prompt(scene["camera"])
    assert brief["framing"]["value"] in vocab.values("framing")
    assert brief["info"]["labels"] == ["221 m"]
    assert brief["info"]["annotation"]["phrase"]


def test_skeleton_order_and_sources(paths, install):
    contract = install(PISA, mutate=with_info)
    result = run_prompt_stage(PISA, llm=FakeLLM(fake_plan(contract)), paths=paths)

    info_scene = next(s for s in result.prompts["scenes"] if s["scene_id"] == 1)
    plain_scene = next(s for s in result.prompts["scenes"] if s["scene_id"] == 2)
    # 이 픽스처의 씬에는 `action`이 없다 — 사건 절은 씬 계약이 걸었을 때만 나온다 (ADR-0076).
    assert section_names(info_scene["video_prompt"]) == [s for s in SECTIONS if s != "ACTION"]
    assert section_names(plain_scene["video_prompt"]) == [
        s for s in SECTIONS if s not in ("RED", "ACTION")
    ]
    assert SECONDS_PLACEHOLDER in line_of(info_scene["video_prompt"], "FORMAT")
    assert line_of(info_scene["video_prompt"], "RED").endswith(vocab.annotation_closing())
    assert '"221 m"' in line_of(info_scene["video_prompt"], "RED")
    assert info_scene["has_info"] and not plain_scene["has_info"]
    # 착지는 어휘의 워크 문구 뒤에 붙는다
    camera = line_of(info_scene["video_prompt"], "CAMERA")
    assert camera.startswith("CAMERA: " + vocab.video_prompt(contract["scenes"][0]["camera"]).rstrip("."))
    assert "arriving on the key part of scene 1" in camera
    # 글자 금지는 info 없는 씬의 NEGATIVE에만
    assert "text" in plain_scene["negative_prompt"].split(", ") and "text" not in info_scene["negative_prompt"].split(", ")


def test_subject_paragraph_is_the_sessions_text(paths, install):
    contract = install(PISA)
    plan = fake_plan(contract, subject="A leaning marble bell tower on a flat neutral studio ground, ")
    result = run_prompt_stage(PISA, llm=FakeLLM(plan), paths=paths)
    subject = line_of(result.prompts["scenes"][0]["video_prompt"], "SUBJECT")
    assert subject.startswith("SUBJECT: A leaning marble bell tower")
    # 한국어 subject·앵커는 프롬프트에 실리지 않는다 (ADR-0060 결정 5)
    assert not any(ord(ch) > 127 for ch in result.prompts["scenes"][0]["video_prompt"])


def test_direction_still_comes_from_the_contract(paths, install):
    def mutate(contract):
        contract["scenes"][0].pop("framing", None)
        contract["scenes"][0].pop("staging", None)
        contract["scenes"][1]["framing"] = "present_wide"
        contract["scenes"][1]["staging"] = "location"
    contract = install(PISA, mutate=mutate)
    result = run_prompt_stage(PISA, llm=FakeLLM(fake_plan(contract)), paths=paths)

    first, second = result.prompts["scenes"][:2]
    assert first["framing_source"] == FROM_DEFAULT and first["staging_source"] == FROM_DEFAULT
    assert second["framing"] == "present_wide" and second["framing_source"] == FROM_SCENE
    assert second["staging"] == "location" and second["staging_source"] == FROM_SCENE
    assert line_of(second["video_prompt"], "STAGING") == "STAGING: " + vocab.phrase("staging", "location")
    # 픽스처의 다른 씬들도 staging을 비워 두었을 수 있다 — 첫 씬이 기본값으로 떨어진 것만 확인한다
    assert result.default_framed_scenes >= 1 and result.default_staged_scenes >= 1


def test_missing_factcheck_is_a_warning_not_a_stop(paths, install):
    contract = install(PISA, factcheck=False)
    result = run_prompt_stage(PISA, llm=FakeLLM(fake_plan(contract)), paths=paths)
    assert result.passed
    assert any("factcheck.md" in w for w in result.warnings)


def test_second_run_is_skipped_and_force_reruns(paths, install):
    contract = install(PISA)
    llm = FakeLLM(fake_plan(contract))
    first = run_prompt_stage(PISA, llm=llm, paths=paths)
    again = run_prompt_stage(PISA, llm=llm, paths=paths)
    forced = run_prompt_stage(PISA, llm=llm, paths=paths, force=True)
    assert not first.skipped and again.skipped and not forced.skipped
    assert len(llm.prompts) == 2


# --- 세션 산출 계약 위반 → 보고·중단 ------------------------------------------------------


def _run_with(paths, install, mutate_plan, *, mutate_contract=None):
    contract = install(PISA, mutate=mutate_contract)
    plan = fake_plan(contract)
    mutate_plan(plan, contract)
    result = run_prompt_stage(PISA, llm=FakeLLM(plan), paths=paths)
    assert not result.passed
    assert not (paths.run_dir(contract["run_id"]) / PROMPTS_FILE).exists()
    return result


def test_korean_in_the_subject_is_rejected(paths, install):
    def korean(plan, _c):
        plan["scenes"][0]["subject_prompt"] = plan["scenes"][0]["subject_prompt"][:-10] + " 경주 석빙고"
    result = _run_with(paths, install, korean)
    assert any("ASCII" in e for e in result.errors)


def test_too_short_subject_is_rejected(paths, install):
    def short(plan, _c):
        plan["scenes"][0]["subject_prompt"] = "a tower"
    result = _run_with(paths, install, short)
    assert any("scenes/0/subject_prompt" in e for e in result.errors)


def test_label_must_be_quoted_verbatim_in_red(paths, install):
    def drop_label(plan, _c):
        plan["scenes"][0]["red_prompt"] = plan["scenes"][0]["red_prompt"].replace('"221 m"', '"221m"')
    result = _run_with(paths, install, drop_label, mutate_contract=with_info)
    assert any('"221 m"' in e and "따옴표" in e for e in result.errors)


def test_red_only_on_info_scenes(paths, install):
    def stray_red(plan, _c):
        plan["scenes"][1]["red_prompt"] = "a pure red arrow pointing at the base of the tower, drafting style, thin and glowing for the whole shot"
    result = _run_with(paths, install, stray_red, mutate_contract=with_info)
    assert any("info가 없는 씬" in e for e in result.errors)

    def missing_red(plan, _c):
        del plan["scenes"][0]["red_prompt"]
    result = _run_with(paths, install, missing_red, mutate_contract=with_info)
    assert any("red_prompt가 없다" in e for e in result.errors)


def test_camera_target_may_not_add_camera_work(paths, install):
    def pan(plan, _c):
        plan["scenes"][0]["camera_target"] = "then pan left to the plaza"
    result = _run_with(paths, install, pan)
    assert any("카메라 워크" in e and "pan" in e for e in result.errors)


def test_red_words_may_only_appear_in_red_prompt(paths, install):
    """석빙고 2차 씬 7 — 착지에 'red arrows'가 들어가 RED를 뗀 강등에서도 빨강이 남았다."""
    def leak(plan, _c):
        plan["scenes"][0]["camera_target"] = "arriving on the vent where the red arrows leave the chamber"
    result = _run_with(paths, install, leak)
    assert any("계측 표시를 언급" in e and "camera_target" in e for e in result.errors)


def test_every_scene_must_be_described(paths, install):
    def drop_scene(plan, _c):
        plan["scenes"].pop(3)
    result = _run_with(paths, install, drop_scene)
    assert any("샷 서술이 없다" in e for e in result.errors)


def test_stale_prompts_json_is_removed_on_failure(paths, install):
    contract = install(PISA)
    run_prompt_stage(PISA, llm=FakeLLM(fake_plan(contract)), paths=paths)
    path = paths.run_dir(contract["run_id"]) / PROMPTS_FILE
    assert path.exists()
    bad = fake_plan(contract)
    bad["scenes"][0]["subject_prompt"] = "short"
    result = run_prompt_stage(PISA, llm=FakeLLM(bad), paths=paths, force=True)
    assert result.errors and not path.exists()


def test_non_json_session_output_raises(paths, install):
    install(PISA)
    with pytest.raises(PromptStageError, match="JSON"):
        run_prompt_stage(PISA, llm=FakeLLM("not json at all"), paths=paths)


def test_missing_contract_raises(paths):
    with pytest.raises(PromptStageError):
        run_prompt_stage("없는-슬러그", llm=FakeLLM({}), paths=paths)


# --- 조립 함수 단독 -----------------------------------------------------------------


def test_build_prompts_output_matches_the_prompts_schema():
    contract = load_script(HOOVER)
    document = build_prompts(contract, fake_plan(contract), source_script="runs/x/scenes.json")
    assert schema_errors(document) == []
    assert document["style"]["base_style"] == vocab.style("base_style")


def test_session_prompt_has_no_unfilled_placeholders():
    contract = load_script(PISA)
    prompt, described = build_session_prompt(
        topic=contract["topic"], script_text=SCRIPT_MD, factcheck=None, contract=contract, refs=None,
    )
    assert "${" not in prompt and described == 0
    assert "(없음" in prompt


# --- 고쳐쓰기 사다리 1칸 (ADR-0098) ------------------------------------------------


class SequenceLLM:
    """호출마다 다음 페이로드를 준다 — 사다리(세션 2회)를 재는 데 쓴다.

    `FakeLLM`은 늘 같은 답을 주므로 「고쳐서 다시 낸다」를 표현하지 못한다.
    """

    def __init__(self, *payloads):
        self.payloads = [
            p if isinstance(p, str) else json.dumps(p, ensure_ascii=False) for p in payloads
        ]
        self.prompts: list[str] = []
        self.labels: list[str] = []

    def run(self, prompt, *, allowed_tools=(), timeout=None, label="", **_kw):
        from types import SimpleNamespace

        self.prompts.append(prompt)
        self.labels.append(label)
        text = self.payloads[min(len(self.prompts) - 1, len(self.payloads) - 1)]
        return SimpleNamespace(
            text=text, meta={"session_id": None, "num_turns": None, "duration_ms": None}
        )


def _entry(plan: dict, scene_id: int) -> dict:
    return next(e for e in plan["scenes"] if e["scene_id"] == scene_id)


def plan_with_camera_word(contract: dict, scene_id: int = 1, word: str = "panning") -> dict:
    """`camera_target`에 워크 단어를 넣어 **씬 단위** 계약 위반을 만든다."""
    plan = fake_plan(contract)
    _entry(plan, scene_id)["camera_target"] = f"{word} across the key part of the scene"
    return plan


def test_contract_violation_is_rewritten_once_and_then_passes(paths, install):
    """위반 → 고쳐쓰기 1회 → 통과하면 prompts.json을 쓴다 (ADR-0098 결정 1)."""
    contract = install(PISA)
    bad = plan_with_camera_word(contract)
    fix = {"scenes": [_entry(fake_plan(contract), 1)]}
    llm = SequenceLLM(bad, fix)

    result = run_prompt_stage(PISA, llm=llm, paths=paths, timeout=5)

    assert result.passed and not result.errors
    assert result.prompts_path.exists()
    assert len(llm.prompts) == 2, "세션은 두 번 돈다 — 본 세션 + 고쳐쓰기 1회"
    assert llm.labels[1].endswith("-fix")
    assert result.revision["scenes"] == [1]
    assert result.revision["changed"] == [1]
    assert any("고쳐쓰기 1회로 살렸다" in w for w in result.warnings)


def test_second_violation_stops_without_writing_prompts(paths, install):
    """두 번째도 어기면 거기서 중단한다 — 루프가 아니다 (ADR-0044 유지)."""
    contract = install(PISA)
    bad = plan_with_camera_word(contract)
    still_bad = {"scenes": [dict(_entry(bad, 1))]}
    llm = SequenceLLM(bad, still_bad)

    result = run_prompt_stage(PISA, llm=llm, paths=paths, timeout=5)

    assert not result.passed and result.errors
    assert not (paths.run_dir(contract["run_id"]) / PROMPTS_FILE).exists()
    assert len(llm.prompts) == 2, "사다리는 1칸이다 — 세 번째 세션은 없다"


def test_failed_fix_session_stops_on_the_first_violation(paths, install):
    """고쳐쓰기 세션이 깨지면 첫 위반으로 중단한다 — 사다리가 사다리를 막지 않는다."""
    contract = install(PISA)
    bad = plan_with_camera_word(contract)
    llm = SequenceLLM(bad, "이건 JSON이 아니다")

    result = run_prompt_stage(PISA, llm=llm, paths=paths, timeout=5)

    assert not result.passed and result.errors
    assert not (paths.run_dir(contract["run_id"]) / PROMPTS_FILE).exists()
    assert "session_error" in result.revision


def test_only_violating_scenes_reach_the_fix_session(paths, install):
    """통과한 씬은 고쳐쓰기 입력에 실리지 않는다 — 멀쩡한 단락이 바뀌면 안 된다."""
    contract = install(PISA)
    assert len(contract["scenes"]) > 1
    bad = plan_with_camera_word(contract, scene_id=1)
    llm = SequenceLLM(bad, {"scenes": [_entry(fake_plan(contract), 1)]})

    run_prompt_stage(PISA, llm=llm, paths=paths, timeout=5)

    fix_prompt = llm.prompts[1]
    assert "## 씬 1" in fix_prompt
    for scene in contract["scenes"][1:]:
        assert f"## 씬 {scene['scene_id']}" not in fix_prompt
    assert "camera_target가 카메라 워크를 새로 지시한다" in fix_prompt, "위반 문장이 사유로 실린다"


def test_non_scene_violation_skips_the_ladder(paths, install):
    """씬에 안 걸리는 위반(스키마·씬 누락)은 사다리를 태우지 않는다 — 고칠 자리가 없다."""
    contract = install(PISA)
    plan = fake_plan(contract)
    plan["scenes"] = plan["scenes"][:-1]          # 씬 하나를 통째로 뺀다
    llm = SequenceLLM(plan, {"scenes": []})

    result = run_prompt_stage(PISA, llm=llm, paths=paths, timeout=5)

    assert not result.passed
    assert len(llm.prompts) == 1, "고쳐쓰기를 부르지 않는다"
    assert result.revision is None


def test_forbidden_words_come_from_vocab_not_a_hand_copy(paths, install):
    """세션 프롬프트의 금지어는 어휘에서 온다 (ADR-0098 결정 2 · ADR-0034).

    `crane`이 어휘에만 있고 프롬프트에 없어 `tokyo-tower-tanks`가 세 번 죽은 것이 근거다.
    """
    contract = install(PISA)
    llm = FakeLLM(fake_plan(contract))
    run_prompt_stage(PISA, llm=llm, paths=paths)

    prompt = llm.prompts[0]
    for word in vocab.camera_target_forbidden_words():
        assert word in prompt, f"어휘의 금지어 {word!r}가 세션 프롬프트에 없다"
    assert "crane" in prompt


def test_violating_scenes_reads_scene_ids_or_gives_up():
    assert violating_scenes(["scenes/3: 어쩌고", "scenes/1: 저쩌고", "scenes/3: 또"]) == [1, 3]
    assert violating_scenes(["씬 4의 샷 서술이 없다"]) is None
    assert violating_scenes([]) == []


def test_schema_violation_number_is_an_array_index_not_a_scene_id():
    """`scenes/{i}/{필드}`의 `i`는 **배열 인덱스**다 (ADR-0098 개정 1).

    씬 id가 1부터 시작하므로 인덱스 13이 씬 14다. 뭉쳐 읽으면 사다리가 엉뚱한 씬을
    고쳐 써서 검사만 통과하고 화면이 틀린다 — `tokyo-tower-tanks` 실측이 근거다.
    """
    plan = {"scenes": [{"scene_id": i + 1} for i in range(16)]}
    assert violating_scenes(["scenes/13/red_prompt: is too long"], plan) == [14]
    # 교차 규칙은 같은 자리에 **씬 번호**를 쓴다 — 같은 문자열 모양, 다른 번호 체계다
    assert violating_scenes(["scenes/13: action_prompt가 …"], plan) == [13]
    # 옮길 수 없는 인덱스는 추측하지 않는다
    assert violating_scenes(["scenes/99/red_prompt: is too long"], plan) is None
    assert violating_scenes(["scenes/0/red_prompt: is too long"], None) is None
    assert violating_scenes(["(root): 'scenes' is a required property"], plan) is None


def test_field_level_schema_violation_rides_the_ladder_on_the_right_scene(paths, install):
    """길이 초과 같은 **필드 내용** 스키마 위반은 사다리에 태우고, 그 씬을 정확히 짚는다."""
    contract = install(PISA, mutate=with_info)
    plan = fake_plan(contract)
    index = len(plan["scenes"]) - 1
    target_id = plan["scenes"][index]["scene_id"]
    assert index != target_id, "인덱스와 씬 번호가 달라야 이 테스트가 뜻이 있다"
    plan["scenes"][index]["camera_target"] = "x" * 4000        # maxLength 위반
    llm = SequenceLLM(plan, {"scenes": [_entry(fake_plan(contract), target_id)]})

    result = run_prompt_stage(PISA, llm=llm, paths=paths, timeout=5)

    assert result.passed, result.errors
    assert result.revision["scenes"] == [target_id]
    assert f"## 씬 {target_id}" in llm.prompts[1]


def test_merge_fix_replaces_only_the_named_scenes_and_drops_absent_keys():
    """없는 키는 지운다 — 「없어야 할 단락이 있다」도 위반이라 키를 빼는 것이 고치는 방법이다."""
    plan = {"scenes": [
        {"scene_id": 1, "subject_prompt": "old one", "red_prompt": "old red"},
        {"scene_id": 2, "subject_prompt": "untouched"},
    ]}
    merged, changed = merge_fix(plan, {"scenes": [{"scene_id": 1, "subject_prompt": "new one"}]}, [1])

    assert changed == [1]
    assert _entry(merged, 1)["subject_prompt"] == "new one"
    assert "red_prompt" not in _entry(merged, 1)
    assert _entry(merged, 2)["subject_prompt"] == "untouched"
    assert _entry(plan, 1)["subject_prompt"] == "old one", "원본 plan은 그대로다"
