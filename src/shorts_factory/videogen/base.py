"""영상 생성 호출 어댑터 인터페이스. ADR-0039.

`imagegen/base.py`와 같은 모양이다. `[7. motion]`은 이 인터페이스에만 의존하고,
프로바이더 교체(MJ → Kling, ADR-0025의 밴 대비 차선)는 어댑터 구현만 바꾼다.

## 왜 이미지와 따로인가

같은 프록시를 쓰지만 **계약이 다르다.** 이미지는 프롬프트 하나로 끝나는데 영상은
**이미 만든 이미지가 입력**이고, MJ의 경우 그 입력이 로컬 파일이 아니라 **MJ가 닿을 수
있는 주소**여야 한다 (ADR-0025). 그 사슬을 `ImageClient`에 끼워 넣으면 `[6]`이 모르는
개념을 지고 가게 된다.

## 요청에 담기는 것

씬 하나의 클립을 만드는 데 필요한 것만이다.

- `source_task_id` — 그 이미지를 만든 이미지 잡의 id. **파일 경로가 아니다.**
  MJ는 자기가 만든 이미지를 자기 잡 id로 가리킬 때만 입력으로 받는다 (ADR-0025).
  출처는 `image_source.json`이다 — `[6]`이 쓰고 `[6r]`이 갱신하는 사이드카 계약이고,
  `[7]`은 `images.json`을 열지 않는다 (ADR-0041, ADR-0024 §2)
- `quadrant` — 그 잡의 몇 번째 장인가. `[6r]`이 사분면을 바꿔 끼웠으면 그 값이다
  (ADR-0031 §2). 기본은 0
- `motion_prompt` — 카메라 워크의 영어 구절. 씬 계약의 `camera`에서 오고, 문자열은
  `specs/schema/vocab.json`의 `meta.camera[*].video_prompt`가 정본이다 (ADR-0034 §3)
- `duration` — 씬 길이 + 디졸브 겹침 (ADR-0024). **지금은 참고값이다** — MJ 클립은
  5.208초 고정이고 길이를 맞추는 것은 `[9]`의 트림이다
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
    """어댑터를 쓸 수 없는 상태 (플랜 미달, 키 없음 등).

    씬 하나의 실패가 아니라 프로바이더 전체의 문제라 `[7]`은 남은 씬을 시도하지 않고
    전부 `kenburns`로 내려간다 — 같은 오류로 27번 실패하며 GPU를 태우는 것은 결과가
    아니라 소음이다. **relax 영상이 Pro 미만에서 막히는 것이 정확히 이 경우다**
    (ADR-0039 §4).
    """


@dataclass(frozen=True)
class VideoRequest:
    """씬 하나에 대한 클립 요청 = 잡 1회.

    입력 이미지를 가리키는 길이 **둘 중 하나**다 (프로바이더가 정한다):

    - **잡 id** (`source_task_id`+`quadrant`) — MJ. 자기가 만든 이미지를 자기 잡 id로만
      받는다 (ADR-0025·0041)
    - **로컬 파일** (`first_frame`(+`last_frame`)) — Veo (ADR-0043). base64 인라인이라
      "닿는 URL"을 만들 왕복이 없다. `last_frame`이 있으면 first→last 보간·조립이다
    """

    scene_id: int
    #: 이 씬 이미지를 만든 이미지 잡의 id (`image_source.json`의 `task_id`, ADR-0041).
    #: 파일 입력 프로바이더(Veo)에서는 비워 둔다.
    source_task_id: str = ""
    #: 그 잡의 몇 번째 장인가. `[6r]`이 고른 사분면 (ADR-0031 §2).
    quadrant: int = 0
    #: 시작 프레임 로컬 파일 (ADR-0043). 인포씬에서 CLEAN(`images/{scene_id}.png`)이다.
    first_frame: Any | None = None
    #: 끝 프레임 로컬 파일 (ADR-0043). 인포씬에서 INFO(`info/{scene_id}.jpg`)다.
    last_frame: Any | None = None
    motion_prompt: str = ""
    #: 씬 길이 + 겹침 (ADR-0024). MJ는 고정 길이를 주고, Veo는 4/6/8초에서 고른다.
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
        # 조용히 틀리는 경로를 막는다 — 프록시가 실패 페이지를 200으로 주면 그것이
        # `clips/`에 mp4 이름으로 앉고, `[9]`가 FFmpeg에서야 터진다.
        if MP4_BRAND not in self.data[:32]:
            raise VideoGenError("mp4가 아니다 — 응답 본문이 영상이 아니다")

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

    #: 이 어댑터가 입력으로 받을 수 있는 **이미지 프로바이더의 이름**
    #: (`image_source.json`의 `provider`, ADR-0041).
    #:
    #: 잡 id는 그것을 만든 곳에서만 통한다 — MJ 잡 id를 다른 프로바이더에 넣으면
    #: 씬 수만큼 실패한다. `[7]`은 이름이 다르면 **호출 없이** 전 씬을 강등한다.
    #: 기본값이 빈 문자열인 것은 의도적이다: 선언하지 않은 어댑터는 아무 입력도 받지
    #: 못하고, `[7]`이 그 사실을 경고로 남긴다.
    source_provider: str = ""

    def concurrency(self) -> int:
        """동시에 던져도 되는 잡 수. `[6]`의 `ImageClient.concurrency()`와 같은 계약이다
        (ADR-0031 G3) — **단계가 숫자를 적지 않는다.** 못 읽으면 1로 떨어진다.
        """
        return 1

    @abstractmethod
    def generate(
        self, request: VideoRequest, *, timeout: int | None = None
    ) -> GeneratedClip:
        """클립 하나. `timeout=None`은 "프로바이더가 정한다"다 (ADR-0035)."""
        raise NotImplementedError
