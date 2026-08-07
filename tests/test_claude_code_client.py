"""claude 헤드리스 어댑터 검증 (ADR-0008). 실제 서브프로세스는 띄우지 않는다."""

import json
import subprocess

import pytest

from shorts_factory.llm import claude_code as cc
from shorts_factory.llm.base import LLMError, LLMRateLimited, LLMTimeout


def _envelope(result: str, *, is_error: bool = False, subtype: str = "success") -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": subtype,
            "is_error": is_error,
            "result": result,
            "session_id": "sess-1",
            "num_turns": 3,
            "duration_ms": 4200,
        }
    )


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(cc.shutil, "which", lambda name: r"C:\fake\claude.exe")
    slept: list[float] = []
    instance = cc.ClaudeCodeClient(
        backoff_base=1, log_dir=tmp_path / "logs", sleep=slept.append
    )
    instance.slept = slept  # type: ignore[attr-defined]
    return instance


def _stub_runs(monkeypatch, outcomes):
    """subprocess.run을 순서대로 정해진 결과로 대체한다."""
    calls: list[dict] = []
    queue = list(outcomes)

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, "input": kwargs.get("input"), "cwd": kwargs.get("cwd")})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        stdout, returncode = item
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")

    monkeypatch.setattr(cc.subprocess, "run", fake_run)
    return calls


def test_parses_result_envelope(client, monkeypatch):
    _stub_runs(monkeypatch, [(_envelope("조사 결과 본문"), 0)])
    result = client.run("프롬프트", label="01-research")

    assert result.text == "조사 결과 본문"
    assert result.session_id == "sess-1"
    assert result.num_turns == 3
    assert result.meta["duration_ms"] == 4200


def test_prompt_goes_through_stdin_not_argv(client, monkeypatch):
    calls = _stub_runs(monkeypatch, [(_envelope("ok"), 0)])
    long_prompt = "한글 프롬프트 " * 5000
    client.run(long_prompt)

    assert calls[0]["input"] == long_prompt
    assert long_prompt not in " ".join(calls[0]["cmd"])


def test_runs_in_neutral_cwd(client, monkeypatch, tmp_path):
    """프로젝트 CLAUDE.md가 세션에 유입되면 ADR-0009 세션 독립성이 깨진다."""
    calls = _stub_runs(monkeypatch, [(_envelope("ok"), 0)])
    client.run("프롬프트")

    cwd = calls[0]["cwd"]
    assert cwd is not None
    assert "shorts-factory" not in str(cwd)


def test_headless_flags(client, monkeypatch):
    calls = _stub_runs(monkeypatch, [(_envelope("ok"), 0)])
    client.run("프롬프트", allowed_tools=("WebSearch", "WebFetch"))

    cmd = calls[0]["cmd"]
    assert "-p" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--allowedTools") + 1] == "WebSearch,WebFetch"
    # 구독 인증 경로를 우회하는 플래그는 쓰지 않는다 (ADR-0008)
    assert "--bare" not in cmd


def test_no_tools_means_no_flag(client, monkeypatch):
    calls = _stub_runs(monkeypatch, [(_envelope("ok"), 0)])
    client.run("프롬프트", allowed_tools=())
    assert "--allowedTools" not in calls[0]["cmd"]


def test_rate_limit_backs_off_then_succeeds(client, monkeypatch):
    _stub_runs(
        monkeypatch,
        [
            (_envelope("Claude AI usage limit reached", is_error=True), 1),
            (_envelope("본문"), 0),
        ],
    )
    result = client.run("프롬프트", label="01-research")

    assert result.text == "본문"
    assert len(client.slept) == 1  # 백오프가 한 번 걸렸다


def test_rate_limit_exhausts_retries(client, monkeypatch):
    client.max_retries = 2
    _stub_runs(
        monkeypatch,
        [(_envelope("rate limit exceeded", is_error=True), 1)] * 2,
    )
    with pytest.raises(LLMRateLimited):
        client.run("프롬프트")


def test_non_retryable_error_fails_immediately(client, monkeypatch):
    _stub_runs(
        monkeypatch,
        [(_envelope("Invalid model name", is_error=True, subtype="error"), 1)],
    )
    with pytest.raises(LLMError, match="Invalid model"):
        client.run("프롬프트")
    assert client.slept == []


def test_timeout_is_surfaced(client, monkeypatch):
    client.max_retries = 1
    _stub_runs(monkeypatch, [subprocess.TimeoutExpired("claude", 900)])
    with pytest.raises(LLMTimeout):
        client.run("프롬프트")


def test_unparseable_stdout_raises(client, monkeypatch):
    _stub_runs(monkeypatch, [("이건 JSON이 아님", 0)])
    with pytest.raises(LLMError, match="JSON"):
        client.run("프롬프트")


def test_session_envelope_is_logged_for_traceability(client, monkeypatch, tmp_path):
    _stub_runs(monkeypatch, [(_envelope("본문"), 0)])
    client.run("프롬프트", label="02-verify")

    logged = tmp_path / "logs" / "02-verify.attempt1.json"
    assert logged.is_file()
    assert json.loads(logged.read_text(encoding="utf-8"))["envelope"]["result"] == "본문"


def test_missing_executable_is_reported_clearly(monkeypatch):
    monkeypatch.setattr(cc.shutil, "which", lambda name: None)
    with pytest.raises(LLMError, match="claude"):
        cc.ClaudeCodeClient()
