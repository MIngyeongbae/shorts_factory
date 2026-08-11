"""이미지 생성 어댑터. `llm/`·`tts/`와 같은 자리다 (ADR-0005).

단계 코드(`stages/imagegen.py`)는 `base.ImageClient`에만 의존한다. 개발·테스트는
`fake.FakeImageClient`로 돌리고, 과금되는 실물 호출은 `nano_banana.NanoBananaClient`가
맡는다 — 다만 그 호출 경로는 아직 결정되지 않았다 (해당 모듈 참고).
"""

from .base import (
    ANCHOR_SUFFIXES,
    GeneratedImage,
    ImageClient,
    ImageGenError,
    ImageGenRateLimited,
    ImageGenTimeout,
    ImageRequest,
    ProviderNotConfigured,
    discover_style_anchors,
)

__all__ = [
    "ANCHOR_SUFFIXES",
    "GeneratedImage",
    "ImageClient",
    "ImageGenError",
    "ImageGenRateLimited",
    "ImageGenTimeout",
    "ImageRequest",
    "ProviderNotConfigured",
    "discover_style_anchors",
]
