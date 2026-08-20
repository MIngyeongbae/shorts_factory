"""MJ 영상 어댑터 — 프록시 사슬. ADR-0039 (+ADR-0025의 정정 3건).

ADR-0025는 처음 실측을 **셋이나 틀리게** 적어 뒀고, 그것을 그대로 구현했으면 셋 다
걸렸다. 그 셋이 이 파일이 잠그는 것이다.

1. 모드 — `/mj-relax/`다. 프록시의 자동 변환에 맡기지 않는다 (ADR-0039 §4)
2. mp4 주소 — `videoUrl`이 아니라 `imageUrl`에 온다. 여기를 틀리면 **성공한 잡에서
   빈손으로** 돌아온다
3. 플랜 — Pro 미만은 거절이고, 씬 하나가 아니라 프로바이더 전체의 문제다

여기에 ADR-0039이 더한 것 둘: `batchSize: 1`(GPU 3.25분의 1)과 **`[6r]`이 고른
사분면**에서 영상이 나와야 한다는 것.
"""

import json

import pytest

from shorts_factory.videogen.base import (
    VideoGenError,
    VideoGenTimeout,
    VideoProviderNotConfigured,
    VideoRequest,
)
from shorts_factory.videogen.midjourney import (
    ACTION_RELAX_PATH,
    VIDEO_SUBMIT_PATH,
    MidjourneyVideoClient,
    upsample_button,
)

#: 최소 mp4. `GeneratedClip`이 `ftyp`를 앞 32바이트에서 찾는다.
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"

CDN = "https://cdn.discordapp.com/attachments/1/2/upscaled.png"
MP4_URL = "https://cdn.discordapp.com/attachments/1/3/clip.mp4"

REQUEST = VideoRequest(
    scene_id=4,
    source_task_id="image-job",
    quadrant=0,
    motion_prompt="slow push in toward the subject",
    duration=3.9,
)


def buttons(*custom_ids):
    return [{"customId": c} for c in custom_ids]


IMAGE_TASK = {
    "id": "image-job",
    "status": "SUCCESS",
    "buttons": buttons(
        "MJ::JOB::upsample::1::abc",
        "MJ::JOB::upsample::2::abc",
        "MJ::JOB::upsample::3::abc",
        "MJ::JOB::upsample::4::abc",
    ),
}


def fake_transport(routes):
    """`(url 조각, 응답)` 목록. 응답이 리스트면 호출 순서대로 꺼낸다."""
    calls: list[tuple[str, str, bytes | None]] = []

    def transport(method, url, headers, body, timeout):
        calls.append((method, url, body))
        for match, response in routes:
            if match in url:
                payload = response.pop(0) if isinstance(response, list) else response
                status, data = payload
                if isinstance(data, (dict, list)):
                    return status, json.dumps(data).encode("utf-8")
                return status, data
        raise AssertionError(f"대본에 없는 호출이다: {method} {url}")

    transport.calls = calls
    return transport


def chain(*, upscale_url=CDN, mp4_url=MP4_URL, video_extra=None):
    """UPSCALE → VIDEO → U1 → mp4가 전부 성공하는 대본."""
    video_task = {
        "id": "video-job",
        "status": "SUCCESS",
        "mode": "RELAX",
        "buttons": buttons("MJ::JOB::video_virtual_upscale::1::xyz"),
        "properties": {"finalPrompt": "… --bs 1 --relax --video 1"},
    }
    video_task.update(video_extra or {})
    return [
        ("/mj/submit/action", [
            (200, {"code": 1, "result": "upscale-job"}),
            (200, {"code": 1, "result": "extract-job"}),
        ]),
        ("/mj/submit/video", (200, {"code": 1, "result": "video-job"})),
        ("/mj/task/image-job/fetch", (200, IMAGE_TASK)),
        ("/mj/task/upscale-job/fetch", (200, {
            "id": "upscale-job", "status": "SUCCESS", "url": upscale_url,
        })),
        ("/mj/task/video-job/fetch", (200, video_task)),
        ("/mj/task/extract-job/fetch", (200, {
            "id": "extract-job", "status": "SUCCESS",
            "imageUrl": mp4_url, "videoUrl": None, "videoUrls": None,
            "width": 464, "height": 832,
        })),
        (mp4_url, (200, MP4)),
    ]


def client(routes, **kw):
    return MidjourneyVideoClient(
        base_url="http://proxy:8086",
        secret="admin",
        transport=fake_transport(routes),
        poll_interval=0,
        sleep=lambda _s: None,
        **kw,
    )


