"""채널 룩 계약 (specs/schema/channel-look.json, ADR-0093).

이 계약이 지키는 것은 **룩이 다른 것을 밟지 않는 것**이다. 그래서 검사가 값 자체보다
경계 쪽으로 기운다 — 엔딩 실사에 닿지 않는가, 자막보다 앞인가, 없는 언어가 정상인가.

값(필터 문자열)은 이 파일에도 코드에도 없다 — 계약에서 로드한다 (ADR-0034 §3).
"""

import pytest

from shorts_factory.schemas import vocab
from shorts_factory.schemas.timed_scenes import LANGUAGES, PRIMARY_LANGUAGE
from shorts_factory.video.ffmpeg import build_filter_graph, clip_filter
from shorts_factory.video.grade import grade_for, languages_with_grade
from shorts_factory.video.timeline import (
    CLIPS_DIR,
    Timeline,
    build_timeline,
    extend_with_ending,
)


def scenes(count=3):
    return [
        {
            "scene_id": i,
            "text": f"줄 {i}",
            "start": round((i - 1) * 3.0, 3),
            "end": round(i * 3.0, 3),
            "beat": "context",
            "transition": "dissolve",
        }
        for i in range(1, count + 1)
    ]


# --- 값의 출처 -----------------------------------------------------------------


def test_the_look_is_loaded_not_declared():
    """코드가 필터 문자열을 선언하지 않는다 (ADR-0034). 계약을 고치면 코드가 따라온다."""
    contract = vocab.CHANNEL_LOOK["meta"]["grade"]

    for lang in LANGUAGES:
        assert grade_for(lang) == contract[lang]


def test_every_language_has_an_entry():
    """빠진 언어가 있으면 그 채널만 조용히 룩이 없어진다 — 계약에 자리를 둔다."""
    contract = vocab.CHANNEL_LOOK["meta"]["grade"]

    assert set(LANGUAGES) <= {k for k in contract if not k.startswith("_")}


def test_an_unknown_language_has_no_look():
    """언어가 늘어도 조립이 죽지 않는다 (D-3)."""
    assert grade_for("xx") == ""


def test_the_primary_language_carries_no_look():
    """ko는 그레이드 없이 올라간 편이 쌓여 있어 사람이 뺐다 (ADR-0093 맥락 4).

    **부재는 오류가 아니다.** 되돌릴 조건 4에서 채우기로 하면 계약만 고친다.
    """
    assert grade_for(PRIMARY_LANGUAGE) == ""
    assert PRIMARY_LANGUAGE not in languages_with_grade()


def test_some_language_actually_carries_one():
    """전부 비면 이 결정이 아무것도 안 한 것이다 — 계약이 비워지는 것을 잡는다."""
    assert languages_with_grade()


# --- 거는 자리 -----------------------------------------------------------------


def test_the_look_rides_the_clip_not_the_final_stream():
    """클립별 필터라야 자막보다 앞이다 (ADR-0093 결정 3)."""
    graded = clip_filter(0, length=3.0, label="c0", grade="eq=saturation=1.04")
    plain = clip_filter(0, length=3.0, label="c0")

    assert "eq=saturation=1.04" in graded
    assert "eq=saturation=1.04" not in plain
    #: 규격을 맞춘 뒤, 8비트로 깎기 전이다.
    assert graded.index("scale=") < graded.index("eq=saturation")
    assert graded.index("eq=saturation") < graded.index("format=")


def test_the_subtitles_are_burned_after_the_look():
    """**흰 글자가 물들면 안 된다** — `ass`가 그래프의 마지막 단계다."""
    timeline = build_timeline(scenes())

    graph = build_filter_graph(
        timeline, subtitles="subtitles.ja.ass", grade="eq=saturation=1.04"
    )

    assert graph.rindex("eq=saturation=1.04") < graph.index("ass=filename=")


def test_an_empty_look_leaves_the_chain_untouched():
    """ko는 지금까지와 **완전히 같은 픽셀**이다 (ADR-0093 결과)."""
    timeline = build_timeline(scenes())

    assert build_filter_graph(
        timeline, subtitles="s.ass", grade=""
    ) == build_filter_graph(timeline, subtitles="s.ass")


# --- 엔딩 실사에 닿지 않는다 (ADR-0055) -----------------------------------------


def test_the_ending_photos_are_never_graded():
    """**선택이 아니라 제약이다** — 무수정 표시가 `cc-by-sa` 사진을 실을 근거이고
    색보정은 개작이다 (ADR-0055, ADR-0093 결정 3).
    """
    timeline = build_timeline(scenes())
    full = extend_with_ending(timeline, [2.4, 2.4], source_dir="ending")
    look = "eq=saturation=1.04"

    graph = build_filter_graph(full, subtitles="s.ass", grade=look)

    scene_labels = [
        f"[c{i}]" for i, seg in enumerate(full.segments) if seg.is_scene
    ]
    ending_labels = [
        f"[c{i}]" for i, seg in enumerate(full.segments) if not seg.is_scene
    ]
    assert ending_labels, "엔딩이 안 붙어 이 계약을 검사할 수 없다"

    for step in graph.split(";"):
        if any(step.endswith(label) for label in ending_labels):
            assert look not in step, f"엔딩 컷에 그레이드가 걸렸다: {step}"
        elif any(step.endswith(label) for label in scene_labels):
            assert look in step, f"씬에 그레이드가 안 걸렸다: {step}"


def test_a_run_without_an_ending_grades_every_clip():
    """엔딩이 없는 편(D-3)에서는 모든 입력이 씬이다."""
    timeline = build_timeline(scenes())
    look = "eq=saturation=1.04"

    graph = build_filter_graph(timeline, subtitles="s.ass", grade=look)

    assert graph.count(look) == len(timeline.segments)


# --- 세기의 상한 (ADR-0093 결정 2) ----------------------------------------------


@pytest.mark.parametrize("lang", LANGUAGES)
def test_the_look_is_a_single_filter_chain(lang):
    """필터 체인 한 줄이라 `clip_filter`에 그대로 얹힌다 — 줄바꿈·따옴표가 섞이면 깨진다."""
    look = grade_for(lang)

    assert "\n" not in look
    assert not look.startswith(",") and not look.endswith(",")
    assert '"' not in look


def test_the_contract_records_why_the_strength_is_capped():
    """세기의 상한(씬 간 편차보다 작을 것)이 계약에 남아 있어야 다음 사람이 안 올린다."""
    meta = vocab.CHANNEL_LOOK["meta"]

    assert "_strength" in meta
    assert "_no_grade" in meta
    #: 엔딩·자막 경계가 계약에 적혀 있는지 — 코드만 아는 제약이 되면 안 된다.
    assert "ADR-0055" in meta["_where"]
