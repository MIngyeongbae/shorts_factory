"""[3s. scenetable] — TTS 실측 줄 경계 위에서 씬 계약 `scenes.json`을 만든다 (ADR-0049 §5).

specs/05-pipeline.md:
    [3s. scenetable] → scenes.json  (씬 계약 — TTS 실측 줄 경계 위에서 씬 분할·연출 어휘
                       선택·인포씬 지정, ADR-0049 §5)

## 왜 TTS 뒤인가 (ADR-0049 §5)

씬의 경계는 추정이 아니라 **실측**이어야 한다. 대본을 먼저 통짜로 쓰고, `[3]`이 잰
줄 경계 위에서 연출을 다는 것이 목표물 문서의 순서다. 그래서 이 단계의 세션은 씬을
만들거나 합칠 수 없다 — `scenes.timed.ko.json`의 줄 목록이 씬의 전부다 (ADR-0013).
**한국어 실측 하나 위에서 1회**이고 연출은 언어와 무관하다 (ADR-0056 결정 5).

## 경계 (ADR-0017 — ADR-0049 개정)

- 입력: `runs/{run_id}/scenes.timed.ko.json` (씬 경계·text의 출처), `topics/{slug}/script.md`
  (**읽기 전용** — 서사 설계 맥락), `topics/{slug}/factcheck.md` (**인포씬 라벨 수치의
  근거 확인 전용 예외** — specs/05 경계 절. 없으면 인포씬만 빠지고 단계는 돈다, D-3)
- 출력: `runs/{run_id}/scenes.json` 하나. `topics/` 아래에 아무것도 쓰지 않는다
- 산출물 파일은 오케스트레이터가 쓴다. 세션에는 도구를 주지 않는다 — 입력이 전부
  프롬프트에 주입된다 (ADR-0011)

## 검증은 산출 직후, 실패는 보고·중단 (ADR-0044)

스키마(`scene.schema.json`) + 교차 규칙(`cast`↔`characters`, `visual_goal` 겹침,
라벨 숫자 에코 — 전부 `schemas/scenes.validate_scenes`)을 통과한 문서만 쓴다.
실패하면 `scenes.json`을 **쓰지 않고** 오류를 보고한다 — 깨진 계약 파일이 남아 있으면
하류 단계가 파일이 있다는 이유로 진행해 버린다 (`[3]`이 같은 이유로 stale 파일을
지운다). 재생성 루프는 없다 — 다시 돌릴지는 사람이 정한다.

## `est_start`/`est_end`에는 실측값이 들어간다

`scene.schema.json`이 두 필드를 요구한다. 이름과 달리 이 파이프라인의
`[3s]`는 TTS 뒤에 돌므로 `scenes.timed.ko.json`의 `start`/`end`를 그대로 옮긴다.
**시각의 정본은 여전히 `scenes.timed.{lang}.json`이다** (ADR-0020) — `[7]`·`[9]`는 그
파일을 읽고, `scenes.json`의 시각은 씬 계약 소비자(`[4]`·`[5]`)가 계약 검증을
통과하기 위한 사본이다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..gate import strip_gate_block
from ..config import Paths, write_text
from ..judgment import JudgmentError, read_video_line
from ..jsonio import dump_json
from ..llm.base import LLMClient
from ..runstate import RunNotFound, RunState, find_run_for_slug
from ..schemas import sceneplan as plan_schema
from ..schemas import vocab
from ..schemas.scenes import validate_scenes
from ..schemas.timed_scenes import PRIMARY_LANGUAGE, timed_scenes_path
from .session import ScriptSessionError, ask_json, format_vocab, load_prompt

log = logging.getLogger(__name__)

STAGE = "3s-scenetable"
PROMPT_FILE = "03-scenetable.md"

#: 씬 경계의 출처 — ko 실측 하나다 (ADR-0056 결정 5).
TIMED_SCENES_FILE = timed_scenes_path(Path("."), PRIMARY_LANGUAGE).name
SCENES_FILE = "scenes.json"
SCRIPT_FILE = "script.md"
FACTCHECK_FILE = "factcheck.md"

#: 세션에 주는 도구. 입력은 전부 프롬프트에 주입되므로 읽을 것이 없다 (ADR-0011).
TOOLS: tuple[str, ...] = ()

#: 세션 상한(초). 생성만 하는 단계라 웹 조사 단계보다 짧다.
TIMEOUT = 900

#: 병합된 씬 객체의 표기 순서. 계약이 아니라 사람이 읽을 때의 배치다 —
#: 스펙 02의 씬 예시와 같은 순서로 쓴다. 목록에 없는 필드는 뒤에 그대로 붙는다 (D-2).
_FIELD_ORDER: tuple[str, ...] = (
    "scene_id", "beat", "text", "est_start", "est_end",
    "visual_goal", "subject", "subject_anchor", "subject_scale",
    "framing", "transition", "camera", "cast", "info", "notes",
)


class ScenetableStageError(Exception):
    """`[3s]`가 결과를 낼 수 없는 경우."""


@dataclass
class ScenetableResult:
    topic: str
    slug: str
    run_id: str
    path: Path | None = None
    scenes: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False

    @property
    def passed(self) -> bool:
        return self.path is not None and not self.errors

    @property
    def info_scenes(self) -> int:
        return sum(1 for s in (self.scenes or {}).get("scenes", []) if s.get("info"))

    @property
    def character_count(self) -> int:
        return len((self.scenes or {}).get("characters") or [])

    @property
    def summary(self) -> str:
        tail = " (스킵)" if self.skipped else ""
        if not self.passed:
            return (
                f"[3s] {self.topic} — 계약 위반 {len(self.errors)}건. "
                f"{SCENES_FILE}을 쓰지 않았다 — 다시 돌릴지는 사람이 정한다 (ADR-0044){tail}"
            )
        total = len(self.scenes.get("scenes", []))
        cast_note = f" / 인물 {self.character_count}명" if self.character_count else ""
        return (
            f"[3s] {self.topic} — {total}씬 (인포 {self.info_scenes}{cast_note}) "
            f"→ {SCENES_FILE}{tail}"
        )


def resolve_run_id(paths: Paths, slug: str) -> str:
    """슬러그 → 가장 최근 run_id. 새 편은 `topic.json`이 계보의 출발점이다."""
    try:
        run_id, _contract = find_run_for_slug(paths, slug)
    except RunNotFound as exc:
        raise ScenetableStageError(str(exc)) from exc
    return run_id


def _load_json(path: Path, what: str) -> dict[str, Any]:
    if not path.exists():
        raise ScenetableStageError(f"{what}이(가) 없다: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScenetableStageError(f"{what}을(를) 읽을 수 없다: {path} — {exc}") from exc


def timed_input_errors(timed: dict[str, Any]) -> list[str]:
    """`scenes.timed.ko.json`에서 이 단계가 읽는 필드만 검사한다 (D-2).

    옛 경로의 실측 파일에는 씬 계약 필드가 더 있고, 새 경로(`script.md` 입력)의
    실측 파일에는 줄과 시각뿐이다. 어느 쪽이든 여기가 쓰는 것은
    `scene_id`·`text`·`start`·`end` 넷이라 그 넷만 본다.
    """
    errors: list[str] = []
    for key in ("run_id", "topic"):
        if not timed.get(key):
            errors.append(f"{key}가 없다")

    scenes = timed.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        errors.append("scenes가 비어 있다")
        return errors

    prev_end: float | None = None
    for index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict):
            errors.append(f"scenes[{index}]: 객체가 아니다")
            continue
        if scene.get("scene_id") != index:
            errors.append(f"scenes[{index}]: scene_id가 연번이 아니다 ({scene.get('scene_id')})")
        if not str(scene.get("text") or "").strip():
            errors.append(f"scenes/{index}: text가 비어 있다")
        try:
            start = float(scene["start"])
            end = float(scene["end"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"scenes/{index}: start/end가 없다")
            continue
        if start >= end:
            errors.append(f"scenes/{index}: start({start}) >= end({end})")
        elif prev_end is not None and start < prev_end:
            errors.append(f"scenes/{index}: start({start})가 앞 씬의 end({prev_end})보다 이르다")
        prev_end = end
    return errors


def format_timed_scenes(timed: dict[str, Any]) -> str:
    """실측 씬 목록을 세션이 읽을 표로. 줄 텍스트와 실측 길이만 준다.

    길이를 주는 이유: 긴 씬(4.6초 초과)은 그림 하나가 오래 걸려 있어 도해·인포씬
    후보이고, 그 판단은 실측 길이를 요구한다 (ADR-0043이 `[3]`을 앞세운 이유와 같다).
    """
    lines = ["| 씬 | 길이(초) | 대본 줄 |", "|---|---|---|"]
    for scene in timed.get("scenes", []):
        duration = float(scene["end"]) - float(scene["start"])
        lines.append(f"| {scene['scene_id']} | {duration:.1f} | {scene['text']} |")
    return "\n".join(lines)


#: 라인이 2샷을 만들지 않을 때 병합에서 떼는 필드 (ADR-0058의 선택 필드, ADR-0059 결정 5).
SHOT2_FIELD = "shot2"


def merge_scenes(
    timed: dict[str, Any], payload: dict[str, Any], *, allow_shot2: bool = True,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """세션의 연출표 + 실측 줄 경계 → `scenes.json` 문서. `(document, errors, warnings)`.

    - **씬 경계는 실측이 정한다.** 세션이 빠뜨린 씬은 오류다 — 씬 계약의 필수 필드를
      채울 곳이 없다. 실측에 없는 씬은 지어낸 것이므로 버리고 경고한다
    - `text`·`est_start`/`est_end`는 실측 파일에서 온다. 세션 출력에 그 필드가 있어도
      스키마(`additionalProperties: false`)가 먼저 걸렀다
    - 어느 필드가 건너가는지는 `sceneplan.carried_fields()`가 계산한다 (ADR-0034)
    - `allow_shot2=False`면 `shot2`를 떼고 경고한다 — 영상 라인이 2샷을 만들지 않는다
      (`vocab.video_line_meta(line)["shot2"]`, ADR-0059 결정 5). 씬은 1샷으로 돈다
    """
    errors: list[str] = []
    warnings: list[str] = []
    dropped_shot2: list[int] = []

    reported: dict[int, dict[str, Any]] = {}
    for entry in payload.get("scenes", []) or []:
        if isinstance(entry, dict) and isinstance(entry.get("scene_id"), int):
            reported[entry["scene_id"]] = entry

    known = [int(s["scene_id"]) for s in timed.get("scenes", [])]
    missing = sorted(set(known) - set(reported))
    if missing:
        errors.append(
            f"세션이 씬 {', '.join(str(s) for s in missing)}을(를) 빠뜨렸다 — "
            "씬 경계는 [3]의 실측이고 세션은 씬을 만들거나 합칠 수 없다"
        )
    stray = sorted(set(reported) - set(known))
    if stray:
        warnings.append(
            f"실측에 없는 씬을 보고해 버렸다: {', '.join(str(s) for s in stray)}"
        )

    fields = plan_schema.carried_fields()
    scenes: list[dict[str, Any]] = []
    for timed_scene in timed.get("scenes", []):
        sid = int(timed_scene["scene_id"])
        entry = reported.get(sid) or {}
        merged: dict[str, Any] = {
            key: entry[key] for key in fields if key in entry
        }
        if not allow_shot2 and merged.pop(SHOT2_FIELD, None) is not None:
            dropped_shot2.append(sid)
        merged["scene_id"] = sid
        merged["text"] = timed_scene["text"]
        merged["est_start"] = timed_scene["start"]
        merged["est_end"] = timed_scene["end"]
        ordered = {key: merged[key] for key in _FIELD_ORDER if key in merged}
        ordered.update({key: value for key, value in merged.items() if key not in ordered})
        scenes.append(ordered)

    if dropped_shot2:
        warnings.append(
            f"영상 라인이 2샷을 만들지 않아 씬 {', '.join(str(s) for s in dropped_shot2)}의 "
            f"{SHOT2_FIELD}를 뗐다 — 1샷으로 돈다 (ADR-0059 결정 5)"
        )

    document: dict[str, Any] = {
        "run_id": timed["run_id"],
        "topic": timed["topic"],
        "total_duration": timed.get(
            "total_duration", scenes[-1]["est_end"] if scenes else 0.0
        ),
    }
    if payload.get("characters"):
        document["characters"] = payload["characters"]
    document["scenes"] = scenes
    return document, errors, warnings


def build_prompt(
    *,
    topic: str,
    script_text: str,
    factcheck_text: str | None,
    timed: dict[str, Any],
) -> str:
    """프롬프트를 조립한다. 값은 계약 파일에서 온다 — 손으로 적지 않는다 (ADR-0034)."""
    checks = vocab.checks()
    factcheck = (
        factcheck_text
        if factcheck_text
        else (
            "(factcheck.md가 없다 — 검증된 수치가 없으므로 **인포씬(`info`)을 하나도 "
            "지정하지 마라.** 화면에 나가는 숫자는 검증분만이다, ADR-0007)"
        )
    )
    return load_prompt(PROMPT_FILE).substitute(
        topic=topic,
        script=script_text.strip(),
        factcheck=factcheck.strip(),
        scenes=format_timed_scenes(timed),
        vocab=format_vocab(),
        overlap_limit=f"{checks['visual_goal_overlap_limit']:.0%}",
    )


def run_scenetable_stage(
    slug: str,
    *,
    llm: LLMClient,
    paths: Paths | None = None,
    run_id: str | None = None,
    force: bool = False,
    timeout: int = TIMEOUT,
) -> ScenetableResult:
    paths = paths or Paths.from_env()
    run_id = run_id or resolve_run_id(paths, slug)
    run_dir = paths.run_dir(run_id)

    timed_path = timed_scenes_path(run_dir, PRIMARY_LANGUAGE)
    timed = _load_json(timed_path, f"실측 씬({TIMED_SCENES_FILE})")
    input_errors = timed_input_errors(timed)
    if input_errors:
        raise ScenetableStageError(
            f"{timed_path}이(가) 이 단계의 입력 계약을 어겼다 ({len(input_errors)}건): "
            + "; ".join(input_errors)
        )

    topic = str(timed["topic"])
    scenes_path = run_dir / SCENES_FILE
    state = RunState.load_or_create(run_dir, run_id, topic=topic, slug=slug)

    if state.is_done(STAGE) and not force and scenes_path.exists():
        log.info("[%s] 이미 완료된 단계라 스킵한다 (run_id=%s)", STAGE, run_id)
        return ScenetableResult(
            topic=topic, slug=slug, run_id=run_id, path=scenes_path,
            scenes=_load_json(scenes_path, SCENES_FILE), skipped=True,
        )

    script_path = paths.topic_dir(slug) / SCRIPT_FILE
    if not script_path.exists():
        raise ScenetableStageError(
            f"대본이 없다: {script_path}. [3s]는 새 편(script.md) 전용이다 — "
            "[3. tts]를 먼저 실행해야 한다."
        )
    script_text = strip_gate_block(script_path.read_text(encoding="utf-8"))

    # 인포씬 라벨 수치의 근거 확인 **전용** 예외다 (specs/05 경계 절). 없으면 인포씬
    # 지정만 빠지고 단계는 돈다 (D-3).
    factcheck_path = paths.topic_dir(slug) / FACTCHECK_FILE
    factcheck_text = (
        factcheck_path.read_text(encoding="utf-8") if factcheck_path.exists() else None
    )

    state.mark_running(STAGE)

    # 지난 실행이 남긴 계약 파일을 먼저 치운다 — 이번 실행이 실패하면 옛 연출표가
    # 남아 하류가 그대로 진행해 버린다 ([3]의 stale 처리와 같은 이유).
    scenes_path.unlink(missing_ok=True)

    prompt = build_prompt(
        topic=topic, script_text=script_text,
        factcheck_text=factcheck_text, timed=timed,
    )
    log.info(
        "[%s] 연출표 세션 시작 — %d씬 (factcheck: %s)",
        STAGE, len(timed["scenes"]), "있음" if factcheck_text else "없음",
    )

    try:
        payload, meta = ask_json(llm, prompt, label=STAGE, tools=TOOLS, timeout=timeout)
    except ScriptSessionError as exc:
        message = f"{exc} 원본은 {run_dir / 'logs'}에 있다."
        state.mark_failed(STAGE, message)
        raise ScenetableStageError(message) from exc

    warnings: list[str] = []
    if factcheck_text is None:
        warnings.append(
            f"{FACTCHECK_FILE}이 없어 인포씬 없이 돈다 — 검증된 수치만 화면에 나간다 (ADR-0007)"
        )

    # 1) 연출표 자체의 계약 (sceneplan.schema.json — 어휘 enum·필드 타입)
    plan_errors, plan_warnings = plan_schema.validate_sceneplan(payload)
    warnings.extend(plan_warnings)

    errors: list[str] = []
    document: dict[str, Any] | None = None
    if plan_errors:
        errors = [f"연출표: {e}" for e in plan_errors]
    else:
        # 2) 실측 줄과의 병합 + 병합본의 씬 계약 (scene.schema.json + 교차 규칙)
        try:
            video_line = read_video_line(paths, slug)
        except JudgmentError as exc:
            raise ScenetableStageError(str(exc)) from exc
        allow_shot2 = bool(vocab.video_line_meta(video_line).get(SHOT2_FIELD, True))
        document, merge_errors, merge_warnings = merge_scenes(timed, payload, allow_shot2=allow_shot2)
        warnings.extend(merge_warnings)
        errors.extend(merge_errors)
        if not errors:
            contract_errors, contract_warnings = validate_scenes(document)
            errors.extend(contract_errors)
            warnings.extend(contract_warnings)

    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)

    info: dict[str, Any] = {
        "scene_count": len(timed["scenes"]),
        "validation_errors": errors,
        "validation_warnings": warnings,
        "direction_summary": plan_schema.direction_summary(payload),
        **meta,
    }

    if errors:
        # 보고·중단 (ADR-0044). scenes.json은 쓰지 않는다 — 깨진 계약 파일이
        # 남으면 하류가 그대로 진행한다. 세션 출력 원본은 logs/에 있다.
        state.mark_failed(STAGE, f"계약 위반 {len(errors)}건", **info)
        return ScenetableResult(
            topic=topic, slug=slug, run_id=run_id, scenes=document,
            errors=errors, warnings=warnings,
        )

    write_text(scenes_path, dump_json(document))
    info["output"] = scenes_path.relative_to(paths.root).as_posix()
    state.mark_done(STAGE, **info)
    return ScenetableResult(
        topic=topic, slug=slug, run_id=run_id, path=scenes_path,
        scenes=document, warnings=warnings,
    )