def posts(transport, path_fragment):
    return [c for c in transport.calls if c[0] == "POST" and path_fragment in c[1]]


# --- 사슬이 실제로 닫힌다 ----------------------------------------------------


def test_the_chain_produces_an_mp4():
    c = client(chain())

    clip = c.generate(REQUEST, timeout=60)

    assert clip.data == MP4
    assert clip.width == 464 and clip.height == 832
    assert clip.raw["batch_size"] == 1


# --- 정정 1: 모드는 엔드포인트가 정한다 (ADR-0039 §4) ------------------------


def test_the_video_goes_to_the_relax_endpoint():
    """relax가 GPU 0이고, 그것이 전 씬 영상을 가능하게 한 유일한 조건이다."""
    c = client(chain())

    c.generate(REQUEST, timeout=60)

    url = posts(c.transport, "/mj/submit/video")[0][1]
    assert url.endswith(VIDEO_SUBMIT_PATH)
    assert "/mj-relax/" in url and "/mj-fast/" not in url


def test_the_upscale_goes_to_relax_too():
    """그리드에서 한 장 뽑는 자리다. 과금 0이라 급할 이유가 없다."""
    c = client(chain())

    c.generate(REQUEST, timeout=60)

    assert posts(c.transport, "/mj/submit/action")[0][1].endswith(ACTION_RELAX_PATH)


# --- 정정 2: mp4는 `imageUrl`에 온다 -----------------------------------------


def test_the_mp4_url_is_not_read_from_video_url():
    """`videoUrl`은 `null`이다. 여기를 틀리면 성공한 잡에서 빈손으로 돌아온다."""
    c = client(chain())

    c.generate(REQUEST, timeout=60)

    assert any(MP4_URL in call[1] for call in c.transport.calls)


def test_a_success_without_an_mp4_address_is_an_error():
    routes = chain()
    routes = [r for r in routes if r[0] != "/mj/task/extract-job/fetch"]
    routes.append(("/mj/task/extract-job/fetch", (200, {
        "id": "extract-job", "status": "SUCCESS", "videoUrl": None,
    })))

    with pytest.raises(VideoGenError, match="mp4 주소가 없다"):
        client(routes).generate(REQUEST, timeout=60)


# --- 정정 3: 플랜 미달은 프로바이더 전체의 문제다 ----------------------------


def test_a_plan_refusal_is_not_a_scene_failure():
    """`[7]`은 이 예외를 보고 남은 씬을 시도하지 않는다 (ADR-0039 §4)."""
    routes = [
        ("/mj/task/image-job/fetch", (200, IMAGE_TASK)),
        ("/mj/submit/action", (200, {"code": 1, "result": "upscale-job"})),
        ("/mj/task/upscale-job/fetch", (200, {
            "id": "upscale-job", "status": "SUCCESS", "url": CDN,
        })),
        ("/mj/submit/video", (200, {
            "code": 24,
            "description": "Invalid request, Relax mode for video is limited to "
                           "Pro and Mega plans. Upgrade your plan or switch to Fast mode.",
        })),
    ]

    with pytest.raises(VideoProviderNotConfigured, match="Pro"):
        client(routes).generate(REQUEST, timeout=60)


def test_an_auth_refusal_is_also_provider_wide():
    with pytest.raises(VideoProviderNotConfigured, match="MJ_API_SECRET"):
        client([("/mj/task/image-job/fetch", (403, b"forbidden"))]).generate(
            REQUEST, timeout=60
        )


# --- ADR-0039: batchSize 1 ---------------------------------------------------


def test_the_submit_asks_for_one_clip():
    """4개를 사서 1개를 쓰지 않는다. fast로 되돌아갈 때 GPU 3.25배가 걸려 있다."""
    c = client(chain())

    c.generate(REQUEST, timeout=60)

    body = json.loads(posts(c.transport, "/mj/submit/video")[0][2])
    assert body["batchSize"] == 1
    assert body["image"] == CDN
    assert body["prompt"] == REQUEST.motion_prompt


def test_a_local_address_is_refused_before_the_video_call():
    """MJ는 자기가 닿는 주소만 받는다. localhost를 넘기면 `Invalid link`로 죽는다."""
    routes = [r for r in chain() if r[0] != "/mj/task/upscale-job/fetch"]
    routes.append(("/mj/task/upscale-job/fetch", (200, {
        "id": "upscale-job", "status": "SUCCESS",
        "url": "http://localhost:8086/attachments/x.png",
    })))

    with pytest.raises(VideoGenError, match="https"):
        client(routes).generate(REQUEST, timeout=60)


