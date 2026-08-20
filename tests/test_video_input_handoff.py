"""영상 입력 인계 — `[6]` → `[6r]` → `[7]`. ADR-0041 (+ADR-0039 전 씬 영상).

`[7]`이 영상을 만들려면 **잡 id와 사분면**이 필요한데 둘 다 이미지 파일 내용에서
복원되지 않는다. 그 값을 들고 있는 `images.json`·`image_review.json`은 기록이지
계약이 아니라 `[7]`이 열지 않는다 (ADR-0024 §2). 그래서 사이드카를 둔다.

확인 대상:

- `[6]`이 `image_source.json`을 쓰고, **영상 입력이 없는 씬은 항목을 내지 않는다**
- `[6r]`이 화면을 바꾸면(사분면 교체·재생성) 사이드카도 같이 바뀐다 — 어긋나면
  `[7]`이 **화면에 없는 그림**으로 영상을 만든다
- `[7]`이 그 파일만 읽고, 못 읽는 경우는 **전부 기록을 남기며** 강등한다.
  조용한 강등이 이 분기가 없던 시절의 사고였다 (ADR-0039)
"""

import json

import pytest
from timed_fixtures import install_images, install_run, timed_document

from shorts_factory.imagegen.fake import FakeImageClient
from shorts_factory.llm.fake import FakeLLMClient
from shorts_factory.schemas import vocab
from shorts_factory.schemas.image_source import validate_image_source
from shorts_factory.stages.imagegen import SOURCE_FILE, run_imagegen_stage
from shorts_factory.stages.imagereview import run_imagereview_stage
from shorts_factory.stages.motion import run_motion_stage
from shorts_factory.stages.prompt import run_prompt_stage
from shorts_factory.video.fake import FakeFFmpeg
from shorts_factory.videogen.base import (
    GeneratedClip,
    VideoClient,
    VideoGenError,
    VideoProviderNotConfigured,
)

from conftest import HOOVER, PISA, install_script

#: 최소 mp4. `GeneratedClip`이 `ftyp`를 앞 32바이트에서 찾는다.
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"


class FakeVideoClient(VideoClient):
    """MJ 영상 어댑터 대역. 호출 인자를 그대로 들고 있는다."""

    name = "fake-video"
    source_provider = "fake"

    def __init__(self, *, fail=None, workers=1):
        self.calls = []
        #: `{scene_id: 예외}` — 그 씬에서만 터진다.
        self.fail = fail or {}
        self.workers = workers

    def concurrency(self):
        return self.workers

    def generate(self, request, *, timeout=None):
        self.calls.append(request)
        if request.scene_id in self.fail:
            raise self.fail[request.scene_id]
        return GeneratedClip(
            data=MP4 + str(request.scene_id).encode(),
            request_id=f"video-{request.scene_id}",
            model_id=self.name,
        )


# --- [6] --------------------------------------------------------------------


@pytest.fixture
def imaged(paths):
    """대본 → `[5]` → `[6]`. `(run_id, 이미지 클라이언트)`."""

    def _run(*, slug=HOOVER, quadrants=4):
        install_script(paths, slug)
        run_id = run_prompt_stage(slug, paths=paths, dialect="nb2").run_id
        images = FakeImageClient(quadrants=quadrants)
        run_imagegen_stage(images=images, run_id=run_id, paths=paths)
        return run_id, images

    return _run


def source_of(paths, run_id) -> dict:
    return json.loads(
        (paths.run_dir(run_id) / SOURCE_FILE).read_text(encoding="utf-8")
    )


def test_imagegen_writes_a_valid_contract(paths, imaged):
    run_id, images = imaged()

    document = source_of(paths, run_id)

    assert validate_image_source(document) == []
    assert document["run_id"] == run_id
    assert document["provider"] == images.name


