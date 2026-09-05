"""클립 풀 계약 (ADR-0095) — 기본 풀 하나 + 언어별 겹풀, 값은 channel-look.json이 진다."""

from __future__ import annotations

from pathlib import Path

import pytest

from shorts_factory.schemas import vocab
from shorts_factory.schemas.timed_scenes import LANGUAGES, PRIMARY_LANGUAGE
from shorts_factory.video.fake import write_fake_clips
from shorts_factory.video.pools import (
    ALL,
    EVEN,
    ODD,
    clip_scenes_for,
    clip_source_dir,
    overlay_dir,
    overlay_languages,
    overlay_record,
    overlay_scene_ids,
)
from shorts_factory.video.timeline import CLIPS_DIR

SCENES = list(range(1, 19))


def test_the_contract_names_every_language_and_the_primary_keeps_the_base_pool():
    values = vocab.CHANNEL_LOOK["meta"]["clip_scenes"]
    assert set(k for k in values if not k.startswith("_")) == set(LANGUAGES)
    assert clip_scenes_for(PRIMARY_LANGUAGE) == ALL


def test_the_human_chose_odd_for_japanese_and_even_for_english():
    """사람 결정 2026-09-04 — *"일본은 홀수, 영어는 짝수"*."""
    assert clip_scenes_for("ja") == ODD and clip_scenes_for("en") == EVEN
    assert overlay_languages() == ("ja", "en")


def test_overlay_scenes_are_the_parity_and_the_two_overlays_never_meet():
    ja, en = overlay_scene_ids("ja", SCENES), overlay_scene_ids("en", SCENES)
    assert ja == [1, 3, 5, 7, 9, 11, 13, 15, 17]
    assert en == [2, 4, 6, 8, 10, 12, 14, 16, 18]
    assert not set(ja) & set(en)                         # ja–en 0%
    assert len(ja) + len(en) == len(SCENES)              # 각 언어는 ko와 절반을 나눈다


def test_the_primary_language_has_no_overlay():
    assert overlay_scene_ids(PRIMARY_LANGUAGE, SCENES) == []
    assert overlay_scene_ids("xx", SCENES) == []         # 모르는 언어도 겹풀이 없다 (D-3)


def test_a_value_outside_the_contract_is_refused(monkeypatch):
    monkeypatch.setitem(__import__("shorts_factory.video.pools", fromlist=["_CLIP_SCENES"])._CLIP_SCENES, "ja", "third")
    with pytest.raises(ValueError, match="계약 밖"):
        clip_scenes_for("ja")


def test_paths_are_derived_from_the_base_pool_name():
    assert overlay_dir("ja") == f"{CLIPS_DIR}.ja"
    assert overlay_record("en") == f"{CLIPS_DIR}.en.json"


def test_lookup_prefers_the_overlay_and_falls_back_to_the_base_pool(tmp_path: Path):
    run_dir = tmp_path
    write_fake_clips(run_dir / CLIPS_DIR, [1, 2])
    write_fake_clips(run_dir / overlay_dir("ja"), [1])

    assert clip_source_dir(run_dir, "ja", 1) == overlay_dir("ja")   # 겹풀에 있다
    assert clip_source_dir(run_dir, "ja", 2) == CLIPS_DIR            # 없으면 기본 풀
    assert clip_source_dir(run_dir, "ko", 1) == CLIPS_DIR            # ko는 언제나 기본 풀
    assert clip_source_dir(run_dir, "", 1) == CLIPS_DIR              # 언어 없음 = 기본 풀
