"""[7. videogen] 단계 계약 (specs/05-pipeline.md `[7]`, ADR-0056 결정 1·2·6).

페이크 프로바이더 + 페이크 FFmpeg + FakeOCR로 네트워크·FFmpeg 없이 전 경로를 돈다.
확인 대상:

- 클립 길이 = ceil(세 언어 최장 + 0.6), 3~10 클램프 / `{seconds}`가 채워진다
- 검수 사다리: 재생성 1회 → (info) RED 절 없는 재생성 `demoted_from: info` → 인접 재사용 `demoted_from: video`
- 기록은 씬마다 직렬로 쓰이고, 재실행은 `done` 씬을 다시 사지 않는다
- 프로바이더 전체 거절은 남은 씬을 시도하지 않고 멈춘다 (D-5)
"""

import json
import math
import time
from unittest import mock

import pytest
from conftest import PISA, load_script
from timed_fixtures import install_run, timed_document

from shorts_factory.config import write_text
from shorts_factory.jsonio import dump_json
from shorts_factory.llm.fake import FakeLLMClient
from shorts_factory.stages.prompt import build_prompts
from conftest import fake_plan
from shorts_factory.stages.videogen import (
    RECORD_FILE,
    REVIEW_DIR,
    REVIEW_FILE,
    STAGE,
    ProviderRefused,
    VideogenStageError,
    build_jobs,
    clip_seconds,
    parse_review,
    render_review_prompt,
    run_videogen_stage,
)
from shorts_factory.stages.videogen import END_FRAME_OCR, _Runner
from shorts_factory.video.fake import FakeFFmpeg
from shorts_factory.video.ocr import FakeOCR
from shorts_factory.videogen.base import (
    VideoGenError,
    VideoGenRateLimited,
    VideoProviderNotConfigured,
)
from shorts_factory.videogen.fake import STUB_MP4, FakeVideoClient

INFO = {"labels": ["4 mm"], "target": "the diameter of the hole", "annotation": "dimension"}


def install(paths, *, langs=None, info_scenes=(), slug=PISA):
    """`[3]`·`[3s]`·`[5]`가 끝난 run — scenes.json·prompts.json·scenes.timed.{lang}.json."""
    run_id, document = install_run(paths, slug, clips=False, langs=langs)
    run_dir = paths.run_dir(run_id)
    contract = load_script(slug)
    contract["run_id"] = run_id
    for scene in contract["scenes"]:
        if scene["scene_id"] in info_scenes:
            scene["info"] = dict(INFO)
    write_text(run_dir / "scenes.json", dump_json(contract))
    prompts = build_prompts(contract, fake_plan(contract), source_script="runs/x/scenes.json")
    write_text(run_dir / "prompts.json", dump_json(prompts))
    return run_id, document


def run(paths, run_id, *, client=None, review="none", ocr=None, llm=None, ffmpeg=None, **kwargs):
    return run_videogen_stage(
        run_id,
        client=client or FakeVideoClient(),
        paths=paths,
        review=review,
        ocr=ocr,
        detect=lambda: None,
        llm=llm,
        runner=ffmpeg or FakeFFmpeg(),
        sleep=lambda _s: None,
        backoff=0.0,
        **kwargs,
    )


def record_of(paths, run_id):
    return json.loads((paths.run_dir(run_id) / RECORD_FILE).read_text(encoding="utf-8"))


def review_of(paths, run_id):
    return json.loads((paths.run_dir(run_id) / REVIEW_FILE).read_text(encoding="utf-8"))


def state_of(paths, run_id):
    data = json.loads((paths.run_dir(run_id) / "state.json").read_text(encoding="utf-8"))
    return data["stages"][STAGE]


@pytest.fixture
def pisa(paths):
    run_id, document = install(paths)
    return paths, run_id, document


# --- 클립 길이 (스펙 05 [7]) ------------------------------------------------------


@pytest.mark.parametrize("durations, expected, clamped", [
    ([3.2], 4, None),          # 3.8 → 4
    ([2.0], 3, None),          # 2.6 → 3
    ([1.0], 3, "min"),         # 1.6 → 2 → 3
    ([4.4, 4.97], 6, None),    # 5.57 → 6
    ([9.3, 9.5], 10, None),    # 10.1 → 11? no: 9.5+0.6=10.1 → 11 → clamp 10
    ([9.5], 10, "max"),
    ([6.0], 7, None),          # 6.6 → 7
    ([5.4], 6, None),          # 6.0 exactly
])
def test_clip_seconds_is_the_ceiling_of_the_longest_language_plus_the_tail(durations, expected, clamped):
    seconds, how = clip_seconds(durations)
    assert seconds == expected
    if durations == [9.3, 9.5]:
        assert how == "max"
    else:
        assert how == clamped


