"""문자 정렬 → 씬(자막 줄) 경계. specs/04-audio-rules.md, specs/05-pipeline.md, ADR-0013.

이 모듈은 파일도 네트워크도 만지지 않는다. `[3. tts+sync]`의 실제 판단은 전부 여기 있고,
API 키 없이 검증된다.

## 경계를 뽑는 방법 (ADR-0013)

    "줄의 끝은 항상 문장의 끝이므로, 문장 경계를 뽑은 뒤 각 줄의 마지막 문장 끝만 취한다"

씬 하나에 문장이 1~3개 들어갈 수 있으므로(ADR-0013) 문장 경계를 그대로 씬 경계로 쓰면
씬이 쪼개진다. 그래서 **줄 안의 마지막 문장부호**가 그 줄의 끝이다.

문장부호를 텍스트에서 찾지 않고 **정렬 배열 안에서** 찾는 이유는, 씬 텍스트를 이어 붙인
문자열과 정렬 배열이 1:1로 대응한다는 사실을 매번 확인하기 위해서다(`character_spans`).
어긋나면 조용히 밀린 타임스탬프를 내놓는 대신 실패한다.

"1.07" 같은 소수점이 문장 끝으로 오인되지 않는 것도 이 규칙 덕이다 — 줄 구간 안에서
**마지막** 문장부호만 보므로 줄 중간의 소수점은 후보가 되지 못한다.

## 씬은 빈틈 없이 이어 붙인다

`start`는 앞 씬의 `end`다 (첫 씬만 0.0). 문장 사이의 숨·묵음을 어느 씬에도 넣지 않으면
타임라인에 구멍이 생기고, `[7. motion]`의 "클립 길이 = 씬 길이 + 디졸브 겹침 0.6초"와
`[9. assemble]`의 자막 번인이 그 구멍을 메울 근거를 갖지 못한다. 1부의 `est_*`도 같은
방식으로 이어져 있어(`est_start[n+1] == est_end[n]`) 추정↔실측 비교가 어긋나지 않는다.
"""

from __future__ import annotations

from typing import Any, Sequence

from .base import Alignment

#: 씬 텍스트를 이어 붙일 때 쓰는 구분자. 줄마다 이미 문장부호로 끝나므로 문장부호를
#: 더하지 않는다 (specs/04: 쉼 제어는 대본 텍스트의 문장부호로만).
LINE_JOINER = " "

#: 문장 끝으로 인정하는 문자.
SENTENCE_ENDINGS = (".", "?", "!", "…")

#: 실측-추정 오차 경고 임계(초). specs/05 "실측-추정 오차 씬당 ±1.5초 초과 시 경고".
DRIFT_TOLERANCE = 1.5


class SyncError(Exception):
    """정렬과 대본이 맞지 않아 씬 경계를 확정할 수 없음."""


def narration_text(texts: Sequence[str], *, joiner: str = LINE_JOINER) -> str:
    """TTS에 보낼 대본 전체. 씬 순서 그대로 한 덩어리로 만든다 (ADR-0004 단일 호출)."""
    return joiner.join(texts)


def character_spans(
    alignment: Alignment, texts: Sequence[str], *, joiner: str = LINE_JOINER
) -> list[tuple[int, int]]:
    """각 씬이 정렬 배열에서 차지하는 `[시작, 끝)` 인덱스.

    정렬이 보낸 텍스트와 글자 하나라도 다르면 실패한다. 관용적으로 맞춰 주면 어긋난
    구간부터 모든 씬의 타임스탬프가 조용히 밀린다 — 그건 영상 전체의 싱크가 깨진
    뒤에야 드러난다.
    """
    if not texts:
        raise SyncError("씬이 없다")

    expected = narration_text(texts, joiner=joiner)
    actual = alignment.text
    if actual != expected:
        raise SyncError(_mismatch_message(expected, actual))

    spans: list[tuple[int, int]] = []
    cursor = 0
    for text in texts:
        spans.append((cursor, cursor + len(text)))
        cursor += len(text) + len(joiner)
    return spans


