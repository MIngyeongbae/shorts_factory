"""CLI 인자 파싱. 전역 옵션은 서브커맨드 앞뒤 어느 쪽에 와도 먹혀야 한다."""

import io
import sys
from pathlib import Path

import pytest

from shorts_factory.cli import _force_utf8_streams
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


def test_validate_requires_slug():
    with pytest.raises(SystemExit):
        parse(["validate"])


def test_validate_takes_slug_and_run_id():
    args = parse(["validate", "--slug", "abc", "--run-id", "20260810-abc"])
    assert args.slug == "abc" and args.run_id == "20260810-abc"


#: 실제로 요약문을 깨뜨린 문자들. em dash는 [0b] 요약, 나머지는 전 단계 공통이다.
KOREAN_SUMMARY = "[0b] 통과 — 피사의 사탑 지반 보강 (사실 92건)"


def test_stdout_is_reconfigured_to_utf8(monkeypatch):
    """cp949 콘솔에서도 한국어 요약을 출력할 수 있어야 한다.

    첫 실전 package 실행이 모든 산출물을 쓴 뒤 이 지점에서 UnicodeEncodeError로
    죽었다. Windows 한국어 로캘의 기본 stdout이 cp949다.
    """
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp949"))

    _force_utf8_streams()
    print(KOREAN_SUMMARY)
    sys.stdout.flush()

    assert raw.getvalue().decode("utf-8").strip() == KOREAN_SUMMARY


def test_force_utf8_tolerates_streams_without_reconfigure(monkeypatch):
    """pytest capsys처럼 reconfigure가 없는 스트림에서도 죽으면 안 된다."""
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    _force_utf8_streams()  # 예외가 나지 않는 것이 전부다

    print(KOREAN_SUMMARY)
    assert sys.stdout.getvalue().strip() == KOREAN_SUMMARY
