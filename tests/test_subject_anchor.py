"""`subject_anchor` (ADR-0028) — 대상을 고정하는 명사는 그림을 아는 단계가 쓴다.

실측 근거는 `runs/20260812-mj-lang-probe/`의 relax 9잡이다. 재질이 빠지면 콘크리트
접합면이 **나무 짜맞춤**이 됐고, 정체 명사(`댐`)가 빠지면 협곡과 강만 나왔다.

확인 대상:
- 계약: **선택 필드**다. 없어도 비어도 통과하고, 있을 때 타입만 본다
- `[3s]`: 앵커를 쓰는 자리. 타입은 `sceneplan.schema.json`이 본다
- `[5]`: 값이 있을 때 **`subject` 바로 뒤**(G1이 검증한 자리)에 싣고 **거르지 않는다**(G2)
- `[5]`: 앵커를 채운 씬 수를 요약에 센다 — ADR-0028 되돌릴 조건의 관측 수단이다
"""

from __future__ import annotations

import pytest

from shorts_factory.config import write_text
from shorts_factory.jsonio import dump_json
from shorts_factory.schemas.scenes import validate_scenes
from shorts_factory.schemas.timed_scenes import (
    build_timed_scenes,
    validate_timed_scenes,
)
from shorts_factory.schemas.visual_rules import (
    build_video_prompt,
    clean_anchors,
    subject_line,
)
from shorts_factory.stages.prompt import run_prompt_stage
from shorts_factory.schemas.sceneplan import validate_sceneplan

from conftest import HOOVER, PISA, load_script

#: 탐침에서 0/4로 무너졌다가 `콘크리트`를 붙여 4/4가 된 그 씬이다 (`s17`).
SUBJECT = "홈이 파인 블록 접합면 클로즈업"
GOAL = "맞물린 홈이 어떤 모양인지"
SHOT = "tight detail close-up of the solution"


# --- 계약 (specs/02) ---------------------------------------------------------


def test_the_real_scripts_satisfy_the_contract_without_the_field():
    """옛 대본이 그대로 통과해야 한다. 백필 단계를 또 만들지 않기 위한 조건이다.

    표본이 후버댐에서 피사로 옮겨 갔다 — 후버댐 픽스처는 앵커가 전 씬에 채워져 있어
    이제 '필드가 없는 대본'의 표본이 아니다 (ADR-0029).
    """
    script = load_script(PISA)
    assert all("subject_anchor" not in s for s in script["scenes"])

    errors, _ = validate_scenes(script)
    assert errors == []


@pytest.mark.parametrize("value", [[], ["콘크리트"], ["후버댐", "콘크리트"]])
def test_a_list_of_strings_is_accepted(value):
    """비어도 된다. 재질도 정체도 무의미한 씬이 실제로 있다 (도해)."""
    script = load_script(HOOVER)
    script["scenes"][0]["subject_anchor"] = value

    errors, _ = validate_scenes(script)
    assert errors == []


@pytest.mark.parametrize("value", ["콘크리트", ["콘크리트", 3], [""], {"재질": "콘크리트"}])
def test_the_contract_checks_the_type_when_present(value):
    """없다고 실패시키지 않는 대신, 있으면 타입은 본다."""
    script = load_script(HOOVER)
    script["scenes"][0]["subject_anchor"] = value

    errors, _ = validate_scenes(script)
    assert any("subject_anchor" in e for e in errors), errors


def test_the_absence_is_not_even_a_warning():
    """선택 필드에 부재 경고를 달면 규칙이 선택이 아니게 된다."""
    _errors, warnings = validate_scenes(load_script(HOOVER))
    assert not any("anchor" in w or "앵커" in w for w in warnings)


# --- 프롬프트 조립 (ADR-0028 G1) ---------------------------------------------


def subject_of(*anchors: str, **kw) -> str:
    return subject_line(SUBJECT, shot=SHOT, anchors=list(anchors), **kw)


