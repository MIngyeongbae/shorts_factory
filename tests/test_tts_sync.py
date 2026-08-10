"""문자 정렬 → 씬 경계 (ADR-0013). `[3. tts+sync]`의 판단이 전부 여기 있다.

확인 대상:
- 줄 안의 **마지막 문장부호**가 그 줄의 끝이다 (한 줄에 문장이 1~3개여도 씬은 하나)
- 줄 중간의 소수점("1.07")은 줄 끝 후보가 되지 못한다
- 정렬이 보낸 대본과 다르면 조용히 밀지 않고 실패한다
- atempo 보정은 1/tempo 배, 씬은 빈틈 없이 이어진다
- 실측-추정 오차 ±1.5초 초과 시 경고 (specs/05)
"""

import pytest

from conftest import PISA, load_script
from shorts_factory.tts.base import (
    API_CHARACTERS,
    API_ENDS,
    API_STARTS,
    Alignment,
    TTSError,
)
from shorts_factory.tts.fake import fake_alignment
from shorts_factory.tts.sync import (
    SyncError,
    character_spans,
    drift_warnings,
    narration_text,
    scale,
    scene_boundaries,
)


def alignment_of(text: str, **kwargs) -> Alignment:
    return fake_alignment(text, speed=10.0, **kwargs)


# --- Alignment 계약 -----------------------------------------------------------


def test_alignment_rejects_ragged_arrays():
    with pytest.raises(TTSError, match="길이가 다르다"):
        Alignment(characters=["a", "b"], starts=[0.0, 0.1], ends=[0.1])


def test_alignment_rejects_empty():
    with pytest.raises(TTSError, match="비어 있다"):
        Alignment(characters=[], starts=[], ends=[])


def test_alignment_from_api_uses_elevenlabs_key_names():
    payload = {
        API_CHARACTERS: ["가", "."],
        API_STARTS: [0.0, 0.2],
        API_ENDS: [0.2, 0.3],
    }
    alignment = Alignment.from_api(payload)
    assert alignment.text == "가."
    assert alignment.duration == 0.3


def test_alignment_from_api_reports_missing_keys():
    with pytest.raises(TTSError, match="키가 없다"):
        Alignment.from_api({"characters": ["가"]})


# --- 문자 구간 ----------------------------------------------------------------


def test_character_spans_cover_each_scene_exactly():
    texts = ["첫 줄입니다.", "둘째 줄입니다."]
    spans = character_spans(alignment_of(narration_text(texts)), texts)

    assert spans == [(0, 7), (8, 16)]
    joined = narration_text(texts)
    assert [joined[lo:hi] for lo, hi in spans] == texts


def test_character_spans_reject_alignment_that_is_not_the_submitted_script():
    texts = ["여덟 줄입니다."]
    normalized = alignment_of("여덟 줄 입니다.")  # 정규화된 정렬을 잘못 넘긴 경우

    with pytest.raises(SyncError, match="정렬이 보낸 대본과 다르다"):
        character_spans(normalized, texts)


def test_character_spans_mismatch_message_points_at_the_first_bad_index():
    texts = ["가나다라마."]
    with pytest.raises(SyncError) as exc:
        character_spans(alignment_of("가나다XX마."), texts)

    assert "3번째 문자부터 어긋남" in str(exc.value)
    assert "normalized_alignment" in str(exc.value)


# --- 씬 경계 ------------------------------------------------------------------


def test_scene_end_is_the_last_sentence_end_in_the_line():
    """ADR-0013: 한 줄에 문장이 3개여도 씬 경계는 줄의 마지막 문장 끝 하나뿐이다."""
    texts = [
        "문제는 그게 바로 안 보인다는 겁니다. 멀쩡해 보이다가. 환장할 노릇이죠.",
        "다음 줄입니다.",
    ]
    alignment = alignment_of(narration_text(texts), pause=0.5)
    boundaries, warnings = scene_boundaries(alignment, texts)

    assert warnings == []
    assert len(boundaries) == 2
    # 첫 씬의 끝 = 첫 줄 마지막 '.'의 끝 시각
    first_line_end = alignment.ends[len(texts[0]) - 1]
    assert boundaries[0][1] == pytest.approx(first_line_end)


