"""`art` 라인 계약 (ADR-0069·0070).

- MJ 한 줄의 형식·예산은 `vocab.json` `meta.mj_dialect`에만 있다 (ADR-0034). 코드는 읽는다
- 어기면 **제출 전에** 멈춘다 — 나중에 실패하면 사유가 "3분 타임아웃"으로 와서 원인이 안 보인다
- 라인이 자기 룩을 지고 **엔진마다 말이 다르다** (`mj_style`·`ttv_style`, ADR-0075) —
  `local`·`api`는 전역 문자열 그대로
- `art`의 **일반 씬**은 스타일을 프레임이 지므로 영상 프롬프트에 STYLE 절이 없다.
  같은 라인의 `info` 씬은 텍스트→영상이라 STYLE 절이 있다 (ADR-0075 결정 3)
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
STYLE = vocab.line_style("art", engine=vocab.MJ_ENGINE)
#: `ftyp` 박스가 있어야 `GeneratedClip`이 받는다 — 어댑터가 JSON과 영상을 가르는 검사다.
MP4 = bytes([0, 0, 0, 24]) + b"ftypmp42" + bytes(16)


# --- 어휘 → 코드 (ADR-0034) -------------------------------------------------


def test_budget_comes_from_the_vocabulary_not_the_code():
    assert vr.MJ_WORDS_MIN == vocab.mj_dialect("words_min")
    assert vr.MJ_WORDS_MAX == vocab.mj_dialect("words_max")
    assert vr.MJ_ORDER == vocab.mj_dialect("order")


def test_art_line_carries_its_own_base_style():
    """라인이 자기 룩을 진다 — 전역을 대체하지 않는다 (ADR-0070)."""
    assert vocab.line_style("art", engine=vocab.MJ_ENGINE) != vocab.style("base_style")
    assert vocab.line_style("art", engine=vocab.TTV_ENGINE) != vocab.style("base_style")
    assert vocab.line_style("local") == vocab.style("base_style")


def test_the_two_engines_get_different_wording():
    """같은 앵커에서 나오지만 **쓰는 말이 엔진의 것**이다 (ADR-0075 결정 7).

    MJ는 명사구 나열이라 47단어 예산이 걸리고, H3 TTV는 서술형이라 그 예산이 없다.
    한 문자열로는 한쪽이 반드시 규약 밖이다 (ADR-0027).
    """
    mj = vocab.line_style("art", engine=vocab.MJ_ENGINE)
    ttv = vocab.line_style("art", engine=vocab.TTV_ENGINE)
    assert mj != ttv
    # MJ 방언은 문장을 만들지 않는다 — 마침표로 끝나는 서술형이면 규약 밖이다.
    assert not mj.rstrip().endswith(".")
    assert ttv.rstrip().endswith(".")


def test_only_art_lets_the_frames_carry_the_style():
    assert vocab.style_in_frames("art") is True
    for line in vocab.values("video_line"):
        if line != "art":
            assert vocab.style_in_frames(line) is False


# --- MJ 한 줄 (ADR-0069) ----------------------------------------------------


def test_subject_comes_before_style():
    """어순이 곧 가중치다 — 뒤집으면 소재가 죽는다 (실측)."""
    style = vocab.line_style("art", engine=vocab.MJ_ENGINE)
    line = vr.build_mj_prompt(
        subject=SUBJECT, mj_style=style,
        negatives=vr.negative_items(has_info=False),
    )
    body = line.split("--", 1)[0]
    # 스타일 낱말을 여기 옮겨 적지 않는다 — 룩은 계약이 바꾼다 (ADR-0034 §3)
    assert body.index("sine wave") < body.index(style.split(",")[0])


def test_flags_follow_the_mj_dialect():
    line = vr.build_mj_prompt(subject=SUBJECT, mj_style=STYLE, negatives=["a", "b"])
    assert f"--ar {vr.ASPECT_RATIO}" in line
    assert "--no a, b" in line


def test_budget_is_measured_on_the_body_not_the_flags():
    """플래그(`--no` 15항목)는 예산에 안 든다 — MJ가 무시하는 것은 본문의 꼬리다."""
    line = vr.build_mj_prompt(
        subject=SUBJECT, mj_style=vocab.line_style("art", engine=vocab.MJ_ENGINE),
        negatives=vr.negative_items(has_info=False),
    )
    assert vr.MJ_WORDS_MIN <= vr.mj_body_words(line) <= vr.MJ_WORDS_MAX
    assert len(line.split()) > vr.MJ_WORDS_MAX  # 플래그까지 세면 넘는다


def test_too_short_is_refused_before_submit():
    with pytest.raises(vr.MJPromptError):
        vr.build_mj_prompt(subject="a bridge", mj_style="watercolour", negatives=[])


def test_too_long_is_refused_before_submit():
    with pytest.raises(vr.MJPromptError):
        vr.build_mj_prompt(
            subject=" ".join(["word"] * (vr.MJ_WORDS_MAX + 10)),
            mj_style=STYLE, negatives=[],
        )


def test_multi_prompt_is_refused():
    """`::`는 v8.2가 거절한다 — 우리 쪽에서 먼저 막는다."""
    with pytest.raises(vr.MJPromptError):
        vr.check_mj_prompt("a :: b --ar 9:16")


def test_colons_are_flattened_to_commas():
    """MJ는 `:`·줄바꿈을 구분자로 읽지 않아 라벨이 화면 지시로 섞인다 (ADR-0027)."""
    line = vr.build_mj_prompt(
        subject=SUBJECT, mj_style="STYLE: " + STYLE.replace(", ", "; ", 3),
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


# --- 프레임을 받는 이유가 둘이다 (ADR-0087) ----------------------------------


def test_the_two_frame_switches_are_different_axes():
    """`style_in_frames`와 `reference_frames`는 겹치지 않는다 — 다른 것을 프레임이 진다.

    **어느 라인이 어느 스위치를 켰는지는 여기서 못박지 않는다** — `local`의
    `reference_frames`는 2026-09-02에 꺼졌고(H3가 프레임을 이어 그리지 않는다) 다시 켜질 수
    있다. 계약은 "둘이 다른 축이고 겹치지 않는다"는 것이다.
    """
    assert vocab.style_in_frames("art") is True
    assert vocab.reference_frames("art") is False
    # 둘 다 참인 라인은 없다 — 있으면 프롬프트 규약이 서로를 덮는다.
    for line in vocab.values("video_line"):
        assert not (vocab.style_in_frames(line) and vocab.reference_frames(line))
        # 어느 한쪽이라도 참이면 그 라인의 `info` 없는 씬은 `[6]`을 탄다.
        assert vocab.scene_takes_frames(line, has_info=False) is (
            vocab.style_in_frames(line) or vocab.reference_frames(line)
        )
        assert vocab.scene_takes_frames(line, has_info=True) is False


def test_the_reference_line_keeps_the_style_clause_in_the_video_prompt():
    """스타일을 프레임이 지지 않으므로 말로 시켜야 한다 (ADR-0087).

    `art`의 일반 씬은 STYLE 절이 없고, `local`의 같은 씬은 있다 — 갈리는 것은
    `style_in_frames`이지 "프레임을 받는가"가 아니다.
    """
    common = dict(
        subject_prompt="a bronze bell hanging in a wooden frame",
        staging="location",
        camera="slow_zoom_in",
        camera_target="the bell lip",
        action_prompt=None,
        red_prompt=None,
    )
    art_prompt, _ = vr.build_video_prompt(**common, frames=True, style="")
    local_prompt, _ = vr.build_video_prompt(
        **common, frames=False, style=vocab.line_style("local", engine=vocab.TTV_ENGINE)
    )
    assert "STYLE" not in art_prompt and "SUBJECT" not in art_prompt
    assert "SUBJECT" in local_prompt
    assert vocab.style("base_style").split(".")[0] in local_prompt


def test_the_still_style_is_the_clip_style_minus_one_clause():
    """`frame_style`은 `base_style`에서 한 절만 뺀 것이다 (ADR-0087 결정 5, 맥락 5).

    둘이 따로 놀면 프레임과 클립의 그림체가 갈린다 — 그래서 어휘를 최대한 공유한다.
    """
    base = vocab.style("base_style")
    frame = vocab.style("frame_style")
    culprit = "real surface microdetail - grain, wear, weave and tool marks on every material, "
    assert culprit in base, "base_style이 바뀌었다 — frame_style을 다시 잰다"
    assert base.replace(culprit, "") == frame
    # 실측이 남기라고 한 절은 살아 있다 (뺀 변종이 전경의 손을 잃었다).
    assert "shallow depth of field on foreground detail" in frame
