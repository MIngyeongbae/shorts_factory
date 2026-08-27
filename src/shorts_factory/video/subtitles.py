"""ASS 자막 생성 — 레이어 B. specs/schema/subtitle-style.json, ADR-0002, ADR-0013.

**스타일 값은 이 파일에 없다.** 계약 파일에서 로드한다 (ADR-0034 §3) — 값이 코드와
스펙 문서 두 곳에 있으면 갈라지고, 갈라진 쪽을 읽은 사람만 기준이 달라진다.

자막은 **후처리 합성**이다 (ADR-0002). 정확해야 하는 한국어를 이미지 생성에 맡기지
않는다. 큐 1개 = 씬 1개다 (ADR-0013) — 씬을 다시 묶거나 쪼개지 않는다.

시각의 출처는 그 언어의 `scenes.timed.{lang}.json` 하나다 (ADR-0020). 이 모듈은 씬의 `start`/`end`를
그대로 쓰고, ASS의 시간 해상도(1/100초)만큼만 반올림한다 — 최대 5ms이므로 specs/00의
±200ms 안이다.

## 세 값은 함께 정해졌다

`44px` · `1줄 22자(전각)` · `2줄`은 서로 맞물린 한 벌이다. 44px에서 전각 22자는 968px이라
가로 안전폭 990px 안에 들어가고, specs/01이 허용하는 **가장 긴 큐(`line_chars_max`)도
두 줄에 들어간다.** 그래서 정상 범위의 큐는 폰트를 줄일 일이 없다 — 큐별 폰트 축소
경로를 두지 않는다.

## 줄당 상한은 로케일의 것이다 (ADR-0062)

**글자 수는 픽셀 폭의 대리값이고, 그 환산은 전각 기준이다.** 한글·가나는 글자 폭이
폰트 크기와 같지만 라틴은 대략 절반이라, 같은 자수가 언어마다 다른 폭이 된다. 그래서
상한은 `subtitle-style.json`의 `locales`에서 온다 (`max_line_chars_for`). 값이 없는
언어는 최상위 값(전각 기준)을 쓴다 — ja는 ko와 같아서 블록이 없다.

로케일 상한 × 2줄에 안 들어가는 큐는 그 언어 대본이 상한을 넘겼다는 뜻이므로 **실패로 올린다**
(`check_overflow`). 자막 단계가 글자를 작게 만들어 삼키면 상류 위반이 화면에서만
티가 나고 기록에는 남지 않는다.

**이 검사가 상류에 없다.** 스펙 01의 `line_chars_max`는 `core_chars`(공백·부호 제외)로 재고
여기는 원문 글자로 재서 단위가 다르다 — 같은 것을 가리키지 않는다 (ADR-0062 되돌릴 조건 5).
그래서 너무 긴 줄은 TTS·영상 비용을 다 쓴 뒤 `[9]`에서 처음 걸린다.

## 제목은 자막의 2배다 (ADR-0074)

제목 훅(ADR-0065)은 폰트·색을 자막에서 물려받지만 **크기·줄 수·줄바꿈·외곽선은 제 것을
쓴다.** 크기는 배율(`title.font_scale`)이고 줄당 상한은 자막 상한을 그 배율로 나눈 값이라,
로케일의 여유(ADR-0062)가 나눗셈에 딸려온다. 줄바꿈은 균형 2분할이 아니라 **채워쓰기**다
(`fill_text`) — 공백에서만 자르는 `wrap_text`로는 공백 없는 ja가 60px, en이 57px에서 막혔다
(실측 2026-08-26, 제목 19편). 넘치면 자막처럼 실패하지 않고 **제목만 빠진다.**
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..schemas import script_rules, vocab

# --- 스타일 값 (specs/schema/subtitle-style.json) -----------------------------
#
# **선언하지 않고 로드한다** (ADR-0034 §3). 이 값들이 코드와 스펙 03 두 곳에 적혀
# 있던 것이 `mj_video` 사고와 같은 모양이라 계약 파일로 옮겼다.

_STYLE = vocab.SUBTITLE_STYLE

PLAY_RES_X, PLAY_RES_Y = _STYLE["play_res"]
FONT_NAME = _STYLE["font_name"]
FONTS_DIR = _STYLE["fonts_dir"]
PRIMARY_COLOUR = _STYLE["primary_colour"]
OUTLINE_COLOUR = _STYLE["outline_colour"]
OUTLINE = _STYLE["outline"]
SHADOW = _STYLE["shadow"]
ALIGNMENT = _STYLE["alignment"]
MARGIN_LR = _STYLE["margin_lr"]

#: 폰트 크기·줄당 글자 수·줄 수는 **함께 정해진 한 벌이다.** 하나를 고치면 셋을 다시
#: 계산해야 하므로 계약 파일의 `_geometry`가 그 산수를 들고 있다.
MAX_LINE_CHARS = _STYLE["max_line_chars"]
MAX_LINES = _STYLE["max_lines"]
FONT_SIZE = _STYLE["font_size"]

#: 자막이 쓸 수 있는 가로 폭(px)
TEXT_WIDTH = PLAY_RES_X - 2 * MARGIN_LR

#: 한글 한 글자의 가로 advance ÷ 폰트 크기. 한글은 전각이라 1.0이다 (숫자·라틴은 더 좁다).
GLYPH_WIDTH_RATIO = 1.0

#: 자막 블록이 놓이는 세로 밴드 (화면 비율)
BAND = tuple(_STYLE["band"])

#: ASS 스타일 이름. **`parse_ass`가 이 이름만 되읽는다** — 제목 이벤트가 싱크 검증의
#: 큐 목록에 섞이면 씬 수가 하나 늘어 검증이 통째로 깨진다 (ADR-0065 결정 5).
SUBTITLE_STYLE_NAME = "Default"

#: 제목 훅 (ADR-0065). 폰트·색·정렬 기준은 자막의 것이고 **크기·줄 수·줄바꿈·외곽선은
#: 제목의 것이다** (ADR-0074가 ADR-0065 결정 3의 그 부분을 뒤집었다).
_TITLE = _STYLE["title"]
TITLE_STYLE_NAME = _TITLE["style_name"]
TITLE_ALIGNMENT = _TITLE["alignment"]
TITLE_BAND = tuple(_TITLE["band"])

#: 자막 크기의 배수 (ADR-0074). **88을 적지 않는다** — 자막이 44에서 움직이면 제목도
#: 따라 움직여야 하고, 줄당 상한도 이 배율이 나눈다 (`title_max_line_chars_for`).
TITLE_FONT_SCALE = int(_TITLE["font_scale"])
TITLE_FONT_SIZE = FONT_SIZE * TITLE_FONT_SCALE
TITLE_MAX_LINES = int(_TITLE["max_lines"])
TITLE_OUTLINE = _TITLE["outline"]
TITLE_SHADOW = _TITLE["shadow"]

#: 일본어 행두 금칙 — 이 글자로 줄을 시작하지 않는다 (ADR-0074 결정 5).
TITLE_KINSOKU = _TITLE["kinsoku"]

#: 자막 블록의 **아래끝**을 밴드의 아래끝에 맞추는 하단 여백(px). 위끝은 줄 수에 따라
#: 움직이며(1줄 78.7% / 2줄 75.4%) 둘 다 밴드 안이다 — `subtitle_band()`가 계산한다.
MARGIN_V = round(PLAY_RES_Y * (1 - BAND[1]))

#: 제목 블록의 **위끝**을 밴드 위끝에 맞추는 상단 여백(px). ASS의 MarginV는 Alignment가
#: 7·8·9일 때 위에서 잰다 — 자막(2 = 아래에서)과 기준선이 반대다.
TITLE_MARGIN_V = round(PLAY_RES_Y * TITLE_BAND[0])

#: libass가 한 줄에 쓰는 세로 크기 ÷ 폰트 크기의 근사치. 밴드 계산에만 쓴다.
LINE_HEIGHT_RATIO = 1.2

#: ASS의 줄바꿈 표시 (WrapStyle 2 = 자동 줄바꿈 없음, 이 표시만 줄을 나눈다)
LINE_BREAK = r"\N"

#: libass(자막)와 drawtext(엔딩 크레딧)가 읽는 폰트 파일 확장자. **읽는 곳이 둘이라
#: 여기 한 번만 적는다** — `FONTS_DIR`을 이미 이 모듈이 들고 있고, 목록이 갈리면
#: 한쪽 경로만 조용히 폰트를 못 찾는다 (ADR-0034 §3의 태도).
FONT_SUFFIXES = (".ttf", ".otf", ".ttc")

#: 언어별 폰트 패밀리 덮어쓰기 환경변수 (스펙 04 — "폰트만 언어별이다"). 계약 파일에
#: 언어별 폰트 표를 넣는 것은 스키마 변경이라 ADR이 필요하므로, 그때까지는 환경변수로
#: 받고 비어 있으면 `font_name`으로 떨어진다. ko는 계약의 `font_name` 그대로다.
FONT_ENV_PATTERN = "SUBTITLE_FONT_{LANG}"


def font_name_for(lang: str, *, environ: Mapping[str, str] | None = None) -> str:
    """그 언어의 폰트 패밀리. `SUBTITLE_FONT_JA`처럼 덮어쓴 값이 있으면 그것, 없으면 계약값."""
    env = os.environ if environ is None else environ
    override = (env.get(FONT_ENV_PATTERN.format(LANG=lang.upper())) or "").strip()
    return override or FONT_NAME


#: 언어별 자막 값 (`subtitle-style.json`의 `locales`). 값이 없는 언어는 최상위 값을 쓴다.
_LOCALES: dict[str, Any] = {
    lang: block
    for lang, block in (_STYLE.get("locales") or {}).items()
    if not lang.startswith("_") and isinstance(block, dict)
}


def max_line_chars_for(lang: str) -> int:
    """그 언어의 줄당 글자 상한 (ADR-0062).

    **글자 수는 픽셀 폭의 대리값이고 최상위 값은 전각 기준이다** — 라틴은 반각이라
    같은 자수가 절반 폭이다. 값이 없는 언어는 최상위 값을 그대로 쓴다 (ja).
    """
    return int(_LOCALES.get(lang, {}).get("max_line_chars", MAX_LINE_CHARS))


def title_max_line_chars_for(lang: str) -> int:
    """제목의 줄당 글자 상한 = 그 언어 자막 상한 ÷ 배율 (ADR-0074).

    **로케일이 남겨 둔 여유가 나눗셈에 딸려온다** — en이 45자 대신 42자를 쓰는 그 여유
    (ADR-0062)가 없으면 실측에서 en 최장 제목이 996px로 안전폭을 6px 넘었다. 안전폭에서
    직접 나누지 않고 자막 상한을 나누는 이유가 그것이다.
    """
    return max(1, max_line_chars_for(lang) // TITLE_FONT_SCALE)


def glyph_width_ratio_for(lang: str) -> float:
    """그 언어 한 글자의 가로 advance ÷ 폰트 크기. 전각이 1.0이고 라틴은 그보다 좁다."""
    return float(_LOCALES.get(lang, {}).get("glyph_width_ratio", GLYPH_WIDTH_RATIO))


class SubtitleError(Exception):
    """자막 문서를 만들 수 없음."""


# --- 줄바꿈 -----------------------------------------------------------------


def _split_at(text: str, index: int) -> tuple[str, str]:
    return text[:index].rstrip(), text[index:].lstrip()


def wrap_text(
    text: str, *, limit: int = MAX_LINE_CHARS, max_lines: int = MAX_LINES
) -> list[str]:
    """자막 한 줄을 화면 줄로 나눈다. 결과는 항상 `max_lines`개 이하다.

    - `limit`자 이하면 나누지 않는다
    - 나눌 때는 **공백에서만** 자른다. 어절 중간을 자르지 않는다
    - 후보 중 긴 쪽이 가장 짧아지는 지점을 고른다 (균형)
    - 공백이 하나도 없으면 어쩔 수 없이 가운데에서 자른다

    `limit`을 넘는 결과가 나올 수 있다 (모듈 독스트링의 충돌). 넘었는지는
    `overflow_warning()`이 판정한다 — 나누는 일과 판정을 섞지 않는다.
    """
    if max_lines < 1:
        raise SubtitleError(f"max_lines는 1 이상이어야 한다: {max_lines}")

    collapsed = " ".join(text.split())
    if not collapsed:
        raise SubtitleError("빈 자막 줄이다")
    if len(collapsed) <= limit or max_lines == 1:
        return [collapsed]

    # 1) 상한으로 채워 **가장 적은 줄 수**를 얻는다. `fill_text`는 상한을 넘는 줄을
    #    내지 않으므로 여기서 나온 줄 수가 이 큐에 필요한 최소값이다.
    base = fill_text(collapsed, limit=limit)
    if len(base) > max_lines:
        # 늘어난 줄 수로도 안 들어간다 — 판정은 `check_overflow`가 한다.
        return base

    # 2) 같은 줄 수를 유지하면서 **가장 좁은 폭**으로 다시 채운다 = 균형.
    #    자막은 큐마다 줄 길이가 고른 편이 보기 좋다 (제목과 다른 점).
    for width in range(-(-len(collapsed) // len(base)), limit):
        candidate = fill_text(collapsed, limit=width)
        if len(candidate) <= len(base):
            return candidate
    return base


def check_overflow(
    scene_id: int, lines: Sequence[str], *, limit: int = MAX_LINE_CHARS, lang: str = "",
) -> None:
    """`limit`자 × `MAX_LINES`줄에 안 들어가는 큐를 실패시킨다.

    **줄 수가 판정 기준이다** (ADR-0078). `wrap_text`가 상한을 넘는 줄을 내지 않으므로
    (채워쓰기), 안 들어가는 큐는 **줄 수가 넘치는** 모양으로 나타난다.

    **폰트를 줄여 삼키지 않는다.** 대본이 길면 줄을 늘려 받되(ADR-0078 — 대본이 상위고
    자막이 맞춘다) 글자 크기는 건드리지 않는다. 자막 단계가 글자를 작게 만들면 상류의
    길이가 화면에서만 티가 나고 기록에는 안 남는다.

    상한은 로케일의 것이다 (ADR-0062) — `lang`은 어느 언어의 대본을 가리킬지 정한다.

    **스펙 01의 `line_chars_max`를 인용하지 않는다.** 그 값은 `core_chars`(공백·부호 제외)로
    재고 여기는 화면에 그려지는 원문 글자로 재서 단위가 다르다 (ko 0.72 · ja 1.00 · en 0.79,
    석빙고 실측). 단위를 섞으면 어느 쪽을 고쳐야 할지 흐려진다 — ADR-0062 되돌릴 조건 5.
    """
    longest = max(len(line) for line in lines)
    if longest <= limit and len(lines) <= MAX_LINES:
        return
    where = f"{lang} 대본" if lang else "대본"
    raise SubtitleError(
        f"scenes/{scene_id}: 자막이 {len(lines)}줄(가장 긴 줄 {longest}자)이라 "
        f"{lang or 'ko'} 자막 폭 줄당 {limit}자 × {MAX_LINES}줄에 안 들어간다. "
        f"그 줄이 {where}에서 너무 길다는 뜻이다 — 1부에서 고쳐야 한다"
    )


def _kinsoku_cut(chunk: str, room: int, kinsoku: str) -> int:
    """`chunk`를 `room`자에서 자를 때 **다음 줄이 금칙 문자로 시작하지 않는** 자리.

    한 글자 앞으로 당긴다(追い出し) — 뒤로 미루면 줄이 길어져 폭 예산을 넘는다.
    한 칸만 본다: 금칙 문자가 연달아 오는 제목은 실측 19편에 없었다.
    """
    if kinsoku and room > 1 and len(chunk) > room and chunk[room] in kinsoku:
        return room - 1
    return room


def fill_text(text: str, *, limit: int, kinsoku: str = "") -> list[str]:
    """한 덩어리를 앞줄부터 채워 나눈다 (ADR-0074). **줄 수는 결과가 정한다.**

    - 공백에서 자른다. 한 덩어리가 `limit`을 넘으면 **그 글자 사이에서** 자른다
      (공백이 없는 CJK). 그 자리에만 금칙이 걸린다 — 공백은 원문이 띄어 쓴 자리다
    - 상한을 넘는 줄은 나오지 않는다. 몇 줄이 됐는지는 부르는 쪽이 판정한다

    **`wrap_text`(자막)와 다른 함수인 이유는 판정이 다르기 때문이다.** 자막은 큐마다
    균형이 보기 좋고 상한 초과가 **실패**지만, 제목은 한 덩어리이고 초과가 **강등**이며
    위에서부터 꽉 차야 크게 읽힌다 (실측: 균형 2분할로는 ja 60px · en 57px이 상한이다).
    """
    if limit < 1:
        raise SubtitleError(f"줄당 상한은 1 이상이어야 한다: {limit}")
    collapsed = " ".join(text.split())
    if not collapsed:
        raise SubtitleError("빈 제목이다")

    lines: list[str] = []
    current = ""
    for word in collapsed.split(" "):
        while len(word) > limit:  # 공백 없는 덩어리 — 글자 사이에서 자른다
            room = limit - (len(current) + 1 if current else 0)
            if room < 1:
                lines.append(current)
                current = ""
                continue
            cut = max(1, _kinsoku_cut(word, room, kinsoku))
            head = word[:cut]
            lines.append(f"{current} {head}" if current else head)
            current = ""
            word = word[cut:]
        candidate = f"{current} {word}" if current else word
        if current and len(candidate) > limit:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def subtitle_band(line_count: int) -> tuple[float, float]:
    """자막 블록이 차지하는 세로 구간 비율 `(위, 아래)`. specs/03 "72~82%" 검증용."""
    bottom = PLAY_RES_Y - MARGIN_V
    top = bottom - line_count * FONT_SIZE * LINE_HEIGHT_RATIO
    return top / PLAY_RES_Y, bottom / PLAY_RES_Y


def title_band(line_count: int) -> tuple[float, float]:
    """제목 블록이 차지하는 세로 구간 비율 `(위, 아래)` (ADR-0065).

    자막과 **기준선이 반대다** — 위끝이 고정이고 아래끝이 줄 수에 따라 내려간다.
    """
    top = TITLE_MARGIN_V
    bottom = top + line_count * TITLE_FONT_SIZE * LINE_HEIGHT_RATIO
    return top / PLAY_RES_Y, bottom / PLAY_RES_Y


# --- ASS 문서 ---------------------------------------------------------------


def escape_text(text: str) -> str:
    """ASS 이벤트 텍스트로 안전하게 만든다.

    `{`/`}`는 오버라이드 태그 블록을 여는 문자라 그대로 두면 자막이 통째로 사라진다.
    줄바꿈은 우리가 넣은 `\\N`만 남는다 — 원문의 개행은 `wrap_text`가 이미 지웠다.
    """
    return text.replace("{", r"\{").replace("}", r"\}")


def ass_timestamp(seconds: float) -> str:
    """ASS 시간 표기 `H:MM:SS.CC`. 해상도가 1/100초라 최대 5ms 반올림 오차가 난다."""
    if seconds < 0:
        raise SubtitleError(f"음수 시각이다: {seconds}")
    centis = int(round(seconds * 100))
    hours, centis = divmod(centis, 360_000)
    minutes, centis = divmod(centis, 6_000)
    secs, centis = divmod(centis, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def parse_timestamp(stamp: str) -> float:
    """`ass_timestamp`의 역함수. 만든 자막을 되읽어 검증할 때 쓴다."""
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2})\.(\d{2})", stamp.strip())
    if not match:
        raise SubtitleError(f"ASS 시간 표기가 아니다: {stamp!r}")
    hours, minutes, secs, centis = (int(g) for g in match.groups())
    return hours * 3600 + minutes * 60 + secs + centis / 100


@dataclass(frozen=True)
class Cue:
    """ASS 이벤트 한 줄 (= 씬 하나)."""

    start: float
    end: float
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        return " ".join(self.lines)


def _style_line(
    name: str, font_name: str, *, alignment: int, margin_v: int,
    font_size: int = FONT_SIZE, outline: Any = OUTLINE, shadow: Any = SHADOW,
) -> str:
    """ASS V4+ 스타일 한 줄. 필드 순서는 규격 그대로다.

    **폰트·색은 한 곳에서 나온다** — 따로 적으면 색이나 폰트가 한쪽만 바뀌는 사고가 난다.
    자막과 제목이 갈리는 것은 크기·외곽선·자리뿐이고, 그 셋만 인자로 받는다 (ADR-0074).
    """
    return (
        f"Style: {name},"
        f"{font_name},{font_size},"
        f"{PRIMARY_COLOUR},&H000000FF,{OUTLINE_COLOUR},&H00000000,"
        "-1,0,0,0,"  # Bold(-1=true), Italic, Underline, StrikeOut
        "100,100,0,0,"  # ScaleX, ScaleY, Spacing, Angle
        f"1,{outline},{shadow},"  # BorderStyle(1=외곽선+그림자)
        f"{alignment},{MARGIN_LR},{MARGIN_LR},{margin_v},1"
    )


def style_line(font_name: str = FONT_NAME) -> str:
    """specs/03 자막 스타일 한 줄. 폰트만 언어별이다 (스펙 04)."""
    return _style_line(
        SUBTITLE_STYLE_NAME, font_name, alignment=ALIGNMENT, margin_v=MARGIN_V
    )


def title_style_line(font_name: str = FONT_NAME) -> str:
    """제목 훅 스타일 한 줄 (ADR-0065·0074).

    자막과 다른 것은 **크기(2배)·외곽선(2배)·Alignment·세로 여백**이다. 외곽선이 같이
    커지는 이유는 대비다 — 글자만 키우면 굵기가 상대적으로 얇아져 배경에서 안 뜬다.
    """
    return _style_line(
        TITLE_STYLE_NAME, font_name, alignment=TITLE_ALIGNMENT, margin_v=TITLE_MARGIN_V,
        font_size=TITLE_FONT_SIZE, outline=TITLE_OUTLINE, shadow=TITLE_SHADOW,
    )


HEADER = "\n".join(
    (
        "[Script Info]",
        "; specs/03-visual-rules.md 자막 스타일 / ADR-0002 레이어 B",
        "ScriptType: v4.00+",
        f"PlayResX: {PLAY_RES_X}",
        f"PlayResY: {PLAY_RES_Y}",
        "WrapStyle: 2",  # 자동 줄바꿈 금지 — 줄 나누기는 wrap_text가 한다
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
    )
)

EVENTS_HEADER = "\n".join(
    (
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text",
    )
)


def title_event(title: str, end: float, *, limit: int) -> tuple[str, str]:
    """제목 훅 이벤트 한 줄 → `(Dialogue 줄, 경고)` (ADR-0065).

    구간은 `0` ~ 첫 씬의 `end`다 — 첫 씬이 끝나면 제목도 사라진다.

    **강등 사다리는 `굽기 → 없음`이다.** 줄 수 안에 안 들어가면 빈 줄과 경고를 돌려주고
    제목만 빠진다. 자막은 같은 상황에서 실패로 올리지만(`check_overflow`) **제목은
    마감이지 본편이 아니다** — 제목 하나 때문에 완성 영상을 잃지 않는다 (specs/05 D-5).
    """
    lines = fill_text(title, limit=limit, kinsoku=TITLE_KINSOKU)
    if len(lines) > TITLE_MAX_LINES:
        return "", (
            f"제목이 줄당 {limit}자 × {TITLE_MAX_LINES}줄에 안 들어가({len(lines)}줄) "
            f"제목 훅 없이 굽는다 (ADR-0065 강등 사다리). 제목: {title!r}"
        )
    text = LINE_BREAK.join(escape_text(line) for line in lines)
    return (
        f"Dialogue: 0,{ass_timestamp(0.0)},{ass_timestamp(end)},"
        f"{TITLE_STYLE_NAME},,0,0,0,,{text}"
    ), ""


def build_ass(
    scenes: Sequence[dict[str, Any]], *, font_name: str = FONT_NAME, lang: str = "",
    title: str = "",
) -> tuple[str, list[str]]:
    """`scenes.timed.{lang}.json`의 씬 배열 → (ASS 문서, 경고).

    씬의 `text`·`start`·`end`를 그대로 옮긴다. 대본을 고치지 않는다 (ADR-0017).
    `font_name`은 그 언어의 폰트이고(`font_name_for`), `lang`은 **줄당 상한**을 고른다
    (`max_line_chars_for`, ADR-0062) — 폰트 크기·줄 수는 세 언어가 같다.

    `title`이 있으면 **첫 씬 구간 동안 상단에 뜨는 제목 훅**을 얹는다 (ADR-0065).
    빈 문자열이면 지금까지와 완전히 같은 문서가 나온다 (D-3). 제목은 자막의 2배 크기라
    **줄당 상한도 줄 수도 자막과 다른 값을 쓴다** (ADR-0074).
    """
    limit = max_line_chars_for(lang) if lang else MAX_LINE_CHARS
    if not scenes:
        raise SubtitleError("씬이 없다")

    warnings: list[str] = []
    events: list[str] = []
    previous_end: float | None = None

    for scene in scenes:
        scene_id = scene["scene_id"]
        start = float(scene["start"])
        end = float(scene["end"])
        if end <= start:
            raise SubtitleError(f"scenes/{scene_id}: start({start}) >= end({end})")
        if previous_end is not None and start < previous_end - 1e-9:
            raise SubtitleError(
                f"scenes/{scene_id}: start({start})가 앞 씬의 end({previous_end})보다 이르다"
            )
        previous_end = end

        lines = wrap_text(scene["text"], limit=limit)
        check_overflow(scene_id, lines, limit=limit, lang=lang)

        text = LINE_BREAK.join(escape_text(line) for line in lines)
        events.append(
            f"Dialogue: 0,{ass_timestamp(start)},{ass_timestamp(end)},"
            f"Default,,0,0,0,,{text}"
        )

    styles = [style_line(font_name)]
    title_events: list[str] = []
    if title.strip():
        event, warning = title_event(
            title.strip(), float(scenes[0]["end"]),
            limit=title_max_line_chars_for(lang) if lang
            else max(1, MAX_LINE_CHARS // TITLE_FONT_SCALE),
        )
        if warning:
            warnings.append(warning)
        else:
            styles.append(title_style_line(font_name))
            title_events.append(event)

    document = "\n".join(
        (HEADER, *styles, "", EVENTS_HEADER, *title_events, *events)
    ) + "\n"
    return document, warnings


#: 이벤트 텍스트 맨 앞의 오버라이드 블록. 태그는 `\`로 시작하므로 escape된 `\{`와
#: 헷갈리지 않는다.
_OVERRIDE_RE = re.compile(r"^\{(\\[^}]*)\}")


def parse_ass(document: str) -> list[Cue]:
    """ASS 문서에서 **자막 큐만** 되읽는다. 만든 자막을 검증할 때만 쓴다 (verify.py).

    `Default`가 아닌 스타일의 이벤트는 건너뛴다 — 제목 훅(`Title`, ADR-0065)이 큐 목록에
    섞이면 씬 수가 하나 늘어 `[9]`의 싱크 검증(±200ms)이 통째로 어긋난다.
    """
    cues: list[Cue] = []
    for line in document.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        fields = line[len("Dialogue:") :].split(",", 9)
        if len(fields) < 10:
            raise SubtitleError(f"필드가 모자란 Dialogue 줄이다: {line!r}")
        if fields[3].strip() != SUBTITLE_STYLE_NAME:
            continue

        #: 큐별 오버라이드는 더 이상 만들지 않지만(폰트 축소 경로 없음), 손으로 고친
        #: 자막을 되읽어도 태그가 본문으로 새지 않도록 걷어내고 읽는다.
        body = _OVERRIDE_RE.sub("", fields[9])

        cues.append(
            Cue(
                start=parse_timestamp(fields[1]),
                end=parse_timestamp(fields[2]),
                lines=tuple(
                    part.replace(r"\{", "{").replace(r"\}", "}")
                    for part in body.split(LINE_BREAK)
                ),
            )
        )
    return cues