def test_decimal_point_inside_a_line_is_not_a_line_end():
    texts = ["1993년 기울기는 5도 34분 7초, 안전율은 1.07.", "다음 줄입니다."]
    alignment = alignment_of(narration_text(texts), pause=0.4)
    boundaries, _ = scene_boundaries(alignment, texts)

    assert boundaries[0][1] == pytest.approx(alignment.ends[len(texts[0]) - 1])
    # 소수점('1.07'의 '.')에서 잘렸다면 이 값보다 한참 작았을 것이다
    assert boundaries[0][1] > alignment.ends[len(texts[0]) - 5]


def test_joiner_time_belongs_to_the_next_scene():
    """줄 사이 공백의 시간은 앞 씬의 end에 들어가지 않는다."""
    texts = ["앞 줄입니다.", "뒷 줄입니다."]
    alignment = alignment_of(narration_text(texts), gap=1.0)
    boundaries, _ = scene_boundaries(alignment, texts)

    assert boundaries[0][1] == pytest.approx(alignment.ends[len(texts[0]) - 1])
    assert boundaries[1][0] == boundaries[0][1]


def test_scenes_are_contiguous_and_start_at_zero():
    texts = [s["text"] for s in load_script(PISA)["scenes"]]
    boundaries, warnings = scene_boundaries(alignment_of(narration_text(texts)), texts)

    assert warnings == []
    assert boundaries[0][0] == 0.0
    for (_, end), (start, _) in zip(boundaries, boundaries[1:]):
        assert start == end


def test_line_without_sentence_punctuation_warns_and_falls_back():
    texts = ["문장부호가 없는 줄", "다음 줄입니다."]
    boundaries, warnings = scene_boundaries(alignment_of(narration_text(texts)), texts)

    assert len(warnings) == 1
    assert "문장부호로 끝나지 않아" in warnings[0]
    assert boundaries[0][1] > 0


def test_zero_length_scene_is_an_error():
    """정렬 시각이 멈춰 있으면 씬 길이가 0이 된다 — 조용히 넘기지 않는다."""
    texts = ["가.", "나."]
    frozen = Alignment(
        characters=list(narration_text(texts)),
        starts=[0.0] * 5,
        ends=[1.0, 1.0, 1.0, 1.0, 1.0],
    )
    with pytest.raises(SyncError, match=r"scenes/2"):
        scene_boundaries(frozen, texts)


def test_empty_scene_list_is_an_error():
    with pytest.raises(SyncError, match="씬이 없다"):
        character_spans(alignment_of("가."), [])


# --- 배속 보정 ----------------------------------------------------------------


def test_scale_applies_one_over_tempo():
    """specs/05: atempo 1.1 적용 후 타임스탬프도 1/1.1 스케일 보정."""
    scaled = scale([(0.0, 11.0), (11.0, 22.0)], 1.0 / 1.1)
    assert scaled == [(0.0, 10.0), (10.0, 20.0)]


def test_scale_keeps_scenes_contiguous_despite_rounding():
    raw = [(0.0, 3.3333), (3.3333, 7.7777), (7.7777, 9.1111)]
    scaled = scale(raw, 1.0 / 1.1)

    assert scaled[0][0] == 0.0
    for (_, end), (start, _) in zip(scaled, scaled[1:]):
        assert start == end


# --- 실측 vs 추정 -------------------------------------------------------------


def test_no_warning_when_measurement_matches_the_estimate():
    scenes = [
        {"scene_id": 1, "est_start": 0.0, "est_end": 4.0},
        {"scene_id": 2, "est_start": 4.0, "est_end": 8.0},
    ]
    assert drift_warnings([(0.0, 4.5), (4.5, 8.9)], scenes) == []


def test_warning_when_a_scene_drifts_more_than_the_tolerance():
    scenes = [
        {"scene_id": 1, "est_start": 0.0, "est_end": 4.0},
        {"scene_id": 2, "est_start": 4.0, "est_end": 8.0},
    ]
    warnings = drift_warnings([(0.0, 5.6), (5.6, 9.0)], scenes)

    assert len(warnings) == 2, "오차는 뒤 씬으로 누적된다"
    assert "scenes/1" in warnings[0]
    assert "+1.60" in warnings[0]


def test_drift_tolerance_is_configurable():
    scenes = [{"scene_id": 1, "est_start": 0.0, "est_end": 4.0}]
    assert drift_warnings([(0.0, 5.6)], scenes, tolerance=2.0) == []
