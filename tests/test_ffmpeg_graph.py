"""FFmpeg 필터 그래프·인자 배열 (specs/00 산출물 정의, specs/03 전환 규칙).

이 환경에는 FFmpeg가 없다. 그래도 `[9]`의 판단은 전부 여기서 결정된다 — 어느 자리에
디졸브가 걸리고, 오프셋이 얼마이고, 클립을 어디서 자르고, 자막을 언제 얹는가. 그래서
그래프 문자열을 실행 없이 검사한다.

`run_ffmpeg`(프로세스를 부르는 유일한 지점)는 목으로 확인한다.
"""

import subprocess
from pathlib import Path

import pytest
from timed_fixtures import PISA, timed_document

from shorts_factory.video.fake import FakeFFmpeg
from shorts_factory.video.ffmpeg import (
    CRF,
    FPS,
    HEIGHT,
    PIXEL_FORMAT,
    VIDEO_CODEC,
    WIDTH,
    FFmpegError,
    build_command,
    build_filter_graph,
    clip_filter,
    escape_filter_path,
    relative_path,
    run_ffmpeg,
)
from shorts_factory.video.timeline import build_timeline


def scene(scene_id, beat, start, end):
    return {"scene_id": scene_id, "beat": beat, "text": "가나다.", "start": start,
            "end": end}


@pytest.fixture
def three_scenes():
    """디졸브 하나 + 하드컷 하나. 두 junction이 한 그래프에 같이 있는 최소 조합."""
    return build_timeline(
        [
            scene(1, "hook_fact", 0.0, 4.0),
            scene(2, "context", 4.0, 7.0),
            scene(3, "turning_point", 7.0, 11.0),
        ]
    )


def graph_of(timeline, **kwargs):
    return build_filter_graph(timeline, subtitles="subtitles.ass", **kwargs)


# --- 클립 준비 ---------------------------------------------------------------


def test_clip_is_trimmed_and_normalised():
    """xfade/concat은 두 입력의 해상도·SAR·픽셀 포맷이 같아야 붙는다."""
    step = clip_filter(2, length=4.6, label="c2")

    assert step.startswith("[2:v]") and step.endswith("[c2]")
    assert "trim=end=4.600" in step
    assert f"scale={WIDTH}:{HEIGHT}" in step
    assert "setsar=1" in step
    assert f"fps={FPS}" in step
    assert f"settb=1/{FPS}" in step
    assert f"format={PIXEL_FORMAT}" in step


# --- 전환 --------------------------------------------------------------------


def test_dissolve_uses_xfade_at_the_scene_start(three_scenes):
    graph = graph_of(three_scenes)

    assert "xfade=transition=fade:duration=0.600:offset=4.000" in graph


def test_hard_cut_uses_concat_not_a_short_xfade(three_scenes):
    """xfade는 duration=0을 받지 않는다. 컷을 짧은 디졸브로 흉내 내지 않는다."""
    graph = graph_of(three_scenes)

    assert "concat=n=2:v=1:a=0" in graph
    assert graph.count("xfade") == 1


def test_concat_restores_the_frame_timebase(three_scenes):
    """`concat`은 출력 타임베이스를 1/1000000으로 바꾼다.

    그대로 두면 하드컷 **뒤에 오는 첫 xfade**가 "timebase do not match"로 설정에
    실패한다. 실물 6씬(피사, 3번이 하드컷) 첫 실행에서 드러난 자리다 — 그래프
    문자열만 보던 동안에는 보이지 않았다.
    """
    graph = graph_of(three_scenes)

    assert f"concat=n=2:v=1:a=0,settb=1/{FPS}" in graph


def test_offsets_are_scene_starts_not_a_running_sum():
    """클립 하나가 계약보다 짧아도 그 뒤 씬의 자막 싱크가 밀리지 않는 근거."""
    document = timed_document(PISA)
    timeline = build_timeline(document["scenes"])
    graph = graph_of(timeline)

    offsets = [
        float(part.split("offset=")[1].split("[")[0])
        for part in graph.split(";")
        if "offset=" in part
    ]
    expected = [
        s.start for s in timeline.segments[1:] if s.transition_in == "dissolve"
    ]
    assert offsets == pytest.approx(expected, abs=0.0005)


def test_chain_threads_every_clip_in_scene_order(three_scenes):
    graph = graph_of(three_scenes)
    steps = graph.split(";")

    assert steps[0].startswith("[0:v]")
    assert steps[1].startswith("[1:v]")
    assert steps[2].startswith("[2:v]")
    assert "[c0][c1]" in steps[3]
    assert "[m1][c2]" in steps[4]


# --- 자막 --------------------------------------------------------------------


def test_subtitles_are_burned_last(three_scenes):
    """디졸브 구간에서 자막까지 같이 페이드되면 두 줄이 겹쳐 보인다."""
    graph = graph_of(three_scenes)
    last = graph.split(";")[-1]

    assert last.startswith("[m2]")
    assert "ass=filename=subtitles.ass" in last
    assert last.endswith("[vout]")


