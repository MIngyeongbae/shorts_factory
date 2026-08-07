"""Claude Code 헤드리스(`claude -p`) 어댑터. ADR-0008.

- 구독 인증 경로만 사용한다. API 키 종량제 플래그(`--bare` 등)는 쓰지 않는다.
- 세션마다 중립 임시 디렉터리에서 실행한다. 프로젝트 CLAUDE.md가 조사·검증·비판
  세션에 섞여 들어가면 ADR-0009가 요구한 세션 독립성이 깨지기 때문이다.
- 파일은 이 어댑터가 아니라 호출자(Python)가 쓴다. 세션에는 읽기 도구만 허용해
  산출물 경로를 파이프라인이 통제한다.
"""

from __future__ import annotations

import json
import logging
import random
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence

from ..config import DEFAULT_BACKOFF_BASE, DEFAULT_LLM_TIMEOUT, DEFAULT_MAX_RETRIES
from .base import LLMClient, LLMError, LLMRateLimited, LLMResult, LLMTimeout

log = logging.getLogger(__name__)

#: 구독 한도/일시적 과부하 신호. 매칭되면 백오프 후 재시도한다.
_RETRYABLE_PATTERNS = (
    r"usage limit",
    r"rate.?limit",
    r"limit reached",
    r"limit exceeded",
    r"too many requests",
    r"overloaded",
    r"service unavailable",
    r"try again later",
    r"\b(429|503|529)\b",
)

_RETRYABLE_RE = re.compile("|".join(_RETRYABLE_PATTERNS), re.IGNORECASE)


def _is_retryable(*chunks: str) -> bool:
    return any(chunk and _RETRYABLE_RE.search(chunk) for chunk in chunks)


class ClaudeCodeClient(LLMClient):
    def __init__(
        self,
        executable: str = "claude",
        *,
        model: str | None = None,
        permission_mode: str = "dontAsk",
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: int = DEFAULT_BACKOFF_BASE,
        default_timeout: int = DEFAULT_LLM_TIMEOUT,
        log_dir: Path | None = None,
        sleep=time.sleep,
    ) -> None:
        resolved = shutil.which(executable)
        if resolved is None:
            raise LLMError(
                f"claude 실행 파일을 찾을 수 없다: {executable!r}. "
                "Claude Code CLI 설치와 PATH를 확인하라 (ADR-0008)."
            )
        self.executable = resolved
        self.model = model
        self.permission_mode = permission_mode
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.default_timeout = default_timeout
        self.log_dir = log_dir
        self._sleep = sleep

    # --- 내부 -------------------------------------------------------------

    def _build_cmd(
        self,
        allowed_tools: Sequence[str],
        system_append: str,
        add_dirs: Sequence[Path] = (),
    ) -> list[str]:
        cmd = [
            self.executable,
            "-p",
            "--output-format", "json",
            "--permission-mode", self.permission_mode,
            "--disable-slash-commands",
        ]
        if allowed_tools:
            # 가변 인자 옵션이므로 콤마로 합쳐 하나의 토큰으로 넘긴다
            cmd += ["--allowedTools", ",".join(allowed_tools)]
        for directory in add_dirs:
            # 작업 디렉터리는 중립 임시 경로로 유지한 채 읽기 경로만 연다 (ADR-0012)
            cmd += ["--add-dir", str(directory)]
        if self.model:
            cmd += ["--model", self.model]
        if system_append:
            cmd += ["--append-system-prompt", system_append]
        return cmd

    def _record(self, label: str, attempt: int, payload: dict[str, Any]) -> None:
        if not self.log_dir or not label:
            return
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / f"{label}.attempt{attempt}.json"
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")

    def _invoke(
        self, cmd: list[str], prompt: str, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        # 중립 작업 디렉터리: 프로젝트 CLAUDE.md/설정이 세션에 유입되지 않게 한다
        with tempfile.TemporaryDirectory(prefix="sf-session-") as workdir:
            try:
                return subprocess.run(
                    cmd,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    cwd=workdir,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise LLMTimeout(f"세션이 {timeout}초 안에 끝나지 않았다") from exc

    # --- 공개 API ---------------------------------------------------------

    def run(
        self,
        prompt: str,
        *,
        allowed_tools: Sequence[str] = (),
        timeout: int | None = None,
        system_append: str = "",
        label: str = "",
        add_dirs: Sequence[Path] = (),
    ) -> LLMResult:
        timeout = timeout or self.default_timeout
        cmd = self._build_cmd(allowed_tools, system_append, add_dirs)
        last_error = ""

        for attempt in range(1, self.max_retries + 1):
            log.info("헤드리스 세션 실행 [%s] 시도 %d/%d", label or "-", attempt, self.max_retries)
            try:
                proc = self._invoke(cmd, prompt, timeout)
            except LLMTimeout as exc:
                last_error = str(exc)
                log.warning("[%s] %s", label or "-", last_error)
                if attempt >= self.max_retries:
                    raise
                self._backoff(attempt, label)
                continue

            stdout, stderr = proc.stdout or "", proc.stderr or ""
            envelope: dict[str, Any] | None = None
            try:
                envelope = json.loads(stdout)
            except json.JSONDecodeError:
                envelope = None

            self._record(
                label,
                attempt,
                {
                    "cmd": cmd,
                    "returncode": proc.returncode,
                    "envelope": envelope,
                    "stdout_raw": None if envelope else stdout[:4000],
                    "stderr": stderr[:4000],
                },
            )

            if envelope is None:
                last_error = (
                    f"JSON 응답을 파싱할 수 없다 (exit={proc.returncode}). "
                    f"stderr={stderr.strip()[:500]!r} stdout={stdout.strip()[:500]!r}"
                )
                if _is_retryable(stdout, stderr) and attempt < self.max_retries:
                    self._backoff(attempt, label)
                    continue
                raise LLMError(last_error)

            text = envelope.get("result") or ""
            is_error = bool(envelope.get("is_error")) or proc.returncode != 0

            if is_error:
                subtype = envelope.get("subtype", "")
                last_error = f"세션 오류 (subtype={subtype}): {text.strip()[:500]}"
                if _is_retryable(text, stderr, subtype):
                    if attempt >= self.max_retries:
                        raise LLMRateLimited(
                            f"사용 한도/과부하로 {self.max_retries}회 재시도 후 실패: {last_error}"
                        )
                    self._backoff(attempt, label, rate_limited=True)
                    continue
                raise LLMError(last_error)

            if not text.strip():
                last_error = "세션이 빈 응답을 반환했다"
                if attempt >= self.max_retries:
                    raise LLMError(last_error)
                self._backoff(attempt, label)
                continue

            return LLMResult(
                text=text,
                session_id=envelope.get("session_id"),
                num_turns=envelope.get("num_turns"),
                duration_ms=envelope.get("duration_ms"),
                raw=envelope,
            )

        raise LLMError(last_error or "알 수 없는 이유로 세션에 실패했다")

    def _backoff(self, attempt: int, label: str, *, rate_limited: bool = False) -> None:
        delay = self.backoff_base * (2 ** (attempt - 1))
        delay += random.uniform(0, self.backoff_base * 0.25)  # thundering herd 방지
        reason = "사용 한도" if rate_limited else "일시 오류"
        log.warning("[%s] %s — %.0f초 대기 후 재시도", label or "-", reason, delay)
        self._sleep(delay)
