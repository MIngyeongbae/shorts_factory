"""[1a] outline · [1s] sceneplan · [1w] write — 갈라진 대본 3단계의 계약 검증 (ADR-0029).

옛 `[1] script` 하나가 아홉 가지 판단을 지던 것을 셋으로 가른 결과다. 그래서 이 파일이
가장 많이 확인하는 것은 **각 단계가 자기 몫만 하는가**다.

- `[1a]`는 씬도 문장도 만들지 않는다 — 산출물에 그런 필드가 아예 없다
- `[1s]`는 문장을 쓰지 않는다 — `says`에 존댓말이 오면 반려된다
- `[1w]`는 씬 구조를 바꿀 수 없다 — 세션에 `scene_id`와 `text` 말고는 받지 않는다

단계는 파일로만 통신한다 (ADR-0011). 그래서 앞 단계의 산출물을 직접 놓고 뒷 단계를
돌리는 것이 정상적인 사용법이고, 아래 픽스처가 그렇게 한다.
"""

import json
from datetime import date

import pytest

from conftest import load_fixture
from shorts_factory.config import write_text
from shorts_factory.jsonio import dump_json
from shorts_factory.llm.fake import FakeLLMClient
from shorts_factory.stages.outline import run_outline_stage
from shorts_factory.stages.research import run_research_stage
from shorts_factory.stages.sceneplan import run_sceneplan_stage
from shorts_factory.stages.session import ScriptSessionError
from shorts_factory.stages.topic import run_topic_stage
from shorts_factory.stages.write import build_scenes, run_write_stage

TODAY = date(2026, 8, 13)
TOPIC = "후버댐 콘크리트 냉각"
SLUG = "hubeodaem-konkeuriteu-naenggak"
RUN_ID = f"20260813-{SLUG}"


def as_text(name: str, **overrides) -> str:
    data = load_fixture(name)
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


@pytest.fixture
def prepared(paths):
    """[0a]+[0b]까지 끝난 프로젝트. 팩트시트만 있고 대본 쪽은 비어 있다."""
    backlog = paths.root / "topics" / "backlog.md"
    backlog.write_text(
        backlog.read_text(encoding="utf-8")
        + f"| {TOPIC} | ✅ | ✅ | ✅ | ✅ | 개척국 기술보고서 | 후보 |\n",
        encoding="utf-8",
    )
    run_topic_stage(TOPIC, paths=paths, today=TODAY)
    run_research_stage(
        SLUG,
        llm=FakeLLMClient(
            ["# 조사", "# 검증", "# 비판", as_text("factsheet_hoover.json")]
        ),
        paths=paths,
    )
    return paths


@pytest.fixture
def with_outline(prepared):
    write_text(prepared.topic_dir(SLUG) / "07-outline.json", as_text("outline_pass.json"))
    return prepared


@pytest.fixture
def with_plan(with_outline):
    write_text(
        with_outline.topic_dir(SLUG) / "08-sceneplan.json", as_text("sceneplan_pass.json")
    )
    return with_outline


def state_of(paths, stage: str) -> dict:
    data = json.loads((paths.run_dir(RUN_ID) / "state.json").read_text(encoding="utf-8"))
    return data["stages"][stage]


# --- [1a] outline ------------------------------------------------------------


def test_outline_writes_the_contract_file(prepared):
    result = run_outline_stage(
        SLUG, llm=FakeLLMClient([as_text("outline_pass.json")]), paths=prepared
    )

    assert result.valid
    assert result.path == prepared.topic_dir(SLUG) / "07-outline.json"
    assert json.loads(result.path.read_text(encoding="utf-8")) == result.outline
    assert state_of(prepared, "1a-outline")["status"] == "done"


def test_outline_makes_no_scenes_and_no_sentences(prepared):
    """이 단계가 씬이나 문장을 내놓으면 스키마가 막는다 — 판단이 섞이는 자리다."""
    result = run_outline_stage(
        SLUG, llm=FakeLLMClient([as_text("outline_pass.json")]), paths=prepared
    )
    assert "scenes" not in result.outline
    assert all("text" not in act for act in result.outline["acts"])


def test_outline_session_gets_no_tools_and_the_factsheet(prepared):
    """팩트시트는 프롬프트에 주입한다. 세션에 읽을 도구를 주지 않는다 (ADR-0011)."""
    llm = FakeLLMClient([as_text("outline_pass.json")])
    run_outline_stage(SLUG, llm=llm, paths=prepared)

    call = llm.calls[0]
    assert call["allowed_tools"] == ()
    assert "933킬로미터" in call["prompt"], "팩트시트가 프롬프트에 실린다"


def test_outline_prompt_carries_the_length_envelope_from_the_contract(prepared):
    """분량 값을 프롬프트 마크다운에 적지 않는다 — script-rules.json에서 온다."""
    llm = FakeLLMClient([as_text("outline_pass.json")])
    run_outline_stage(SLUG, llm=llm, paths=prepared)

    from shorts_factory.schemas import vocab

    low, high = vocab.limits()["total_chars"]
    assert f"{low}~{high}" in llm.calls[0]["prompt"]