def _mismatch_message(expected: str, actual: str) -> str:
    limit = min(len(expected), len(actual))
    index = next((i for i in range(limit) if expected[i] != actual[i]), limit)
    lo = max(0, index - 20)
    return (
        "정렬이 보낸 대본과 다르다 "
        f"(대본 {len(expected)}자 / 정렬 {len(actual)}자, {index}번째 문자부터 어긋남)\n"
        f"  대본: …{expected[lo:index + 20]!r}\n"
        f"  정렬: …{actual[lo:index + 20]!r}\n"
        "  normalized_alignment를 넘기지 않았는지 확인하라 (ADR-0004)."
    )


def _sentence_end_index(alignment: Alignment, lo: int, hi: int) -> int | None:
    """`[lo, hi)` 안의 마지막 문장부호 인덱스."""
    for index in range(hi - 1, lo - 1, -1):
        if alignment.characters[index] in SENTENCE_ENDINGS:
            return index
    return None


def scene_boundaries(
    alignment: Alignment, texts: Sequence[str], *, joiner: str = LINE_JOINER
) -> tuple[list[tuple[float, float]], list[str]]:
    """씬별 `(start, end)` 실측값과 경고 목록. 시각은 원속(atempo 적용 전) 기준이다."""
    spans = character_spans(alignment, texts, joiner=joiner)
    warnings: list[str] = []
    boundaries: list[tuple[float, float]] = []

    start = 0.0
    for scene_id, (lo, hi) in enumerate(spans, start=1):
        index = _sentence_end_index(alignment, lo, hi)
        if index is None:
            # ADR-0013의 전제("줄의 끝은 항상 문장의 끝")가 깨진 줄. 마지막 문자로
            # 대신하되 조용히 넘어가지 않는다 — 대본 쪽 문제라 1부가 알아야 한다.
            index = hi - 1
            warnings.append(
                f"scenes/{scene_id}: 줄이 문장부호로 끝나지 않아 마지막 문자를 줄 끝으로 썼다"
            )
        end = alignment.ends[index]
        if end <= start:
            raise SyncError(
                f"scenes/{scene_id}: 실측 end({end:.3f}초)가 start({start:.3f}초) 이하다. "
                "정렬 시각이 역행하거나 씬이 0초다"
            )
        boundaries.append((start, end))
        start = end

    return boundaries, warnings


def scale(
    boundaries: Sequence[tuple[float, float]], factor: float, *, ndigits: int = 3
) -> list[tuple[float, float]]:
    """모든 시각에 배속 보정을 건다.

    ADR-0004: 원속 생성 후 FFmpeg `atempo 1.1`이 기본값이다. 오디오가 1.1배 빨라지므로
    타임스탬프는 `1/1.1`배가 된다 (specs/05).

    이어 붙인 구조를 유지하려고 `end`만 스케일하고 `start`는 앞 씬의 `end`를 그대로
    받는다. 반올림 때문에 `start != 앞 end`가 되는 것을 막는다.
    """
    scaled: list[tuple[float, float]] = []
    start = 0.0
    for _, end in boundaries:
        scaled_end = round(end * factor, ndigits)
        scaled.append((start, scaled_end))
        start = scaled_end
    return scaled


def drift_warnings(
    boundaries: Sequence[tuple[float, float]],
    scenes: Sequence[dict[str, Any]],
    *,
    tolerance: float = DRIFT_TOLERANCE,
) -> list[str]:
    """실측과 1부 추정(`est_*`)의 씬당 오차 경고. specs/05.

    비교 대상은 **배속 보정 후** 실측값이다. `est_*`는 1부가 명목 발화 속도로 잡은
    최종 영상 기준 추정치이므로 같은 시간축에서만 비교가 성립한다.

    차단하지 않는다 — 추정이 빗나간 것은 대본의 결함이 아니고, 2부는 1부를 다시
    돌리지 않는다 (ADR-0017).
    """
    warnings: list[str] = []
    for (start, end), scene in zip(boundaries, scenes):
        est_start = float(scene.get("est_start", 0.0))
        est_end = float(scene.get("est_end", 0.0))
        delta_start = start - est_start
        delta_end = end - est_end
        if max(abs(delta_start), abs(delta_end)) > tolerance:
            warnings.append(
                f"scenes/{scene.get('scene_id')}: 실측 {start:.2f}~{end:.2f}초 / "
                f"추정 {est_start:.2f}~{est_end:.2f}초 — 오차 "
                f"{delta_start:+.2f}/{delta_end:+.2f}초 (허용 ±{tolerance}초)"
            )
    return warnings
