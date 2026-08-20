"""Veo 어댑터 계약 (ADR-0043).

- 시작 프레임은 필수, 끝 프레임은 선택 — 로컬 파일을 base64 인라인으로 싣는다
- 클립 길이는 4/6/8초 중 요구 길이를 덮는 최솟값이다 — **단 first/last 보간이면 8초 고정**
- `predictLongRunning` 제출 → 폴링 → (uri 내려받기 | base64) 의 전 경로
- 프로바이더 전체의 문제(401·403·파라미터 거부)와 씬 하나의 실패를 가른다
"""

import base64
import json

import pytest

from shorts_factory.videogen.base import (
    VideoGenError,
    VideoGenRateLimited,
    VideoGenTimeout,
    VideoProviderNotConfigured,
    VideoRequest,
)
from shorts_factory.videogen.veo import (
    ASSEMBLY_PROMPT,
    INTERPOLATION_DURATION,
    VeoClient,
    build_body,
    pick_duration,
)

MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"


@pytest.fixture
def frames(tmp_path):
    clean = tmp_path / "1.png"
    clean.write_bytes(b"clean-png")
    info = tmp_path / "1.jpg"
    info.write_bytes(b"info-jpg")
    return clean, info


def request_for(frames, **overrides) -> VideoRequest:
    clean, info = frames
    defaults = dict(
        scene_id=1, first_frame=clean, last_frame=info,
        motion_prompt="slow push-in", duration=3.2,
    )
    defaults.update(overrides)
    return VideoRequest(**defaults)


