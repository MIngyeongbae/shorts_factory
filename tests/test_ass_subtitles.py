"""ASS 자막 (specs/03 "자막 스타일", ADR-0002 레이어 B, ADR-0013 씬=큐).

한국어 자막은 이미지 생성이 아니라 후처리 합성이다 (ADR-0002). 그 합성 경로의 입력이
이 문서이고, 여기서 검증하는 것은 세 가지다.

- 씬 하나가 큐 하나로 나가는가 (ADR-0013)
- 시각이 `scenes.timed.json`과 같은가 (ADR-0020 — 출처는 그 파일 하나)
- 스타일이 스펙 03의 숫자(위치 72~82%, 외곽선 3px, 1줄 18자, 2줄 상한)와 맞는가
"""

import pytest
from timed_fixtures import HOOVER, PISA, timed_document

from shorts_factory.video.subtitles import (
    ALIGNMENT,
    FONT_SIZE,
    LINE_BREAK,
    MAX_LINE_CHARS,
    MAX_LINES,
    OUTLINE,
    PLAY_RES_X,
    PLAY_RES_Y,
    TEXT_WIDTH,
    SubtitleError,
    ass_timestamp,
    build_ass,
    check_overflow,
    escape_text,
    parse_ass,
    parse_timestamp,
    style_line,
    subtitle_band,
    wrap_text,
)


def scene(scene_id, text, start, end, beat="context"):
    return {
        "scene_id": scene_id, "beat": beat, "text": text,
        "start": start, "end": end,
    }


# --- 줄바꿈 (specs/03 "1줄 최대 18자, 2줄 초과 금지") -------------------------


def test_short_line_stays_on_one_line():
    assert wrap_text("탑 하나가 기울었습니다.") == ["탑 하나가 기울었습니다."]


def test_long_line_becomes_two():
    lines = wrap_text("가장 쉬운 방법은 기울어진 쪽 지반을 다지는 것이었습니다.")
    assert len(lines) == 2
    assert " ".join(lines) == "가장 쉬운 방법은 기울어진 쪽 지반을 다지는 것이었습니다."


def test_never_exceeds_two_lines():
    """3줄이 되면 자막 블록이 72~82% 밴드를 넘어 피사체 영역을 침범한다."""
    for slug in (PISA, HOOVER):
        for item in timed_document(slug)["scenes"]:
            assert len(wrap_text(item["text"])) <= MAX_LINES


def test_split_happens_at_whitespace_not_inside_a_word():
    text = "피사의 사탑은 무너지기 직전이었습니다 정말로"
    lines = wrap_text(text)

    assert " ".join(lines) == text, "어절 중간을 자르면 이어 붙인 결과가 달라진다"
    assert all(line in text for line in lines)


def test_split_is_balanced():
    lines = wrap_text("한 줄 두 줄 세 줄 네 줄 다섯 줄 여섯 줄 일곱 줄 여덟")
    assert abs(len(lines[0]) - len(lines[1])) <= 4


def test_text_without_spaces_is_split_in_the_middle():
    lines = wrap_text("가" * 30)
    assert lines == ["가" * 15, "가" * 15]


def test_line_within_the_limit_passes_quietly():
    check_overflow(1, ["스물두자까지들어가는한줄입니다"])


def test_line_over_the_limit_fails_instead_of_shrinking():
    """22자×2줄을 넘긴 큐는 스펙 01의 43자를 넘겼다는 뜻이라 1부 문제다."""
    with pytest.raises(SubtitleError, match="1부에서 고쳐야"):
        check_overflow(7, ["가" * 24, "나" * 20])


# --- 가로 폭 -----------------------------------------------------------------


def test_the_three_subtitle_values_fit_together():
    """40px·22자·2줄은 한 벌이다. 22자가 안전폭 안에 들어가야 이 조합이 성립한다."""
    assert FONT_SIZE * MAX_LINE_CHARS <= TEXT_WIDTH


def test_the_longest_cue_spec_01_allows_still_fits_two_lines():
    """스펙 01의 상한(줄당 43자)이 스펙 03의 22자×2줄 안에 들어간다.

    이게 성립하는 동안에는 폰트 축소 경로가 필요 없다. 깨지면 두 스펙이 다시 충돌한다.
    """
    assert 43 <= MAX_LINE_CHARS * MAX_LINES


def test_every_real_cue_fits_the_frame_width():
    for slug in (PISA, HOOVER):
        for cue in parse_ass(build_ass(timed_document(slug)["scenes"])[0]):
            assert FONT_SIZE * max(len(line) for line in cue.lines) <= TEXT_WIDTH


def test_no_cue_carries_a_font_override():
    """모든 큐가 같은 크기로 나온다 — 큐마다 글자 크기가 달라지지 않는다."""
    for slug in (PISA, HOOVER):
        assert r"{\fs" not in build_ass(timed_document(slug)["scenes"])[0]


def test_empty_text_is_refused():
    with pytest.raises(SubtitleError, match="빈 자막"):
        wrap_text("   ")


# --- 위치·스타일 (specs/03) ---------------------------------------------------


@pytest.mark.parametrize("lines", [1, 2])
def test_subtitle_block_sits_in_the_72_to_82_percent_band(lines):
    top, bottom = subtitle_band(lines)
    assert 0.72 <= top < bottom <= 0.82


