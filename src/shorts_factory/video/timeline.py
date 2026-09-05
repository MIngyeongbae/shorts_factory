"""전환 계획 — 씬 계약을 타임라인 배치로 옮긴다. specs/03 "전환".

**전환을 고르는 것은 `[1s. sceneplan]`이다** (ADR-0033 §3). 이 모듈은 씬 계약의
`transition`을 읽고, 비어 있을 때만 `beat-defaults.json`의 기본값으로 떨어진다.

읽는 곳은 그 언어의 `scenes.timed.{lang}.json` 하나다 (ADR-0020). 배치가 시각에 걸려 있어
`start`/`end`를 같은 파일에서 읽는다.

## 디졸브 길이를 0.6초로 고정하는 근거

**클립 계약이 그 값을 이미 정했다** — specs/05 `[7. videogen]`: "클립 길이 = 씬 길이 +
디졸브 겹침 0.6초". 더 짧게 두면 클립마다 그 차이만큼이 쓰이지 않고 남는다. 값을
고르는 것이 아니라 이미 고른 값을 읽는 것이고, 그래서 자유화 대상이 아니다 (ADR-0024).

## 배치 (기하)

클립 `i`의 로컬 0초가 씬 `i`의 `start`다. 클립은 씬 길이보다 0.6초 길고, 그 꼬리가
다음 씬으로 넘어가는 겹침이다 (위 `[7]` 계약의 직독). 그래서

    - 디졸브: 다음 씬의 `start`에서 시작해 0.6초 — 앞 클립의 꼬리를 정확히 소진한다
    - 하드컷: 앞 클립을 씬 `end`에서 자르고 다음 클립을 그 자리에 붙인다 (꼬리는 버린다)

두 경우 모두 **클립 `k`를 붙인 뒤의 누적 길이 = `end_k` + (그 클립의 남은 꼬리)**라는
불변식이 성립한다. 마지막 클립은 꼬리를 쓰지 않으므로 타임라인 총 길이는
`total_duration`과 같아진다 — `[10. mix]`가 붙일 나레이션 길이와 맞는다.

**클립 안에서 씬이 어디부터인가는 이제 계약이다** — "로컬 0초 = 씬 `start`, 0.6초는
전부 꼬리"(specs/05 `[7. videogen]`, ADR-0024). 겹침을 앞뒤로 나누는 중앙 정렬은 채택하지
않았다. 되돌린다면 바뀌는 것은 여기 상수 하나가 아니라 기하 전체이고, 판정 근거는
완성 영상이다 (ADR-0024 "되돌릴 조건").
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Sequence

from ..schemas import vocab

#: 씬 클립이 놓이는 자리 (`[7]`의 산출물, specs/05). `[9]`가 같은 값을 다시 선언하지
#: 않도록 여기에 한 번만 둔다 — `clip_name`이 이미 이 배치를 알고 있다.
CLIPS_DIR = "clips"

#: 전환 어휘 (`specs/schema/vocab.json`). 값 목록을 선언하지 않고 이름으로 가리킨다.
DISSOLVE = vocab.require("transition", "dissolve")
HARD_CUT = vocab.require("transition", "hard_cut")

#: specs/05 `[7. videogen]` "클립 길이 = 씬 길이 + 디졸브 겹침 0.6초"가 정한 겹침.
DISSOLVE_SECONDS: float = vocab.meta("transition")[DISSOLVE]["seconds"]

#: 씬이 이어지는지 판정하는 허용 오차(초). `[3]`은 빈틈 없이 이어 붙이므로(tts/sync.py)
#: 여기 걸리는 것은 계산 오류이지 반올림이 아니다.
CONTINUITY_TOLERANCE = 0.001


class TimelineError(Exception):
    """씬 계약으로 타임라인을 만들 수 없음."""


def transition_into(scene: dict[str, Any]) -> str:
    """이 씬으로 **진입**하는 전환. 씬이 고른 값이 먼저다 (ADR-0033 §3).

    비어 있으면 `beat-defaults.json`의 기본값으로 떨어진다. **멈추지 않는다** —
    선택 필드의 부재는 경고가 아니고(단계 독립 D-3), 모르는 비트에도 폴백이 있다.
    """
    chosen = scene.get("transition")
    if chosen in (DISSOLVE, HARD_CUT):
        return chosen
    return vocab.default_transition(scene.get("beat", ""))


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
    #: 이 클립 파일이 있는 run 하위 디렉터리. 씬은 `[7]`의 `clips/`, 엔딩 실사 컷은
    #: `[8]`의 `ending/`이다 (ADR-0055).
    source_dir: str = CLIPS_DIR
    #: 씬을 담은 구간인가. **엔딩 실사 컷은 씬이 아니다** — 자막 큐도, 대조할 실측
    #: 시각도, thump 지점도 없다 (ADR-0055). 그 셋을 세는 자리가 이 플래그를 본다.
    is_scene: bool = True

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
        """산출물 파일 이름 (specs/05 `clips/{scene_id}.mp4`, `ending/{n}.mp4`)."""
        return f"{self.scene_id}.mp4"

    @property
    def clip_path(self) -> str:
        """run 디렉터리 기준 경로. `[9]`가 여는 자리다."""
        return f"{self.source_dir}/{self.clip_name}"


@dataclass(frozen=True)
class Timeline:
    segments: tuple[Segment, ...]
    dissolve: float = DISSOLVE_SECONDS

    @property
    def total_duration(self) -> float:
        """조립 후 영상 길이. 마지막 구간의 `end`와 같다."""
        return self.segments[-1].end if self.segments else 0.0

    @property
    def scene_segments(self) -> tuple[Segment, ...]:
        """씬을 담은 구간만. 자막·싱크 검증이 대조하는 집합이다."""
        return tuple(s for s in self.segments if s.is_scene)

    @property
    def scene_duration(self) -> float:
        """씬 구간의 끝 = 나레이션 길이. 엔딩 꼬리는 여기 안 든다 (ADR-0055)."""
        scenes = self.scene_segments
        return scenes[-1].end if scenes else 0.0

    @property
    def ending_segments(self) -> tuple[Segment, ...]:
        """엔딩 실사 컷 구간."""
        return tuple(s for s in self.segments if not s.is_scene)

    @property
    def cut_scene_ids(self) -> tuple[int, ...]:
        """하드컷으로 진입하는 씬. `[10. mix]`의 thump 동기화 지점이다 (specs/04).

        **엔딩 진입의 하드컷은 세지 않는다.** 그것은 연출 선택이 아니라 기하가 강제한
        고정 마감이고(ADR-0055), 나레이션이 끝난 뒤라 맞출 소리도 없다.
        """
        return tuple(
            s.scene_id for s in self.scene_segments if s.transition_in == HARD_CUT
        )

    @property
    def counts(self) -> dict[str, int]:
        """씬 사이 전환의 수. ADR-0033 되돌릴 조건의 관측 지표라 **씬만 센다** —
        엔딩 컷의 전환은 편마다 같은 고정값이므로 섞으면 지표가 흐려진다.
        """
        kinds = [s.transition_in for s in self.scene_segments if s.transition_in]
        return {
            DISSOLVE: kinds.count(DISSOLVE),
            HARD_CUT: kinds.count(HARD_CUT),
        }


def build_timeline(
    scenes: Sequence[dict[str, Any]], *, dissolve: float = DISSOLVE_SECONDS
) -> Timeline:
    """`scenes.timed.{lang}.json`의 씬 배열 → 전환 계획.

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
        transitions.append(transition_into(scene))

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


