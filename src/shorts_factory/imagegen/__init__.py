"""이미지 생성 어댑터 — `[6. frames]`가 쓴다 (ADR-0071).

ADR-0056이 이미지 단계를 뗐다가, ADR-0071이 **프레임을 입력으로 받는 라인**을 위해
`[6]`을 되살렸다. 지금 소비자는 `stages/frames.py` 하나다:

- `midjourney.py` — CLEAN. imagine(그리드) → `upscale(quadrant)`(낱장 주소) →
  `download` → `upload`(우리 CLEAN을 공개 주소로). 제출·폴링·취소·인물 시트 참조 왕복은
  ADR-0025·0031·0035·0039·0051의 실측이 든 코드 그대로다
- `nano_banana.py` — **소비자 없음.** `edit()`가 CLEAN 위에 계측 표시를 얹던 경로는
  ADR-0075 결정 2가 폐기했다 (지표는 좋았지만 사람 판독에서 졌다). 어댑터만 남겨 둔다 (ADR-0021의 호출
  경로 그대로, ADR-0043의 `[6i]`가 쓰던 그 함수다)
- `base.py` 계약과 `fake.py` 대역

프레임을 안 받는 라인(`local`·`api`)은 이 패키지를 지나가지 않는다 — 그 라인에서
화면 그래픽은 `[7]`이 영상 모델에 프롬프트로 시킨다.

HTTP 경계는 공용 `transport.py`다 (`CDN_HEADERS` 포함 — R2 저장에서 기본 UA가 막힌다).
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