def test_the_anchor_lands_right_after_the_subject():
    """실측한 자리다. 뒤로 밀면 스타일·구도 토큰 뒤가 되어 검증한 적 없는 배치가 된다."""
    assert subject_of("콘크리트").startswith(f"{SUBJECT}, 콘크리트.")


def test_the_shipped_anchor_string_matches_what_was_verified():
    """G1 실측 2건이 통과시킨 그 문자열이다 — `subject` 뒤 콤마 항목."""
    assert subject_of("콘크리트").startswith("홈이 파인 블록 접합면 클로즈업, 콘크리트")
    assert subject_line(
        "협곡을 가득 메운 거대한 댐 덩어리", shot=SHOT, anchors=["콘크리트"]
    ).startswith("협곡을 가득 메운 거대한 댐 덩어리, 콘크리트")


def test_the_order_given_is_the_order_shipped():
    """구체적인 것부터 — 고유명사 → 정체 → 재질. `[5]`가 순서를 바꾸지 않는다."""
    line = subject_line("강철 파이프 격자", shot=SHOT, anchors=["후버댐", "댐", "콘크리트"])
    assert line.startswith("강철 파이프 격자, 후버댐, 댐, 콘크리트.")


def test_no_anchor_changes_not_one_byte():
    """선택 필드다. 안 쓴 씬의 프롬프트는 앵커 도입 전과 같아야 한다."""
    base = dict(subject=SUBJECT, shot=SHOT, staging="studio", camera="static")
    assert build_video_prompt(**base) == build_video_prompt(**base, anchors=[])


def test_the_anchor_reaches_the_whole_prompt():
    prompt, _negative = build_video_prompt(
        subject=SUBJECT, shot=SHOT, staging="studio", camera="static", anchors=["콘크리트"],
    )
    assert "콘크리트" in prompt


def test_the_anchor_stays_on_the_subject_line():
    """앵커도 피사체 서술이다 — SUBJECT 절 안, 구도 문구 앞이다 (스펙 03)."""
    line = subject_of("콘크리트")
    assert line.index("콘크리트") < line.index(SHOT)
    assert line.endswith(f"{SHOT}.")


def test_the_prompt_stage_does_not_filter_anchors():
    """G2 — 약한 피사체 위의 고유명사는 해롭지만, 거를 축이 룰 테이블에 없다.

    `[5]`는 어느 명사가 이 씬에서 더 센지 알 수단이 없다. 고르는 것은 `[3s]`다.
    여기서 조용히 떨어뜨리면 `[3s]`가 잘못 골랐다는 사실이 영영 안 보인다.
    """
    assert "후버댐" in subject_of("후버댐", "콘크리트")


def test_blank_items_are_dropped():
    assert clean_anchors(["  콘크리트 ", "", "   "]) == ["콘크리트"]
    assert subject_of("", "  ") == subject_of()


# --- [3s. scenetable] 연출표 --------------------------------------------------


def planned_scene(**overrides) -> dict:
    """연출표의 씬 하나. 앵커의 생산자는 이제 `[3s]`다 (ADR-0049)."""
    scene = {
        "scene_id": 1, "beat": "hook_fact",
        "visual_goal": "그림이 지는 설명", "subject": "피사체",
        "subject_scale": "wide", "camera": "static",
    }
    scene.update(overrides)
    return scene


def plan_of(scene: dict) -> dict:
    return {"topic": "t", "scenes": [scene]}


def test_a_bare_string_is_rejected_by_the_plan_contract():
    """문자열을 그냥 두면 낱글자가 앵커가 되어 프롬프트에 실린다.

    타입 검사는 값이 태어나는 자리(`[3s]`의 연출표 스키마)에서 막는다 (ADR-0034 §3).
    """
    errors, _ = validate_sceneplan(plan_of(planned_scene(subject_anchor="콘크리트")))
    assert any("subject_anchor" in e for e in errors)


