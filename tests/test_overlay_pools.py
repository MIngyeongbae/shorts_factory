"""언어별 겹풀 — `[7]`이 만들고 `[9]`·`[9t]`가 읽는다 (ADR-0095, specs/05 `[7]`·`[9]`·`[9t]`).

- `[7]`: 기본 풀(전 씬, 있는 언어의 최장) 뒤에 판정이 고른 ko 아닌 언어마다 홀·짝 씬을
  **그 언어의 길이**로 산다. 기록은 풀마다 따로, 재실행은 풀마다 자기 기록으로 스킵
- `[9]`: 겹풀에 있는 씬은 거기서, 없는 씬은 기본 풀에서 — 파일 존재로 가른다
- `[9t]`: 판도 그 언어의 풀에서
"""

from __future__ import annotations

import json

import pytest
from conftest import PISA
from test_thumbnail_stage import FIXTURE_TITLE, set_title
from test_thumbnail_stage import run as run_thumbnail
from test_videogen_stage import install, record_of, run as run_videogen
from timed_fixtures import install_run

from shorts_factory.stages.assemble import run_assemble_stage
from shorts_factory.video.fake import FakeFFmpeg, write_fake_clips
from shorts_factory.video.pools import overlay_dir, overlay_record, overlay_scene_ids
from shorts_factory.video.timeline import CLIPS_DIR
from shorts_factory.videogen.fake import FakeVideoClient

LANGS = {"ja": 1.13, "en": 1.05}


@pytest.fixture
def trilingual(paths):
    """`[3]`이 세 언어를 실측한 run — 블록 없는 옛 편이라 있는 언어 전부가 대상이다."""
    run_id, document = install(paths, langs=LANGS)
    return paths, run_id, document


# --- [7] ------------------------------------------------------------------------


def test_the_stage_buys_the_base_pool_then_half_the_scenes_per_language(trilingual):
    paths, run_id, document = trilingual
    ids = [s["scene_id"] for s in document["scenes"]]
    client = FakeVideoClient()

    result = run_videogen(paths, run_id, client=client)

    assert result.passed
    run_dir = paths.run_dir(run_id)
    assert sorted(result.overlays) == ["en", "ja"]
    for lang in ("ja", "en"):
        expected = overlay_scene_ids(lang, ids)
        assert [o.scene_id for o in result.overlays[lang]] == expected
        for sid in expected:
            assert (run_dir / overlay_dir(lang) / f"{sid}.mp4").exists()
        assert not any((run_dir / overlay_dir(lang) / f"{sid}.mp4").exists() for sid in ids if sid not in expected)
        assert (run_dir / overlay_record(lang)).exists()
    # 기본 풀은 그대로 전 씬이다 — 겹풀은 위에 얹히는 것이지 기본 풀을 줄이지 않는다
    assert all((run_dir / CLIPS_DIR / f"{sid}.mp4").exists() for sid in ids)
    assert len(client.calls) == len(ids) + len(ids) // 2 + (len(ids) - len(ids) // 2)


def test_overlay_clips_are_bought_at_that_language_s_length(trilingual):
    """겹풀은 그 언어의 씬 길이 + 꼬리다 — 세 언어 최장이 아니다 (ADR-0095 결정 2)."""
    paths, run_id, _document = trilingual
    result = run_videogen(paths, run_id)

    base = {o.scene_id: o for o in result.outcomes}
    for o in result.overlays["en"]:
        assert set(o.lang_seconds) == {"en"}
        assert o.seconds <= base[o.scene_id].seconds       # en(×1.05) ≤ 최장(ja ×1.13)
    for o in result.overlays["ja"]:
        assert set(o.lang_seconds) == {"ja"}


def test_a_named_language_limits_the_overlays(trilingual):
    paths, run_id, _document = trilingual
    result = run_videogen(paths, run_id, langs=["ja"])
    assert list(result.overlays) == ["ja"]
    assert not (paths.run_dir(run_id) / overlay_dir("en")).exists()


def test_a_rerun_does_not_buy_done_overlay_scenes(trilingual):
    paths, run_id, _document = trilingual
    first = FakeVideoClient()
    run_videogen(paths, run_id, client=first)

    (paths.run_dir(run_id) / overlay_dir("ja") / "3.mp4").unlink()
    second = FakeVideoClient()
    result = run_videogen(paths, run_id, client=second)

    assert result.passed and not result.skipped
    assert len(second.calls) == 1                           # ja 씬 3 하나만 다시 샀다
    record = json.loads((paths.run_dir(run_id) / overlay_record("ja")).read_text(encoding="utf-8"))
    assert record["pool"] == "ja" and record["clips_dir"] == overlay_dir("ja")


def test_a_fully_done_run_is_skipped_with_its_overlays(trilingual):
    paths, run_id, _document = trilingual
    run_videogen(paths, run_id)
    result = run_videogen(paths, run_id, client=FakeVideoClient())
    assert result.skipped and sorted(result.overlays) == ["en", "ja"]


def test_a_monolingual_run_has_no_overlays(paths):
    run_id, _document = install(paths)
    result = run_videogen(paths, run_id)
    assert result.passed and result.overlays == {}
    assert record_of(paths, run_id)["pool"] == "base"


# --- [9] ------------------------------------------------------------------------


def test_assemble_reads_the_overlay_scene_and_the_base_pool_for_the_rest(paths):
    run_id, document = install_run(paths, PISA, langs={"ja": 1.13})
    run_dir = paths.run_dir(run_id)
    write_fake_clips(run_dir / overlay_dir("ja"), [1, 3])
    ffmpeg = FakeFFmpeg()

    run_assemble_stage(run_id, paths=paths, langs=["ja"], runner=ffmpeg)

    cmd = ffmpeg.last
    inputs = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-i"]
    expected = [
        f"{overlay_dir('ja')}/{s['scene_id']}.mp4" if s["scene_id"] in (1, 3)
        else f"{CLIPS_DIR}/{s['scene_id']}.mp4"
        for s in document["scenes"]
    ]
    assert inputs == expected


def test_korean_never_reads_an_overlay(paths):
    run_id, document = install_run(paths, PISA)
    write_fake_clips(paths.run_dir(run_id) / overlay_dir("ja"), [1])
    ffmpeg = FakeFFmpeg()

    run_assemble_stage(run_id, paths=paths, langs=["ko"], runner=ffmpeg)

    cmd = ffmpeg.last
    inputs = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-i"]
    assert inputs == [f"{CLIPS_DIR}/{s['scene_id']}.mp4" for s in document["scenes"]]


# --- [9t] -----------------------------------------------------------------------


def test_the_thumbnail_plate_comes_from_that_language_s_pool(paths):
    run_id, _document = install_run(paths, PISA, langs={"ja": 1.13})
    set_title(paths, run_id, FIXTURE_TITLE)
    set_title(paths, run_id, "ピサの斜塔", lang="ja")
    ffmpeg = FakeFFmpeg()
    probe = run_thumbnail(paths, run_id, ffmpeg=ffmpeg, langs=["ja"])
    plate = probe.languages["ja"].scene_id
    write_fake_clips(paths.run_dir(run_id) / overlay_dir("ja"), [plate])

    ffmpeg = FakeFFmpeg()
    result = run_thumbnail(paths, run_id, ffmpeg=ffmpeg, langs=["ja"], force=True)

    assert result.languages["ja"].scene_id == plate
    inputs = [ffmpeg.last[i + 1] for i, a in enumerate(ffmpeg.last) if a == "-i"]
    assert inputs == [f"{overlay_dir('ja')}/{plate}.mp4"]