def test_outline_hides_low_confidence_facts(prepared):
    """confidence: low는 "쓰지 마라"가 아니라 아예 안 보여준다 (specs/06)."""
    sheet = load_fixture("factsheet_hoover.json")
    sheet["facts"][0]["confidence"] = "low"
    write_text(prepared.topic_dir(SLUG) / "04-factsheet.json", dump_json(sheet))

    llm = FakeLLMClient([as_text("outline_pass.json")])
    run_outline_stage(SLUG, llm=llm, paths=prepared)
    assert "221미터" not in llm.calls[0]["prompt"]


def test_outline_grounded_in_must_exist_in_the_factsheet(prepared):
    """지어낸 근거 id는 그라운딩 위반이다 (ADR-0007)."""
    outline = load_fixture("outline_pass.json")
    outline["acts"][0]["grounded_in"] = ["f99"]
    result = run_outline_stage(
        SLUG, llm=FakeLLMClient([json.dumps(outline, ensure_ascii=False)]), paths=prepared
    )

    assert not result.valid
    assert any("f99" in e for e in result.errors)
    assert result.path.exists(), "실패해도 산출물은 남긴다 — [2]가 판정한다"


def test_outline_refuses_a_rejected_factsheet(prepared):
    sheet = load_fixture("factsheet_hoover.json")
    sheet["verdict"] = "fail"
    write_text(prepared.topic_dir(SLUG) / "04-factsheet.json", dump_json(sheet))

    with pytest.raises(ScriptSessionError, match="verdict"):
        run_outline_stage(SLUG, llm=FakeLLMClient([]), paths=prepared)


def test_outline_skips_when_it_already_exists(with_outline):
    result = run_outline_stage(SLUG, llm=FakeLLMClient([]), paths=with_outline)
    assert result.skipped and result.valid


def test_outline_force_regenerates(with_outline):
    llm = FakeLLMClient([as_text("outline_pass.json")])
    result = run_outline_stage(SLUG, llm=llm, paths=with_outline, force=True)
    assert not result.skipped
    assert len(llm.calls) == 1


# --- [1s] sceneplan ----------------------------------------------------------


def test_sceneplan_needs_the_outline(prepared):
    with pytest.raises(ScriptSessionError, match=r"\[1a\. outline\]"):
        run_sceneplan_stage(SLUG, llm=FakeLLMClient([]), paths=prepared)


def test_sceneplan_writes_the_contract_file(with_outline):
    result = run_sceneplan_stage(
        SLUG, llm=FakeLLMClient([as_text("sceneplan_pass.json")]), paths=with_outline
    )

    assert result.valid
    assert result.path == with_outline.topic_dir(SLUG) / "08-sceneplan.json"
    assert len(result.plan["scenes"]) == 25


def test_sceneplan_prompt_carries_the_outline_and_the_vocabulary(with_outline):
    """연출은 자유 기술이 아니라 어휘에서의 선택이다 (ADR-0033 §3)."""
    llm = FakeLLMClient([as_text("sceneplan_pass.json")])
    run_sceneplan_stage(SLUG, llm=llm, paths=with_outline)

    prompt = llm.calls[0]["prompt"]
    assert "통짜가 아니다" in prompt, "구성안의 단 이름이 실린다"
    assert "aerial_diorama" in prompt and "slow_zoom_in" in prompt
    assert "kenburns" in prompt and "dissolve" in prompt


def test_sceneplan_rejects_sentences_in_says(with_outline):
    """`says`에 존댓말이 오면 `[1w]`가 할 일이 없어진다."""
    plan = load_fixture("sceneplan_pass.json")
    plan["scenes"][0]["says"] = "후버댐은 하나의 덩어리가 아닙니다"
    result = run_sceneplan_stage(
        SLUG, llm=FakeLLMClient([json.dumps(plan, ensure_ascii=False)]), paths=with_outline
    )

    assert not result.valid
    assert any("says" in e for e in result.errors)


def test_sceneplan_act_budget_must_match_the_outline(with_outline):
    plan = load_fixture("sceneplan_pass.json")
    plan["scenes"][0]["char_budget"] += 40
    result = run_sceneplan_stage(
        SLUG, llm=FakeLLMClient([json.dumps(plan, ensure_ascii=False)]), paths=with_outline
    )

    assert not result.valid
    assert any("단 예산" in e for e in result.errors)


def test_sceneplan_records_the_direction_distribution(with_outline):
    """ADR-0033 되돌릴 조건의 관측 수단. 판정하지 않고 세어 둔다."""
    run_sceneplan_stage(
        SLUG, llm=FakeLLMClient([as_text("sceneplan_pass.json")]), paths=with_outline
    )
    direction = state_of(with_outline, "1s-sceneplan")["direction"]

    assert direction["scene_count"] == 25
    assert direction["framing"].get("(없음)") is None, "픽스처는 전 씬이 구도를 골랐다"
    assert direction["emphasis"] == 5