def test_jobs_take_the_longest_language(pisa):
    paths, run_id, document = pisa
    install(paths, langs={"ja": 1.13, "en": 0.78})
    contract = json.loads((paths.run_dir(run_id) / "scenes.json").read_text(encoding="utf-8"))
    prompts = json.loads((paths.run_dir(run_id) / "prompts.json").read_text(encoding="utf-8"))
    timed = {
        lang: json.loads((paths.run_dir(run_id) / f"scenes.timed.{lang}.json").read_text(encoding="utf-8"))
        for lang in ("ko", "ja", "en")
    }

    jobs, warnings = build_jobs(contract, prompts, timed)

    first = document["scenes"][0]
    ko_len = first["end"] - first["start"]
    assert jobs[0].lang_seconds["ja"] == pytest.approx(ko_len * 1.13, abs=0.002)
    assert jobs[0].seconds == math.ceil(round(ko_len * 1.13, 3) + 0.6)
    assert f", {jobs[0].seconds} seconds long," in jobs[0].prompt
    assert "{seconds}" not in jobs[0].prompt
    assert warnings == []


def test_over_ten_seconds_is_clamped_with_a_warning(paths):
    document = timed_document(PISA)
    scene = document["scenes"][2]
    scene["end"] = round(scene["start"] + 9.8, 3)
    shift = scene["end"] - document["scenes"][3]["start"]
    for later in document["scenes"][3:]:
        later["start"] = round(later["start"] + shift, 3)
        later["end"] = round(later["end"] + shift, 3)
    document["total_duration"] = document["scenes"][-1]["end"]
    run_id, _ = install_run(paths, PISA, clips=False, document=document)
    install(paths)  # prompts + contract
    (paths.run_dir(run_id) / "scenes.timed.ko.json").write_text(dump_json(document), encoding="utf-8")

    client = FakeVideoClient()
    result = run(paths, run_id, client=client)

    call = next(c for c in client.calls if c["scene_id"] == 3)
    assert call["seconds"] == 10
    assert any("10초" in w for w in result.warnings)
    assert [s for s in record_of(paths, run_id)["scenes"] if s["scene_id"] == 3][0]["clamped"] == "max"


# --- 통과 경로 ------------------------------------------------------------------------


def test_stage_buys_one_clip_per_scene_and_writes_both_records(pisa):
    paths, run_id, document = pisa
    client = FakeVideoClient()
    ffmpeg = FakeFFmpeg()

    result = run(paths, run_id, client=client, ffmpeg=ffmpeg)

    assert result.passed and result.scene_count == len(document["scenes"])
    assert len(client.calls) == len(document["scenes"])
    run_dir = paths.run_dir(run_id)
    for scene in document["scenes"]:
        assert (run_dir / "clips" / f"{scene['scene_id']}.mp4").exists()
    record = record_of(paths, run_id)
    assert record["provider"] == "fake" and len(record["scenes"]) == len(document["scenes"])
    entry = record["scenes"][0]
    assert entry["status"] == "done" and entry["file"] == "clips/1.mp4"
    assert entry["demoted_from"] is None
    assert entry["attempts"] == 1 and entry["retries"] == 0
    assert set(entry) >= {"provider", "seconds", "lang_seconds", "wall_seconds", "request_ids"}
    assert review_of(paths, run_id)["scenes"][0]["final"] == "pass"
    assert state_of(paths, run_id)["status"] == "done"


def test_prompt_sent_to_the_provider_has_the_seconds_filled(pisa):
    paths, run_id, _document = pisa
    client = FakeVideoClient()
    run(paths, run_id, client=client)

    for call in client.calls:
        assert "{seconds}" not in call["prompt"]
        assert f", {call['seconds']} seconds long," in call["prompt"]
        assert 3 <= call["seconds"] <= 10


def test_clips_are_normalized_through_ffmpeg(pisa):
    """프로바이더 원본 → 규격(1080×1920·30fps·무음·정확한 길이) → clips/."""
    paths, run_id, _document = pisa
    ffmpeg = FakeFFmpeg()
    run(paths, run_id, ffmpeg=ffmpeg)

    normalizes = [c["cmd"] for c in ffmpeg.calls if "-vf" in c["cmd"] and "-an" in c["cmd"]]
    assert len(normalizes) == 25
    assert "tpad=stop_mode=clone" in ffmpeg.vf
    assert normalizes[0][-1].startswith(f"{REVIEW_DIR}/")


def test_no_review_means_no_frames(pisa):
    paths, run_id, _document = pisa
    ffmpeg = FakeFFmpeg()
    run(paths, run_id, ffmpeg=ffmpeg, review="none")
    assert not any("-frames:v" in c["cmd"] for c in ffmpeg.calls)


