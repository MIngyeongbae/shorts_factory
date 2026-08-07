"""LLM 텍스트 출력에서 JSON 객체를 추출한다.

헤드리스 세션에 "JSON만 출력"을 지시해도 코드펜스나 짧은 서문이 붙는 경우가 있다.
파이프라인 계약은 JSON이므로 관용적으로 추출하되, 실패하면 명확히 실패시킨다.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class JSONExtractionError(ValueError):
    """텍스트에서 유효한 JSON 객체를 찾지 못함."""


def _balanced_object(text: str) -> str | None:
    """첫 번째 최상위 `{...}` 블록을 문자열 리터럴을 존중하며 잘라낸다."""
    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None


def extract_json_object(text: str) -> dict[str, Any]:
    """텍스트에서 JSON 객체 하나를 파싱한다."""
    if not text or not text.strip():
        raise JSONExtractionError("빈 응답이다")

    candidates: list[str] = []

    stripped = text.strip()
    candidates.append(stripped)

    for match in _FENCE_RE.finditer(text):
        candidates.append(match.group(1).strip())

    balanced = _balanced_object(text)
    if balanced:
        candidates.append(balanced)

    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    preview = text.strip()[:300]
    raise JSONExtractionError(f"JSON 객체를 찾지 못했다. 응답 앞부분: {preview!r}")


def dump_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
