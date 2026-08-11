"""Nano Banana 2 어댑터 자리 — **아직 호출 경로가 결정되지 않았다.**

ADR-0005가 정한 것은 *모델*("Nano Banana 2 + 스타일 앵커 레퍼런스, 2K, 4K 금지")이지
*호출 방법*이 아니다. 아래 여섯 가지가 정해져야 이 클래스가 실물이 된다. 전부 되돌리기
비싼 선택이라 코드에서 임의로 정하지 않는다 (CLAUDE.md 원칙 2 — ADR 대상).

1. 전송 경로: 공식 SDK인가 REST인가 (의존성 추가 여부가 갈린다)
2. 엔드포인트·모델 식별자 문자열
3. 인증 방식과 API 키를 담을 환경변수 이름
4. 스타일 앵커 첨부 방식 — 파일 업로드인가 base64 인라인인가, 회당 최대 장수는 몇인가
   (ADR-0005는 3~5장을 전제한다)
5. 종횡비·해상도 파라미터 표기 (`aspect_ratio="9:16"` / 픽셀 지정 / 프리셋 중 무엇인가)
6. 레이트리밋·과금 오류의 응답 형태 → `ImageGenRateLimited` 매핑

## 그래서 지금 이 모듈이 하는 일

`[6]`의 기본 프로바이더로 남아 **호출을 시도하지 않고 즉시 거절한다.** 기본값을
페이크로 두면 "이미지를 만들었다"고 착각한 채 단색 PNG 27장을 들고 다음 단계로 가게
된다 — 그쪽이 훨씬 비싼 실수다.

호출 형태 자체는 프로바이더 중립으로 이미 잡혀 있다 (`base.ImageRequest`). 위 여섯이
정해지면 이 클래스의 `generate` 본문만 채우면 된다.
"""

from __future__ import annotations

from .base import GeneratedImage, ImageClient, ImageRequest, ProviderNotConfigured

#: ADR-0005가 고른 모델. 실제 API 식별자 문자열은 아직 정해진 적이 없다.
MODEL_NAME = "nano-banana-2"

UNDECIDED = (
    "Nano Banana 2 호출 경로가 아직 결정되지 않았다 (ADR-0005는 모델만 정했다). "
    "정해야 할 것: (1) SDK인가 REST인가 (2) 엔드포인트·모델 식별자 "
    "(3) 인증 방식과 API 키 환경변수 이름 (4) 스타일 앵커 첨부 방식과 회당 최대 장수 "
    "(5) 종횡비·해상도 파라미터 표기 (6) 레이트리밋 응답 매핑. "
    "되돌리기 비싼 선택이라 ADR로 정한다. "
    "개발·테스트는 --provider fake로 돌린다 (편당 ~$2.5를 쓰지 않는다)."
)


class NanoBananaClient(ImageClient):
    """ADR-0005의 기본 모델 자리. 호출 경로가 정해질 때까지 거절한다."""

    #: 앵커 없이 과금 호출을 돌리면 씬 간 룩이 갈리고 어차피 다시 만들게 된다.
    requires_style_anchors = True
    name = MODEL_NAME

    def __init__(self, *, model_id: str = MODEL_NAME, api_key: str | None = None) -> None:
        self.model_id = model_id
        self.api_key = api_key

    def generate(
        self, request: ImageRequest, *, timeout: int | None = None
    ) -> GeneratedImage:
        raise ProviderNotConfigured(UNDECIDED)