def test_a_non_string_item_is_rejected_by_the_plan_contract():
    errors, _ = validate_sceneplan(plan_of(planned_scene(subject_anchor=["콘크리트", 3])))
    assert any("subject_anchor" in e for e in errors)


def test_a_blank_item_is_rejected_by_the_plan_contract():
    errors, _ = validate_sceneplan(plan_of(planned_scene(subject_anchor=["콘크리트", " "])))
    assert any("subject_anchor" in e for e in errors)


# --- [5. prompt] 단계 --------------------------------------------------------


@pytest.fixture
def anchored(paths):
    """실물 대본의 앞 두 씬에**만** 앵커를 넣어 격리된 루트에 놓는다.

    나머지 씬의 앵커는 지운다 — 후버댐 픽스처는 전 씬에 앵커가 채워져 있어
    (ADR-0029) 지우지 않으면 "몇 씬이 앵커를 가졌나"를 세는 테스트가 대본 내용에
    끌려다닌다.
    """

    def _install(anchors: list[str]) -> dict:
        script = load_script(HOOVER)
        for scene in script["scenes"]:
            scene.pop("subject_anchor", None)
        for scene in script["scenes"][:2]:
            scene["subject_anchor"] = anchors
        run_dir = paths.run_dir(script["run_id"])
        run_dir.mkdir(parents=True, exist_ok=True)
        write_text(
            run_dir / "topic.json",
            dump_json(
                {"run_id": script["run_id"], "slug": HOOVER, "topic": script["topic"]}
            ),
        )
        write_text(run_dir / "scenes.json", dump_json(script))
        return script

    return _install


def test_the_anchor_reaches_the_prompt_of_that_scene(paths, anchored):
    script = anchored(["후버댐", "콘크리트"])
    result = run_prompt_stage(HOOVER, paths=paths)

    entries = result.prompts["scenes"]
    subject_lines = [
        next(l for l in e["prompt"].splitlines() if l.startswith("SUBJECT:")) for e in entries
    ]
    assert subject_lines[0].startswith(
        f"SUBJECT: {script['scenes'][0]['subject']}, 후버댐, 콘크리트."
    )
    # 앵커를 안 준 씬은 그대로다 — 이 단계가 추론해 채우지 않는다 (ADR-0001).
    assert "후버댐, 콘크리트" not in entries[2]["prompt"]


def test_the_summary_counts_anchored_scenes(paths, anchored):
    """되돌릴 조건("`[1]`이 습관적으로 비우면")을 관측하는 값이다."""
    anchored(["콘크리트"])
    result = run_prompt_stage(HOOVER, paths=paths)

    assert result.anchored_scenes == 2
    assert "대상 앵커 2씬" in result.summary


def test_an_empty_anchor_is_not_counted(paths, anchored):
    anchored([])
    result = run_prompt_stage(HOOVER, paths=paths)

    assert result.anchored_scenes == 0


# --- [3] scenes.timed.json ---------------------------------------------------


def test_the_anchor_travels_into_the_measured_file():
    """피사체 서술이라 실측 파일에도 남는다 — `subject`·`subject_scale`과 같은 자리다.

    `visual_goal`처럼 떨어뜨리지(`DROPPED`) 않는다. 그 규칙은 **이미지 지시**를 겨눈
    것이고(ADR-0020·0022), 앵커는 `[1]`이 쓰는 피사체 서술이다 (ADR-0018과 같은 판단).
    스키마가 씬 계약에서 파생되므로 이 동작은 자동으로 따라온다 — 그래서 못을 박는다.
    """
    script = load_script(HOOVER)
    script["scenes"][0]["subject_anchor"] = ["후버댐", "콘크리트"]
    doc = build_timed_scenes(
        script, [(s["est_start"], s["est_end"]) for s in script["scenes"]]
    )

    assert doc["scenes"][0]["subject_anchor"] == ["후버댐", "콘크리트"]
    assert validate_timed_scenes(doc)[0] == []
