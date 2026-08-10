"""대본 규칙 검증. specs/01-script-template.md의 검증 5항목 + 분량 규칙.

단위는 자막 줄(=씬)이다. 문장이 아니다 (ADR-0013).

## errors — 스펙 01 "검증 (validator가 체크할 항목)" 5개

1. 7단이 순서대로 모두 존재하는가
2. "그래서 발상을 뒤집습니다."가 44~54초 구간에 존재하는가
3. 총 글자 수·자막 줄 수가 범위 내인가
4. `solution` 구간에 구체적 숫자가 2개 이상 있는가
5. ending이 hook의 핵심 명사를 재사용하는가 (수미상관)

## warnings — 스펙 01 분량 규칙 중 5항목에 없는 것

줄당 글자 수 상한, 줄당 시간, 발화 속도, 총 길이. `est_*`는 TTS 이전 추정치라
실측으로 갱신되므로(스펙 05) 차단하지 않는다.

스펙 02 씬 스키마와 ADR-0007 그라운딩은 각각 다른 검증기가 맡는다. 이 모듈은
스키마가 이미 통과한 scenes.json을 받는다고 가정한다.
"""

from __future__ import annotations

import re
from typing import Any

from .grounding import extract_values

#: specs/01 분량 규칙. 원본 3편 실측을 모두 포함하는 엔벨로프다 —
#: 반올림값을 경계로 쓰면 백악 편(5.396자/초, 0.875초 줄)이 자기 기준에서 탈락한다.
TOTAL_CHARS = (545, 575)
LINE_COUNT = (23, 28)
LINE_CHARS_MAX = 43
LINE_SECONDS = (0.8, 6.9)
SPEED_RANGE = (5.3, 6.3)
TOTAL_SECONDS = (90.0, 102.0)

#: specs/01 — 어미까지 고정된 필수 문구는 이것 하나뿐 (3/3편)
TURNING_PHRASE = "그래서 발상을 뒤집습니다."
TURNING_WINDOW = (44.0, 54.0)

#: specs/01 검증 4항목
MIN_SOLUTION_NUMBERS = 2
#: 3단·4단이 각각 존재하려면 실패 대안이 두 번 나와야 한다
MIN_FAILED_SOLUTIONS = 2

#: 비트 → 템플릿 단계 (specs/02 대응 테이블). 튜플은 그 비트가 걸칠 수 있는 단계다.
STAGES_OF_BEAT: dict[str, tuple[int, ...]] = {
    "hook_fact": (1,),
    "hook_twist": (1,),
    "context": (2,),
    "context_number": (2, 6),
    "failed_solution": (3, 4),
    "failure_reason": (3, 4),
    "dilemma_peak": (4,),
    "turning_point": (5,),
    "solution_step": (6,),
    "solution_number": (6,),
    "present_link": (7,),
    "ending_echo": (7,),
}

#: 단계별로 하나는 있어야 하는 비트. 3단·4단은 비트만으로 구분되지 않아
#: (specs/02가 failed_solution·failure_reason을 "3, 4"로 둘 다 매핑한다)
#: MIN_FAILED_SOLUTIONS로 따로 센다.
REQUIRED_STAGE_BEATS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (1, "hook", ("hook_fact", "hook_twist")),
    (2, "context", ("context", "context_number")),
    (5, "turning_point", ("turning_point",)),
    (6, "solution", ("solution_step", "solution_number")),
    (7, "ending", ("present_link", "ending_echo")),
)

HOOK_BEATS = ("hook_fact", "hook_twist")
ENDING_BEATS = ("present_link", "ending_echo")
SOLUTION_BEATS = ("solution_step", "solution_number")

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
    '파비아의' → '파비아'처럼 아/어/야로 끝나는 명사가 통째로 사라진다. 도메인이
    국가 무관이 된 뒤로(ADR-0016) 그런 외래 고유명사가 기본값이라 이건 예외가
    아니다 — 피사 편 첫 대본의 수미상관(파비아)이 실제로 이걸로 오탐을 맞았다.

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