def test_stage_never_writes_under_topics(pisa):
    paths, run_id, _document = pisa
    run(paths, run_id)
    assert not paths.topic_dir(PISA).exists()


def test_korean_measurements_are_required(paths):
    run_id, _ = install(paths)
    (paths.run_dir(run_id) / "scenes.timed.ko.json").unlink()
    with pytest.raises(VideogenStageError, match="scenes.timed.ko.json"):
        run(paths, run_id)


def test_language_scene_count_mismatch_is_refused(paths):
    run_id, document = install(paths, langs={"ja": 1.13})
    ja_path = paths.run_dir(run_id) / "scenes.timed.ja.json"
    ja = json.loads(ja_path.read_text(encoding="utf-8"))
    ja["scenes"].pop()
    ja_path.write_text(dump_json(ja), encoding="utf-8")
    with pytest.raises(VideogenStageError, match="씬 수"):
        run(paths, run_id)


# --- 검수 사다리 ----------------------------------------------------------------------


def ocr_for(texts: dict[str, str], default: str = "") -> FakeOCR:
    return FakeOCR(texts, default=default)


@pytest.mark.skipif(
    not END_FRAME_OCR,
    reason="끝 프레임 OCR 게이트가 계약에서 꺼져 있다 (ADR-0068). `art` 라인에서 되살릴 때 이 테스트를 그대로 쓴다",
)
def test_ocr_gate_retries_once_then_reuses_a_neighbour(pisa):
    """일반 씬 3에 글자가 계속 보인다 → 재생성 1회 → 인접(앞) 씬 재사용."""
    paths, run_id, _document = pisa
    ocr = ocr_for({"3-1-end": "HOOVER", "3-2-end": "DAM"})
    client = FakeVideoClient()

    result = run(paths, run_id, client=client, review="ocr", ocr=ocr)

    assert result.passed
    scene3 = [s for s in record_of(paths, run_id)["scenes"] if s["scene_id"] == 3][0]
    assert scene3["attempts"] == 2 and scene3["retries"] == 1
    assert scene3["demoted_from"] == "video" and scene3["source_scene"] == 2
    assert scene3["provider"] == "reuse"
    assert sum(1 for c in client.calls if c["scene_id"] == 3) == 2
    review = [s for s in review_of(paths, run_id)["scenes"] if s["scene_id"] == 3][0]
    assert review["final"] == "demoted_video"
    assert [a["ocr"]["passed"] for a in review["attempts"]] == [False, False]
    assert any("재사용" in w for w in result.warnings)


@pytest.mark.skipif(
    not END_FRAME_OCR,
    reason="끝 프레임 OCR 게이트가 계약에서 꺼져 있다 (ADR-0068). `art` 라인에서 되살릴 때 이 테스트를 그대로 쓴다",
)
def test_info_scene_demotes_to_a_prompt_without_red(paths):
    """info 씬 5: 라벨이 두 번 안 읽힌다 → RED 절을 뺀 재생성 → 통과 (demoted_from: info)."""
    run_id, _ = install(paths, info_scenes=(5,))
    ocr = ocr_for({"5-1-end": "", "5-2-end": "4 rnm", "5-3-end": ""})
    client = FakeVideoClient()

    result = run(paths, run_id, client=client, review="ocr", ocr=ocr)

    assert result.passed
    calls = [c for c in client.calls if c["scene_id"] == 5]
    assert len(calls) == 3
    assert "RED:" in calls[0]["prompt"] and "RED:" in calls[1]["prompt"]
    assert "RED:" not in calls[2]["prompt"] and "numbers" in calls[2]["negative_prompt"]
    scene5 = [s for s in record_of(paths, run_id)["scenes"] if s["scene_id"] == 5][0]
    assert scene5["demoted_from"] == "info" and scene5["source_scene"] is None
    review = [s for s in review_of(paths, run_id)["scenes"] if s["scene_id"] == 5][0]
    assert review["final"] == "demoted_info"
    assert [a["variant"] for a in review["attempts"]] == ["info", "info", "no_red"]
    assert review["attempts"][1]["ocr"]["missing_labels"] == ["4 mm"]


def test_info_scene_passes_when_the_label_is_read(paths):
    run_id, _ = install(paths, info_scenes=(5,))
    ocr = ocr_for({"5-1-end": "4 MM"})
    client = FakeVideoClient()

    result = run(paths, run_id, client=client, review="ocr", ocr=ocr)

    scene5 = [s for s in record_of(paths, run_id)["scenes"] if s["scene_id"] == 5][0]
    assert scene5["demoted_from"] is None and scene5["attempts"] == 1
    assert result.demoted("info") == 0


