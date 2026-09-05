"""`script.md` 맨 위의 판정 블록 — 사람은 주석만 푼다 (ADR-0094).

    //reject
    [
    //ko,
    //ja,
    //en
    ]
    # {소재 제목}
    …

`[1] draft`가 전부 주석인 채로 붙이고, 사람이 **주석을 푸는** 것이 판정이다. `reject`가
풀리면 반려(언어 줄과 무관하게 이긴다), 언어가 풀리면 그 언어가 2부 대상, 아무것도 안
풀렸으면 보류다. 꼴의 정본은 스펙 01이고 의미의 정본은 스펙 07이다.

이 모듈이 `stages/`가 아니라 여기 있는 이유는 **`judgment.py`가 읽기 때문이다** —
`stages/__init__`이 단계 전부를 끌어오고 `stages/tts.py`가 `judgment`를 끌어오므로,
파서가 `stages/` 안에 있으면 임포트가 돈다. 파싱은 스키마(`LANGUAGES`)만 안다.

블록이 **없는** 대본은 옛 편이다 — 이미 판정을 거쳤으므로 있는 언어 전부가 대상이다
(`Gate.present`가 거짓). 그 편들에 블록을 소급해 붙이지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from .schemas.timed_scenes import LANGUAGES

#: 주석 표시. 사람이 지우는 문자열이다.
GATE_COMMENT = "//"
#: 반려 토큰.
GATE_REJECT = "reject"
_OPEN, _CLOSE = "[", "]"
#: 한 줄 = `//`(선택) + 토큰 + `,`(선택). 앞뒤 공백은 호출자가 뗀다.
_TOKEN_RE = re.compile(r"^(?P<comment>//)?\s*(?P<word>[a-z]+)\s*,?$")


class GateError(Exception):
    """블록이 있는데 읽을 수 없다 — 닫히지 않았거나 모르는 토큰이 있다."""


@dataclass(frozen=True)
class Gate:
    """판정. `present`가 거짓이면 블록이 없는 옛 편이고 나머지 값은 뜻이 없다."""

    present: bool = False
    rejected: bool = False
    #: 주석이 풀린 언어, `LANGUAGES` 순서.
    languages: tuple[str, ...] = ()

    @property
    def pending(self) -> bool:
        """블록은 있는데 아무것도 안 풀렸다 — 2부의 어느 단계도 돌지 않는다."""
        return self.present and not self.rejected and not self.languages

    @property
    def status(self) -> str:
        """`STATUS.md` 머리글로의 투영 (ADR-0094 결정 8). 옛 편은 보류로 보인다."""
        if self.rejected:
            return "no-go"
        if self.languages:
            return "go"
        return "보류"


def render_gate_block(languages: Sequence[str] = LANGUAGES) -> str:
    """전부 주석인 블록. `[1]`이 대본 앞에 붙인다."""
    body = [f"{GATE_COMMENT}{lang}," for lang in languages[:-1]]
    body.append(f"{GATE_COMMENT}{languages[-1]}")
    return "\n".join([f"{GATE_COMMENT}{GATE_REJECT}", _OPEN, *body, _CLOSE]) + "\n"


def split_gate_block(text: str) -> tuple[str, str]:
    """`(블록 원문, 나머지)`. 블록이 없으면 `('', text)`.

    블록은 첫 비어 있지 않은 줄이 `reject` 토큰일 때만 있다 — `# 제목`으로 시작하는
    대본은 그대로 나머지다. 블록 앞의 빈 줄은 버린다.
    """
    lines = text.split("\n")
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start >= len(lines):
        return "", text
    head = _TOKEN_RE.match(lines[start].strip())
    if not head or head.group("word") != GATE_REJECT:
        return "", text
    end = start + 1
    while end < len(lines) and lines[end].strip() != _CLOSE:
        end += 1
    if end >= len(lines):
        raise GateError("판정 블록이 `]`로 닫히지 않았다 (스펙 01, ADR-0094)")
    block = "\n".join(lines[start : end + 1]) + "\n"
    rest = "\n".join(lines[end + 1 :])
    return block, rest


def strip_gate_block(text: str) -> str:
    """블록을 뗀 대본. 세션에 실을 때 쓴다 — 세션이 블록을 보면 산출에 베껴 쓴다."""
    return split_gate_block(text)[1]


def attach_gate_block(text: str, block: str) -> str:
    """`[2]`가 정정본을 쓸 때 옛 블록을 되붙인다. 블록이 비면 그대로다."""
    return (block + text) if block else text


def parse_gate(text: str) -> Gate:
    block, _rest = split_gate_block(text)
    if not block:
        return Gate()
    rejected = False
    chosen: list[str] = []
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line in (_OPEN, _CLOSE):
            continue
        match = _TOKEN_RE.match(line)
        if not match:
            raise GateError(f"판정 블록의 줄을 읽을 수 없다: {raw!r}")
        word, commented = match.group("word"), bool(match.group("comment"))
        if word == GATE_REJECT:
            rejected = not commented
        elif word in LANGUAGES:
            if not commented:
                chosen.append(word)
        else:
            raise GateError(
                f"판정 블록에 모르는 값이 있다: {word!r} "
                f"(가능: {GATE_REJECT}, {', '.join(LANGUAGES)})"
            )
    return Gate(
        present=True,
        rejected=rejected,
        languages=tuple(lang for lang in LANGUAGES if lang in chosen),
    )


__all__ = [
    "GATE_COMMENT",
    "GATE_REJECT",
    "Gate",
    "GateError",
    "attach_gate_block",
    "parse_gate",
    "render_gate_block",
    "split_gate_block",
    "strip_gate_block",
]
