"""로컬 ComfyUI + H3 어댑터 계약 (ADR-0059 결정 3, `runs/20260823-comfy-*` 프로브의 호출 규약).

- 템플릿(`config/comfy/h3-t2v.api.json`)의 노드를 `class_type`으로 찾아 다섯 자리만 꽂는다
- `POST /prompt` → `GET /history/{id}` 폴링 → `GET /view` → mp4
- 연결 불가·`node_errors`는 프로바이더 전체 문제(`VideoProviderNotConfigured`), 잡 실패는 씬 실패
- 종횡비는 어휘에서, 메가픽셀은 환경변수 → 템플릿 순
"""

from __future__ import annotations

import json

import pytest

from shorts_factory.schemas import vocab
from shorts_factory.transport import TransportError, TransportTimeout
from shorts_factory.videogen.base import (
    VideoClient,
    VideoGenError,
    VideoGenTimeout,
    VideoProviderNotConfigured,
    VideoRequest,
)
from shorts_factory.videogen.comfy_h3 import (
    MEGAPIXELS_ENV,
    NODE_PROMPT,
    NODE_RESOLUTION,
    NODE_SAVE,
    NODE_SECONDS,
    NODE_SEED,
    TEMPLATE_PATH,
    URL_ENV,
    ComfyH3Client,
    build_workflow,
    find_node,
    find_output_video,
    load_template,
)

MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 16
PROMPT_ID = "dfaf8a80-e299-42b0-b5ab-c74f0659e198"


def request_for(**overrides) -> VideoRequest:
    defaults = dict(
        scene_id=4,
        prompt="A vertical 9:16 shot, 5 seconds long, semi-stylized 3D.\nSUBJECT: a coin.",
        negative_prompt="",
        seconds=5,
    )
    defaults.update(overrides)
    return VideoRequest(**defaults)