@pytest.mark.skipif(
    not END_FRAME_OCR,
    reason="끝 프레임 OCR 게이트가 계약에서 꺼져 있다 (ADR-0068). `art` 라인에서 되살릴 때 이 테스트를 그대로 쓴다",
)
def test_info_scene_exhausting_the_ladder_reuses_a_neighbour(paths):
    run_id, _ = install(paths, info_scenes=(5,))
    ocr = ocr_for({"5-1-end": "", "5-2-end": "", "5-3-end": "OOPS", "5-4-end": "OOPS"})
    client = FakeVideoClient()

    run(paths, run_id, client=client, review="ocr", ocr=ocr)

    scene5 = [s for s in record_of(paths, run_id)["scenes"] if s["scene_id"] == 5][0]
    assert scene5["demoted_from"] == "video" and scene5["source_scene"] == 4
    assert sum(1 for c in client.calls if c["scene_id"] == 5) == 4


@pytest.mark.skipif(
    not END_FRAME_OCR,
    reason="끝 프레임 OCR 게이트가 계약에서 꺼져 있다 (ADR-0068). `art` 라인에서 되살릴 때 이 테스트를 그대로 쓴다",
)
def test_first_scene_reuses_the_next_one_when_nothing_is_earlier(pisa):
    paths, run_id, _document = pisa
    ocr = ocr_for({"1-1-end": "X1", "1-2-end": "X2"})

    run(paths, run_id, review="ocr", ocr=ocr)

    scene1 = record_of(paths, run_id)["scenes"][0]
    assert scene1["demoted_from"] == "video" and scene1["source_scene"] == 2


def test_generation_error_counts_as_a_failed_attempt(pisa):
    """프로바이더 오류(씬 단위)는 사다리의 한 칸이다 — 멈추지 않는다."""
    paths, run_id, _document = pisa
    responses = []
    for sid in range(1, 26):
        responses.append(VideoGenError("blip") if sid == 4 else STUB_MP4)
    client = FakeVideoClient(responses + [STUB_MP4] * 5, concurrency=1)

    result = run(paths, run_id, client=client, jobs=1)

    assert result.passed
    scene4 = [s for s in record_of(paths, run_id)["scenes"] if s["scene_id"] == 4][0]
    assert scene4["attempts"] == 1 and scene4["demoted_from"] is None
    review4 = [s for s in review_of(paths, run_id)["scenes"] if s["scene_id"] == 4][0]
    assert "blip" in review4["attempts"][0]["error"]


@pytest.mark.skipif(
    not END_FRAME_OCR,
    reason="끝 프레임 OCR 게이트가 계약에서 꺼져 있다 (ADR-0068). `art` 라인에서 되살릴 때 이 테스트를 그대로 쓴다",
)
def test_ocr_backend_absence_skips_the_gate_with_a_warning(pisa):
    """tesseract가 없으면 OCR 게이트만 빠지고 경고를 기록한다 — 실패가 아니다."""
    paths, run_id, _document = pisa
    result = run(paths, run_id, review="ocr", ocr=None)

    assert result.passed
    assert any("tesseract" in w for w in result.warnings)
    assert any("tesseract" in w for w in review_of(paths, run_id)["warnings"])
    assert review_of(paths, run_id)["ocr_backend"] is None


# --- 비전 검수 (씬당 1세션) -----------------------------------------------------------


def verdict(verdict="pass", reasons=(), pointed=None):
    return json.dumps({"verdict": verdict, "reasons": list(reasons), "target_pointed": pointed})


def test_full_review_runs_one_session_per_scene_with_the_frames(pisa):
    paths, run_id, document = pisa
    llm = FakeLLMClient([verdict()] * 25)

    result = run(paths, run_id, review="full", llm=llm, jobs=1, review_jobs=1)

    assert result.passed and len(llm.calls) == len(document["scenes"])
    first = llm.calls[0]
    assert first["allowed_tools"] == ("Read",)
    assert "1-1-start.png" in first["prompt"] and "1-1-end.png" in first["prompt"]
    assert first["label"] == f"{STAGE}:1"


def test_vision_prompt_carries_the_contract_fields_but_not_the_narration(pisa):
    """ADR-0038 — 판정 세션은 그림 목표만 본다. 대본 문장은 주지 않는다."""
    paths, run_id, document = pisa
    contract = json.loads((paths.run_dir(run_id) / "scenes.json").read_text(encoding="utf-8"))
    scene = contract["scenes"][0]
    prompt = render_review_prompt(
        topic="피사", scene_id=1,
        fields={"subject": scene["subject"], "subject_anchor": scene.get("subject_anchor", []),
                "visual_goal": scene["visual_goal"], "info": None},
        frames=["a.jpg", "b.jpg", "c.jpg"],
    )
    assert scene["subject"] in prompt and scene["visual_goal"] in prompt
    assert scene["text"] not in prompt
    assert "없어야" in prompt  # info 없는 씬 — 글자·숫자·빨강 금지


