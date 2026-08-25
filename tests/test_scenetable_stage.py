"""[3s. scenetable] 단계 계약 (ADR-0049 §5).

세션은 페이크다 — 검증하는 것은 배선과 계약이다: 실측 줄이 프롬프트에 실리는가,
씬 경계를 세션이 바꿀 수 없는가, 병합본이 씬 계약(`scene.schema.json`)을 통과하는가,
실패 시 `scenes.json`을 쓰지 않는가 (ADR-0044 — 재생성 루프 없음).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from shorts_factory.config import write_text
from shorts_factory.jsonio import dump_json
from shorts_factory.schemas.scenes import LABEL_NUMBERS_FROM_LINE, validate_scenes
from shorts_factory.stages.scenetable import (
    STAGE,
    ScenetableStageError,
    resolve_run_id,
    run_scenetable_stage,
    timed_input_errors,
)

SLUG = "test-topic"
RUN_ID = "20260821-test-topic"

SCRIPT_MD = """# 테스트 소재

- 시드: https://ko.wikipedia.org/wiki/테스트
- 핵심 질문: 어떻게 넘는가?

## 대본

첫 줄입니다.
둘째 줄에는 2만 2천 명 넘는 희생이 나옵니다.
셋째 줄이 마무리합니다.
"""

FACTCHECK_MD = """# 팩트체크: 테스트 소재

