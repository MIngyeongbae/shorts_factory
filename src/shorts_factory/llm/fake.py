"""테스트용 LLM 어댑터.

픽스처 텍스트를 순서대로 반환한다. 단계 로직(스키마 검증·verdict 분기·재시작)을
네트워크나 구독 한도 없이 검증하기 위한 것이다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Sequence

from .base import LLMClient, LLMError, LLMResult


class FakeLLMClient(LLMClient):
    def __init__(
        self,
        responses: Sequence[str | Callable[[str], str] | Exception],
        *,
        num_turns: int = 4,
    ) -> None:
        self._responses = list(responses)
        self.num_turns = num_turns  # 도구 사용 여부 감지 테스트용
        self.calls: list[dict] = []

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
        self.calls.append(
            {
                "prompt": prompt,
                "allowed_tools": tuple(allowed_tools),
                "label": label,
                "system_append": system_append,
                "add_dirs": tuple(add_dirs),
            }
        )
        if not self._responses:
            raise LLMError(f"준비된 픽스처 응답이 없다 (label={label})")

        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        text = item(prompt) if callable(item) else item
        return LLMResult(
            text=text, session_id=f"fake-{len(self.calls)}", num_turns=self.num_turns
        )
