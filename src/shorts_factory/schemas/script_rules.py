"""대본 규칙 — 엔벨로프 값과 텍스트 유틸. specs/01 + specs/schema/script-rules.json.

단위는 자막 줄(=씬)이다. 문장이 아니다 (ADR-0013). **대본 검증기는 여기 없다** —
`[1] draft` 직후의 기계 엔벨로프 검사는 `stages/scriptmd.check_script_md`가 한다
(ADR-0049. 씬 계약 기반의 옛 검증기는 ADR-0052가 지웠다).

값은 전부 `specs/schema/script-rules.json`에서 로드한다 (ADR-0034 §3).
"""

from __future__ import annotations

import re

from . import vocab

_LIMITS = vocab.limits()

#: specs/schema/script-rules.json — 원본 3편 실측을 모두 포함하는 엔벨로프다.
TOTAL_CHARS = tuple(_LIMITS["total_chars"])
LINE_COUNT = tuple(_LIMITS["line_count"])
LINE_CHARS_MAX = _LIMITS["line_chars_max"]
LINE_SECONDS = tuple(_LIMITS["line_seconds"])
SPEED_RANGE = tuple(_LIMITS["speed_cps"])
TOTAL_SECONDS = tuple(float(v) for v in _LIMITS["total_seconds"])


def max_total_seconds(lang: str = "ko") -> float:
    """그 언어의 총 길이 상한. 로케일 블록이 있으면 그것, 없으면 ko의 상한이다 (ADR-0056 결정 5).

    ja·en은 하한을 보지 않는다 — en은 ko의 ×0.78이라 90초를 깨는데 짧은 쇼츠는 손해가
    아니다 (스펙 05 `[2l]`·`[3]`).
    """
    bound = vocab.locale_limits(lang).get("total_seconds")
    if isinstance(bound, list) and len(bound) == 2:
        return float(bound[1])
    return TOTAL_SECONDS[1]

_PUNCT = re.compile(r"[\s.,!?…·「」『』\"'()\[\]\-~:;]")
_HANGUL = re.compile(r"[가-힣]+")

#: 조사 — 긴 것부터 떼어낸다
_JOSA = (
    "에서만", "에게서", "이라도", "까지도", "만큼은", "으로는", "에서는", "이라는",
    "까지", "부터", "에게", "한테", "보다", "처럼", "만큼", "이나", "조차", "마저",
    "에는", "으로", "이랑", "에서", "라도", "에도", "이란", "라는",
    "은", "는", "이", "가", "을", "를", "에", "의", "도", "만", "로", "와", "과", "랑", "뿐", "께",
)
#: 용언 종결 어미로 끝나는 어절은 명사 후보에서 제외
_VERB_END = ("다", "죠", "요", "까", "네", "군", "걸", "지", "야", "어", "아")
#: 수미상관 판정에서 걸러낼 기능어·부사
_STOPWORDS = frozenset(
    {"그런데", "그리고", "그래서", "하지만", "지금", "이건", "그건", "저건",
     "여기", "거기", "우리", "자기", "때문", "정말", "이번", "다시", "바로",
     "아주", "가장", "이제", "조금", "모두", "전부"}
)


def core_chars(text: str) -> str:
    """공백·문장부호를 제외한 본문 (specs/01 글자 수 기준)."""
    return _PUNCT.sub("", text or "")


def noun_stems(text: str) -> set[str]:
    """명사 후보 어간. 형태소 분석기 없이 조사만 떼는 휴리스틱이다.

    용언 판정은 **어절 원형에만** 한다. 조사를 뗀 어간에 같은 검사를 다시 걸면
    '파비아의' → '파비아'처럼 아/어/야로 끝나는 명사가 통째로 사라진다. 도메인 제한이
    없어진 뒤로(ADR-0016·0033) 그런 외래 고유명사가 기본값이라 이건 예외가 아니다 —
    피사 편 첫 대본의 수미상관(파비아)이 실제로 이걸로 오탐을 맞았다.

    남는 한계: 조사가 붙지 않은 채 아/어/다로 끝나는 명사('바다', '언어')는 여전히
    용언으로 보고 버린다. 조사가 붙으면('바다에') 잡힌다.
    """
    found: set[str] = set()
    for word in _HANGUL.findall(text or ""):
        if word.endswith(_VERB_END):
            continue
        for josa in _JOSA:
            if word.endswith(josa) and len(word) - len(josa) >= 2:
                word = word[: -len(josa)]
                break
        if len(word) >= 2 and word not in _STOPWORDS:
            found.add(word)
    return found


