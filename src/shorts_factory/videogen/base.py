"""영상 생성 호출 어댑터 인터페이스. ADR-0039 → ADR-0056.

`[7. videogen]`은 이 인터페이스에만 의존하고, 프로바이더 교체(Omni 직접 → fal 경유,
ADR-0056 되돌릴 조건 2)는 어댑터 구현만 바꾼다.

## 요청에 담기는 것

씬 하나의 클립을 만드는 데 필요한 것만이다. **주 경로는 텍스트→영상이다** (ADR-0056):

- `prompt`·`negative_prompt` — `[5]`의 `prompts.json`에서 온 영상 지시. FORMAT 절의
  초 수는 `[7]`이 채운 뒤 넘긴다 (`{seconds}` 자리표시자, 스펙 03)
- `seconds` — 요청 클립 길이(정수 초). 세 언어 중 최장 씬 + 0.6초의 올림, 3~10 (스펙 05 `[7]`)

아래는 **참조 프레임 경로**(`videogen/veo.py`, ADR-0043 실측)가 쓰던 필드다. 그 어댑터는
파이프라인 밖이지만 실측 독스트링을 들고 있어 지우지 않았고, 필드도 그대로 둔다:

- `first_frame`·`last_frame` — 시작·끝 프레임 로컬 파일 (Veo)
- `source_task_id`·`quadrant` — MJ 영상의 잡 id 입력 (어댑터는 삭제됐다, ADR-0056)
- `motion_prompt`·`duration` — Veo가 쓰던 카메라 구절·실수 길이
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

#: mp4 시그니처. `ftyp` 박스가 4바이트 오프셋에 온다.
MP4_BRAND = b"ftyp"


class VideoGenError(Exception):
    """영상 생성 실패 일반. 씬 단위 강등 대상이다."""


class VideoGenRateLimited(VideoGenError):
    """사용 한도 도달."""


class VideoGenTimeout(VideoGenError):
    """잡이 제한 시간 내에 끝나지 않음."""


class VideoProviderNotConfigured(VideoGenError):
    """어댑터를 쓸 수 없는 상태 (플랜 미달, 키 없음, 파라미터 거부 등).

    씬 하나의 실패가 아니라 프로바이더 전체의 문제라 `[7]`은 **남은 씬을 시도하지 않고
    멈춘다** (스펙 05 `[7]` — D-5의 "돈이 나가는" 경우). 같은 오류로 25번 실패하며
    과금을 쌓는 것은 결과가 아니라 소음이다.
    """


@dataclass(frozen=True)
class VideoRequest:
    """씬 하나에 대한 클립 요청 = 호출 1회.

    **텍스트→영상**(Omni, ADR-0056)은 `prompt`·`negative_prompt`·`seconds`만 쓴다.
    참조 프레임 필드(`first_frame`·`last_frame`)는 Veo 어댑터(ADR-0043 실측)의 것이고
    파이프라인은 더 이상 채우지 않는다.
    """

    scene_id: int
    #: 영상 지시 — `prompts.json`의 `prompt`에 FORMAT 초 수를 채운 것 (스펙 03·05).
    prompt: str = ""
    negative_prompt: str = ""
    #: 요청 클립 길이(정수 초). 3~10 (스펙 05 `[7]`). 0이면 프로바이더가 정한다.
    seconds: int = 0
    #: --- 아래는 참조 프레임·잡 id 경로의 필드 (모듈 독스트링) ---
    source_task_id: str = ""
    quadrant: int = 0
    first_frame: Any | None = None
    last_frame: Any | None = None
    motion_prompt: str = ""
    duration: float = 0.0
    label: str = ""


@dataclass
class GeneratedClip:
    """잡 1회의 결과 — mp4 바이트 + 추적 정보."""

    data: bytes
    request_id: str | None = None
    model_id: str | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.data:
            raise VideoGenError("클립 바이트가 비어 있다")
        # 조용히 틀리는 경로를 막는다 — 서버가 실패 페이지를 200으로 주면 그것이
        # `clips/`에 mp4 이름으로 앉고, `[9]`가 FFmpeg에서야 터진다.
        if not self.looks_like_mp4(self.data):
            raise VideoGenError("mp4가 아니다 — 응답 본문이 영상이 아니다")

    @staticmethod
    def looks_like_mp4(data: bytes) -> bool:
        """`ftyp` 박스가 머리에 있는가. 어댑터가 JSON과 영상을 가를 때도 쓴다."""
        return MP4_BRAND in data[:32]

    @property
    def meta(self) -> dict[str, Any]:
        """실행 기록에 남길 추적 정보. 바이트는 빼고 넘긴다."""
        return {
            "request_id": self.request_id,
            "model_id": self.model_id,
            "width": self.width,
            "height": self.height,
            "duration": self.duration,
            "bytes": len(self.data),
        }


class VideoClient(ABC):
    """`[7]`이 보는 유일한 영상 생성 표면."""

    #: 실행 기록에 남길 프로바이더 이름.
    name: str = "video-client"

    #: 이 프로바이더가 내는 파일의 확장자.
    output_suffix: str = ".mp4"

    #: 잡 id 입력 경로(삭제된 MJ 영상 어댑터)가 쓰던 선언. 텍스트→영상 어댑터는 비워 둔다.
    source_provider: str = ""

    #: 이 프로바이더가 받는 클립 길이(정수 초)의 범위. `[7]`이 세 언어 최장 + 꼬리를 여기에
    #: 가둔다 (스펙 05 `[7]`). 어댑터마다 다르다 (Omni 3~10, H3 4~10 — ADR-0059) — 단계가
    #: 한 어댑터의 상수를 import하지 않는다.
    min_seconds: int = 3
    max_seconds: int = 10

    def concurrency(self) -> int:
        """동시에 던져도 되는 잡 수 (ADR-0031 G3의 계약) — **단계가 숫자를 적지 않는다.**
        `[7]`은 이 값을 워커 수 기본으로 쓰고, 429가 오면 1로 줄인다 (스펙 05). `--jobs`가
        이긴다. 못 읽으면 1로 떨어진다.
        """
        return 1

    @abstractmethod
    def generate(
        self, request: VideoRequest, *, timeout: int | None = None
    ) -> GeneratedClip:
        """클립 하나. `timeout=None`은 "프로바이더가 정한다"다 (ADR-0035)."""
        raise NotImplementedError
