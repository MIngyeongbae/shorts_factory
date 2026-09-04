"""codex 헤드리스(`codex exec`) 어댑터. ADR-0097.

- **ChatGPT 플랜 인증만 쓴다.** `codex login`이 남긴 `~/.codex/auth.json`이 정한다
  (`auth_mode=chatgpt`). API 키 경로(`codex login --with-api-key`)는 쓰지 않으며,
  `.env`에 `OPENAI_API_KEY`를 두면 자식 프로세스가 물려받아 종량제로 샌다 (ADR-0097 금지선).
- 격리 계약은 클로드 어댑터와 같다 (ADR-0009·0011): 중립 임시 작업 디렉터리(`-C`),
  읽기 전용 샌드박스(`-s read-only`), 프로젝트·사용자 지침 차단(`--ignore-user-config`
  `--ignore-rules`). 클로드 쪽이 임시 디렉터리로 우회하던 것을 이 엔진은 플래그로 건다.
- 파일은 이 어댑터가 아니라 호출자(Python)가 쓴다. 세션은 읽기 전용이라 산출물 경로를
  파이프라인이 통제한다.
- **`--ephemeral`을 항상 켠다.** 이 엔진이 맡은 `[1] draft`·`[2l] localize`는 한 세션에
  한 번에 끝나 재청할 일이 없으므로, 대화 기록을 남길 이유가 없다. 대신 **재청 요청은
  명시적으로 거절한다** — 기록이 없는데 이어 붙이는 척하면 새 세션이 열려 처음부터 다시
  돈다. 조용한 재실행보다 소리 나는 거절이 싸다 (ADR-0044·0097).
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

#: 한도/일시적 과부하 신호. **이 엔진의 실측 문구는 아직 없다** (ADR-0097 결과) —
#: 공통 신호로 시작하고, 클로드 어댑터가 `session limit`을 실측으로 얻은 것처럼
#: 실제로 한도에 걸린 문구가 관측되면 여기에 더한다.
_RETRYABLE_PATTERNS = (
    r"usage limit",
    r"rate.?limit",
    r"quota",
    r"limit reached",
    r"limit exceeded",
    r"too many requests",
    r"overloaded",
    r"service unavailable",
    r"try again later",
    r"\b(429|503|529)\b",
)

_RETRYABLE_RE = re.compile("|".join(_RETRYABLE_PATTERNS), re.IGNORECASE)

#: 타임아웃으로 도는 시도의 상한 (ADR-0048). 클로드 어댑터와 같은 이유·같은 값이다 —
#: 한도 재시도는 대기가 비용의 전부지만 타임아웃 재시도는 회당 타임아웃을 통째로 태운다.
MAX_TIMEOUT_ATTEMPTS = 2


def _is_retryable(*chunks: str) -> bool:
    return any(chunk and _RETRYABLE_RE.search(chunk) for chunk in chunks)


class CodexClient(LLMClient):
    def __init__(
        self,
        executable: str = "codex",
        *,
        model: str | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: int = DEFAULT_BACKOFF_BASE,
        default_timeout: int = DEFAULT_LLM_TIMEOUT,
        log_dir: Path | None = None,
        sleep=time.sleep,
    ) -> None:
        resolved = shutil.which(executable)
        if resolved is None:
            raise LLMError(
                f"codex 실행 파일을 찾을 수 없다: {executable!r}. "
                "Codex CLI 설치와 PATH를 확인하라 (ADR-0097). "
                "설치 뒤 `codex login`으로 ChatGPT 플랜 인증이 필요하다."
            )
        self.executable = resolved
        self.model = model
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.default_timeout = default_timeout
        self.log_dir = log_dir
        self._sleep = sleep

    # --- 내부 -------------------------------------------------------------

    def _build_cmd(
        self,
        allowed_tools: Sequence[str],
        workdir: Path,
        out_path: Path,
    ) -> list[str]:
        cmd = [
            self.executable, "exec", "-",
            "--ephemeral",            # 세션 파일을 남기지 않는다 — 재청이 없는 단계들이다
            "--skip-git-repo-check",
            "--ignore-user-config",   # $CODEX_HOME/config.toml 유입 차단 (인증은 그대로)
            "--ignore-rules",         # 사용자·프로젝트 .rules 유입 차단
            "-s", "read-only",        # 세션은 쓰지 못한다 — 파일은 호출자가 쓴다 (ADR-0011)
            "-C", str(workdir),       # 중립 작업 디렉터리 (ADR-0009)
            "-o", str(out_path),      # 최종 메시지만 파일로 회수한다
            "--json",                 # 이벤트 JSONL — logs/에 그대로 남긴다
        ]
        if allowed_tools:
            # **이 엔진에는 도구 이름 단위 제어가 없다** — 있는 것은 웹 on/off뿐이다.
            # `WEB_TOOLS`("WebSearch","WebFetch")는 "웹 켬"으로 번역된다 (ADR-0097).
            cmd += ["-c", "tools.web_search=true"]
        if self.model:
            cmd += ["-m", self.model]
        return cmd

    #: 라벨에 들어오지만 파일 이름에 못 쓰는 문자. 클로드 어댑터와 같은 이유다 —
    #: Windows에서 `a:b`는 NTFS 대체 데이터 스트림이라 기록이 조용히 사라진다.
    _UNSAFE_IN_FILENAME = ':*?"<>|/\\'

    @classmethod
    def _safe_name(cls, label: str) -> str:
        for char in cls._UNSAFE_IN_FILENAME:
            label = label.replace(char, "-")
        return label

    def _record(self, label: str, attempt: int, payload: dict[str, Any]) -> None:
        if not self.log_dir or not label:
            return
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / f"{self._safe_name(label)}.codex.attempt{attempt}.json"
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")

    @staticmethod
    def _parse_events(stdout: str) -> tuple[list[dict[str, Any]], str | None, str, int]:
        """JSONL 이벤트 → (이벤트, thread_id, 실패 사유, 완료한 턴 수).

        파싱 못 하는 줄은 버린다 — 이벤트 스트림은 기록이지 계약이 아니다.
        """
        events: list[dict[str, Any]] = []
        thread_id: str | None = None
        failure = ""
        turns = 0
        for raw in stdout.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            events.append(event)
            kind = event.get("type")
            if kind == "thread.started":
                thread_id = event.get("thread_id") or thread_id
            elif kind == "turn.completed":
                turns += 1
            elif kind == "turn.failed":
                error = event.get("error")
                failure = (
                    error.get("message", "") if isinstance(error, dict) else str(error or "")
                )
        return events, thread_id, failure, turns

    def _invoke(
        self, cmd: list[str], prompt: str, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
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
        resume: str | None = None,
    ) -> LLMResult:
        timeout = timeout or self.default_timeout
        if resume:
            # `--ephemeral`로 도는 엔진이라 이어 붙일 기록이 없다. 새 세션을 조용히
            # 여는 대신 여기서 멈춘다 — 재청이 필요한 단계는 claude로 돌린다 (ADR-0097).
            raise LLMError(
                "codex 어댑터는 재청(resume)을 지원하지 않는다 — 세션 기록을 남기지 "
                "않는다 (ADR-0097). 재청이 필요한 단계는 `--engine claude`로 돌려라."
            )
        if system_append:
            # 이 엔진에는 `--append-system-prompt`가 없다. 계약을 조용히 버리지 않고
            # 프롬프트 앞에 붙인다 — 세션이 보는 내용은 같다.
            prompt = f"{system_append}\n\n{prompt}"
        if add_dirs:
            # `--add-dir`은 **쓰기** 경로를 여는 플래그라 ADR-0012의 읽기 경로와 뜻이 다르다.
            # 읽기 전용 샌드박스는 이미 읽을 수 있으므로 열 것이 없다 — 기록만 남긴다.
            log.debug(
                "[%s] add_dirs %s — codex 읽기 전용 샌드박스는 별도 개방이 필요 없다",
                label or "-", [str(d) for d in add_dirs],
            )

        last_error = ""
        timeouts = 0

        for attempt in range(1, self.max_retries + 1):
            log.info("codex 세션 실행 [%s] 시도 %d/%d", label or "-", attempt, self.max_retries)
            started = time.monotonic()
            with tempfile.TemporaryDirectory(prefix="sf-codex-") as tmp:
                workdir = Path(tmp) / "work"
                workdir.mkdir()
                out_path = Path(tmp) / "last-message.md"
                cmd = self._build_cmd(allowed_tools, workdir, out_path)
                try:
                    proc = self._invoke(cmd, prompt, timeout)
                except LLMTimeout as exc:
                    last_error = str(exc)
                    log.warning("[%s] %s", label or "-", last_error)
                    timeouts += 1
                    if timeouts >= MAX_TIMEOUT_ATTEMPTS or attempt >= self.max_retries:
                        raise
                    self._backoff(attempt, label)
                    continue

                duration_ms = int((time.monotonic() - started) * 1000)
                stdout, stderr = proc.stdout or "", proc.stderr or ""
                events, thread_id, failure, turns = self._parse_events(stdout)
                text = out_path.read_text(encoding="utf-8") if out_path.exists() else ""

            self._record(
                label,
                attempt,
                {
                    "cmd": cmd,
                    "returncode": proc.returncode,
                    "thread_id": thread_id,
                    "duration_ms": duration_ms,
                    "events": events,
                    "stderr": stderr[:4000],
                },
            )

            is_error = bool(failure) or proc.returncode != 0
            if is_error:
                last_error = (
                    f"세션 오류 (exit={proc.returncode}): "
                    f"{(failure or stderr).strip()[:500]}"
                )
                if _is_retryable(failure, stderr, text):
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
                # 기록용이다 — `--ephemeral`이라 이 id로 이어 붙일 수는 없다
                session_id=thread_id,
                num_turns=turns or None,
                duration_ms=duration_ms,
                raw={"events": events},
            )

        raise LLMError(last_error or "알 수 없는 이유로 세션에 실패했다")

    def _backoff(self, attempt: int, label: str, *, rate_limited: bool = False) -> None:
        delay = self.backoff_base * (2 ** (attempt - 1))
        delay += random.uniform(0, self.backoff_base * 0.25)  # thundering herd 방지
        reason = "사용 한도" if rate_limited else "일시 오류"
        log.warning("[%s] %s — %.0f초 대기 후 재시도", label or "-", reason, delay)
        self._sleep(delay)
