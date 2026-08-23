"""[5. prompt] — 씬 계약을 씬별 **영상 프롬프트**로 옮긴다 (ADR-0056).

specs/05-pipeline.md:
    [5. prompt] → prompts.json (씬별 영상 프롬프트 — 닫힌 골격, 스펙 03)

## 경계 (ADR-0017 — ADR-0052)

입력은 씬 계약(`runs/{run_id}/scenes.json`, `[3s]` 산출)과 **선택적** `refs.json`
(`[4]` 산출, ADR-0030 — 씬별 실사 서술을 앵커 뒤에 싣는다. 없으면 그냥 간다) 둘이고
모두 **읽기 전용**이다. 산출물은 `runs/{run_id}/prompts.json`뿐이고, `run_id`는 계약
파일에 적힌 값을 그대로 쓴다 — 계보를 run_id로 잇는다는 ADR-0017 그대로다. 이 단계는
`topics/` 아래에 아무것도 쓰지 않는다.

## 이 단계가 판단하지 않는 것

**연출은 전부 씬 계약에서 온다** (ADR-0033 §3). 고른 것은 `[3s. scenetable]`이고 이
모듈은 그것을 스펙 03 「프롬프트 골격」에 채우는 변환기다 (`visual_rules.build_video_prompt`).
씬이 `framing`·`staging`을 비웠을 때만 `beat-defaults.json`의 기본값으로 떨어지며,
**그 씬 수를 요약에 낸다** — 연출 선택이 형식적으로 비어 있는지를 보는 유일한 수단이다.
어휘 밖의 연출은 만들지 않는다. **영어 문장은 전부 `vocab.json`의 것이다** (ADR-0034) —
이 파일에도 `visual_rules.py`에도 프롬프트 문장이 없다.

## 길이를 쓰지 않는다

클립 길이는 `[7]`이 세 언어의 실측에서 정한다 (ADR-0056 결정 2). FORMAT 절의 초 수는
`{seconds}` 자리표시자로 남고 `[7]`이 그때 채운다. `prompts.json`은 시간 정보를 담지 않는다.

## 외부 의존 없음

순수 텍스트 변환이다. 네트워크도 API 키도 LLM 세션도 쓰지 않는다. 같은 입력이면 항상
같은 바이트가 나오도록 산출물에 타임스탬프를 넣지 않는다 (재현성).

## 게이트는 여기가 아니다

ADR-0017의 `judgment/human.json` 게이트는 2부 **진입점**인 `[3. tts+sync]`가 본다.
`[5]`는 이미 진입한 run 안에서 도는 단계라 게이트를 다시 검사하지 않는다.
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
from ..schemas import refs as refs_schema
from .contract import SceneContractNotFound, load_scene_contract
from ..schemas.scenes import validate_scenes
from ..schemas.visual_rules import (
    ASPECT_RATIO,
    BASE_STYLE,
    COMPOSITION,
    FRAMINGS,
    FROM_DEFAULT,
    RESOLUTION,
    build_video_prompt,
    resolve_framing,
    resolve_staging,
    schema_errors,
)

log = logging.getLogger(__name__)

STAGE = "5-prompt"

PROMPTS_FILE = "prompts.json"

#: `[4] refpack`의 산출물 (ADR-0030). **선택적 입력이다** (D-3) — 없으면 그냥 간다.
#: 소비하는 것은 `description`(서술 경로) 하나다 — 영상 프로바이더에 이미지 입력이 없다.
REFS_FILE = refs_schema.RECORD_FILE


class PromptStageError(Exception):
    pass


@dataclass
class PromptResult:
    topic: str
    slug: str
    run_id: str
    prompts_path: Path | None = None
    prompts: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False
    #: `subject_anchor`가 비어 있지 않은 씬 수 (ADR-0028). 씬 계약에서 센다 —
    #: prompts.json에는 앵커가 프롬프트 문자열에 녹아 들어가 따로 남지 않는다.
    anchored_scenes: int = 0
    #: `refs.json`의 서술이 실린 씬 수 (ADR-0030 서술 경로). 앵커와 같은 이유로
    #: 요약에 낸다 — [4]가 습관적으로 비우는지를 보는 관측 수단이다.
    described_scenes: int = 0

    @property
    def scene_count(self) -> int:
        return len(self.prompts["scenes"]) if self.prompts else 0

    @property
    def scale_counts(self) -> dict[str, int]:
        """스케일별 씬 수. ADR-0018의 되돌릴 조건("한 값으로 90% 이상 쏠림") 관측용."""
        counts: dict[str, int] = {}
        for scene in self.prompts["scenes"] if self.prompts else []:
            counts[scene["subject_scale"]] = counts.get(scene["subject_scale"], 0) + 1
        return counts

    def _defaulted(self, key: str) -> int:
        if not self.prompts:
            return 0
        return sum(1 for s in self.prompts["scenes"] if s[key] == FROM_DEFAULT)

    @property
    def default_framed_scenes(self) -> int:
        """구도를 씬이 고르지 않아 **기본값으로 떨어진** 씬 수 (ADR-0033).

        이 값이 되돌릴 조건의 관측 수단이다 — `[3s]`가 연출을 습관적으로 비우면
        구도가 다시 비트 표에서 나오게 되고, 그건 ADR-0033 이전과 같은 상태다.
        경고가 아니라 요약에 내는 이유는 한 편만 보고 판정할 값이 아니어서다.
        """
        return self._defaulted("framing_source")

    @property
    def default_staged_scenes(self) -> int:
        """무대를 씬이 고르지 않아 기본값(`studio`)으로 떨어진 씬 수 (ADR-0056 결정 4)."""
        return self._defaulted("staging_source")

    @property
    def info_scenes(self) -> int:
        """RED 절이 있는 씬 수 — 화면 계측 표시를 세운 씬 (ADR-0056 결정 3)."""
        if not self.prompts:
            return 0
        return sum(1 for s in self.prompts["scenes"] if s["has_info"])

    @property
    def summary(self) -> str:
        tail = " (스킵)" if self.skipped else ""
        scales = " / ".join(
            f"{scale} {count}" for scale, count in sorted(self.scale_counts.items())
        )
        return (
            f"[5] {self.topic} — {self.scene_count}씬 ({scales}) / "
            f"인포 {self.info_scenes}씬 / "
            f"대상 앵커 {self.anchored_scenes}씬 / "
            f"참조 서술 {self.described_scenes}씬 / "
            f"구도 기본값 {self.default_framed_scenes}씬 / "
            f"무대 기본값 {self.default_staged_scenes}씬 "
            f"→ {PROMPTS_FILE}{tail}"
        )


def _load_json(path: Path, what: str) -> dict[str, Any]:
    if not path.exists():
        raise PromptStageError(f"{what}이(가) 없다: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PromptStageError(f"{what}을(를) 읽을 수 없다: {path} — {exc}") from exc


def build_prompts(
    script: dict[str, Any],
    *,
    source_script: str,
    refs: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """씬 계약 → prompts.json 문서. (문서, 경고) 를 돌려준다.

    `refs`는 `[4] refpack`의 `refs.json` 문서다 (ADR-0030 — **서술 경로**). 씬의
    `description`을 앵커 뒤에 싣는다 (스펙 03의 항목 순서). 없거나 그 씬이 비어
    있으면 그냥 간다 — 부재는 경고가 아니다 (D-3).

    `characters`(ADR-0051)는 서술 경로뿐이다 — `cast` 씬의 SUBJECT에 `appearance`를
    싣고 `cast`를 복사한다. 시트 프롬프트는 없다: 프로바이더에 참조 입력이 없다
    (ADR-0056 되돌릴 조건 6).
    """
    scenes: list[dict[str, Any]] = script["scenes"]
    warnings: list[str] = []
    out_scenes: list[dict[str, Any]] = []

    # ADR-0051 — 인물 블록. 없으면 인물 경로 전체가 이 함수를 그냥 통과한다 (D-3).
    characters: list[dict[str, Any]] = script.get("characters") or []
    characters_by_id = {c["id"]: c for c in characters}

    for scene in scenes:
        sid = scene["scene_id"]
        beat = scene["beat"]
        scale = scene["subject_scale"]

        token, framing_source = resolve_framing(scene)
        staging, staging_source = resolve_staging(scene)

        # ADR-0051 — cast는 characters의 id만 가리킨다. 스키마가 못 잡는 교차 규칙이라
        # 여기서 막는다 — 깨진 참조로 만든 프롬프트는 [7]에서 돈만 쓰고 실패한다.
        cast = scene.get("cast") or []
        unknown = [cid for cid in cast if cid not in characters_by_id]
        if unknown:
            raise PromptStageError(
                f"씬 {sid}: characters에 없는 cast id {unknown}. "
                "고치는 곳은 씬 계약 하나다 (ADR-0020)"
            )
        appearances = [characters_by_id[cid]["appearance"] for cid in cast]

        info = scene.get("info") or None
        try:
            prompt_text, negative_text = build_video_prompt(
                subject=scene["subject"],
                shot=FRAMINGS[token].shot,
                staging=staging,
                camera=scene["camera"],
                # ADR-0028 — 선택 필드다. 없는 씬도 비운 씬도 그냥 빈 목록이고,
                # 부재를 경고하지 않는다. 경고를 달면 선택 필드가 선택이 아니게 된다.
                anchors=scene.get("subject_anchor") or (),
                description=refs_schema.description_of(refs, sid) if refs else "",
                appearances=appearances,
                info=info,
            )
        except ValueError as exc:
            raise PromptStageError(f"씬 {sid}: {exc}") from exc

        entry: dict[str, Any] = {
            "scene_id": sid,
            # 씬 계약의 값을 그대로 복사한다 — 고치는 곳은 씬 계약 하나다 (ADR-0020).
            "beat": beat,
            "subject_scale": scale,
            "camera": scene["camera"],
            "staging": staging,
            "staging_source": staging_source,
            "framing": token,
            "framing_source": framing_source,
            "has_info": info is not None,
            "prompt": prompt_text,
            "negative_prompt": negative_text,
        }
        if cast:
            entry["cast"] = list(cast)
        out_scenes.append(entry)

    document = {
        "run_id": script["run_id"],
        "topic": script["topic"],
        "source_script": source_script,
        "style": {
            "base_style": BASE_STYLE,
            "composition": COMPOSITION,
            "aspect_ratio": ASPECT_RATIO,
            "resolution": RESOLUTION,
        },
        "scenes": out_scenes,
    }
    return document, warnings


def _anchored_scenes(script: dict[str, Any]) -> int:
    """`subject_anchor`를 채운 씬 수 (ADR-0028).

    **이 값이 되돌릴 조건의 관측 수단이다.** `[3s]`가 습관적으로 비우면(예: 27씬 중 2씬)
    선택 필드가 죽은 것이므로 프롬프트를 고칠지 필수로 올릴지 다시 본다. 그래서 경고가
    아니라 요약에 낸다 — 한 편만 보고 판정할 값이 아니다.
    """
    return sum(1 for scene in script["scenes"] if scene.get("subject_anchor"))


def _load_refs(
    run_dir: Path, run_id: str
) -> tuple[dict[str, Any] | None, list[str]]:
    """`refs.json`을 연다 — **선택적 입력이다** (D-3). `(문서, 경고)`.

    부재는 조용히 넘어간다. 있는데 못 쓰는 경우(파싱 실패·계약 위반·다른 run)는
    전부 경고하고 없는 것으로 친다 — 서술은 있으면 좋은 것이지 조건이 아니라서
    여기서 멈추면 참조 없는 편이 전부 멈춘다. `[4]`가 `[5]`보다 늦게 돌았으면
    `--force`로 다시 낸다 — 재생성은 무료다.
    """
    path = run_dir / REFS_FILE
    if not path.exists():
        return None, []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, [f"{REFS_FILE}을 읽을 수 없다 ({exc}) → 참조 서술 없이 간다"]
    errors = refs_schema.validate_refs(document)
    if errors:
        return None, [
            f"{REFS_FILE}이 계약을 위반한다 ({errors[0]}) → 참조 서술 없이 간다"
        ]
    if document.get("run_id") != run_id:
        # 계보는 run_id로 잇는다 (ADR-0017). 다른 편의 사진 서술을 실으면
        # 실패가 아니라 틀린 그림이 나온다.
        return None, [
            f"{REFS_FILE}의 run_id({document.get('run_id')})가 대상 run({run_id})과 "
            "다르다 → 참조 서술 없이 간다"
        ]
    return document, []


def run_prompt_stage(
    slug: str,
    *,
    paths: Paths | None = None,
    force: bool = False,
) -> PromptResult:
    paths = paths or Paths.from_env()

    try:
        script, script_path = load_scene_contract(paths, slug)
    except SceneContractNotFound as exc:
        raise PromptStageError(str(exc)) from exc

    # 읽기 전용 입력이지만 계약 위반은 여기서 막는다. 깨진 씬으로 만든 프롬프트는
    # [7]에서 돈만 쓰고 실패한다.
    errors, scene_warnings = validate_scenes(script)
    if errors:
        raise PromptStageError(
            f"{script_path}가 씬 계약을 어겼다 ({len(errors)}건): " + "; ".join(errors)
        )

    run_id = script["run_id"]
    topic = script["topic"]
    run_dir = paths.run_dir(run_id)
    prompts_path = run_dir / PROMPTS_FILE
    state = RunState.load_or_create(run_dir, run_id, topic=topic, slug=slug)

    if state.is_done(STAGE) and not force and prompts_path.exists():
        previous = _load_json(prompts_path, PROMPTS_FILE)
        log.info("[%s] 이미 완료된 단계라 스킵한다 (run_id=%s)", STAGE, run_id)
        return PromptResult(
            topic=topic, slug=slug, run_id=run_id, skipped=True,
            prompts_path=prompts_path, prompts=previous,
            anchored_scenes=_anchored_scenes(script),
        )

    state.mark_running(STAGE)

    # [4]의 refs.json — 있으면 서술을 싣고 없으면 그냥 간다 (specs/05, ADR-0030).
    refs_doc, refs_warnings = _load_refs(run_dir, run_id)
    described = sum(
        1
        for s in script["scenes"]
        if refs_doc and refs_schema.description_of(refs_doc, s["scene_id"])
    )

    try:
        document, warnings = build_prompts(
            script,
            source_script=script_path.relative_to(paths.root).as_posix(),
            refs=refs_doc,
        )
    except PromptStageError as exc:
        # specs/05 실패 정책: 단계 실패는 run 디렉터리에 기록하고 종료
        state.mark_failed(STAGE, str(exc))
        raise
    warnings = list(scene_warnings) + refs_warnings + warnings

    output_errors = schema_errors(document)
    if output_errors:
        message = f"{PROMPTS_FILE} 스키마 위반: " + "; ".join(output_errors)
        state.mark_failed(STAGE, message)
        raise PromptStageError(message)

    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)

    write_text(prompts_path, dump_json(document))
    result = PromptResult(
        topic=topic, slug=slug, run_id=run_id,
        prompts_path=prompts_path, prompts=document, warnings=warnings,
        anchored_scenes=_anchored_scenes(script),
        described_scenes=described,
    )
    state.mark_done(
        STAGE,
        output=prompts_path.relative_to(paths.root).as_posix(),
        source_script=document["source_script"],
        scenes=len(document["scenes"]),
        # 편이 쌓여야 판정할 값이라 run 상태에도 남긴다 (ADR-0028·0030·0033·0056 되돌릴 조건).
        info_scenes=result.info_scenes,
        anchored_scenes=result.anchored_scenes,
        described_scenes=described,
        default_framed_scenes=result.default_framed_scenes,
        default_staged_scenes=result.default_staged_scenes,
        warnings=warnings,
    )
    return result
