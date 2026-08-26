"""`[6] frames` 계약 (ADR-0071, ADR-0075가 범위를 좁혔다).

- **`info` 씬은 job이 되지 않는다** — 텍스트→영상이라 프레임을 안 받는다 (결정 1·2).
  건너뛴 것은 강등이 아니라 정상이다
- MJ 한 줄은 **`[5]`의 완성본(`mj_image_prompt`)을 그대로 쓴다** — 여기서 다시 조립하면
  `[5]`가 잰 줄과 보내는 줄이 갈린다 (결정 3)
- 사분면은 q0로 시작하고 **CLEAN 게이트가 기각할 때만** 바뀐다 (사람 결정 2026-08-25)
- 여덟 장이 다 걸려도 CLEAN은 나간다 — `demoted_from: unreviewed` (ADR-0072의 마지막 칸)
- MJ에 넘길 주소는 **공개 https**여야 한다. 아니면 잡을 사기 전에 멈춘다
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shorts_factory.imagegen.base import (
    GeneratedImage,
    ImageGenError,
)
from shorts_factory.llm.fake import FakeLLMClient
from shorts_factory.runstate import RunState
from shorts_factory.schemas import promptplan, vocab
from shorts_factory.schemas import visual_rules as vr
from shorts_factory.stages import frames as frames_stage
from shorts_factory.stages.frames import (
    DEMOTED_UNREVIEWED,
    DONE,
    MJ_IMAGE_PROMPT_FIELD,
    FramesStageError,
    ProviderRefused,
    build_jobs,
    run_frames_stage,
)

PNG = b"\x89PNG\r\n\x1a\n" + bytes(64)
CLEAN_URL = "https://pub-x.r2.dev/attachments/clean.png"

#: 예산(28~38단어)을 채우는 소재 한 줄. 짧으면 계약이 정당히 거절한다.
MJ_SUBJECT = (
    "a wide braided river channel cutting through a dense low town, pale gravel bars, "
    "two truss road bridges, packed house roofs and factory sheds on both banks, "
    "seen from a high three-quarter angle"
)
#: `[5]`가 싣는 완성본 — 조립은 거기서 끝났고 `[6]`은 이 문자열을 그대로 보낸다.
MJ_LINE = vr.build_mj_prompt(
    subject=MJ_SUBJECT,
    mj_style=vocab.line_style("art", engine=vocab.MJ_ENGINE),
    negatives=vr.negative_items(has_info=False),
)


def _scene(scene_id: int, *, info: dict | None = None) -> dict:
    scene = {
        "scene_id": scene_id,
        "text": "줄",
        "est_start": 0.0,
        "est_end": 3.0,
        "beat": "context",
        "camera": "static",
        "framing": "frontal_diagram",
        "staging": "studio",
        "subject": "강",
        "subject_scale": "diagram",
        "visual_goal": "강이 도시를 가른다",
    }
    if info:
        scene["info"] = info
    return scene


def _contract(scenes: list[dict], run_id: str = "20260825-probe") -> dict:
    return {
        "run_id": run_id, "topic": "프로브", "total_duration": 60.0, "scenes": scenes,
    }


def _prompts(scenes: list[dict], run_id: str = "20260825-probe", *, mj: bool = True) -> dict:
    """`[5]`의 산출. **`info` 씬에는 MJ 필드가 없다** — 그 씬은 MJ를 타지 않는다."""
    entries = []
    for scene in scenes:
        has_info = bool(scene.get("info"))
        entry = {
            "scene_id": scene["scene_id"],
            "beat": "context",
            "subject_scale": "diagram",
            "camera": "static",
            "staging": "studio",
            "staging_source": "scene",
            "framing": "frontal_diagram",
            "framing_source": "scene",
            "has_info": has_info,
            "video_prompt": "FORMAT: …" if has_info else "slow push in toward the subject.",
            "negative_prompt": "logos",
            "subject_prompt": "…",
        }
        if not has_info and mj:
            entry[promptplan.MJ_SUBJECT_FIELD] = MJ_SUBJECT
            entry[MJ_IMAGE_PROMPT_FIELD] = MJ_LINE
        entries.append(entry)
    return {
        "run_id": run_id,
        "topic": "프로브",
        "source_script": "runs/x/scenes.json",
        "line": "art",
        "style": {
            "base_style": vocab.line_style("art", engine=vocab.TTV_ENGINE),
            "mj_style": vocab.line_style("art", engine=vocab.MJ_ENGINE),
            "composition": vr.COMPOSITION,
            "aspect_ratio": vr.ASPECT_RATIO,
            "resolution": vr.RESOLUTION,
        },
        "scenes": entries,
    }


INFO = {
    "labels": ["Left bank", "Right bank"],
    "target": "which side of the river each bank of the city sits on",
    "annotation": "leader",
}


# --- 라인이 스타일을 프레임에 넘긴다 (ADR-0070) ------------------------------


def test_format_line_drops_the_style_when_the_frames_carry_it():
    assert vr.BASE_STYLE not in vr.format_line(style="")
    assert vr.BASE_STYLE in vr.format_line()


def _frame_prompt(**over):
    fields = dict(
        subject_prompt="A terrain model of a river.", staging="studio",
        camera="tilt_down", camera_target="settling on the channel",
        red_prompt=None, frames=True,
    )
    fields.update(over)
    return vr.build_video_prompt(**fields)


def test_a_frame_line_prompt_is_only_the_camera_move():
    """프레임이 그림을 진다 — 남는 말은 카메라 워크와 그 착지뿐이다 (ADR-0072 결정 3).

    **착지는 남는다**: 결정 3의 "착지점은 last 프레임이 정한다"는 끝 그림이 있을 때의
    말이고, 일반 씬은 CLEAN 한 장만 준다 — 착지까지 빼면 카메라가 갈 곳을 아무도 말하지
    않아 피사체를 놓친다 (실측 2026-08-25, 일반 씬 6개 기각).
    """
    prompt, _negative = _frame_prompt()
    assert prompt.startswith(vr.CAMERA_PROMPTS["tilt_down"])
    assert "settling on the channel" in prompt
    # 표제 절도, 스타일도, 초 수 자리도 없다 — 규약 밖이다.
    for absent in ("FORMAT", "STAGING", "SUBJECT", "CAMERA:", "NEGATIVE", "9:16"):
        assert absent not in prompt
    assert vr.BASE_STYLE not in prompt and "A terrain model" not in prompt
    assert vr.SECONDS_PLACEHOLDER not in prompt
    assert len(prompt.split()) < vr.MJ_WORDS_MIN


def test_mj_subject_budget_is_the_leftover_of_the_mj_style():
    """예산이 재는 것은 **MJ 엔진의 룩**이다 — `ttv_style`은 이 한 줄에 안 눕는다 (ADR-0075 결정 7)."""
    low, high = vr.mj_subject_budget("art")
    style_words = len(vocab.line_style("art", engine=vocab.MJ_ENGINE).split())
    assert (low, high) == (vr.MJ_WORDS_MIN - style_words, vr.MJ_WORDS_MAX - style_words)


# --- `[5]`의 계약 (ADR-0071 결정 4) -------------------------------------------


def test_promptplan_requires_mj_subject_on_a_frame_line():
    contract = _contract([_scene(1)])
    plan = {"scenes": [{"scene_id": 1, "subject_prompt": "x" * 320, "camera_target": "on the bank"}]}
    errors = promptplan.cross_errors(plan, contract, line="art")
    assert any(promptplan.MJ_SUBJECT_FIELD in e for e in errors)
    # 프레임을 안 받는 라인에는 없어야 정상이다.
    assert not promptplan.cross_errors(plan, contract, line="local")


def test_promptplan_rejects_mj_subject_on_a_line_that_never_uses_it():
    contract = _contract([_scene(1)])
    plan = {
        "scenes": [{
            "scene_id": 1, "subject_prompt": "x" * 320, "camera_target": "on the bank",
            promptplan.MJ_SUBJECT_FIELD: MJ_SUBJECT,
        }]
    }
    assert any(
        promptplan.MJ_SUBJECT_FIELD in e
        for e in promptplan.cross_errors(plan, contract, line="local")
    )


def test_promptplan_measures_the_budget_on_the_assembled_line():
    contract = _contract([_scene(1)])
    plan = {
        "scenes": [{
            "scene_id": 1, "subject_prompt": "x" * 320, "camera_target": "on the bank",
            promptplan.MJ_SUBJECT_FIELD: "a river",
        }]
    }
    errors = promptplan.cross_errors(plan, contract, line="art")
    assert any("단어" in e for e in errors)


def test_mj_subject_may_not_mention_the_red_annotation():
    """CLEAN에는 글자도 빨강도 없다 — 다른 단락과 같은 규칙을 받는다 (ADR-0060)."""
    contract = _contract([_scene(1)])
    plan = {
        "scenes": [{
            "scene_id": 1, "subject_prompt": "x" * 320, "camera_target": "on the bank",
            promptplan.MJ_SUBJECT_FIELD: MJ_SUBJECT + ", a red arrow label",
        }]
    }
    assert any("계측 표시를 언급한다" in e for e in promptplan.cross_errors(plan, contract, line="art"))


# --- 작업 조립 -----------------------------------------------------------------


def test_build_jobs_sends_the_line_the_prompt_stage_assembled():
    """조립은 `[5]`가 끝냈다 — `[6]`은 그 문자열을 그대로 보낸다 (ADR-0075 결정 3)."""
    jobs, _warnings = build_jobs(_contract([_scene(1)]), _prompts([_scene(1)]), line="art")
    assert [job.scene_id for job in jobs] == [1]
    assert jobs[0].mj_prompt == MJ_LINE
    # 그 한 줄에는 라인의 MJ 룩과 글자 금지가 이미 들어 있다.
    assert vocab.line_style("art", engine=vocab.MJ_ENGINE) in jobs[0].mj_prompt
    for item in vr.NO_TEXT_NEGATIVES:
        assert item in jobs[0].mj_prompt
    # 원료도 함께 온다 — 고쳐쓰기 사다리가 고치는 것은 이쪽이다 (ADR-0067).
    assert jobs[0].mj_subject == MJ_SUBJECT


def test_an_info_scene_gets_no_job_at_all():
    """`info` 씬은 `[7]`에서 텍스트→영상으로 간다 — 만들 프레임이 없다 (ADR-0075 결정 1)."""
    scenes = [_scene(1), _scene(2, info=INFO)]
    jobs, warnings = build_jobs(_contract(scenes), _prompts(scenes), line="art")
    assert [job.scene_id for job in jobs] == [1]
    assert any("info 1씬은 건너뛴다" in w for w in warnings)
    # 건너뛴 것은 강등이 아니다 — 경고 문구가 그렇게 말한다.
    assert any("강등이 아니다" in w for w in warnings)


def test_every_scene_being_an_info_scene_is_not_an_error():
    """만들 CLEAN이 하나도 없어도 정상이다 — 경고 하나를 남기고 빈 목록을 낸다."""
    scenes = [_scene(1, info=INFO), _scene(2, info=INFO)]
    jobs, warnings = build_jobs(_contract(scenes), _prompts(scenes), line="art")
    assert jobs == []
    assert any("전 씬이 info" in w for w in warnings)


def test_build_jobs_stops_when_the_prompt_stage_did_not_assemble_the_mj_line():
    with pytest.raises(FramesStageError) as exc:
        build_jobs(_contract([_scene(1)]), _prompts([_scene(1)], mj=False), line="art")
    assert MJ_IMAGE_PROMPT_FIELD in str(exc.value)


def test_build_jobs_stops_when_a_scene_is_missing_from_the_prompt_stage():
    with pytest.raises(FramesStageError) as exc:
        build_jobs(_contract([_scene(1), _scene(2)]), _prompts([_scene(1)]), line="art")
    assert "[5]를 다시 돌려야 한다" in str(exc.value)


# --- 단계 실행 -----------------------------------------------------------------


class FakeImageClient:
    """MJ 프록시 표면 셋 — imagine · U 추출 · 내려받기. 업로드는 NB2와 함께 빠졌다."""

    name = "midjourney"

    def __init__(self, *, clean_url: str = CLEAN_URL) -> None:
        self.clean_url = clean_url
        self.grids = 0
        self.upscales: list[int] = []
        self.prompts: list[str] = []

    def generate(self, request, *, timeout=None):
        self.grids += 1
        self.prompts.append(request.prompt)
        return GeneratedImage(data=PNG, mime_type="image/png", request_id=f"grid{request.scene_id}")

    def upscale(self, task_id, *, quadrant=0, timeout=None):
        self.upscales.append(quadrant)

        class _Ref:
            url = f"{self.clean_url}?q={quadrant}"

        return _Ref()

    def download(self, url, *, timeout=None):
        return PNG

    def concurrency(self):
        return 1


def _verdict(verdict: str = "pass", reasons: list[str] | None = None) -> str:
    return json.dumps({"verdict": verdict, "reasons": reasons or []})


def _setup(paths, scenes: list[dict], run_id: str = "20260825-probe") -> Path:
    run_dir = paths.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "scenes.json").write_text(
        json.dumps(_contract(scenes, run_id), ensure_ascii=False), encoding="utf-8"
    )
    (run_dir / "prompts.json").write_text(
        json.dumps(_prompts(scenes, run_id), ensure_ascii=False), encoding="utf-8"
    )
    RunState.load_or_create(run_dir, run_id, topic="프로브")
    return run_dir


def test_a_scene_ends_with_one_clean_and_one_gate_session(paths):
    run_dir = _setup(paths, [_scene(1)])
    client, llm = FakeImageClient(), FakeLLMClient([_verdict()])
    result = run_frames_stage(
        "20260825-probe", client=client, paths=paths, llm=llm, line="art", jobs=1,
    )
    outcome = result.outcomes[0]
    assert outcome.status == DONE and outcome.clean_url
    assert client.upscales == [0]  # q0로 시작한다
    assert client.prompts == [MJ_LINE]  # [5]가 완성한 그 줄을 그대로 보냈다
    assert (run_dir / "frames" / "1-clean.png").exists()
    # 평시 세션은 씬당 1회 — CLEAN 게이트뿐이다 (INFO 검수는 폐기됐다).
    assert len(llm.calls) == 1
    assert "1-clean.png" in llm.calls[0]["prompt"]
    assert llm.calls[0]["allowed_tools"] == ("Read",)
    assert (run_dir / "frames") in llm.calls[0]["add_dirs"]


def test_all_info_scenes_end_the_stage_with_an_empty_record(paths):
    """만들 CLEAN이 하나도 없으면 오류가 아니라 정상 종료다 (ADR-0075 결정 2)."""
    run_dir = _setup(paths, [_scene(1, info=INFO), _scene(2, info=INFO)])
    client = FakeImageClient()
    result = run_frames_stage(
        "20260825-probe", client=client, paths=paths, llm=None, line="art", jobs=1,
    )
    assert client.grids == 0 and result.outcomes == []
    assert result.skipped_info == 2
    document = json.loads((run_dir / frames_stage.RECORD_FILE).read_text(encoding="utf-8"))
    assert document["scenes"] == []
    assert any("전 씬이 info" in w for w in document["warnings"])


def test_a_rejected_clean_is_remade_from_the_next_quadrant(paths):
    _setup(paths, [_scene(1)])
    client = FakeImageClient()
    llm = FakeLLMClient([_verdict("fail", ["1 — 계약은 강인데 화면에 도로만 있다"]), _verdict()])
    result = run_frames_stage(
        "20260825-probe", client=client, paths=paths, llm=llm, line="art", jobs=1,
    )
    outcome = result.outcomes[0]
    assert client.upscales == [0, 1] and outcome.quadrant == 1
    assert client.grids == 1  # 그리드는 다시 사지 않는다 — U 추출이 공짜다
    assert outcome.status == DONE and outcome.demoted_from is None
    assert result.quadrant_swaps == 1


def test_the_gate_lowers_its_standard_every_attempt(paths):
    """같은 잣대로 반복 기각하면 사다리가 안 닫힌다 (ADR-0072)."""
    _setup(paths, [_scene(1)])
    llm = FakeLLMClient([_verdict("fail", ["1"]), _verdict("fail", ["1"]), _verdict()])
    run_frames_stage(
        "20260825-probe", client=FakeImageClient(), paths=paths, llm=llm, line="art", jobs=1,
    )
    standards = [vocab.review_standard(i) for i in range(3)]
    for call, standard in zip(llm.calls, standards):
        assert standard in call["prompt"]


def test_every_quadrant_rejected_still_ships_the_last_clean(paths):
    """사다리 끝은 채택이다 — `[7]`은 first 프레임 없이 못 돈다 (ADR-0072의 마지막 칸)."""
    _setup(paths, [_scene(1)])
    client = FakeImageClient()
    # 사분면 넷 → 고쳐쓰기 실패 → 채택. 고쳐쓰기 세션이 빈 값을 내면 사다리가 끝난다.
    llm = FakeLLMClient(
        [_verdict("fail", ["1"])] * frames_stage.ATTEMPTS + [json.dumps({"mj_subject": ""})]
    )
    result = run_frames_stage(
        "20260825-probe", client=client, paths=paths, llm=llm, line="art", jobs=1,
    )
    outcome = result.outcomes[0]
    assert len(outcome.attempts) == frames_stage.ATTEMPTS
    assert [a["quadrant"] for a in outcome.attempts] == list(range(frames_stage.ATTEMPTS))
    assert outcome.status == DONE and outcome.clean_url
    assert outcome.demoted_from == DEMOTED_UNREVIEWED and result.unreviewed == 1
    assert any(DEMOTED_UNREVIEWED in w for w in outcome.warnings)


def test_a_broken_review_session_does_not_block_the_frame(paths):
    """검수기 고장으로 파이프라인을 세우지 않는다 — 경고와 함께 채택한다."""
    _setup(paths, [_scene(1)])
    llm = FakeLLMClient(["JSON이 아니다", "역시 아니다"])
    result = run_frames_stage(
        "20260825-probe", client=FakeImageClient(), paths=paths, llm=llm, line="art", jobs=1,
    )
    outcome = result.outcomes[0]
    assert outcome.status == DONE and outcome.clean_url
    assert any("CLEAN 검수 세션 실패" in w for w in outcome.warnings)


def test_the_gate_does_not_run_without_review(paths):
    _setup(paths, [_scene(1)])
    llm = FakeLLMClient([])
    result = run_frames_stage(
        "20260825-probe", client=FakeImageClient(), paths=paths, llm=llm,
        line="art", review=False, jobs=1,
    )
    assert result.outcomes[0].status == DONE and llm.calls == []


def test_a_non_https_clean_address_is_refused(paths):
    _setup(paths, [_scene(1)])
    client = FakeImageClient(clean_url="http://localhost:8086/clean.png")
    with pytest.raises(ProviderRefused):
        run_frames_stage(
            "20260825-probe", client=client, paths=paths,
            llm=FakeLLMClient([]), line="art", jobs=1,
        )


def test_the_stage_refuses_lines_that_do_not_take_frames(paths):
    _setup(paths, [_scene(1)])
    with pytest.raises(FramesStageError) as exc:
        run_frames_stage(
            "20260825-probe", client=FakeImageClient(), paths=paths,
            llm=FakeLLMClient([]), line="local", jobs=1,
        )
    assert "style_in_frames" in str(exc.value)


def test_a_second_run_does_not_buy_the_done_scenes_again(paths):
    _setup(paths, [_scene(1), _scene(2)])
    first = FakeImageClient()
    run_frames_stage(
        "20260825-probe", client=first, paths=paths,
        llm=FakeLLMClient([_verdict(), _verdict()]), line="art", jobs=1,
    )
    assert first.grids == 2
    second = FakeImageClient()
    result = run_frames_stage(
        "20260825-probe", client=second, paths=paths,
        llm=FakeLLMClient([]), line="art", jobs=1,
    )
    assert second.grids == 0 and result.skipped


def test_the_record_carries_what_the_next_stage_reads(paths):
    run_dir = _setup(paths, [_scene(1), _scene(2, info=INFO)])
    result = run_frames_stage(
        "20260825-probe", client=FakeImageClient(), paths=paths,
        llm=FakeLLMClient([_verdict()]), line="art", jobs=1,
    )
    document = json.loads((run_dir / frames_stage.RECORD_FILE).read_text(encoding="utf-8"))
    assert document["run_id"] == "20260825-probe" and document["line"] == "art"
    by_id = {e["scene_id"]: e for e in document["scenes"]}
    # `info` 씬은 아예 기록에 없다 — `[7]`이 프레임 없이 그린다.
    assert list(by_id) == [1] and by_id[1]["clean_url"]
    assert result.skipped_info == 1


def test_a_failed_clean_call_fails_the_scene_not_the_provider(paths):
    """씬 하나의 실패는 프로바이더 거절이 아니다 — 남은 씬은 계속 돈다."""
    _setup(paths, [_scene(1), _scene(2)])

    class Flaky(FakeImageClient):
        def __init__(self) -> None:
            super().__init__()
            self.tried: list[int] = []

        def generate(self, request, *, timeout=None):
            self.tried.append(request.scene_id)
            if request.scene_id == 1:
                raise ImageGenError("MJ가 프롬프트를 다시 썼다")
            return super().generate(request, timeout=timeout)

    client = Flaky()
    with pytest.raises(FramesStageError) as exc:
        run_frames_stage(
            "20260825-probe", client=client, paths=paths,
            llm=FakeLLMClient([_verdict()]), line="art", jobs=1,
        )
    assert "[1]" in str(exc.value)
    assert client.tried == [1, 2]  # 씬 하나가 죽어도 남은 씬은 돈다


def test_four_rejected_quadrants_buy_a_new_grid_from_a_fixed_subject(paths):
    """사분면 넷이 다 걸리면 사분면 운이 아니라 소재의 문제다 — 단락을 고쳐 새 그리드를 산다."""
    _setup(paths, [_scene(1)])
    client = FakeImageClient()
    fixed = json.dumps({
        "mj_subject": " ".join(["stone"] * 30),
        "changed": "척도 기준을 넣었다",
    })
    llm = FakeLLMClient(
        [_verdict("fail", ["1 — 척도가 계약과 모순"])] * frames_stage.ATTEMPTS
        + [fixed]
        + [_verdict("pass")]
    )
    result = run_frames_stage(
        "20260825-probe", client=client, paths=paths, llm=llm, line="art", jobs=1,
    )
    outcome = result.outcomes[0]
    # 그리드를 두 번 샀다 — 고쳐쓰기 뒤 한 번 더다 (여기서만 과금이 는다).
    assert client.grids == frames_stage.GRID_ROUNDS
    assert outcome.status == DONE and outcome.demoted_from is None
    assert any("소재 단락을 고쳐 새 그리드" in w for w in outcome.warnings)
    # 고쳐쓰기 세션이 받는 것은 **원료**다 — 완성본을 주면 스타일 나열과 플래그까지 고친다.
    fix_prompt = llm.calls[frames_stage.ATTEMPTS]["prompt"]
    assert MJ_SUBJECT in fix_prompt and "--ar" not in fix_prompt
    # 새로 산 그리드는 고친 단락으로 다시 조립한 줄이다.
    assert client.prompts[1] != MJ_LINE and "stone stone" in client.prompts[1]


def test_a_fixed_subject_that_breaks_the_budget_is_rolled_back(paths):
    """고친 단락도 예산·방언을 지켜야 한다 — 어기면 되돌리고 사다리를 끝낸다 (ADR-0069)."""
    _setup(paths, [_scene(1)])
    client = FakeImageClient()
    llm = FakeLLMClient(
        [_verdict("fail", ["1"])] * frames_stage.ATTEMPTS
        + [json.dumps({"mj_subject": "too short", "changed": "x"})]
    )
    result = run_frames_stage(
        "20260825-probe", client=client, paths=paths, llm=llm, line="art", jobs=1,
    )
    outcome = result.outcomes[0]
    assert client.grids == 1, "예산을 어긴 단락으로 그리드를 사지 않는다"
    assert outcome.demoted_from == DEMOTED_UNREVIEWED
    assert any("예산을 어겨 되돌린다" in w for w in outcome.warnings)
