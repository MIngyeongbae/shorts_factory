"""전환 계획 (specs/03 "전환", specs/05 `[7]`의 클립 길이 계약).

확인 대상:
- 씬이 고른 전환이 이기고, 비었을 때만 기본값으로 떨어지는가 (ADR-0033 §3)
- 클립을 놓는 자리와 자르는 길이가 씬 시각에서만 나오는가 (누적합이 아니라)
- 씬 사이에 구멍이 있으면 조용히 밀지 않고 멈추는가
"""

import pytest
from timed_fixtures import HOOVER, PISA, timed_document

from shorts_factory.schemas import vocab
from shorts_factory.schemas.scenes import BEATS
from shorts_factory.video.timeline import (
    DISSOLVE,
    DISSOLVE_SECONDS,
    HARD_CUT,
    TimelineError,
    build_timeline,
    transition_into,
)


def scene(scene_id, beat, start, end, text="가나다.", **extra):
    return {
        "scene_id": scene_id, "beat": beat, "text": text,
        "start": start, "end": end, **extra,
    }


# --- 전환은 씬이 고른다 (ADR-0033 §3) ----------------------------------------


@pytest.mark.parametrize("chosen", [DISSOLVE, HARD_CUT])
def test_scene_transition_wins(chosen):
    assert transition_into(scene(2, "context", 1.0, 2.0, transition=chosen)) == chosen


@pytest.mark.parametrize("beat", BEATS)
def test_empty_transition_falls_back_to_a_real_value(beat):
    """비트마다 떨어질 기본값이 있어야 한다 — 옛 대본에는 이 필드가 없다."""
    assert transition_into(scene(2, beat, 1.0, 2.0)) in (DISSOLVE, HARD_CUT)


@pytest.mark.parametrize("beat", BEATS)
def test_fallback_matches_the_default_table(beat):
    assert transition_into(scene(2, beat, 1.0, 2.0)) == vocab.default_transition(beat)


def test_unknown_beat_does_not_stop_the_timeline():
    """어휘가 늘어도 [9]는 돈다 (단계 독립 D-5)."""
    assert transition_into(scene(2, "새_비트", 1.0, 2.0)) in (DISSOLVE, HARD_CUT)


def test_transition_outside_the_vocabulary_falls_back():
    assert transition_into(scene(2, "context", 1.0, 2.0, transition="wipe")) == DISSOLVE


def test_dissolve_length_matches_the_clip_contract():
    """specs/05 `[7]`: "클립 길이 = 씬 길이 + 디졸브 겹침 0.6초".

    이 값에서만 클립 꼬리가 남김 없이 쓰인다 (ADR-0024). 어휘 파일이 출처다.
    """
    assert DISSOLVE_SECONDS == 0.6


# --- 기하 --------------------------------------------------------------------


def test_first_clip_has_no_incoming_transition():
    timeline = build_timeline([scene(1, "hook_fact", 0.0, 4.0)])
    assert timeline.segments[0].transition_in is None
    assert timeline.segments[0].transition_out is None


def test_clip_is_trimmed_to_the_scene_plus_one_overlap():
    timeline = build_timeline(
        [scene(1, "hook_fact", 0.0, 4.0), scene(2, "context", 4.0, 7.0)]
    )
    first, second = timeline.segments

    assert first.clip_length == 4.6, "디졸브가 뒤따르면 겹침 0.6초까지 쓴다"
    assert second.clip_length == 3.0, "마지막 클립의 꼬리는 쓰지 않는다"


def test_clip_before_a_hard_cut_drops_its_tail():
    timeline = build_timeline(
        [scene(1, "hook_fact", 0.0, 4.0), scene(2, "turning_point", 4.0, 7.0)]
    )
    assert timeline.segments[0].clip_length == 4.0
    assert timeline.segments[1].transition_in == HARD_CUT


def test_clips_are_placed_at_their_scene_start():
    scenes = timed_document(PISA)["scenes"]
    timeline = build_timeline(scenes)

    assert [s.start for s in timeline.segments] == [s["start"] for s in scenes]


def test_timeline_length_equals_the_narration_length():
    """마지막 클립의 꼬리를 쓰지 않으므로 `[10. mix]`가 붙일 오디오와 길이가 같다."""
    document = timed_document(PISA)
    timeline = build_timeline(document["scenes"])

    assert timeline.total_duration == document["total_duration"]