def test_every_generated_scene_gets_an_entry_at_quadrant_zero(paths, imaged):
    """`[6]`은 항상 q0을 `images/{scene_id}`에 복사한다 (specs/05). 고르는 것은 `[6r]`."""
    run_id, _ = imaged()

    scenes = source_of(paths, run_id)["scenes"]

    assert scenes
    assert {s["quadrant"] for s in scenes} == {0}
    assert all(s["task_id"] for s in scenes)
    assert {s["set_by"] for s in scenes} == {"6-imagegen"}


def test_a_provider_without_job_ids_yields_no_entries(paths):
    """잡 id 개념이 없는 프로바이더면 항목이 없다 — 널을 심지 않는다 (ADR-0041)."""
    install_script(paths, HOOVER)
    run_id = run_prompt_stage(HOOVER, paths=paths, dialect="nb2").run_id

    class NoIdFake(FakeImageClient):
        name = "no-id"

        def generate(self, request, *, timeout=None):
            image = super().generate(request, timeout=timeout)
            image.request_id = None
            return image

    run_imagegen_stage(images=NoIdFake(), run_id=run_id, paths=paths)

    document = source_of(paths, run_id)
    assert document["scenes"] == []
    assert validate_image_source(document) == []


# --- [6r] -------------------------------------------------------------------


def reviews(ids, *, overrides=None) -> str:
    overrides = overrides or {}
    out = []
    for sid in ids:
        item = {"scene_id": sid, "verdict": "pass", "pick": None, "reason": "근거"}
        item.update(overrides.get(sid, {}))
        out.append(item)
    return json.dumps({"reviews": out}, ensure_ascii=False)


def test_a_picked_quadrant_moves_into_the_contract(paths, imaged):
    """사분면을 바꿔 끼웠으면 영상도 **그 장**에서 나와야 한다 (ADR-0031 §2)."""
    run_id, images = imaged()
    before = {s["scene_id"]: s for s in source_of(paths, run_id)["scenes"]}
    target = min(before)

    run_imagereview_stage(
        llm=FakeLLMClient(
            responses=[
                reviews(
                    sorted(before),
                    overrides={target: {"verdict": "pick", "pick": "q2"}},
                )
            ]
        ),
        images=images,
        run_id=run_id,
        paths=paths,
    )

    after = {s["scene_id"]: s for s in source_of(paths, run_id)["scenes"]}
    assert after[target]["quadrant"] == 2
    assert after[target]["task_id"] == before[target]["task_id"]  # 같은 잡의 다른 장
    assert after[target]["set_by"] == "6r-imagereview"
    # 안 건드린 씬은 그대로다
    untouched = max(before)
    assert after[untouched] == before[untouched]


