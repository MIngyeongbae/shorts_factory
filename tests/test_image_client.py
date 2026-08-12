"""이미지 생성 어댑터 (`imagegen/`) — 인터페이스·페이크·미결정 프로바이더.

확인 대상:
- ADR-0005 — 모든 호출에 스타일 앵커를 붙인다. 과금 어댑터는 앵커 없이 돌지 않는다
- ADR-0020 — 요청에 담기는 것은 `prompt`·`negative_prompt`·`style`뿐이다.
  `framing_reuse_of`는 캐시 키가 아니고, 시간 정보는 애초에 없다
- ADR-0001·0002 — 한국어 `subject`가 번역 없이 그대로 가고, 화면에 글자로 그리지 말라는
  지시가 함께 간다
- ADR-0021 — 실물 어댑터의 요청 본문·응답 파싱·오류 매핑. **실제 호출은 하지 않는다.**
  HTTP 경계(`transport`)를 페이크로 갈아끼워 전 경로를 검증한다
"""

import base64
import json
import struct
import zlib

import pytest

from shorts_factory.imagegen.base import (
    GeneratedImage,
    ImageGenError,
    ImageGenRateLimited,
    ImageRequest,
    ProviderNotConfigured,
    discover_style_anchors,
)
from shorts_factory.imagegen.fake import (
    FakeImageClient,
    fake_image,
    solid_png,
)
from shorts_factory.imagegen.nano_banana import NanoBananaClient

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

STYLE = {
    "base_style": "photorealistic 3D diorama / aerial-render look, natural light",
    "composition": "vertical 9:16; subject placed in the centre to upper third",
    "aspect_ratio": "9:16",
    "resolution": "2K",
    "style_anchors": "assets/style_anchors",
    "global_overlays": [],
}

SCENE = {
    "scene_id": 6,
    "beat": "context_number",
    "subject_scale": "wide",
    "camera": "slow_zoom_in",
    "motion": "kenburns",
    "framing": "aerial_diorama",
    "framing_source": "beat_rule",
    "prompt": (
        "Subject (Korean description — depict it; do not write these words in the "
        "image): 협곡을 가득 메운 거대한 콘크리트 댐 덩어리\n"
        "Shot: bird's-eye diorama view of the scene"
    ),
    "negative_prompt": (
        "Do not include: burned-in subtitles or caption bars; sparkle particle "
        "overlay; large red number text."
    ),
    "overlays": [{"type": "big_red_text", "layer": "B", "value": "325만"}],
}


def png_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """PNG를 청크로 뜯는다. CRC가 어긋나면 그 자리에서 실패한다."""
    assert data.startswith(PNG_MAGIC)
    chunks = []
    pos = len(PNG_MAGIC)
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        assert crc == zlib.crc32(kind + payload) & 0xFFFFFFFF, f"{kind} CRC 불일치"
        chunks.append((kind, payload))
        pos += 12 + length
    return chunks


# --- 페이크가 만드는 것이 진짜 PNG인가 ---------------------------------------


def test_fake_png_is_structurally_valid():
    """[7. motion]이 FFmpeg에 먹일 파일이다. 헤더만 흉내 낸 바이트면 거기서 터진다."""
    data = solid_png(9, 16, (10, 20, 30))
    kinds = [kind for kind, _ in png_chunks(data)]
    assert kinds == [b"IHDR", b"IDAT", b"IEND"]

    header = dict(png_chunks(data))[b"IHDR"]
    width, height, depth, color_type = struct.unpack(">IIBB", header[:10])
    assert (width, height, depth, color_type) == (9, 16, 8, 2)

    raw = zlib.decompress(dict(png_chunks(data))[b"IDAT"])
    assert raw == (b"\x00" + bytes((10, 20, 30)) * 9) * 16


def test_fake_png_decodes_with_a_real_decoder():
    """선택적 검증 — Pillow가 있으면 실제 디코딩까지 확인한다 (의존성은 아니다)."""
    Image = pytest.importorskip("PIL.Image")
    import io

    with Image.open(io.BytesIO(solid_png(9, 16, (1, 2, 3)))) as img:
        assert img.size == (9, 16)
        assert img.convert("RGB").getpixel((4, 8)) == (1, 2, 3)


def test_same_request_gives_the_same_bytes_and_a_different_one_does_not():
    """폴백이 인접 씬 이미지를 복사했는지를 바이트 비교로 판별할 수 있어야 한다."""
    first = fake_image(ImageRequest.from_prompt_scene(SCENE, STYLE)).data
    same = fake_image(ImageRequest.from_prompt_scene(SCENE, STYLE)).data
    other = fake_image(
        ImageRequest.from_prompt_scene({**SCENE, "prompt": "다른 피사체"}, STYLE)
    ).data
    assert first == same
    assert first != other