def test_vision_prompt_names_the_target_and_labels_for_info_scenes():
    prompt = render_review_prompt(
        topic="종", scene_id=5,
        fields={"subject": "종의 구멍", "subject_anchor": ["에밀레종"], "visual_goal": "구멍 지름", "info": INFO},
        frames=["a", "b", "c"],
    )
    assert "the diameter of the hole" in prompt and '"4 mm"' in prompt and "dimension" in prompt


def fix(subject="A single continuous span, one bridge only, seen from directly above.",
        target="settling on the middle of that one span"):
    """고쳐쓰기 세션 응답 (ADR-0067) — 세션이 쓰는 것은 단락뿐이다."""
    return json.dumps({"subject_prompt": subject, "camera_target": target})


def test_vision_fail_triggers_the_ladder(pisa):
    paths, run_id, _document = pisa
    # 세션은 씬 순서대로 소비된다 — 생성·검수 둘 다 동시 1이어야 그 순서가 선다
    # (ADR-0072로 검수가 별도 자원이 되어 기본값은 병렬이다). 씬 7은 두 번 판정받고 두 번 다 fail이며,
    # **그 사이에 고쳐쓰기 세션이 1회 낀다** (ADR-0067).
    responses = [verdict()] * 6 + [
        verdict("fail", ["기준 1: 엉뚱한 건물"]),
        fix(),
        verdict("fail", ["still wrong"]),
    ]
    responses += [verdict()] * 18
    llm = FakeLLMClient(responses)

    run(paths, run_id, review="full", llm=llm, jobs=1, review_jobs=1)

    scene7 = [s for s in record_of(paths, run_id)["scenes"] if s["scene_id"] == 7][0]
    # 옆 씬을 복사하지 않는다 — 그 씬의 첫 후보를 그대로 쓴다 (사람 결정 2026-08-25).
    assert scene7["demoted_from"] == "unreviewed"
    assert scene7["file"] == "clips/7.mp4"
    review7 = [s for s in review_of(paths, run_id)["scenes"] if s["scene_id"] == 7][0]
    assert review7["attempts"][0]["vision"]["verdict"] == "fail"
    assert "엉뚱한 건물" in review7["reasons"][0]


def test_retry_uses_a_prompt_revised_from_the_review(pisa):
    """같은 프롬프트를 두 번 던지지 않는다 (ADR-0067) — 기각 사유가 다음 시도로 간다."""
    paths, run_id, _document = pisa
    llm = FakeLLMClient([
        verdict("fail", ["기준 2: 두 파형의 파장이 같아 촘촘함 차이가 안 보인다"]),
        fix(subject="Two sine bands: exactly 12 crests on top and exactly 10 below."),
        verdict(),
    ] + [verdict()] * 30)
    client = FakeVideoClient()

    run(paths, run_id, client=client, review="full", llm=llm, jobs=1, review_jobs=1)

    first, second = [c for c in client.calls if c["scene_id"] == 1][:2]
    assert first["prompt"] != second["prompt"], "재시도가 같은 프롬프트였다"
    assert "exactly 12 crests" in second["prompt"]
    # 고쳐쓰기 세션은 기각 사유를 받아야 한다.
    fix_call = [c for c in llm.calls if c["label"].startswith("7-videogen:fix:")][0]
    assert "파장이 같아" in fix_call["prompt"]
    # 연출 골격은 여전히 코드·어휘의 것이다 (ADR-0033 §3).
    assert second["prompt"].startswith("FORMAT:") and "STAGING:" in second["prompt"]
    review1 = [s for s in review_of(paths, run_id)["scenes"] if s["scene_id"] == 1][0]
    assert set(review1["attempts"][0]["revision"]["changed"]) == {
        "subject_prompt", "camera_target",
    }
    assert review1["attempts"][0]["revision"]["reasons"]


def test_revision_that_breaks_the_contract_is_rolled_back(pisa):
    """고친 단락이 계약을 어기면 직전 단락으로 돌아간다 — 사다리를 막지 않는다 (D-5)."""
    paths, run_id, _document = pisa
    llm = FakeLLMClient([
        verdict("fail", ["기준 3: 기형"]),
        # 착지에 카메라 워크 단어를 넣었다 — promptplan이 막는 값이다.
        fix(target="then the camera pans right across the facade"),
        verdict(),
    ] + [verdict()] * 30)
    client = FakeVideoClient()

    run(paths, run_id, client=client, review="full", llm=llm, jobs=1, review_jobs=1)

    first, second = [c for c in client.calls if c["scene_id"] == 1][:2]
    assert first["prompt"] == second["prompt"], "계약을 어긴 단락이 프롬프트에 들어갔다"
    review1 = [s for s in review_of(paths, run_id)["scenes"] if s["scene_id"] == 1][0]
    assert review1["attempts"][0]["revision"]["rejected"]


