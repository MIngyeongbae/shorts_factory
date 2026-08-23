"""Omni Flash 어댑터 계약 (ADR-0056 결정 2, `runs/20260822-omni-api-probe/`).

- 본문: `model`·`input`·`response_format{type: video, aspect_ratio: 9:16, delivery: uri, duration: "{N}s"}`
- 응답: `steps[*].content[*].uri` → 같은 키로 GET → mp4 (실호출 픽스처) / 인라인 `data`
- 429 → `VideoGenRateLimited`, 401·403·파라미터 거부 → `VideoProviderNotConfigured`
- 모르는 모양은 최상위 키를 담아 요란하게 실패한다
"""

import base64
import json

import pytest
from conftest import load_fixture

from shorts_factory.schemas import vocab
from shorts_factory.videogen.base import (
    VideoGenError,
    VideoGenRateLimited,
    VideoGenTimeout,
    VideoProviderNotConfigured,
    VideoRequest,
)
from shorts_factory.videogen.omni import (
    DEFAULT_CONCURRENCY,
    DEFAULT_MODEL,
    OmniClient,
    build_body,
    duration_value,
    find_videos,
)

MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 16
PROBE = load_fixture("omni_interaction.json")


def request_for(**overrides) -> VideoRequest:
    defaults = dict(
        scene_id=3,
        prompt="FORMAT: An 6-second vertical 9:16 shot, style.\nSUBJECT: a bolt.",
        negative_prompt="logos, watermark",
        seconds=6,
    )
    defaults.update(overrides)
    return VideoRequest(**defaults)


