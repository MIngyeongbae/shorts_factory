"""[2. validate] — 검증 실패 사유를 피드백해 대본을 재생성한다.

specs/05-pipeline.md:
    [2. validate] → 06-script.json (구조·스키마·그라운딩 검증, 최대 3회 재생성)

    "스펙 01의 검증 항목(분량·수미상관) + 씬 계약 스키마 검증 + 그라운딩 검증(대본 숫자 전수 추출 →
     팩트시트 대조, ADR-0007). 실패 시 실패 사유를 프롬프트에 피드백하여 재생성
     (최대 3회, 초과 시 중단·리포트)."

## 왜 '수정'이 아니라 '재생성'인가

스펙은 이 루프를 **재생성**이라 쓰고, 피드백할 것으로 **실패 사유**만 든다. 직전 대본을
프롬프트에 되돌려주라는 말은 없다. 씬 단위 수정 지시(`fix_directives`)로 고쳐 쓰는
revise 루프는 스펙 07의 `[2b. judge]` 소관이고 상한도 2회로 다르다. 두 루프는 입력도
상한도 다르므로 여기서 합치지 않는다.

## 재생성 범위가 실패 종류를 따라간다 (ADR-0029)

**분량이 안 맞는다고 그림 계획까지 버리지 않는다.** 어디로 되돌아갈지는 실패 종류가
정한다.

| 실패 | 재진입 | 살아남는 것 |
|---|---|---|
| 분량·문체·시그니처 | `[1w]`만 | 훅 각도, 씬 분할, **그림·연출 계획 전부** |
| 씬 수·`visual_goal` 겹침·연출 | `[1s]`부터 | 훅 각도와 단별 배분 |
| 그라운딩 위반, 수미상관 실패 | `[1a]`부터 | — |

**모르는 오류는 `[1a]`로 간다.** 좁게 잡아 틀리면 같은 실패가 반복되며 상한만 태우고,
넓게 잡아 틀리면 살릴 수 있었던 계획을 버릴 뿐이다 — 후자가 싸다.

## 입력이 05-candidates/01.json인 이유 (스펙과의 임시 어긋남)

스펙 05의 파이프라인 순서는 `[1] → [1c] → [1b] → [2]`이고, `06-script.json`의 생산자로
`[1b. score]`와 `[2. validate]` 둘을 모두 적어 뒀다. 원래대로면 `[1b]`가 후보 여럿을
채점해 `06-script.json`으로 선발하고 `[2]`는 그것을 검증한다.

**`[1b]`가 아직 없고 후보도 1개라서**(specs/05: "후보 수는 `[1b. score]` 도입 전까지
1개다") 지금은 `[2]`가 `05-candidates/01.json`을 직접 읽는다. `[1b]`가 들어오면 이
입력은 `06-script.json`으로 바뀌어야 한다 — 그때 이 주석을 지운다.

## 이 단계가 하지 않는 것

- **채점·선발.** 후보가 여럿일 때 고르는 것은 `[1b. score]`다 (위 참고)
- **go/no-go 판정.** `[2b. judge]`와 사람 게이트 몫이다 (ADR-0009)
- 검증기 자체는 여기 없다. 세 검증기를 묶어 부르는 `validate_candidate`는 `[1w]`와 공유한다
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Paths, write_text
from ..jsonio import dump_json
from ..llm.base import LLMClient
from ..runstate import RunState
from ..schemas.outline import validate_outline
from ..schemas.sceneplan import validate_sceneplan
from .outline import OUTLINE_FILE, generate_outline
from .research import find_run_for_slug
from .sceneplan import PLAN_FILE, generate_sceneplan
from .session import ScriptSessionError
from .write import generate_candidate, validate_candidate

log = logging.getLogger(__name__)

STAGE = "2-validate"

#: specs/05 "최대 3회". `[1w]`가 만든 후보가 실패했을 때 다시 만들 수 있는 횟수다.
#: 소진하면 중단하고 리포트한다 — 무한 루프와 한도 소모를 동시에 막는 상한이다.
MAX_REGENERATIONS = 3

#: `[1w]`가 쓰는 첫 후보. 이 단계의 입력이다.
FIRST_CANDIDATE = "01.json"

CANDIDATES_DIR = "05-candidates"
SCRIPT_FILE = "06-script.json"

#: 재진입 지점. 앞선 것일수록 다시 도는 범위가 크다 (ADR-0029).
OUTLINE, SCENEPLAN, WRITE = "1a", "1s", "1w"
_DEPTH = {OUTLINE: 0, SCENEPLAN: 1, WRITE: 2}

#: 오류 문구 → 재진입 지점. 문구는 검증기가 낸 그대로이고, 앞에 붙은 대괄호는
#: `validate_candidate`가 붙인 검증기 이름이다.
_REENTRY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # 사실 선택이 틀렸다 — 각도와 근거부터 다시 고른다
    (re.compile(r"^\[그라운딩\]"), OUTLINE),
    (re.compile(r"수미상관"), OUTLINE),
    # 씬 분할·그림·연출 — 문장은 못 살리지만 훅 각도와 단 배분은 산다
    (re.compile(r"자막 \d+줄"), SCENEPLAN),
    (re.compile(r"visual_goal"), SCENEPLAN),
    (re.compile(r"영상 모션"), SCENEPLAN),
    (re.compile(r"scene_id"), SCENEPLAN),
    # 문장 문제 — 여기만 다시 쓴다. 그림 계획은 전부 살아남는다
    (re.compile(r"총 \d+자"), WRITE),
    (re.compile(r"줄당 최대"), WRITE),
    (re.compile(r"시그니처"), WRITE),
    (re.compile(r"\b(text|est_start|est_end)\b"), WRITE),
    # 위에 안 걸린 스키마 위반은 계획이 만든 필드 쪽이다
    (re.compile(r"^\[스키마\]"), SCENEPLAN),
)


def reentry_for(errors: list[str]) -> str:
    """가장 앞선 재진입 지점. 하나라도 `[1a]`짜리면 전부 다시 돈다.

    **모르는 오류는 `[1a]`다.** 좁게 잡아 틀리면 같은 실패가 반복되며 상한만 태우고,
    넓게 잡아 틀리면 살릴 수 있었던 계획을 버릴 뿐이다.
    """
    point = WRITE
    for error in errors:
        matched = next(
            (stage for pattern, stage in _REENTRY_PATTERNS if pattern.search(error)),
            OUTLINE,
        )
        if _DEPTH[matched] < _DEPTH[point]:
            point = matched
    return point


class ValidateStageError(Exception):
    pass


@dataclass
class Attempt:
    """후보 하나의 검증 결과. state.json 리포트에 그대로 실린다."""

    candidate: str
    regenerated: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: 이 후보를 만들려고 되돌아간 지점 (`1a`/`1s`/`1w`). 첫 후보에는 없다.
    reentry: str | None = None
    #: 세션이 씬을 못 내놓은 경우. 검증 실패와 구분한다 (검증기가 아니라 생성이 깨진 것)
    generation_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "candidate": self.candidate,
            "regenerated": self.regenerated,
            "errors": self.errors,
            "warnings": self.warnings,
        }
        if self.reentry:
            record["reentry"] = self.reentry
        if self.generation_error:
            record["generation_error"] = self.generation_error
        return record


@dataclass
class ValidateResult:
    topic: str
    slug: str
    run_id: str
    attempts: list[Attempt] = field(default_factory=list)
    script_path: Path | None = None
    scenes: dict[str, Any] | None = None
    skipped: bool = False

    @property
    def passed(self) -> bool:
        return self.script_path is not None

    @property
    def regenerations(self) -> int:
        return sum(1 for attempt in self.attempts if attempt.regenerated)

    @property
    def errors(self) -> list[str]:
        """마지막 시도에 남은 검증 오류. 통과했으면 빈 목록이다."""
        return self.attempts[-1].errors if self.attempts else []

    @property
    def warnings(self) -> list[str]:
        return self.attempts[-1].warnings if self.attempts else []

    @property
    def summary(self) -> str:
        tail = " (스킵)" if self.skipped else ""
        regen = f"재생성 {self.regenerations}회"
        if self.passed:
            assert self.scenes is not None
            return (
                f"[2] {self.topic} — {regen} / {len(self.scenes['scenes'])}줄 / "
                f"{self.scenes['total_duration']:.1f}초 → 검증 통과 → {SCRIPT_FILE}{tail}"
            )
        return (
            f"[2] {self.topic} — {regen}(상한 {MAX_REGENERATIONS}) 소진 → "
            f"중단. 검증 실패 {len(self.errors)}건{tail}"
        )


def build_feedback(errors: list[str]) -> str:
    """검증 실패 사유를 프롬프트 꼬리(`${feedback}`)에 붙일 지시문으로 만든다.

    사유만 넣는다 — 직전 대본은 되돌려주지 않는다 (모듈 독스트링 참고). 오류 문구는
    검증기가 낸 그대로 쓴다. "594자 (범위 545~575자)"처럼 이미 무엇을 어디로 옮겨야
    하는지가 들어 있어, 여기서 다시 풀어 쓰면 스펙에 없는 지시를 지어내게 된다.
    """
    lines = "\n".join(f"{i}. {error}" for i, error in enumerate(errors, 1))
    return (
        "\n\n# 재생성 지시\n\n"
        "직전 대본이 아래 검증을 통과하지 못했다. 이번 대본은 전부 만족시켜라.\n\n"
        f"{lines}\n"
    )


def _reject_if_invalid(what: str, errors: list[str]) -> None:
    """되돌아가서 다시 만든 것이 제 계약을 못 지키면 그 시도를 버린다.

    깨진 구성안 위에 씬 계획을 얹으면 실패가 한 단계 아래에서 다른 모양으로 나오고,
    재진입 판단이 그 다음부터 틀린 곳을 가리킨다.
    """
    if errors:
        raise ScriptSessionError(
            f"다시 만든 {what}이(가) 계약 위반이다 ({len(errors)}건): {errors[0]}"
        )


def _load_json(path: Path, what: str) -> dict[str, Any]:
    if not path.exists():
        raise ValidateStageError(f"{what}이(가) 없다: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidateStageError(f"{what}을(를) 읽을 수 없다: {path} — {exc}") from exc


def run_validate_stage(
    slug: str,
    *,
    llm: LLMClient,
    paths: Paths | None = None,
    run_id: str | None = None,
    force: bool = False,
) -> ValidateResult:
    paths = paths or Paths.from_env()

    if run_id:
        contract = _load_json(paths.run_dir(run_id) / "topic.json", "topic.json")
    else:
        run_id, contract = find_run_for_slug(paths, slug)

    topic = contract["topic"]
    topic_dir = paths.topic_dir(slug)
    run_dir = paths.run_dir(run_id)
    state = RunState.load_or_create(run_dir, run_id, topic=topic, slug=slug)

    script_path = topic_dir / SCRIPT_FILE
    if state.is_done(STAGE) and not force and script_path.exists():
        log.info("[%s] 이미 완료된 단계라 스킵한다 (run_id=%s)", STAGE, run_id)
        return ValidateResult(
            topic=topic, slug=slug, run_id=run_id, skipped=True,
            script_path=script_path,
            scenes=_load_json(script_path, SCRIPT_FILE),
        )

    factsheet = _load_json(topic_dir / "04-factsheet.json", "팩트시트")
    candidates_dir = topic_dir / CANDIDATES_DIR
    first_path = candidates_dir / FIRST_CANDIDATE
    if not first_path.exists():
        raise ValidateStageError(
            f"대본 후보가 없다: {first_path}. [1w. write]를 먼저 실행하라."
        )

    state.mark_running(STAGE)

    scenes = _load_json(first_path, FIRST_CANDIDATE)
    errors, warnings = validate_candidate(scenes, factsheet)
    attempts = [Attempt(FIRST_CANDIDATE, regenerated=False,
                        errors=errors, warnings=warnings)]
    log.info("[%s] %s 검증 — 오류 %d건 / 경고 %d건",
             STAGE, FIRST_CANDIDATE, len(errors), len(warnings))

    # 구성안·씬 계획은 **되돌아갈 때만** 읽는다. 첫 후보가 통과하면 없어도 된다 —
    # [1a]·[1s] 이전에 만든 대본이 아직 리포지토리에 있다 (ADR-0029: 백필하지 않는다).
    plans: dict[str, dict[str, Any]] = {}

    def load_plans() -> tuple[dict[str, Any], dict[str, Any]]:
        if not plans:
            plans["outline"] = _load_json(topic_dir / OUTLINE_FILE, OUTLINE_FILE)
            plans["plan"] = _load_json(topic_dir / PLAN_FILE, PLAN_FILE)
        return plans["outline"], plans["plan"]

    regenerations = 0
    while errors and regenerations < MAX_REGENERATIONS:
        regenerations += 1
        point = reentry_for(errors)
        name = f"{regenerations + 1:02d}.json"
        label = f"{STAGE}.regen{regenerations}"
        feedback = build_feedback(errors)
        log.info("[%s] 재생성 %d/%d — 실패 사유 %d건 → [%s]부터 다시 돈다",
                 STAGE, regenerations, MAX_REGENERATIONS, len(errors), point)

        try:
            outline, plan = load_plans()

            if point == OUTLINE:
                outline, _meta = generate_outline(
                    llm=llm, topic=topic, factsheet=factsheet,
                    feedback=feedback, label=f"{label}.1a",
                )
                _reject_if_invalid(OUTLINE_FILE, validate_outline(outline)[0])
                write_text(topic_dir / OUTLINE_FILE, dump_json(outline))
                plans["outline"] = outline

            if point in (OUTLINE, SCENEPLAN):
                plan, _meta = generate_sceneplan(
                    llm=llm, topic=topic, outline=outline, factsheet=factsheet,
                    feedback=feedback, label=f"{label}.1s",
                )
                _reject_if_invalid(PLAN_FILE, validate_sceneplan(plan, outline)[0])
                write_text(topic_dir / PLAN_FILE, dump_json(plan))
                plans["plan"] = plan

            candidate, _meta = generate_candidate(
                llm=llm, topic=topic, run_id=run_id, outline=outline, plan=plan,
                feedback=feedback, label=f"{label}.1w",
            )
        except ScriptSessionError as exc:
            # 생성이 깨진 것은 검증 실패와 다르다. 남은 재생성 횟수를 버리지 않고
            # 같은 사유로 한 번 더 간다 — errors를 갱신하지 않으므로 피드백은 그대로다.
            log.warning("[%s] 재생성 %d 실패: %s", STAGE, regenerations, exc)
            attempts.append(Attempt(name, regenerated=True, errors=errors,
                                    warnings=warnings, reentry=point,
                                    generation_error=str(exc)))
            continue

        write_text(candidates_dir / name, dump_json(candidate))
        scenes = candidate
        errors, warnings = validate_candidate(scenes, factsheet)
        attempts.append(Attempt(name, regenerated=True, errors=errors,
                                warnings=warnings, reentry=point))
        log.info("[%s] %s 검증 — 오류 %d건 / 경고 %d건",
                 STAGE, name, len(errors), len(warnings))

    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)

    info: dict[str, Any] = {
        "regenerations": regenerations,
        "max_regenerations": MAX_REGENERATIONS,
        "report": [attempt.as_dict() for attempt in attempts],
    }

    if errors:
        # specs/05 "초과 시 중단·리포트". 후보 파일은 전부 남기고 사유를 state에 적는다.
        message = (
            f"재생성 {regenerations}회를 소진하고도 검증 실패 {len(errors)}건이 남았다"
        )
        state.mark_failed(STAGE, message, **info)
        log.warning("[%s] %s", STAGE, message)
        return ValidateResult(
            topic=topic, slug=slug, run_id=run_id, attempts=attempts, scenes=scenes,
        )

    write_text(script_path, dump_json(scenes))
    info["output"] = script_path.relative_to(paths.root).as_posix()
    info["source_candidate"] = attempts[-1].candidate
    state.mark_done(STAGE, **info)

    return ValidateResult(
        topic=topic, slug=slug, run_id=run_id, attempts=attempts,
        script_path=script_path, scenes=scenes,
    )