def test_style_carries_the_spec_numbers():
    style = style_line()
    fields = style[len("Style: ") :].split(",")

    # 베이스가 흰 종이라 흰 글자는 외곽선만 남는다 (ADR-0023에서 뒤집었다).
    assert fields[3] == "&H00000000", "검정 본문"
    assert fields[5] == "&H00FFFFFF", "흰색 외곽선"
    assert fields[7] == "-1", "굵게"
    assert fields[16] == str(OUTLINE) == "3", "외곽선 3px"
    assert fields[18] == str(ALIGNMENT) == "2", "하단 중앙"


def test_play_res_matches_the_output_format():
    """specs/00 "9:16 세로, 1080×1920". PlayRes가 실제 해상도와 같아야 3px이 3px이다."""
    document, _ = build_ass([scene(1, "가나다.", 0.0, 1.0)])
    assert f"PlayResX: {PLAY_RES_X}" in document
    assert f"PlayResY: {PLAY_RES_Y}" in document
    assert (PLAY_RES_X, PLAY_RES_Y) == (1080, 1920)


def test_wrapping_is_ours_not_libass():
    """WrapStyle 2 = 자동 줄바꿈 없음. 18자 규칙을 libass에 맡기지 않는다."""
    document, _ = build_ass([scene(1, "가나다.", 0.0, 1.0)])
    assert "WrapStyle: 2" in document


# --- 시각 --------------------------------------------------------------------


@pytest.mark.parametrize(
    "seconds,stamp",
    [
        (0.0, "0:00:00.00"),
        (4.444, "0:00:04.44"),
        (97.437, "0:01:37.44"),
        (3661.5, "1:01:01.50"),
    ],
)
def test_timestamp_format(seconds, stamp):
    assert ass_timestamp(seconds) == stamp


#: ASS 시간 해상도(1/100초)의 절반 + 부동소수 여유. specs/00의 ±200ms 안에 넉넉히 든다.
CENTISECOND_ROUNDING = 0.0051


def test_timestamp_round_trip_stays_within_a_centisecond():
    for item in timed_document(PISA)["scenes"]:
        for value in (item["start"], item["end"]):
            drift = abs(parse_timestamp(ass_timestamp(value)) - value)
            assert drift <= CENTISECOND_ROUNDING


def test_negative_time_is_refused():
    with pytest.raises(SubtitleError, match="음수"):
        ass_timestamp(-0.1)


# --- 문서 --------------------------------------------------------------------


def test_one_scene_makes_one_cue():
    """ADR-0013: 씬 1개 = 자막 줄(큐) 1개. 씬을 다시 묶거나 쪼개지 않는다."""
    document, _ = build_ass(timed_document(PISA)["scenes"])
    assert len(parse_ass(document)) == 25


def test_cue_times_come_from_the_scene_contract():
    scenes = timed_document(HOOVER)["scenes"]
    cues = parse_ass(build_ass(scenes)[0])

    for item, cue in zip(scenes, cues):
        assert cue.start == pytest.approx(item["start"], abs=CENTISECOND_ROUNDING)
        assert cue.end == pytest.approx(item["end"], abs=CENTISECOND_ROUNDING)


def test_cue_text_is_the_script_text_unchanged():
    """2부는 대본 문장을 고치지 않는다 (ADR-0017). 줄만 나눈다."""
    scenes = timed_document(PISA)["scenes"]
    cues = parse_ass(build_ass(scenes)[0])

    for item, cue in zip(scenes, cues):
        assert cue.text == " ".join(item["text"].split())


def test_two_line_cues_use_the_ass_line_break():
    long_text = "가장 쉬운 방법은 기울어진 쪽 지반을 다지는 것이었습니다"
    document, _ = build_ass([scene(1, long_text, 0.0, 4.0)])
    dialogue = [ln for ln in document.splitlines() if ln.startswith("Dialogue:")][0]

    assert LINE_BREAK in dialogue


def test_real_scripts_produce_no_subtitle_warnings():
    """40px·22자로 정한 뒤 실물 대본 2편의 자막 경고가 0건이 됐다 (이전엔 피사 8·후버 1)."""
    for slug in (PISA, HOOVER):
        document, warnings = build_ass(timed_document(slug)["scenes"])
        assert document
        assert warnings == []


def test_braces_cannot_open_an_override_block():
    """`{`를 그대로 두면 libass가 태그로 읽어 자막이 통째로 사라진다."""
    assert escape_text("{빨강}") == r"\{빨강\}"
    document, _ = build_ass([scene(1, "{빨강} 표시", 0.0, 2.0)])
    assert r"\{빨강\}" in document
    assert parse_ass(document)[0].text == "{빨강} 표시"


def test_cues_do_not_overlap_on_screen():
    """씬은 빈틈 없이 이어진다. 반올림으로 두 줄이 겹치면 화면에 두 줄이 뜬다."""
    cues = parse_ass(build_ass(timed_document(HOOVER)["scenes"])[0])
    for before, after in zip(cues, cues[1:]):
        assert after.start >= before.end


def test_backwards_scene_is_refused():
    with pytest.raises(SubtitleError, match="start"):
        build_ass([scene(1, "가나다.", 4.0, 2.0)])


def test_scene_starting_before_the_previous_end_is_refused():
    with pytest.raises(SubtitleError, match="이르다"):
        build_ass([scene(1, "가.", 0.0, 4.0), scene(2, "나.", 3.0, 6.0)])


def test_empty_scene_list_is_refused():
    with pytest.raises(SubtitleError, match="씬이 없다"):
        build_ass([])
