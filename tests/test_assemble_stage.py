"""[9. assemble] 단계 계약 (specs/05-pipeline.md, specs/03, ADR-0002/0013/0017/0020).

입력은 run 디렉터리에 있는 것 둘뿐이다 — `scenes.timed.json`과 `clips/{scene_id}.mp4`.
확인 대상:

- 씬의 `text`·`start`·`end`·`beat`을 `scenes.timed.json`에서만 읽는다 (ADR-0020)
- 산출물 2종(`subtitles.ass`·`timeline.mp4`)이 전부 `runs/{run_id}/` 아래에 떨어진다
- `topics/` 아래에는 아무것도 쓰지 않는다 (ADR-0017)
- 싱크 오차 ±200ms를 FFmpeg를 부르기 **전에** 검증한다
- 클립이 없으면 구멍 난 타임라인을 만들지 않고 멈춘다
"""

import json

import pytest
from conftest import HOOVER, PISA, install_script
from timed_fixtures import install_run, timed_document

from shorts_factory.cli import parse_args
from shorts_factory.stages.assemble import (
    STAGE,
    AssembleStageError,
    resolve_run_id,
    run_assemble_stage,
)
from shorts_factory.video.fake import FakeFFmpeg
from shorts_factory.video.subtitles import parse_ass


def run(paths, run_id, *, ffmpeg=None, **kwargs):
    return run_assemble_stage(
        run_id, paths=paths, runner=ffmpeg or FakeFFmpeg(), **kwargs
    )


@pytest.fixture
def pisa(paths):
    """`[3]`과 `[7]`이 끝난 run 디렉터리."""
    run_id, document = install_run(paths, PISA)
    return paths, run_id, document


def state_of(paths, run_id):
    data = json.loads((paths.run_dir(run_id) / "state.json").read_text(encoding="utf-8"))
    return data["stages"][STAGE]


# --- 통과 경로 ---------------------------------------------------------------


@pytest.mark.parametrize("slug", [PISA, HOOVER])
def test_stage_produces_the_subtitle_and_the_timeline(paths, slug):
    run_id, document = install_run(paths, slug)

    result = run(paths, run_id)

    assert result.passed
    run_dir = paths.run_dir(run_id)
    assert result.subtitles_path == run_dir / "subtitles.ass"
    assert result.timeline_path == run_dir / "timeline.mp4"
    assert result.subtitles_path.exists() and result.timeline_path.exists()
    assert result.scene_count == len(document["scenes"])


def test_subtitles_come_from_the_scene_contract(pisa):
    paths, run_id, document = pisa
    result = run(paths, run_id)

    cues = parse_ass(result.subtitles_path.read_text(encoding="utf-8"))
    assert [c.text for c in cues] == [
        " ".join(s["text"].split()) for s in document["scenes"]
    ]


def test_transitions_follow_the_beat_rule(pisa):
    """specs/03 — 하드컷은 turning_point·hook_twist·dilemma_peak 진입 자리뿐이다."""
    paths, run_id, _document = pisa
    result = run(paths, run_id)

    assert result.cut_scene_ids == (3, 13, 14)
    assert result.dissolves + result.cuts == result.scene_count - 1


def test_sync_is_verified_within_the_spec_tolerance(pisa):
    paths, run_id, _document = pisa
    result = run(paths, run_id)

    assert result.max_drift <= 0.2
    assert state_of(paths, run_id)["max_drift"] == result.max_drift


def test_command_feeds_every_clip_in_scene_order(pisa):
    paths, run_id, document = pisa
    ffmpeg = FakeFFmpeg()
    run(paths, run_id, ffmpeg=ffmpeg)

    cmd = ffmpeg.last
    inputs = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-i"]
    assert inputs == [f"clips/{s['scene_id']}.mp4" for s in document["scenes"]]


def test_command_runs_inside_the_run_directory(pisa):
    paths, run_id, _document = pisa
    ffmpeg = FakeFFmpeg()
    run(paths, run_id, ffmpeg=ffmpeg)

    assert ffmpeg.calls[0]["kwargs"]["cwd"] == str(paths.run_dir(run_id))
    assert ffmpeg.last[-1] == "timeline.mp4"
    assert "subtitles.ass" in ffmpeg.graph


def test_state_records_the_outputs(pisa):
    paths, run_id, _document = pisa
    run(paths, run_id)
    stage = state_of(paths, run_id)

    assert stage["status"] == "done"
    assert stage["scene_count"] == 25
    assert stage["cuts"] == 3
    assert sorted(stage["outputs"]) == [
        f"runs/{run_id}/subtitles.ass",
        f"runs/{run_id}/timeline.mp4",
    ]


