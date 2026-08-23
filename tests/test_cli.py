"""CLI 인자 파싱. 전역 옵션은 서브커맨드 앞뒤 어느 쪽에 와도 먹혀야 한다."""

import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from shorts_factory.cli import _force_utf8_streams
from shorts_factory.cli import parse_args as parse
from shorts_factory.config import DEFAULT_MAX_RETRIES
from shorts_factory.tts.audio import DEFAULT_TEMPO


def test_global_flag_before_subcommand():
    args = parse(["-v", "draft", "--slug", "abc"])
    assert args.verbose is True and args.slug == "abc"


def test_global_flag_after_subcommand():
    """`run.py draft --slug X -v` 는 자연스러운 타이핑 순서다."""
    args = parse(["draft", "--slug", "abc", "-v"])
    assert args.verbose is True and args.slug == "abc"


def test_force_after_subcommand():
    assert parse(["topic", "--force"]).force is True


def test_model_after_subcommand():
    assert parse(["part1", "--model", "sonnet"]).model == "sonnet"


def test_defaults_survive_when_flag_absent():
    args = parse(["draft", "--slug", "abc"])
    assert args.verbose is False
    assert args.force is False
    assert args.model is None
    assert args.claude_bin == "claude"
    assert args.max_retries == DEFAULT_MAX_RETRIES
    assert args.root is None


def test_global_value_is_not_clobbered_by_subparser_default():
    args = parse(["--model", "opus", "--force", "draft", "--slug", "abc"])
    assert args.model == "opus" and args.force is True


def test_root_is_a_path():
    args = parse(["topic", "--root", "C:/tmp/proj"])
    assert isinstance(args.root, Path)


def test_subcommand_is_required():
    with pytest.raises(SystemExit):
        parse(["-v"])


def test_draft_requires_slug():
    with pytest.raises(SystemExit):
        parse(["draft"])


def test_factcheck_requires_slug():
    with pytest.raises(SystemExit):
        parse(["factcheck"])


def test_factcheck_takes_slug_and_run_id():
    args = parse(["factcheck", "--slug", "abc", "--run-id", "20260810-abc"])
    assert args.run_id == "20260810-abc"


def test_localize_requires_slug():
    with pytest.raises(SystemExit):
        parse(["localize"])


def test_localize_takes_slug_run_id_and_lang():
    args = parse(["localize", "--slug", "abc", "--run-id", "20260822-abc", "--lang", "ja"])
    assert args.slug == "abc" and args.run_id == "20260822-abc" and args.lang == "ja"
    assert parse(["localize", "--slug", "abc"]).lang is None


def test_localize_takes_the_common_model_flag():
    assert parse(["localize", "--slug", "abc", "--model", "sonnet"]).model == "sonnet"


def test_prompt_requires_slug():
    with pytest.raises(SystemExit):
        parse(["prompt"])


def test_prompt_takes_no_run_id():
    """[5]의 run_id는 씬 계약(scenes.json)이 들고 있다 (ADR-0017 계보 = run_id)."""
    args = parse(["prompt", "--slug", "abc"])
    assert args.slug == "abc"
    assert not hasattr(args, "run_id")


def test_prompt_has_no_dialect_option():
    """ADR-0056 — 프로바이더가 하나라 방언 분기(ADR-0027)가 접혔다."""
    args = parse(["prompt", "--slug", "abc"])
    assert not hasattr(args, "dialect")
    with pytest.raises(SystemExit):
        parse(["prompt", "--slug", "abc", "--dialect", "mj"])


@pytest.mark.parametrize("command", ["imagegen", "imagereview", "info"])
def test_image_stage_commands_are_gone(command):
    """ADR-0056 — 이미지 단계([6]·[6r]·[6i])는 CLI에서 단계째 사라졌다."""
    with pytest.raises(SystemExit):
        parse([command, "--slug", "abc"])


def test_videogen_command_replaces_motion():
    """[7]은 `videogen`이다 (ADR-0056). `motion`과 MJ·Veo 플래그는 사라졌다."""
    args = parse(["videogen", "--slug", "abc", "--jobs", "2"])
    assert args.slug == "abc" and args.jobs == 2
    # 어댑터 기본값은 CLI에 없다 — 사람의 영상 라인(human.json video_line)이 정한다 (ADR-0059)
    assert args.provider is None and args.review == "full"
    assert parse(["videogen", "--slug", "abc", "--provider", "comfy-h3"]).provider == "comfy-h3"
    assert parse(["videogen", "--run-id", "x", "--provider", "fake", "--review", "none"]).review == "none"
    with pytest.raises(SystemExit):
        parse(["motion", "--slug", "abc"])
    for gone in ("--video", "--info-video", "--dialect"):
        with pytest.raises(SystemExit):
            parse(["videogen", "--slug", "abc", gone, "x"])
    with pytest.raises(SystemExit):
        parse(["videogen", "--slug", "abc", "--review", "maybe"])


def test_scenetable_requires_slug():
    with pytest.raises(SystemExit):
        parse(["scenetable"])


def test_scenetable_takes_slug_and_run_id():
    args = parse(["scenetable", "--slug", "abc", "--run-id", "20260821-abc"])
    assert args.run_id == "20260821-abc"
    assert args.timeout is None


def test_tts_requires_slug():
    with pytest.raises(SystemExit):
        parse(["tts"])


def test_tts_takes_no_run_id():
    """[3]의 run_id는 runs/*/topic.json에서 슬러그로 찾는다 (ADR-0052)."""
    args = parse(["tts", "--slug", "abc"])
    assert args.slug == "abc"
    assert not hasattr(args, "run_id")


