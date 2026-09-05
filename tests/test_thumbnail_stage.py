"""[9t. thumbnail] 단계 계약 (specs/05-pipeline.md, specs/03, ADR-0091).

확인 대상:

- 판은 **씬 클립**이고 `timeline.{lang}.mp4`를 안 읽는다 (제목이 두 겹이 된다)
- **판이 되는 씬은 언어마다 다르다** (ADR-0092) — 후보는 `info`가 없는 씬이고 ko가 가장
  이른 자리를 받는다. 후보 클립이 없으면 씬 1로 떨어지되 편을 세우지 않는다
- **새 렌더 값이 없다** — 크기·줄 수·줄바꿈이 제목 훅(ADR-0065·0074·0080)에서 온다
- 막의 아래끝이 **제목 줄 수를 따라 움직인다** (2줄에 화면 절반을 안 덮는다)
- 제목이 없거나 줄 수를 넘으면 **굽지 않고 경고**한다 — 편을 실패로 만들지 않는다 (D-5)
- **기존 단계를 수정하지 않는다** (D-4) — `[8]`·`[9]`의 산출물을 안 읽고 안 쓴다
"""

import json

import pytest
from conftest import PISA
from timed_fixtures import install_run

from shorts_factory.cli import parse_args
from shorts_factory.stages.thumbnail import (
    ASS_PATTERN,
    STAGE,
    ThumbnailStageError,
    plate_path,
    run_thumbnail_stage,
    title_of,
)
from shorts_factory.video.fake import FakeFFmpeg
from shorts_factory.video.timeline import CLIPS_DIR
from shorts_factory.video.subtitles import (
    TITLE_MAX_LINES,
    TITLE_STYLE_NAME,
    title_max_line_chars_for,
)
from shorts_factory.video.thumbnail import (
    PLATE_SCENE_ID,
    plate_candidates,
    plate_scene_id,
    SCRIM_ALPHA,
    SCRIM_HOLD,
    THUMBNAIL_PATTERN,
    ThumbnailError,
    build_filter,
    scrim_bottom,
    scrim_expression,
)


def run(paths, run_id, *, ffmpeg=None, **kwargs):
    return run_thumbnail_stage(
        run_id, paths=paths, runner=ffmpeg or FakeFFmpeg(), **kwargs
    )


#: 픽스처의 실측 문서에는 `title`이 없다 — 제목은 ADR-0065의 **선택** 필드다.
#: 이 단계는 제목이 일 전부라 픽스처마다 심어 준다.
FIXTURE_TITLE = "피사의 사탑은 왜 아직 서 있나"


@pytest.fixture
def pisa(paths):
    """`[3]`과 `[7]`이 끝난 run 디렉터리 + 제목."""
    run_id, document = install_run(paths, PISA)
    set_title(paths, run_id, FIXTURE_TITLE)
    return paths, run_id, document


def state_of(paths, run_id):
    data = json.loads(
        (paths.run_dir(run_id) / "state.json").read_text(encoding="utf-8")
    )
    return data["stages"][STAGE]


def set_title(paths, run_id, title, lang="ko"):
    path = paths.run_dir(run_id) / f"scenes.timed.{lang}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    if title:
        document["title"] = title
    else:
        document.pop("title", None)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# --- 통과 경로 ---------------------------------------------------------------


def test_stage_burns_one_thumbnail_per_language(pisa):
    paths, run_id, _document = pisa

    result = run(paths, run_id)

    assert result.passed
    assert result.burned == ["ko"]
    item = result.languages["ko"]
    assert item.path == paths.run_dir(run_id) / THUMBNAIL_PATTERN.format(lang="ko")
    assert item.line_count >= 1


def test_the_plate_is_scene_one_not_the_timeline(pisa):
    """`timeline.{lang}.mp4`를 읽으면 제목이 두 겹이 되고 자막까지 딸려온다 (맥락 3)."""
    paths, run_id, _document = pisa
    ffmpeg = FakeFFmpeg()

    run(paths, run_id, ffmpeg=ffmpeg)

    inputs = [ffmpeg.last[i + 1] for i, a in enumerate(ffmpeg.last) if a == "-i"]
    assert inputs == [f"clips/{PLATE_SCENE_ID}.mp4"]
    assert not any("timeline" in str(a) for a in ffmpeg.last)


def test_three_languages_take_different_plates(paths):
    """**판이 언어마다 다르다** (ADR-0092) — 세 채널에 같은 화면이 올라가는 것을 줄인다."""
    run_id, _document = install_run(paths, PISA)
    set_title(paths, run_id, FIXTURE_TITLE)
    for lang in ("ja", "en"):
        source = paths.run_dir(run_id) / "scenes.timed.ko.json"
        target = paths.run_dir(run_id) / f"scenes.timed.{lang}.json"
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    set_title(paths, run_id, "ピサの斜塔", lang="ja")
    ffmpeg = FakeFFmpeg()

    result = run(paths, run_id, ffmpeg=ffmpeg)

    assert sorted(result.burned) == ["en", "ja", "ko"]
    plates = [call["cmd"][call["cmd"].index("-i") + 1] for call in ffmpeg.calls]
    assert len(set(plates)) == 3, f"세 언어가 판을 나눠 갖지 않았다: {plates}"
    # ko는 가장 이른 후보다 — ADR-0091이 실측한 자리에 제일 가깝게 남는다.
    assert result.languages["ko"].scene_id == min(
        item.scene_id for item in result.languages.values()
    )