class FakeTransport:
    """(method, url) → 준비된 (status, body). 호출을 순서대로 기록한다."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({
            "method": method, "url": url, "headers": headers,
            "body": json.loads(body) if body else None, "timeout": timeout,
        })
        status, payload = self.responses.pop(0)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return status, raw


def client_with(transport, **kwargs) -> OmniClient:
    return OmniClient(
        api_key="test-key", transport=transport, poll_interval=0,
        sleep=lambda _s: None, **kwargs,
    )


# --- 본문 계약 -----------------------------------------------------------------


def test_body_matches_the_documented_interactions_shape():
    body = build_body(request_for(), model=DEFAULT_MODEL)

    assert body["model"] == "gemini-omni-flash-preview"
    assert body["input"].startswith("FORMAT: An 6-second")
    assert body["response_format"]["type"] == "video"
    assert body["response_format"]["aspect_ratio"] == "9:16"
    assert body["response_format"]["delivery"] == "uri"
    assert set(body) == {"model", "input", "response_format"}


def test_aspect_ratio_comes_from_the_vocab_not_the_code():
    body = build_body(request_for(), model=DEFAULT_MODEL)
    assert body["response_format"]["aspect_ratio"] == vocab.style("aspect_ratio")


def test_duration_is_sent_as_a_protobuf_duration_string():
    """프로브 — `duration: 3`은 Invalid input, `"3s"`는 본문 검증 통과. 효과는 미실측(n=0)."""
    body = build_body(request_for(seconds=6), model=DEFAULT_MODEL)
    assert body["response_format"]["duration"] == "6s"
    assert duration_value(10) == "10s"


def test_duration_field_can_be_switched_off():
    """필드가 런타임에 거절되면 프롬프트의 'An N-second'만으로 간다 (프로브 n=1 — 3.008초)."""
    body = build_body(request_for(), model=DEFAULT_MODEL, send_duration=False)
    assert "duration" not in body["response_format"]


def test_body_matches_the_recorded_probe_request():
    """실호출 요청과 같은 키·값이다 (길이 필드만 이번에 더해졌다)."""
    recorded = PROBE["request"]
    body = build_body(
        VideoRequest(scene_id=1, prompt=recorded["input"], seconds=3), model=recorded["model"],
        send_duration=False,
    )
    assert body == recorded


@pytest.mark.parametrize("seconds", [2, 11])
def test_out_of_range_seconds_is_refused_before_the_call(seconds):
    with pytest.raises(VideoGenError, match="범위"):
        build_body(request_for(seconds=seconds), model=DEFAULT_MODEL)


def test_empty_prompt_is_refused():
    with pytest.raises(VideoGenError, match="프롬프트"):
        build_body(request_for(prompt="  "), model=DEFAULT_MODEL)


# --- 응답 파싱 -----------------------------------------------------------------


def test_find_videos_reads_steps_content_uri_from_the_probe():
    videos = find_videos(PROBE["response"]["steps"])
    assert len(videos) == 1
    assert videos[0]["uri"].endswith(":download?alt=media")
    assert videos[0]["mime_type"] == "video/mp4"


def test_find_videos_ignores_non_video_content():
    payload = {"steps": [{"type": "model_output", "content": [
        {"type": "text", "text": "hello"},
        {"type": "image", "mime_type": "image/png", "data": "aaaa"},
        {"type": "video", "mime_type": "video/mp4", "data": "bbbb"},
    ]}]}
    assert [v["data"] for v in find_videos(payload)] == ["bbbb"]


def test_generate_downloads_the_uri_with_the_same_key():
    """실호출 픽스처의 모양 그대로 — 동기 응답, uri GET이 곧 mp4다 (폴링 없음)."""
    transport = FakeTransport([(200, PROBE["response"]), (200, MP4)])
    clip = client_with(transport).generate(request_for())

    assert clip.data == MP4
    assert clip.request_id == PROBE["response"]["id"]
    assert clip.model_id == "gemini-omni-flash-preview"
    assert clip.duration == 6.0
    assert clip.raw["total_output_tokens"] == PROBE["response"]["usage"]["total_output_tokens"]
    post, get = transport.calls
    assert post["method"] == "POST" and post["url"].endswith("/v1beta/interactions")
    assert post["headers"]["x-goog-api-key"] == "test-key"
    assert get["method"] == "GET" and get["url"] == PROBE["response"]["steps"][1]["content"][0]["uri"]
    assert get["headers"] == {"x-goog-api-key": "test-key"}


def test_generate_accepts_inline_base64_data():
    payload = {"id": "i1", "status": "completed", "steps": [{"type": "model_output", "content": [
        {"type": "video", "mime_type": "video/mp4", "data": base64.b64encode(MP4).decode()},
    ]}]}
    transport = FakeTransport([(200, payload)])
    clip = client_with(transport).generate(request_for())
    assert clip.data == MP4
    assert len(transport.calls) == 1


def test_file_resource_is_polled_until_active():
    """uri가 파일 리소스(JSON)면 ACTIVE까지 기다린 뒤 내려받는다."""
    resource_uri = "https://generativelanguage.googleapis.com/v1beta/files/abc"
    payload = {"id": "i1", "status": "completed", "steps": [{"type": "model_output", "content": [
        {"type": "video", "mime_type": "video/mp4", "uri": resource_uri},
    ]}]}
    transport = FakeTransport([
        (200, payload),
        (200, {"name": "files/abc", "state": "PROCESSING"}),
        (200, {"name": "files/abc", "state": "ACTIVE", "downloadUri": resource_uri + ":download?alt=media"}),
        (200, MP4),
    ])
    clip = client_with(transport).generate(request_for())
    assert clip.data == MP4
    assert transport.calls[-1]["url"].endswith(":download?alt=media")


def test_incomplete_interaction_is_polled_by_id():
    pending = {"id": "i9", "status": "in_progress", "steps": []}
    done = {"id": "i9", "status": "completed", "steps": [{"type": "model_output", "content": [
        {"type": "video", "mime_type": "video/mp4", "data": base64.b64encode(MP4).decode()},
    ]}]}
    transport = FakeTransport([(200, pending), (200, done)])
    clip = client_with(transport).generate(request_for())
    assert clip.data == MP4
    assert transport.calls[1]["url"].endswith("/interactions/i9")


def test_unknown_shape_fails_with_the_top_level_keys():
    transport = FakeTransport([(200, {"id": "i1", "status": "completed", "unexpected": {"x": 1}})])
    with pytest.raises(VideoGenError, match="unexpected"):
        client_with(transport).generate(request_for())


def test_failed_interaction_surfaces_the_message():
    transport = FakeTransport([(200, {"id": "i1", "status": "failed", "error": {"message": "safety"}})])
    with pytest.raises(VideoGenError, match="safety"):
        client_with(transport).generate(request_for())


def test_non_mp4_download_is_refused():
    transport = FakeTransport([(200, PROBE["response"]), (200, b"<html>oops</html>")])
    with pytest.raises(VideoGenError):
        client_with(transport).generate(request_for())


def test_timeout_stops_the_poll():
    ticks = iter([0.0, 100.0, 1000.0, 2000.0])
    pending = {"id": "i9", "status": "in_progress", "steps": []}
    client = OmniClient(
        api_key="k", transport=FakeTransport([(200, pending)] * 5),
        poll_interval=0, sleep=lambda _s: None, clock=lambda: next(ticks),
    )
    with pytest.raises(VideoGenTimeout):
        client.generate(request_for(), timeout=500)


# --- 프로바이더 전체의 문제 ------------------------------------------------------


def test_rate_limit_is_distinct():
    transport = FakeTransport([(429, {"error": {"message": "quota"}})])
    with pytest.raises(VideoGenRateLimited):
        client_with(transport).generate(request_for())


@pytest.mark.parametrize("status", [401, 403])
def test_auth_refusal_is_provider_wide(status):
    transport = FakeTransport([(status, {"error": {"message": "denied"}})])
    with pytest.raises(VideoProviderNotConfigured):
        client_with(transport).generate(request_for())


@pytest.mark.parametrize("message", [
    "Unknown parameter 'duration_seconds' at 'response_format'.",
    "Invalid input at 'response_format'.",
])
def test_parameter_rejection_is_provider_wide(message):
    """프로브에서 본 두 400 — 같은 본문으로 씬 수만큼 실패할 문제라 승격한다."""
    transport = FakeTransport([(400, {"error": {"message": message, "code": "invalid_request"}})])
    with pytest.raises(VideoProviderNotConfigured, match="파라미터 거부"):
        client_with(transport).generate(request_for())


def test_unknown_model_is_provider_wide():
    transport = FakeTransport([(404, {"error": {"message": "Model 'x' not found.", "code": "not_found"}})])
    with pytest.raises(VideoProviderNotConfigured, match="OMNI_MODEL"):
        client_with(transport).generate(request_for())


def test_missing_key_is_provider_not_configured(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(VideoProviderNotConfigured):
        OmniClient(transport=FakeTransport([])).generate(request_for())


def test_model_env_override(monkeypatch):
    monkeypatch.setenv("OMNI_MODEL", "gemini-omni-flash-002")
    assert OmniClient(api_key="k").model_id == "gemini-omni-flash-002"


def test_concurrency_is_the_adapters_to_say():
    assert OmniClient(api_key="k").concurrency() == DEFAULT_CONCURRENCY
    assert OmniClient(api_key="k", concurrency=2).concurrency() == 2
