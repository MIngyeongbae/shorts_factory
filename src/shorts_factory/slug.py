"""한글 소재명 → 디렉터리 슬러그.

`topics/{slug}/`는 폴더 이름이므로 **결정성**이 언어학적 정확성보다 중요하다.
음운 변화(자음동화·구개음화 등)를 적용하지 않는 음절 단위 국어의 로마자 표기법을
쓴다. 같은 소재명은 언제 돌려도 같은 슬러그가 나온다.

예) "한양도성 각자성석" -> "hanyangdoseong-gakjaseongseok"
"""

from __future__ import annotations

import re
import unicodedata

_BASE = 0xAC00
_LAST = 0xD7A3

# 국어의 로마자 표기법 (문화체육관광부 고시) 기준 음절 구성요소 표기
_CHOSEONG = (
    "g", "kk", "n", "d", "tt", "r", "m", "b", "pp",
    "s", "ss", "", "j", "jj", "ch", "k", "t", "p", "h",
)
_JUNGSEONG = (
    "a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa",
    "wae", "oe", "yo", "u", "wo", "we", "wi", "yu", "eu", "ui", "i",
)
# 28개(받침 없음 + 27종). 순서는 유니코드 종성 인덱스와 정확히 일치해야 한다.
# ㄱ ㄲ ㄳ ㄴ ㄵ ㄶ ㄷ ㄹ ㄺ ㄻ ㄼ ㄽ ㄾ ㄿ ㅀ ㅁ ㅂ ㅄ ㅅ ㅆ ㅇ ㅈ ㅊ ㅋ ㅌ ㅍ ㅎ
_JONGSEONG = (
    "", "k", "k", "k", "n", "n", "n", "t", "l", "k",
    "m", "l", "l", "l", "p", "l", "m", "p", "p", "t",
    "t", "ng", "t", "t", "k", "t", "p", "t",
)
assert len(_CHOSEONG) == 19 and len(_JUNGSEONG) == 21 and len(_JONGSEONG) == 28

#: 슬러그 최대 길이. 넘으면 음절 경계가 아닌 하이픈 경계에서 자른다.
MAX_SLUG_LEN = 60


def romanize(text: str) -> str:
    """한글 음절을 로마자로 옮긴다. 한글이 아닌 문자는 그대로 통과시킨다."""
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if _BASE <= code <= _LAST:
            offset = code - _BASE
            out.append(_CHOSEONG[offset // 588])
            out.append(_JUNGSEONG[(offset % 588) // 28])
            out.append(_JONGSEONG[offset % 28])
        else:
            out.append(ch)
    return "".join(out)


def slugify(text: str) -> str:
    """소재명을 [a-z0-9-] 슬러그로 변환한다."""
    if not text or not text.strip():
        raise ValueError("소재명이 비어 있어 슬러그를 만들 수 없다")

    romanized = romanize(text.strip())
    # 로마자화되지 않은 라틴 문자의 악센트를 제거
    normalized = unicodedata.normalize("NFKD", romanized)
    ascii_only = "".join(c for c in normalized if not unicodedata.combining(c))

    lowered = ascii_only.lower()
    hyphenated = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")

    if not hyphenated:
        raise ValueError(f"슬러그를 만들 수 없는 소재명이다: {text!r}")

    if len(hyphenated) > MAX_SLUG_LEN:
        head = hyphenated[:MAX_SLUG_LEN]
        # 단어 중간에서 잘리면 마지막 하이픈까지 되돌린다
        if "-" in head:
            head = head.rsplit("-", 1)[0]
        hyphenated = head.strip("-")

    return hyphenated