#: 엔딩 구간에 붙이는 비트 라벨. 어휘 값이 아니라 **기록 라벨이다** — 씬이 고를 수 없고
#: (`[3s]`는 엔딩을 모른다) `[8]`의 존재가 고른다. `[7]`의 `veo`·`info_still`과 같은 지위.
ENDING_BEAT = "ending"


def extend_with_ending(
    timeline: Timeline,
    ending_clips: Sequence[float],
    *,
    source_dir: str,
    clip_ids: Sequence[int] | None = None,
) -> Timeline:
    """씬 타임라인 뒤에 엔딩 실사 컷을 잇는다 (ADR-0055). `ending_clips`는 장당 표시 초.

    `clip_ids`는 그 컷이 여는 **파일의 id**다 (ADR-0092) — `ending.json`의 풀에서 이
    언어가 고른 사진들이라 자리 번호와 다르다. 생략하면 자리를 그대로 쓴다.

    ## 왜 하드컷으로 진입하는가 — 기하가 정한다

    마지막 씬 클립은 `transition_out`이 없어 **꼬리 0.6초가 렌더되어 있지 않다**
    (위 「배치」의 불변식). 디졸브로 진입하면 xfade가 소진할 프레임이 앞 클립에 없다.
    꼬리를 만들려면 `[7]`을 고쳐야 하는데 그건 D-4(새 단계는 기존 단계를 수정하지
    않는다) 위반이고, 하드컷은 연출로도 맞는 자리다 (specs/00 "서사가 끊기는 자리").

    하드컷의 `tail`도 0이라 **이미 렌더된 씬 클립을 다시 만들 필요가 없다.**

    ## 엔딩 컷 사이는 디졸브다

    엔딩 클립은 `[8]`이 자기 꼬리를 포함해 렌더하므로(`clip_length`) 씬 사이와 같은
    불변식이 그대로 선다 — 마지막 컷만 꼬리를 쓰지 않고, 타임라인 총 길이는
    `씬 길이 + Σ표시 초`가 된다.
    """
    if not ending_clips:
        return timeline
    if not timeline.segments:
        raise TimelineError("씬이 없는 타임라인에는 엔딩을 붙일 수 없다")
    if any(seconds <= 0 for seconds in ending_clips):
        raise TimelineError(f"엔딩 컷 길이는 0보다 커야 한다: {list(ending_clips)}")

    segments = list(timeline.segments)
    # 마지막 씬은 이제 엔딩으로 나간다. 하드컷이라 tail은 그대로 0이다 — 클립 파일을
    # 다시 만들지 않아도 되는 것이 이 선택의 값이다.
    segments[-1] = replace(segments[-1], transition_out=HARD_CUT)

    cursor = timeline.total_duration
    for index, seconds in enumerate(ending_clips, start=1):
        last = index == len(ending_clips)
        # 파일을 가리키는 것은 **자리(index)가 아니라 풀에서 고른 컷 id**다 (ADR-0092) —
        # 언어마다 다른 사진을 다른 순서로 쓰므로 둘이 갈린다. `clip_ids`가 없으면
        # 자리를 그대로 쓴다 (배분 없이 쓰던 옛 `ending.json`).
        clip_id = clip_ids[index - 1] if clip_ids is not None else index
        segments.append(
            Segment(
                scene_id=clip_id,
                beat=ENDING_BEAT,
                start=cursor,
                end=round(cursor + seconds, 3),
                transition_in=HARD_CUT if index == 1 else DISSOLVE,
                transition_out=None if last else DISSOLVE,
                dissolve=timeline.dissolve,
                source_dir=source_dir,
                is_scene=False,
            )
        )
        cursor = round(cursor + seconds, 3)

    return Timeline(segments=tuple(segments), dissolve=timeline.dissolve)