def test_tts_defaults_to_the_paid_provider():
    """페이크가 기본이면 무음 wav를 만들어 놓고 성공했다고 착각한다."""
    args = parse(["tts", "--slug", "abc"])
    assert args.provider == "elevenlabs"
    assert args.tempo == DEFAULT_TEMPO
    assert args.ffmpeg == "ffmpeg"


def test_tts_provider_and_tempo_are_overridable():
    args = parse(["tts", "--slug", "abc", "--provider", "fake", "--tempo", "1.2"])
    assert args.provider == "fake" and args.tempo == 1.2


def test_tts_rejects_unknown_provider():
    with pytest.raises(SystemExit):
        parse(["tts", "--slug", "abc", "--provider", "openai"])


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


# --- part1 체인 (ADR-0049·0056·0061) -----------------------------------------


def _ok(**extra):
    return SimpleNamespace(summary="ok", warnings=[], errors=[], unfit=False, **extra)


def _patch_part1(monkeypatch, order: list[str], *, localize=None, seedfetch=None):
    """[0]~[2l]을 전부 페이크로 갈아 끼우고 호출 순서만 기록한다."""
    # `seedfetch`는 `[0f]`(ADR-0061). 페이크의 기본은 **성공**이고, 실패해도
    # 체인이 안 서는 것은 아래 test_part1_continues_when_seedfetch_fails가 본다.
    from shorts_factory import cli

    topic = SimpleNamespace(
        accepted=True, slug="abc", run_id="20260822-abc", run_dir=Path("runs/20260822-abc"),
        summary="[0] ok",
    )
    monkeypatch.setattr(cli, "run_topic_stage", lambda *a, **k: (order.append("topic"), topic)[1])
    monkeypatch.setattr(
        cli, "run_seedfetch_stage",
        lambda *a, **k: (order.append("seedfetch"),
                         seedfetch or SimpleNamespace(summary="[0f] ok", warnings=[],
                                                      passed=True))[1],
    )
    monkeypatch.setattr(cli, "_make_client", lambda *a, **k: object())
    monkeypatch.setattr(cli, "run_draft_stage", lambda *a, **k: (order.append("draft"), _ok())[1])
    monkeypatch.setattr(
        cli, "run_factcheck_stage", lambda *a, **k: (order.append("factcheck"), _ok())[1]
    )
    monkeypatch.setattr(
        cli, "run_localize_stage",
        lambda *a, **k: (order.append("localize"), localize or _ok())[1],
    )
    return cli


def test_part1_runs_localize_after_factcheck(monkeypatch, capsys):
    order: list[str] = []
    cli = _patch_part1(monkeypatch, order)
    code = cli._cmd_part1(parse(["part1"]), None)
    assert code == 0
    assert order == ["topic", "seedfetch", "draft", "factcheck", "localize"]


def test_part1_exits_5_when_localize_check_fails(monkeypatch, capsys):
    order: list[str] = []
    failed = SimpleNamespace(summary="[2l] fail", warnings=[], errors=["en: 줄 수"], unfit=False)
    cli = _patch_part1(monkeypatch, order, localize=failed)
    assert cli._cmd_part1(parse(["part1"]), None) == cli.LOCALIZE_FAILURE == 5
    assert order[-1] == "localize"


def test_part1_continues_when_seedfetch_fails(monkeypatch):
    """`[0f]` 실패는 파이프라인을 세우지 않는다 (D-5, ADR-0061) — `[1]`이 WebFetch로 내려간다."""
    order: list[str] = []
    failed = SimpleNamespace(summary="[0f] 본문을 못 얻었다", warnings=["브라우저 없음"],
                             passed=False)
    cli = _patch_part1(monkeypatch, order, seedfetch=failed)

    assert cli._cmd_part1(parse(["part1"]), None) == 0
    assert order == ["topic", "seedfetch", "draft", "factcheck", "localize"]


def test_part1_survives_a_seedfetch_error(monkeypatch):
    """단계가 시작조차 못 해도 마찬가지다 — 예외가 체인을 끊지 않는다 (D-5)."""
    from shorts_factory import cli as cli_mod

    order: list[str] = []
    cli = _patch_part1(monkeypatch, order)

    def boom(*_a, **_kw):
        order.append("seedfetch")
        raise cli_mod.SeedfetchStageError("topic.json이 없다")

    monkeypatch.setattr(cli, "run_seedfetch_stage", boom)
    assert cli._cmd_part1(parse(["part1"]), None) == 0
    assert order == ["topic", "seedfetch", "draft", "factcheck", "localize"]


def test_part1_stops_before_localize_when_factcheck_fails(monkeypatch):
    from shorts_factory import cli

    order: list[str] = []
    _patch_part1(monkeypatch, order)
    failed = SimpleNamespace(summary="[2] fail", warnings=[], errors=["x"], unfit=False)
    monkeypatch.setattr(
        cli, "run_factcheck_stage", lambda *a, **k: (order.append("factcheck"), failed)[1]
    )
    assert cli._cmd_part1(parse(["part1"]), None) == cli.ENVELOPE_FAILURE == 4
    assert "localize" not in order


# --- [8] ending (ADR-0055) ----------------------------------------------------


def test_ending_accepts_slug_or_run_id():
    assert parse(["ending", "--slug", "abc"]).slug == "abc"
    assert parse(["ending", "--run-id", "20260822-abc"]).run_id == "20260822-abc"


def test_ending_takes_the_ffmpeg_and_timeout_options():
    args = parse(["ending", "--slug", "abc", "--ffmpeg", "/opt/ffmpeg", "--timeout", "90"])
    assert args.ffmpeg == "/opt/ffmpeg" and args.timeout == 90


def test_ending_defaults_to_the_stage_timeout():
    """상한은 단계가 정한다 — CLI가 숫자를 들면 두 곳이 갈린다."""
    assert parse(["ending", "--slug", "abc"]).timeout is None