def test_the_plate_avoids_scenes_that_carry_measurement_labels():
    """`info` 씬은 빨간 계측 표시가 구워져 있어 제목 글자와 겹친다 (ADR-0075·0092)."""
    rows = [
        {"scene_id": 1, "info": {"labels": ["4x"]}},
        {"scene_id": 2},
        {"scene_id": 3, "info": {"labels": ["300,000"]}},
        {"scene_id": 4},
    ]

    assert plate_candidates(rows) == [2, 4]


def test_every_scene_is_a_candidate_when_all_of_them_carry_labels():
    """판이 없는 것보다 겹치는 편이 낫다 — 막이 그라데이션이라 무너지지는 않는다."""
    rows = [{"scene_id": 1, "info": {}}, {"scene_id": 2, "info": {"labels": ["1 m"]}}]

    #: `info: {}`는 빈 값이라 계측 표시가 아니다 — 실제로 표시가 있는 씬만 뺀다.
    assert plate_candidates(rows) == [1]
    assert plate_candidates([{"scene_id": 9, "info": {"labels": ["x"]}}]) == [9]


def test_the_window_spreads_the_languages_and_gives_ko_the_earliest():
    """언어 자리 0은 항상 가장 이른 후보다 (ADR-0092)."""
    candidates = [2, 4, 5, 7, 9, 11]
    picked = [
        plate_scene_id(candidates, position=i, total=3) for i in range(3)
    ]

    assert picked[0] == candidates[0]
    assert len(set(picked)) == 3
    assert picked == sorted(picked)
    #: 후보가 언어 수보다 적으면 겹친다 — 못 고치는 자리라 오류가 아니다.
    assert plate_scene_id([3], position=2, total=3) == 3
    assert plate_scene_id([], position=1, total=3) == PLATE_SCENE_ID


def test_the_ass_carries_the_title_style_not_a_new_one(pisa):
    """**새 렌더 값이 없다** — 제목 훅과 같은 스타일 줄을 쓴다 (사람 결정)."""
    paths, run_id, _document = pisa

    run(paths, run_id)

    ass = (paths.run_dir(run_id) / ASS_PATTERN.format(lang="ko")).read_text(
        encoding="utf-8"
    )
    assert f"Style: {TITLE_STYLE_NAME}," in ass
    assert ass.count("Dialogue:") == 1     # 씬 자막도 CTA도 없다


def test_state_records_the_outputs(pisa):
    paths, run_id, _document = pisa
    run(paths, run_id)
    stage = state_of(paths, run_id)

    assert stage["status"] == "done"
    assert stage["burned"] == ["ko"]
    assert sorted(stage["outputs"]) == [
        f"runs/{run_id}/thumbnail.ko.ass",
        f"runs/{run_id}/thumbnail.ko.png",
    ]


# --- 막의 기하 (ADR-0091 맥락 5) ---------------------------------------------


def test_the_scrim_follows_the_title_block():
    """2줄 제목에 화면 절반을 덮지 않는다 — 아래끝이 줄 수를 따라간다."""
    assert scrim_bottom(1) < scrim_bottom(2) < scrim_bottom(3) < scrim_bottom(6)
    assert scrim_bottom(2) / 1920 < 0.35
    #: 6줄(제목 훅 상한)이 ADR-0091 되돌릴 조건 1의 관측 자리다.
    assert scrim_bottom(TITLE_MAX_LINES) / 1920 == pytest.approx(0.54, abs=0.01)


def test_the_scrim_fades_instead_of_ending_on_a_hard_edge():
    """딱딱한 띠는 반투명이라 **그림을 자른 자국**으로 남았다 (실측 3편)."""
    bottom = scrim_bottom(3)
    expression = scrim_expression(bottom)
    top = round(SCRIM_ALPHA * 255)
    hold = round(bottom * SCRIM_HOLD)

    assert f"lt(Y,{hold}),{top}" in expression      # hold까지는 그대로
    assert expression.endswith(",0))")              # 아래끝에서 0이다
    assert hold < bottom


def test_a_scrim_that_holds_all_the_way_down_is_refused(monkeypatch):
    """`hold`가 1.0이면 그라데가 아니라 딱딱한 띠다 — 실측이 죽인 그 모양이다."""
    monkeypatch.setattr("shorts_factory.video.thumbnail.SCRIM_HOLD", 1.0)
    with pytest.raises(ThumbnailError):
        scrim_expression(scrim_bottom(3))


def test_the_filter_escapes_the_expression_commas():
    """`geq` 식의 쉼표는 filtergraph의 인자 구분자라 그대로 두면 필터가 쪼개진다."""
    graph = build_filter(scrim_bottom(2), "thumbnail.ko.ass", None)

    assert "geq=r=0:g=0:b=0:a=" in graph
    assert "if(lt(Y\\," in graph
    assert "ass=filename=thumbnail.ko.ass" in graph