| 줄 | 주장 | 판정 | 근거 (URL) |
|---|---|---|---|
| 2 | 사망 22,000명 이상 | 확인 | https://example.org/source |
"""


class FakeLLM:
    def __init__(self, payload):
        self.text = payload if isinstance(payload, str) else json.dumps(
            payload, ensure_ascii=False
        )
        self.prompts: list[str] = []
        self.tools: list[tuple[str, ...]] = []

    def run(self, prompt, *, allowed_tools=(), timeout=None, label="", **_kw):
        self.prompts.append(prompt)
        self.tools.append(tuple(allowed_tools))
        return SimpleNamespace(
            text=self.text,
            meta={"session_id": None, "num_turns": None, "duration_ms": None},
        )


def timed_document() -> dict:
    """새 경로의 `[3]` 산출 모양 — 줄과 실측 시각뿐이다 (specs/05 계약 표)."""
    return {
        "run_id": RUN_ID,
        "topic": "테스트 소재",
        "total_duration": 11.4,
        "scenes": [
            {"scene_id": 1, "text": "첫 줄입니다.", "start": 0.0, "end": 3.8},
            {"scene_id": 2, "text": "둘째 줄에는 2만 2천 명 넘는 희생이 나옵니다.",
             "start": 3.8, "end": 7.6},
            {"scene_id": 3, "text": "셋째 줄이 마무리합니다.", "start": 7.6, "end": 11.4},
        ],
    }


def table_payload() -> dict:
    return {
        "scenes": [
            {"scene_id": 1, "beat": "hook_fact", "visual_goal": "대상의 규모",
             "subject": "협곡의 댐 전경", "subject_anchor": ["Hoover Dam"],
             "subject_scale": "wide", "framing": "drone_wide",
             "camera": "slow_zoom_in"},
            {"scene_id": 2, "beat": "context", "visual_goal": "희생이 난 자리",
             "subject": "공사 현장", "subject_scale": "wide",
             "framing": "problem_wide", "transition": "dissolve", "camera": "static",
             "staging": "location",
             "info": {"labels": ["22,000+"], "target": "the workers lost on site",
                      "annotation": "leader"}},
            {"scene_id": 3, "beat": "ending_echo", "visual_goal": "완공 후의 모습",
             "subject": "완공된 댐", "subject_scale": "wide",
             "framing": "present_wide", "transition": "dissolve",
             "camera": "slow_zoom_out"},
        ]
    }


@pytest.fixture
def installed(paths):
    """새 편 하나 — topic.json(계보) + 실측 줄 + script.md + factcheck.md."""
    write_text(
        paths.run_dir(RUN_ID) / "topic.json",
        dump_json({"topic": "테스트 소재", "slug": SLUG, "run_id": RUN_ID}),
    )
    write_text(paths.run_dir(RUN_ID) / "scenes.timed.ko.json", dump_json(timed_document()))
    write_text(paths.topic_dir(SLUG) / "script.md", SCRIPT_MD)
    write_text(paths.topic_dir(SLUG) / "factcheck.md", FACTCHECK_MD)
    return paths


# --- 정상 경로 ---------------------------------------------------------------


def test_writes_a_valid_scene_contract(installed):
    llm = FakeLLM(table_payload())
    result = run_scenetable_stage(SLUG, llm=llm, paths=installed)

    assert result.passed and result.path.is_file()
    document = json.loads(result.path.read_text(encoding="utf-8"))
    errors, _ = validate_scenes(document)
    assert errors == []
    assert document["run_id"] == RUN_ID
    assert document["total_duration"] == 11.4


def test_text_and_time_come_from_the_measured_file(installed):
    """세션이 아니라 실측 파일이 문장·시각의 출처다 (ADR-0020)."""
    result = run_scenetable_stage(SLUG, llm=FakeLLM(table_payload()), paths=installed)

    scene = result.scenes["scenes"][1]
    assert scene["text"] == "둘째 줄에는 2만 2천 명 넘는 희생이 나옵니다."
    assert scene["est_start"] == 3.8 and scene["est_end"] == 7.6


def test_session_gets_no_tools_and_all_inputs_in_the_prompt(installed):
    """입력은 전부 프롬프트 주입이고 파일은 오케스트레이터가 쓴다 (ADR-0011)."""
    llm = FakeLLM(table_payload())
    run_scenetable_stage(SLUG, llm=llm, paths=installed)

    assert llm.tools == [()]
    prompt = llm.prompts[0]
    assert "첫 줄입니다." in prompt              # 실측 줄
    assert "핵심 질문" in prompt                 # script.md 머리
    assert "https://example.org/source" in prompt  # factcheck.md
    assert "hook_fact" in prompt                 # 어휘 (vocab.json에서)


def test_prompt_offers_staging_annotation_and_unit_vocabulary(installed):
    """ADR-0056 — 세션이 무대·계측 표시 방식·단위를 어휘에서 고른다. 값은 vocab.json에서 온다."""
    from shorts_factory.schemas import vocab

    llm = FakeLLM(table_payload())
    run_scenetable_stage(SLUG, llm=llm, paths=installed)
    prompt = llm.prompts[0]

    for name in ("staging", "annotation", "unit"):
        for value in vocab.values(name):
            assert f"`{value}`" in prompt, (name, value)
    assert "ASCII" in prompt
    assert "motion" not in prompt.replace("`motion`은 없다", "")
    assert "NB2" not in prompt and "인포씬 이미지" not in prompt


def test_run_id_resolves_from_topic_json(installed):
    assert resolve_run_id(installed, SLUG) == RUN_ID


def test_second_run_is_skipped(installed):
    run_scenetable_stage(SLUG, llm=FakeLLM(table_payload()), paths=installed)
    llm = FakeLLM(table_payload())
    result = run_scenetable_stage(SLUG, llm=llm, paths=installed)

    assert result.skipped and llm.prompts == []


# --- 인물 경로 (ADR-0051) ----------------------------------------------------


def _cast_payload() -> dict:
    payload = table_payload()
    payload["characters"] = [
        {"id": "stevens", "name": "스티븐스", "anchor": "John Frank Stevens",
         "appearance": "정장 조끼에 중절모를 쓴 중년 기술자"}
    ]
    payload["scenes"][0]["cast"] = ["stevens"]
    payload["scenes"][0]["framing"] = "figure_wide"
    payload["scenes"][1]["cast"] = ["stevens"]
    payload["scenes"][1]["framing"] = "figure_back"
    return payload


def test_characters_block_travels_into_the_contract(installed):
    result = run_scenetable_stage(SLUG, llm=FakeLLM(_cast_payload()), paths=installed)

    assert result.passed
    assert result.scenes["characters"][0]["id"] == "stevens"
    assert result.scenes["scenes"][0]["cast"] == ["stevens"]


def test_no_characters_means_no_block_at_all(installed):
    """인물이 없으면 블록 자체가 없다 — 그래야 인물 경로가 통째로 스킵된다 (D-3)."""
    result = run_scenetable_stage(SLUG, llm=FakeLLM(table_payload()), paths=installed)

    assert "characters" not in result.scenes


def test_cast_pointing_nowhere_fails_the_contract(installed):
    payload = _cast_payload()
    payload["scenes"][2]["cast"] = ["ghost"]
    result = run_scenetable_stage(SLUG, llm=FakeLLM(payload), paths=installed)

    assert not result.passed
    assert any("ghost" in e for e in result.errors)
    assert not (installed.run_dir(RUN_ID) / "scenes.json").exists()


# --- 씬 경계는 세션이 바꿀 수 없다 ------------------------------------------


def test_missing_scene_is_an_error(installed):
    payload = table_payload()
    del payload["scenes"][1]
    result = run_scenetable_stage(SLUG, llm=FakeLLM(payload), paths=installed)

    assert not result.passed
    assert any("빠뜨렸다" in e for e in result.errors)
    assert not (installed.run_dir(RUN_ID) / "scenes.json").exists()


def test_invented_scene_is_dropped_with_a_warning(installed):
    payload = table_payload()
    payload["scenes"].append(dict(payload["scenes"][2], scene_id=4))
    result = run_scenetable_stage(SLUG, llm=FakeLLM(payload), paths=installed)

    assert result.passed
    assert any("실측에 없는 씬" in w for w in result.warnings)
    assert len(result.scenes["scenes"]) == 3


def test_session_cannot_rewrite_the_text(installed):
    """연출표 스키마가 text를 막는다 — 대본은 [3s]의 것이 아니다."""
    payload = table_payload()
    payload["scenes"][0]["text"] = "지어낸 문장"
    result = run_scenetable_stage(SLUG, llm=FakeLLM(payload), paths=installed)

    assert not result.passed
    assert any("연출표" in e for e in result.errors)


# --- 검증 실패는 보고·중단, 파일은 쓰지 않는다 (ADR-0044) --------------------


def test_vocabulary_violation_reports_and_stops(installed):
    payload = table_payload()
    payload["scenes"][0]["framing"] = "dutch_angle"
    result = run_scenetable_stage(SLUG, llm=FakeLLM(payload), paths=installed)

    assert not result.passed
    assert any("framing" in e for e in result.errors)
    assert not (installed.run_dir(RUN_ID) / "scenes.json").exists()


def test_label_number_the_line_does_not_say_is_caught(installed):
    """화면 라벨의 숫자는 그 줄이 말하는 숫자다 (ADR-0060 결정 4) — 조절하는 쪽은 라벨이다.

    검사할지는 계약의 스위치가 정한다 (`script-rules.json` `checks.label_numbers_from_line`).
    꺼져 있으면 같은 입력이 통과해야 한다 — 듣는 숫자와 보는 숫자를 맞추는 일은 사람 판독이 진다.
    """
    timed = timed_document()
    timed["scenes"][1]["text"] = "둘째 줄은 숫자를 말하지 않습니다."
    write_text(installed.run_dir(RUN_ID) / "scenes.timed.ko.json", dump_json(timed))

    result = run_scenetable_stage(SLUG, llm=FakeLLM(table_payload()), paths=installed)

    assert result.passed is not LABEL_NUMBERS_FROM_LINE
    assert any("ADR-0060" in e for e in result.errors) is LABEL_NUMBERS_FROM_LINE


def test_visual_goal_that_restates_the_line_is_caught(installed):
    payload = table_payload()
    payload["scenes"][0]["visual_goal"] = "첫 줄입니다"
    result = run_scenetable_stage(SLUG, llm=FakeLLM(payload), paths=installed)

    assert not result.passed
    assert any("겹친다" in e for e in result.errors)


def test_failed_rerun_removes_the_stale_contract(installed):
    """지난 실행의 scenes.json이 남아 있으면 하류가 그대로 진행해 버린다."""
    run_scenetable_stage(SLUG, llm=FakeLLM(table_payload()), paths=installed)
    assert (installed.run_dir(RUN_ID) / "scenes.json").exists()

    bad = table_payload()
    del bad["scenes"][0]
    result = run_scenetable_stage(SLUG, llm=FakeLLM(bad), paths=installed, force=True)

    assert not result.passed
    assert not (installed.run_dir(RUN_ID) / "scenes.json").exists()


def test_non_json_session_output_raises(installed):
    with pytest.raises(ScenetableStageError):
        run_scenetable_stage(SLUG, llm=FakeLLM("연출표가 아니라 잡담"), paths=installed)


# --- 선택 입력과 경계 --------------------------------------------------------


def test_without_factcheck_the_stage_still_runs(installed):
    """factcheck.md 부재는 인포씬만 빼고 돈다 (D-3) — 검증 없는 수치는 화면에 못 나간다."""
    (installed.topic_dir(SLUG) / "factcheck.md").unlink()
    payload = table_payload()
    del payload["scenes"][1]["info"]

    llm = FakeLLM(payload)
    result = run_scenetable_stage(SLUG, llm=llm, paths=installed)

    assert result.passed
    assert any("인포씬" in w for w in result.warnings)
    assert "인포씬(`info`)을 하나도 지정하지 마라" in llm.prompts[0]


def test_without_script_md_raises(installed):
    """script.md가 없으면 돌 수 없다 — [1] draft가 먼저다 (ADR-0049)."""
    (installed.topic_dir(SLUG) / "script.md").unlink()
    with pytest.raises(ScenetableStageError):
        run_scenetable_stage(SLUG, llm=FakeLLM(table_payload()), paths=installed)


def test_without_measured_scenes_raises(installed):
    (installed.run_dir(RUN_ID) / "scenes.timed.ko.json").unlink()
    with pytest.raises(ScenetableStageError):
        run_scenetable_stage(SLUG, llm=FakeLLM(table_payload()), paths=installed)


def test_state_records_the_stage(installed):
    run_scenetable_stage(SLUG, llm=FakeLLM(table_payload()), paths=installed)
    state = json.loads(
        (installed.run_dir(RUN_ID) / "state.json").read_text(encoding="utf-8")
    )
    entry = state["stages"][STAGE]
    assert entry["status"] == "done"
    assert entry["direction_summary"]["scene_count"] == 3


# --- 실측 파일의 입력 계약 (이 단계가 읽는 필드만 — D-2) ----------------------


def test_timed_input_tolerates_extra_fields():
    """옛 경로의 실측 파일에는 씬 계약 필드가 더 있다 — 모르는 필드는 통과다 (D-2)."""
    timed = timed_document()
    timed["scenes"][0]["beat"] = "hook_fact"
    timed["scenes"][0]["camera"] = "static"
    assert timed_input_errors(timed) == []


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda t: t["scenes"].clear(), "비어"),
        (lambda t: t["scenes"][0].pop("text"), "text"),
        (lambda t: t["scenes"][1].update(start=1.0), "이르다"),
        (lambda t: t["scenes"][1].update(scene_id=7), "연번"),
        (lambda t: t["scenes"][0].update(start=3.8), ">="),
    ],
)
def test_broken_timed_input_is_rejected(mutate, needle):
    timed = timed_document()
    mutate(timed)
    assert any(needle in e for e in timed_input_errors(timed))


# --- 영상 라인과 2샷 (ADR-0059 결정 5) -------------------------------------------------


def _two_shot_payload() -> dict:
    payload = table_payload()
    payload["scenes"][0]["shot2"] = {"framing": "problem_closeup", "camera": "static"}
    return payload


def _write_line(paths, line: str) -> None:
    from shorts_factory.judgment import human_path
    write_text(human_path(paths, SLUG), json.dumps({"judge": "human", "decision": "go", "video_line": line}))


def _lines_by_shot2(flag: bool) -> list[str]:
    from shorts_factory.schemas import vocab
    return [v for v in vocab.values("video_line") if vocab.video_line_meta(v)["shot2"] is flag]


def test_line_without_two_shots_drops_shot2_and_says_so(installed):
    """기본 라인(로컬 H3)은 컷 시각이 ±0.5초라 2샷을 만들지 않는다 — 씬은 1샷으로 돈다."""
    lines = _lines_by_shot2(False)
    assert lines, "2샷을 끄는 라인이 어휘에 없다"
    _write_line(installed, lines[0])
    result = run_scenetable_stage(SLUG, llm=FakeLLM(_two_shot_payload()), paths=installed)

    assert result.passed
    assert "shot2" not in result.scenes["scenes"][0]
    assert any("shot2" in w and "1샷" in w for w in result.warnings)


def test_line_with_two_shots_keeps_shot2(installed):
    lines = _lines_by_shot2(True)
    assert lines, "2샷을 켜는 라인이 어휘에 없다"
    _write_line(installed, lines[0])
    result = run_scenetable_stage(SLUG, llm=FakeLLM(_two_shot_payload()), paths=installed)

    assert result.passed
    assert result.scenes["scenes"][0]["shot2"] == {"framing": "problem_closeup", "camera": "static"}


def test_merge_drops_shot2_only_when_told():
    from shorts_factory.stages.scenetable import merge_scenes

    kept, _, _ = merge_scenes(timed_document(), _two_shot_payload())
    dropped, _, warnings = merge_scenes(timed_document(), _two_shot_payload(), allow_shot2=False)
    assert "shot2" in kept["scenes"][0]
    assert "shot2" not in dropped["scenes"][0]
    assert len(warnings) == 1 and "씬 1" in warnings[0]


def test_bad_video_line_stops_the_stage(installed):
    _write_line(installed, "locl")
    with pytest.raises(ScenetableStageError, match="어휘 밖"):
        run_scenetable_stage(SLUG, llm=FakeLLM(table_payload()), paths=installed)

