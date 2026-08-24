"""발화형 — 대본 줄을 TTS가 읽을 텍스트로 편다. ADR-0063.

`sync.py`처럼 파일도 네트워크도 만지지 않는다 (계약 파일만 읽는다). `[3]`의 판단 중
"무엇을 읽힐 것인가"가 전부 여기 있고 API 키 없이 검증된다.

## 왜 두 텍스트가 필요한가

보는 텍스트와 읽는 텍스트의 요구가 반대다. 자막은 `12cm`여야 하고(화면 계측 라벨과
숫자가 맞아야 한다 — ADR-0060 결정 4), 나레이션은 `십이 센티미터`여야 한다. 그래서
`[3]`만 두 텍스트를 갖고, `scenes.timed.{lang}.json`으로 나가는 것은 **원문**이다.

## 아는 것만 편다

사전(`specs/schema/speech-rules.json`)에 없는 단위, `system`이 `ambiguous`인 조수사,
범위 표기(`1975~79년`)는 **원문 그대로 보내고 경고만 남긴다.** 엔진의 기본 정규화가
그 자리에 그대로 남으므로 이 변환은 한 방향으로만 움직인다 — 아는 토큰만 좋아지고
모르는 토큰은 전과 같다. 표를 코드가 들지 않기 때문에 되돌리는 단위가 **사전 항목
하나**다 (ADR-0063 되돌릴 조건 2).

한국어 수사가 조수사에 딸린 값이라는 것이 이 설계의 이유다 — `27단`은 `이십칠 단`인데
`27대 선덕여왕`은 `이십칠 대`, 기계 27대는 `스물일곱 대`다. 표 없이 규칙만으로 밀면
우리 변환이 엔진보다 나빠진다.

## 줄은 나뉘지도 합쳐지지도 않는다

씬 경계는 줄이다 (ADR-0013). 변환이 줄 안에서 끝나야 `[3]`의 경계 계산이 흔들리지
않으므로, 줄 수를 그대로 두고 **문장 끝 부호를 보존한다**. 어느 쪽이든 깨지면 그 줄만
원문으로 되돌리고 경고한다 (ADR-0063 결정 6) — 호출 전이라 과금이 없다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..schemas import speech_rules

#: 숫자 토큰. 천 단위 구분(`8,000`)과 소수(`0.4`)를 한 덩어리로 잡는다.
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")

#: 숫자 뒤에 이것이 오면 단위가 아니라 **문장**이다. 여기 없는 문자는 단위 후보로 본다
#: (`℉`처럼 사전에 없는 기호도 조용히 지나가지 않고 경고를 남기게 하려는 것이다).
STOP_CHARS = frozenset(
    " \t\r\n"
    + ".,!?…·:;/"
    + "()[]{}<>"
    + "\"'`"
    + "、。，！？；：／"
    + "「」『』（）〈〉《》【】"
    + "“”‘’"
)

#: 문장 끝으로 인정하는 문자. `sync.SENTENCE_ENDINGS`와 같은 집합이어야 한다 — 변환이
#: 이 부호를 먹으면 그 줄의 경계 추출이 마지막 문자로 떨어진다 (ADR-0013).
SENTENCE_ENDINGS = (".", "?", "!", "…")


@dataclass(frozen=True)
class SpokenLine:
    """줄 하나의 원문·발화형·경고."""

    text: str
    spoken: str
    warnings: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return self.spoken != self.text


@dataclass(frozen=True)
class SpokenScript:
    """대본 한 편의 발화형. `lines`가 TTS로 가고 `changes`는 실행 기록으로 간다."""

    lines: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    changes: tuple[dict[str, Any], ...] = field(default=())

    @property
    def changed_count(self) -> int:
        return len(self.changes)


def spoken_lines(texts: Sequence[str], lang: str) -> SpokenScript:
    """대본 줄 목록 → 발화형 줄 목록. **줄 수와 순서는 그대로다.**

    경고에는 씬 번호를 붙인다 — 사람이 사전을 늘릴 때 어느 줄인지가 먼저 필요하다.
    """
    lines: list[str] = []
    warnings: list[str] = []
    changes: list[dict[str, Any]] = []

    for scene_id, text in enumerate(texts, start=1):
        result = spoken_line(text, lang)
        problem = _reject(text, result.spoken)
        if problem:
            # 우리가 만든 발화형이 줄의 전제를 깬 경우다. 그 줄만 원문으로 돌린다 —
            # 대본은 멀쩡하므로 편 전체를 세울 이유가 없다.
            warnings.append(f"scenes/{scene_id}: 발화형을 버리고 원문을 보낸다 — {problem}")
            lines.append(text)
            continue
        lines.append(result.spoken)
        warnings.extend(f"scenes/{scene_id}: {w}" for w in result.warnings)
        if result.changed:
            changes.append({"scene_id": scene_id, "text": text, "spoken": result.spoken})

    return SpokenScript(tuple(lines), tuple(warnings), tuple(changes))


def _reject(text: str, spoken: str) -> str | None:
    """발화형을 받아들일 수 없는 사유. 받아들일 수 있으면 `None`."""
    if not spoken.strip():
        return "빈 줄이 됐다"
    if "\n" in spoken:
        return "줄이 나뉘었다"
    if text.rstrip().endswith(SENTENCE_ENDINGS) and not spoken.rstrip().endswith(
        SENTENCE_ENDINGS
    ):
        return "문장 끝 부호가 사라졌다 (씬 경계 추출의 전제다 — ADR-0013)"
    return None


def spoken_line(text: str, lang: str) -> SpokenLine:
    """줄 하나를 편다. 로케일 블록이 없는 언어는 원문을 그대로 돌려준다."""
    rules = speech_rules.locale(lang)
    if not rules:
        return SpokenLine(text=text, spoken=text)

    table = speech_rules.units(lang)
    ranges = speech_rules.range_chars()
    spell = bool(rules.get("spell_numbers"))
    allow_gap = rules.get("unit_space", False)
    warn_max = int(rules.get("warn_suffix_max_len", 4))

    out: list[str] = []
    warnings: list[str] = []
    cursor = 0

    for match in NUMBER.finditer(text):
        if match.start() < cursor:  # pragma: no cover - finditer는 겹치지 않는다
            continue
        out.append(text[cursor : match.start()])
        cursor = match.start()
        raw = match.group()

        if _in_range(text, match, ranges):
            warnings.append(
                f"'{_range_token(text, match, ranges)}' — 범위·부호 표기라 그대로 보낸다 "
                "(확정하려면 대본에 말로 쓴다)"
            )
            out.append(raw)
            cursor = match.end()
            continue

        key, entry, end = _find_unit(text, match.end(), table, allow_gap)

        if key is None:
            # 모르는 단위 경고는 **붙어 있는** 글자만 본다. 공백을 건너 보면 한국어의
            # '1 모형'이 단위 후보가 된다 — 영어는 단위를 띄어 쓰므로 예외다.
            probe = match.end()
            if allow_gap is True and text[probe : probe + 1] == " ":
                probe += 1
            suffix = _suffix(text, probe)
            if suffix:
                if len(suffix) <= warn_max:
                    warnings.append(
                        f"'{raw}{suffix}' — 단위 '{suffix}'가 사전에 없어 그대로 보낸다 "
                        f"(speech-rules.json locales.{lang}.units)"
                    )
                out.append(raw)
            elif spell:
                out.append(_spell_number(raw, lang, rules["default_system"]))
            else:
                out.append(raw)
            cursor = match.end()
            continue

        if entry.get("system") == "ambiguous":
            warnings.append(
                f"'{raw}{key}' — 조수사 '{key}'는 문맥이 갈려 그대로 보낸다 "
                f"(speech-rules.json locales.{lang}.units.{key})"
            )
            out.append(raw)
            cursor = match.end()
            continue

        out.append(_read(raw, key, entry, lang, rules, spell))
        cursor = end

    out.append(text[cursor:])
    return SpokenLine(text=text, spoken="".join(out), warnings=_dedupe(warnings))


def _dedupe(warnings: Sequence[str]) -> tuple[str, ...]:
    """같은 말을 두 번 하지 않는다 — 범위 표기는 양쪽 숫자가 같은 경고를 낸다."""
    seen: dict[str, None] = {}
    for warning in warnings:
        seen.setdefault(warning, None)
    return tuple(seen)


# --- 조각들 -----------------------------------------------------------------


def _in_range(text: str, match: re.Match[str], ranges: Sequence[str]) -> bool:
    """숫자 양옆에 범위·부호 문자가 **붙어** 있는가 (`1975~79년`, `-3`).

    떨어져 있으면(`1592년 — 임진왜란`) 범위가 아니라 문장부호다.
    """
    before = text[match.start() - 1 : match.start()]
    after = text[match.end() : match.end() + 1]
    return before in ranges or after in ranges


def _range_token(text: str, match: re.Match[str], ranges: Sequence[str]) -> str:
    """경고에 보일 범위 표기 전체 (`1975~79`).

    양쪽 숫자가 같은 문자열을 내야 경고가 한 줄로 합쳐진다 (`_dedupe`).
    """
    start, end = match.start(), match.end()
    while start > 0 and (text[start - 1].isdigit() or text[start - 1] in ranges):
        start -= 1
    while end < len(text) and (text[end].isdigit() or text[end] in ranges):
        end += 1
    return text[start:end]


def _find_unit(
    text: str,
    index: int,
    table: Sequence[tuple[str, dict[str, Any]]],
    allow_gap: Any,
) -> tuple[str | None, dict[str, Any], int]:
    """숫자 뒤의 단위 — 붙은 것을 먼저 보고, 허용되면 공백 하나를 건너 다시 본다.

    `unit_space`는 세 값이다: `false`(붙은 것만), `true`(공백 허용 — 영어), `"latin"`
    (라틴·기호 단위에만 공백 허용 — 한국어·일본어). 한국어는 조수사가 붙어 오지만
    라틴 약어는 띄어 쓴다 (`3 m`·`8,000 m³`, 실측: tests/fixtures/contract_pisa.json).
    한글 키까지 공백을 허용하면 `5분의 1 모형`의 `모형`이 단위 후보가 된다.
    """
    key, entry = _match_unit(text, index, table)
    if key is not None:
        return key, entry, index + len(key)
    if not allow_gap or text[index : index + 1] != " ":
        return None, {}, index
    key, entry = _match_unit(text, index + 1, table)
    if key is None or (allow_gap is not True and not _latin_key(key)):
        return None, {}, index
    return key, entry, index + 1 + len(key)


def _latin_key(key: str) -> bool:
    """한글·가나·한자가 섞이지 않은 키 (`cm`·`%`·`℃`). 공백 건너뛰기의 자격이다."""
    return not any(char.isalpha() and not char.isascii() for char in key)


def _match_unit(
    text: str, index: int, table: Sequence[tuple[str, dict[str, Any]]]
) -> tuple[str | None, dict[str, Any]]:
    """`index`에서 시작하는 가장 긴 사전 키. 없으면 `(None, {})`.

    라틴 약어(`m`·`kg`)는 **낱말이 끝나는 자리에서만** 맞는다 — 그러지 않으면 `12 mi`가
    `12 metersi`가 된다. 한글·가나 키에는 이 경계를 걸지 않는다: 조수사 뒤에 조사가
    바로 붙는 것이 정상이다 (`12cm` → `십이 센티미터`, `194그루입니다`).
    """
    for key, entry in table:  # 사전은 긴 키부터 정렬돼 온다 (speech_rules.units)
        if not text.startswith(key, index):
            continue
        if _latin(key[-1]) and _latin(text[index + len(key) : index + len(key) + 1]):
            continue
        return key, entry
    return None, {}


def _latin(char: str) -> bool:
    """ASCII 낱말 문자. 낱말 경계 판정에만 쓴다."""
    return bool(char) and char.isascii() and char.isalnum()


def _suffix(text: str, index: int) -> str:
    """단위 자리에 온 모르는 글자들. 문장부호·공백을 만나면 끝난다."""
    end = index
    while end < len(text) and text[end] not in STOP_CHARS and not text[end].isdigit():
        end += 1
    return text[index:end]


def _read(
    raw: str,
    key: str,
    entry: dict[str, Any],
    lang: str,
    rules: dict[str, Any],
    spell: bool,
) -> str:
    """숫자 + 단위 하나의 읽기."""
    digits = raw.replace(",", "")
    overrides = entry.get("overrides") or {}
    if digits in overrides:
        # 음이 준 꼴은 통째로 대체한다 — 6월은 육월이 아니라 유월이다.
        return str(overrides[digits])

    number = _spell_number(raw, lang, entry.get("system") or rules["default_system"]) if spell else raw
    read = entry.get("read", key)
    if _value(digits) == 1 and entry.get("read_one"):
        read = entry["read_one"]

    space = entry.get("space")
    gap = (" " if space else "") if isinstance(space, bool) else rules.get("unit_space_out", " ")
    return f"{number}{gap}{read}"


def _value(digits: str) -> float:
    try:
        return float(digits)
    except ValueError:  # pragma: no cover - NUMBER가 잡은 것은 항상 수다
        return 0.0


def _spell_number(raw: str, lang: str, system: str) -> str:
    """`8,000` · `0.4` → 그 언어의 읽기."""
    digits = raw.replace(",", "")
    whole, _, frac = digits.partition(".")
    sysdef = speech_rules.system(lang, system)

    if frac and sysdef.get("composition") == "ko_native":
        # 고유어 수사는 소수를 읽지 않는다 (speech-rules.json의 _decimal).
        sysdef = speech_rules.system(lang, sysdef["head_system"])

    spoken = _whole(int(whole or "0"), sysdef, lang)
    if not frac:
        return spoken
    table = sysdef["decimal_digits"]
    tail = sysdef.get("decimal_join", "").join(table[int(d)] for d in frac)
    return f"{spoken}{sysdef['decimal_point']}{tail}"


def _whole(value: int, sysdef: dict[str, Any], lang: str) -> str:
    composition = sysdef.get("composition")
    if composition == "ko_native":
        return _ko_native(value, sysdef, lang)
    return _cjk_myriad(value, sysdef)


def _cjk_myriad(value: int, sysdef: dict[str, Any]) -> str:
    """만 단위로 끊어 읽는 한자어 수사 (한국어·일본어 공통 골격)."""
    if value == 0:
        return sysdef["zero"]

    groups: list[tuple[int, int]] = []
    rest = value
    index = 0
    while rest > 0:
        chunk = rest % 10000
        if chunk:
            groups.append((index, chunk))
        rest //= 10000
        index += 1

    names = sysdef["groups"]
    omit = set(sysdef.get("omit_one_groups", ()))
    parts: list[str] = []
    for group, chunk in reversed(groups):
        if group >= len(names):  # pragma: no cover - 조(10^12)를 넘는 수는 대본에 없다
            raise ValueError(f"읽을 수 없는 자릿수다: {value}")
        body = _chunk(chunk, sysdef)
        if names[group] and chunk == 1 and group in omit:
            body = ""
        parts.append(body + names[group])
    return "".join(parts)


def _chunk(chunk: int, sysdef: dict[str, Any]) -> str:
    """1~9999 한 덩어리."""
    digits = sysdef["digits"]
    places = sysdef["places"]
    omit = set(sysdef.get("omit_one_places", ()))
    overrides = sysdef.get("place_overrides") or {}

    out: list[str] = []
    for place in range(len(places) - 1, -1, -1):
        digit = (chunk // 10**place) % 10
        if not digit:
            continue
        override = overrides.get(f"{digit}:{place}")
        if override:
            out.append(override)
        elif digit == 1 and place in omit:
            out.append(places[place])
        else:
            out.append(digits[digit] + places[place])
    return "".join(out)


def _ko_native(value: int, sysdef: dict[str, Any], lang: str) -> str:
    """고유어 수사. 조수사 앞이므로 관형형이다 (스무 대 / 스물한 대)."""
    if value == 0:
        return speech_rules.system(lang, sysdef["head_system"])["zero"]

    tens = sysdef["tens"]
    tens_attr = sysdef["tens_attr"]
    ones_attr = sysdef["ones_attr"]

    if value > int(sysdef["max"]):
        # 백 이상은 앞자리를 한자어로 읽고 일의 자리만 고유어다 — 194그루 = 백구십네 그루.
        head = speech_rules.system(lang, sysdef["head_system"])
        ones = value % 10
        spoken = _cjk_myriad(value - ones, head)
        return spoken + (ones_attr[ones] if ones else "")

    ten, one = divmod(value, 10)
    if one == 0:
        return tens_attr[ten]
    return (tens[ten] if ten else "") + ones_attr[one]
