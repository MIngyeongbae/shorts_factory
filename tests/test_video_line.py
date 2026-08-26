"""영상 라인 계약 (ADR-0059) — 어휘의 `video_line`, `judgment/human.json` 읽기, CLI의 어댑터 선택.

- 라인 목록·기본값·라인→어댑터 표는 `vocab.json`에만 있다 (ADR-0034). 코드는 읽는다
- 사람이 비우면 기본 라인, 오타면 실패 — 조용히 기본으로 내려가면 유료·무료가 뒤바뀐다
- `--provider`가 라인을 이긴다. 어댑터 없는 라인(`provider: null`)은 멈춘다
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from shorts_factory.cli import VIDEO_PROVIDERS, _resolve_video_provider
from shorts_factory.config import write_text
from shorts_factory.judgment import (
    JudgmentError,
    human_path,
    read_video_line,
    slug_from_run_id,
)
from shorts_factory.schemas import vocab
from shorts_factory.stages.videogen import VideogenStageError

SLUG = "test-topic"
RUN_ID = "20260823-test-topic"


def write_human(paths, **fields):
    write_text(human_path(paths, SLUG), json.dumps({"judge": "human", "decision": "go", **fields}))


# --- 어휘 -----------------------------------------------------------------------


def test_default_line_is_one_of_the_enum():
    assert vocab.video_line_default() in vocab.values("video_line")


def test_every_line_has_meta_with_provider_and_shot2():
    for line in vocab.values("video_line"):
        meta = vocab.video_line_meta(line)
        assert "provider" in meta and "shot2" in meta
        assert isinstance(meta["shot2"], bool)


def test_every_declared_provider_is_a_cli_adapter():
    """`meta.video_line.*.provider`가 가리키는 이름은 CLI 어댑터 표에 있어야 한다 (null은 미구현)."""
    for line in vocab.values("video_line"):
        provider = vocab.video_line_meta(line)["provider"]
        assert provider is None or provider in VIDEO_PROVIDERS, (line, provider)


def test_default_line_has_an_adapter_and_costs_nothing_to_pick():
    """기본 라인은 구현돼 있어야 한다 — 사람이 아무것도 안 적고 돌렸을 때 멈추면 안 된다."""
    assert vocab.video_line_meta(vocab.video_line_default())["provider"] in VIDEO_PROVIDERS


def test_unknown_line_is_refused():
    with pytest.raises(ValueError, match="video_line"):
        vocab.video_line_meta("cloud")


# --- human.json -----------------------------------------------------------------


def test_missing_file_means_the_default_line(paths):
    assert read_video_line(paths, SLUG) == vocab.video_line_default()


def test_empty_field_means_the_default_line(paths):
    write_human(paths, video_line="")
    assert read_video_line(paths, SLUG) == vocab.video_line_default()


def test_chosen_line_is_returned(paths):
    chosen = [v for v in vocab.values("video_line") if v != vocab.video_line_default()][0]
    write_human(paths, video_line=chosen)
    assert read_video_line(paths, SLUG) == chosen


def test_typo_fails_instead_of_falling_back(paths):
    write_human(paths, video_line="locl")
    with pytest.raises(JudgmentError, match="어휘 밖"):
        read_video_line(paths, SLUG)


def test_broken_json_fails(paths):
    write_text(human_path(paths, SLUG), "{not json")
    with pytest.raises(JudgmentError, match="읽을 수 없다"):
        read_video_line(paths, SLUG)


def test_slug_from_run_id_round_trips():
    assert slug_from_run_id(RUN_ID) == SLUG
    assert slug_from_run_id("20260821-panama-unha-san-wiui-hosu") == "panama-unha-san-wiui-hosu"


def test_slug_from_malformed_run_id_fails():
    with pytest.raises(JudgmentError, match="YYYYMMDD"):
        slug_from_run_id("test-topic")


# --- CLI 선택 ---------------------------------------------------------------------


def args_for(provider=None, slug=None, line=None):
    return SimpleNamespace(provider=provider, slug=slug, line=line)


def test_explicit_provider_wins_over_the_line(paths):
    write_human(paths, video_line=vocab.video_line_default())
    assert _resolve_video_provider(args_for(provider="fake"), paths, RUN_ID) == "fake"


def test_line_resolves_through_the_vocab_table(paths):
    for line in vocab.values("video_line"):
        provider = vocab.video_line_meta(line)["provider"]
        if provider is None:
            continue
        write_human(paths, video_line=line)
        assert _resolve_video_provider(args_for(), paths, RUN_ID) == provider


def test_the_line_flag_moves_the_adapter_too(paths):
    """`--line`은 라인 전체를 갈아 끼운다 — 어댑터와 프레임 해석이 갈리면 안 된다 (ADR-0071)."""
    write_human(paths, video_line="local")
    for line in vocab.values("video_line"):
        provider = vocab.video_line_meta(line)["provider"]
        if provider is None:
            continue
        assert _resolve_video_provider(args_for(line=line), paths, RUN_ID) == provider


def test_line_without_an_adapter_stops_instead_of_switching_lines(paths):
    unimplemented = [v for v in vocab.values("video_line") if vocab.video_line_meta(v)["provider"] is None]
    if not unimplemented:
        pytest.skip("모든 라인에 어댑터가 있다")
    write_human(paths, video_line=unimplemented[0])
    with pytest.raises(VideogenStageError, match="아직 어댑터가 없다"):
        _resolve_video_provider(args_for(), paths, RUN_ID)


def test_slug_argument_is_preferred_to_deriving_it_from_run_id(paths):
    write_human(paths, video_line="locl")  # 이 슬러그를 읽었다면 실패해야 한다
    with pytest.raises(JudgmentError):
        _resolve_video_provider(args_for(slug=SLUG), paths, "20260101-other")


# --- 씬마다 엔진이 갈린다 (ADR-0072 결정 5) -----------------------------------


def test_art_line_sends_info_scenes_to_a_text_to_video_engine():
    """`info`는 텍스트→영상, 나머지는 라인의 엔진 — 한 라인 안에서 갈린다.

    ADR-0075 결정 1이 이 자리의 엔진을 first/last 보간에서 **TTV**로 바꿨다: 계측 표시를
    정지 이미지가 지던 경로(MJ CLEAN → NB2 편집)가 사람 판독에서 졌다.
    """
    meta = vocab.video_line_meta("art")
    assert meta["provider"] == "mj-endimage"
    assert meta["info_provider"] == "comfy-h3"
    # 다른 라인은 갈리지 않는다 — 이 분기는 `art`의 것이다.
    for line in ("local", "api"):
        assert not vocab.video_line_meta(line).get("info_provider")


def test_the_info_engine_is_text_to_video():
    """`info` 씬은 프레임을 안 받는다 — 그 엔진에 프레임 입력을 요구하지 않는다 (ADR-0075)."""
    from shorts_factory.videogen.comfy_h3 import ComfyH3Client

    assert ComfyH3Client.accepts_frames is False
    assert vocab.video_line_meta("art")["info_provider"] == ComfyH3Client.name


def test_scenes_that_take_no_frames_go_to_the_info_engine():
    """프레임을 받는 씬만 주 엔진이다 — `info` 씬은 강등된 뒤에도 텍스트→영상이다.

    RED를 뺀 변종(`no_red`)도 그 씬엔 CLEAN이 없으므로 주 엔진(MJ)으로 못 간다.
    `takes_frames`가 그 판정을 진다 (ADR-0075 결정 3).
    """
    from shorts_factory.stages.videogen import VARIANT_INFO, VARIANT_NO_RED, VARIANT_VIDEO, SceneJob, _Runner

    runner = _Runner.__new__(_Runner)
    runner.client = object()
    runner.info_client = object()

    def job(**over):
        fields = dict(scene_id=1, prompt="p", negative_prompt="", has_info=True, labels=[],
                      seconds=5, lang_seconds={}, clamped=None, review_fields={})
        fields.update(over)
        return SceneJob(**fields)

    info_scene = job(takes_frames=False)
    assert runner.engine_for(info_scene, VARIANT_INFO) is runner.info_client
    # RED를 뺀 변종도 CLEAN이 없어 주 엔진으로 못 돌아간다 — 그대로 텍스트→영상이다.
    assert runner.engine_for(info_scene, VARIANT_NO_RED) is runner.info_client
    # 프레임을 받는 일반 씬은 주 엔진(MJ)이다.
    plain = job(has_info=False, takes_frames=True, first_frame="https://x/c.png")
    assert runner.engine_for(plain, VARIANT_VIDEO) is runner.client
