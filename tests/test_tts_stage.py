"""[3. tts+sync] 단계 계약 (specs/05-pipeline.md, ADR-0004/0013/0017).

입력은 실물 대본 2편(피사 25씬 / 후버댐 27씬)이다. 확인 대상:
- 산출물 3종이 전부 `runs/{run_id}/` 아래에 떨어진다
- `06-script.json`은 물론 `topics/` 아래 무엇도 건드리지 않는다 (ADR-0017)
- 1부의 나머지 산출물(topic.json·팩트시트·후보) 없이도 끝까지 돈다
- 총 길이 102초 초과 시 멈추고, 대본을 다시 만들라고 리포트한다
- 실측-추정 오차 ±1.5초 초과는 경고일 뿐 차단하지 않는다
"""

import json

import pytest

from conftest import HOOVER, PISA, install_script, load_script
from shorts_factory.schemas.timed_scenes import validate_timed_scenes
from shorts_factory.stages.tts import (
    MAX_TOTAL_SECONDS,
    STAGE,
    TTSStageError,
    run_tts_stage,
)
from shorts_factory.tts.base import Alignment, Narration, TTSError
from shorts_factory.tts.fake import (
    DEFAULT_RAW_SPEED,
    FakeFFmpeg,
    FakeTTSClient,
    fake_alignment,
    fake_narration,
)
from shorts_factory.tts.sync import narration_text


def run(paths, slug=PISA, *, tts=None, ffmpeg=None, **kwargs):
    return run_tts_stage(
        slug,
        tts=tts or FakeTTSClient(),
        paths=paths,
        runner=ffmpeg or FakeFFmpeg(),
        **kwargs,
    )


@pytest.fixture
def pisa(paths):
    """경계면 파일 하나만 놓인 격리 루트 (ADR-0017)."""
    install_script(paths, PISA)
    return paths


def state_of(paths, run_id):
    data = json.loads((paths.run_dir(run_id) / "state.json").read_text(encoding="utf-8"))
    return data["stages"][STAGE]


# --- 통과 경로 ---------------------------------------------------------------


@pytest.mark.parametrize("slug", [PISA, HOOVER])
def test_stage_produces_the_three_contract_files(paths, slug):
    install_script(paths, slug)
    result = run(paths, slug)

    assert result.passed
    source = load_script(slug)
    run_dir = paths.run_dir(source["run_id"])
    assert result.narration_path == run_dir / "narration.wav"
    assert result.timing_path == run_dir / "timing.json"
    assert result.scenes_path == run_dir / "scenes.timed.json"
    for path in (result.narration_path, result.timing_path, result.scenes_path):
        assert path.exists()
    assert result.scene_count == len(source["scenes"])


def test_run_id_comes_from_the_script_not_from_part_one_artifacts(pisa):
    """topic.json도 팩트시트도 없는 상태다. 계보는 대본의 run_id가 잇는다."""
    result = run(pisa)

    assert result.run_id == load_script(PISA)["run_id"]
    assert not (result.run_dir / "topic.json").exists()


def test_script_is_a_single_call_with_every_line_joined(pisa):
    """ADR-0004: 대본 전체를 단일 호출로. 문장별 분할 호출은 톤이 끊긴다."""
    tts = FakeTTSClient()
    run(pisa, tts=tts)

    assert len(tts.calls) == 1
    texts = [s["text"] for s in load_script(PISA)["scenes"]]
    assert tts.calls[0]["text"] == narration_text(texts)
    assert tts.calls[0]["label"] == STAGE


def test_timed_scenes_use_measured_field_names(pisa):
    result = run(pisa)
    timed = json.loads(result.scenes_path.read_text(encoding="utf-8"))

    assert validate_timed_scenes(timed) == ([], [])
    first = timed["scenes"][0]
    assert "start" in first and "end" in first
    assert "est_start" not in first and "est_end" not in first
    assert timed["total_duration"] == timed["scenes"][-1]["end"]


def test_timed_scenes_carry_the_script_untouched(pisa):
    result = run(pisa)
    timed = json.loads(result.scenes_path.read_text(encoding="utf-8"))
    source = load_script(PISA)

    assert [s["text"] for s in timed["scenes"]] == [s["text"] for s in source["scenes"]]
    assert [s["beat"] for s in timed["scenes"]] == [s["beat"] for s in source["scenes"]]
    assert [s["camera"] for s in timed["scenes"]] == [s["camera"] for s in source["scenes"]]


