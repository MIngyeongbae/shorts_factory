"""전환 계획 — 씬 계약을 타임라인 배치로 옮긴다. specs/03 "전환 규칙".

    - 기본: 크로스 디졸브 0.4~0.6초
    - `turning_point` 진입 시: 디졸브 없이 컷 + 오디오 룰의 차임 동기화
    - 하드컷은 `hook_twist`, `dilemma_peak` 진입 시에만 허용

읽는 곳은 `scenes.timed.json` 하나다 (ADR-0020). 전환이 비트에 걸려 있어 `beat`을,
배치가 시각에 걸려 있어 `start`/`end`를 같은 파일에서 읽는다.

## 디졸브 길이를 0.6초로 고정하는 근거

스펙 03은 0.4~0.6초의 폭을 준다. 그중 0.6을 쓰는 이유는 **클립 계약이 그 값을 이미
정했기 때문**이다 — specs/05 `[7. motion]`: "클립 길이 = 씬 길이 + 디졸브 겹침 0.6초".
0.4를 쓰면 클립마다 0.2초가 쓰이지 않고 남는다. 값을 고르는 것이 아니라 이미 고른 값을
읽는 것이다.

## 배치 (기하)

클립 `i`의 로컬 0초가 씬 `i`의 `start`다. 클립은 씬 길이보다 0.6초 길고, 그 꼬리가
다음 씬으로 넘어가는 겹침이다 (위 `[7]` 계약의 직독). 그래서

    - 디졸브: 다음 씬의 `start`에서 시작해 0.6초 — 앞 클립의 꼬리를 정확히 소진한다
    - 하드컷: 앞 클립을 씬 `end`에서 자르고 다음 클립을 그 자리에 붙인다 (꼬리는 버린다)

두 경우 모두 **클립 `k`를 붙인 뒤의 누적 길이 = `end_k` + (그 클립의 남은 꼬리)**라는
불변식이 성립한다. 마지막 클립은 꼬리를 쓰지 않으므로 타임라인 총 길이는
`total_duration`과 같아진다 — `[10. mix]`가 붙일 나레이션 길이와 맞는다.

**클립 안에서 씬이 어디부터인가는 이제 계약이다** — "로컬 0초 = 씬 `start`, 0.6초는
전부 꼬리"(specs/05 `[7. motion]`, ADR-0024). 겹침을 앞뒤로 나누는 중앙 정렬은 채택하지
않았다. 되돌린다면 바뀌는 것은 여기 상수 하나가 아니라 기하 전체이고, 판정 근거는
완성 영상이다 (ADR-0024 "되돌릴 조건").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from ..schemas.scenes import BEATS

#: specs/05 `[7. motion]` "클립 길이 = 씬 길이 + 디졸브 겹침 0.6초"가 정한 겹침.
#: specs/03의 "0.4~0.6초" 중 이 값만이 클립 꼬리를 남기지 않는다.
DISSOLVE_SECONDS = 0.6

DISSOLVE = "dissolve"
HARD_CUT = "cut"

#: specs/03 전환 규칙 — 이 비트로 **진입**하는 자리에는 디졸브를 걸지 않는다.
#:
#: `turning_point`는 스펙이 직접 지시한다("디졸브 없이 컷"). `hook_twist`·`dilemma_peak`은
#: "하드컷은 … 진입 시에만 허용"이라는 문장에서 왔다. 그 문장을 허가가 아니라 지시로
#: 읽었다 — 허가로만 읽으면 파이프라인의 어떤 룰도 그 두 비트에서 하드컷을 만들지 않아
#: 문장이 죽는다. 세 비트 모두 서사가 끊기는 자리라는 점도 같다.
#:
#: 이 독법이 스펙 저자의 의도와 다르면 **여기 튜플에서 두 값을 빼면 된다.** 그 경우
#: 전 구간이 디졸브가 되고 하드컷은 `turning_point` 한 번뿐이다.
HARD_CUT_BEATS: tuple[str, ...] = ("turning_point", "hook_twist", "dilemma_peak")

#: 씬이 이어지는지 판정하는 허용 오차(초). `[3]`은 빈틈 없이 이어 붙이므로(tts/sync.py)
#: 여기 걸리는 것은 계산 오류이지 반올림이 아니다.
CONTINUITY_TOLERANCE = 0.001


class TimelineError(Exception):
    """씬 계약으로 타임라인을 만들 수 없음."""


def transition_into(beat: str) -> str:
    """씬 `beat`으로 **진입**하는 전환. specs/03 전환 규칙.

    룰 테이블에 없는 비트는 기본값으로 흘려보내지 않고 멈춘다. 스펙 02의 enum이 먼저
    막지만, 비트가 늘어나면 전환 규칙도 함께 늘어야 한다 (ADR-0001).
    """
    if beat not in BEATS:
        raise TimelineError(
            f"스펙 02·03 룰 테이블에 없는 비트 '{beat}'. 전환을 임의로 정하지 않는다"
        )
    return HARD_CUT if beat in HARD_CUT_BEATS else DISSOLVE


@dataclass(frozen=True)
class Segment:
    """타임라인에 놓인 클립 하나."""

    scene_id: int
    beat: str
    #: 타임라인에서 이 클립이 시작하는 시각 = 씬의 `start`
    start: float
    #: 씬의 `end`. 클립은 여기서 `tail`만큼 더 간다
    end: float
    #: 이 씬으로 들어오는 전환. 첫 클립은 없다
    transition_in: str | None
    #: 이 씬에서 나가는 전환. 마지막 클립은 없다
    transition_out: str | None
    dissolve: float = DISSOLVE_SECONDS

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)

    @property
    def tail(self) -> float:
        """다음 클립과 겹치는 꼬리. 하드컷·마지막 클립은 0이다."""
        return self.dissolve if self.transition_out == DISSOLVE else 0.0

    @property
    def clip_length(self) -> float:
        """이 클립에서 실제로 쓰는 길이 (`trim`으로 자른 뒤)."""
        return round(self.duration + self.tail, 3)

    @property
    def clip_name(self) -> str:
        """`[7. motion]`의 산출물 이름 (specs/05 `clips/{scene_id}.mp4`)."""
        return f"{self.scene_id}.mp4"


@dataclass(frozen=True)
class Timeline:
    segments: tuple[Segment, ...]
    dissolve: float = DISSOLVE_SECONDS

    @property
    def total_duration(self) -> float:
        """조립 후 영상 길이. 마지막 씬의 `end`와 같다."""
        return self.segments[-1].end if self.segments else 0.0

    @property
    def cut_scene_ids(self) -> tuple[int, ...]:
        """하드컷으로 진입하는 씬. `[10. mix]`의 차임 동기화 지점이다 (specs/04)."""
        return tuple(s.scene_id for s in self.segments if s.transition_in == HARD_CUT)

    @property
    def counts(self) -> dict[str, int]:
        kinds = [s.transition_in for s in self.segments if s.transition_in]
        return {
            DISSOLVE: kinds.count(DISSOLVE),
            HARD_CUT: kinds.count(HARD_CUT),
        }


def build_timeline(
    scenes: Sequence[dict[str, Any]], *, dissolve: float = DISSOLVE_SECONDS
) -> Timeline:
    """`scenes.timed.json`의 씬 배열 → 전환 계획.

    씬은 빈틈 없이 이어져야 한다 (tts/sync.py). 구멍이 있으면 그 구간을 덮을 클립이
    없고, 겹치면 클립을 어디에 놓을지가 정해지지 않는다. 조용히 밀어 두면 그 뒤 전부가
    어긋나므로 여기서 멈춘다.
    """
    if not scenes:
        raise TimelineError("씬이 없다")
    if dissolve <= 0:
        raise TimelineError(f"디졸브 길이는 0보다 커야 한다: {dissolve}")

    transitions: list[str | None] = [None]
    for scene in scenes[1:]:
        transitions.append(transition_into(scene["beat"]))

    segments: list[Segment] = []
    for index, scene in enumerate(scenes):
        start = float(scene["start"])
        end = float(scene["end"])
        if end <= start:
            raise TimelineError(
                f"scenes/{scene['scene_id']}: start({start}) >= end({end})"
            )
        if index and abs(start - float(scenes[index - 1]["end"])) > CONTINUITY_TOLERANCE:
            raise TimelineError(
                f"scenes/{scene['scene_id']}: 앞 씬의 end({scenes[index - 1]['end']})와 "
                f"start({start})가 이어지지 않는다. 클립이 덮지 못하는 구간이 생긴다"
            )
        segments.append(
            Segment(
                scene_id=scene["scene_id"],
                beat=scene["beat"],
                start=start,
                end=end,
                transition_in=transitions[index],
                transition_out=(
                    transitions[index + 1] if index + 1 < len(transitions) else None
                ),
                dissolve=dissolve,
            )
        )

    return Timeline(segments=tuple(segments), dissolve=dissolve)
