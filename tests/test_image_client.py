"""이미지 생성 어댑터 (`imagegen/`) — **휴면** 패키지의 인터페이스·페이크 계약 (ADR-0056).

어느 단계도 부르지 않지만 어댑터가 썩지 않게 계약을 지킨다 (ADR-0056 결정 1 —
정지 이미지 쇼츠가 결정되면 되살린다). NB2 어댑터 테스트는 어댑터와 함께 지웠다.

확인 대상:
- ADR-0020 — 요청에 담기는 것은 `prompt`·`negative_prompt`·`style`뿐이다.
  `framing_reuse_of`는 캐시 키가 아니고, 시간 정보는 애초에 없다
- ADR-0001 — 한국어 `subject`가 번역 없이 그대로 간다
"""

import struct
import zlib

import pytest

from shorts_factory.imagegen.base import (
    GeneratedImage,
    ImageGenError,
    ImageRequest,
    discover_style_anchors,
)
from shorts_factory.imagegen.fake import (
    FakeImageClient,
    fake_image,
    solid_png,
)

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
    "beat": "context",
    "subject_scale": "wide",
    "camera": "slow_zoom_in",
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
    """FFmpeg에 먹일 수 있는 파일이어야 한다. 헤더만 흉내 낸 바이트면 거기서 터진다."""
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


def test_request_excludes_layer_b_overlays_and_scene_direction():
    """카메라·모션·비트는 [7]·[9]가 실측+계약 병합에서 읽는다 — 이미지 요청에는 프롬프트뿐이다."""
    request = ImageRequest.from_prompt_scene(SCENE, STYLE)
    payload = f"{request.prompt}\n{request.negative_prompt}"

    assert "325만" not in payload  # 대형 숫자는 레이어 B (ADR-0002)
    assert not hasattr(request, "overlays")
    assert not hasattr(request, "camera")
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