# --- 결과 계약 ---------------------------------------------------------------


def test_bytes_that_contradict_the_declared_mime_are_rejected():
    """어댑터가 스스로 밝힌 포맷과 다른 바이트를 주면 [7]이 FFmpeg에서 터진다."""
    with pytest.raises(ImageGenError, match="시그니처"):
        GeneratedImage(data=PNG_MAGIC + b"...", mime_type="image/jpeg")


def test_unstorable_format_is_rejected():
    with pytest.raises(ImageGenError, match="저장할 수 없는"):
        GeneratedImage(data=b"GIF89a", mime_type="image/gif")


def test_suffix_follows_the_mime_type():
    """실물은 JPEG, 페이크는 PNG를 낸다 (ADR-0021). 파일명이 그것을 따라간다."""
    assert GeneratedImage(data=solid_png(2, 2, (0, 0, 0))).suffix == ".png"
    jpeg = GeneratedImage(data=b"\xff\xd8\xff\xe0JFIF", mime_type="image/jpeg")
    assert jpeg.suffix == ".jpg"


def test_empty_response_is_rejected():
    with pytest.raises(ImageGenError, match="비어"):
        GeneratedImage(data=b"")


def test_meta_leaves_the_image_bytes_behind():
    image = fake_image(ImageRequest.from_prompt_scene(SCENE, STYLE))
    assert image.meta["bytes"] == len(image.data)
    assert not any(isinstance(v, bytes) for v in image.meta.values())


# --- 요청에 담기는 것 (ADR-0020) ---------------------------------------------


def test_request_carries_the_korean_subject_untranslated():
    request = ImageRequest.from_prompt_scene(SCENE, STYLE)
    assert "협곡을 가득 메운 거대한 콘크리트 댐 덩어리" in request.prompt
    assert "do not write these words in the image" in request.prompt


def test_request_excludes_layer_b_overlays_and_scene_direction():
    """오버레이는 [8], 카메라·모션·비트는 [7]·[9]가 scenes.timed.json에서 읽는다."""
    request = ImageRequest.from_prompt_scene(SCENE, STYLE)
    payload = f"{request.prompt}\n{request.negative_prompt}"

    assert "325만" not in payload  # 대형 숫자는 레이어 B (ADR-0002)
    assert not hasattr(request, "overlays")
    assert not hasattr(request, "camera")
    assert not hasattr(request, "motion")
    assert not hasattr(request, "beat")


def test_request_has_no_time_information():
    """prompts.json에 시간이 없다 (ADR-0020). 있어야 한다고 느끼면 설계 오해다."""
    fields = set(ImageRequest.__dataclass_fields__)
    assert not fields & {"start", "end", "est_start", "est_end", "duration"}


def test_framing_reuse_is_not_a_cache_key():
    """구도만 같고 subject는 다르다. 지문이 같아지면 다른 피사체가 같은 그림이 된다."""
    reuser = {
        **SCENE,
        "scene_id": 25,
        "framing_reuse_of": 6,
        "prompt": SCENE["prompt"].replace("협곡을 가득 메운", "지금도 서 있는"),
    }
    assert (
        ImageRequest.from_prompt_scene(SCENE, STYLE).digest
        != ImageRequest.from_prompt_scene(reuser, STYLE).digest
    )


def test_digest_follows_the_style_block_and_the_anchors(tmp_path):
    """앵커가 생기면 룩이 바뀐다 — 이미 만든 이미지를 이어받으면 안 된다 (ADR-0005)."""
    base = ImageRequest.from_prompt_scene(SCENE, STYLE)
    with_anchor = ImageRequest.from_prompt_scene(
        SCENE, STYLE, style_anchors=(tmp_path / "anchor-01.png",)
    )
    lower_res = ImageRequest.from_prompt_scene(SCENE, {**STYLE, "resolution": "1K"})

    assert base.digest != with_anchor.digest
    assert base.digest != lower_res.digest


# --- 스타일 앵커 탐색 (ADR-0005) ---------------------------------------------


def test_anchor_discovery_is_sorted_and_filtered(tmp_path):
    for name in ("b.png", "a.jpg", "README.md", "c.txt", "d.webp"):
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "sub").mkdir()

    assert [p.name for p in discover_style_anchors(tmp_path)] == [
        "a.jpg", "b.png", "d.webp",
    ]