def _join(scenes: list[dict[str, Any]], beats: tuple[str, ...]) -> str:
    return " ".join(str(s.get("text", "")) for s in scenes if s.get("beat") in beats)


def _stage_order_errors(scenes: list[dict[str, Any]]) -> list[str]:
    """검증 1항목: 7단이 순서대로 모두 존재하는가."""
    errors: list[str] = []
    beats = [s.get("beat") for s in scenes]

    # 순서: 각 비트에 '앞 단계 이상'인 최소 단계를 그리디로 배정한다.
    # 배정이 불가능하면 단계가 역행한 것이다.
    current = 1
    for scene in scenes:
        options = [st for st in STAGES_OF_BEAT.get(scene.get("beat"), ()) if st >= current]
        if not options:
            errors.append(
                f"scenes/{scene.get('scene_id')}: 비트 '{scene.get('beat')}'가 "
                f"{current}단 뒤에 올 수 없다 (7단 순서 역행)"
            )
            break
        current = min(options)

    for stage, name, candidates in REQUIRED_STAGE_BEATS:
        if not any(b in candidates for b in beats):
            errors.append(f"beats: {stage}단({name}) 비트가 없다 — {' | '.join(candidates)} 중 하나 필요")

    failed = beats.count("failed_solution")
    if failed < MIN_FAILED_SOLUTIONS:
        errors.append(
            f"beats: failed_solution이 {failed}회다 — 3단·4단에 각각 하나씩 "
            f"최소 {MIN_FAILED_SOLUTIONS}회 필요"
        )
    return errors


def validate_script(scenes: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(errors, warnings)를 돌려준다. errors가 비어야 스펙 01 통과."""
    errors: list[str] = []
    warnings: list[str] = []
    items: list[dict[str, Any]] = [s for s in scenes.get("scenes", []) if isinstance(s, dict)]
    if not items:
        return ["scenes: 씬이 없다"], []

    full_text = " ".join(str(s.get("text", "")) for s in items)
    total_chars = len(core_chars(full_text))
    duration = float(scenes.get("total_duration") or items[-1].get("est_end", 0))

    # 1. 7단 순서
    errors.extend(_stage_order_errors(items))

    # 2. 필수 시그니처 문구와 그 위치
    hits = [s for s in items if TURNING_PHRASE in str(s.get("text", ""))]
    if not hits:
        errors.append(f"text: 필수 문구 '{TURNING_PHRASE}'가 없다")
    else:
        start = float(hits[0].get("est_start", 0))
        low, high = TURNING_WINDOW
        if not low <= start <= high:
            errors.append(
                f"scenes/{hits[0].get('scene_id')}: '{TURNING_PHRASE}'가 {start:.1f}초에 있다 "
                f"({low:.0f}~{high:.0f}초 구간이어야 한다)"
            )

    # 3. 총 글자 수 · 자막 줄 수
    if not TOTAL_CHARS[0] <= total_chars <= TOTAL_CHARS[1]:
        errors.append(
            f"text: 총 {total_chars}자 (범위 {TOTAL_CHARS[0]}~{TOTAL_CHARS[1]}자)"
        )
    if not LINE_COUNT[0] <= len(items) <= LINE_COUNT[1]:
        errors.append(
            f"scenes: 자막 {len(items)}줄 (범위 {LINE_COUNT[0]}~{LINE_COUNT[1]}줄)"
        )

    # 4. solution 구간 숫자
    solution_numbers = extract_values(_join(items, SOLUTION_BEATS))
    if len(solution_numbers) < MIN_SOLUTION_NUMBERS:
        errors.append(
            f"beats/solution: 구체적 숫자가 {len(solution_numbers)}개다 "
            f"(최소 {MIN_SOLUTION_NUMBERS}개)"
        )

    # 5. 수미상관
    shared = noun_stems(_join(items, HOOK_BEATS)) & noun_stems(_join(items, ENDING_BEATS))
    if not shared:
        errors.append("text: ending이 hook의 명사를 하나도 재사용하지 않는다 (수미상관 실패)")

    # --- 분량 규칙 (경고) ---
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
