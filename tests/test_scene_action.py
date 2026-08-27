"""씬 계약의 **사건**(`action`)과 프롬프트의 `ACTION` 절 — ADR-0076.

확인 대상:

- 사건이 씬 계약에서 프롬프트까지 **실제로 실린다.** 없던 증상: `[3s]`는 "갈고리가 물지
  못하고 비틀려 벌어진다"를 적었는데 `[5]`가 `has failed`(완료 상태)로 바꿔 정물이 됐다
- **MJ에는 사건이 가지 않는다** (대안 D). 프레임 경로의 소비자는 `mj-endimage`이고, 정지
  이미지 계열이라 동작 서술이 모션블러로 나온다. 긴 본문은 MJ가 다시 써서 프록시가 결과를
  못 묶는 실측도 있다 (ADR-0069·0071 — 358단어에 10분 타임아웃)
- **자격 검사** — 계약에 `action`이 있으면 `action_prompt`가 있어야 하고, 없으면 없어야
  한다. 정물 씬에 동작을 지어내면 대본에 없는 사건이 화면에 뜬다
- **STAGING 복창 반려** (맥락 8). 코드가 이미 넣는 무대 문구를 세션이 SUBJECT에 또 쓰면
  한 프롬프트에 같은 무대가 두 번 들어가 씬이 전부 같은 그림으로 수렴한다
"""

from __future__ import annotations

from shorts_factory.schemas import promptplan, vocab
from shorts_factory.schemas.visual_rules import build_video_prompt

SUBJECT = (
    "two facing rows of small metal hooks and crossbars sit along a pair of flanged rails, "
    "each hook curled to dip under the bar opposite it, the brass worn to a dull shine"
)
ACTION = (
    "one hook fails to catch and twists sideways against its bar, and the row springs open "
    "at that single point while the pieces to either side stay closed"
)
TARGET = "on the single skewed hook jammed against its crossbar"


def _scene(scene_id: int = 1, **overrides) -> dict:
    scene = {
        "scene_id": scene_id,
        "text": "줄",
        "est_start": 0.0,
        "est_end": 3.0,
        "beat": "failure_reason",
        "camera": "static",
        "framing": "frontal_diagram",
        "staging": "studio",
        "subject": "저드슨식 잠금장치",
        "subject_scale": "diagram",
        "visual_goal": "고장이 나는 순간의 형태",
    }
    scene.update(overrides)
    return scene


def _contract(scenes: list[dict]) -> dict:
    return {"run_id": "20260826-probe", "topic": "프로브", "total_duration": 60.0, "scenes": scenes}


def _entry(scene_id: int = 1, **overrides) -> dict:
    entry = {"scene_id": scene_id, "subject_prompt": SUBJECT, "camera_target": TARGET}
    entry.update(overrides)
    return entry


# --- 골격에 실린다 -------------------------------------------------------------


def test_the_action_clause_carries_the_event_into_the_prompt():
    prompt, _ = build_video_prompt(
        subject_prompt=SUBJECT, staging="studio", camera="static",
        camera_target=TARGET, action_prompt=ACTION,
    )
    assert "ACTION:" in prompt
    assert ACTION in prompt


def test_a_static_scene_gets_no_action_clause():
    prompt, _ = build_video_prompt(
        subject_prompt=SUBJECT, staging="studio", camera="static", camera_target=TARGET,
    )
    assert "ACTION:" not in prompt


def test_the_action_clause_never_reaches_the_frames_path():
    """프레임 경로 = `mj-endimage` = MJ. 사건은 H3·Omni만 받는다 (ADR-0076 대안 D)."""
    prompt, _ = build_video_prompt(
        subject_prompt=SUBJECT, staging="studio", camera="static",
        camera_target=TARGET, action_prompt=ACTION, frames=True,
    )
    assert "ACTION:" not in prompt
    assert ACTION not in prompt


def test_the_frames_path_is_an_mj_engine():
    """대안 D의 전제가 어휘에서 성립하는가 — 프레임을 받는 라인의 엔진이 MJ인가."""
    for line in vocab.meta("video_line"):
        if line.startswith("_") or not vocab.style_in_frames(line):
            continue
        assert "mj" in str(vocab.video_line_meta(line).get("provider") or "")


# --- 자격 검사 -----------------------------------------------------------------


def test_a_scene_with_an_action_must_get_an_action_prompt():
    contract = _contract([_scene(action="갈고리가 비틀려 열이 벌어진다")])
    errors = promptplan.cross_errors({"scenes": [_entry()]}, contract)
    assert any(promptplan.ACTION_FIELD in e for e in errors)


def test_a_scene_without_an_action_may_not_invent_one():
    """대본에 없는 동작이 화면에 뜨면 그것이 곧 대본과 겉도는 영상이다."""
    contract = _contract([_scene()])
    errors = promptplan.cross_errors(
        {"scenes": [_entry(action_prompt=ACTION)]}, contract
    )
    assert any(promptplan.ACTION_FIELD in e for e in errors)


