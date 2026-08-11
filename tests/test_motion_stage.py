"""[7. motion] 단계 계약 (specs/05-pipeline.md, specs/03, ADR-0006/0017/0020/0024).

입력은 run 디렉터리에 있는 것 둘뿐이다 — `scenes.timed.json`과 `images/{scene_id}.*`.
확인 대상:

- 클립 길이를 `[9]`와 **같은 함수**에서 얻는다 (씬 길이 + 디졸브 꼬리 0.6초)
- 중복 그림을 `images.json`이 아니라 **파일 내용**으로 판정한다 (ADR-0024)
- `kling`은 조용히 넘어가지 않고 강등으로 기록된다
- 산출물이 전부 `runs/{run_id}/` 아래에 떨어진다 (ADR-0017)
"""

import json

import pytest
from conftest import HOOVER, PISA, install_script
from timed_fixtures import install_images, install_run, timed_document

from shorts_factory.cli import parse_args
from shorts_factory.stages.motion import (
    STAGE,
    MotionStageError,
    resolve_run_id,
    run_motion_stage,
)
from shorts_factory.video.fake import FakeFFmpeg
from shorts_factory.video.kenburns import frame_count
from shorts_factory.video.timeline import build_timeline


def install(paths, slug=PISA, *, document=None, copies=None, suffix=".png"):
    """`[3]`과 `[6]`이 끝난 run 디렉터리."""
    run_id, document = install_run(paths, slug, clips=False, document=document)
    install_images(
        paths,
        run_id,
        [s["scene_id"] for s in document["scenes"]],
        suffix=suffix,
        copies=copies,
    )
    return run_id, document


def run(paths, run_id, *, ffmpeg=None, **kwargs):
    return run_motion_stage(
        run_id, paths=paths, runner=ffmpeg or FakeFFmpeg(), **kwargs
    )


def record_of(paths, run_id):
    return json.loads(
        (paths.run_dir(run_id) / "clips.json").read_text(encoding="utf-8")
    )


def state_of(paths, run_id):
    data = json.loads(
        (paths.run_dir(run_id) / "state.json").read_text(encoding="utf-8")
    )
    return data["stages"][STAGE]


# --- 통과 경로 ---------------------------------------------------------------


@pytest.mark.parametrize("slug", [PISA, HOOVER])
def test_stage_makes_one_clip_per_scene(paths, slug):
    run_id, document = install(paths, slug)

    result = run(paths, run_id)

    assert result.passed
    assert result.scene_count == len(document["scenes"])
    clips = paths.run_dir(run_id) / "clips"
    for scene in document["scenes"]:
        assert (clips / f"{scene['scene_id']}.mp4").exists()


def test_stage_writes_only_under_the_run_directory(paths):
    # 2부는 06-script.json을 고치지 않는다 (ADR-0017)
    script = install_script(paths, PISA)
    before = script.read_bytes()
    run_id, _ = install(paths, PISA)

    run(paths, run_id)

    assert script.read_bytes() == before
    assert not (paths.topic_dir(PISA) / "clips").exists()


def test_state_records_the_stage_as_done(paths):
    run_id, document = install(paths, PISA)

    run(paths, run_id)

    info = state_of(paths, run_id)
    assert info["status"] == "done"
    assert info["rendered"] == len(document["scenes"])
    assert info["failed"] == 0
    assert "clips.json" in " ".join(info["outputs"])


# --- 클립 기하 (ADR-0024) ----------------------------------------------------


def test_clip_length_comes_from_the_same_timeline_the_assembler_uses(paths):
    """`[9]`와 같은 `build_timeline`의 `clip_length`를 프레임으로 옮긴다."""
    run_id, document = install(paths, PISA)
    ffmpeg = FakeFFmpeg()

    run(paths, run_id, ffmpeg=ffmpeg)

    expected = [frame_count(s.clip_length) for s in build_timeline(document["scenes"]).segments]
    actual = [int(ffmpeg.option_of(call["cmd"], "-frames:v")) for call in ffmpeg.calls]
    assert actual == expected


def test_dissolve_scenes_carry_the_tail_and_cut_scenes_do_not(paths):
    """꼬리 0.6초는 **디졸브로 나가는** 클립에만 붙는다 (timeline.py의 기하)."""
    run_id, document = install(paths, PISA)
    segments = build_timeline(document["scenes"]).segments
    record = None

    run(paths, run_id)
    record = {s["scene_id"]: s for s in record_of(paths, run_id)["scenes"]}

    for segment in segments:
        expected = frame_count(segment.duration + segment.tail)
        assert record[segment.scene_id]["frames"] == expected
        assert record[segment.scene_id]["clip_length"] == segment.clip_length


