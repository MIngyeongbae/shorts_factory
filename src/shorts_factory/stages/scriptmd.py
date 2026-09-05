"""script.md — 1부 대본 정본의 파싱·기계 검사·프롬프트 배선 (ADR-0049).

포맷의 정본은 specs/01-script-template.md다. 기계는 `## 대본` 절의 비어 있지 않은
줄만 센다 (한 줄 = 자막 한 줄 ≈ 씬 하나, ADR-0013). 검사는 LLM 세션이 아니라 여기
코드가 한다 — `[1. draft]` 직후, 초 단위, 값은 전부 `specs/schema/script-rules.json`
에서 로드한다 (ADR-0034 §3).

프롬프트 배선(load_prompt·format_limits)은 공용 배선인
session.py에서 재수출한다. 단계끼리는 여전히 파일로만 통신한다 (ADR-0011).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from ..gate import (  # noqa: F401 — 재수출 (ADR-0094)
    Gate,
    GateError,
    attach_gate_block,
    parse_gate,
    render_gate_block,
    split_gate_block,
    strip_gate_block,
)
from ..schemas import speech_rules, vocab
from ..schemas.script_rules import core_chars, max_total_seconds, noun_stems
from ..schemas.timed_scenes import PRIMARY_LANGUAGE
from .session import format_limits, load_prompt  # noqa: F401 — 재수출


#: 시드 기사 열람·교차 확인용. 산출물 파일은 오케스트레이터가 쓴다 (ADR-0011).
WEB_TOOLS: tuple[str, ...] = ("WebSearch", "WebFetch")

#: 수미상관을 볼 때 앞뒤로 떼는 비율 (schemas/script_rules.py와 같은 값)
EDGE_RATIO = 0.15

SCRIPT_HEADING = "대본"
CLAIMS_HEADING = "주장"
#: TTS가 읽을 줄이 담기는 절 (ADR-0073). 절 이름은 `speech-rules.json`이 정하고, 어느
#: 언어가 이 절을 갖는지는 그 파일의 `locales.{lang}.reading_line` 블록이 정한다 —
#: 코드는 언어 이름을 손으로 들지 않는다 (ADR-0034 §3).
READING_HEADING = speech_rules.reading_section()
FITNESS_KEY = "매체 적합성"
UNFIT_MARK = "부적합"

#: 대본 파일명 (specs/05 1부↔2부 경계 절, ADR-0056) — ko는 `script.md`, 나머지는
#: `script.{lang}.md`. 2부(`stages/tts.py`)도 같은 이름을 읽는다 — 파일명이 계약이다.
#: 값은 `stages_script_name.py`에 있다 — `judgment.py`가 `stages/` 없이 읽어야 해서다.
from ..stages_script_name import SCRIPT_MD_FILE, SCRIPT_MD_PATTERN  # noqa: E402,F401


def script_md_name(lang: str) -> str:
    """그 언어의 대본 파일명."""
    return SCRIPT_MD_FILE if lang == PRIMARY_LANGUAGE else SCRIPT_MD_PATTERN.format(lang=lang)


#: `[2. factcheck]` 세션 출력의 절 구분 마커 (prompts/02-factcheck.md와 계약)
FACTCHECK_MARK = "=== FACTCHECK ==="
SCRIPT_MARK = "=== SCRIPT ==="

#: `[2l. localize]` 세션 출력의 언어별 절 마커 — `=== SCRIPT.ja ===`
#: (prompts/02l-localize.md와 계약). 파일명과 같은 꼴이라 사람이 읽어도 어느 파일인지 보인다.
LOCALIZED_MARK = "=== SCRIPT.{lang} ==="

#: 읽기 절만 만드는 경로의 마커 — `=== READING.ja ===` (ADR-0073 결정 7). 대본이 이미
#: 있어 다시 만들지 않는 언어의 `## 읽기` 절만 세션이 낸다. 절 안은 마크다운 문서가
#: 아니라 **줄 목록**이다.
READING_MARK = "=== READING.{lang} ==="

#: 두 마커를 한 정규식으로 찾는다 — 한 세션 출력에 둘이 섞여 나올 수 있고, SCRIPT 절이
#: 끝나는 자리가 READING 마커일 수 있기 때문이다. 따로 찾으면 SCRIPT 절이 뒤의 READING
#: 블록을 통째로 삼킨다.
_MARK_RE = re.compile(r"^=== (SCRIPT|READING)\.([a-z]{2}) ===[ \t]*$", re.MULTILINE)


def localized_mark(lang: str) -> str:
    return LOCALIZED_MARK.format(lang=lang)


def reading_mark(lang: str) -> str:
    return READING_MARK.format(lang=lang)


def _marked_sections(text: str) -> list[tuple[str, str, str]]:
    """세션 출력 → `[(kind, lang, 본문)]`. kind는 `SCRIPT` | `READING`."""
    body = strip_code_fence(text)
    marks = list(_MARK_RE.finditer(body))
    out: list[tuple[str, str, str]] = []
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(body)
        out.append((mark.group(1), mark.group(2), body[mark.end() : end].strip()))
    return out


class ScriptMdError(Exception):
    pass


# --- 세션 출력 정리 ---------------------------------------------------------

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n(.*)\n```\s*$", re.S)


def strip_code_fence(text: str) -> str:
    """세션이 전체를 코드펜스로 감쌌으면 벗긴다. 부분 펜스는 건드리지 않는다."""
    match = _FENCE_RE.match(text.strip())
    return match.group(1) if match else text.strip()


def split_factcheck_output(text: str) -> tuple[str, str | None]:
    """`[2. factcheck]` 세션 출력 → (factcheck.md 본문, 갱신 script.md 본문 | None).

    SCRIPT 절이 없으면 대본 무변경으로 취급한다 — 죽이지 않고 호출자가 경고를 단다.
    """
    body = strip_code_fence(text)
    if FACTCHECK_MARK in body:
        body = body.split(FACTCHECK_MARK, 1)[1]
    if SCRIPT_MARK in body:
        fact, script = body.split(SCRIPT_MARK, 1)
        script = strip_code_fence(script.strip())
        return fact.strip() + "\n", (script + "\n") if script.startswith("#") else None
    return body.strip() + "\n", None


def split_localized_output(text: str) -> dict[str, str]:
    """`[2l. localize]` 세션 출력 → `{lang: script.{lang}.md 본문}`.

    마커(`=== SCRIPT.ja ===`)로 시작하는 절마다 하나. 마커 뒤 본문이 `#`로 시작하지
    않으면 그 언어는 없는 것으로 친다 — 호출자가 언어별로 실패를 기록한다. 여기서
    죽이지 않는 이유는 한 언어가 깨져도 다른 언어는 쓸 수 있기 때문이다.
    """
    sections: dict[str, str] = {}
    for kind, lang, body in _marked_sections(text):
        if kind != "SCRIPT":
            continue
        section = strip_code_fence(body)
        if section.startswith("#"):
            sections[lang] = section + "\n"
    return sections


def split_reading_output(text: str) -> dict[str, list[str]]:
    """`[2l]` 세션 출력 → `{lang: 읽기 줄 목록}` (ADR-0073).

    `=== READING.ja ===` 절만 본다. 절 안은 마크다운 문서가 아니라 줄 목록이므로 비어
    있지 않은 줄을 그대로 모은다 — 옳고 그름은 `check_reading_lines`가 판정한다.
    """
    readings: dict[str, list[str]] = {}
    for kind, lang, body in _marked_sections(text):
        if kind != "READING":
            continue
        lines = [line.strip() for line in strip_code_fence(body).splitlines()]
        readings[lang] = [line for line in lines if line]
    return readings


# --- script.md 파싱 ---------------------------------------------------------


@dataclass
class ScriptMd:
    title: str = ""
    header: dict[str, str] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)
    claims: list[str] = field(default_factory=list)
    has_script_section: bool = False
    #: `## 읽기` 절의 줄 — TTS가 읽을 텍스트다 (ADR-0073). 자막·씬 계약은 `lines`를 쓴다.
    reading: list[str] = field(default_factory=list)
    has_reading_section: bool = False

    @property
    def unfit(self) -> bool:
        return self.header.get(FITNESS_KEY, "").startswith(UNFIT_MARK)

    @property
    def seed_url(self) -> str:
        return self.header.get("시드", "")



def parse_script_md(text: str) -> ScriptMd:
    doc = ScriptMd()
    section: str | None = None
    # 판정 블록은 대본이 아니다 (ADR-0094) — 머리 항목 파싱 전에 뗀다.
    for raw in strip_gate_block(text).splitlines():
        line = raw.strip()
        if line.startswith("## "):
            name = line[3:].strip()
            if name == SCRIPT_HEADING:
                section = "script"
                doc.has_script_section = True
            elif name == CLAIMS_HEADING:
                section = "claims"
            elif name == READING_HEADING:
                section = "reading"
                doc.has_reading_section = True
            else:
                section = "other"
            continue
        if line.startswith("# ") and not doc.title:
            doc.title = line[2:].strip()
            continue
        if not line or line.startswith("<!--"):
            continue
        if section is None:
            if line.startswith("- ") and ":" in line:
                key, _, value = line[2:].partition(":")
                doc.header[key.strip().lstrip("*").rstrip("*").strip()] = value.strip()
            continue
        if section == "script":
            if line.startswith("#"):
                continue
            doc.lines.append(line)
        elif section == "reading":
            if line.startswith("#") or line.startswith(">"):
                continue
            doc.reading.append(line)
        elif section == "claims" and line.startswith("- "):
            doc.claims.append(line[2:].strip())
    return doc


# --- 기계 검사 (specs/01 「기계 검사」 절) ----------------------------------


def check_script_md(text: str) -> tuple[list[str], list[str]]:
    """(errors, warnings). errors가 비어야 통과다. LLM을 부르지 않는다."""
    doc = parse_script_md(text)
    errors: list[str] = []
    warnings: list[str] = []

    if doc.unfit:
        reason = doc.header.get(FITNESS_KEY, "")
        return [f"매체 부적합 반려 — {reason}"], []

    if not doc.has_script_section or not doc.lines:
        return ["`## 대본` 절이 없거나 비어 있다 (specs/01 포맷)"], []

    limits = vocab.limits()

    n = len(doc.lines)
    lo, hi = limits["line_count"]
    if not lo <= n <= hi:
        errors.append(f"대본 {n}줄 — 범위 {lo}~{hi}줄 (script-rules.json line_count)")

    total = len(core_chars(" ".join(doc.lines)))
    lo, hi = limits["total_chars"]
    if not lo <= total <= hi:
        errors.append(
            f"총 {total}자(공백·부호 제외) — 범위 {lo}~{hi}자 (script-rules.json total_chars)"
        )

    max_chars = limits["line_chars_max"]
    for idx, line in enumerate(doc.lines, 1):
        chars = len(core_chars(line))
        if chars > max_chars:
            warnings.append(f"{idx}줄 {chars}자 — 줄당 최대 {max_chars}자 초과")

    # 수미상관 — 마지막 줄들이 첫 줄들의 명사를 재사용하는가 (스펙 01)
    #
    # 엔딩에서는 형태소를 다시 추정하지 않고 **훅에서 뽑은 명사가 글자로 다시 나오는지**만
    # 본다. `noun_stems`는 조사를 떼어 명사를 *찾는* 도구라 후보를 뽑는 쪽에는 맞지만,
    # "그 말이 또 나왔는가"를 재는 데 쓰면 그 휴리스틱의 한계가 그대로 오탐이 된다 —
    # 조사 없이 끝나는 명사(바다·언어)를 용언으로 보고 버리기 때문이다. 야프섬 편이
    # 실측이다 (2026-08-24): 훅 "돈을 **바다에** 빠뜨렸는데" / 엔딩 "**바다** 밑 그 돌은
    # … 주소만 **바다**였죠"인데 교집합이 0이었다. 조사가 붙은 쪽만 잡혔던 것이다.
    #
    # 이 판정은 옛 규칙보다 **느슨하기만 하다** — tail 명사 집합의 원소는 언제나 tail
    # 텍스트의 부분 문자열이므로, 전에 통과하던 대본은 그대로 통과한다.
    size = max(1, round(n * EDGE_RATIO))
    head = noun_stems(" ".join(doc.lines[:size]))
    tail_text = " ".join(doc.lines[-size:])
    if not any(stem in tail_text for stem in head):
        errors.append("엔딩이 훅의 명사를 하나도 재사용하지 않는다 (수미상관 실패)")

    if not doc.claims:
        warnings.append("`## 주장` 절이 비어 있다 — [2. factcheck]의 1차 목록이 없다")

    return errors, warnings


# --- 번안 대본 기계 검사 (specs/01 「번안 대본」, `[2l. localize]` 직후) -------


def script_section_inner_blank_lines(text: str) -> int:
    """`## 대본` 절 **안쪽**(첫 줄과 마지막 줄 사이)의 빈 줄 수."""
    return section_inner_blank_lines(text, SCRIPT_HEADING)


def section_inner_blank_lines(text: str, heading: str) -> int:
    """`## {heading}` 절 **안쪽**(첫 줄과 마지막 줄 사이)의 빈 줄 수.

    `parse_script_md`는 빈 줄을 세지 않으므로 줄 수가 맞아도 중간에 빈 줄이 남아 있을
    수 있다 — 세션이 줄 하나를 비워 둔 채 줄 수를 맞춘 흔적이다. 절 머리·꼬리의 빈 줄은
    정본 포맷의 여백이라 세지 않는다.
    """
    raw: list[str] = []
    section: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if section == "target":
                break
            section = "target" if stripped[3:].strip() == heading else "other"
            continue
        if section == "target":
            raw.append(stripped)
    while raw and not raw[0]:
        raw.pop(0)
    while raw and not raw[-1]:
        raw.pop()
    return sum(1 for line in raw if not line)


def check_localized_script_md(
    text: str, *, lang: str, expected_lines: int
) -> tuple[list[str], list[str]]:
    """번안 대본(script.{lang}.md)의 기계 검사. (errors, warnings). LLM을 부르지 않는다.

    specs/01 「번안 대본」 + specs/05 `[2l]`: 줄 수 일치, 빈 줄 없음, 그리고 분량 —
    `script-rules.json`의 `locales.{lang}.limits`가 있으면 그 엔벨로프(ko 검사와 같은
    키·같은 규칙), 없으면 하한 없음. **총 길이(초)는 여기서 추정하지 않는다** — 계약에
    언어별 추정 속도가 없고, ko 검사도 초를 추정하지 않는다. 상한은 `[3] tts`가 실측으로
    본다(`max_total_seconds(lang)`). 그 사실을 경고로 남긴다.

    수미상관·어미 같은 한국어 휴리스틱은 걸지 않는다 — 정본이 지켰고 번안은 줄 1:1이다.
    """
    doc = parse_script_md(text)
    errors: list[str] = []
    warnings: list[str] = []

    if not doc.has_script_section or not doc.lines:
        return [f"{lang}: `## 대본` 절이 없거나 비어 있다 (specs/01 포맷)"], []

    n = len(doc.lines)
    if n != expected_lines:
        errors.append(
            f"{lang}: 대본 {n}줄 — 정본 {expected_lines}줄과 다르다 (줄 1:1, ADR-0056)"
        )

    blanks = script_section_inner_blank_lines(text)
    if blanks:
        errors.append(f"{lang}: `## 대본` 절 안에 빈 줄 {blanks}개 — 줄 사이에 빈 줄을 두지 않는다")

    limits = vocab.locale_limits(lang)
    if "line_count" in limits:
        lo, hi = limits["line_count"]
        if not lo <= n <= hi:
            errors.append(
                f"{lang}: 대본 {n}줄 — 범위 {lo}~{hi}줄 (script-rules.json locales.{lang})"
            )
    if "total_chars" in limits:
        total = len(core_chars(" ".join(doc.lines)))
        lo, hi = limits["total_chars"]
        if not lo <= total <= hi:
            errors.append(
                f"{lang}: 총 {total}자(공백·부호 제외) — 범위 {lo}~{hi}자 "
                f"(script-rules.json locales.{lang} total_chars)"
            )
    if "line_chars_max" in limits:
        max_chars = limits["line_chars_max"]
        for idx, line in enumerate(doc.lines, 1):
            chars = len(core_chars(line))
            if chars > max_chars:
                warnings.append(f"{lang}: {idx}줄 {chars}자 — 줄당 최대 {max_chars}자 초과")

    warnings.append(
        f"{lang}: 총 길이(초)는 여기서 추정하지 않는다 — 언어별 추정 속도가 계약에 없다. "
        f"상한 {max_total_seconds(lang):g}초는 [3] tts가 실측으로 본다"
        + ("" if limits else f" (locales.{lang} 블록 없음 — 줄 수 일치만 검사했다)")
    )

    if not doc.claims:
        warnings.append(f"{lang}: `## 주장` 절이 비어 있다 — 정본의 주장 목록이 옮겨지지 않았다")

    return errors, warnings


# --- `## 읽기` 절 (specs/01 「번안 대본」 4, ADR-0073) ------------------------

#: 한자와 반복부호. 읽기 줄에 남아 있으면 안 편 것이다 — 숫자 뒤 창만 예외다.
_KANJI_RE = re.compile(r"[一-鿿㐀-䶿々〆]")

#: 숫자 토큰. `tts/speech.py`의 `NUMBER`와 같은 꼴이어야 한다 — 읽기 줄이 숫자를 그대로
#: 두는지 보는 검사가 이 토큰 목록을 대조하고, 그 숫자를 나중에 펴는 것이 그 모듈이다.
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

#: 줄 끝 문장부호. 씬 경계를 줄 끝에서 잡으므로(ADR-0013) 읽기가 이것을 먹으면 안 된다.
_TRAILING_PUNCT = "。．.?？!！…、，,"


def requires_reading(lang: str) -> bool:
    """그 언어의 대본이 `## 읽기` 절을 가져야 하는가 (`speech-rules.json`이 정한다)."""
    return speech_rules.reading_line(lang) is not None


def _kanji_outside_window(line: str, window: int) -> str:
    """숫자 뒤 창 밖에 남은 한자들. 비어 있으면 통과다.

    창은 숫자가 끝난 자리부터 `window`글자다 — `1本`(0) · `4か所`(1) · `10兆円`(1)이
    전부 그 안에 든다. 값은 계약이 준다 (`reading_line.kanji_window_after_digit`).
    """
    allowed: set[int] = set()
    for match in _NUMBER_RE.finditer(line):
        allowed.update(range(match.end(), min(len(line), match.end() + window)))
    return "".join(
        char
        for index, char in enumerate(line)
        if index not in allowed and _KANJI_RE.match(char)
    )


def _trailing_punct(line: str) -> str:
    """줄 끝에 붙은 문장부호 (없으면 빈 문자열)."""
    end = len(line)
    while end > 0 and line[end - 1] in _TRAILING_PUNCT:
        end -= 1
    return line[end:]


def check_reading_lines(
    reading: Sequence[str], script_lines: Sequence[str], *, lang: str
) -> tuple[list[str], list[str]]:
    """`## 읽기` 절의 기계 검사. (errors, warnings). LLM을 부르지 않는다.

    ADR-0073 결정 5의 다섯 가지다 — 줄 수 일치, 빈 줄 없음, 숫자 뒤 창 밖의 한자 없음,
    대본 줄과 숫자열 일치, 문장 끝 부호 보존. **읽기가 실패해도 대본은 산다** (결정 6):
    호출부는 읽기 절만 빼고 대본을 쓴다. 그러면 `[3]`이 원문을 보내고 그것이 ADR-0063
    이전의 동작이라, 이 계약은 한 방향으로만 움직인다.

    검사는 **바닥**이다. 창 안에서는 느슨한 쪽으로 틀린다 — 무엇을 어떻게 펼지는
    프롬프트가 정하고, 여기서는 "안 편 한자"와 "숫자를 건드린 흔적"만 잡는다.
    """
    rules = speech_rules.reading_line(lang)
    if rules is None:
        return [f"{lang}: 읽기 절을 요구하지 않는 언어다 (speech-rules.json reading_line)"], []

    errors: list[str] = []
    warnings: list[str] = []

    if len(reading) != len(script_lines):
        errors.append(
            f"{lang}: 읽기 {len(reading)}줄 — 대본 {len(script_lines)}줄과 다르다 "
            "(줄 1:1, ADR-0073)"
        )
        return errors, warnings

    window = int(rules.get("kanji_window_after_digit", 2))
    for index, (spoken, source) in enumerate(zip(reading, script_lines), start=1):
        if not spoken.strip():
            errors.append(f"{lang}: 읽기 {index}줄이 비어 있다")
            continue

        left = _kanji_outside_window(spoken, window)
        if left:
            errors.append(
                f"{lang}: 읽기 {index}줄에 안 편 한자 '{left}' — 숫자 뒤 {window}글자 밖의 "
                f"한자는 가나로 편다 (specs/01 「번안 대본」 4)"
            )

        numbers = _NUMBER_RE.findall(source)
        if _NUMBER_RE.findall(spoken) != numbers:
            errors.append(
                f"{lang}: 읽기 {index}줄의 숫자가 대본과 다르다 "
                f"(대본 {numbers or '없음'}) — 숫자는 원문 그대로 두고 [3]이 편다 (ADR-0063)"
            )

        if _trailing_punct(spoken) != _trailing_punct(source):
            errors.append(
                f"{lang}: 읽기 {index}줄의 끝 문장부호가 대본과 다르다 — 씬 경계를 줄 끝에서 "
                "잡는다 (ADR-0013)"
            )

        if spoken == source and _KANJI_RE.search(source):
            warnings.append(f"{lang}: 읽기 {index}줄이 대본과 같다 — 한자가 있는데 안 폈다")

    return errors, warnings


def render_reading_section(lines: Sequence[str]) -> str:
    """`## 읽기` 절 본문 (앞 빈 줄 포함, 끝 개행 없음)."""
    body = "\n".join(lines)
    return f"## {READING_HEADING}\n\n{body}"


def strip_reading_section(text: str) -> str:
    """`## 읽기` 절을 들어낸 대본 전문. 나머지 절은 글자까지 그대로다.

    세션이 낸 읽기가 검사에 걸렸을 때 쓴다 — 대본은 멀쩡하므로 읽기만 빼고 쓴다
    (ADR-0073 결정 6). 그러면 `[3]`이 원문을 보내고 그것이 ADR-0063 이전의 동작이다.
    """
    out: list[str] = []
    dropping = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            dropping = stripped[3:].strip() == READING_HEADING
        if not dropping:
            out.append(line)
    return "\n".join(out).rstrip() + "\n"


def append_reading_section(text: str, lines: Sequence[str]) -> str:
    """대본 전문 뒤에 `## 읽기` 절을 붙인다 — **앞의 바이트는 손대지 않는다**.

    대본이 이미 있고 읽기 절만 없는 파일을 채우는 경로다 (ADR-0073 결정 7). 재생성이
    아니라 빠진 절을 더하는 것이라 사람이 고친 `## 대본`·`## 주장`이 그대로 산다.
    """
    return f"{text.rstrip()}\n\n{render_reading_section(lines)}\n"
