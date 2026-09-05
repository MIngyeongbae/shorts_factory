"""대본 파일명 — `stages/scriptmd.py`와 `judgment.py`가 같은 값을 보게 하는 자리.

`judgment.py`가 `stages/`를 끌어오면 `stages/__init__` → `stages/tts.py` → `judgment`로
임포트가 돈다. 파일명 둘만 여기 두고 `scriptmd.py`가 재수출한다 (specs/05 경계 절, ADR-0056).
"""

from __future__ import annotations

SCRIPT_MD_FILE = "script.md"
SCRIPT_MD_PATTERN = "script.{lang}.md"