class FakeTransport:
    """(method, url) → 준비된 (status, body). 호출을 순서대로 기록한다."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(
            {"method": method, "url": url, "headers": headers,
             "body": json.loads(body) if body else None}
        )
        status, payload = self.responses.pop(0)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return status, raw


def client_with(transport) -> VeoClient:
    return VeoClient(
        api_key="test-key", transport=transport,
        poll_interval=0, sleep=lambda _s: None,
    )


# --- 본문 계약 -----------------------------------------------------------------


def test_body_carries_both_frames_inline(frames):
    body = build_body(request_for(frames))
    instance = body["instances"][0]

    assert instance["image"]["bytesBase64Encoded"] == base64.b64encode(b"clean-png").decode()
    assert instance["image"]["mimeType"] == "image/png"
    assert instance["lastFrame"]["bytesBase64Encoded"] == base64.b64encode(b"info-jpg").decode()
    assert instance["lastFrame"]["mimeType"] == "image/jpeg"


def test_body_is_vertical_and_single_sample(frames):
    body = build_body(request_for(frames))
    assert body["parameters"]["aspectRatio"] == "9:16"
    assert body["parameters"]["sampleCount"] == 1


def test_prompt_carries_the_camera_and_the_assembly_instruction(frames):
    body = build_body(request_for(frames))
    prompt = body["instances"][0]["prompt"]
    assert "slow push-in" in prompt, "카메라 구절은 씬 계약에서 온다"
    assert "assemble in sequence" in prompt, "조립 지시는 어댑터 소관이다 (ADR-0043)"


def test_last_frame_is_optional(frames):
    body = build_body(request_for(frames, last_frame=None))
    assert "lastFrame" not in body["instances"][0]


def test_first_frame_is_required(frames):
    with pytest.raises(VideoGenError, match="첫 프레임"):
        build_body(request_for(frames, first_frame=None))


@pytest.mark.parametrize(
    "needed, chosen",
    [(3.2, 4), (4.0, 4), (4.5, 6), (6.0, 6), (6.1, 8), (9.5, 8)],
)
def test_duration_is_the_smallest_cover(needed, chosen):
    assert pick_duration(needed) == chosen


@pytest.mark.parametrize("needed", [1.0, 3.2, 4.0, 4.566, 6.0, 9.5])
def test_interpolation_ignores_scene_length(needed):
    """first/last 보간은 길이를 고르지 않는다 — Veo가 8초만 받는다 (2026-08-20 실측).

    이 규칙이 없어서 인포씬이 4·6초를 요청했고 한 씬도 빠짐없이 400으로 죽었다.
    """
    assert pick_duration(needed, interpolation=True) == INTERPOLATION_DURATION


def test_last_frame_forces_eight_seconds(frames):
    """끝 프레임을 실으면 본문의 길이는 씬 길이와 무관하게 8초다."""
    body = build_body(request_for(frames, duration=4.566))
    assert "lastFrame" in body["instances"][0]
    assert body["parameters"]["durationSeconds"] == INTERPOLATION_DURATION


def test_without_last_frame_scene_length_still_decides(frames):
    """보간이 아니면 규칙은 그대로다 — 8초 고정은 `lastFrame`에만 붙는다."""
    body = build_body(request_for(frames, last_frame=None, duration=3.2))
    assert "lastFrame" not in body["instances"][0]
    assert body["parameters"]["durationSeconds"] == 4


# --- 생성 경로 -----------------------------------------------------------------


def operation(name="models/veo/operations/op1", done=False, **extra):
    doc = {"name": name, "done": done}
    doc.update(extra)
    return doc


def test_generate_polls_until_done_and_downloads_the_uri(frames):
    transport = FakeTransport([
        (200, operation()),
        (200, operation(done=False)),
        (200, operation(done=True, response={
            "generateVideoResponse": {"generatedSamples": [
                {"video": {"uri": "https://files.example/video.mp4"}}
            ]}
        })),
        (200, MP4),
    ])
    clip = client_with(transport).generate(request_for(frames))

    assert clip.data.startswith(MP4[:12])
    assert clip.request_id == "models/veo/operations/op1"
    methods = [(c["method"], c["url"].split("/")[-1]) for c in transport.calls]
    assert methods[0][0] == "POST"
    assert all(m == "GET" for m, _ in methods[1:])
    # 내려받기에도 키가 실린다 — files URI는 키 없이는 403이다
    assert transport.calls[-1]["headers"]["x-goog-api-key"] == "test-key"


def test_generate_accepts_inline_base64_video(frames):
    inline = base64.b64encode(MP4).decode()
    transport = FakeTransport([
        (200, operation(done=True, response={
            "generatedVideos": [{"video": {"bytesBase64Encoded": inline}}]
        })),
    ])
    clip = client_with(transport).generate(request_for(frames))
    assert clip.data == MP4


def test_generate_reports_the_chosen_duration(frames):
    transport = FakeTransport([
        (200, operation(done=True, response={
            "generatedVideos": [{"video": {"bytesBase64Encoded": base64.b64encode(MP4).decode()}}]
        })),
    ])
    # 끝 프레임이 실린 요청이라 8초가 나간다 — 씬의 4.5초가 아니다.
    clip = client_with(transport).generate(request_for(frames, duration=4.5))
    assert clip.duration == float(INTERPOLATION_DURATION)


def test_generate_reports_scene_length_without_last_frame(frames):
    transport = FakeTransport([
        (200, operation(done=True, response={
            "generatedVideos": [{"video": {"bytesBase64Encoded": base64.b64encode(MP4).decode()}}]
        })),
    ])
    clip = client_with(transport).generate(
        request_for(frames, last_frame=None, duration=4.5)
    )
    assert clip.duration == 6.0


def test_missing_video_fails_with_the_response_keys(frames):
    transport = FakeTransport([
        (200, operation(done=True, response={"unexpected": "shape"})),
    ])
    with pytest.raises(VideoGenError, match="unexpected"):
        client_with(transport).generate(request_for(frames))


def test_operation_error_is_surfaced(frames):
    transport = FakeTransport([
        (200, operation(done=True, error={"message": "safety rejection"})),
    ])
    with pytest.raises(VideoGenError, match="safety rejection"):
        client_with(transport).generate(request_for(frames))


def test_timeout_stops_the_poll(frames):
    ticks = iter([0.0, 100.0, 1000.0])
    client = VeoClient(
        api_key="k",
        transport=FakeTransport([(200, operation())] + [(200, operation())] * 5),
        poll_interval=0, sleep=lambda _s: None, clock=lambda: next(ticks),
    )
    with pytest.raises(VideoGenTimeout):
        client.generate(request_for(frames), timeout=500)


# --- 프로바이더 전체의 문제 ------------------------------------------------------


def test_auth_refusal_is_provider_wide(frames):
    transport = FakeTransport([(403, {"error": "denied"})])
    with pytest.raises(VideoProviderNotConfigured):
        client_with(transport).generate(request_for(frames))


def test_unsupported_parameter_is_provider_wide(frames):
    """파라미터 거부는 씬 수만큼 반복될 실패다 — 남은 씬이 시도하지 않게 승격한다."""
    transport = FakeTransport([(400, b'{"error": "lastFrame is not supported"}')])
    with pytest.raises(VideoProviderNotConfigured):
        client_with(transport).generate(request_for(frames))


def test_rate_limit_is_distinct(frames):
    transport = FakeTransport([(429, {"error": "quota"})])
    with pytest.raises(VideoGenRateLimited):
        client_with(transport).generate(request_for(frames))


def test_missing_key_is_provider_not_configured(frames, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    client = VeoClient(transport=FakeTransport([]))
    with pytest.raises(VideoProviderNotConfigured):
        client.generate(request_for(frames))


def test_model_env_override(monkeypatch):
    monkeypatch.setenv("VEO_MODEL", "veo-3.1-lite-generate-preview")
    assert VeoClient(api_key="k").model_id == "veo-3.1-lite-generate-preview"
