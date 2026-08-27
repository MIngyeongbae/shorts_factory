"""TTS 호출 어댑터 인터페이스. ADR-0004, ADR-0081.

`llm/base.py`와 같은 모양이다. 단계 코드는 이 인터페이스에만 의존하고, **엔진 교체는
어댑터 구현만 바꾼다** — 지금 실물이 둘이다 (`typecast.TypecastClient` 기본,
`elevenlabs.ElevenLabsClient`. ADR-0081).

## 왜 어댑터인가

ADR-0004가 정한 것은 "with-timestamps 단일 호출"이지 특정 벤더도, 특정 HTTP
클라이언트도 아니다. 그리고 이 단계의 실제 일은 호출이 아니라 **문자 정렬에서 씬 경계를
뽑는 것**이다 (ADR-0013). 그 로직은 API 키 없이 전부 검증할 수 있어야 하므로, 호출
표면을 좁게 잘라 페이크(`tts/fake.py`)로 갈아끼운다. 엔진을 실제로 갈아 본 것이
ADR-0081이고, 그때 `stages/tts.py`는 한 줄도 바뀌지 않았다.

## 정렬(alignment)에 대한 계약

**정렬은 언제나 보낸 텍스트 그대로여야 한다.** 씬의 문자 구간을 짚는 근거이기 때문이다
(`sync.character_spans`). 벤더가 주는 모양이 둘이라 생성자도 둘이다.

- `Alignment.from_api()` — ElevenLabs 모양. 평행 배열 세 개(`characters`,
  `character_start_times_seconds`, `character_end_times_seconds`)를 받는다
- `Alignment.from_segments()` — 타입캐스트 모양. `{"text","start","end"}` 객체의 목록을
  받는다 (`with-timestamps`의 `characters`, `granularity=char`)

ElevenLabs 응답에는 `normalized_alignment`가 함께 온다 — 숫자·단위를 읽는 대로 풀어 쓴
문자 배열("8,000 m³" → "팔천 …")이라 문자 수가 원문과 다르다. 어댑터가 그것을 넘기면
`[3]`의 대조 검사가 불일치로 즉시 실패한다 — 조용히 어긋나지 않는다. 타입캐스트에는
대응물이 없다 (ADR-0081 — 엔진이 무엇을 더 읽었는지 보는 창이 그 제공자에서는 닫힌다).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

#: ElevenLabs with-timestamps 응답의 정렬 키. 어댑터가 여기에 맞춰 넘긴다.
API_CHARACTERS = "characters"
API_STARTS = "character_start_times_seconds"
API_ENDS = "character_end_times_seconds"

#: 타입캐스트 with-timestamps의 정렬 원소 키 (`characters[i]`). ADR-0081.
SEGMENT_TEXT = "text"
SEGMENT_START = "start"
SEGMENT_END = "end"

#: 오디오 인코딩. 원시 리틀엔디언 16bit PCM이다 — ElevenLabs는 `output_format=pcm_*`로
#: 받고, 타입캐스트는 wav로 받아 어댑터가 컨테이너를 벗긴다 (ADR-0081 결정 4).
#: wav 컨테이너 씌우기는 `tts/audio.py`가 표준 라이브러리로 한다 (의존성 추가 없음).
PCM_S16LE = "pcm_s16le"


class TTSError(Exception):
    """TTS 호출 실패 일반."""


class TTSNotConfigured(TTSError):
    """키·voice_id·플랜이 없어 호출 자체가 성립하지 않음. 재시도할 것이 없다."""


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
        """마지막 문자가 끝나는 시각(초). **어댑터가 내준 오디오와 같은 시간축**이다
        (엔진이 배속을 걸었으면 그것이 실려 있다 — `Narration.tempo` 참고)."""
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

    @classmethod
    def from_segments(cls, segments: Sequence[Mapping[str, Any]]) -> "Alignment":
        """`[{"text": "가", "start": 0.1, "end": 0.2}, …]` 모양의 정렬 (ADR-0081).

        타입캐스트 `with-timestamps`의 `characters`가 이 모양이다. `granularity=char`로
        불러야 원소 하나가 글자 하나다 — word로 부르면 ja·zh는 문장 전체가 원소 하나로
        뭉쳐 오고, 그러면 씬 구간을 짚을 수 없다 (`typecast.py` 참고).
        """
        if not isinstance(segments, (list, tuple)):
            raise TTSError(f"정렬이 목록이 아니다: {type(segments).__name__}")

        characters: list[str] = []
        starts: list[float] = []
        ends: list[float] = []
        for index, segment in enumerate(segments):
            try:
                characters.append(str(segment[SEGMENT_TEXT]))
                starts.append(float(segment[SEGMENT_START]))
                ends.append(float(segment[SEGMENT_END]))
            except (KeyError, TypeError, ValueError) as exc:
                raise TTSError(
                    f"정렬 원소 {index}에 필요한 키가 없다 "
                    f"({SEGMENT_TEXT}/{SEGMENT_START}/{SEGMENT_END}): {segment!r}"
                ) from exc
        return cls(characters=characters, starts=starts, ends=ends)


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
    #: 어느 어댑터가 만든 오디오인가 (`typecast` | `elevenlabs` | `fake`). 엔진이 둘이
    #: 된 뒤로는(ADR-0081) 모델 id만으로 가릴 수 없다 — `timing.{lang}.json`이 이걸 남긴다.
    provider: str | None = None
    #: **이 오디오와 정렬에 이미 실려 있는 배속** (ADR-0081 결정 6 개정). 1.0이면 원속이고
    #: 남은 배속은 `[3]`이 FFmpeg atempo로 건다. 엔진이 직접 건 경우(타입캐스트
    #: `output.audio_tempo`)에는 정렬도 같은 시간축이라 `[3]`이 더 손대면 안 된다.
    tempo: float = 1.0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def raw_duration(self) -> float:
        """정렬 기준 길이(초) — **FFmpeg atempo 적용 전**이다.

        `tempo`가 1.0이 아니면 그 배속은 이미 실려 있다 (엔진이 직접 걸었다). 즉 이 값은
        "엔진이 내준 오디오의 길이"이지 언제나 원속은 아니다 (ADR-0081 결정 6 개정).
        """
        return self.alignment.duration

    @property
    def meta(self) -> dict[str, Any]:
        """run 상태·timing.json에 남길 추적 정보. 오디오 바이트는 빼고 넘긴다."""
        return {
            "provider": self.provider,
            "tempo": self.tempo,
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
    환경변수 — 언어별, ADR-0056 결정 7). 단계는 "이 텍스트를 읽어라"만 안다.
    """

    def check_configured(self) -> None:
        """**호출 전에** 키·voice_id가 있는지 확인한다. 없으면 `TTSNotConfigured`.

        `[3]`이 세 언어를 도는데 두 번째 언어의 id가 비어 있으면 첫 언어의 과금이 헛되다
        (스펙 05 `[3]` — "대본 파일이 있는데 id가 비어 있으면 진입 전에 멈춘다"). 그래서
        단계가 어느 언어도 부르기 전에 전부 묻는다. 기본은 "확인할 것이 없다"다.
        """
        return None

    @abstractmethod
    def synthesize(
        self,
        text: str,
        *,
        timeout: int | None = None,
        label: str = "",
        tempo: float = 1.0,
    ) -> Narration:
        """대본 전체를 **한 번에** 읽어 오디오와 문자 정렬을 돌려준다.

        문장별 분할 호출은 하지 않는다 — 문장 간 톤 불연속이 ADR-0004의 탈락 사유다.

        `tempo`는 `[3]`이 이 편에 걸려는 **총 배속**이다 (specs/04는 1.1~1.2).
        **어댑터는 걸 수 있으면 걸고, 건 만큼을 `Narration.tempo`로 알린다** — 못 걸면
        1.0을 그대로 두면 되고, 그러면 `[3]`이 FFmpeg atempo로 남은 몫을 건다. 어느
        쪽이든 `[3]`은 `tempo / narration.tempo`만 후처리하므로 배속이 겹치지 않는다
        (ADR-0081 결정 6 개정 — 엔진 쪽 배속은 정렬도 같이 당긴다는 것이 실측됐다).
        """
        raise NotImplementedError