def test_sceneplan_summary_counts_the_blank_framings(with_outline):
    plan = load_fixture("sceneplan_pass.json")
    for scene in plan["scenes"]:
        scene.pop("framing", None)
    result = run_sceneplan_stage(
        SLUG, llm=FakeLLMClient([json.dumps(plan, ensure_ascii=False)]), paths=with_outline
    )

    assert result.valid, "구도를 비우는 것은 오류가 아니다 (기본값으로 떨어진다)"
    assert "구도 미선택 25씬" in result.summary


# --- [1w] write --------------------------------------------------------------


def test_write_needs_the_plan(with_outline):
    with pytest.raises(ScriptSessionError, match=r"\[1s\. sceneplan\]"):
        run_write_stage(SLUG, llm=FakeLLMClient([]), paths=with_outline)


def test_write_produces_a_valid_candidate(with_plan):
    result = run_write_stage(
        SLUG, llm=FakeLLMClient([as_text("write_session.json")]), paths=with_plan
    )

    assert result.valid, result.errors
    assert result.candidate_path.name == "01.json"
    assert len(result.scenes["scenes"]) == 25


def test_write_copies_the_picture_and_direction_fields(with_plan):
    """씬 구조는 계획에서만 온다 — 세션은 `text`만 냈다."""
    result = run_write_stage(
        SLUG, llm=FakeLLMClient([as_text("write_session.json")]), paths=with_plan
    )
    plan = load_fixture("sceneplan_pass.json")

    for planned, written in zip(plan["scenes"], result.scenes["scenes"]):
        for field in ("beat", "visual_goal", "subject", "subject_scale",
                      "framing", "transition", "camera", "motion"):
            assert written.get(field) == planned.get(field)
        assert "says" not in written and "char_budget" not in written


def test_write_prompt_hides_the_picture_fields(with_plan):
    """그림 지시를 보여 주면 자막에 섞여 들어온다. 문장에 필요한 것만 준다."""
    llm = FakeLLMClient([as_text("write_session.json")])
    run_write_stage(SLUG, llm=llm, paths=with_plan)

    prompt = llm.calls[0]["prompt"]
    assert "후버댐이 한 덩어리 콘크리트가 아니라는 사실" in prompt, "says는 준다"
    assert "drone_wide" not in prompt and "협곡을 가로막은 후버댐 전경" not in prompt


def test_write_prompt_carries_the_signature_phrases(with_plan):
    llm = FakeLLMClient([as_text("write_session.json")])
    run_write_stage(SLUG, llm=llm, paths=with_plan)
    assert "그래서 발상을 뒤집습니다." in llm.calls[0]["prompt"]


def test_write_computes_the_timestamps(with_plan):
    """산술은 세션이 아니라 코드가 한다 (ADR-0014)."""
    result = run_write_stage(
        SLUG, llm=FakeLLMClient([as_text("write_session.json")]), paths=with_plan
    )
    scenes = result.scenes["scenes"]

    assert scenes[0]["est_start"] == 0.0
    assert all(
        b["est_start"] == pytest.approx(a["est_end"]) for a, b in zip(scenes, scenes[1:])
    )
    assert result.scenes["total_duration"] == scenes[-1]["est_end"]


def test_write_rejects_a_missing_scene(with_plan):
    session = load_fixture("write_session.json")
    session["scenes"] = session["scenes"][:20]
    with pytest.raises(ScriptSessionError, match="21번 씬"):
        run_write_stage(
            SLUG, llm=FakeLLMClient([json.dumps(session, ensure_ascii=False)]),
            paths=with_plan,
        )


def test_write_ignores_fields_the_session_should_not_send(with_plan):
    """씬을 바꿀 수단을 주지 않는다 — 얹어 보내도 계획이 이긴다."""
    session = load_fixture("write_session.json")
    session["scenes"][0]["subject"] = "세션이 바꾸려 한 피사체"
    session["scenes"][0]["beat"] = "ending_echo"
    result = run_write_stage(
        SLUG, llm=FakeLLMClient([json.dumps(session, ensure_ascii=False)]), paths=with_plan
    )

    assert result.scenes["scenes"][0]["subject"] == "협곡을 가로막은 후버댐 전경"
    assert result.scenes["scenes"][0]["beat"] == "hook_fact"


def test_build_scenes_refuses_to_write_when_the_copy_drifts():
    """계획과 대본이 어긋나면 파일을 쓰지 않는다 (ADR-0029).

    계획에서 만들므로 정상 경로에서는 절대 걸리지 않는다. 걸린다면 이 함수가 틀린
    것이고, 그때 조용히 넘어가면 그림 계획이 소리 없이 바뀐다.
    """
    plan = load_fixture("sceneplan_pass.json")
    texts = {s["scene_id"]: s["text"] for s in load_fixture("write_session.json")["scenes"]}
    doc = build_scenes(plan, texts, run_id=RUN_ID, topic=TOPIC)
    assert doc["scenes"][0]["subject"]

    plan["scenes"][0]["scene_id"] = 99
    with pytest.raises(ScriptSessionError, match="계획과 대본이 어긋난다|비어 있다"):
        build_scenes(plan, texts, run_id=RUN_ID, topic=TOPIC)
