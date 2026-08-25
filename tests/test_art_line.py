"""`art` 라인 계약 (ADR-0069·0070).

- MJ 한 줄의 형식·예산은 `vocab.json` `meta.mj_dialect`에만 있다 (ADR-0034). 코드는 읽는다
- 어기면 **제출 전에** 멈춘다 — 나중에 실패하면 사유가 "3분 타임아웃"으로 와서 원인이 안 보인다
- 라인이 자기 룩을 진다 (`video_line.art.base_style`) — `local`·`api`는 전역 문자열 그대로
- `art`는 스타일을 프레임이 지므로 영상 프롬프트에 STYLE 절을 싣지 않는다
- MJ 영상 어댑터는 **공개 https 주소만** 받는다 (로컬 주소는 3분 뒤 `Invalid link`로 죽는다)
"""

from __future__ import annotations

import json

import pytest

from shorts_factory.schemas import vocab
from shorts_factory.schemas import visual_rules as vr
from shorts_factory.videogen.base import VideoGenError, VideoRequest
from shorts_factory.videogen.midjourney import (
    MidjourneyEndImageClient,
    extract_button,
    public_url,
)

SUBJECT = (
    "two sine wave curves on one shared time axis, ten wide crests above, twelve tight "
    "crests below, pale grey posts at each end, ruled baseline with fine ticks, pale grey "
    "studio floor, low three-quarter angle"
)
#: 예산을 채우는 스타일 픽스처. 짧은 문자열은 계약이 정당히 거절하므로 실물 길이를 쓴다.
STYLE = vocab.line_style("art")
#: `ftyp` 박스가 있어야 `GeneratedClip`이 받는다 — 어댑터가 JSON과 영상을 가르는 검사다.
MP4 = bytes([0, 0, 0, 24]) + b"ftypmp42" + bytes(16)


# --- 어휘 → 코드 (ADR-0034) -------------------------------------------------


def test_budget_comes_from_the_vocabulary_not_the_code():
    assert vr.MJ_WORDS_MIN == vocab.mj_dialect("words_min")
    assert vr.MJ_WORDS_MAX == vocab.mj_dialect("words_max")
    assert vr.MJ_ORDER == vocab.mj_dialect("order")


def test_art_line_carries_its_own_base_style():
    """라인이 자기 룩을 진다 — 전역을 대체하지 않는다 (ADR-0070)."""
    assert vocab.line_style("art") != vocab.style("base_style")
    assert vocab.line_style("local") == vocab.style("base_style")


def test_only_art_lets_the_frames_carry_the_style():
    assert vocab.style_in_frames("art") is True
    for line in vocab.values("video_line"):
        if line != "art":
            assert vocab.style_in_frames(line) is False


# --- MJ 한 줄 (ADR-0069) ----------------------------------------------------


def test_subject_comes_before_style():
    """어순이 곧 가중치다 — 뒤집으면 소재가 죽는다 (실측)."""
    style = vocab.line_style("art")
    line = vr.build_mj_prompt(
        subject=SUBJECT, base_style=style,
        negatives=vr.negative_items(has_info=False),
    )
    body = line.split("--", 1)[0]
    # 스타일 낱말을 여기 옮겨 적지 않는다 — 룩은 계약이 바꾼다 (ADR-0034 §3)
    assert body.index("sine wave") < body.index(style.split(",")[0])


def test_flags_follow_the_mj_dialect():
    line = vr.build_mj_prompt(subject=SUBJECT, base_style=STYLE, negatives=["a", "b"])
    assert f"--ar {vr.ASPECT_RATIO}" in line
    assert "--no a, b" in line


def test_budget_is_measured_on_the_body_not_the_flags():
    """플래그(`--no` 15항목)는 예산에 안 든다 — MJ가 무시하는 것은 본문의 꼬리다."""
    line = vr.build_mj_prompt(
        subject=SUBJECT, base_style=vocab.line_style("art"),
        negatives=vr.negative_items(has_info=False),
    )
    assert vr.MJ_WORDS_MIN <= vr.mj_body_words(line) <= vr.MJ_WORDS_MAX
    assert len(line.split()) > vr.MJ_WORDS_MAX  # 플래그까지 세면 넘는다


def test_too_short_is_refused_before_submit():
    with pytest.raises(vr.MJPromptError):
        vr.build_mj_prompt(subject="a bridge", base_style="watercolour", negatives=[])