def test_fontsdir_is_passed_only_when_we_have_one(three_scenes):
    assert "fontsdir" not in graph_of(three_scenes)
    assert "fontsdir=../../assets/fonts" in graph_of(
        three_scenes, fontsdir="../../assets/fonts"
    )


def test_single_scene_still_gets_subtitles():
    timeline = build_timeline([scene(1, "hook_fact", 0.0, 4.0)])
    graph = graph_of(timeline)

    assert "xfade" not in graph and "concat" not in graph
    assert graph.split(";")[-1].startswith("[c0]ass=")


# --- 경로 --------------------------------------------------------------------


def test_filter_path_escapes_the_windows_drive_letter():
    """필터 그래프에서 `:`는 인자 구분자다. `C:`가 그대로 들어가면 그래프가 깨진다."""
    assert escape_filter_path(r"C:\runs\x\subtitles.ass") == r"C\:/runs/x/subtitles.ass"


def test_filter_path_prefers_a_relative_path():
    base = Path(r"C:\proj\runs\20260811-x")
    assert escape_filter_path(base / "subtitles.ass", base) == "subtitles.ass"


def test_command_paths_are_not_escaped():
    """`-i` 인자는 셸을 거치지 않는다. escape를 넣으면 파일을 못 찾는다."""
    base = Path(r"C:\proj\runs\20260811-x")
    assert relative_path(base / "clips" / "1.mp4", base) == "clips/1.mp4"


# --- 명령 --------------------------------------------------------------------


def test_command_shape(three_scenes):
    cmd = build_command(
        three_scenes,
        inputs=["clips/1.mp4", "clips/2.mp4", "clips/3.mp4"],
        filter_graph=graph_of(three_scenes),
        output="timeline.mp4",
    )

    assert cmd[0] == "ffmpeg"
    assert cmd.count("-i") == 3
    assert cmd[cmd.index("-map") + 1] == "[vout]"
    assert cmd[-1] == "timeline.mp4"


def test_output_matches_the_delivery_format(three_scenes):
    """specs/00: 9:16 1080×1920, 30fps, h264."""
    cmd = build_command(
        three_scenes, inputs=["a", "b", "c"], filter_graph="x", output="timeline.mp4"
    )

    assert cmd[cmd.index("-c:v") + 1] == VIDEO_CODEC
    assert cmd[cmd.index("-pix_fmt") + 1] == PIXEL_FORMAT
    assert cmd[cmd.index("-r") + 1] == str(FPS)
    assert cmd[cmd.index("-crf") + 1] == str(CRF)


def test_timeline_carries_no_audio(three_scenes):
    """나레이션·SFX·BGM은 [10. mix]가 붙인다 (specs/05)."""
    cmd = build_command(
        three_scenes, inputs=["a", "b", "c"], filter_graph="x", output="timeline.mp4"
    )
    assert "-an" in cmd


def test_input_count_must_match_the_clips(three_scenes):
    with pytest.raises(FFmpegError, match="개수가 다르다"):
        build_command(
            three_scenes, inputs=["a"], filter_graph="x", output="timeline.mp4"
        )


# --- 실행 경계 ---------------------------------------------------------------


def test_runner_gets_the_run_directory_as_cwd(tmp_path):
    """명령 안의 경로가 전부 상대 경로라 cwd가 맞아야 파일을 찾는다."""
    fake = FakeFFmpeg()
    run_ffmpeg(
        ["ffmpeg", "-i", "clips/1.mp4", "timeline.mp4"],
        cwd=tmp_path, produces=tmp_path / "timeline.mp4", runner=fake,
    )

    assert fake.calls[0]["kwargs"]["cwd"] == str(tmp_path)
    assert (tmp_path / "timeline.mp4").exists()


def test_missing_ffmpeg_says_so(tmp_path):
    def missing(*args, **kwargs):
        raise FileNotFoundError

    with pytest.raises(FFmpegError, match="찾을 수 없다"):
        run_ffmpeg(["ffmpeg"], cwd=tmp_path, produces=tmp_path / "x.mp4",
                   runner=missing)


def test_nonzero_exit_carries_the_stderr(tmp_path):
    fake = FakeFFmpeg(returncode=1, stderr="Invalid argument")

    with pytest.raises(FFmpegError, match="Invalid argument"):
        run_ffmpeg(["ffmpeg"], cwd=tmp_path, produces=tmp_path / "x.mp4", runner=fake)


def test_silent_success_without_an_output_is_a_failure(tmp_path):
    fake = FakeFFmpeg(writes_output=False)

    with pytest.raises(FFmpegError, match="산출물을 쓰지 않았다"):
        run_ffmpeg(["ffmpeg", "x.mp4"], cwd=tmp_path, produces=tmp_path / "x.mp4",
                   runner=fake)


def test_timeout_is_reported(tmp_path):
    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1)

    with pytest.raises(FFmpegError, match="끝나지 않았다"):
        run_ffmpeg(["ffmpeg"], cwd=tmp_path, produces=tmp_path / "x.mp4", runner=slow)