def test_last_scene_has_no_tail(paths):
    run_id, document = install(paths, PISA)

    run(paths, run_id)

    segments = build_timeline(document["scenes"]).segments
    last = record_of(paths, run_id)["scenes"][-1]
    assert last["clip_length"] == pytest.approx(segments[-1].duration)


# --- 중복 그림 판정 (ADR-0024) -----------------------------------------------


def test_duplicate_image_flips_the_camera_on_the_second_use(paths):
    """`[6]`이 폴백으로 씬 5의 그림을 씬 6에 복사한 상황."""
    run_id, document = install(paths, PISA, copies={6: 5})
    contracted = {s["scene_id"]: s["camera"] for s in document["scenes"]}

    result = run(paths, run_id)

    record = {s["scene_id"]: s for s in record_of(paths, run_id)["scenes"]}
    assert record[5]["camera_used"] == contracted[5]
    assert record[5]["camera_reversed"] is False
    assert record[6]["camera_reversed"] is True
    assert record[6]["camera_used"] != contracted[6]
    assert result.reversed_count == 1
    assert any("역방향" in w for w in result.warnings)


def test_the_contracted_camera_is_kept_in_the_record(paths):
    # 값의 출처는 06-script.json 하나다 (ADR-0020) — 씬 계약을 고치지 않는다
    run_id, document = install(paths, PISA, copies={6: 5})
    contracted = {s["scene_id"]: s["camera"] for s in document["scenes"]}

    run(paths, run_id)

    record = {s["scene_id"]: s for s in record_of(paths, run_id)["scenes"]}
    assert record[6]["camera"] == contracted[6]


def test_duplicate_detection_ignores_images_json(paths):
    """`[7]`은 `images.json`을 읽지 않는다 (ADR-0024).

    기록이 "중복 없음"이라고 말해도 바이트가 같으면 역방향이 걸려야 한다.
    """
    run_id, _ = install(paths, PISA, copies={6: 5})
    (paths.run_dir(run_id) / "images.json").write_text(
        json.dumps({"scenes": [{"scene_id": 6, "status": "generated"}]}),
        encoding="utf-8",
    )

    run(paths, run_id)

    record = {s["scene_id"]: s for s in record_of(paths, run_id)["scenes"]}
    assert record[6]["camera_reversed"] is True


def test_three_uses_of_one_image_raise_the_alarm(paths):
    # 카메라 워크로 가릴 문제가 아니다 (스펙 03)
    run_id, _ = install(paths, PISA, copies={6: 5, 7: 5})

    result = run(paths, run_id)

    assert any("3번째" in w for w in result.warnings)


def test_distinct_images_never_trigger_a_reversal(paths):
    run_id, _ = install(paths, PISA)

    result = run(paths, run_id)

    assert result.reversed_count == 0


# --- 강등 사다리 -------------------------------------------------------------


def test_kling_scenes_are_demoted_loudly(paths):
    document = timed_document(PISA)
    document["scenes"][3]["motion"] = "kling"
    run_id, _ = install(paths, PISA, document=document)

    result = run(paths, run_id)

    record = {s["scene_id"]: s for s in record_of(paths, run_id)["scenes"]}
    target = document["scenes"][3]["scene_id"]
    assert record[target]["demoted_from"] == "kling"
    assert record[target]["motion_used"] == "kenburns"
    assert result.demoted == 1
    assert any("강등" in w for w in result.warnings)


class FailsMovingCameras(FakeFFmpeg):
    """zoompan이 들어간 명령만 실패시킨다 — `static` 강등 경로를 열기 위한 대역."""

    def __call__(self, cmd, **kwargs):
        if "zoompan" in self.option_of(cmd, "-vf"):
            self.calls.append({"cmd": list(cmd), "cwd": kwargs.get("cwd"), "kwargs": kwargs})
            import subprocess

            return subprocess.CompletedProcess(list(cmd), 1, "", "zoompan 실패")
        return super().__call__(cmd, **kwargs)


def test_a_failed_work_falls_back_to_static(paths):
    run_id, document = install(paths, PISA)

    result = run(paths, run_id, ffmpeg=FailsMovingCameras())

    assert result.passed
    record = {s["scene_id"]: s for s in record_of(paths, run_id)["scenes"]}
    moved = [
        s["scene_id"] for s in document["scenes"] if s["camera"] != "static"
    ]
    assert moved, "픽스처에 움직이는 씬이 있어야 이 테스트가 의미 있다"
    for scene_id in moved:
        assert record[scene_id]["camera_used"] == "static"
        assert record[scene_id]["camera_fallback"] is True
        assert record[scene_id]["attempts"] == 2


