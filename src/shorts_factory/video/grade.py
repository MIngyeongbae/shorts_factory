"""채널 룩 — 언어별 그레이드 한 줄. ADR-0093.

specs/03-visual-rules.md 「채널 룩」:

    `[9. assemble]`이 씬 클립마다 거는 **아주 약한 색보정**이다. 정체성이지 교정이 아니다.

## 왜 이 모듈이 따로 있는가

값이 `specs/schema/channel-look.json`에 있고 코드는 로드만 한다 (ADR-0034 §3). 사람이
렌더를 보며 고치는 값이라 **파일 하나만 고치면 코드가 따라온다** — 되돌릴 조건 1·3·4가
전부 그 파일 안에서 끝난다.

## 거는 자리가 계약의 일부다

`clip_filter`(입력마다)에 얹는다. 두 가지가 여기서 나온다.

- **자막·제목이 안 물든다.** `ass` 번인은 `build_filter_graph`의 마지막 단계라
  클립 쪽 필터가 글자보다 앞이다
- **엔딩 실사를 뺀다** (`Segment.is_scene`). **선택이 아니라 제약이다** — ADR-0055의
  무수정 표시가 `cc-by-sa` 사진을 영상에 실을 근거이고 **색보정은 개작이다**

## 없는 것이 정상인 언어가 있다

ko는 빈 문자열이다 — 그레이드 없이 올라간 편이 이미 쌓여 있어 사람이 뺐다 (ADR-0093
맥락 4). 부재는 경고가 아니다 (specs/05 D-3). 나중에 넣기로 하면 계약 파일만 채운다.
"""

from __future__ import annotations

from ..schemas import vocab

#: 언어별 그레이드 필터. 값은 계약이 지고 코드는 로드한다 (ADR-0034 §3).
_GRADE: dict[str, str] = {
    lang: str(value)
    for lang, value in vocab.CHANNEL_LOOK["meta"]["grade"].items()
    if not lang.startswith("_")
}


def grade_for(lang: str) -> str:
    """그 언어의 그레이드 필터 한 줄. **없으면 빈 문자열이고 그것이 정상이다.**

    모르는 언어도 빈 문자열이다 — 언어가 늘어도 조립이 죽지 않는다 (D-3).
    """
    return _GRADE.get(lang, "")


def languages_with_grade() -> tuple[str, ...]:
    """그레이드를 지는 언어. 계약을 되짚는 자리이고 배치를 정하지 않는다."""
    return tuple(lang for lang, value in _GRADE.items() if value)


__all__ = ["grade_for", "languages_with_grade"]