def test_the_filter_takes_already_escaped_paths():
    """`[9]`의 `build_filter_graph`와 같은 계약 — 두 곳에서 따로 이스케이프하지 않는다."""
    graph = build_filter(scrim_bottom(2), "a.ass", "..\\/..\\/assets\\/fonts")

    assert "fontsdir=..\\/..\\/assets\\/fonts" in graph


# --- 굽지 않는 경우 (D-3 · D-5) ----------------------------------------------


def test_a_language_without_a_title_is_skipped_with_a_warning(pisa):
    """**이 산출물이 하는 일 전부가 제목이다** — 제목 없는 썸네일은 만들 이유가 없다."""
    paths, run_id, _document = pisa
    set_title(paths, run_id, "")
    ffmpeg = FakeFFmpeg()

    result = run(paths, run_id, ffmpeg=ffmpeg)

    assert result.burned == []
    assert ffmpeg.calls == []
    assert any("title이 없어" in w for w in result.warnings)
    assert not (paths.run_dir(run_id) / "thumbnail.ko.png").exists()


def test_a_title_that_overflows_is_skipped_not_fatal(pisa):
    """제목 훅과 같은 상한이다. 편을 실패로 만들지 않는다 (D-5)."""
    paths, run_id, _document = pisa
    limit = title_max_line_chars_for("ko")
    set_title(paths, run_id, "가" * (limit * (TITLE_MAX_LINES + 1)))
    ffmpeg = FakeFFmpeg()

    result = run(paths, run_id, ffmpeg=ffmpeg)

    assert result.burned == []
    assert ffmpeg.calls == []
    assert any("줄에 안 들어가" in w for w in result.warnings)


def test_missing_plate_clip_is_refused(paths):
    """**후보 클립이 하나도 없을 때만** 세운다 — 판이 여러 씬으로 넓어졌다 (ADR-0092)."""
    run_id, _document = install_run(paths, PISA)
    for clip in (paths.run_dir(run_id) / CLIPS_DIR).glob("*.mp4"):
        clip.unlink()

    with pytest.raises(ThumbnailStageError, match="판이 될 클립이 없다"):
        run(paths, run_id)


def test_one_missing_candidate_clip_falls_back_instead_of_failing(paths):
    """`[7]`이 한 씬에서 실패해도 썸네일은 나온다 — 마감이지 본편이 아니다 (D-5)."""
    run_id, _document = install_run(paths, PISA)
    set_title(paths, run_id, FIXTURE_TITLE)
    run_dir = paths.run_dir(run_id)
    candidates = plate_candidates(
        json.loads((run_dir / "scenes.json").read_text(encoding="utf-8"))["scenes"]
    )
    chosen = plate_scene_id(candidates, position=0, total=3)
    if chosen != PLATE_SCENE_ID:
        plate_path(run_dir, chosen).unlink()

    result = run(paths, run_id, ffmpeg=FakeFFmpeg())

    assert result.languages["ko"].burned
    assert result.languages["ko"].scene_id == PLATE_SCENE_ID or chosen == PLATE_SCENE_ID


# --- 경계 (D-1 · D-4) --------------------------------------------------------


def test_stage_reads_nothing_from_the_later_stages(pisa):
    """`[8]`·`[9]`와 선후가 없다 — 그 산출물이 없어도 돈다 (D-4)."""
    paths, run_id, _document = pisa
    run_dir = paths.run_dir(run_id)
    assert not (run_dir / "ending.json").exists()
    assert not (run_dir / "timeline.ko.mp4").exists()

    assert run(paths, run_id).passed


def test_stage_never_writes_under_topics(paths):
    run_id, _document = install_run(paths, PISA)
    set_title(paths, run_id, FIXTURE_TITLE)

    run(paths, run_id)

    assert not paths.topic_dir(PISA).exists()


def test_title_of_is_quiet_when_the_language_is_absent(paths):
    run_id, _document = install_run(paths, PISA)

    assert title_of(paths.run_dir(run_id), "ja") == ""


# --- 재시작 ------------------------------------------------------------------


def test_second_run_skips_and_does_not_re_render(pisa):
    paths, run_id, _document = pisa
    run(paths, run_id)

    ffmpeg = FakeFFmpeg()
    again = run(paths, run_id, ffmpeg=ffmpeg)

    assert again.languages["ko"].skipped
    assert ffmpeg.calls == []


def test_force_re_renders(pisa):
    paths, run_id, _document = pisa
    run(paths, run_id)

    ffmpeg = FakeFFmpeg()
    again = run(paths, run_id, ffmpeg=ffmpeg, force=True)

    assert not again.languages["ko"].skipped
    assert len(ffmpeg.calls) == 1


# --- CLI ---------------------------------------------------------------------


def test_cli_exposes_the_stage():
    args = parse_args(["thumbnail", "--run-id", "20260831-x", "--lang", "ko,ja"])

    assert args.run_id == "20260831-x"
    assert args.lang == "ko,ja"
