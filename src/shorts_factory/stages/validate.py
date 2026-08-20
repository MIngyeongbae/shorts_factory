"""[2. validate] — 최종 게이트. **재생성하지 않는다** (ADR-0044).

specs/05-pipeline.md:
    [2. validate] → 06-script.json (구조·스키마·그라운딩 최종 게이트. 재생성하지 않는다 —
    실패는 보고·중단이고, 오류는 각 단계 직후 검증이 그 자리에서 잡는다)

## 왜 루프가 없는가 (ADR-0044)

옛 재생성 루프는 실패 1건에 `[1a]→[1s]→[1w]` 사슬 전체를 다시 돌렸다 — 다다미 편
실측으로 재생성 1회가 38분이었고, 1부 80분 중 60분이 같은 24줄을 여섯 번 쓴 시간이었다.

오류는 이제 **만든 자리에서 잡는다**: `[1s]`가 산출 직후 계약·그라운딩을 검증해 같은
세션에 재청하고, `[1w]`가 씬 단위로 검증해 실패 씬만 재청한다. 여기는 그 뒤에 서는
최종 게이트다 — 실패하면 보고하고 중단하며, 다시 돌릴지는 사람이 정한다. **여기서
걸리는 것은 직후 검증의 구멍이라는 뜻이고, 그것은 재생성이 아니라 검증기 수리 대상이다.**

## LLM을 부르지 않는다

재생성이 없으므로 세션도 없다. 순수 기계 검증이다 — 같은 입력이면 같은 판정이다.

## 입력은 06-script.json이다 — `[1b]`가 선발한 것

스펙 05의 순서는 `[1w] → [1c] → [1b] → [2]`이고 `[1b. score]`가 후보를 채점해
`06-script.json`으로 선발한다. 이 단계는 **선발된 것만** 검증한다.

## 이 단계가 하지 않는 것

- **채점·선발.** 후보가 여럿일 때 고르는 것은 `[1b. score]`다
- **go/no-go 판정.** `[2b. judge]`와 사람 게이트 몫이다 (ADR-0009)
- 검증기 자체는 여기 없다. 세 검증기를 묶어 부르는 `validate_candidate`는 `[1w]`와 공유한다
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Paths, write_text
from ..jsonio import dump_json
from ..runstate import RunState
from .research import find_run_for_slug
from .write import validate_candidate

log = logging.getLogger(__name__)

STAGE = "2-validate"

#: `[1b]`가 선발해 놓은 대본. 이 단계의 입력이다 (ADR-0040).
SELECTED = "06-script.json"
SCRIPT_FILE = "06-script.json"


class ValidateStageError(Exception):
    pass


@dataclass
class ValidateResult:
    topic: str
    slug: str
    run_id: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    script_path: Path | None = None
    scenes: dict[str, Any] | None = None
    skipped: bool = False

    @property
    def passed(self) -> bool:
        return self.script_path is not None

    @property
    def summary(self) -> str:
        tail = " (스킵)" if self.skipped else ""
        if self.passed:
            assert self.scenes is not None
            return (
                f"[2] {self.topic} — {len(self.scenes['scenes'])}줄 / "
                f"{self.scenes['total_duration']:.1f}초 → 최종 게이트 통과 → {SCRIPT_FILE}{tail}"
            )
        return (
            f"[2] {self.topic} — 최종 게이트 실패 {len(self.errors)}건 → 중단·보고. "
            f"재생성하지 않는다 (ADR-0044){tail}"
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
    selected_path = topic_dir / SELECTED
    if not selected_path.exists():
        raise ValidateStageError(
            f"선발된 대본이 없다: {selected_path}. [1b. score]를 먼저 실행하라."
        )

    state.mark_running(STAGE)

    scenes = _load_json(selected_path, SELECTED)
    errors, warnings = validate_candidate(scenes, factsheet)
    log.info("[%s] %s 검증 — 오류 %d건 / 경고 %d건",
             STAGE, SELECTED, len(errors), len(warnings))
    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)

    info: dict[str, Any] = {
        "validation_errors": errors,
        "validation_warnings": warnings,
    }

    if errors:
        # 보고·중단 (ADR-0044). 다시 돌릴지는 사람이 정한다.
        message = f"최종 게이트 실패 {len(errors)}건 — 재생성하지 않는다"
        state.mark_failed(STAGE, message, **info)
        log.warning("[%s] %s", STAGE, message)
        return ValidateResult(
            topic=topic, slug=slug, run_id=run_id,
            errors=errors, warnings=warnings, scenes=scenes,
        )

    write_text(script_path, dump_json(scenes))
    info["output"] = script_path.relative_to(paths.root).as_posix()
    state.mark_done(STAGE, **info)

    return ValidateResult(
        topic=topic, slug=slug, run_id=run_id, warnings=warnings,
        script_path=script_path, scenes=scenes,
    )
