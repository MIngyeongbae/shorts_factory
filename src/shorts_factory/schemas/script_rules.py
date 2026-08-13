"""대본 규칙 검증. specs/01-script-template.md + specs/schema/script-rules.json.

단위는 자막 줄(=씬)이다. 문장이 아니다 (ADR-0013).

## 무엇을 막는가 (errors)

1. 총 글자 수·자막 줄 수가 범위 내인가
2. 엔딩이 훅의 핵심 명사를 재사용하는가 (수미상관)

**구조 검증은 없다** (ADR-0033 §5). 단 구성이 소재마다 다르므로 기계가 볼 수 있는 것이
아니고, 서사가 성립하는지는 `[2b] judge`가 본다. 예전의 순서·필수 비트·시그니처 위치
검사가 사라진 자리다.

## 무엇을 알리는가 (warnings)

줄당 글자 수 상한, 줄당 시간, 발화 속도, 총 길이, 시그니처 문구 부재, 대본 전체 숫자
개수. `est_*`는 TTS 이전 추정치라 실측으로 갱신되므로(스펙 05) 차단하지 않는다.

값은 전부 `specs/schema/script-rules.json`에서 로드한다 (ADR-0034 §3). 씬 스키마와
ADR-0007 그라운딩은 각각 다른 검증기가 맡는다.
"""

from __future__ import annotations

import re
from typing import Any

from . import vocab
from .grounding import extract_values

_LIMITS = vocab.limits()
_CHECKS = vocab.checks()

#: specs/schema/script-rules.json — 원본 3편 실측을 모두 포함하는 엔벨로프다.
TOTAL_CHARS = tuple(_LIMITS["total_chars"])
LINE_COUNT = tuple(_LIMITS["line_count"])
LINE_CHARS_MAX = _LIMITS["line_chars_max"]
LINE_SECONDS = tuple(_LIMITS["line_seconds"])
SPEED_RANGE = tuple(_LIMITS["speed_cps"])
TOTAL_SECONDS = tuple(float(v) for v in _LIMITS["total_seconds"])

#: 대본 전체 기준. 구간을 비트로 자를 수 없으므로 편 단위로 센다 (ADR-0033).
MIN_NUMBERS = _CHECKS["min_numbers"]

#: 시그니처 문구. 필수가 아니라 권장이고, 없으면 경고다 (ADR-0033 §2).
SIGNATURES = vocab.signature_phrases()
PRIMARY_SIGNATURE = next(
    (item["phrase"] for item in SIGNATURES if item.get("primary")), ""
)

#: 수미상관을 볼 때 훅 쪽·엔딩 쪽으로 치는 비트 (vocab.json `meta.beat.position`).
_BEAT_META = vocab.meta("beat")
HOOK_BEATS = tuple(b for b, m in _BEAT_META.items() if m.get("position") == "open")
ENDING_BEATS = tuple(b for b, m in _BEAT_META.items() if m.get("position") == "close")

#: 라벨이 하나도 없는 대본에서 앞뒤로 떼어 볼 비율. 최소 1씬은 본다.
EDGE_RATIO = 0.15

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


def _text_of(scenes: list[dict[str, Any]]) -> str:
    return " ".join(str(s.get("text", "")) for s in scenes)


def _edge_scenes(
    items: list[dict[str, Any]], beats: tuple[str, ...], *, head: bool
) -> list[dict[str, Any]]:
    """훅 쪽 / 엔딩 쪽 씬. 라벨이 있으면 라벨이 이긴다.

    비트는 이제 라벨일 뿐이고 어느 편에서든 있으리라는 보장이 없다 (ADR-0033 §4).
    하나도 없으면 앞뒤 씬을 떼어 본다 — 수미상관은 구조가 아니라 결과에 거는 제약이라
    라벨이 없다고 검사를 포기하지 않는다.
    """
    tagged = [s for s in items if s.get("beat") in beats]
    if tagged:
        return tagged
    size = max(1, round(len(items) * EDGE_RATIO))
    return items[:size] if head else items[-size:]


def validate_script(scenes: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(errors, warnings)를 돌려준다. errors가 비어야 스펙 01 통과."""
    errors: list[str] = []
    warnings: list[str] = []
    items: list[dict[str, Any]] = [s for s in scenes.get("scenes", []) if isinstance(s, dict)]
    if not items:
        return ["scenes: 씬이 없다"], []

    full_text = _text_of(items)
    total_chars = len(core_chars(full_text))
    duration = float(scenes.get("total_duration") or items[-1].get("est_end", 0))

    # 1. 총 글자 수 · 자막 줄 수
    if not TOTAL_CHARS[0] <= total_chars <= TOTAL_CHARS[1]:
        errors.append(
            f"text: 총 {total_chars}자 (범위 {TOTAL_CHARS[0]}~{TOTAL_CHARS[1]}자)"
        )
    if not LINE_COUNT[0] <= len(items) <= LINE_COUNT[1]:
        errors.append(
            f"scenes: 자막 {len(items)}줄 (범위 {LINE_COUNT[0]}~{LINE_COUNT[1]}줄)"
        )

    # 2. 수미상관
    opening = _edge_scenes(items, HOOK_BEATS, head=True)
    closing = _edge_scenes(items, ENDING_BEATS, head=False)
    if not noun_stems(_text_of(opening)) & noun_stems(_text_of(closing)):
        errors.append("text: 엔딩이 훅의 명사를 하나도 재사용하지 않는다 (수미상관 실패)")

    # --- 경고 ---
    if PRIMARY_SIGNATURE and PRIMARY_SIGNATURE not in full_text:
        warnings.append(
            f"시그니처 문구 '{PRIMARY_SIGNATURE}'가 없다 — 문제 해결 서사가 아니면 "
            "쓰지 않아도 된다 (ADR-0033 §2)"
        )

    numbers = extract_values(full_text)
    if len(numbers) < MIN_NUMBERS:
        warnings.append(
            f"대본 전체의 구체적 숫자가 {len(numbers)}개다 (권장 최소 {MIN_NUMBERS}개)"
        )

    if not TOTAL_SECONDS[0] <= duration <= TOTAL_SECONDS[1]:
        warnings.append(
            f"total_duration: {duration:.1f}초 (권장 {TOTAL_SECONDS[0]:.0f}~{TOTAL_SECONDS[1]:.0f}초)"
        )
    if duration > 0:
        speed = total_chars / duration
        if not SPEED_RANGE[0] <= speed <= SPEED_RANGE[1]:
            warnings.append(
                f"발화 속도 {speed:.2f}자/초 (권장 {SPEED_RANGE[0]}~{SPEED_RANGE[1]}자/초)"
            )
    for scene in items:
        sid = scene.get("scene_id")
        chars = len(core_chars(str(scene.get("text", ""))))
        if chars > LINE_CHARS_MAX:
            warnings.append(f"scenes/{sid}: {chars}자 (줄당 최대 {LINE_CHARS_MAX}자)")
        span = float(scene.get("est_end", 0)) - float(scene.get("est_start", 0))
        if not LINE_SECONDS[0] <= span <= LINE_SECONDS[1]:
            warnings.append(
                f"scenes/{sid}: {span:.2f}초 (줄당 {LINE_SECONDS[0]}~{LINE_SECONDS[1]}초)"
            )

    return errors, warnings
