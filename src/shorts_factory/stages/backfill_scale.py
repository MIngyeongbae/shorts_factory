"""[1x. backfill-scale] — 확정된 대본에 `subject_scale`만 채운다. ADR-0018.

ADR-0018이 씬 계약에 `subject_scale`을 required로 추가했다. 그 전에 만들어져 이미
사람이 승인한 `06-script.json`에는 이 필드가 없다. `[1. script]`를 다시 돌리면 대본
자체가 달라지므로(피사 대본은 재생성 2회 만에 수렴한 것이다) **대본을 그대로 두고
필드만 채우는** 일회성 경로가 따로 필요하다.

## 이 단계가 절대 하지 않는 것

`text`·`beat`·`subject`·`est_start`·`est_end`·`emphasis`·`camera`·`motion`·`notes`를
건드리지 않는다. 쓰기 전에 실제로 비교해서 확인하고, 하나라도 달라지면 파일을 쓰지 않고
실패한다 — 승인된 대본을 조용히 바꾸는 것이 이 단계가 낼 수 있는 최악의 결과다.

## 경계 (ADR-0017)

`topics/{slug}/06-script.json`은 **2부에게** 읽기 전용이다. 이 단계는 1부 소관이므로
그 파일을 쓴다. 2부는 이 단계를 부르지 않는다.

## 새 토픽에는 쓰지 않는다

`[1]`이 `subject_scale`을 직접 출력하므로(ADR-0018) 이 단계는 필드가 없는 옛 대본에만
해당한다. 이미 전 씬에 값이 있으면 세션을 부르지 않고 그대로 끝낸다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any

from ..config import Paths, write_text
from ..jsonio import JSONExtractionError, dump_json, extract_json_object
from ..llm.base import LLMClient
from ..schemas.scenes import validate_scenes
from ..schemas.visual_rules import SUBJECT_SCALES

log = logging.getLogger(__name__)

STAGE = "1x-backfill-scale"
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
PROMPT = "06-subject-scale.md"

SCRIPT_FILE = "06-script.json"

#: 분류만 하는 세션이라 대본 생성보다 짧다.
TIMEOUT = 300

#: 팩트시트도 웹도 필요 없다 — 프롬프트에 피사체가 전부 들어간다 (ADR-0011).
TOOLS: tuple[str, ...] = ()

#: 이 단계가 채우는 필드. 이것 말고는 전부 그대로여야 한다.
ADDED_FIELD = "subject_scale"


class BackfillStageError(Exception):
    pass


@dataclass
class BackfillResult:
    topic: str
    slug: str
    script_path: Path
    scale_counts: dict[str, int] = field(default_factory=dict)
    scene_count: int = 0
    skipped: bool = False

    @property
    def summary(self) -> str:
        if self.skipped:
            return f"[1x] {self.topic} — 전 씬에 {ADDED_FIELD}가 이미 있다 (스킵)"
        scales = " / ".join(
            f"{scale} {count}" for scale, count in sorted(self.scale_counts.items())
        )
        return (
            f"[1x] {self.topic} — {self.scene_count}씬에 {ADDED_FIELD} 채움 "
            f"({scales}) → {SCRIPT_FILE}"
        )


def _load_prompt() -> Template:
    path = PROMPTS_DIR / PROMPT
    if not path.exists():
        raise BackfillStageError(f"프롬프트 파일이 없다: {path}")
    return Template(path.read_text(encoding="utf-8"))


def render_scenes(scenes: list[dict[str, Any]]) -> str:
    """세션에 보여줄 판정 대상. 피사체만 준다 — 대본 본문은 분류에 필요 없다."""
    return "\n".join(
        f"- scene_id {scene['scene_id']}: {scene['subject']}" for scene in scenes
    )


def parse_scales(payload: dict[str, Any], scenes: list[dict[str, Any]]) -> dict[int, str]:
    """세션 출력 → `{scene_id: subject_scale}`.

    빠진 씬·모르는 씬·허용 목록 밖의 값은 전부 실패로 돌린다. 일부만 채워 놓으면
    나머지가 어떤 값이었는지 나중에 구분할 수 없다.
    """
    raw = payload.get("scales")
    if not isinstance(raw, list):
        raise BackfillStageError("세션 출력에 scales 배열이 없다")

    expected = {scene["scene_id"] for scene in scenes}
    scales: dict[int, str] = {}
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise BackfillStageError(f"{index}번째 항목이 객체가 아니다: {item!r}")
        sid = item.get("scene_id")
        scale = item.get(ADDED_FIELD)
        if sid not in expected:
            raise BackfillStageError(f"{index}번째 항목의 scene_id가 대본에 없다: {sid!r}")
        if scale not in SUBJECT_SCALES:
            raise BackfillStageError(
                f"씬 {sid}의 {ADDED_FIELD}가 {'/'.join(SUBJECT_SCALES)} 중에 없다: {scale!r}"
            )
        if sid in scales:
            raise BackfillStageError(f"씬 {sid}가 두 번 나왔다")
        scales[sid] = scale

    missing = sorted(expected - set(scales))
    if missing:
        raise BackfillStageError(
            f"판정이 빠진 씬 {len(missing)}개: {', '.join(str(i) for i in missing)}"
        )
    return scales


def apply_scales(
    script: dict[str, Any], scales: dict[int, str]
) -> dict[str, Any]:
    """`subject_scale`만 끼운 새 문서. 원본은 건드리지 않는다.

    필드는 `subject` 바로 뒤에 놓는다 — 스펙 02의 씬 스키마 순서 그대로다.
    """
    scenes: list[dict[str, Any]] = []
    for scene in script["scenes"]:
        rebuilt: dict[str, Any] = {}
        for key, value in scene.items():
            if key == ADDED_FIELD:
                continue  # 이미 있으면 새 판정으로 덮어쓴다
            rebuilt[key] = value
            if key == "subject":
                rebuilt[ADDED_FIELD] = scales[scene["scene_id"]]
        scenes.append(rebuilt)

    updated = dict(script)
    updated["scenes"] = scenes
    return updated


def assert_only_scale_changed(
    before: dict[str, Any], after: dict[str, Any], *, field_name: str = ADDED_FIELD
) -> None:
    """대본이 그대로인지 실제로 비교한다. 다르면 쓰지 않고 실패한다.

    `field_name`은 백필 대상 필드다 — 승인된 대본에 필드 하나만 끼우는 마이그레이션은
    ADR-0018(`subject_scale`)에 이어 ADR-0022(`visual_goal`)로 두 번째라 여기를 열어 뒀다.
    이 가드가 이 계열 단계의 존재 이유다: 승인된 대본을 조용히 바꾸는 것이 낼 수 있는
    최악의 결과이므로, 쓰기 전에 나머지 필드를 전부 대조하고 하나라도 다르면 멈춘다.
    """
    if before.keys() != after.keys():
        raise BackfillStageError("대본 최상위 키가 달라졌다")
    for key in before:
        if key != "scenes" and before[key] != after[key]:
            raise BackfillStageError(f"대본의 '{key}'가 달라졌다")

    old_scenes, new_scenes = before["scenes"], after["scenes"]
    if len(old_scenes) != len(new_scenes):
        raise BackfillStageError(
            f"씬 수가 {len(old_scenes)}개에서 {len(new_scenes)}개로 달라졌다"
        )
    for old, new in zip(old_scenes, new_scenes):
        stripped = {k: v for k, v in new.items() if k != field_name}
        original = {k: v for k, v in old.items() if k != field_name}
        if stripped != original:
            raise BackfillStageError(
                f"씬 {old.get('scene_id')}에서 {field_name} 말고 다른 필드가 달라졌다"
            )


def run_backfill_scale_stage(
    slug: str,
    *,
    llm: LLMClient,
    paths: Paths | None = None,
    force: bool = False,
) -> BackfillResult:
    paths = paths or Paths.from_env()

    script_path = paths.topic_dir(slug) / SCRIPT_FILE
    if not script_path.exists():
        raise BackfillStageError(
            f"대본이 없다: {script_path}. [2. validate]가 끝난 토픽에만 쓴다."
        )
    try:
        script = json.loads(script_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BackfillStageError(f"{SCRIPT_FILE}을(를) 읽을 수 없다: {exc}") from exc

    scenes = script.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise BackfillStageError(f"{script_path}에 scenes 배열이 없다")

    topic = script.get("topic", slug)

    if not force and all(scene.get(ADDED_FIELD) for scene in scenes):
        log.info("[%s] 전 씬에 %s가 이미 있다 (slug=%s)", STAGE, ADDED_FIELD, slug)
        return BackfillResult(
            topic=topic, slug=slug, script_path=script_path,
            scene_count=len(scenes), skipped=True,
        )

    prompt = _load_prompt().safe_substitute(scenes=render_scenes(scenes))
    log.info("[%s] 분류 세션 시작 — %d씬 (도구 없음)", STAGE, len(scenes))
    result = llm.run(prompt, allowed_tools=TOOLS, timeout=TIMEOUT, label=STAGE)

    try:
        payload = extract_json_object(result.text)
    except JSONExtractionError as exc:
        raise BackfillStageError(f"세션 출력이 JSON 객체가 아니다: {exc}") from exc

    scales = parse_scales(payload, scenes)
    updated = apply_scales(script, scales)
    assert_only_scale_changed(script, updated)

    errors, _warnings = validate_scenes(updated)
    if errors:
        listed = "\n".join(f"  - {e}" for e in errors[:5])
        raise BackfillStageError(
            f"{ADDED_FIELD}를 채운 대본이 씬 계약을 어긴다:\n{listed}"
        )

    write_text(script_path, dump_json(updated))

    counts: dict[str, int] = {}
    for scale in scales.values():
        counts[scale] = counts.get(scale, 0) + 1

    return BackfillResult(
        topic=topic, slug=slug, script_path=script_path,
        scale_counts=counts, scene_count=len(scenes),
    )
