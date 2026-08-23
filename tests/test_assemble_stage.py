"""[9. assemble] 단계 계약 (specs/05-pipeline.md, specs/03, ADR-0002/0013/0017/0020/0056).

입력은 run 디렉터리에 있는 것뿐이다 — `scenes.timed.{lang}.json`과 `clips/{scene_id}.mp4`.
확인 대상:

- 씬의 `text`·`start`·`end`·`beat`을 그 언어의 `scenes.timed.{lang}.json`에서만 읽는다 (ADR-0020)
- 산출물(`subtitles.{lang}.ass`·`timeline.{lang}.mp4`)이 전부 `runs/{run_id}/` 아래에 떨어진다
- `topics/` 아래에는 아무것도 쓰지 않는다 (ADR-0017)
- 싱크 오차 ±200ms를 FFmpeg를 부르기 **전에** 검증한다
- 클립이 없으면 구멍 난 타임라인을 만들지 않고 멈춘다
- **언어당 1회 돈다** — 같은 클립 풀, 언어별 트림·자막·나레이션 (ADR-0056 결정 5)
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
    assert result.subtitles_path == run_dir / "subtitles.ko.ass"
    assert result.timeline_path == run_dir / "timeline.ko.mp4"
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
    assert state_of(paths, run_id)["languages"]["ko"]["max_drift"] == result.max_drift


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
    assert ffmpeg.last[-1] == "timeline.ko.mp4"
    assert "subtitles.ko.ass" in ffmpeg.graph


def test_state_records_the_outputs(pisa):
    paths, run_id, _document = pisa
    run(paths, run_id)
    stage = state_of(paths, run_id)

    assert stage["status"] == "done"
    assert stage["scene_count"] == 25
    assert stage["languages"]["ko"]["cuts"] == 3
    assert sorted(stage["outputs"]) == [
        f"runs/{run_id}/subtitles.ko.ass",
        f"runs/{run_id}/timeline.ko.mp4",
    ]


def test_hard_cut_scenes_are_left_for_the_audio_stage(pisa):
    """specs/04 — hard_cut 진입에 thump를 맞춘다. 붙이는 것은 [10]이다."""
    paths, run_id, _document = pisa
    run(paths, run_id)

    assert state_of(paths, run_id)["languages"]["ko"]["cut_scene_ids"] == [3, 13, 14]


def test_summary_names_the_transitions_and_the_drift(pisa):
    paths, run_id, _document = pisa
    summary = run(paths, run_id).summary

    assert "디졸브" in summary and "하드컷" in summary
    assert "싱크 오차" in summary


# --- 경계 (ADR-0017 / ADR-0020) ----------------------------------------------


def test_stage_never_writes_under_topics(paths):
    run_id, _document = install_run(paths, PISA)
    contract = paths.run_dir(run_id) / "scenes.json"
    before = contract.read_bytes()

    run(paths, run_id)

    assert contract.read_bytes() == before
    assert not paths.topic_dir(PISA).exists()


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
    with pytest.raises(AssembleStageError, match="run이 없다"):
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
    (paths.run_dir(run_id) / "scenes.timed.ko.json").unlink()

    with pytest.raises(AssembleStageError, match=r"\[3\. tts\+sync\]"):
        run(paths, run_id)


def test_missing_clips_point_at_the_videogen_stage(paths):
    run_id, _document = install_run(paths, PISA, clips=False)

    with pytest.raises(AssembleStageError, match=r"\[7\. videogen\]"):
        run(paths, run_id)


def test_one_missing_clip_stops_the_stage(paths):
    run_id, _document = install_run(paths, PISA)
    (paths.run_dir(run_id) / "clips" / "7.mp4").unlink()

    with pytest.raises(AssembleStageError, match="7.mp4"):
        run(paths, run_id)


def test_broken_scene_contract_is_refused(paths):
    run_id, document = install_run(paths, PISA)
    document["scenes"][3]["beat"] = "montage"
    (paths.run_dir(run_id) / "scenes.timed.ko.json").write_text(
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
    (paths.run_dir(run_id) / "scenes.timed.ko.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(AssembleStageError, match="run_id"):
        run(paths, run_id)


def test_ffmpeg_failure_fails_the_stage(pisa):
    paths, run_id, _document = pisa

    with pytest.raises(AssembleStageError, match="FFmpeg 실패"):
        run(paths, run_id, ffmpeg=FakeFFmpeg(returncode=1, stderr="no such filter"))

    assert state_of(paths, run_id)["status"] == "failed"
    assert not (paths.run_dir(run_id) / "timeline.ko.mp4").exists()


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

    assert (paths.run_dir(run_id) / "subtitles.ko.ass").exists()


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


def test_the_real_script_raises_no_subtitle_warning(pisa):
    """피사는 예전 18자 상한에서 8씬이 경고를 냈다. 22자로 정한 뒤 0건이다."""
    paths, run_id, _document = pisa
    result = run(paths, run_id)

    assert result.passed
    assert not any("상한" in w for w in result.warnings)


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


# --- 엔딩 실사 컷 (ADR-0055) --------------------------------------------------
#
# `[9]`가 지는 것은 **붙이는 일**뿐이다. 무엇을 붙일지는 `[8]`이 이미 정했고,
# 자막·싱크 검증은 여전히 씬 구간만 본다.


def install_ending(paths, run_id, count=2, *, seconds=2.4, clips=True):
    """`[8]`이 끝난 상태 — `ending.json`과 렌더된 컷."""
    from shorts_factory.config import write_text
    from shorts_factory.jsonio import dump_json
    from shorts_factory.schemas import ending as ending_schema

    run_dir = paths.run_dir(run_id)
    photos = [
        {
            "index": i,
            "file": f"ending/{i}.mp4",
            "seconds": seconds,
            "source": f"refs/1/0{i}.jpg",
            "source_url": f"https://example.org/{i}.jpg",
            "license": "public-domain",
        }
        for i in range(1, count + 1)
    ]
    write_text(
        run_dir / ending_schema.RECORD_FILE,
        dump_json(ending_schema.build_document(run_id, photos)),
    )
    if clips:
        for photo in photos:
            target = run_dir / photo["file"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fake-ending")
    return photos


def test_ending_is_appended_after_the_last_scene(pisa):
    paths, run_id, document = pisa
    install_ending(paths, run_id, 2)
    ffmpeg = FakeFFmpeg()

    result = run(paths, run_id, ffmpeg=ffmpeg)

    inputs = [ffmpeg.last[i + 1] for i, a in enumerate(ffmpeg.last) if a == "-i"]
    assert inputs[-2:] == ["ending/1.mp4", "ending/2.mp4"]
    assert result.ending_cuts == 2
    assert result.scene_count == len(document["scenes"])


def test_ending_extends_the_timeline_but_not_the_narration(pisa):
    """나레이션은 `total_duration`에서 끝나고 꼬리는 BGM이 덮는다 (`[10] mix` 계약)."""
    paths, run_id, document = pisa
    install_ending(paths, run_id, 2, seconds=2.4)

    result = run(paths, run_id)
    stage = state_of(paths, run_id)

    assert stage["languages"]["ko"]["narration_duration"] == pytest.approx(document["total_duration"])
    assert result.total_duration == pytest.approx(document["total_duration"] + 4.8)


def test_ending_enters_on_a_hard_cut(pisa):
    """디졸브로 붙이면 xfade가 `[7]`이 렌더하지 않은 꼬리를 요구한다."""
    paths, run_id, _document = pisa
    install_ending(paths, run_id, 2)
    ffmpeg = FakeFFmpeg()

    run(paths, run_id, ffmpeg=ffmpeg)

    steps = ffmpeg.graph.split(";")
    joins = [s for s in steps if "xfade" in s or "concat" in s]
    assert "concat" in joins[-2], "엔딩 진입은 하드컷이다"
    assert "xfade" in joins[-1], "엔딩 컷 사이는 디졸브다"


def test_subtitles_still_cover_only_the_scenes(pisa):
    """엔딩에는 자막 큐가 없다 — 대조할 실측 시각이 없다."""
    paths, run_id, document = pisa
    install_ending(paths, run_id, 3)

    result = run(paths, run_id)

    cues = parse_ass(result.subtitles_path.read_text(encoding="utf-8"))
    assert len(cues) == len(document["scenes"])
    assert cues[-1].end == pytest.approx(document["total_duration"], abs=0.01)


def test_sync_verification_ignores_the_ending(pisa):
    """싱크 검증의 대조 대상은 씬 구간이다. 엔딩이 붙어도 오차가 늘지 않는다."""
    paths, run_id, _document = pisa
    without = run(paths, run_id).max_drift
    install_ending(paths, run_id, 3)

    assert run(paths, run_id, force=True).max_drift == without


def test_transition_counts_stay_a_scene_metric(pisa):
    """ADR-0033 되돌릴 조건의 관측 지표라 고정 마감이 섞이면 안 된다."""
    paths, run_id, _document = pisa
    before = run(paths, run_id)
    install_ending(paths, run_id, 3)

    after = run(paths, run_id, force=True)

    assert (after.dissolves, after.cuts) == (before.dissolves, before.cuts)
    assert after.cut_scene_ids == before.cut_scene_ids


def test_no_ending_file_changes_nothing(pisa):
    """D-3 — 파일이 없으면 지금과 완전히 같이 돈다."""
    paths, run_id, _document = pisa
    ffmpeg = FakeFFmpeg()

    result = run(paths, run_id, ffmpeg=ffmpeg)

    assert result.ending_cuts == 0
    assert "ending/" not in ffmpeg.graph
    assert result.passed


def test_a_broken_ending_contract_is_dropped_not_fatal(pisa):
    """엔딩은 마감이지 본편이 아니다 — 깨진 엔딩 때문에 영상을 잃지 않는다 (D-5)."""
    from shorts_factory.config import write_text
    from shorts_factory.jsonio import dump_json

    paths, run_id, _document = pisa
    install_ending(paths, run_id, 1)
    write_text(
        paths.run_dir(run_id) / "ending.json",
        dump_json({"run_id": run_id, "photos": [
            {"index": 1, "file": "ending/1.mp4", "seconds": 2.4,
             "source": "refs/1/01.jpg", "source_url": "https://example.org/1.jpg",
             "license": "copyrighted"},
        ]}),
    )

    result = run(paths, run_id, force=True)

    assert result.passed and result.ending_cuts == 0
    assert any("계약을 어겨" in w for w in result.warnings)


def test_a_missing_ending_clip_is_dropped_not_fatal(pisa):
    paths, run_id, _document = pisa
    install_ending(paths, run_id, 2, clips=False)

    result = run(paths, run_id)

    assert result.passed and result.ending_cuts == 0
    assert any("엔딩 클립" in w for w in result.warnings)


# --- 언어 루프 (ADR-0056 결정 5) ------------------------------------------------
#
# 클립 풀은 하나이고 트림·자막·나레이션만 언어별이다. 실측 파일이 있는 언어만 돈다.


def write_narration(paths, run_id, lang):
    target = paths.run_dir(run_id) / f"narration.{lang}.wav"
    target.write_bytes(b"RIFF-fake")
    return target


def test_every_present_language_gets_its_own_timeline(paths):
    run_id, _document = install_run(paths, PISA, langs={"ja": 1.13, "en": 0.78})
    ffmpeg = FakeFFmpeg()

    result = run(paths, run_id, ffmpeg=ffmpeg)

    assert sorted(result.languages) == ["en", "ja", "ko"]
    run_dir = paths.run_dir(run_id)
    for lang in ("ko", "ja", "en"):
        assert (run_dir / f"subtitles.{lang}.ass").exists()
        assert (run_dir / f"timeline.{lang}.mp4").exists()
    outputs = [c["cmd"][-1] for c in ffmpeg.calls]
    assert outputs == ["timeline.ko.mp4", "timeline.ja.mp4", "timeline.en.mp4"]


def test_absent_languages_are_not_a_warning(pisa):
    """D-3 — ja·en 실측이 없으면 그 언어의 쇼츠가 없을 뿐이다."""
    paths, run_id, _document = pisa
    result = run(paths, run_id)

    assert list(result.languages) == ["ko"]
    assert not any("ja" in w or "en" in w for w in result.warnings)


def test_each_language_trims_to_its_own_scene_length(paths):
    """같은 클립을 언어별 실측으로 자른다 — ja는 ko의 ×1.13, en은 ×0.78."""
    run_id, document = install_run(paths, PISA, langs={"ja": 1.13, "en": 0.78})
    ffmpeg = FakeFFmpeg()
    run(paths, run_id, ffmpeg=ffmpeg)

    first_ko = document["scenes"][0]["end"] - document["scenes"][0]["start"]
    graphs = {c["cmd"][-1]: ffmpeg.graph_of(c["cmd"]) for c in ffmpeg.calls}
    assert f"trim=end={first_ko + 0.6:.3f}" in graphs["timeline.ko.mp4"]
    assert f"trim=end={round(first_ko * 1.13, 3) + 0.6:.3f}" in graphs["timeline.ja.mp4"]
    assert f"trim=end={round(first_ko * 0.78, 3) + 0.6:.3f}" in graphs["timeline.en.mp4"]


def test_subtitles_are_per_language(paths):
    run_id, _document = install_run(paths, PISA, langs={"ja": 1.13})
    result = run(paths, run_id)

    ko = parse_ass(result.languages["ko"].subtitles_path.read_text(encoding="utf-8"))
    ja = parse_ass(result.languages["ja"].subtitles_path.read_text(encoding="utf-8"))
    assert len(ko) == len(ja)
    assert ja[-1].end == pytest.approx(ko[-1].end * 1.13, abs=0.02)


def test_lang_option_limits_the_loop(paths):
    run_id, _document = install_run(paths, PISA, langs={"ja": 1.13, "en": 0.78})
    ffmpeg = FakeFFmpeg()

    result = run(paths, run_id, ffmpeg=ffmpeg, langs=["ja"])

    assert list(result.languages) == ["ja"]
    assert [c["cmd"][-1] for c in ffmpeg.calls] == ["timeline.ja.mp4"]


def test_requesting_a_language_without_measurements_is_refused(pisa):
    paths, run_id, _document = pisa
    with pytest.raises(AssembleStageError, match="scenes.timed.ja.json"):
        run(paths, run_id, langs=["ja"])


def test_narration_is_muxed_when_present(paths):
    """스펙 05 [9] — 그 언어의 narration.{lang}.wav가 입력이다."""
    run_id, _document = install_run(paths, PISA, langs={"ja": 1.13})
    write_narration(paths, run_id, "ko")
    ffmpeg = FakeFFmpeg()

    result = run(paths, run_id, ffmpeg=ffmpeg)

    ko_cmd, ja_cmd = (c["cmd"] for c in ffmpeg.calls)
    assert "narration.ko.wav" in ko_cmd and "-an" not in ko_cmd
    assert "-an" in ja_cmd, "나레이션이 없는 언어는 소리 없는 영상이다"
    assert result.languages["ko"].narration_muxed
    assert any("narration.ja.wav" in w for w in result.languages["ja"].warnings)


def test_short_clip_is_padded_with_its_last_frame(paths):
    """[7]의 10초 클램프 — 클립이 씬보다 짧으면 tpad로 정지시키고 경고한다 (스펙 05 [9])."""
    from shorts_factory.config import write_text
    from shorts_factory.jsonio import dump_json

    run_id, document = install_run(paths, PISA)
    scene = document["scenes"][4]
    length = scene["end"] - scene["start"]
    write_text(
        paths.run_dir(run_id) / "clips.json",
        dump_json({"run_id": run_id, "scenes": [
            {"scene_id": scene["scene_id"], "seconds": round(length - 0.5, 3)},
        ]}),
    )
    ffmpeg = FakeFFmpeg()

    result = run(paths, run_id, ffmpeg=ffmpeg)

    steps = ffmpeg.graph.split(";")
    padded = [s for s in steps if "tpad=" in s]
    assert len(padded) == 1 and padded[0].startswith(f"[{scene['scene_id'] - 1}:v]")
    assert "tpad=stop_mode=clone" in padded[0]
    assert result.languages["ko"].padded_scene_ids == (scene["scene_id"],)
    assert any("정지로 늘린다" in w for w in result.warnings)


def test_clips_record_is_optional(pisa):
    """clips.json이 없으면 패딩 없이 지금과 같이 돈다 (D-3)."""
    paths, run_id, _document = pisa
    ffmpeg = FakeFFmpeg()
    run(paths, run_id, ffmpeg=ffmpeg)
    assert "tpad=" not in ffmpeg.graph


def test_font_override_per_language(paths, monkeypatch):
    """스펙 04 — 폰트만 언어별이다. 계약 파일에 표가 없는 동안은 환경변수로 덮어쓴다."""
    monkeypatch.setenv("SUBTITLE_FONT_JA", "Noto Sans JP")
    run_id, _document = install_run(paths, PISA, langs={"ja": 1.13})

    result = run(paths, run_id)

    ko = result.languages["ko"].subtitles_path.read_text(encoding="utf-8")
    ja = result.languages["ja"].subtitles_path.read_text(encoding="utf-8")
    assert "Style: Default,Pretendard," in ko
    assert "Style: Default,Noto Sans JP," in ja
    assert result.languages["ja"].font_name == "Noto Sans JP"


def test_second_run_skips_only_finished_languages(paths):
    """한 언어가 나중에 생기면 그 언어만 돈다."""
    run_id, _document = install_run(paths, PISA)
    run(paths, run_id)

    install_run(paths, PISA, langs={"ja": 1.13})
    ffmpeg = FakeFFmpeg()
    again = run(paths, run_id, ffmpeg=ffmpeg)

    assert again.languages["ko"].skipped and not again.languages["ja"].skipped
    assert [c["cmd"][-1] for c in ffmpeg.calls] == ["timeline.ja.mp4"]


def test_a_failing_language_marks_the_stage_failed(paths):
    run_id, _document = install_run(paths, PISA, langs={"ja": 1.13})
    calls = {"n": 0}

    class FailSecond(FakeFFmpeg):
        def __call__(self, cmd, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                self.returncode = 1
            return super().__call__(cmd, **kwargs)

    with pytest.raises(AssembleStageError, match=r"\[ja\]"):
        run(paths, run_id, ffmpeg=FailSecond())

    stage = state_of(paths, run_id)
    assert stage["status"] == "failed"
    assert stage["languages"]["ko"]["status"] == "done"
    assert stage["languages"]["ja"]["status"] == "failed"


def test_cli_lang_option_is_parsed():
    assert parse_args(["assemble", "--run-id", "x", "--lang", "ko,ja"]).lang == "ko,ja"
    assert parse_args(["assemble", "--run-id", "x"]).lang is None