def test_revision_is_skipped_when_prompts_json_has_no_parts(pisa):
    """옛 `prompts.json`(단락 없음)에서도 사다리는 그대로 돈다 — 세션을 부르지 않는다."""
    paths, run_id, _document = pisa
    path = paths.run_dir(run_id) / "prompts.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    for scene in document["scenes"]:
        for key in ("subject_prompt", "camera_target", "red_prompt", "subject_prompt_shot2"):
            scene.pop(key, None)
    write_text(path, dump_json(document))
    llm = FakeLLMClient([verdict("fail", ["기준 3: 기형"]), verdict()] + [verdict()] * 30)

    run(paths, run_id, review="full", llm=llm, jobs=1, review_jobs=1)

    assert not [c for c in llm.calls if ":fix:" in c["label"]]


def test_vision_session_failure_passes_with_a_warning(pisa):
    """검수 불능은 클립의 잘못이 아니다 — 돈을 더 쓰지 않고 경고로 남긴다."""
    paths, run_id, _document = pisa
    responses = ["not json", "still not json"] + [verdict()] * 24
    llm = FakeLLMClient(responses)

    result = run(paths, run_id, review="full", llm=llm, jobs=1, review_jobs=1)

    assert result.passed
    scene1 = result.outcomes[0]
    assert scene1.demoted_from is None
    assert any("비전 세션 실패" in w for w in scene1.warnings)


def test_full_review_requires_a_session_client(pisa):
    paths, run_id, _document = pisa
    with pytest.raises(VideogenStageError, match="세션"):
        run(paths, run_id, review="full", llm=None)


def test_parse_review_treats_unknown_verdicts_as_fail():
    parsed = parse_review({"verdict": "maybe", "reasons": "one reason", "target_pointed": "yes"})
    assert parsed["verdict"] == "fail" and parsed["target_pointed"] is None
    assert "one reason" in parsed["reasons"][0]


# --- 재실행·직렬 기록 -----------------------------------------------------------------


def test_second_run_does_not_buy_done_scenes(pisa):
    paths, run_id, _document = pisa
    run(paths, run_id)

    client = FakeVideoClient()
    again = run(paths, run_id, client=client)

    assert again.skipped and client.calls == []


def test_rerun_buys_only_the_scenes_that_are_not_done(pisa):
    paths, run_id, _document = pisa
    run(paths, run_id)
    record = record_of(paths, run_id)
    record["scenes"][4]["status"] = "failed"
    (paths.run_dir(run_id) / RECORD_FILE).write_text(dump_json(record), encoding="utf-8")
    (paths.run_dir(run_id) / "clips" / "9.mp4").unlink()

    client = FakeVideoClient()
    again = run(paths, run_id, client=client)

    assert sorted(c["scene_id"] for c in client.calls) == [5, 9]
    assert again.passed and state_of(paths, run_id)["reused_from_previous_run"] == 23


def test_force_buys_everything_again(pisa):
    paths, run_id, _document = pisa
    run(paths, run_id)
    client = FakeVideoClient()
    run(paths, run_id, client=client, force=True)
    assert len(client.calls) == 25


def test_records_are_written_after_each_scene(pisa):
    """씬 하나가 끝날 때마다 직렬로 쓴다 — 중간에 죽어도 산 클립은 기록에 남는다."""
    paths, run_id, _document = pisa
    seen = []
    record_path = paths.run_dir(run_id) / RECORD_FILE

    def spy(request, **_):
        if record_path.exists():
            done = [s for s in json.loads(record_path.read_text(encoding="utf-8"))["scenes"] if s["status"] == "done"]
            seen.append(len(done))
        return STUB_MP4

    client = FakeVideoClient([spy] * 25, concurrency=1)
    # 생성·검수 둘 다 1이면 직렬이라 done 수가 한 씬씩 오른다 (ADR-0072).
    run(paths, run_id, client=client, jobs=1, review_jobs=1)

    assert seen[:3] == [0, 1, 2]


def test_the_record_is_never_read_half_written(pisa):
    """기록은 **원자적으로** 갈아 끼운다 — 병렬 갱신 중에 읽어도 찢어진 JSON이 아니다.

    씬이 동시에 끝나면 갱신이 잦아진다 (ADR-0072). `open("w")`는 파일을 먼저 비우므로
    그 틈에 읽는 쪽이 빈 파일이나 조각을 본다 — 진행 감시도 사람도 그걸 읽는다.
    """
    paths, run_id, _document = pisa
    record_path = paths.run_dir(run_id) / RECORD_FILE
    torn: list[str] = []

    def spy(request, **_):
        if record_path.exists():
            raw = record_path.read_text(encoding="utf-8")
            try:
                json.loads(raw)
            except json.JSONDecodeError:
                torn.append(raw[:80])
        return STUB_MP4

    client = FakeVideoClient([spy] * 40, concurrency=4)
    run(paths, run_id, client=client, jobs=4, review_jobs=4)

    assert not torn, f"쓰다 만 기록을 읽었다: {torn[:2]}"


