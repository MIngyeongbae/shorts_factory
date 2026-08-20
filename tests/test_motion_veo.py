"""[7. motion]의 인포씬(Veo) 분기 계약 (ADR-0043).

- `info/{scene_id}.jpg`의 **존재**가 Veo 경로를 고른다 — 씬의 `motion` 값이 아니다
- first=CLEAN, last=INFO를 파일로 넘긴다 (base64 인라인 — URL 왕복 없음)
- 강등 사다리: `veo → info_still → mj_video → kenburns` (ADR-0043 개정 2026-08-21).
  Veo가 없거나 실패해도 INFO가 있으면 **라벨을 지키는 정지**로 내려간다 — INFO를
  버리고 일반 영상을 만드는 것은 강등이 아니라 다른 것을 만드는 것이다.
  조용한 강등은 없다 (`demoted_from`)
- 프로바이더 전체가 막히면 남은 인포씬은 시도조차 하지 않는다
- INFO 파일이 바뀌면 지문이 바뀌어 클립을 다시 만든다
"""

import json

from conftest import PISA
from timed_fixtures import install_images, install_run

from shorts_factory.stages.motion import run_motion_stage
from shorts_factory.video.fake import FakeFFmpeg
from shorts_factory.videogen.base import (
    GeneratedClip,
    VideoClient,
    VideoGenError,
    VideoProviderNotConfigured,
)

MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"


class FakeVeoClient(VideoClient):
    """Veo 대역 — 파일 입력을 받는 인포씬 어댑터."""

    name = "fake-veo"
    source_provider = ""  # 로컬 파일 입력이라 image_source.json을 안 쓴다

    def __init__(self, *, fail=None):
        self.calls = []
        self.fail = fail or {}

    def concurrency(self):
        return 2

    def generate(self, request, *, timeout=None):
        self.calls.append(request)
        if request.scene_id in self.fail:
            raise self.fail[request.scene_id]
        return GeneratedClip(
            data=MP4 + str(request.scene_id).encode(),
            request_id=f"veo-{request.scene_id}",
            model_id=self.name,
            duration=4.0,
        )


class FakeMJVideoClient(VideoClient):
    name = "fake-video"
    source_provider = "fake"

    def __init__(self):
        self.calls = []

    def generate(self, request, *, timeout=None):
        self.calls.append(request)
        return GeneratedClip(
            data=MP4 + b"mj" + str(request.scene_id).encode(),
            request_id=f"mj-{request.scene_id}",
            model_id=self.name,
        )


def install(paths, *, info_scenes=(1, 3)):
    """`[3]`·`[6]`·`[6i]`가 끝난 run — 일부 씬에 INFO 이미지가 있다."""
    run_id, document = install_run(paths, PISA, clips=False)
    install_images(paths, run_id, [s["scene_id"] for s in document["scenes"]])
    info_dir = paths.run_dir(run_id) / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    for sid in info_scenes:
        (info_dir / f"{sid}.jpg").write_bytes(b"\xff\xd8\xffinfo" + str(sid).encode())
    return run_id, document


def install_sources(paths, run_id, scene_ids):
    """`image_source.json` — mj_video 강등 폴백을 검사할 때만 놓는다."""
    document = {
        "run_id": run_id,
        "provider": "fake",
        "scenes": [
            {"scene_id": sid, "task_id": f"task-{sid}", "quadrant": 0,
             "set_by": "6-imagegen"}
            for sid in scene_ids
        ],
    }
    path = paths.run_dir(run_id) / "image_source.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def record_of(paths, run_id):
    data = json.loads(
        (paths.run_dir(run_id) / "clips.json").read_text(encoding="utf-8")
    )
    return {s["scene_id"]: s for s in data["scenes"]}


def run(paths, run_id, **kwargs):
    return run_motion_stage(run_id, paths=paths, runner=FakeFFmpeg(), **kwargs)


# --- 통과 경로 -----------------------------------------------------------------


def test_info_file_selects_the_veo_path(paths):
    run_id, _ = install(paths, info_scenes=(1, 3))
    veo = FakeVeoClient()

    result = run(paths, run_id, info_video=veo)

    assert result.passed
    assert result.veo_count == 2
    record = record_of(paths, run_id)
    assert record[1]["motion_used"] == "veo"
    assert record[1]["info_image"] == "info/1.jpg"
    assert record[2]["motion_used"] == "kenburns", "INFO 없는 씬은 일반 경로다"
    assert record[1]["demoted_from"] is None