def test_folding_the_clips_never_loses_the_absolute_time_axis():
    """xfade/concat을 접어 나가도 누적 길이가 `end + 꼬리`라는 불변식.

    이게 성립해야 `xfade`의 offset을 씬 `start`로 줄 수 있다 — 클립 길이의 누적합으로
    계산하면 클립 하나가 계약보다 짧을 때 그 뒤 전부의 자막 싱크가 밀린다.
    """
    timeline = build_timeline(timed_document(HOOVER)["scenes"])
    segments = timeline.segments

    accumulated = segments[0].clip_length
    for segment in segments[1:]:
        if segment.transition_in == DISSOLVE:
            assert segment.start <= accumulated - timeline.dissolve + 1e-9, (
                "xfade offset이 앞 스트림의 길이를 넘으면 FFmpeg가 거부한다"
            )
            accumulated = round(accumulated + segment.clip_length - timeline.dissolve, 3)
        else:
            assert segment.start == pytest.approx(accumulated, abs=1e-9)
            accumulated = round(accumulated + segment.clip_length, 3)
        assert accumulated == pytest.approx(segment.end + segment.tail, abs=1e-9)

    assert accumulated == pytest.approx(timeline.total_duration, abs=1e-9)


# --- 실물 대본 ----------------------------------------------------------------


def test_pisa_cuts_land_on_the_three_rule_beats():
    document = timed_document(PISA)
    timeline = build_timeline(document["scenes"])
    beats = {s["scene_id"]: s["beat"] for s in document["scenes"]}

    assert timeline.cut_scene_ids == (3, 13, 14)
    assert [beats[i] for i in timeline.cut_scene_ids] == [
        "hook_twist", "dilemma_peak", "turning_point",
    ]


def test_hoover_cuts_land_on_the_three_rule_beats():
    document = timed_document(HOOVER)
    timeline = build_timeline(document["scenes"])

    assert timeline.cut_scene_ids == (2, 3, 14, 15)


def test_counts_cover_every_junction():
    timeline = build_timeline(timed_document(PISA)["scenes"])
    counts = timeline.counts

    assert counts[DISSOLVE] + counts[HARD_CUT] == len(timeline.segments) - 1
    assert counts[HARD_CUT] == 3


def test_clip_name_follows_the_stage_contract():
    """specs/05: `[7. motion]` → `clips/{scene_id}.mp4`."""
    timeline = build_timeline(timed_document(PISA)["scenes"])
    assert [s.clip_name for s in timeline.segments[:3]] == ["1.mp4", "2.mp4", "3.mp4"]


# --- 거부 --------------------------------------------------------------------


def test_gap_between_scenes_is_refused():
    with pytest.raises(TimelineError, match="이어지지 않는다"):
        build_timeline(
            [scene(1, "hook_fact", 0.0, 4.0), scene(2, "context", 4.5, 7.0)]
        )


def test_overlapping_scenes_are_refused():
    with pytest.raises(TimelineError, match="이어지지 않는다"):
        build_timeline(
            [scene(1, "hook_fact", 0.0, 4.0), scene(2, "context", 3.5, 7.0)]
        )


def test_zero_length_scene_is_refused():
    with pytest.raises(TimelineError, match="start"):
        build_timeline([scene(1, "hook_fact", 2.0, 2.0)])


def test_empty_scene_list_is_refused():
    with pytest.raises(TimelineError, match="씬이 없다"):
        build_timeline([])


def test_unknown_beat_keeps_building_the_timeline():
    """어휘 밖의 비트가 와도 전환은 기본값으로 떨어진다 (단계 독립 D-5).

    예전에는 여기서 멈췄다. 연출이 씬 계약에서 오게 된 뒤로(ADR-0033 §3) 비트는
    전환을 정하지 않으므로, 모르는 라벨 하나로 `[9]`를 세울 이유가 없다.
    """
    assert "montage" not in BEATS
    timeline = build_timeline(
        [scene(1, "hook_fact", 0.0, 4.0), scene(2, "montage", 4.0, 7.0)]
    )
    assert timeline.segments[1].transition_in in (DISSOLVE, HARD_CUT)
