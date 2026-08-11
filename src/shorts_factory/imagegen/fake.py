"""테스트·개발용 이미지 어댑터. `llm/fake.py`·`tts/fake.py`와 같은 자리다.

`[6. imagegen]`의 판단은 전부 실패 처리 위에서 일어난다(재시도 1회, 인접 씬 폴백,
이어받기). 편당 ~$2.5를 쓰지 않고 단계 전체를 돌릴 수 있어야 하므로 이 모듈이 대역을
선다.

## 페이크가 흉내 내는 것

- **유효한 PNG 바이트.** 하류 `[7. motion]`이 FFmpeg에 먹일 수 있는 형식이어야 하므로
  헤더만 흉내 낸 가짜 바이트가 아니라 실제로 디코딩되는 PNG를 만든다 (표준 라이브러리
  `zlib`만 쓴다 — 의존성 추가 없음)
- **요청마다 다른 그림.** 색을 요청 지문에서 뽑는다. 같은 프롬프트면 같은 바이트,
  다른 프롬프트면 다른 바이트다. 폴백으로 인접 씬 이미지를 복사했는지 여부가
  바이트 비교로 드러난다
- **9:16 비율.** 크기는 작게 잡는다(기본 90×160). 테스트가 편당 2K 이미지 27장을
  들고 다닐 이유가 없다

그림의 내용·미학은 흉내 내지 않는다. 그건 페이크로 검증할 수 있는 것이 아니다.
"""

from __future__ import annotations

import struct
import zlib
from typing import Any, Callable, Mapping

from .base import GeneratedImage, ImageClient, ImageGenError, ImageRequest

#: 페이크 이미지 크기. 9:16이라 비율 검사는 실물과 같은 경로를 탄다.
FAKE_WIDTH = 90
FAKE_HEIGHT = 160


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """단색 PNG 한 장. 표준 라이브러리만 쓴다."""
    if width < 1 or height < 1:
        raise ImageGenError(f"PNG 크기가 잘못됐다: {width}x{height}")
    row = b"\x00" + bytes(rgb) * width  # 필터 타입 0 (None)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8bit truecolor
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _chunk(b"IHDR", header),
            _chunk(b"IDAT", zlib.compress(row * height, 9)),
            _chunk(b"IEND", b""),
        )
    )


def fake_image(
    request: ImageRequest,
    *,
    width: int = FAKE_WIDTH,
    height: int = FAKE_HEIGHT,
    model_id: str = "fake-image-model",
) -> GeneratedImage:
    """요청 지문에서 색을 뽑은 단색 PNG."""
    digest = request.digest
    rgb = (int(digest[0:2], 16), int(digest[2:4], 16), int(digest[4:6], 16))
    return GeneratedImage(
        data=solid_png(width, height, rgb),
        request_id=f"fake-{digest}",
        model_id=model_id,
        seed=int(digest, 16) % 100_000,
        raw={"digest": digest, "size": [width, height]},
    )


class FakeImageClient(ImageClient):
    """호출을 기록하고 그럴듯한 PNG를 돌려준다.

    `fail_scenes`는 `{scene_id: 실패시킬 호출 횟수}`다. `{7: 1}`이면 7번 씬의 첫
    호출만 실패하고 재시도는 성공한다(specs/05의 "씬당 1회 재시도"가 실제로 먹히는지),
    `{7: 2}`면 재시도까지 실패해 인접 씬 폴백 경로로 간다.
    """

    #: 페이크는 앵커가 없어도 돈다. 앵커 0장 차단은 과금되는 실물 어댑터의 규칙이다.
    requires_style_anchors = False
    name = "fake"

    def __init__(
        self,
        *,
        fail_scenes: Mapping[int, int] | None = None,
        error: Callable[[int, int], Exception] | None = None,
        width: int = FAKE_WIDTH,
        height: int = FAKE_HEIGHT,
        model_id: str = "fake-image-model",
    ) -> None:
        self.fail_scenes = dict(fail_scenes or {})
        self.error = error
        self.width = width
        self.height = height
        self.model_id = model_id
        self.calls: list[dict[str, Any]] = []

    @property
    def scene_calls(self) -> list[int]:
        """호출된 씬 id 순서. 재시도가 있으면 같은 id가 여러 번 나온다."""
        return [call["scene_id"] for call in self.calls]

    def attempts_for(self, scene_id: int) -> int:
        return self.scene_calls.count(scene_id)

    def generate(
        self, request: ImageRequest, *, timeout: int | None = None
    ) -> GeneratedImage:
        attempt = self.attempts_for(request.scene_id) + 1
        self.calls.append(
            {
                "scene_id": request.scene_id,
                "attempt": attempt,
                "digest": request.digest,
                "prompt": request.prompt,
                "negative_prompt": request.negative_prompt,
                "anchors": tuple(p.name for p in request.style_anchors),
                "timeout": timeout,
                "label": request.label,
            }
        )

        remaining = self.fail_scenes.get(request.scene_id, 0)
        if attempt <= remaining:
            if self.error is not None:
                raise self.error(request.scene_id, attempt)
            raise ImageGenError(
                f"페이크 실패 (씬 {request.scene_id}, {attempt}번째 호출)"
            )

        return fake_image(
            request, width=self.width, height=self.height, model_id=self.model_id
        )