def test_hard_cut_scenes_are_left_for_the_audio_stage(pisa):
    """specs/04 — turning_point 진입에 차임을 맞춘다. 붙이는 것은 [10]이다."""
    paths, run_id, _document = pisa
    run(paths, run_id)

    assert state_of(paths, run_id)["cut_scene_ids"] == [3, 13, 14]


def test_summary_names_the_transitions_and_the_drift(pisa):
    paths, run_id, _document = pisa
    summary = run(paths, run_id).summary

    assert "디졸브" in summary and "하드컷" in summary
    assert "싱크 오차" in summary


# --- 경계 (ADR-0017 / ADR-0020) ----------------------------------------------


def test_stage_never_writes_under_topics(paths):
    install_script(paths, PISA)
    run_id, _document = install_run(paths, PISA)
    topic_dir = paths.topic_dir(PISA)
    before = {p.name: p.read_bytes() for p in topic_dir.rglob("*") if p.is_file()}

    run(paths, run_id)

    after = {p.name: p.read_bytes() for p in topic_dir.rglob("*") if p.is_file()}
    assert after == before
    assert list(after) == ["06-script.json"]


def test_stage_runs_without_any_other_contract_file(pisa):
    """`timing.json`도 `prompts.json`도 열지 않는다 (ADR-0020)."""
    paths, run_id, _document = pisa
    run_dir = paths.run_dir(run_id)
    assert not (run_dir / "timing.json").exists()
    assert not (run_dir / "prompts.json").exists()

    assert run(paths, run_id).passed


def test_stage_runs_without_the_topic_package(pisa):
    """run 디렉터리만으로 돈다. `--run-id`를 주면 대본조차 열지 않는다."""
    paths, run_id, _document = pisa
    assert not paths.topic_dir(PISA).exists()

    assert run(paths, run_id).passed


# --- run 찾기 ----------------------------------------------------------------


def test_run_id_can_be_read_from_the_boundary_script(paths):
    """ADR-0017 — 계보는 run_id로 잇는다. 1부의 topic.json을 뒤지지 않는다."""
    install_script(paths, PISA)

    assert resolve_run_id(paths, slug=PISA) == timed_document(PISA)["run_id"]


def test_explicit_run_id_wins(paths):
    assert resolve_run_id(paths, run_id="20260811-x", slug=PISA) == "20260811-x"


def test_missing_script_says_run_id_is_an_option(paths):
    with pytest.raises(AssembleStageError, match="--run-id"):
        resolve_run_id(paths, slug="없는-슬러그")


def test_neither_run_id_nor_slug_is_refused(paths):
    with pytest.raises(AssembleStageError):
        resolve_run_id(paths)


# --- 재시작 ------------------------------------------------------------------


def test_second_run_skips_and_does_not_re_encode(pisa):
    paths, run_id, _document = pisa
    run(paths, run_id)

    ffmpeg = FakeFFmpeg()
    again = run(paths, run_id, ffmpeg=ffmpeg)

    assert again.skipped and again.passed
    assert ffmpeg.calls == []
    assert again.cuts == 3


def test_force_re_encodes(pisa):
    paths, run_id, _document = pisa
    run(paths, run_id)

    ffmpeg = FakeFFmpeg()
    again = run(paths, run_id, ffmpeg=ffmpeg, force=True)

    assert not again.skipped
    assert len(ffmpeg.calls) == 1


def test_missing_timeline_defeats_the_skip(pisa):
    paths, run_id, _document = pisa
    result = run(paths, run_id)
    result.timeline_path.unlink()

    ffmpeg = FakeFFmpeg()
    again = run(paths, run_id, ffmpeg=ffmpeg)

    assert not again.skipped
    assert len(ffmpeg.calls) == 1


# --- 입력이 없을 때 ----------------------------------------------------------


def test_missing_timed_scenes_points_at_the_tts_stage(paths):
    install_run(paths, PISA)
    run_id = timed_document(PISA)["run_id"]
    (paths.run_dir(run_id) / "scenes.timed.json").unlink()

    with pytest.raises(AssembleStageError, match=r"\[3\. tts\+sync\]"):
        run(paths, run_id)


def test_missing_clips_point_at_the_motion_stage(paths):
    run_id, _document = install_run(paths, PISA, clips=False)

    with pytest.raises(AssembleStageError, match=r"\[7\. motion\]"):
        run(paths, run_id)


def test_one_missing_clip_stops_the_stage(paths):
    run_id, _document = install_run(paths, PISA)
    (paths.run_dir(run_id) / "clips" / "7.mp4").unlink()

    with pytest.raises(AssembleStageError, match="7.mp4"):
        run(paths, run_id)


