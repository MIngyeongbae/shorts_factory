"""TTS 호출 어댑터 인터페이스. ADR-0004.

`llm/base.py`와 같은 모양이다. 단계 코드는 이 인터페이스에만 의존하고, 엔진 교체
(IVC voice_id → PVC voice_id, 나아가 다른 엔진)는 어댑터 구현만 바꾼다.

## 왜 어댑터인가

ADR-0004가 정한 것은 "ElevenLabs with-timestamps 단일 호출"이지 특정 HTTP 클라이언트가
아니다. 그리고 이 단계의 실제 일은 호출이 아니라 **문자 정렬에서 씬 경계를 뽑는 것**이다
(ADR-0013). 그 로직은 API 키 없이 전부 검증할 수 있어야 하므로, 호출 표면을 좁게 잘라
페이크(`tts/fake.py`)로 갈아끼운다.

## 정렬(alignment)에 대한 계약

ElevenLabs `with-timestamps`는 응답에 `alignment`와 `normalized_alignment` 둘을 담는다.

- `alignment`: **보낸 텍스트 그대로**의 문자 배열. 씬 경계 추출은 이것만 쓴다.
- `normalized_alignment`: 숫자·단위를 읽는 대로 풀어 쓴 문자 배열("8,000 m³" → "팔천 …").
  문자 수가 원문과 달라 씬의 문자 구간을 짚을 수 없다.

`Alignment.from_api()`가 받는 것은 전자다. 어댑터가 후자를 넘기면 `[3]`의 대조 검사
(`sync.character_spans`)가 불일치로 즉시 실패한다 — 조용히 어긋나지 않는다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

#: ElevenLabs with-timestamps 응답의 정렬 키. 어댑터가 여기에 맞춰 넘긴다.
API_CHARACTERS = "characters"
API_STARTS = "character_start_times_seconds"
API_ENDS = "character_end_times_seconds"

#: 오디오 인코딩. ElevenLabs `output_format=pcm_*`의 원시 리틀엔디언 16bit PCM.
#: wav 컨테이너 씌우기는 `tts/audio.py`가 표준 라이브러리로 한다 (의존성 추가 없음).
PCM_S16LE = "pcm_s16le"


class TTSError(Exception):
    """TTS 호출 실패 일반."""


class TTSRateLimited(TTSError):
    """사용 한도/과금 한도 도달."""


class TTSTimeout(TTSError):
    """응답이 제한 시간 내에 오지 않음."""


@dataclass(frozen=True)
class Alignment:
    """문자 단위 정렬. 세 배열의 길이는 항상 같다."""

    characters: tuple[str, ...]
    starts: tuple[float, ...]
    ends: tuple[float, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "characters", tuple(self.characters))
        object.__setattr__(self, "starts", tuple(float(t) for t in self.starts))
        object.__setattr__(self, "ends", tuple(float(t) for t in self.ends))

        sizes = (len(self.characters), len(self.starts), len(self.ends))
        if len(set(sizes)) != 1:
            raise TTSError(
                "정렬 배열의 길이가 다르다 "
                f"(characters={sizes[0]}, starts={sizes[1]}, ends={sizes[2]})"
            )
        if not self.characters:
            raise TTSError("정렬이 비어 있다")

    @property
    def text(self) -> str:
        """정렬이 덮는 텍스트. 보낸 대본과 글자 하나까지 같아야 한다."""
        return "".join(self.characters)

    @property
    def duration(self) -> float:
        """마지막 문자가 끝나는 시각(초). 원속 기준이다 (atempo 적용 전)."""
        return max(self.ends)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "Alignment":
        """ElevenLabs 응답의 `alignment` 객체를 그대로 받는다."""
        try:
            return cls(
                characters=payload[API_CHARACTERS],
                starts=payload[API_STARTS],
                ends=payload[API_ENDS],
            )
        except (KeyError, TypeError) as exc:
            raise TTSError(
                f"정렬 객체에 필요한 키가 없다 ({API_CHARACTERS}/{API_STARTS}/{API_ENDS}): {exc}"
            ) from exc


@dataclass
class Narration:
    """단일 호출의 결과 — 오디오 + 문자 정렬.

    ADR-0004의 "대본 전체를 단일 호출로 생성"이라 이 객체 하나가 한 편 전체다.
    """

    audio: bytes
    alignment: Alignment
    sample_rate: int
    channels: int = 1
    #: 샘플당 바이트 수. 16bit PCM이면 2.
    sample_width: int = 2
    encoding: str = PCM_S16LE
    request_id: str | None = None
    voice_id: str | None = None
    model_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def raw_duration(self) -> float:
        """정렬 기준 원속 길이(초). atempo 적용 전이다."""
        return self.alignment.duration

    @property
    def meta(self) -> dict[str, Any]:
        """run 상태·timing.json에 남길 추적 정보. 오디오 바이트는 빼고 넘긴다."""
        return {
            "request_id": self.request_id,
            "voice_id": self.voice_id,
            "model_id": self.model_id,
            "encoding": self.encoding,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "characters": len(self.alignment.characters),
        }


class TTSClient(ABC):
    """단계 코드가 보는 유일한 TTS 표면.

    voice_id·model_id·API 키는 어댑터 생성자에서 주입한다 (ADR-0004: voice_id는
    환경변수). 단계는 "이 텍스트를 읽어라"만 안다.
    """

    @abstractmethod
    def synthesize(
        self,
        text: str,
        *,
        timeout: int | None = None,
        label: str = "",
    ) -> Narration:
        """대본 전체를 **한 번에** 읽어 오디오와 문자 정렬을 돌려준다.

        문장별 분할 호출은 하지 않는다 — 문장 간 톤 불연속이 ADR-0004의 탈락 사유다.
        """
        raise NotImplementedError