def test_missing_anchor_dir_is_empty_not_an_error(tmp_path):
    """디렉터리가 없어도 예외가 아니라 빈 튜플이다. 판단은 단계가 한다."""
    assert discover_style_anchors(tmp_path / "없음") == ()


# --- 페이크의 실패 주입 -------------------------------------------------------


def test_fake_fails_the_requested_number_of_times():
    client = FakeImageClient(fail_scenes={6: 1})
    request = ImageRequest.from_prompt_scene(SCENE, STYLE)

    with pytest.raises(ImageGenError):
        client.generate(request)
    assert client.generate(request).data.startswith(PNG_MAGIC)
    assert client.attempts_for(6) == 2


def test_fake_records_what_was_sent(tmp_path):
    anchor = tmp_path / "anchor-01.png"
    client = FakeImageClient()
    client.generate(
        ImageRequest.from_prompt_scene(SCENE, STYLE, style_anchors=(anchor,)),
        timeout=180,
    )
    call = client.calls[0]
    assert call["scene_id"] == 6
    assert call["anchors"] == ("anchor-01.png",)
    assert call["timeout"] == 180


def test_fake_does_not_require_style_anchors():
    assert FakeImageClient().requires_style_anchors is False


def test_paid_provider_demands_style_anchors():
    assert NanoBananaClient().requires_style_anchors is True


# --- 실물 어댑터 (ADR-0021). 실제 호출 0회 -------------------------------------


def ok_response(data: bytes = b"", **extra) -> tuple[int, bytes]:
    """이미지 한 장을 담은 200 응답."""
    payload = {
        "id": "int_abc",
        "model": "gemini-3.1-flash-image",
        "output_image": {
            "mime_type": "image/png",
            "data": base64.b64encode(data or solid_png(8, 8, (1, 2, 3))).decode("ascii"),
        },
    }
    payload.update(extra)
    return 200, json.dumps(payload).encode("utf-8")


class RecordingTransport:
    """호출을 받아 적고 정해진 응답을 돌려준다. 네트워크를 쓰지 않는다."""

    def __init__(self, *responses: tuple[int, bytes]) -> None:
        self.responses = list(responses) or [ok_response()]
        self.calls: list[dict] = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "body": json.loads(body),
                "timeout": timeout,
            }
        )
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


def client(*responses, **kwargs) -> NanoBananaClient:
    kwargs.setdefault("api_key", "test-key")
    kwargs.setdefault("transport", RecordingTransport(*responses))
    return NanoBananaClient(**kwargs)


def test_missing_key_refuses_before_touching_the_network():
    """키가 없으면 호출 자체가 없다. transport를 부르면 안 된다."""
    transport = RecordingTransport()
    with pytest.raises(ProviderNotConfigured, match="GEMINI_API_KEY"):
        NanoBananaClient(transport=transport).generate(
            ImageRequest.from_prompt_scene(SCENE, STYLE)
        )
    assert transport.calls == []


def test_key_is_read_at_call_time_not_construction():
    """--help가 어댑터를 만드는 것만으로 실패하면 안 된다."""
    NanoBananaClient()  # 예외 없음


def test_request_carries_the_contract_values_verbatim():
    """`9:16`·`2K`가 API 허용값과 문자열까지 같다 — 변환 테이블이 없다 (ADR-0021)."""
    c = client()
    c.generate(ImageRequest.from_prompt_scene(SCENE, STYLE))

    call = c.transport.calls[0]
    assert call["url"].endswith("/v1beta/interactions")
    assert call["headers"]["x-goog-api-key"] == "test-key"
    assert call["body"]["model"] == "gemini-3.1-flash-image"
    assert call["body"]["response_format"] == {
        "type": "image",
        "mime_type": "image/jpeg",
        "aspect_ratio": "9:16",
        "image_size": "2K",
    }


def test_korean_subject_goes_out_untranslated():
    """ADR-0001·0014 — `subject`는 한국어 그대로 프롬프트에 들어간다."""
    c = client()
    c.generate(ImageRequest.from_prompt_scene(SCENE, STYLE))

    text = c.transport.calls[0]["body"]["input"][0]["text"]
    assert SCENE["prompt"] in text


def test_negative_prompt_is_appended_to_the_text():
    c = client()
    c.generate(ImageRequest.from_prompt_scene(SCENE, STYLE))

    text = c.transport.calls[0]["body"]["input"][0]["text"]
    assert SCENE["negative_prompt"] in text


def test_empty_negative_prompt_adds_no_stray_instruction():
    """빈 'Avoid:' 줄이 남으면 모델이 그것도 지시로 읽는다."""
    c = client()
    c.generate(
        ImageRequest.from_prompt_scene({**SCENE, "negative_prompt": "  "}, STYLE)
    )

    assert "Avoid:" not in c.transport.calls[0]["body"]["input"][0]["text"]


