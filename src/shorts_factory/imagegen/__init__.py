"""이미지 생성 어댑터 — **휴면**.

휴면 — 호출자 없음. 정지 이미지 쇼츠가 결정되면 되살린다 (ADR-0056 되돌릴 조건 6).

ADR-0056이 이미지 단계(`[6]`·`[6r]`·`[6i]`)를 파이프라인에서 뗐다. 어느 단계도 CLI도
이 패키지를 import하지 않는다. 남긴 것은 MJ 이미지 어댑터(`midjourney.py` — 제출·폴링·
취소·내려받기·인물 시트 참조 왕복)와 그 계약(`base.py`)·페이크(`fake.py`)이고, 계약
테스트(`test_image_client`·`test_midjourney_client`)가 썩지 않게 지킨다. NB2 어댑터는
소비자가 없어져 삭제됐다 (ADR-0005·0021 폐기).

살아 있는 코드가 여기에 기대면 안 된다 — HTTP 경계는 공용 `transport.py`로 옮겼다.
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
