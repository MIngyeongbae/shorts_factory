"""codex 헤드리스 어댑터 + 단계별 엔진 선택 검증 (ADR-0097).

실제 서브프로세스는 띄우지 않는다. 여기서 지키는 것은 **계약**이다 —
격리 플래그(ADR-0009·0011), 재청 경로(ADR-0044), 어느 단계가 어느 엔진인가.
"""

import json
import subprocess

import pytest

from shorts_factory.llm import codex_cli as cx
from shorts_factory.llm.base import LLMError, LLMRateLimited, LLMTimeout


def _events(text: str = "본문", *, thread_id: str = "th-1", failure: str = "") -> str:
    lines = [
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "turn.started"},
        {"type": "item.completed",
         "item": {"id": "item_0", "type": "agent_message", "text": text}},
    ]
    if failure:
        lines.append({"type": "turn.failed", "error": {"message": failure}})
    else:
        lines.append({"type": "turn.completed", "usage": {"output_tokens": 12}})
    return "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(cx.shutil, "which", lambda name: r"C:\fake\codex.exe")
    slept: list[float] = []
    instance = cx.CodexClient(
        backoff_base=1, log_dir=tmp_path / "logs", sleep=slept.append
    )
    instance.slept = slept  # type: ignore[attr-defined]
    return instance


def _stub_runs(monkeypatch, outcomes):
    """subprocess.run을 순서대로 정해진 결과로 대체한다.

    `-o` 자리의 파일에 최종 메시지를 써 준다 — 어댑터가 텍스트를 거기서 읽기 때문이다.
    """
    calls: list[dict] = []
    queue = list(outcomes)

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, "input": kwargs.get("input")})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        stdout, returncode, last_message = item
        if last_message is not None:
            out_path = cmd[cmd.index("-o") + 1]
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(last_message)
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")

    monkeypatch.setattr(cx.subprocess, "run", fake_run)
    return calls


# --- 격리 계약 (ADR-0009·0011) --------------------------------------------


def test_isolation_flags_are_always_present(client, monkeypatch):
    calls = _stub_runs(monkeypatch, [(_events(), 0, "# 대본\n")])
    client.run("프롬프트", label="1-draft")

    cmd = calls[0]["cmd"]
    assert cmd[1] == "exec" and cmd[2] == "-"        # 프롬프트는 stdin이다
    assert calls[0]["input"] == "프롬프트"
    for flag in ("--skip-git-repo-check", "--ignore-user-config", "--ignore-rules", "--json"):
        assert flag in cmd, flag
    assert cmd[cmd.index("-s") + 1] == "read-only"    # 세션은 쓰지 못한다
    assert "-C" in cmd and "-o" in cmd                # 중립 작업 디렉터리 + 회수 파일


def test_ephemeral_is_always_used(client, monkeypatch):
    """이 엔진이 맡은 단계는 한 세션에 한 번에 끝난다 — 남길 대화 기록이 없다 (ADR-0097)."""
    calls = _stub_runs(monkeypatch, [(_events(), 0, "# 대본\n")])
    client.run("프롬프트", label="1-draft")
    assert "--ephemeral" in calls[0]["cmd"]


def test_workdir_is_neutral_and_temporary(client, monkeypatch):
    calls = _stub_runs(monkeypatch, [(_events(), 0, "# 대본\n")])
    client.run("프롬프트", label="1-draft")

    workdir = calls[0]["cmd"][calls[0]["cmd"].index("-C") + 1]
    # 프로젝트 안이 아니어야 한다 — CLAUDE.md/AGENTS.md 유입 방지 (ADR-0009)
    assert "shorts-factory" not in workdir.replace("\\", "/")


# --- 도구 매핑 -------------------------------------------------------------


def test_web_tools_become_web_search_flag(client, monkeypatch):
    """이 엔진에는 도구 이름 단위 제어가 없다 — 웹 on/off로 번역된다 (ADR-0097)."""
    calls = _stub_runs(monkeypatch, [(_events(), 0, "# 대본\n")])
    client.run("프롬프트", allowed_tools=("WebSearch", "WebFetch"), label="1-draft")

    cmd = calls[0]["cmd"]
    assert "tools.web_search=true" in cmd
    assert cmd[cmd.index("tools.web_search=true") - 1] == "-c"


def test_no_tools_means_no_web(client, monkeypatch):
    calls = _stub_runs(monkeypatch, [(_events(), 0, "# 대본\n")])
    client.run("프롬프트", label="2l-localize")
    assert "tools.web_search=true" not in calls[0]["cmd"]


def test_system_append_is_prepended_to_prompt(client, monkeypatch):
    """이 엔진에는 `--append-system-prompt`가 없다 — 계약을 버리지 않고 앞에 붙인다."""
    calls = _stub_runs(monkeypatch, [(_events(), 0, "# 대본\n")])
    client.run("본문", system_append="머리말", label="1-draft")
    assert calls[0]["input"].startswith("머리말")
    assert "본문" in calls[0]["input"]


# --- 산출 회수 -------------------------------------------------------------