def test_style_anchors_ride_along_as_base64(tmp_path):
    anchors = []
    for name in ("a.png", "b.png"):
        p = tmp_path / name
        p.write_bytes(solid_png(4, 4, (4, 5, 6)))
        anchors.append(p)

    c = client()
    c.generate(
        ImageRequest.from_prompt_scene(SCENE, STYLE, style_anchors=tuple(anchors))
    )

    parts = c.transport.calls[0]["body"]["input"]
    images = [p for p in parts if p["type"] == "image"]
    assert len(images) == 2
    assert all(p["mime_type"] == "image/png" for p in images)
    assert base64.b64decode(images[0]["data"]) == solid_png(4, 4, (4, 5, 6))


def test_more_than_three_anchors_is_refused(tmp_path):
    """상한이 3장이다 (ADR-0021). 앞 3장만 쓰면 파일 이름 순서가 룩을 정한다."""
    anchors = []
    for name in ("a.png", "b.png", "c.png", "d.png"):
        p = tmp_path / name
        p.write_bytes(solid_png(4, 4, (4, 5, 6)))
        anchors.append(p)

    transport = RecordingTransport()
    with pytest.raises(ProviderNotConfigured, match="3장"):
        NanoBananaClient(api_key="k", transport=transport).generate(
            ImageRequest.from_prompt_scene(SCENE, STYLE, style_anchors=tuple(anchors))
        )
    assert transport.calls == [], "상한 위반은 호출 전에 걸러진다"


def test_image_is_read_from_the_steps_array_too():
    """편의 필드가 없고 단계 배열만 오는 응답도 읽는다."""
    payload = {
        "steps": [
            {"content": [{"type": "text", "text": "..."}]},
            {
                "content": [
                    {
                        "type": "image",
                        "mime_type": "image/png",
                        "data": base64.b64encode(solid_png(6, 6, (7, 8, 9))).decode("ascii"),
                    }
                ]
            },
        ]
    }
    c = client((200, json.dumps(payload).encode("utf-8")))
    result = c.generate(ImageRequest.from_prompt_scene(SCENE, STYLE))

    assert result.data == solid_png(6, 6, (7, 8, 9))


def test_response_without_an_image_names_the_shape_it_got():
    """형태가 바뀌면 추측이 아니라 한 줄 오류로 드러나야 한다 (ADR-0021 되돌릴 조건)."""
    body = json.dumps({"steps": [], "finish_reason": "SAFETY"}).encode("utf-8")
    with pytest.raises(ImageGenError, match="finish_reason"):
        client((200, body)).generate(ImageRequest.from_prompt_scene(SCENE, STYLE))


def test_mismatched_signature_is_refused():
    """200이어도 본문이 이미지가 아니면 거기서 멈춘다."""
    payload = {
        "output_image": {
            "mime_type": "image/jpeg",
            "data": base64.b64encode(b"not-an-image").decode("ascii"),
        }
    }
    with pytest.raises(ImageGenError, match="시그니처"):
        client((200, json.dumps(payload).encode("utf-8"))).generate(
            ImageRequest.from_prompt_scene(SCENE, STYLE)
        )


def test_rate_limit_maps_to_its_own_error():
    with pytest.raises(ImageGenRateLimited):
        client((429, b'{"error":"quota"}')).generate(
            ImageRequest.from_prompt_scene(SCENE, STYLE)
        )


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_stops_the_provider_and_mentions_the_paid_tier(status):
    """유료 티어가 아니면 첫 호출이 막힌다 — 키 검증만으로는 알 수 없다 (ADR-0021)."""
    with pytest.raises(ProviderNotConfigured, match="유료"):
        client((status, b'{"error":"denied"}')).generate(
            ImageRequest.from_prompt_scene(SCENE, STYLE)
        )


def test_other_http_errors_are_retryable_scene_failures():
    """500은 씬 하나의 실패다. `[6]`이 재시도하고 폴백한다."""
    with pytest.raises(ImageGenError) as exc:
        client((500, b"boom")).generate(ImageRequest.from_prompt_scene(SCENE, STYLE))
    assert not isinstance(exc.value, ProviderNotConfigured)


def test_two_hundred_that_is_not_json_fails_clearly():
    with pytest.raises(ImageGenError, match="JSON"):
        client((200, b"<html>502</html>")).generate(
            ImageRequest.from_prompt_scene(SCENE, STYLE)
        )
