"""공용 HTTP 경계 `transport.py` (ADR-0056 — 휴면 코드에 살아 있는 코드가 기대지 않는다).

확인 대상:
- 살아 있는 어댑터(`tts/elevenlabs.py`·`videogen/veo.py`)가 `imagegen/`을 import하지 않는다
- 오류 응답은 예외가 아니라 `(status, headers, body)`다 — 서버가 본문에 사유를 적는다
- 타임아웃·연결 실패는 `TransportTimeout`·`TransportError`이고, 어댑터마다 자기 예외로
  바뀐다 — 호출부가 보는 것은 전과 같다
"""

from __future__ import annotations

import ast
import io
import urllib.error
from pathlib import Path

import pytest

import shorts_factory.tts.elevenlabs as elevenlabs
import shorts_factory.videogen.veo as veo
from shorts_factory import transport
from shorts_factory.imagegen import midjourney
from shorts_factory.imagegen.base import ImageGenError, ImageGenTimeout
from shorts_factory.tts.base import TTSError, TTSTimeout
from shorts_factory.videogen.base import VideoGenError, VideoGenTimeout, VideoRequest


class FakeResponse(io.BytesIO):
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _imports_of(module) -> set[str]:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


@pytest.mark.parametrize("module", [elevenlabs, veo])
def test_living_adapters_do_not_import_the_dormant_package(module):
    assert not any("imagegen" in name for name in _imports_of(module)), _imports_of(module)


def test_dormant_adapter_may_lean_on_the_shared_module():
    assert any(name.endswith("transport") for name in _imports_of(midjourney))


# --- urllib_request -----------------------------------------------------------


def test_success_returns_status_headers_and_body(monkeypatch):
    monkeypatch.setattr(
        transport.urllib.request, "urlopen",
        lambda request, timeout: FakeResponse(200, b"ok", {"request-id": "r1"}),
    )
    status, headers, body = transport.urllib_request("GET", "http://x", {}, None, 5)
    assert (status, headers, body) == (200, {"request-id": "r1"}, b"ok")
    assert transport.urllib_transport("GET", "http://x", {}, None, 5) == (200, b"ok")


def test_http_error_is_returned_not_raised(monkeypatch):
    def boom(request, timeout):
        raise urllib.error.HTTPError("http://x", 429, "Too Many", {"retry-after": "3"}, io.BytesIO(b"slow down"))

    monkeypatch.setattr(transport.urllib.request, "urlopen", boom)
    status, headers, body = transport.urllib_request("POST", "http://x", {}, b"{}", 5)
    assert status == 429 and body == b"slow down"
    assert headers.get("retry-after") == "3"


def test_timeout_becomes_transport_timeout(monkeypatch):
    def boom(request, timeout):
        raise urllib.error.URLError(TimeoutError())

    monkeypatch.setattr(transport.urllib.request, "urlopen", boom)
    with pytest.raises(transport.TransportTimeout, match="5초"):
        transport.urllib_request("GET", "http://x", {}, None, 5)


def test_connection_failure_becomes_transport_error(monkeypatch):
    def boom(request, timeout):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(transport.urllib.request, "urlopen", boom)
    with pytest.raises(transport.TransportError, match="연결 실패"):
        transport.urllib_request("GET", "http://x", {}, None, 5)
    assert issubclass(transport.TransportTimeout, transport.TransportError)


# --- 어댑터마다 자기 예외로 바꾼다 --------------------------------------------


def _timeout_then(monkeypatch):
    def boom(request, timeout):
        raise urllib.error.URLError(TimeoutError())

    monkeypatch.setattr(transport.urllib.request, "urlopen", boom)


def _refused(monkeypatch):
    def boom(request, timeout):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(transport.urllib.request, "urlopen", boom)


def test_elevenlabs_keeps_its_post_signature_and_exceptions(monkeypatch):
    monkeypatch.setattr(
        transport.urllib.request, "urlopen",
        lambda request, timeout: FakeResponse(200, b"{}", {"request-id": "r1"}),
    )
    assert elevenlabs.urllib_transport("http://x", {}, b"{}", 5) == (200, {"request-id": "r1"}, b"{}")

    _timeout_then(monkeypatch)
    with pytest.raises(TTSTimeout):
        elevenlabs.urllib_transport("http://x", {}, b"{}", 5)
    _refused(monkeypatch)
    with pytest.raises(TTSError):
        elevenlabs.urllib_transport("http://x", {}, b"{}", 5)


def test_midjourney_keeps_its_exceptions(monkeypatch):
    _timeout_then(monkeypatch)
    with pytest.raises(ImageGenTimeout):
        midjourney.urllib_transport("GET", "http://x", {}, None, 5)
    _refused(monkeypatch)
    with pytest.raises(ImageGenError):
        midjourney.urllib_transport("GET", "http://x", {}, None, 5)


def test_veo_maps_transport_failures_to_its_own_exceptions(tmp_path):
    """전에는 휴면 어댑터의 ImageGenTimeout이 그대로 새어 나왔다 — 이제 Veo의 예외다."""
    frame = tmp_path / "f.png"
    frame.write_bytes(b"\x89PNG")
    request = VideoRequest(scene_id=1, first_frame=frame, motion_prompt="", duration=3.0)

    def timing_out(method, url, headers, body, timeout):
        raise transport.TransportTimeout("5초 안에 응답이 오지 않았다")

    client = veo.VeoClient(api_key="k", transport=timing_out, poll_interval=0, sleep=lambda _s: None)
    with pytest.raises(VideoGenTimeout):
        client.generate(request, timeout=5)

    def refused(method, url, headers, body, timeout):
        raise transport.TransportError("연결 실패: refused")

    client = veo.VeoClient(api_key="k", transport=refused, poll_interval=0, sleep=lambda _s: None)
    with pytest.raises(VideoGenError, match="연결 실패"):
        client.generate(request, timeout=5)
