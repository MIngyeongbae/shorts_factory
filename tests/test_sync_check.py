"""싱크 검증 (specs/00 성공 기준 4 "±200ms 이내", specs/05 `[9]`).

`[9]`가 만든 두 산출물(ASS 자막·클립 배치)이 씬 계약에서 얼마나 벗어났는지를 잰다.
오디오 파일을 열지 않는 이유는 오디오의 시각축이 곧 `scenes.timed.json`이기 때문이다
(ADR-0020 — 씬의 시각을 읽는 곳은 그 파일 하나).
"""

import pytest
from timed_fixtures import HOOVER, PISA, timed_document

from shorts_factory.video.subtitles import Cue, build_ass, parse_ass
from shorts_factory.video.timeline import build_timeline
from shorts_factory.video.verify import SYNC_TOLERANCE, check_sync


def report_for(slug=PISA, **overrides):
    document = timed_document(slug)
    scenes = document["scenes"]
    timeline = build_timeline(scenes)
    cues = parse_ass(build_ass(scenes)[0])
    kwargs = {
        "cues": cues,
        "timeline": timeline,
        "total_duration": document["total_duration"],
    }
    kwargs.update(overrides)
    return scenes, check_sync(scenes, **kwargs)


def test_tolerance_is_the_spec_number():
    assert SYNC_TOLERANCE == 0.2


@pytest.mark.parametrize("slug", [PISA, HOOVER])
def test_real_scripts_pass_with_room_to_spare(slug):
    _scenes, report = report_for(slug)

    assert report.passed
    assert report.max_drift <= 0.006, "ASS 1/100초 반올림 말고는 오차원이 없다"


def test_shifted_subtitles_are_caught():
    scenes, base = report_for()
    shifted = [
        Cue(start=c.start + 0.25, end=c.end + 0.25, lines=c.lines)
        for c in parse_ass(build_ass(scenes)[0])
    ]

    _scenes, report = report_for(cues=shifted)
    assert not report.passed
    assert "자막 시작" in report.errors[0]
    assert report.max_drift == pytest.approx(0.25, abs=0.01)
    assert base.passed


def test_drift_inside_the_tolerance_passes():
    scenes, _ = report_for()
    nudged = [
        Cue(start=c.start + 0.1, end=c.end + 0.1, lines=c.lines)
        for c in parse_ass(build_ass(scenes)[0])
    ]

    _scenes, report = report_for(cues=nudged)
    assert report.passed


def test_missing_cue_is_caught_before_anything_else():
    scenes, _ = report_for()
    short = parse_ass(build_ass(scenes)[0])[:-1]

    _scenes, report = report_for(cues=short)
    assert not report.passed
    assert "개수가 다르다" in report.errors[0]


def test_timeline_length_mismatch_is_caught():
    _scenes, report = report_for(total_duration=99.0)

    assert not report.passed
    assert any("타임라인 길이" in e for e in report.errors)


def test_clip_placement_is_checked_against_the_contract():
    """자막만 맞고 영상이 밀리는 경우를 잡는다."""
    document = timed_document(PISA)
    scenes = document["scenes"]
    # 6번 씬부터 통째로 0.3초 밀린 계획 (씬은 계속 이어져 있어 build_timeline은 통과한다)
    shifted = [
        dict(s, start=round(s["start"] + 0.3, 3), end=round(s["end"] + 0.3, 3))
        if index >= 5
        else dict(s)
        for index, s in enumerate(scenes)
    ]
    shifted[5]["start"] = scenes[4]["end"]

    report = check_sync(
        scenes,
        cues=parse_ass(build_ass(scenes)[0]),
        timeline=build_timeline(shifted),
        total_duration=document["total_duration"],
    )

    assert not report.passed
    assert any("클립 배치" in e for e in report.errors)