def test_text_comes_from_output_file_and_session_id_from_events(client, monkeypatch):
    _stub_runs(monkeypatch, [(_events(thread_id="th-42"), 0, "# 대본\n\n## 대본\n한 줄.\n")])
    result = client.run("프롬프트", label="1-draft")

    assert result.text.startswith("# 대본")
    assert result.session_id == "th-42"       # 기록용 — 이 id로 이어 붙일 수는 없다
    assert result.num_turns == 1
    assert result.duration_ms is not None


def test_resume_is_refused_instead_of_silently_restarting(client, monkeypatch):
    """기록이 없는데 이어 붙이는 척하면 새 세션이 열려 입력 전부를 다시 읽는다 (ADR-0097).

    ADR-0044가 없애려던 비용이 바로 그 재독이라, 조용한 재실행보다 소리 나는 거절이 싸다 —
    프로세스를 아예 띄우지 않는다.
    """
    calls = _stub_runs(monkeypatch, [])
    with pytest.raises(LLMError, match="재청"):
        client.run("프롬프트", label="1-draft", resume="th-42")
    assert calls == []


def test_session_log_is_recorded(client, monkeypatch, tmp_path):
    _stub_runs(monkeypatch, [(_events(), 0, "# 대본\n")])
    client.run("프롬프트", label="1-draft")

    path = tmp_path / "logs" / "1-draft.codex.attempt1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["thread_id"] == "th-1"
    assert payload["returncode"] == 0
    assert payload["events"]


# --- 실패 처리 -------------------------------------------------------------


def test_turn_failed_raises(client, monkeypatch):
    _stub_runs(monkeypatch, [(_events(failure="사용자 취소"), 1, None)])
    with pytest.raises(LLMError, match="사용자 취소"):
        client.run("프롬프트", label="1-draft")


def test_rate_limit_retries_then_raises(client, monkeypatch):
    outcomes = [(_events(failure="429 too many requests"), 1, None)] * client.max_retries
    _stub_runs(monkeypatch, outcomes)
    with pytest.raises(LLMRateLimited):
        client.run("프롬프트", label="1-draft")
    assert len(client.slept) == client.max_retries - 1


def test_empty_answer_retries_then_succeeds(client, monkeypatch):
    _stub_runs(monkeypatch, [
        (_events(), 0, "   \n"),
        (_events(), 0, "# 대본\n"),
    ])
    result = client.run("프롬프트", label="1-draft")
    assert result.text.startswith("# 대본")


def test_timeout_attempts_are_capped(client, monkeypatch):
    _stub_runs(monkeypatch, [
        subprocess.TimeoutExpired("codex", 10),
        subprocess.TimeoutExpired("codex", 10),
    ])
    with pytest.raises(LLMTimeout):
        client.run("프롬프트", label="1-draft")
    assert len(client.slept) == cx.MAX_TIMEOUT_ATTEMPTS - 1


def test_missing_executable_names_the_login_step(monkeypatch):
    monkeypatch.setattr(cx.shutil, "which", lambda name: None)
    with pytest.raises(LLMError, match="codex login"):
        cx.CodexClient()


# --- 단계별 엔진 선택 (ADR-0097 결정) --------------------------------------


def test_script_stages_default_to_codex_and_the_rest_to_claude():
    from shorts_factory import cli
    from shorts_factory.stages.draft import STAGE as DRAFT
    from shorts_factory.stages.factcheck import STAGE as FACTCHECK
    from shorts_factory.stages.localize import STAGE as LOCALIZE
    from shorts_factory.stages.scenetable import STAGE as SCENETABLE

    args = cli.parse_args(["draft", "--slug", "x"])
    assert cli._engine_for(args, DRAFT) == "codex"        # 한국어 정본
    assert cli._engine_for(args, LOCALIZE) == "codex"     # ja·en 번안
    assert cli._engine_for(args, FACTCHECK) == "claude"   # 웹 검증은 그대로
    assert cli._engine_for(args, SCENETABLE) == "claude"
    assert cli._engine_for(args, "") == "claude"


def test_engine_flag_wins_over_stage_default():
    from shorts_factory import cli
    from shorts_factory.stages.draft import STAGE as DRAFT

    args = cli.parse_args(["draft", "--slug", "x", "--engine", "claude"])
    assert cli._engine_for(args, DRAFT) == "claude"

    args = cli.parse_args(["scenetable", "--slug", "x", "--engine", "codex"])
    assert cli._engine_for(args, "3s-scenetable") == "codex"


def test_make_client_picks_the_adapter(monkeypatch, tmp_path):
    from shorts_factory import cli
    from shorts_factory.stages.draft import STAGE as DRAFT

    monkeypatch.setattr(cx.shutil, "which", lambda name: r"C:\fake\codex.exe")
    monkeypatch.setattr(
        "shorts_factory.llm.claude_code.shutil.which", lambda name: r"C:\fake\claude.exe"
    )
    args = cli.parse_args(["draft", "--slug", "x"])
    assert isinstance(cli._make_client(args, tmp_path, DRAFT), cx.CodexClient)
    assert isinstance(cli._make_client(args, tmp_path, "2-factcheck"), cli.ClaudeCodeClient)
