"""LLM 호출 어댑터 인터페이스.

ADR-0008: LLM 단계는 Claude Code 헤드리스(구독)로 실행하되, 한도가 병목이 되면
해당 단계만 API 종량제로 전환할 수 있도록 **호출 레이어만 교체 가능한** 어댑터를
유지한다. 단계 코드는 이 인터페이스에만 의존한다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence


class LLMError(Exception):
    """LLM 호출 실패 일반."""


class LLMRateLimited(LLMError):
    """구독 사용 한도 도달. 파이프라인은 중단이 아니라 지연으로 처리한다 (ADR-0008)."""


class LLMTimeout(LLMError):
    """세션이 제한 시간 내에 끝나지 않음."""


@dataclass
class LLMResult:
    """헤드리스 세션 1회의 결과."""

    text: str
    session_id: str | None = None
    num_turns: int | None = None
    duration_ms: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def meta(self) -> dict[str, Any]:
        """run 상태에 남길 추적 정보."""
        return {
            "session_id": self.session_id,
            "num_turns": self.num_turns,
            "duration_ms": self.duration_ms,
        }


class LLMClient(ABC):
    """단계 코드가 보는 유일한 LLM 표면."""

    @abstractmethod
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
        """프롬프트 1건을 **독립 세션**으로 실행하고 최종 텍스트를 돌려준다.

        세션 간 컨텍스트는 공유되지 않는다 (ADR-0009 자기 검증 오염 방지).

        `add_dirs`는 세션이 Read할 수 있게 열어줄 디렉터리다 (ADR-0012의 소스 카드).
        작업 디렉터리는 여전히 중립 임시 경로이고 도구도 읽기 전용이므로,
        ADR-0011의 격리 계약은 그대로다.
        """
        raise NotImplementedError