def test_timestamps_are_scaled_by_one_over_tempo(pisa):
    """specs/05: atempo 1.1 적용 후 타임스탬프도 1/1.1 스케일 보정."""
    result = run(pisa, tempo=1.1)

    assert result.total_duration == pytest.approx(result.raw_duration / 1.1, abs=0.01)
    timing = json.loads(result.timing_path.read_text(encoding="utf-8"))
    assert timing["tempo"] == 1.1
    assert timing["raw_duration"] > timing["total_duration"]


def test_narration_length_matches_the_scene_timeline(pisa):
    result = run(pisa)

    assert result.audio_duration == pytest.approx(result.total_duration, abs=0.5)
    assert not any("narration.wav 길이" in w for w in result.warnings)


def test_timing_json_carries_cues_for_the_subtitle_stage(pisa):
    """[9. assemble]이 자막(ASS)을 만들 때 보는 파일이다 (specs/05)."""
    result = run(pisa)
    timing = json.loads(result.timing_path.read_text(encoding="utf-8"))
    source = load_script(PISA)

    assert [c["scene_id"] for c in timing["cues"]] == [
        s["scene_id"] for s in source["scenes"]
    ]
    assert [c["text"] for c in timing["cues"]] == [s["text"] for s in source["scenes"]]
    assert timing["cues"][0]["start"] == 0.0
    for before, after in zip(timing["cues"], timing["cues"][1:]):
        assert after["start"] == before["end"]


def test_measured_timing_lands_near_the_estimate_for_a_nominal_voice(pisa):
    """명목 속도로 읽으면 1부 추정과 어긋나지 않는다 — 오차 경고가 뜨지 않는다."""
    result = run(pisa)

    assert result.warnings == []
    assert result.total_duration == pytest.approx(
        load_script(PISA)["total_duration"], abs=0.5
    )


def test_state_records_the_outputs(pisa):
    result = run(pisa)
    stage = state_of(pisa, result.run_id)

    assert stage["status"] == "done"
    assert stage["scene_count"] == 25
    assert sorted(stage["outputs"]) == [
        f"runs/{result.run_id}/narration.wav",
        f"runs/{result.run_id}/scenes.timed.json",
        f"runs/{result.run_id}/timing.json",
    ]


# --- 읽기 전용 경계 (ADR-0017) -----------------------------------------------


def test_stage_never_writes_under_topics(pisa):
    topic_dir = pisa.topic_dir(PISA)
    before = {p.name: p.read_bytes() for p in topic_dir.rglob("*") if p.is_file()}

    run(pisa)

    after = {p.name: p.read_bytes() for p in topic_dir.rglob("*") if p.is_file()}
    assert after == before
    assert list(after) == ["06-script.json"]


def test_estimates_in_the_script_are_not_updated(pisa):
    """specs/02: est_start/est_end는 갱신되지 않는다."""
    run(pisa)
    assert json.loads(
        (pisa.topic_dir(PISA) / "06-script.json").read_text(encoding="utf-8")
    ) == load_script(PISA)


# --- 재시작 ------------------------------------------------------------------


def test_second_run_skips_and_does_not_call_the_engine(pisa):
    run(pisa)
    tts = FakeTTSClient()
    again = run(pisa, tts=tts)

    assert again.skipped and again.passed
    assert tts.calls == [], "TTS는 편당 과금이다. 완료된 단계를 다시 사지 않는다"
    assert again.total_duration > 0


def test_force_reruns_the_engine(pisa):
    run(pisa)
    tts = FakeTTSClient()
    again = run(pisa, tts=tts, force=True)

    assert not again.skipped
    assert len(tts.calls) == 1


def test_missing_output_defeats_the_skip(pisa):
    result = run(pisa)
    result.scenes_path.unlink()

    tts = FakeTTSClient()
    again = run(pisa, tts=tts)
    assert not again.skipped
    assert len(tts.calls) == 1


# --- 길이 초과 (specs/05: 리포트하고 멈춘다) ---------------------------------


@pytest.fixture
def slow_voice():
    """대본은 그대로인데 낭독이 느려 102초를 넘기는 경우."""
    return FakeTTSClient(speed=4.8)


def test_over_length_stops_before_writing_the_scene_contract(pisa, slow_voice):
    result = run(pisa, tts=slow_voice)

    assert result.over_length
    assert not result.passed
    assert result.total_duration > MAX_TOTAL_SECONDS
    assert not (result.run_dir / "scenes.timed.json").exists()


def test_over_length_removes_a_stale_contract_from_an_earlier_run(pisa, slow_voice):
    """옛 타임스탬프가 새 오디오와 짝이 맞지 않는 채로 남으면 하류가 그대로 쓴다."""
    first = run(pisa)
    assert first.scenes_path.exists()

    again = run(pisa, tts=slow_voice, force=True)

    assert again.over_length
    assert not (again.run_dir / "scenes.timed.json").exists()


