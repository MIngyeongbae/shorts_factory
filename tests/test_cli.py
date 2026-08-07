"""CLI 인자 파싱. 전역 옵션은 서브커맨드 앞뒤 어느 쪽에 와도 먹혀야 한다."""

from pathlib import Path

import pytest

from shorts_factory.cli import parse_args as parse
from shorts_factory.config import DEFAULT_MAX_RETRIES


def test_global_flag_before_subcommand():
    args = parse(["-v", "research", "--slug", "abc"])
    assert args.verbose is True and args.slug == "abc"


def test_global_flag_after_subcommand():
    """`run.py research --slug X -v` 는 자연스러운 타이핑 순서다."""
    args = parse(["research", "--slug", "abc", "-v"])
    assert args.verbose is True and args.slug == "abc"


def test_force_after_subcommand():
    assert parse(["topic", "--force"]).force is True


def test_model_after_subcommand():
    assert parse(["package", "--model", "sonnet"]).model == "sonnet"


def test_defaults_survive_when_flag_absent():
    args = parse(["research", "--slug", "abc"])
    assert args.verbose is False
    assert args.force is False
    assert args.model is None
    assert args.claude_bin == "claude"
    assert args.max_retries == DEFAULT_MAX_RETRIES
    assert args.root is None


def test_global_value_is_not_clobbered_by_subparser_default():
    args = parse(["--model", "opus", "--force", "research", "--slug", "abc"])
    assert args.model == "opus" and args.force is True


def test_root_is_a_path():
    args = parse(["topic", "--root", "C:/tmp/proj"])
    assert isinstance(args.root, Path)


def test_subcommand_is_required():
    with pytest.raises(SystemExit):
        parse(["-v"])


def test_research_requires_slug():
    with pytest.raises(SystemExit):
        parse(["research"])


def test_only_flag():
    assert parse(["research", "--slug", "a", "--only", "02-verify"]).only == "02-verify"