def test_static_scenes_are_not_retried_with_static(paths):
    """계약이 이미 `static`이면 사다리에 아래가 없다."""
    run_id, document = install(paths, PISA)

    run(paths, run_id, ffmpeg=FailsMovingCameras())

    record = {s["scene_id"]: s for s in record_of(paths, run_id)["scenes"]}
    still = [s["scene_id"] for s in document["scenes"] if s["camera"] == "static"]
    assert all(record[scene_id]["attempts"] == 1 for scene_id in still)


def test_the_stage_stops_when_a_clip_cannot_be_made(paths):
    run_id, _ = install(paths, PISA)

    with pytest.raises(MotionStageError, match="클립을 만들지 못한"):
        run(paths, run_id, ffmpeg=FakeFFmpeg(returncode=1, stderr="망함"))

    assert state_of(paths, run_id)["status"] == "failed"


# --- 입력 검증 ---------------------------------------------------------------


def test_missing_images_stop_before_any_encoding(paths):
    run_id, document = install(paths, PISA)
    (paths.run_dir(run_id) / "images" / f"{document['scenes'][4]['scene_id']}.png").unlink()
    ffmpeg = FakeFFmpeg()

    with pytest.raises(MotionStageError, match="이미지가 없는 씬"):
        run(paths, run_id, ffmpeg=ffmpeg)

    assert ffmpeg.calls == []


def test_missing_images_directory_points_at_stage_six(paths):
    run_id, _ = install_run(paths, PISA, clips=False)

    with pytest.raises(MotionStageError, match=r"\[6. imagegen\]"):
        run(paths, run_id)


def test_two_extensions_for_one_scene_stop_the_stage(paths):
    run_id, _ = install(paths, PISA)
    images = paths.run_dir(run_id) / "images"
    (images / "3.jpg").write_bytes((images / "3.png").read_bytes())

    with pytest.raises(MotionStageError, match="씬 하나에 그림 하나"):
        run(paths, run_id)


def test_provider_suffix_is_not_hardcoded(paths):
    # 확장자는 프로바이더가 정한다 (ADR-0021) — 실물은 .jpg, 페이크는 .png다
    run_id, _ = install(paths, PISA, suffix=".jpg")

    assert run(paths, run_id).passed


def test_missing_timed_scenes_points_at_stage_three(paths):
    run_id, _ = install(paths, PISA)
    (paths.run_dir(run_id) / "scenes.timed.json").unlink()

    with pytest.raises(MotionStageError, match=r"\[3. tts\+sync\]"):
        run(paths, run_id)


def test_a_run_id_mismatch_stops_the_stage(paths):
    # 계보는 run_id로 잇는다 (ADR-0017)
    run_id, _ = install(paths, PISA)

    with pytest.raises(MotionStageError, match="run_id"):
        run(paths, "20260101-other-run")


# --- 이어받기 ---------------------------------------------------------------


def test_a_finished_run_is_skipped(paths):
    run_id, _ = install(paths, PISA)
    run(paths, run_id)
    ffmpeg = FakeFFmpeg()

    result = run(paths, run_id, ffmpeg=ffmpeg)

    assert result.skipped
    assert ffmpeg.calls == []


def test_force_rebuilds_every_clip(paths):
    run_id, document = install(paths, PISA)
    run(paths, run_id)
    ffmpeg = FakeFFmpeg()

    result = run(paths, run_id, ffmpeg=ffmpeg, force=True)

    assert not result.skipped
    assert len(ffmpeg.calls) == len(document["scenes"])


def test_a_new_image_reruns_only_that_scene(paths):
    """지문이 그대로인 씬은 다시 만들지 않는다."""
    run_id, document = install(paths, PISA)
    run(paths, run_id)
    # 단계는 done이지만 클립 하나가 사라졌다 — 스킵 조건이 깨진다
    target = document["scenes"][2]["scene_id"]
    (paths.run_dir(run_id) / "clips" / f"{target}.mp4").unlink()
    ffmpeg = FakeFFmpeg()

    run(paths, run_id, ffmpeg=ffmpeg)

    outputs = [call["cmd"][-1] for call in ffmpeg.calls]
    assert outputs == [f"clips/{target}.mp4"]


# --- CLI --------------------------------------------------------------------


def test_cli_exposes_the_stage():
    args = parse_args(["motion", "--slug", PISA])
    assert args.slug == PISA
    assert args.ffmpeg == "ffmpeg"


def test_run_id_comes_from_the_boundary_file(paths):
    # 2부는 06-script.json에서 run_id 한 줄만 읽는다 (ADR-0017)
    install_script(paths, PISA)
    expected = timed_document(PISA)["run_id"]

    assert resolve_run_id(paths, slug=PISA) == expected