def test_over_length_keeps_the_audio_it_paid_for(pisa, slow_voice):
    result = run(pisa, tts=slow_voice)

    assert result.narration_path.exists()
    assert result.timing_path.exists()


def test_over_length_reports_that_part_one_must_shorten_the_script(pisa, slow_voice):
    result = run(pisa, tts=slow_voice)
    stage = state_of(pisa, result.run_id)

    assert stage["status"] == "failed"
    assert "대본 축약" in stage["error"]
    assert "1부" in stage["error"]
    assert "축약" in result.summary


def test_over_length_does_not_regenerate_the_script(pisa, slow_voice):
    """2부는 1부를 다시 돌리지 않는다 (ADR-0017 단방향 경계)."""
    run(pisa, tts=slow_voice)

    topic_dir = pisa.topic_dir(PISA)
    assert [p.name for p in topic_dir.rglob("*") if p.is_file()] == ["06-script.json"]


# --- 오차 경고 ---------------------------------------------------------------


def test_drift_beyond_the_tolerance_warns_but_still_produces_the_contract(pisa):
    result = run(pisa, tts=FakeTTSClient(speed=6.4))

    assert result.passed, "오차는 경고다. 대본 품질은 1부 소관이라 여기서 막지 않는다"
    assert result.warnings
    assert any("허용 ±1.5초" in w for w in result.warnings)
    assert json.loads(result.timing_path.read_text(encoding="utf-8"))["warnings"]


# --- 실패 ---------------------------------------------------------------------


def test_missing_script_points_at_part_one(paths):
    with pytest.raises(TTSStageError, match=r"\[2\. validate\]"):
        run(paths)


def test_script_that_breaks_the_scene_contract_is_refused(pisa):
    path = pisa.topic_dir(PISA) / "06-script.json"
    broken = load_script(PISA)
    broken["scenes"][3]["camera"] = "dolly_zoom"
    path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(TTSStageError, match="씬 계약"):
        run(pisa)


def test_alignment_that_does_not_match_the_script_fails_loudly(pisa):
    """정렬이 밀리면 영상 전체의 싱크가 깨진다. 관용적으로 맞추지 않는다."""
    def wrong(text: str) -> Narration:
        return fake_narration(text.replace("1989", "천구백팔십구"))

    with pytest.raises(TTSStageError, match="정렬이 보낸 대본과 다르다") as exc:
        run(pisa, tts=FakeTTSClient([wrong]))

    assert "normalized_alignment" in str(exc.value)
    run_id = load_script(PISA)["run_id"]
    assert state_of(pisa, run_id)["status"] == "failed"
    assert not (pisa.run_dir(run_id) / "scenes.timed.json").exists()


def test_ffmpeg_failure_fails_the_stage(pisa):
    with pytest.raises(TTSStageError, match="FFmpeg 실패"):
        run(pisa, ffmpeg=FakeFFmpeg(returncode=1, stderr="no such filter"))

    run_id = load_script(PISA)["run_id"]
    assert state_of(pisa, run_id)["status"] == "failed"
    assert (pisa.run_dir(run_id) / "narration.raw.wav").exists()


def test_engine_error_propagates(pisa):
    """호출 실패는 어댑터가 재시도할 몫이지 이 단계가 삼킬 것이 아니다."""
    with pytest.raises(TTSError, match="한도"):
        run(pisa, tts=FakeTTSClient([TTSError("사용 한도 초과")]))


def test_line_without_sentence_punctuation_is_reported_as_a_warning(pisa):
    path = pisa.topic_dir(PISA) / "06-script.json"
    edited = load_script(PISA)
    edited["scenes"][0]["text"] = edited["scenes"][0]["text"].rstrip(".")
    path.write_text(json.dumps(edited, ensure_ascii=False), encoding="utf-8")

    result = run(pisa)

    assert result.passed
    assert any("문장부호로 끝나지 않아" in w for w in result.warnings)


# --- 페이크 자체 --------------------------------------------------------------


def test_fake_default_speed_lands_on_the_nominal_rate_after_atempo():
    """페이크의 원속은 atempo 1.1을 거치면 1부의 명목 5.85자/초가 된다."""
    assert DEFAULT_RAW_SPEED * 1.1 == pytest.approx(5.85)


def test_fake_alignment_covers_the_text_exactly():
    alignment = fake_alignment("가나 다.")
    assert alignment.text == "가나 다."
    assert isinstance(alignment, Alignment)