def test_a_matched_action_passes():
    contract = _contract([_scene(action="갈고리가 비틀려 열이 벌어진다")])
    assert promptplan.cross_errors(
        {"scenes": [_entry(action_prompt=ACTION)]}, contract
    ) == []


def test_the_action_prompt_may_not_direct_the_camera():
    """움직이는 것은 피사체다 — 워크는 씬 계약의 `camera`가 정한다 (ADR-0033 §3)."""
    walk = vocab.camera_target_forbidden_words()[0]
    contract = _contract([_scene(action="갈고리가 비틀린다")])
    errors = promptplan.cross_errors(
        {"scenes": [_entry(action_prompt=f"the camera {walk}s as the hook twists open")]},
        contract,
    )
    assert any("카메라 워크를 지시한다" in e for e in errors)


def test_the_action_prompt_may_not_mention_the_red_annotation():
    """RED를 뗀 강등 재생성에서 빨강이 남지 않게 — 다른 단락과 같은 규칙이다 (ADR-0060)."""
    contract = _contract([_scene(action="갈고리가 비틀린다")])
    errors = promptplan.cross_errors(
        {"scenes": [_entry(action_prompt=ACTION + ", and a red arrow follows it")]},
        contract,
    )
    assert any("계측 표시를 언급한다" in e for e in errors)


# --- STAGING 복창 (맥락 8) -----------------------------------------------------


def test_restating_the_staging_clause_is_rejected():
    """복창 실측: zipper 12/12 · us-penny-halt 8/8 · rai-stones 7/7 (ADR-0076 맥락 8)."""
    echoed = SUBJECT + ", " + vocab.phrase("staging", "studio")
    contract = _contract([_scene()])
    errors = promptplan.cross_errors(
        {"scenes": [_entry(subject_prompt=echoed)]}, contract
    )
    assert any("STAGING" in e for e in errors)


def test_a_subject_that_does_not_restate_the_staging_passes():
    contract = _contract([_scene()])
    assert promptplan.cross_errors({"scenes": [_entry()]}, contract) == []


# --- 빈 사건은 정물 씬이다 (2026-08-27 실측: 세션이 ""로 말한다) ------------------


def test_an_empty_action_validates_and_means_no_action():
    """세션은 정물 씬을 `""`로 말한다 — 키 생략과 같은 뜻이라 계약이 받아야 한다.

    실측: `zipper-late-adoption` 재실행에서 `[3s]`가 정물 씬 4개에 `""`를 넣었고
    `minLength: 1`이 그것을 반려해 세션 하나를 통째로 버렸다.
    """
    from shorts_factory.schemas import sceneplan

    scene = _scene(action="")
    contract = _contract([scene])
    # 씬 계약 스키마가 받는다
    from shorts_factory.schemas.scenes import validate_scenes

    errors, _ = validate_scenes(contract)
    assert errors == []
    # 그리고 사건 없는 씬으로 읽힌다 — `action_prompt`를 요구하지 않는다
    assert promptplan.cross_errors({"scenes": [_entry()]}, contract) == []
    assert sceneplan.direction_summary({"scenes": [scene]})["action"] == 0


# --- 지시문과 계약이 갈리면 세션이 진다 (2026-08-27 실측) -----------------------


def test_the_mj_block_does_not_ask_for_mj_subject_on_every_scene():
    """`mj_subject` 지시문이 계약(ADR-0075 결정 1)과 같은 말을 하는가.

    실측: 지시문이 "모든 씬"이라 적혀 있어 세션이 `info` 씬 8개에도 `mj_subject`를 썼고,
    검사기가 *"info 씬은 MJ를 타지 않는다"*로 전부 반려해 세션 하나를 통째로 버렸다.
    ADR-0075가 규칙을 좁힐 때 지시문이 따라가지 않은 자리다.
    """
    from shorts_factory.stages.prompt import MJ_BLOCK

    assert "모든 씬" not in MJ_BLOCK
    assert "info" in MJ_BLOCK


def test_every_session_paragraph_can_be_carried_into_prompts_json():
    """`[5]` 세션 산출의 단락이 전부 `prompts.json` 레코드에도 자리가 있는가.

    두 스키마는 갈라질 수 없다 (ADR-0034의 태도). 실측: `action_prompt`를 세션 계약
    (`promptplan.schema.json`)에만 넣고 레코드 스키마(`PROMPT_SCENE_SCHEMA`)에 안 넣어
    `[5]`가 *"Additional properties are not allowed"*로 죽었다 — 세션은 옳게 썼는데
    코드가 그것을 실을 자리를 안 만든 것이다.
    """
    from shorts_factory.schemas.visual_rules import PROMPT_SCENE_SCHEMA

    record_fields = set(PROMPT_SCENE_SCHEMA["properties"])
    paragraphs = {
        promptplan.SUBJECT_FIELD,
        promptplan.ACTION_FIELD,
        promptplan.CAMERA_TARGET_FIELD,
        promptplan.RED_FIELD,
        promptplan.SHOT2_FIELD,
        promptplan.MJ_SUBJECT_FIELD,
    }
    assert paragraphs <= record_fields, paragraphs - record_fields