class CountingFake(FakeImageClient):
    """잡마다 다른 id를 낸다. 기본 페이크는 프롬프트 해시라 재생성해도 id가 같다."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.count = 0

    def generate(self, request, *, timeout=None):
        image = super().generate(request, timeout=timeout)
        self.count += 1
        image.request_id = f"job-{self.count}"
        return image


def test_a_regenerated_scene_points_at_the_new_job(paths):
    install_script(paths, HOOVER)
    run_id = run_prompt_stage(HOOVER, paths=paths, dialect="nb2").run_id
    images = CountingFake(quadrants=4)
    run_imagegen_stage(images=images, run_id=run_id, paths=paths)

    before = {s["scene_id"]: s for s in source_of(paths, run_id)["scenes"]}
    target = min(before)

    run_imagereview_stage(
        llm=FakeLLMClient(
            responses=[reviews(sorted(before), overrides={target: {"verdict": "redo"}})]
        ),
        images=images,
        run_id=run_id,
        paths=paths,
    )

    after = {s["scene_id"]: s for s in source_of(paths, run_id)["scenes"]}
    assert after[target]["task_id"] != before[target]["task_id"]
    assert after[target]["quadrant"] == 0
    assert after[target]["set_by"] == "6r-imagereview"
    assert validate_image_source(source_of(paths, run_id)) == []


def test_a_missing_contract_is_a_warning_not_a_crash(paths, imaged):
    """`[6]`이 안 남긴 run이면 갱신하지 않는다. 여기서 새로 만들면 `[6]`을 흉내 낸다."""
    run_id, images = imaged()
    (paths.run_dir(run_id) / SOURCE_FILE).unlink()
    record = json.loads(
        (paths.run_dir(run_id) / "images.json").read_text(encoding="utf-8")
    )
    ids = sorted(s["scene_id"] for s in record["scenes"])

    result = run_imagereview_stage(
        llm=FakeLLMClient(responses=[reviews(ids)]),
        images=images,
        run_id=run_id,
        paths=paths,
    )

    assert any(SOURCE_FILE in w for w in result.warnings)


def test_a_broken_contract_is_not_rewritten_broken(paths, imaged):
    """계약을 어긴 문서를 그대로 다시 쓰면 `[7]`이 **전 씬을 강등**한다.

    `provider`가 빠진 문서를 읽고 `""`로 채워 쓰던 자리가 있었다 — 스키마는
    `minLength: 1`이라 그 문서는 계약이 아니고, `[7]`은 호출 없이 강등한다.
    """
    run_id, images = imaged()
    path = paths.run_dir(run_id) / SOURCE_FILE
    document = json.loads(path.read_text(encoding="utf-8"))
    del document["provider"]
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    ids = sorted(s["scene_id"] for s in document["scenes"])

    result = run_imagereview_stage(
        llm=FakeLLMClient(
            responses=[reviews(ids, overrides={ids[0]: {"verdict": "pick", "pick": "q2"}})]
        ),
        images=images,
        run_id=run_id,
        paths=paths,
    )

    assert any(SOURCE_FILE in w for w in result.warnings)
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after == document  # 손대지 않았다
    assert "provider" not in after  # 빈 문자열로 채워 쓰지 않았다


# --- [7] --------------------------------------------------------------------


def install_motion(paths, *, slug=PISA):
    """`[3]`·`[6]`이 끝난 run. 전 씬이 `mj_video`다 (ADR-0039 기본값)."""
    document = timed_document(slug)
    for scene in document["scenes"]:
        scene["motion"] = "mj_video"
    run_id, document = install_run(paths, slug, clips=False, document=document)
    install_images(paths, run_id, [s["scene_id"] for s in document["scenes"]])
    return run_id, document


def put_contract(paths, run_id, scene_ids, *, provider="fake", quadrant=0, owner=None):
    document = {
        "run_id": owner or run_id,
        "provider": provider,
        "scenes": [
            {
                "scene_id": sid,
                "task_id": f"job-{sid}",
                "quadrant": quadrant,
                "set_by": "6-imagegen",
            }
            for sid in scene_ids
        ],
    }
    path = paths.run_dir(run_id) / SOURCE_FILE
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def run_motion(paths, run_id, **kwargs):
    return run_motion_stage(run_id, paths=paths, runner=FakeFFmpeg(), **kwargs)


def clips_record(paths, run_id):
    data = json.loads(
        (paths.run_dir(run_id) / "clips.json").read_text(encoding="utf-8")
    )
    return {s["scene_id"]: s for s in data["scenes"]}


def test_every_scene_becomes_a_video(paths):
    """ADR-0039 — 전 씬 영상. 강등이 0이어야 하이브리드 폐기가 실제로 도는 것이다."""
    run_id, document = install_motion(paths)
    ids = [s["scene_id"] for s in document["scenes"]]
    put_contract(paths, run_id, ids)
    video = FakeVideoClient()

    result = run_motion(paths, run_id, video=video)

    assert result.passed
    assert result.video_count == len(ids)
    assert result.demoted == 0
    assert len(video.calls) == len(ids)
    clips = paths.run_dir(run_id) / "clips"
    assert (clips / f"{ids[0]}.mp4").read_bytes().startswith(MP4)


def test_the_picked_quadrant_reaches_the_provider(paths):
    """q0 고정으로 두면 **판정이 고른 그림과 영상이 다른 그림**이 된다."""
    run_id, document = install_motion(paths)
    ids = [s["scene_id"] for s in document["scenes"]]
    put_contract(paths, run_id, ids, quadrant=3)
    video = FakeVideoClient()

    run_motion(paths, run_id, video=video)

    assert {c.quadrant for c in video.calls} == {3}
    assert {c.source_task_id for c in video.calls} == {f"job-{i}" for i in ids}


def test_the_motion_prompt_comes_from_the_vocabulary(paths):
    """카메라 워크 문자열의 출처는 `vocab.json` 하나다 (ADR-0034 §3)."""
    run_id, document = install_motion(paths)
    ids = [s["scene_id"] for s in document["scenes"]]
    put_contract(paths, run_id, ids)
    video = FakeVideoClient()

    run_motion(paths, run_id, video=video)

    allowed = {
        m["video_prompt"]
        for m in vocab.meta("camera").values()
        if isinstance(m, dict) and "video_prompt" in m
    }
    assert {c.motion_prompt for c in video.calls} <= allowed
    assert all(c.motion_prompt for c in video.calls)


# --- 강등: 넷 다 소리를 낸다 -------------------------------------------------


def test_no_video_provider_demotes_loudly(paths):
    run_id, document = install_motion(paths)

    result = run_motion(paths, run_id, video=None)

    assert result.passed
    assert result.video_count == 0
    assert result.demoted == len(document["scenes"])
    assert any("영상 프로바이더가 없다" in w for w in result.warnings)
    record = clips_record(paths, run_id)
    assert all(r["demoted_from"] == "mj_video" for r in record.values())


def test_a_missing_contract_file_demotes_loudly(paths):
    run_id, document = install_motion(paths)

    result = run_motion(paths, run_id, video=FakeVideoClient())

    assert result.demoted == len(document["scenes"])
    assert any(SOURCE_FILE in w for w in result.warnings)


def test_a_scene_without_an_entry_demotes_loudly(paths):
    """`[6]`의 인접 씬 폴백 씬이 이 경로다 (ADR-0041 결정 2)."""
    run_id, document = install_motion(paths)
    ids = [s["scene_id"] for s in document["scenes"]]
    put_contract(paths, run_id, ids[1:])
    video = FakeVideoClient()

    result = run_motion(paths, run_id, video=video)

    assert result.demoted == 1
    assert len(video.calls) == len(ids) - 1
    assert clips_record(paths, run_id)[ids[0]]["demoted_from"] == "mj_video"


def test_a_provider_mismatch_makes_no_calls_at_all(paths):
    """MJ 잡 id를 다른 프로바이더에 넣으면 씬 수만큼 실패한다. 호출 전에 막는다."""
    run_id, document = install_motion(paths)
    ids = [s["scene_id"] for s in document["scenes"]]
    put_contract(paths, run_id, ids, provider="midjourney")
    video = FakeVideoClient()  # source_provider = "fake"

    result = run_motion(paths, run_id, video=video)

    assert video.calls == []
    assert result.demoted == len(ids)
    assert any("midjourney" in w for w in result.warnings)


def test_a_contract_from_another_run_is_refused(paths):
    """계보는 run_id로 잇는다 (ADR-0017). 다른 편의 잡으로 만들면 조용히 틀린다."""
    run_id, document = install_motion(paths)
    ids = [s["scene_id"] for s in document["scenes"]]
    put_contract(paths, run_id, ids, owner="20260101-other")
    video = FakeVideoClient()

    result = run_motion(paths, run_id, video=video)

    assert video.calls == []
    assert any("run_id" in w for w in result.warnings)


def test_a_broken_contract_demotes_instead_of_crashing(paths):
    run_id, document = install_motion(paths)
    (paths.run_dir(run_id) / SOURCE_FILE).write_text(
        json.dumps({"run_id": run_id, "provider": "fake", "scenes": [{"scene_id": 1}]}),
        encoding="utf-8",
    )

    result = run_motion(paths, run_id, video=FakeVideoClient())

    assert result.passed
    assert result.demoted == len(document["scenes"])


def test_one_failed_call_demotes_only_that_scene(paths):
    run_id, document = install_motion(paths)
    ids = [s["scene_id"] for s in document["scenes"]]
    put_contract(paths, run_id, ids)
    video = FakeVideoClient(fail={ids[2]: VideoGenError("잡이 FAILURE로 끝났다")})

    result = run_motion(paths, run_id, video=video)

    assert result.passed
    assert result.demoted == 1
    assert result.video_count == len(ids) - 1
    # 재시도하지 않는다 (ADR-0035) — 실패한 씬도 호출 1회다
    assert sum(1 for c in video.calls if c.scene_id == ids[2]) == 1
    assert clips_record(paths, run_id)[ids[2]]["motion_used"] == "kenburns"


def test_a_dead_provider_stops_the_remaining_scenes(paths):
    """같은 오류로 27번 실패하는 것은 결과가 아니라 소음이다 (ADR-0039 §4)."""
    run_id, document = install_motion(paths)
    ids = [s["scene_id"] for s in document["scenes"]]
    put_contract(paths, run_id, ids)
    video = FakeVideoClient(
        fail={
            ids[0]: VideoProviderNotConfigured(
                "Relax mode for video is limited to Pro and Mega plans"
            )
        }
    )

    result = run_motion(paths, run_id, video=video)

    assert result.passed
    assert result.video_count == 0
    assert result.demoted == len(ids)
    assert len(video.calls) == 1  # 첫 씬에서 알았으면 나머지는 안 던진다


# --- 이어받기 ---------------------------------------------------------------


def test_a_made_clip_is_not_bought_twice(paths):
    """씬당 200초짜리 relax 잡을 두 번 던지지 않는다."""
    run_id, document = install_motion(paths)
    ids = [s["scene_id"] for s in document["scenes"]]
    put_contract(paths, run_id, ids)

    run_motion(paths, run_id, video=FakeVideoClient())
    second = FakeVideoClient()
    run_motion(paths, run_id, video=second)

    assert second.calls == []


def test_a_changed_quadrant_changes_the_fingerprint(paths):
    """`[6r]`이 사분면을 바꾸면 화면의 그림이 바뀐다 — 지문도 바뀌어야 한다.

    그림 파일 해시로는 못 잡는다: 영상은 `task_id`+`quadrant`로 만들어지고
    `images/{scene_id}`가 그대로여도 다른 장에서 나올 수 있다.
    """
    run_id, document = install_motion(paths)
    ids = [s["scene_id"] for s in document["scenes"]]
    put_contract(paths, run_id, ids)
    run_motion(paths, run_id, video=FakeVideoClient())
    before = clips_record(paths, run_id)[ids[0]]["digest"]

    put_contract(paths, run_id, ids, quadrant=1)
    second = FakeVideoClient()
    run_motion(paths, run_id, video=second, force=True)

    after = clips_record(paths, run_id)[ids[0]]
    assert after["digest"] != before
    assert after["quadrant"] == 1
    assert {c.quadrant for c in second.calls} == {1}


# --- 동시 제출 ---------------------------------------------------------------


def test_workers_come_from_the_provider_not_the_stage(paths):
    """단계가 숫자를 적지 않는다 (ADR-0031 G3와 같은 계약)."""
    run_id, document = install_motion(paths)
    ids = [s["scene_id"] for s in document["scenes"]]
    put_contract(paths, run_id, ids)

    result = run_motion(paths, run_id, video=FakeVideoClient(workers=3))

    assert result.workers == 3
    assert [s["scene_id"] for s in result.scenes] == ids  # 순서는 씬 순서다


def test_kenburns_only_runs_serially(paths):
    """영상 씬이 없으면 워커는 1이다 — CPU 동시성은 이 단계가 정할 값이 아니다."""
    run_id, _ = install_motion(paths)

    result = run_motion(paths, run_id, video=None)

    assert result.workers == 1
