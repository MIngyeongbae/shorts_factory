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


def args_for(provider=None, slug=None):
    return SimpleNamespace(provider=provider, slug=slug)


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