def test_too_long_is_refused_before_submit():
    with pytest.raises(vr.MJPromptError):
        vr.build_mj_prompt(
            subject=" ".join(["word"] * (vr.MJ_WORDS_MAX + 10)),
            base_style=STYLE, negatives=[],
        )


def test_multi_prompt_is_refused():
    """`::`는 v8.2가 거절한다 — 우리 쪽에서 먼저 막는다."""
    with pytest.raises(vr.MJPromptError):
        vr.check_mj_prompt("a :: b --ar 9:16")


def test_colons_are_flattened_to_commas():
    """MJ는 `:`·줄바꿈을 구분자로 읽지 않아 라벨이 화면 지시로 섞인다 (ADR-0027)."""
    line = vr.build_mj_prompt(
        subject=SUBJECT, base_style="STYLE: " + STYLE.replace(", ", "; ", 3),
        negatives=[],
    )
    body = line.split("--", 1)[0]
    assert ":" not in body and ";" not in body and "\n" not in body


# --- 어댑터 (ADR-0070) ------------------------------------------------------


def test_local_urls_are_refused_before_the_job_is_bought():
    """MJ는 자기가 닿는 주소만 받는다. 안 막으면 3분 뒤 `Invalid link`로 죽는다."""
    for bad in ("http://localhost:8086/a.png", "/tmp/a.png", ""):
        with pytest.raises(VideoGenError):
            public_url(bad, what="last_frame")
    assert public_url("https://pub-x.r2.dev/a.png", what="last_frame")


def test_extract_button_is_read_not_assembled():
    task = {"id": "1", "buttons": [
        {"customId": "MJ::JOB::reroll::0::abc::SOLO"},
        {"customId": "MJ::JOB::video_virtual_upscale::1::abc"},
    ]}
    assert extract_button(task).endswith("video_virtual_upscale::1::abc")
    with pytest.raises(VideoGenError):
        extract_button({"id": "2", "buttons": []})


def test_turbo_is_not_a_mode():
    """MJ가 `Turbo mode isn't supported for video jobs`로 거절한다 (실측)."""
    with pytest.raises(Exception):
        MidjourneyEndImageClient(mode="turbo")


def test_endimage_rides_the_video_submit_body():
    """`last_frame`이 있으면 `endImage`로 실린다 — 없으면 안 싣는다 (옛 동작 유지)."""
    calls: list[tuple[str, dict]] = []

    def transport(method, url, headers, body, timeout):
        payload = json.loads(body.decode()) if body else {}
        calls.append((url, payload))
        if url.endswith("/mj/submit/video"):
            return 200, json.dumps({"code": 1, "result": "v1"}).encode()
        if url.endswith("/mj/submit/action"):
            return 200, json.dumps({"code": 1, "result": "u1"}).encode()
        if "/mj/task/v1/fetch" in url:
            return 200, json.dumps({
                "id": "v1", "status": "SUCCESS",
                "buttons": [{"customId": "MJ::JOB::video_virtual_upscale::1::j"}],
            }).encode()
        if "/mj/task/u1/fetch" in url:
            return 200, json.dumps({
                "id": "u1", "status": "SUCCESS", "imageUrl": "https://cdn/x.mp4",
            }).encode()
        return 200, MP4

    client = MidjourneyEndImageClient(transport=transport, sleep=lambda _: None)
    clip = client.generate(VideoRequest(
        scene_id=1,
        first_frame="https://cdn.discordapp.com/clean.png",
        last_frame="https://pub-x.r2.dev/info.png",
        motion_prompt="slow drift",
    ))
    submit = next(p for u, p in calls if u.endswith("/mj/submit/video"))
    assert submit["endImage"] == "https://pub-x.r2.dev/info.png"
    assert submit["image"] == "https://cdn.discordapp.com/clean.png"
    assert clip.data == MP4

    calls.clear()
    client.generate(VideoRequest(
        scene_id=2, first_frame="https://cdn.discordapp.com/clean.png",
        motion_prompt="slow drift",
    ))
    submit = next(p for u, p in calls if u.endswith("/mj/submit/video"))
    assert "endImage" not in submit


def test_mode_picks_the_endpoint_prefix_not_a_flag():
    """모드는 프리픽스가 정한다 — 틀리면 다른 엔드포인트가 된다 (ADR-0039 §4)."""
    assert MidjourneyEndImageClient(mode="fast").prefix == "/mj-fast"
    assert MidjourneyEndImageClient(mode="relax").prefix == "/mj-relax"
