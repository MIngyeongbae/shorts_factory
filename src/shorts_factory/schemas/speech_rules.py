"""발화형 규칙 — `specs/schema/speech-rules.json`을 로드한다 (ADR-0063).

**이 모듈은 값을 선언하지 않는다.** 수사표·단위 사전은 전부 계약 파일에 있고 여기는
그것을 꺼내 주는 창구다 (ADR-0034 §3). 사전 항목 하나를 빼면 그 토큰이 엔진 기본
동작으로 돌아가는 성질(ADR-0063 되돌릴 조건 2)은 코드가 표를 들지 않을 때만 산다.

읽는 곳은 `tts/speech.py` 하나다.
"""

from __future__ import annotations

from typing import Any

from . import vocab

SPEECH_RULES: dict[str, Any] = vocab.load("speech-rules.json")


def _public(block: dict[str, Any]) -> dict[str, Any]:
    """`_`로 시작하는 키는 사람에게 하는 설명이라 뺀다 (specs/schema/README 규칙 4)."""
    return {key: value for key, value in block.items() if not key.startswith("_")}


def languages() -> tuple[str, ...]:
    """로케일 블록이 있는 언어."""
    return tuple(_public(SPEECH_RULES["locales"]))


def locale(lang: str) -> dict[str, Any] | None:
    """그 언어의 규칙 블록. 없으면 `None` — 변환하지 않는다는 뜻이다.

    모르는 언어를 실패로 만들지 않는다. 발화형은 **품질 개선이지 계약이 아니다** —
    블록이 없으면 대본 줄이 그대로 TTS에 간다 (ADR-0063 이전의 동작).
    """
    block = SPEECH_RULES["locales"].get(lang)
    return None if block is None else _public(block)


def reading_section() -> str:
    """읽기 절의 이름 (`## 읽기`). 절 이름은 로케일과 무관하다 — 대본 포맷의 것이다."""
    return SPEECH_RULES["reading_section"]


def reading_line(lang: str) -> dict[str, Any] | None:
    """그 언어가 요구하는 읽기 절 규칙. 없으면 `None` — 읽기 절을 요구하지 않는다.

    블록의 유무가 곧 "이 언어의 대본에 `## 읽기` 절이 있어야 하는가"다 (ADR-0073 결정 1).
    코드가 `"ja"`를 손으로 들지 않게 하려는 것이다 — 로케일이 늘면 계약만 고친다.
    """
    rules = (locale(lang) or {}).get("reading_line")
    return None if rules is None else _public(rules)


def reading_languages() -> tuple[str, ...]:
    """읽기 절을 요구하는 언어들."""
    return tuple(lang for lang in languages() if reading_line(lang))


#: 어느 로케일에나 문장 끝인 부호. 로케일이 자기 부호를 더한다 (`sentence_end`).
BASE_SENTENCE_ENDINGS = (".", "?", "!", "…")


def sentence_endings() -> tuple[str, ...]:
    """문장 끝으로 인정하는 부호 전부 (ADR-0089).

    라틴 넷은 코드가 들고, **로케일 부호는 계약에서 온다** (ADR-0034) — 일본어 `。`가
    빠져 있으면 `[3]`이 일본어 줄마다 *"줄이 문장부호로 끝나지 않는다"*고 거짓 경고를
    낸다. `tts/speech.py`(부호를 붙이는 쪽)와 `tts/sync.py`(씬 끝을 찾는 쪽)가 **같은
    집합**을 봐야 한다 — 갈리면 붙여 놓고도 못 알아본다.
    """
    marks = list(BASE_SENTENCE_ENDINGS)
    for lang in languages():
        mark = str((locale(lang) or {}).get("sentence_end") or "")
        if mark and mark not in marks:
            marks.append(mark)
    return tuple(marks)


def range_chars() -> tuple[str, ...]:
    """범위·부호 문자. 숫자 양옆에 붙으면 그 숫자를 건드리지 않는다 (ADR-0063 결정 4)."""
    return tuple(SPEECH_RULES["range_chars"])


def units(lang: str) -> tuple[tuple[str, dict[str, Any]], ...]:
    """단위 사전을 **긴 키부터** 돌려준다.

    긴 것부터 맞춰야 `mm`이 `m`에, `년대`가 `년`에 먹히지 않는다. 정렬을 호출부에
    맡기면 한 곳만 빠뜨려도 조용히 틀린 읽기가 나오므로 여기서 한 번에 정한다.
    """
    block = locale(lang)
    if not block:
        return ()
    table = _public(block.get("units", {}))
    return tuple(sorted(table.items(), key=lambda kv: len(kv[0]), reverse=True))


def system(lang: str, name: str) -> dict[str, Any]:
    """그 언어의 수사 체계 하나 (`systems.{name}`). 없으면 계약이 깨진 것이다."""
    block = locale(lang) or {}
    try:
        return _public(block["systems"][name])
    except KeyError as exc:  # pragma: no cover - 계약 파일이 깨진 경우
        raise KeyError(
            f"speech-rules.json locales.{lang}.systems에 '{name}'이 없다"
        ) from exc