# --- 프로바이더 전체 거절 (D-5) -------------------------------------------------------


def test_provider_refusal_stops_without_trying_the_rest(pisa):
    paths, run_id, _document = pisa
    client = FakeVideoClient([STUB_MP4, STUB_MP4, VideoProviderNotConfigured("plan")] + [STUB_MP4] * 30, concurrency=1)

    with pytest.raises(ProviderRefused, match="plan"):
        run(paths, run_id, client=client, jobs=1)

    assert len(client.calls) == 3, "남은 씬을 시도하지 않는다"
    record = record_of(paths, run_id)
    assert [s["status"] for s in record["scenes"][:2]] == ["done", "done"]
    assert state_of(paths, run_id)["status"] == "failed"
    assert (paths.run_dir(run_id) / "clips" / "1.mp4").exists()


def test_review_does_not_hold_a_generation_slot(paths):
    """생성과 검수는 다른 자원이다 — 검수 중인 씬이 프로바이더 슬롯을 쥐면 안 된다 (ADR-0072 결정 1).

    생성 동시 1로 묶고 검수를 느리게 만든 뒤, **검수가 도는 동안 생성이 나가는지** 본다.
    씬당 워커 하나가 [생성→검수]를 다 지던 구조에서는 이 겹침이 한 번도 일어나지 않는다.
    """
    import threading

    run_id, _ = install(paths)
    reviewing = threading.Event()
    overlapped: list[int] = []

    class Slow(FakeVideoClient):
        def generate(self, request, *, timeout=None):
            if reviewing.is_set():
                overlapped.append(request.scene_id)
            return super().generate(request, timeout=timeout)

    def slow_review(_self, _job, attempt, _variant, _candidate):
        reviewing.set()
        time.sleep(0.3)
        reviewing.clear()
        attempt["passed"] = True
        return True

    client = Slow([STUB_MP4] * 40, concurrency=1)
    with mock.patch.object(_Runner, "_review_attempt", slow_review):
        result = run_videogen_stage(
            run_id, client=client, paths=paths, review="none", detect=lambda: None,
            runner=FakeFFmpeg(), jobs=1, review_jobs=4,
        )

    assert result.passed
    assert overlapped, "검수가 도는 동안 생성이 한 번도 나가지 않았다 — 슬롯이 검수에 묶여 있다"


def test_rate_limit_backs_off_and_retries_serially(pisa):
    """429 → 워커 1 + 백오프 → 재시도 (스펙 05 [7])."""
    paths, run_id, _document = pisa
    responses = [VideoGenRateLimited("429")] + [STUB_MP4] * 25
    client = FakeVideoClient(responses, concurrency=4)
    slept = []

    result = run_videogen_stage(
        run_id, client=client, paths=paths, review="none", detect=lambda: None,
        runner=FakeFFmpeg(), sleep=slept.append, backoff=1.0, jobs=1,
    )

    assert result.passed
    assert slept == [1.0]
    assert state_of(paths, run_id)["rate_limited"] == 1
    assert record_of(paths, run_id)["scenes"][0]["rate_limited"] == 1


# --- 결과·CLI -----------------------------------------------------------------------


@pytest.mark.skipif(
    not END_FRAME_OCR,
    reason="끝 프레임 OCR 게이트가 계약에서 꺼져 있다 (ADR-0068). `art` 라인에서 되살릴 때 이 테스트를 그대로 쓴다",
)
def test_summary_counts_calls_seconds_and_demotions(paths):
    run_id, _ = install(paths, info_scenes=(5,))
    ocr = ocr_for({"5-1-end": "", "5-2-end": "", "3-1-end": "XX", "3-2-end": "XX"})
    result = run(paths, run_id, review="ocr", ocr=ocr)

    assert "호출 28회" in result.summary  # 23 + 씬 3 (2회) + 씬 5 (3회)
    assert "강등 info 1 · video 1" in result.summary
    assert result.purchased_seconds > 0


def test_cli_runs_the_stage_with_the_fake_provider(paths, monkeypatch, capsys):
    import shorts_factory.cli as cli
    from conftest import install_script

    install_script(paths, PISA)
    run_id, _ = install(paths)

    def stub(resolved, **kwargs):
        assert kwargs["review"] == "none"
        return run_videogen_stage(
            resolved, runner=FakeFFmpeg(), detect=lambda: None, **kwargs,
        )

    monkeypatch.setattr(cli, "run_videogen_stage", stub)
    code = cli.main([
        "videogen", "--slug", PISA, "--provider", "fake", "--review", "none",
        "--root", str(paths.root),
    ])

    assert code == 0
    assert "[7]" in capsys.readouterr().out
    assert (paths.run_dir(run_id) / "clips" / "1.mp4").exists()


