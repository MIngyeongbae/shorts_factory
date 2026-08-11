"""이미지 생성 호출 어댑터 인터페이스. ADR-0005.

`llm/base.py`·`tts/base.py`와 같은 모양이다. 단계 코드는 이 인터페이스에만 의존하고,
모델 교체(Nano Banana 2 → Pro, 나아가 FLUX 2 — ADR-0005의 되돌릴 조건)는 어댑터 구현만
바꾼다.

## 왜 어댑터인가

이 단계의 실제 일은 호출이 아니라 **실패 처리**다 (specs/05: 씬당 1회 재시도, 2회 실패 시
인접 씬 이미지 재사용 폴백). 그 로직은 편당 ~$2.5를 쓰지 않고 전부 검증할 수 있어야
하므로 호출 표면을 좁게 잘라 페이크(`imagegen/fake.py`)로 갈아끼운다.

## 요청에 담기는 것 = 계약이 [6]에게 준 것뿐이다 (ADR-0020)

`prompts.json`의 `prompt`·`negative_prompt`와 `style` 블록이 전부다.

- `overlays`는 담지 않는다. 전부 레이어 B라 `[8. overlay]` 소관이다 (ADR-0019)
- `framing_reuse_of`도 담지 않는다. **이미지 재사용 힌트가 아니다** — 구도만 같고
  `subject`는 가리키는 씬과 다르다. 캐시 키로 쓰면 다른 피사체가 같은 그림이 된다
- 시간 정보는 애초에 `prompts.json`에 없다 (ADR-0020)
- `prompt` 안의 `subject`는 한국어 그대로다. 번역하지 않고, 그 한국어를 화면에 글자로
  그리지 말라는 지시가 프롬프트 본문에 이미 들어 있다 (ADR-0001·0002·0014)
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 파일 시그니처. 어댑터가 **스스로 밝힌 mime과 다른 바이트**를 돌려주면 그 자리에서
#: 실패로 친다 — 조용히 어긋나면 `[7]`이 FFmpeg에서 터진다.
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"

#: 저장 가능한 포맷 → (시그니처, 확장자).
#: JPEG이 여기 있는 이유는 **Nano Banana 2가 JPEG만 주기 때문이다** — `image/png`을
#: 요청하면 400으로 거절한다 (ADR-0021, 실호출로 확인). 페이크는 stdlib로 만들 수 있는
#: PNG를 낸다. 그래서 확장자는 프로바이더가 정한다 (`ImageClient.output_suffix`).
IMAGE_FORMATS = {
    "image/png": (PNG_MAGIC, ".png"),
    "image/jpeg": (JPEG_MAGIC, ".jpg"),
}

#: 요청 지문을 만들 때 필드를 잇는 구분자. 프롬프트 본문에 나올 수 없는 제어문자라
#: 필드 경계가 섞이지 않는다 (`"a|b"` + `"c"`와 `"a"` + `"b|c"`가 같은 지문이 되는 일).
SEPARATOR = "\x1f"

#: 스타일 앵커로 인정하는 확장자 (ADR-0005). `.gitignore`의 예외 목록과 같다.
#: `stages/prompt.py`에도 같은 목록이 있다 — 둘 다 .gitignore에서 왔고, 공유 모듈을
#: 새로 만들면서 다른 워크트리와 충돌 지점을 만들지 않기 위해 여기 다시 적는다.
ANCHOR_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


class ImageGenError(Exception):
    """이미지 생성 호출 실패 일반. 씬 단위 재시도 대상이다."""


class ImageGenRateLimited(ImageGenError):
    """사용 한도/과금 한도 도달."""


class ImageGenTimeout(ImageGenError):
    """응답이 제한 시간 내에 오지 않음."""


class ProviderNotConfigured(ImageGenError):
    """어댑터를 쓸 수 없는 상태 (키 없음, 호출 경로 미결정 등).

    씬 하나의 실패가 아니라 프로바이더 전체의 문제라 `[6]`은 재시도하지 않고 즉시 멈춘다.
    씬마다 같은 오류로 두 번씩 실패하며 전 씬을 폴백으로 채우는 것은 결과가 아니라 소음이다.
    """


def discover_style_anchors(anchor_dir: Path) -> tuple[Path, ...]:
    """스타일 앵커 이미지 목록 (ADR-0005 "모든 생성 호출에 레퍼런스로 첨부").

    이름 순으로 고정한다 — 순서가 흔들리면 요청 다이제스트가 흔들리고, 그러면
    아무것도 안 바뀌었는데 이미 만든 이미지를 다시 사게 된다.
    """
    if not anchor_dir.is_dir():
        return ()
    return tuple(
        sorted(
            (p for p in anchor_dir.iterdir()
             if p.is_file() and p.suffix.lower() in ANCHOR_SUFFIXES),
            key=lambda p: p.name,
        )
    )


@dataclass(frozen=True)
class ImageRequest:
    """씬 하나에 대한 생성 요청 = 호출 1회 (ADR-0019: 2-pass 없음)."""

    scene_id: int
    prompt: str
    negative_prompt: str
    aspect_ratio: str
    resolution: str
    #: ADR-0005의 룩 일관성 수단. 지금 저장소에 0장이라 비어 있을 수 있다.
    style_anchors: tuple[Path, ...] = ()
    label: str = ""

    @classmethod
    def from_prompt_scene(
        cls,
        scene: dict[str, Any],
        style: dict[str, Any],
        *,
        style_anchors: tuple[Path, ...] = (),
        label: str = "",
    ) -> "ImageRequest":
        """`prompts.json`의 씬 항목 + `style` 블록 → 요청.

        계약이 `[6]`에게 준 필드만 읽는다. `beat`·`camera`·`motion`·`framing`은
        읽지 않는다 — `[7]`과 `[9]`가 `scenes.timed.json`에서 읽는 값이고, 여기서
        같은 값을 또 해석하면 출처가 둘이 된다 (ADR-0020).
        """
        return cls(
            scene_id=scene["scene_id"],
            prompt=scene["prompt"],
            negative_prompt=scene["negative_prompt"],
            aspect_ratio=style["aspect_ratio"],
            resolution=style["resolution"],
            style_anchors=tuple(style_anchors),
            label=label,
        )

    @property
    def digest(self) -> str:
        """요청 지문. 같은 지문이면 이미 만든 이미지를 다시 사지 않는다.

        앵커는 파일 **이름**만 넣는다. 내용까지 해싱하면 매 실행마다 수 MB를 읽게 되고,
        앵커 교체는 파일 추가/교체로 드러나므로 이름만으로 충분하다 (ADR-0005는 앵커
        변경을 스펙 수정으로 취급한다 — 조용히 바뀌지 않는다).
        """
        material = SEPARATOR.join(
            (
                self.prompt,
                self.negative_prompt,
                self.aspect_ratio,
                self.resolution,
                *(p.name for p in self.style_anchors),
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


@dataclass
class GeneratedImage:
    """호출 1회의 결과 — 이미지 바이트 + 추적 정보."""

    data: bytes
    mime_type: str = "image/png"
    request_id: str | None = None
    model_id: str | None = None
    seed: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.data:
            raise ImageGenError("이미지 바이트가 비어 있다")
        known = IMAGE_FORMATS.get(self.mime_type)
        if known is None:
            raise ImageGenError(
                f"저장할 수 없는 포맷이다: {self.mime_type} "
                f"(가능: {', '.join(sorted(IMAGE_FORMATS))})"
            )
        magic, _ = known
        if not self.data.startswith(magic):
            raise ImageGenError(
                f"{self.mime_type}이라면서 시그니처가 다르다. 응답 본문이 이미지가 아니다"
            )

    @property
    def suffix(self) -> str:
        """이 바이트를 저장할 확장자."""
        return IMAGE_FORMATS[self.mime_type][1]

    @property
    def meta(self) -> dict[str, Any]:
        """실행 기록에 남길 추적 정보. 이미지 바이트는 빼고 넘긴다."""
        return {
            "request_id": self.request_id,
            "model_id": self.model_id,
            "seed": self.seed,
            "bytes": len(self.data),
        }


class ImageClient(ABC):
    """단계 코드가 보는 유일한 이미지 생성 표면.

    모델 id·API 키·해상도 파라미터는 어댑터 생성자에서 주입한다. 단계는 "이 요청으로
    한 장 만들어라"만 안다.
    """

    #: ADR-0005의 룩 일관성 수단(스타일 앵커)이 없으면 돌리면 안 되는 프로바이더인가.
    #: 과금되는 실물 어댑터는 True다 — 앵커 0장으로 편당 ~$2.5를 쓰면 씬 간 룩이
    #: 갈리고 어차피 다시 만들게 된다. 페이크는 False다.
    requires_style_anchors: bool = True

    #: 실행 기록에 남길 프로바이더 이름.
    name: str = "image-client"

    #: 이 프로바이더가 내는 파일의 확장자. 단계가 파일명을 지을 때 쓴다.
    #: 기본값이 `.jpg`인 이유는 실물(Nano Banana 2)이 JPEG만 주기 때문이다 (ADR-0021).
    output_suffix: str = ".jpg"

    @abstractmethod
    def generate(
        self, request: ImageRequest, *, timeout: int | None = None
    ) -> GeneratedImage:
        """씬 하나의 베이스 이미지를 **한 장** 만든다.

        어노테이션 2-pass는 없다 (ADR-0019). 모든 베이스 이미지는 클린이다.
        """
        raise NotImplementedError