# --- ADR-0031 §2: 영상은 화면에 쓰는 그 장에서 나온다 ------------------------


def test_the_quadrant_picks_the_upsample_button():
    assert upsample_button(IMAGE_TASK, 0).endswith("upsample::1::abc")
    assert upsample_button(IMAGE_TASK, 2).endswith("upsample::3::abc")


def test_the_picked_quadrant_reaches_the_upscale_call():
    """q0 고정으로 두면 판정이 고른 그림과 영상이 다른 그림이 된다."""
    c = client(chain())

    c.generate(
        VideoRequest(
            scene_id=4, source_task_id="image-job", quadrant=3,
            motion_prompt="slow downward tilt",
        ),
        timeout=60,
    )

    body = json.loads(posts(c.transport, "/mj/submit/action")[0][2])
    assert "upsample::4::" in body["customId"]
    assert body["taskId"] == "image-job"


def test_a_missing_button_names_what_was_available():
    with pytest.raises(VideoGenError, match="upsample"):
        upsample_button({"buttons": buttons("MJ::JOB::variation::1::x")}, 0)


# --- ADR-0035: 대기 상한은 어댑터가 안다 -------------------------------------


def test_a_stuck_job_times_out_without_killing_it():
    """잡은 프록시에서 계속 돈다. 죽이지 않고 taskId를 남긴다."""
    clock = iter([0.0, 0.0, 999.0, 999.0])
    routes = [
        ("/mj/task/image-job/fetch", (200, IMAGE_TASK)),
        ("/mj/submit/action", (200, {"code": 1, "result": "upscale-job"})),
        ("/mj/task/upscale-job/fetch", (200, {
            "id": "upscale-job", "status": "SUBMITTED", "progress": "0%",
        })),
    ]

    with pytest.raises(VideoGenTimeout, match="upscale-job"):
        client(routes, clock=lambda: next(clock)).generate(REQUEST, timeout=10)


def test_none_falls_back_to_the_adapter_own_budget():
    """단계가 상한을 안 주면 어댑터가 자기 값을 쓴다 (ADR-0035)."""
    c = client(chain())

    c.generate(REQUEST, timeout=None)

    assert c.transport.calls  # 예외 없이 끝났다는 것이 확인이다


# --- ADR-0031 G3: 워커 수는 어댑터가 계정에서 읽는다 --------------------------


def accounts_payload(*accounts):
    return {"list": list(accounts)}


def account(*, enable=True, relax=3):
    return {"id": "3fc4f795", "enable": enable, "relaxCoreSize": relax}


def test_concurrency_reads_relax_core_size_from_the_account():
    """영상 잡은 relax 큐로 간다 (ADR-0039) — 그 큐의 동시 한도가 워커 수다."""
    c = client([("/mj/admin/accounts", (200, accounts_payload(account(relax=3))))])
    assert c.concurrency() == 3


def test_concurrency_ignores_disabled_accounts():
    """잡은 활성 계정으로만 간다. 꺼진 계정의 한도를 따르면 근거 없는 숫자다."""
    c = client([(
        "/mj/admin/accounts",
        (200, accounts_payload(account(enable=False, relax=9), account(relax=3))),
    )])
    assert c.concurrency() == 3


def test_concurrency_takes_the_smallest_of_several_accounts():
    """잡이 어느 계정으로 갈지는 프록시가 정한다. 큰 쪽에 맞추면 작은 쪽이 429를 낸다."""
    c = client([(
        "/mj/admin/accounts",
        (200, accounts_payload(account(relax=5), account(relax=2))),
    )])
    assert c.concurrency() == 2


@pytest.mark.parametrize(
    "response",
    (
        (500, {"error": "그런 거 없다"}),
        (200, {"list": []}),
        (200, {"list": [{"enable": True}]}),   # relaxCoreSize가 없다
        (200, {"pagination": {}}),             # list 자체가 없다
        (200, b"<html>login</html>"),          # JSON이 아니다
    ),
)
def test_concurrency_falls_back_to_one_instead_of_raising(response):
    """워커 수는 속도를 정할 뿐이다 — 못 읽었다고 영상을 못 만드는 것이 아니다."""
    c = client([("/mj/admin/accounts", response)])
    assert c.concurrency() == 1