# --- 프레임을 입력으로 받는 라인 (ADR-0070·0071) ------------------------------


def _write_frames(paths, run_id, *, line="art", scenes=None, run_key=None):
    """`[6]`의 산출물을 흉내 낸다 — `[7]`이 읽는 것은 주소 둘뿐이다."""
    document = {
        "run_id": run_key or run_id,
        "topic": "t",
        "line": line,
        "provider": "midjourney",
        "scenes": scenes if scenes is not None else [],
    }
    write_text(paths.run_dir(run_id) / "frames.json", dump_json(document))
    return document


def _all_scenes(paths, run_id, *, info_scene=None):
    contract = json.loads((paths.run_dir(run_id) / "scenes.json").read_text(encoding="utf-8"))
    return [
        {
            "scene_id": scene["scene_id"],
            "status": "done",
            "clean_url": f"https://pub-x.r2.dev/clean{scene['scene_id']}.png",
            "info_url": (
                "https://pub-x.r2.dev/info.png" if scene["scene_id"] == info_scene else None
            ),
        }
        for scene in contract["scenes"]
    ]


def test_a_frame_line_ships_clean_as_first_and_never_a_last(paths):
    """일반 씬만 CLEAN을 first로 받는다. **끝 그림은 이제 없다** (ADR-0075 결정 2).

    NB2가 만들던 INFO가 폐기됐고, 계측 표시를 싣는 씬은 텍스트→영상이라 프레임 자체를
    안 받는다 — `info` 씬에 프레임이 실리면 그 폐기가 안 된 것이다.
    """
    run_id, _document = install(paths, info_scenes=(3,))
    _write_frames(paths, run_id, scenes=_all_scenes(paths, run_id, info_scene=3))
    client = FakeVideoClient()
    run(paths, run_id, client=client, line="art")
    by_scene = {call["scene_id"]: call for call in client.calls}
    # info 씬은 프레임을 안 받는다 — 텍스트→영상이다.
    assert by_scene[3]["first_frame"] is None and by_scene[3]["last_frame"] is None
    # 일반 씬은 CLEAN 한 장을 first로 받고, 끝 그림은 어느 씬에도 없다.
    assert by_scene[1]["first_frame"].endswith("clean1.png")
    assert all(call["last_frame"] is None for call in client.calls)


def test_a_text_to_video_line_ships_no_frames(paths):
    run_id, _document = install(paths)
    client = FakeVideoClient()
    run(paths, run_id, client=client, line="local")
    assert all(
        call["first_frame"] is None and call["last_frame"] is None for call in client.calls
    )


def test_a_frame_line_without_the_frames_file_stops(paths):
    run_id, _document = install(paths)
    with pytest.raises(VideogenStageError) as exc:
        run(paths, run_id, line="art")
    assert "frames.json" in str(exc.value) and "[6]" in str(exc.value)


def test_a_scene_missing_its_clean_address_stops(paths):
    """CLEAN 없이 사면 라벨 없는 클립을 돈 주고 사게 된다."""
    run_id, _document = install(paths)
    scenes = _all_scenes(paths, run_id)
    scenes[2]["clean_url"] = None
    _write_frames(paths, run_id, scenes=scenes)
    with pytest.raises(VideogenStageError) as exc:
        run(paths, run_id, line="art")
    assert "CLEAN" in str(exc.value)


def test_frames_from_another_line_are_refused(paths):
    run_id, _document = install(paths)
    _write_frames(paths, run_id, line="local", scenes=_all_scenes(paths, run_id))
    with pytest.raises(VideogenStageError) as exc:
        run(paths, run_id, line="art")
    assert "라인" in str(exc.value)


def test_frames_from_another_run_are_refused(paths):
    run_id, _document = install(paths)
    _write_frames(paths, run_id, scenes=_all_scenes(paths, run_id), run_key="20260101-other")
    with pytest.raises(VideogenStageError) as exc:
        run(paths, run_id, line="art")
    assert "run_id" in str(exc.value)


def test_an_adapter_that_ignores_the_frames_is_refused(paths):
    """프레임을 실었는데 어댑터가 버리면 계측 표시가 조용히 사라진다 (ADR-0071)."""
    run_id, _document = install(paths, info_scenes=(3,))
    _write_frames(paths, run_id, scenes=_all_scenes(paths, run_id, info_scene=3))

    class TextOnly(FakeVideoClient):
        accepts_frames = False

    with pytest.raises(VideogenStageError) as exc:
        run(paths, run_id, client=TextOnly(), line="art")
    assert "안 받는다" in str(exc.value)