class FakeTransport:
    """(method, url) → 준비된 (status, body). 호출을 순서대로 기록한다. 예외도 낼 수 있다."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({
            "method": method, "url": url, "headers": headers,
            "body": json.loads(body) if body else None, "timeout": timeout,
        })
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, payload = item
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return status, raw


def history_done(filename="video/shorts-factory-s4_00001_.mp4"):
    return {PROMPT_ID: {
        "status": {"status_str": "success", "completed": True, "messages": []},
        "outputs": {"92": {"images": [{"filename": filename, "subfolder": "video", "type": "output"}]}},
    }}


def client_with(transport, **kwargs) -> ComfyH3Client:
    kwargs.setdefault("base_url", "http://comfy.test:8188")
    kwargs.setdefault("seed_fn", lambda: 4242)
    return ComfyH3Client(
        transport=transport, poll_interval=0, sleep=lambda _s: None, **kwargs,
    )


# --- 템플릿 ---------------------------------------------------------------------


def test_repository_template_has_exactly_the_nodes_the_adapter_fills():
    workflow = load_template(TEMPLATE_PATH)
    for class_type in (NODE_PROMPT, NODE_RESOLUTION, NODE_SEED, NODE_SECONDS, NODE_SAVE):
        assert find_node(workflow, class_type)


def test_template_aspect_ratio_agrees_with_the_vocab():
    workflow = load_template(TEMPLATE_PATH)
    ratio = workflow[find_node(workflow, NODE_RESOLUTION)]["inputs"]["aspect_ratio"]
    assert ratio.startswith(vocab.style("aspect_ratio"))


def test_template_prompt_is_a_placeholder_not_a_leftover_probe_prompt():
    workflow = load_template(TEMPLATE_PATH)
    assert workflow[find_node(workflow, NODE_PROMPT)]["inputs"]["prompt"] == "{prompt}"


def test_build_workflow_fills_the_five_slots_and_nothing_else():
    template = load_template(TEMPLATE_PATH)
    workflow = build_workflow(
        template, request_for(), seed=7, megapixels=1.0, filename_prefix="video/x-s4",
    )
    assert workflow[find_node(workflow, NODE_PROMPT)]["inputs"]["prompt"].startswith("A vertical 9:16")
    assert workflow[find_node(workflow, NODE_RESOLUTION)]["inputs"]["megapixels"] == 1.0
    assert workflow[find_node(workflow, NODE_SEED)]["inputs"]["noise_seed"] == 7
    assert workflow[find_node(workflow, NODE_SECONDS)]["inputs"]["value"] == 5.0
    assert workflow[find_node(workflow, NODE_SAVE)]["inputs"]["filename_prefix"] == "video/x-s4"
    # 템플릿은 손대지 않는다 — 깊은 복사다
    assert template[find_node(template, NODE_PROMPT)]["inputs"]["prompt"] == "{prompt}"
    # 나머지 노드는 그대로다 (모델·샘플러·스텝)
    untouched = {NODE_PROMPT, NODE_RESOLUTION, NODE_SEED, NODE_SECONDS, NODE_SAVE}
    for node_id, node in template.items():
        if node["class_type"] not in untouched:
            assert workflow[node_id] == node


def test_megapixels_none_keeps_the_template_value():
    template = load_template(TEMPLATE_PATH)
    before = template[find_node(template, NODE_RESOLUTION)]["inputs"]["megapixels"]
    workflow = build_workflow(template, request_for(), seed=1, megapixels=None, filename_prefix="p")
    assert workflow[find_node(workflow, NODE_RESOLUTION)]["inputs"]["megapixels"] == before


@pytest.mark.parametrize("seconds", [3, 11])
def test_out_of_range_seconds_is_refused_before_the_call(seconds):
    with pytest.raises(VideoGenError, match="범위"):
        build_workflow(load_template(TEMPLATE_PATH), request_for(seconds=seconds), seed=1,
                       megapixels=None, filename_prefix="p")


def test_empty_prompt_is_refused():
    with pytest.raises(VideoGenError, match="프롬프트"):
        build_workflow(load_template(TEMPLATE_PATH), request_for(prompt="  "), seed=1,
                       megapixels=None, filename_prefix="p")


def test_template_with_a_foreign_aspect_ratio_is_a_provider_problem():
    template = load_template(TEMPLATE_PATH)
    template[find_node(template, NODE_RESOLUTION)]["inputs"]["aspect_ratio"] = "16:9 (Widescreen)"
    with pytest.raises(VideoProviderNotConfigured, match="종횡비"):
        build_workflow(template, request_for(), seed=1, megapixels=None, filename_prefix="p")


def test_duplicate_or_missing_node_is_a_provider_problem():
    template = load_template(TEMPLATE_PATH)
    del template[find_node(template, NODE_SEED)]
    with pytest.raises(VideoProviderNotConfigured, match=NODE_SEED):
        find_node(template, NODE_SEED)


def test_missing_template_file_is_a_provider_problem(tmp_path):
    with pytest.raises(VideoProviderNotConfigured, match="템플릿"):
        load_template(tmp_path / "nope.json")


# --- 출력 찾기 ------------------------------------------------------------------


@pytest.mark.parametrize("key", ["images", "videos", "gifs"])
def test_output_video_is_found_under_any_list_key(key):
    outputs = {"92": {key: [{"filename": "a.png"}, {"filename": "clip.mp4", "subfolder": "video"}]}}
    assert find_output_video(outputs)["filename"] == "clip.mp4"


def test_no_mp4_output_is_none():
    assert find_output_video({"92": {"images": [{"filename": "frame.png"}]}}) is None


# --- 호출 경로 ------------------------------------------------------------------


def test_submit_poll_download_happy_path():
    transport = FakeTransport([
        (200, {"prompt_id": PROMPT_ID, "number": 3, "node_errors": {}}),
        (200, {}),                       # 아직 큐에 있다
        (200, history_done()),
        (200, MP4),
    ])
    clip = client_with(transport).generate(request_for())

    assert clip.data == MP4
    assert clip.request_id == PROMPT_ID
    assert clip.raw["seed"] == 4242
    assert clip.raw["filename"].endswith(".mp4")

    submit, poll1, poll2, view = transport.calls
    assert submit["method"] == "POST" and submit["url"] == "http://comfy.test:8188/prompt"
    assert submit["body"]["client_id"]
    workflow = submit["body"]["prompt"]
    assert workflow[find_node(workflow, NODE_SEED)]["inputs"]["noise_seed"] == 4242
    assert workflow[find_node(workflow, NODE_SECONDS)]["inputs"]["value"] == 5.0
    assert workflow[find_node(workflow, NODE_SAVE)]["inputs"]["filename_prefix"].endswith("-s4")
    assert poll1["url"] == poll2["url"] == f"http://comfy.test:8188/history/{PROMPT_ID}"
    assert view["url"].startswith("http://comfy.test:8188/view?")
    assert "filename=video%2Fshorts-factory-s4_00001_.mp4" in view["url"]
    assert "subfolder=video" in view["url"] and "type=output" in view["url"]


def test_model_id_is_the_templates_unet_name():
    clip_client = client_with(FakeTransport([]))
    assert clip_client.model_id.endswith(".safetensors")


def test_node_errors_on_submit_stop_the_whole_provider():
    transport = FakeTransport([
        (200, {"prompt_id": PROMPT_ID, "node_errors": {"105:6": {"errors": [{"message": "Value not in list: unet_name"}]}}}),
    ])
    with pytest.raises(VideoProviderNotConfigured, match="node_errors"):
        client_with(transport).generate(request_for())


def test_connection_refused_on_submit_is_a_provider_problem():
    transport = FakeTransport([TransportError("연결 실패: [WinError 10061]")])
    with pytest.raises(VideoProviderNotConfigured, match="ComfyUI에 연결할 수 없다"):
        client_with(transport).generate(request_for())


def test_submit_timeout_is_a_timeout_not_a_provider_problem():
    transport = FakeTransport([TransportTimeout("60초 안에 응답이 오지 않았다")])
    with pytest.raises(VideoGenTimeout):
        client_with(transport).generate(request_for())


def test_failed_job_is_a_scene_failure():
    transport = FakeTransport([
        (200, {"prompt_id": PROMPT_ID, "node_errors": {}}),
        (200, {PROMPT_ID: {"status": {"status_str": "error", "completed": False,
                                      "messages": [["execution_error", {"exception_message": "CUDA out of memory"}]]},
                           "outputs": {}}}),
    ])
    with pytest.raises(VideoGenError, match="CUDA out of memory"):
        client_with(transport).generate(request_for())


def test_finished_job_without_mp4_is_a_scene_failure():
    transport = FakeTransport([
        (200, {"prompt_id": PROMPT_ID, "node_errors": {}}),
        (200, {PROMPT_ID: {"status": {"status_str": "success", "completed": True},
                           "outputs": {"92": {"images": [{"filename": "frame.png"}]}}}}),
    ])
    with pytest.raises(VideoGenError, match="mp4 출력이 없다"):
        client_with(transport).generate(request_for())


def test_transient_poll_error_is_retried_within_budget():
    transport = FakeTransport([
        (200, {"prompt_id": PROMPT_ID, "node_errors": {}}),
        TransportError("connection reset"),
        (200, history_done()),
        (200, MP4),
    ])
    assert client_with(transport).generate(request_for()).data == MP4


def test_budget_exhausted_while_polling_is_a_timeout():
    ticks = iter(range(0, 10_000, 100))
    transport = FakeTransport([
        (200, {"prompt_id": PROMPT_ID, "node_errors": {}}),
    ] + [(200, {})] * 50)
    with pytest.raises(VideoGenTimeout, match="제한 시간"):
        client_with(transport, clock=lambda: next(ticks)).generate(request_for(), timeout=300)


def test_non_mp4_download_is_refused():
    transport = FakeTransport([
        (200, {"prompt_id": PROMPT_ID, "node_errors": {}}),
        (200, history_done()),
        (200, b"<html>not a video</html>"),
    ])
    with pytest.raises(VideoGenError, match="mp4"):
        client_with(transport).generate(request_for())


# --- 설정 -----------------------------------------------------------------------


def test_one_gpu_means_concurrency_one():
    assert client_with(FakeTransport([])).concurrency() == 1


def test_seconds_range_is_exposed_for_the_stage():
    client = client_with(FakeTransport([]))
    assert isinstance(client, VideoClient)
    assert (client.min_seconds, client.max_seconds) == (4, 10)


def test_url_and_megapixels_come_from_env_when_not_given(monkeypatch):
    monkeypatch.setenv(URL_ENV, "http://gpu-box:9999/")
    monkeypatch.setenv(MEGAPIXELS_ENV, "1.0")
    client = ComfyH3Client(transport=FakeTransport([]))
    assert client.base_url == "http://gpu-box:9999"
    assert client.megapixels == 1.0


def test_empty_megapixels_env_falls_back_to_the_template(monkeypatch):
    monkeypatch.delenv(URL_ENV, raising=False)
    monkeypatch.setenv(MEGAPIXELS_ENV, "")
    client = ComfyH3Client(transport=FakeTransport([]))
    assert client.megapixels is None
    assert client.base_url == "http://127.0.0.1:8188"


# --- first-only 어댑터 (ADR-0087 결정 6) ---------------------------------------


class LooseTransport(FakeTransport):
    """멀티파트 업로드 바디가 섞여도 죽지 않는 트랜스포트."""

    def __call__(self, method, url, headers, body, timeout):
        try:
            parsed = json.loads(body) if body else None
        except (ValueError, UnicodeDecodeError):
            parsed = {"_multipart": len(body or b"")}
        self.calls.append({
            "method": method, "url": url, "headers": headers,
            "body": parsed, "timeout": timeout, "raw": body,
        })
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, payload = item
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return status, raw


def first_client(transport, **kwargs):
    from shorts_factory.videogen.comfy_h3 import ComfyH3FirstClient

    kwargs.setdefault("base_url", "http://comfy.test:8188")
    kwargs.setdefault("seed_fn", lambda: 4242)
    return ComfyH3FirstClient(
        transport=transport, poll_interval=0, sleep=lambda _s: None, **kwargs,
    )


def test_the_first_only_template_has_no_last_frame():
    """`h3-f2v`는 first 하나만 받는다 — 그것이 `fl2v`와 갈리는 유일한 지점이다."""
    from shorts_factory.videogen.comfy_h3 import (
        F2V_TEMPLATE_PATH, FL2V_TEMPLATE_PATH, TITLE_FIRST, TITLE_LAST,
        find_node, find_node_by_title, load_template,
    )

    f2v = load_template(F2V_TEMPLATE_PATH)
    fl2v = load_template(FL2V_TEMPLATE_PATH)
    node = f2v[find_node(f2v, "MiniMaxH3ImageToVideo")]["inputs"]
    assert "first_frame" in node and "last_frame" not in node
    assert find_node_by_title(f2v, TITLE_FIRST)
    with pytest.raises(VideoProviderNotConfigured):
        find_node_by_title(f2v, TITLE_LAST)
    # 나머지는 fl2v 그대로다 — LoadImage·ImageScale 한 쌍만 빠졌다.
    assert len(fl2v) - len(f2v) == 2


def test_a_local_frame_path_is_read_from_disk_not_fetched(tmp_path):
    """`local`의 프레임 주소는 로컬 경로다 (ADR-0087 결정 7) — HTTP로 받으러 가지 않는다."""
    frame = tmp_path / "1-clean.png"
    frame.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(16))
    transport = LooseTransport([
        (200, {"name": "sf-s4-clean.png", "subfolder": ""}),   # /upload/image
        (200, {"prompt_id": PROMPT_ID, "node_errors": {}}),
        (200, history_done()),
        (200, MP4),
    ])
    clip = first_client(transport).generate(request_for(first_frame=frame.as_posix()))

    assert clip.data == MP4
    upload, submit, _poll, _view = transport.calls
    assert upload["url"].endswith("/upload/image")
    # 모든 호출이 ComfyUI로만 갔다 — 프레임을 받으러 밖으로 나가지 않았다.
    assert all(call["url"].startswith("http://comfy.test:8188") for call in transport.calls)
    workflow = submit["body"]["prompt"]
    from shorts_factory.videogen.comfy_h3 import TITLE_FIRST, find_node_by_title
    assert workflow[find_node_by_title(workflow, TITLE_FIRST)]["inputs"]["image"] == "sf-s4-clean.png"


def test_a_missing_first_frame_stops_the_scene():
    """first가 없으면 멈춘다 — 조용히 텍스트→영상으로 내려가지 않는다."""
    with pytest.raises(VideoGenError, match="first"):
        first_client(LooseTransport([])).generate(request_for(first_frame=None))


def test_the_first_only_adapter_does_not_require_a_last_frame():
    """`fl2v`는 둘 다 요구한다 — 그 자리를 안 건드리고 새 경로를 단 것이 이 어댑터다."""
    from shorts_factory.videogen.comfy_h3 import ComfyH3FirstLastClient

    with pytest.raises(VideoGenError, match="둘 다"):
        ComfyH3FirstLastClient(
            transport=LooseTransport([]), poll_interval=0, sleep=lambda _s: None,
        ).generate(request_for(first_frame="https://x/1.png", last_frame=None))


def test_the_first_only_adapter_is_wired_and_ready():
    """`reference_frames`를 켠 라인이 가리킬 어댑터가 실제로 있다 (ADR-0034).

    지금 그 스위치를 켠 라인은 없다 — `local`이 2026-09-02에 껐다(H3가 프레임을 이어 그리지
    않는다). **어댑터와 템플릿은 남겨 둔다**: 프레임을 이어 그리는 엔진이 생기면 어휘 한 줄로
    다시 켜는 자리이고, 그때 이 배선이 이미 서 있어야 한다.
    """
    from shorts_factory.videogen.comfy_h3 import ComfyH3FirstClient

    assert ComfyH3FirstClient.name == "comfy-h3-f2v"
    assert ComfyH3FirstClient.accepts_frames is True
    assert F2V_TEMPLATE_PATH_EXISTS(), "h3-f2v 템플릿이 없다"


def F2V_TEMPLATE_PATH_EXISTS() -> bool:
    from shorts_factory.videogen.comfy_h3 import F2V_TEMPLATE_PATH
    return F2V_TEMPLATE_PATH.is_file()