def test_veo_receives_both_frames_as_files(paths):
    run_id, _ = install(paths, info_scenes=(1,))
    veo = FakeVeoClient()

    run(paths, run_id, info_video=veo)

    request = veo.calls[0]
    assert request.first_frame.name == "1.png", "first = CLEAN"
    assert request.last_frame.name == "1.jpg", "last = INFO"
    assert request.last_frame.parent.name == "info"
    assert request.source_task_id == "", "파일 입력이라 잡 id가 없다"


def test_a_made_info_clip_is_not_bought_twice(paths):
    run_id, _ = install(paths, info_scenes=(1,))
    run(paths, run_id, info_video=FakeVeoClient())

    second = FakeVeoClient()
    run(paths, run_id, info_video=second)
    assert second.calls == []


def test_a_changed_info_image_changes_the_fingerprint(paths):
    """[6i]가 INFO를 다시 만들면 끝 프레임이 바뀐다 — 지문도 바뀌어야 한다.

    CLEAN 해시로는 못 잡는다: 클립은 CLEAN+INFO 둘에서 나오고 CLEAN이 그대로여도
    INFO가 바뀌면 다른 영상이다.
    """
    run_id, _ = install(paths, info_scenes=(1,))
    run(paths, run_id, info_video=FakeVeoClient())
    before = record_of(paths, run_id)[1]["digest"]

    (paths.run_dir(run_id) / "info" / "1.jpg").write_bytes(b"\xff\xd8\xffnew")
    second = FakeVeoClient()
    run(paths, run_id, info_video=second, force=True)

    after = record_of(paths, run_id)[1]
    assert after["digest"] != before
    assert 1 in [r.scene_id for r in second.calls]


# --- 강등 사다리 ---------------------------------------------------------------


def test_without_a_veo_client_info_scenes_demote_to_info_still(paths):
    """`--info-video none`의 테스트 배치 — INFO를 버리지 않고 정지로 세운다 (ADR-0043 개정)."""
    run_id, _ = install(paths, info_scenes=(1,))

    result = run(paths, run_id)  # info_video 없음

    record = record_of(paths, run_id)
    assert record[1]["motion_used"] == "info_still"
    assert record[1]["demoted_from"] == "veo"
    assert record[1]["info_image"] == "info/1.jpg", "라벨은 화면에 남는다"
    assert any("Veo 프로바이더가 없다" in w for w in result.warnings)
    assert result.info_still_count == 1
    assert result.passed


def test_veo_failure_falls_to_info_still_not_mj_video(paths):
    """Veo가 실패해도 INFO가 있으면 라벨을 지킨다 — mj_video로 가면 라벨이 사라진다."""
    run_id, document = install(paths, info_scenes=(1,))
    ids = [s["scene_id"] for s in document["scenes"]]
    for scene in document["scenes"]:
        scene["motion"] = "mj_video"
    install_run(paths, PISA, clips=False, document=document)
    install_sources(paths, run_id, ids)

    veo = FakeVeoClient(fail={1: VideoGenError("잡 실패")})
    mj = FakeMJVideoClient()
    result = run(paths, run_id, info_video=veo, video=mj)

    record = record_of(paths, run_id)
    assert record[1]["motion_used"] == "info_still"
    assert record[1]["demoted_from"] == "veo"
    assert 1 not in [r.scene_id for r in mj.calls], "인포씬은 mj_video를 부르지 않는다"
    assert result.passed


def test_veo_failure_without_sources_falls_to_info_still(paths):
    run_id, _ = install(paths, info_scenes=(1,))
    veo = FakeVeoClient(fail={1: VideoGenError("잡 실패")})

    result = run(paths, run_id, info_video=veo)

    record = record_of(paths, run_id)
    assert record[1]["motion_used"] == "info_still"
    assert record[1]["demoted_from"] == "veo"
    assert result.passed


def test_provider_down_skips_the_remaining_info_scenes(paths):
    run_id, _ = install(paths, info_scenes=(1, 3, 5))
    veo = FakeVeoClient(fail={1: VideoProviderNotConfigured("키 없음")})

    result = run(paths, run_id, info_video=veo, jobs=1)

    assert len(veo.calls) == 1, "전체 문제면 남은 인포씬은 시도하지 않는다"
    record = record_of(paths, run_id)
    assert all(record[sid]["demoted_from"] == "veo" for sid in (1, 3, 5))
    assert result.passed, "강등이지 실패가 아니다"
