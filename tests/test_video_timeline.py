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
    CLIPS_DIR,
    DISSOLVE,
    DISSOLVE_SECONDS,
    HARD_CUT,
    TimelineError,
    build_timeline,
    extend_with_ending,
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


def test_hoover_cuts_come_from_the_scene_contract():
    """후버댐 픽스처는 전환이 씬마다 채워져 있다 (ADR-0033 §3).

    비트 기본값 표가 아니라 씬 계약의 `transition`이 하드컷의 출처다. 피사 편은
    아직 그 필드가 없는 옛 대본이라 위 테스트가 기본값 경로를 그대로 지킨다.
    """
    document = timed_document(HOOVER)
    timeline = build_timeline(document["scenes"])
    chosen = tuple(
        s["scene_id"] for s in document["scenes"][1:] if s.get("transition") == "hard_cut"
    )

    assert chosen, "이 편은 전환을 고른 대본이다"
    assert timeline.cut_scene_ids == chosen


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


# --- 엔딩 실사 컷 (ADR-0055) --------------------------------------------------
#
# 여기서 지키는 것은 연출이 아니라 **기하**다. 엔딩을 잘못 붙이면 이미 렌더된 씬 클립을
# 다시 만들어야 하거나 xfade가 없는 프레임을 요구한다.


def two_scenes():
    return build_timeline(
        [
            scene(1, "hook_fact", 0.0, 4.0),
            scene(2, "context", 4.0, 7.0, transition=DISSOLVE),
        ]
    )


def test_no_ending_leaves_the_timeline_untouched():
    """D-3 — 선택적 입력의 부재는 경고가 아니고, 같은 객체가 그대로 나간다."""
    timeline = two_scenes()

    assert extend_with_ending(timeline, [], source_dir="ending") is timeline


def test_the_last_scene_clip_never_needs_a_rerender():
    """**이 결정의 급소다.** 하드컷 진입이라 마지막 씬의 꼬리가 0으로 남는다 —
    디졸브로 붙였다면 `[7]`이 렌더하지 않은 0.6초를 요구했을 것이다."""
    before = two_scenes().segments[-1]
    after = extend_with_ending(two_scenes(), [2.4], source_dir="ending").segments[1]

    assert after.transition_out == HARD_CUT
    assert after.tail == 0.0
    assert after.clip_length == before.clip_length


def test_ending_cuts_carry_their_own_tails():
    full = extend_with_ending(two_scenes(), [2.4, 2.4, 2.4], source_dir="ending")
    cuts = full.ending_segments

    assert [c.transition_in for c in cuts] == [HARD_CUT, DISSOLVE, DISSOLVE]
    assert [c.clip_length for c in cuts] == [3.0, 3.0, 2.4]


def test_ending_starts_where_the_narration_ends():
    full = extend_with_ending(two_scenes(), [2.4, 2.4], source_dir="ending")

    assert full.scene_duration == 7.0
    assert full.ending_segments[0].start == 7.0
    assert full.total_duration == 11.8


def test_each_dissolve_exactly_consumes_the_previous_tail():
    """timeline.py의 불변식 — 누적 길이 = end + 남은 꼬리. 깨지면 xfade가 언다."""
    full = extend_with_ending(two_scenes(), [2.4, 2.4, 2.4], source_dir="ending")

    for previous, current in zip(full.segments, full.segments[1:]):
        if current.transition_in != DISSOLVE:
            continue
        assert previous.end + previous.tail >= current.start + current.dissolve - 1e-9


def test_ending_clips_come_from_their_own_directory():
    full = extend_with_ending(two_scenes(), [2.4], source_dir="ending")

    assert full.scene_segments[0].source_dir == CLIPS_DIR
    assert full.ending_segments[0].clip_path == "ending/1.mp4"


def test_ending_is_not_a_scene():
    """자막 큐도 thump 지점도 전환 지표도 엔딩을 세지 않는다."""
    scenes = [
        scene(1, "hook_fact", 0.0, 4.0),
        scene(2, "context", 4.0, 7.0, transition=HARD_CUT),
    ]
    full = extend_with_ending(build_timeline(scenes), [2.4, 2.4], source_dir="ending")

    assert len(full.scene_segments) == 2
    assert full.cut_scene_ids == (2,)
    assert full.counts[HARD_CUT] == 1  # 엔딩 진입의 하드컷은 안 센다
    assert full.counts[DISSOLVE] == 0  # 엔딩 컷 사이의 디졸브도 안 센다


def test_zero_length_ending_cut_is_refused():
    with pytest.raises(TimelineError, match="0보다 커야"):
        extend_with_ending(two_scenes(), [0.0], source_dir="ending")