def test_broken_scene_contract_is_refused(paths):
    run_id, document = install_run(paths, PISA)
    document["scenes"][3]["beat"] = "montage"
    (paths.run_dir(run_id) / "scenes.timed.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(AssembleStageError, match="씬 계약"):
        run(paths, run_id)


def test_gap_between_scenes_stops_the_stage(paths):
    """`[3]`은 빈틈 없이 이어 붙인다. 구멍이 있으면 덮을 클립이 없다."""
    document = timed_document(PISA)
    for item in document["scenes"][5:]:
        item["start"] = round(item["start"] + 0.5, 3)
        item["end"] = round(item["end"] + 0.5, 3)
    document["total_duration"] = document["scenes"][-1]["end"]
    run_id, _ = install_run(paths, PISA, document=document)

    with pytest.raises(AssembleStageError, match="이어지지 않는다"):
        run(paths, run_id)


def test_scene_contract_from_another_run_is_refused(paths):
    """계보는 run_id로 잇는다 (ADR-0017). 남의 타임스탬프로 조립하면 통째로 어긋난다."""
    run_id, document = install_run(paths, PISA)
    document["run_id"] = "20260811-other"
    (paths.run_dir(run_id) / "scenes.timed.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(AssembleStageError, match="run_id"):
        run(paths, run_id)


def test_ffmpeg_failure_fails_the_stage(pisa):
    paths, run_id, _document = pisa

    with pytest.raises(AssembleStageError, match="FFmpeg 실패"):
        run(paths, run_id, ffmpeg=FakeFFmpeg(returncode=1, stderr="no such filter"))

    assert state_of(paths, run_id)["status"] == "failed"
    assert not (paths.run_dir(run_id) / "timeline.mp4").exists()


def test_failed_rerun_removes_the_stale_timeline(pisa):
    """옛 영상이 새 자막과 짝이 맞지 않는 채로 남으면 [10. mix]가 그대로 쓴다."""
    paths, run_id, _document = pisa
    first = run(paths, run_id)
    assert first.timeline_path.exists()

    with pytest.raises(AssembleStageError):
        run(paths, run_id, ffmpeg=FakeFFmpeg(returncode=1), force=True)

    assert not first.timeline_path.exists()


def test_subtitles_survive_a_failed_encode(pisa):
    """자막은 사람이 열어 고칠 수 있는 산출물이다. 인코딩 실패로 지우지 않는다."""
    paths, run_id, _document = pisa

    with pytest.raises(AssembleStageError):
        run(paths, run_id, ffmpeg=FakeFFmpeg(returncode=1))

    assert (paths.run_dir(run_id) / "subtitles.ass").exists()


# --- 폰트 (ADR-0002) ---------------------------------------------------------


def test_missing_subtitle_font_is_a_warning(pisa):
    paths, run_id, _document = pisa
    result = run(paths, run_id)

    assert any("자막 폰트가 없다" in w for w in result.warnings)


def test_repository_font_is_handed_to_libass(pisa):
    paths, run_id, _document = pisa
    fonts = paths.root / "assets" / "fonts"
    fonts.mkdir(parents=True)
    (fonts / "Pretendard-Bold.otf").write_bytes(b"fake-font")

    ffmpeg = FakeFFmpeg()
    result = run(paths, run_id, ffmpeg=ffmpeg)

    assert not any("자막 폰트가 없다" in w for w in result.warnings)
    assert "fontsdir=" in ffmpeg.graph


def test_long_lines_are_reported_but_do_not_stop_the_stage(pisa):
    """스펙 03(18자)과 스펙 01(43자)이 충돌하는 씬. 막지 않고 알린다."""
    paths, run_id, _document = pisa
    result = run(paths, run_id)

    assert result.passed
    assert any("상한 18자" in w for w in result.warnings)


# --- CLI ---------------------------------------------------------------------


def test_cli_takes_a_run_id_or_a_slug():
    args = parse_args(["assemble", "--run-id", "20260811-x"])
    assert args.run_id == "20260811-x" and args.slug is None
    assert parse_args(["assemble", "--slug", "abc"]).slug == "abc"


def test_cli_ffmpeg_path_is_overridable():
    assert parse_args(["assemble", "--run-id", "x", "--ffmpeg", "C:/bin/ffmpeg.exe"]
                      ).ffmpeg == "C:/bin/ffmpeg.exe"


def test_cli_resolves_the_run_and_prints_the_summary(paths, monkeypatch, capsys):
    import shorts_factory.cli as cli

    install_script(paths, PISA)
    run_id, _document = install_run(paths, PISA)
    seen = {}

    def stub(resolved, **kwargs):
        seen["run_id"] = resolved
        seen.update(kwargs)
        return run_assemble_stage(resolved, runner=FakeFFmpeg(), **kwargs)

    monkeypatch.setattr(cli, "run_assemble_stage", stub)
    code = cli.main(["assemble", "--slug", PISA, "--root", str(paths.root)])

    assert code == 0
    assert seen["run_id"] == run_id
    assert "[9]" in capsys.readouterr().out
