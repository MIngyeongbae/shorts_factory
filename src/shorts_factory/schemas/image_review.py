"""`[6r] imagereview`의 판정 어휘와 세션 출력 검증. ADR-0031 §1~3.

## 왜 별도 모듈인가

판정 어휘(`pass`/`pick`/`redo`)를 **스테이지 코드가 선언하지 않게** 하려는 것이다.
`[6]`의 상태 문자열이 `stages/imagegen.py`에 흩어져 있는 것과 다른 선택이고, 이유는
이 셋이 **세션 출력의 계약**이기 때문이다 — 프롬프트가 내는 값과 코드가 받는 값이
갈라지면 그 씬은 조용히 판정 없이 통과한다.

`specs/schema/`에 두지 않은 이유는 `image_review.json`이 **계약이 아니라 기록**이기
때문이다 (specs/05 2부 계약 표). 하류 단계가 판단 근거로 읽는 파일이 아니라 `[11] report`와
사람이 읽는 실행 기록이고, `images.json`·`clips.json`·`timing.json`이 같은 성격이다.

## 검증이 무는 것

세션이 낸 판정을 **그대로 믿지 않는다.** 이미지를 보고 내리는 판단이라 기계가 대신
내릴 수는 없지만, **형식과 범위**는 기계가 지킬 수 있다.

- 판정 대상 씬이 빠지거나 모르는 씬이 끼면 실패한다. 일부만 판정하면 나머지가 왜
  통과했는지 나중에 구분할 수 없다
- 후보가 없는 씬에 `pick`이 오면 실패한다. 바꿔 낄 파일이 없다
- 없는 후보 이름(`q7`)이 오면 실패한다
"""

from __future__ import annotations

import re
from typing import Any

#: 세션이 낼 수 있는 판정. ADR-0031 §1이 정한 셋이고 순서는 기각 강도 순이다.
PASS = "pass"
PICK = "pick"
REDO = "redo"

VERDICTS: tuple[str, ...] = (PASS, PICK, REDO)

#: 후보 이름. `[6]`이 `images/_cand/{scene_id}/q{n}{suffix}`로 남긴 그 `q{n}`이다.
CANDIDATE_PATTERN = re.compile(r"^q(\d+)$")


class ImageReviewError(Exception):
    """세션 출력이 판정으로 성립하지 않는다."""


def candidate_index(name: str) -> int:
    """`"q2"` → `2`. 형식이 아니면 실패한다."""
    match = CANDIDATE_PATTERN.match(name.strip()) if isinstance(name, str) else None
    if match is None:
        raise ImageReviewError(f"후보 이름이 q{{n}} 형식이 아니다: {name!r}")
    return int(match.group(1))


def parse_reviews(
    payload: dict[str, Any], candidates: dict[int, list[str]]
) -> dict[int, dict[str, Any]]:
    """세션 출력 → `{scene_id: 판정}`.

    `candidates`는 씬별 후보 파일 목록이다 (`[6]`의 `images.json`에서 온다).
    빈 목록인 씬은 `pick`을 받을 수 없다 — 바꿔 낄 파일이 없다.
    """
    raw = payload.get("reviews")
    if not isinstance(raw, list):
        raise ImageReviewError("세션 출력에 reviews 배열이 없다")

    expected = set(candidates)
    reviews: dict[int, dict[str, Any]] = {}
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ImageReviewError(f"{index}번째 항목이 객체가 아니다: {item!r}")

        sid = item.get("scene_id")
        if sid not in expected:
            raise ImageReviewError(f"{index}번째 항목의 scene_id가 대상에 없다: {sid!r}")
        if sid in reviews:
            raise ImageReviewError(f"씬 {sid}가 두 번 나왔다")

        verdict = item.get("verdict")
        if verdict not in VERDICTS:
            raise ImageReviewError(
                f"씬 {sid}의 verdict가 {'/'.join(VERDICTS)} 중에 없다: {verdict!r}"
            )

        reason = str(item.get("reason") or "").strip()
        if not reason:
            raise ImageReviewError(f"씬 {sid}의 reason이 비어 있다")

        pick = item.get("pick")
        if verdict == PICK:
            available = candidates[sid]
            if not available:
                raise ImageReviewError(
                    f"씬 {sid}는 후보가 없는데 pick이 왔다 (바꿔 낄 파일이 없다)"
                )
            slot = candidate_index(pick)
            if slot >= len(available):
                raise ImageReviewError(
                    f"씬 {sid}의 후보 q{slot}이 없다 (있는 것: q0~q{len(available) - 1})"
                )
            pick = f"q{slot}"
        elif pick not in (None, ""):
            raise ImageReviewError(
                f"씬 {sid}는 verdict가 {verdict}인데 pick={pick!r}이 왔다"
            )
        else:
            pick = None

        reviews[sid] = {"verdict": verdict, "pick": pick, "reason": reason}

    missing = sorted(expected - set(reviews))
    if missing:
        raise ImageReviewError(
            f"판정이 빠진 씬 {len(missing)}개: {', '.join(str(i) for i in missing)}"
        )
    return reviews
