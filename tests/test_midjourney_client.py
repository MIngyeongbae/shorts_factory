"""Midjourney 어댑터 (ADR-0025) — 프록시 REST의 제출·폴링·내려받기.

실호출 없이 전 경로를 본다. HTTP 경계가 `transport` 하나라서 가능하다
(`nano_banana.py`와 같은 모양).

확인 대상:
- **relax는 URL 프리픽스로 고른다.** Fast로 새면 이미지 27잡이 GPU 27분을 태운다
- 보내는 문자열은 `prompt` + 공백 + `negative_prompt` (ADR-0027이 검증한 그 조합)
- `imageUrls[0].url`을 쓴다 — 썸네일도, 4장 병합 그리드(`imageUrl`)도 아니다
- 방언 선언(`dialect = "mj"`) — 빠지면 `[6]`의 대조가 무력해진다
"""

from __future__ import annotations

import json

import pytest

from shorts_factory.imagegen.base import (
    ImageGenError,
    ImageGenTimeout,
    ImageRequest,
    ProviderNotConfigured,
)
from shorts_factory.imagegen.midjourney import (
    SUBMIT_PATH,
    MidjourneyClient,
    build_prompt,
    result_image,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64

CDN = "https://cdn.midjourney.com/6defdb42/0_0.png"

REQUEST = ImageRequest(
    scene_id=1,
    prompt="홈이 파인 블록 접합면 클로즈업, 콘크리트, tight detail --ar 9:16",
    negative_prompt="--no burned-in subtitles, legible text",
    aspect_ratio="9:16",
    resolution="2K",
)


def fake_transport(script):
    """`(method, url) -> (status, payload)` 대본을 받아 transport를 만든다.

    호출 기록을 `calls`에 남긴다 — 어느 엔드포인트로 갔는지가 이 어댑터의 핵심이다.
    """
    calls: list[tuple[str, str, bytes | None]] = []

    def transport(method, url, headers, body, timeout):
        calls.append((method, url, body))
        for match, response in script:
            if match in url:
                status, payload = response.pop(0) if isinstance(response, list) else response
                if isinstance(payload, (dict, list)):
                    return status, json.dumps(payload).encode("utf-8")
                return status, payload
        raise AssertionError(f"대본에 없는 호출이다: {method} {url}")

    transport.calls = calls
    return transport


def success_payload(url: str = CDN) -> dict:
    return {
        "status": "SUCCESS",
        "progress": "100%",
        "imageUrl": "http://localhost:8086/attachments/merges/merged_x.webp",
        "imageUrls": [
            {"url": url, "thumbnail": "https://cdn.midjourney.com/x/0_0_640_N.webp"},
            {"url": "https://cdn.midjourney.com/x/0_1.png", "thumbnail": ""},
        ],
    }


def client(script, **kw):
    return MidjourneyClient(
        base_url="http://proxy:8086",
        secret="admin",
        transport=fake_transport(script),
        poll_interval=0,
        sleep=lambda _s: None,
        **kw,
    )


# --- 보내는 것 ---------------------------------------------------------------


def test_the_shipped_string_is_prompt_then_negative():
    """ADR-0027이 검증한 조합이다. 여기서 재조립하면 검증한 적 없는 문자열이 나간다."""
    assert build_prompt(REQUEST) == f"{REQUEST.prompt} {REQUEST.negative_prompt}"


def test_an_empty_negative_leaves_no_trailing_space():
    request = ImageRequest(
        scene_id=1, prompt="댐 --ar 9:16", negative_prompt="",
        aspect_ratio="9:16", resolution="2K",
    )
    assert build_prompt(request) == "댐 --ar 9:16"


def test_the_submit_goes_to_the_relax_endpoint():
    """**Fast로 새면 이미지 27잡이 GPU 27분을 태운다** (ADR-0025)."""
    c = client([("submit", (200, {"code": 1, "result": "task-1"}))])
    c.submit(REQUEST, timeout=30)

    method, url, body = c.transport.calls[0]
    assert method == "POST"
    assert url == f"http://proxy:8086{SUBMIT_PATH}"
    assert "/mj-relax/" in url and "/mj-fast/" not in url
    # 프록시가 `--relax`를 붙인다. 어댑터가 프롬프트를 만지지 않는다.
    assert "--relax" not in json.loads(body)["prompt"]


def test_the_korean_prompt_survives_json_encoding():
    c = client([("submit", (200, {"code": 1, "result": "t"}))])
    c.submit(REQUEST, timeout=30)

    sent = json.loads(c.transport.calls[0][2].decode("utf-8"))["prompt"]
    assert "홈이 파인 블록 접합면 클로즈업" in sent


@pytest.mark.parametrize("code", [1, 21, 22])
def test_queued_submissions_are_accepted(code):
    """21·22는 큐에 들어갔다는 뜻이고 taskId를 준다. 실패로 치면 멀쩡한 잡을 버린다."""
    c = client([("submit", (200, {"code": code, "result": "task-9"}))])
    assert c.submit(REQUEST, timeout=30) == "task-9"


def test_a_rejected_submission_carries_the_proxy_reason():
    """실측한 실패다 — 계정 인스턴스가 없으면 code 3이 온다."""
    c = client([("submit", (200, {"code": 3, "description": "무가용 계정", "result": None}))])
    with pytest.raises(ImageGenError, match="code=3"):
        c.submit(REQUEST, timeout=30)


def test_auth_rejection_stops_the_whole_provider():
    """씬 하나의 실패가 아니다. 27씬을 같은 오류로 두 번씩 실패시키지 않는다."""
    c = client([("submit", (403, b"forbidden"))])
    with pytest.raises(ProviderNotConfigured):
        c.submit(REQUEST, timeout=30)


# --- 폴링 -------------------------------------------------------------------


def test_the_discord_grid_is_cropped_before_it_is_returned():
    """씬마다 2×2 그리드가 들어가면 `[7]`이 그것을 그대로 움직인다."""
    grid = "http://localhost:8086/attachments/1/2/apple.webp"
    calls: list[tuple[bytes, str]] = []

    def fake_crop(data, *, suffix, ffmpeg):
        calls.append((data, suffix))
        return PNG

    c = client([
        ("submit", (200, {"code": 1, "result": "task-1"})),
        ("/fetch", (200, {"status": "SUCCESS", "imageUrls": None, "imageUrl": grid})),
        (grid, (200, b"RIFF____WEBPgrid")),
    ])
    import shorts_factory.imagegen.midjourney as mj

    original, mj.crop_first_quadrant = mj.crop_first_quadrant, fake_crop
    try:
        image = c.generate(REQUEST, timeout=60)
    finally:
        mj.crop_first_quadrant = original

    assert calls == [(b"RIFF____WEBPgrid", ".webp")]
    assert image.data == PNG and image.raw["grid"] is True


def test_it_polls_until_success_then_downloads():
    c = client([
        ("submit", (200, {"code": 1, "result": "task-1"})),
        ("/fetch", [
            (200, {"status": "SUBMITTED"}),
            (200, {"status": "IN_PROGRESS", "progress": "40%"}),
            (200, success_payload()),
        ]),
        (CDN, (200, PNG)),
    ])
    image = c.generate(REQUEST, timeout=60)

    assert image.data == PNG
    assert image.mime_type == "image/png"
    assert image.request_id == "task-1"
    assert [m for m, _u, _b in c.transport.calls] == ["POST", "GET", "GET", "GET", "GET"]


def test_a_failed_task_reports_its_reason():
    c = client([
        ("submit", (200, {"code": 1, "result": "task-1"})),
        ("/fetch", (200, {"status": "FAILURE", "failReason": "서비스 이상"})),
    ])
    with pytest.raises(ImageGenError, match="서비스 이상"):
        c.generate(REQUEST, timeout=60)


def test_a_slow_task_times_out_without_resubmitting():
    """relax 큐가 밀린 것뿐이면 **같은 잡을 또 제출하는 것이 더 나쁘다.**"""
    ticks = iter([0.0, 0.0, 100.0, 200.0])
    c = client(
        [
            ("submit", (200, {"code": 1, "result": "task-1"})),
            ("/fetch", (200, {"status": "IN_PROGRESS"})),
        ],
        clock=lambda: next(ticks),
    )
    with pytest.raises(ImageGenTimeout, match="task-1"):
        c.generate(REQUEST, timeout=60)

    assert [m for m, _u, _b in c.transport.calls].count("POST") == 1


# --- 응답 파싱 ---------------------------------------------------------------


def test_official_mode_takes_the_full_image_not_the_thumbnail():
    """공식 웹 모드는 4장을 개별 URL로 준다. 썸네일은 640px 축소본이라 안 쓴다."""
    assert result_image(success_payload()) == (CDN, False)


def test_a_bare_string_url_is_also_accepted():
    assert result_image({"imageUrls": [CDN]}) == (CDN, False)


def test_discord_mode_falls_back_to_the_grid_and_asks_for_a_crop():
    """실측: Discord 모드는 `imageUrls`가 null이고 `imageUrl`이 2×2 그리드다.

    개별 URL이 없으므로 4분할한다 — ADR-0025가 남겨 둔 예비 경로다. U1 업스케일 잡을
    따로 던지면 같은 픽셀을 얻자고 잡 수가 27 → 54가 된다.
    """
    grid = "http://localhost:8086/attachments/1/2/apple_7ba7dd71.webp?ex=6a7d"
    assert result_image({"status": "SUCCESS", "imageUrls": None, "imageUrl": grid}) == (
        grid,
        True,
    )


def test_no_image_at_all_fails_loudly():
    with pytest.raises(ImageGenError, match="이미지 URL이 없다"):
        result_image({"status": "SUCCESS", "imageUrls": None, "imageUrl": None})


def test_the_cdn_download_carries_no_proxy_secret():
    """CDN 주소다. 프록시 토큰을 외부 호스트로 보내지 않는다."""
    c = client([(CDN, (200, PNG))])
    c.download(CDN, timeout=30)
    assert c.transport.calls == [("GET", CDN, None)]


# --- 계약 선언 ---------------------------------------------------------------


def test_the_adapter_declares_its_dialect():
    """빠뜨리면 ADR-0027의 대조가 무력해지고, 틀린 문법이 과금 뒤에야 실패한다."""
    assert MidjourneyClient.dialect == "mj"


def test_style_anchors_are_not_required_on_this_path():
    """`--sref`가 BASE_STYLE을 이겨 마감을 망가뜨렸다 (ADR-0025 G3). 0장이 정상이다."""
    assert MidjourneyClient.requires_style_anchors is False


# --- 워커 수는 프록시가 안다 (ADR-0031 §4·G3) ---------------------------------
#
# 이 값을 리포에 적어 두면 구독 플랜을 바꾼 날 조용히 틀린다. 그래서 계정에서 읽는다.
# 대신 **읽기에 실패해도 단계가 멈추면 안 된다** — 얼마나 빨리 돌릴지의 문제이지
# 그림을 만들 수 있느냐의 문제가 아니다.


def accounts_payload(*accounts) -> dict:
    return {"list": list(accounts), "pagination": {"total": len(accounts)}}


def account(*, enable=True, relax=3, token="secret-token") -> dict:
    # 실제 응답에는 userToken·cookie가 함께 온다 (실측). 그것까지 흉내 내야
    # "비밀을 어디에도 남기지 않는다"를 확인할 수 있다.
    return {
        "id": "3fc4f795", "enable": enable, "relaxCoreSize": relax,
        "coreSize": 3, "userToken": token, "cookie": None,
    }


def test_concurrency_reads_relax_core_size_from_the_account():
    mj = client([("/mj/admin/accounts", (200, accounts_payload(account(relax=3))))])
    assert mj.concurrency() == 3


def test_concurrency_ignores_disabled_accounts():
    """잡은 활성 계정으로만 간다. 꺼진 계정의 한도를 따르면 근거 없는 숫자다."""
    mj = client([(
        "/mj/admin/accounts",
        (200, accounts_payload(account(enable=False, relax=9), account(relax=3))),
    )])
    assert mj.concurrency() == 3


def test_concurrency_takes_the_smallest_of_several_accounts():
    """잡이 어느 계정으로 갈지는 프록시가 정한다. 큰 쪽에 맞추면 작은 쪽이 429를 낸다."""
    mj = client([(
        "/mj/admin/accounts",
        (200, accounts_payload(account(relax=5), account(relax=2))),
    )])
    assert mj.concurrency() == 2


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
    """못 읽으면 1이다. 느려질 뿐 틀리지 않는다 — 여기서 예외를 올리면 그림이 안 나온다."""
    mj = client([("/mj/admin/accounts", response)])
    assert mj.concurrency() == 1


def test_concurrency_does_not_leak_the_account_token(caplog):
    """응답에 userToken이 실려 온다. 로그에 새면 리포 밖으로 나간다 (ADR-0032 §3)."""
    mj = client([("/mj/admin/accounts", (500, {"userToken": "secret-token"}))])
    with caplog.at_level("WARNING"):
        assert mj.concurrency() == 1
    assert "secret-token" not in caplog.text


def test_concurrency_uses_the_admin_endpoint_not_the_relax_prefix():
    """`/mj-relax/` 프리픽스는 제출 전용이다. 관리 API에 붙이면 404가 난다."""
    transport = fake_transport([
        ("/mj/admin/accounts", (200, accounts_payload(account())))
    ])
    MidjourneyClient(
        base_url="http://proxy:8086", secret="admin", transport=transport
    ).concurrency()
    method, url, _ = transport.calls[0]
    assert method == "POST"
    assert url.endswith("/mj/admin/accounts")
    assert "/mj-relax/" not in url
