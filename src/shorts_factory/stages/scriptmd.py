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

from ..schemas import vocab
from ..schemas.script_rules import core_chars, max_total_seconds, noun_stems
from ..schemas.timed_scenes import PRIMARY_LANGUAGE
from .session import format_limits, load_prompt  # noqa: F401 — 재수출


#: 시드 기사 열람·교차 확인용. 산출물 파일은 오케스트레이터가 쓴다 (ADR-0011).
WEB_TOOLS: tuple[str, ...] = ("WebSearch", "WebFetch")

#: 수미상관을 볼 때 앞뒤로 떼는 비율 (schemas/script_rules.py와 같은 값)
EDGE_RATIO = 0.15

SCRIPT_HEADING = "대본"
CLAIMS_HEADING = "주장"
FITNESS_KEY = "매체 적합성"
UNFIT_MARK = "부적합"

#: 대본 파일명 (specs/05 1부↔2부 경계 절, ADR-0056) — ko는 `script.md`, 나머지는
#: `script.{lang}.md`. 2부(`stages/tts.py`)도 같은 이름을 읽는다 — 파일명이 계약이다.
SCRIPT_MD_FILE = "script.md"
SCRIPT_MD_PATTERN = "script.{lang}.md"


def script_md_name(lang: str) -> str:
    """그 언어의 대본 파일명."""
    return SCRIPT_MD_FILE if lang == PRIMARY_LANGUAGE else SCRIPT_MD_PATTERN.format(lang=lang)


#: `[2. factcheck]` 세션 출력의 절 구분 마커 (prompts/02-factcheck.md와 계약)
FACTCHECK_MARK = "=== FACTCHECK ==="
SCRIPT_MARK = "=== SCRIPT ==="

#: `[2l. localize]` 세션 출력의 언어별 절 마커 — `=== SCRIPT.ja ===`
#: (prompts/02l-localize.md와 계약). 파일명과 같은 꼴이라 사람이 읽어도 어느 파일인지 보인다.
LOCALIZED_MARK = "=== SCRIPT.{lang} ==="
_LOCALIZED_MARK_RE = re.compile(r"^=== SCRIPT\.([a-z]{2}) ===[ \t]*$", re.MULTILINE)


def localized_mark(lang: str) -> str:
    return LOCALIZED_MARK.format(lang=lang)


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
    body = strip_code_fence(text)
    marks = list(_LOCALIZED_MARK_RE.finditer(body))
    sections: dict[str, str] = {}
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(body)
        section = strip_code_fence(body[mark.end():end].strip())
        if section.startswith("#"):
            sections[mark.group(1)] = section + "\n"
    return sections


# --- script.md 파싱 ---------------------------------------------------------


@dataclass
class ScriptMd:
    title: str = ""
    header: dict[str, str] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)
    claims: list[str] = field(default_factory=list)
    has_script_section: bool = False

    @property
    def unfit(self) -> bool:
        return self.header.get(FITNESS_KEY, "").startswith(UNFIT_MARK)

    @property
    def seed_url(self) -> str:
        return self.header.get("시드", "")


def parse_script_md(text: str) -> ScriptMd:
    doc = ScriptMd()
    section: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            name = line[3:].strip()
            if name == SCRIPT_HEADING:
                section = "script"
                doc.has_script_section = True
            elif name == CLAIMS_HEADING:
                section = "claims"
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
    size = max(1, round(n * EDGE_RATIO))
    head = noun_stems(" ".join(doc.lines[:size]))
    tail = noun_stems(" ".join(doc.lines[-size:]))
    if not head & tail:
        errors.append("엔딩이 훅의 명사를 하나도 재사용하지 않는다 (수미상관 실패)")

    if not doc.claims:
        warnings.append("`## 주장` 절이 비어 있다 — [2. factcheck]의 1차 목록이 없다")

    return errors, warnings


# --- 번안 대본 기계 검사 (specs/01 「번안 대본」, `[2l. localize]` 직후) -------


def script_section_inner_blank_lines(text: str) -> int:
    """`## 대본` 절 **안쪽**(첫 줄과 마지막 줄 사이)의 빈 줄 수.

    `parse_script_md`는 빈 줄을 세지 않으므로 줄 수가 맞아도 중간에 빈 줄이 남아 있을
    수 있다 — 세션이 줄 하나를 비워 둔 채 줄 수를 맞춘 흔적이다. 절 머리·꼬리의 빈 줄은
    정본 포맷의 여백이라 세지 않는다.
    """
    raw: list[str] = []
    section: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if section == "script":
                break
            section = "script" if stripped[3:].strip() == SCRIPT_HEADING else "other"
            continue
        if section == "script":
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
