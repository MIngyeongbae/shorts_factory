"""`[6] frames` 계약 (ADR-0071).

- 편집 지시는 **어휘에서** 조립된다 — 코드가 영어 문장을 짓지 않는다 (ADR-0034)
- `[5]`의 `red_prompt`를 편집 지시로 쓰지 않는다 (실측: 무대 어구가 그림을 갈아엎었다)
- 사분면은 q0로 시작하고 **재시도에서만** 바뀐다 (사람 결정 2026-08-25)
- INFO가 두 번 걸리면 그 씬은 CLEAN만 남기고 강등한다 — 부재가 곧 신호다
- MJ에 넘길 주소는 **공개 https**여야 한다. 아니면 잡을 사기 전에 멈춘다
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shorts_factory.imagegen.base import (
    GeneratedImage,
    ImageGenError,
    ProviderNotConfigured,
)
from shorts_factory.llm.fake import FakeLLMClient
from shorts_factory.runstate import RunState
from shorts_factory.schemas import promptplan, vocab
from shorts_factory.schemas import visual_rules as vr
from shorts_factory.stages import frames as frames_stage
from shorts_factory.stages.frames import (
    DEMOTED_INFO,
    DONE,
    FramesStageError,
    ProviderRefused,
    build_jobs,
    run_frames_stage,
)

PNG = b"\x89PNG\r\n\x1a\n" + bytes(64)
JPEG = b"\xff\xd8\xff" + bytes(64)
CLEAN_URL = "https://pub-x.r2.dev/attachments/clean.png"
INFO_URL = "https://pub-x.r2.dev/attachments/info.png"

#: 예산(28~38단어)을 채우는 소재 한 줄. 짧으면 계약이 정당히 거절한다.
MJ_SUBJECT = (
    "a wide braided river channel cutting through a dense low town, pale gravel bars, "
    "two truss road bridges, packed house roofs and factory sheds on both banks, "
    "seen from a high three-quarter angle"
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


def _prompts(scene_ids: list[int], run_id: str = "20260825-probe", *, mj: bool = True) -> dict:
    return {
        "run_id": run_id,
        "topic": "프로브",
        "source_script": "runs/x/scenes.json",
        "line": "art",
        "style": {
            "base_style": vocab.line_style("art"),
            "composition": vr.COMPOSITION,
            "aspect_ratio": vr.ASPECT_RATIO,
            "resolution": vr.RESOLUTION,
        },
        "scenes": [
            {
                "scene_id": sid,
                "beat": "context",
                "subject_scale": "diagram",
                "camera": "static",
                "staging": "studio",
                "staging_source": "scene",
                "framing": "frontal_diagram",
                "framing_source": "scene",
                "has_info": False,
                "prompt": "FORMAT: …",
                "negative_prompt": "logos",
                "subject_prompt": "…",
                **({promptplan.MJ_SUBJECT_FIELD: MJ_SUBJECT} if mj else {}),
            }
            for sid in scene_ids
        ],
    }


INFO = {
    "labels": ["Left bank", "Right bank"],
    "target": "which side of the river each bank of the city sits on",
    "annotation": "leader",
}


# --- 편집 지시는 어휘의 것이다 (ADR-0034) -------------------------------------


def test_edit_instruction_is_assembled_from_the_vocabulary():
    line = vr.build_edit_instruction(
        annotation="leader", target=INFO["target"], labels=INFO["labels"]
    )
    assert line.startswith(vocab.edit_preamble())
    assert line.endswith(vocab.annotation_closing())
    # 라벨은 따옴표째, 어휘의 잇는 문구로 이어진다.
    assert '"Left bank" and "Right bank"' in line
    assert INFO["target"] in line


def test_edit_instruction_uses_the_annotation_kind_the_contract_chose():
    dimension = vr.build_edit_instruction(
        annotation="dimension", target="the width of the channel", labels=["340 m"]
    )
    assert vocab.phrase("annotation", "dimension").split("{")[0] in dimension
    assert "leader line" not in dimension


def test_edit_instruction_refuses_values_outside_the_vocabulary():
    with pytest.raises(ValueError):
        vr.build_edit_instruction(annotation="halo", target="the arch", labels=["1 m"])


def test_edit_instruction_needs_a_target_and_a_label():
    with pytest.raises(ValueError):
        vr.build_edit_instruction(annotation="leader", target="  ", labels=["1 m"])
    with pytest.raises(ValueError):
        vr.build_edit_instruction(annotation="leader", target="the arch", labels=[])


def test_the_video_red_prompt_is_not_the_edit_instruction():
    """영상용 단락에는 무대 어구가 붙는다 — 그것을 편집 지시로 옮기면 그림이 상한다."""
    instruction = vr.build_edit_instruction(
        annotation="leader", target=INFO["target"], labels=INFO["labels"]
    )
    assert "Do not redraw" in instruction  # 편집 서두는 영상 프롬프트에 없는 문장이다
    prompt, _negative = vr.build_video_prompt(
        subject_prompt="A terrain model of a river.", staging="studio", camera="static",
        red_prompt='RED: one red leader line pointing at the bank that reads exactly "Left bank".',
    )
    assert vocab.edit_preamble() not in prompt


# --- 라인이 스타일을 프레임에 넘긴다 (ADR-0070) ------------------------------


def test_format_line_drops_the_style_when_the_frames_carry_it():
    assert vr.BASE_STYLE not in vr.format_line(style="")
    assert vr.BASE_STYLE in vr.format_line()


def test_a_frame_line_prompt_is_only_the_length_and_the_camera():
    """프레임이 그림을 진다 — 남는 말은 둘뿐이다. 길면 MJ가 다시 써서 잡이 죽는다 (ADR-0069)."""
    prompt, _negative = vr.build_video_prompt(
        subject_prompt="A terrain model of a river.", staging="studio",
        camera="tilt_down", camera_target="settling on the channel",
        red_prompt='RED: one red leader line that reads exactly "Left bank".',
        frames=True,
    )
    assert [line.split(":")[0] for line in prompt.split(chr(10))] == ["FORMAT", "CAMERA"]
    assert vr.BASE_STYLE not in prompt
    assert "A terrain model" not in prompt and "Left bank" not in prompt
    assert vr.SECONDS_PLACEHOLDER in prompt   # [7]이 초 수를 채우는 자리는 남는다
    assert len(prompt.split()) < vr.MJ_WORDS_MIN


def test_mj_subject_budget_is_the_leftover_of_the_line_style():
    low, high = vr.mj_subject_budget("art")
    style_words = len(vocab.line_style("art").split())
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


def test_build_jobs_puts_the_line_style_into_the_mj_line():
    jobs, _warnings = build_jobs(
        _contract([_scene(1), _scene(2, info=INFO)]), _prompts([1, 2]), line="art",
    )
    assert [job.scene_id for job in jobs] == [1, 2]
    assert vocab.line_style("art") in jobs[0].mj_prompt
    assert jobs[0].mj_prompt.endswith(tuple(vr.GLOBAL_NEGATIVES[-1:] + vr.NO_TEXT_NEGATIVES[-1:]))
    # CLEAN에는 어느 씬에서도 글자가 없어야 한다 — info 씬도 글자 금지를 단다.
    for job in jobs:
        for item in vr.NO_TEXT_NEGATIVES:
            assert item in job.mj_prompt


def test_build_jobs_only_writes_an_edit_instruction_for_info_scenes():
    jobs, _warnings = build_jobs(
        _contract([_scene(1), _scene(2, info=INFO)]), _prompts([1, 2]), line="art",
    )
    assert jobs[0].edit_instruction is None
    assert jobs[1].edit_instruction is not None
    assert '"Left bank" and "Right bank"' in jobs[1].edit_instruction


def test_build_jobs_stops_when_the_session_did_not_write_mj_subject():
    with pytest.raises(FramesStageError) as exc:
        build_jobs(_contract([_scene(1)]), _prompts([1], mj=False), line="art")
    assert promptplan.MJ_SUBJECT_FIELD in str(exc.value)


def test_build_jobs_stops_before_buying_anything_when_one_scene_breaks_the_budget():
    prompts = _prompts([1, 2])
    prompts["scenes"][1][promptplan.MJ_SUBJECT_FIELD] = "a river"
    with pytest.raises(vr.MJPromptError):
        build_jobs(_contract([_scene(1), _scene(2)]), prompts, line="art")


# --- 단계 실행 -----------------------------------------------------------------


class FakeImageClient:
    """MJ 프록시 표면 넷 — imagine · U 추출 · 내려받기 · 업로드."""

    name = "midjourney"

    def __init__(self, *, upload_url: str = INFO_URL, clean_url: str = CLEAN_URL) -> None:
        self.upload_url = upload_url
        self.clean_url = clean_url
        self.grids = 0
        self.upscales: list[int] = []
        self.uploads = 0

    def generate(self, request, *, timeout=None):
        self.grids += 1
        return GeneratedImage(data=PNG, mime_type="image/png", request_id=f"grid{request.scene_id}")

    def upscale(self, task_id, *, quadrant=0, timeout=None):
        self.upscales.append(quadrant)

        class _Ref:
            url = f"{self.clean_url}?q={quadrant}"

        return _Ref()

    def download(self, url, *, timeout=None):
        return PNG

    def upload(self, data, *, mime_type, timeout=None):
        self.uploads += 1
        if not self.upload_url.startswith("https://"):
            raise ProviderNotConfigured("업로드 주소가 https가 아니다")
        return self.upload_url

    def concurrency(self):
        return 1


class FakeEditor:
    name = "gemini-3.1-flash-image"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def edit(self, instruction, image_path, *, timeout=None):
        self.calls.append(instruction)
        return GeneratedImage(data=JPEG, mime_type="image/jpeg", model_id=self.name)


def _verdict(verdict: str = "pass", reasons: list[str] | None = None) -> str:
    return json.dumps({
        "verdict": verdict, "reasons": reasons or [], "target_pointed": verdict == "pass",
    })


def _setup(paths, scenes: list[dict], run_id: str = "20260825-probe") -> Path:
    run_dir = paths.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "scenes.json").write_text(
        json.dumps(_contract(scenes, run_id), ensure_ascii=False), encoding="utf-8"
    )
    (run_dir / "prompts.json").write_text(
        json.dumps(_prompts([s["scene_id"] for s in scenes], run_id), ensure_ascii=False),
        encoding="utf-8",
    )
    RunState.load_or_create(run_dir, run_id, topic="프로브")
    return run_dir


def test_a_scene_without_info_ends_with_one_image_and_no_session(paths):
    run_dir = _setup(paths, [_scene(1)])
    client, editor, llm = FakeImageClient(), FakeEditor(), FakeLLMClient([])
    result = run_frames_stage(
        "20260825-probe", client=client, editor=editor, paths=paths, llm=llm,
        line="art", jobs=1,
    )
    outcome = result.outcomes[0]
    assert outcome.status == DONE and outcome.clean_url and outcome.info_url is None
    assert editor.calls == [] and llm.calls == []
    assert client.upscales == [0]  # q0로 시작한다
    assert (run_dir / "frames" / "1-clean.png").exists()


def test_an_info_scene_gets_an_edited_frame_and_a_public_url(paths):
    _setup(paths, [_scene(1, info=INFO)])
    client, editor = FakeImageClient(), FakeEditor()
    llm = FakeLLMClient([_verdict()])
    result = run_frames_stage(
        "20260825-probe", client=client, editor=editor, paths=paths, llm=llm,
        line="art", jobs=1,
    )
    outcome = result.outcomes[0]
    assert outcome.info_url == INFO_URL and outcome.info_file.endswith(".jpg")
    assert outcome.labels == INFO["labels"]
    # 지시는 어휘에서 온 그것이다 — 세션이 쓴 red_prompt가 아니다.
    assert editor.calls[0].startswith(vocab.edit_preamble())


def test_the_review_session_sees_both_frames(paths):
    run_dir = _setup(paths, [_scene(1, info=INFO)])
    llm = FakeLLMClient([_verdict()])
    run_frames_stage(
        "20260825-probe", client=FakeImageClient(), editor=FakeEditor(), paths=paths,
        llm=llm, line="art", jobs=1,
    )
    call = llm.calls[0]
    assert "1-clean.png" in call["prompt"] and "1-info.jpg" in call["prompt"]
    assert call["allowed_tools"] == ("Read",)
    assert (run_dir / "frames") in call["add_dirs"]


def test_a_rejected_info_frame_is_remade_from_the_next_quadrant(paths):
    _setup(paths, [_scene(1, info=INFO)])
    client, editor = FakeImageClient(), FakeEditor()
    llm = FakeLLMClient([_verdict("fail", ["기준 2: 강폭이 아니라 도로를 잰다"]), _verdict()])
    result = run_frames_stage(
        "20260825-probe", client=client, editor=editor, paths=paths, llm=llm,
        line="art", jobs=1,
    )
    outcome = result.outcomes[0]
    assert client.upscales == [0, 1] and outcome.quadrant == 1
    assert client.grids == 1  # 그리드는 다시 사지 않는다 — U 추출이 공짜다
    assert outcome.status == DONE and outcome.info_url == INFO_URL
    assert result.quadrant_swaps == 1


def test_two_rejections_demote_the_scene_to_clean_only(paths):
    _setup(paths, [_scene(1, info=INFO)])
    client = FakeImageClient()
    llm = FakeLLMClient([_verdict("fail", ["기준 1"]), _verdict("fail", ["기준 1"])])
    result = run_frames_stage(
        "20260825-probe", client=client, editor=FakeEditor(), paths=paths, llm=llm,
        line="art", jobs=1,
    )
    outcome = result.outcomes[0]
    assert outcome.status == DONE and outcome.demoted_from == DEMOTED_INFO
    assert outcome.info_url is None and outcome.clean_url
    assert client.uploads == 0
    assert any("demoted_from: info" in w for w in outcome.warnings)


def test_a_broken_review_session_does_not_block_the_frame(paths):
    """검수기 고장으로 돈을 더 쓰지 않는다 — 경고와 함께 채택한다."""
    _setup(paths, [_scene(1, info=INFO)])
    llm = FakeLLMClient(["JSON이 아니다", "역시 아니다"])
    result = run_frames_stage(
        "20260825-probe", client=FakeImageClient(), editor=FakeEditor(), paths=paths,
        llm=llm, line="art", jobs=1,
    )
    outcome = result.outcomes[0]
    assert outcome.status == DONE and outcome.info_url == INFO_URL
    assert any("검수 세션 실패" in w for w in outcome.warnings)


def test_a_local_upload_address_stops_the_run_before_more_scenes(paths):
    _setup(paths, [_scene(1, info=INFO), _scene(2, info=INFO)])
    client = FakeImageClient(upload_url="http://localhost:8086/x.png")
    llm = FakeLLMClient([_verdict(), _verdict()])
    with pytest.raises(ProviderRefused):
        run_frames_stage(
            "20260825-probe", client=client, editor=FakeEditor(), paths=paths, llm=llm,
            line="art", jobs=1,
        )


def test_a_non_https_clean_address_is_refused(paths):
    _setup(paths, [_scene(1)])
    client = FakeImageClient(clean_url="http://localhost:8086/clean.png")
    with pytest.raises(ProviderRefused):
        run_frames_stage(
            "20260825-probe", client=client, editor=FakeEditor(), paths=paths,
            llm=FakeLLMClient([]), line="art", jobs=1,
        )


def test_the_stage_refuses_lines_that_do_not_take_frames(paths):
    _setup(paths, [_scene(1)])
    with pytest.raises(FramesStageError) as exc:
        run_frames_stage(
            "20260825-probe", client=FakeImageClient(), editor=FakeEditor(),
            paths=paths, llm=FakeLLMClient([]), line="local", jobs=1,
        )
    assert "style_in_frames" in str(exc.value)


def test_an_info_scene_without_an_editor_stops_the_stage(paths):
    _setup(paths, [_scene(1, info=INFO)])
    with pytest.raises(FramesStageError) as exc:
        run_frames_stage(
            "20260825-probe", client=FakeImageClient(), editor=None, paths=paths,
            llm=FakeLLMClient([]), line="art", jobs=1,
        )
    assert "편집 어댑터" in str(exc.value)


def test_a_second_run_does_not_buy_the_done_scenes_again(paths):
    _setup(paths, [_scene(1), _scene(2)])
    first = FakeImageClient()
    run_frames_stage(
        "20260825-probe", client=first, editor=FakeEditor(), paths=paths,
        llm=FakeLLMClient([]), line="art", jobs=1,
    )
    assert first.grids == 2
    second = FakeImageClient()
    result = run_frames_stage(
        "20260825-probe", client=second, editor=FakeEditor(), paths=paths,
        llm=FakeLLMClient([]), line="art", jobs=1,
    )
    assert second.grids == 0 and result.skipped


def test_the_record_carries_what_the_next_stage_reads(paths):
    run_dir = _setup(paths, [_scene(1), _scene(2, info=INFO)])
    run_frames_stage(
        "20260825-probe", client=FakeImageClient(), editor=FakeEditor(), paths=paths,
        llm=FakeLLMClient([_verdict()]), line="art", jobs=1,
    )
    document = json.loads((run_dir / frames_stage.RECORD_FILE).read_text(encoding="utf-8"))
    assert document["run_id"] == "20260825-probe" and document["line"] == "art"
    by_id = {e["scene_id"]: e for e in document["scenes"]}
    assert by_id[1]["clean_url"] and by_id[1]["info_url"] is None
    assert by_id[2]["info_url"] == INFO_URL


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
            "20260825-probe", client=client, editor=FakeEditor(), paths=paths,
            llm=FakeLLMClient([]), line="art", jobs=1,
        )
    assert "[1]" in str(exc.value)
    assert client.tried == [1, 2]  # 씬 하나가 죽어도 남은 씬은 돈다

