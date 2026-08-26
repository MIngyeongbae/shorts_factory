"""[5. prompt] — 씬 계약 위에서 **헤드리스 세션 1회**가 씬별 샷 서술을 쓰고, 코드가 골격에 얹는다. ADR-0060.

specs/05-pipeline.md:
    [5. prompt] → prompts.json (씬별 영상 프롬프트 — 어휘 골격 + 세션 단락, 스펙 03)

## 경계 (ADR-0017 — ADR-0052 — ADR-0060)

입력은 넷이고 전부 **읽기 전용**이다:

- `runs/{run_id}/scenes.json` — 씬 계약 (`[3s]`). 연출·무대·라벨·인물은 여기서 정해졌다
- `topics/{slug}/script.md` — 대본 전문. 머리(핵심 질문·반전·보이는 것)가 설계도다
- `topics/{slug}/factcheck.md` — 형태·치수·연도·재질의 근거. 없으면 "(없음)"으로 간다 (D-3)
- `runs/{run_id}/refs.json` — `[4]`의 실사 서술. 없으면 그냥 간다 (ADR-0030)

산출물은 `runs/{run_id}/prompts.json`뿐이고 출력 계약은 ADR-0056의 것 그대로다 — `[7]`은 이
ADR을 모른다 (단계 독립 D-2). `topics/` 아래에는 아무것도 쓰지 않는다.

## 세션이 쓰는 것과 코드가 쓰는 것

세션(`prompts/05-prompt.md`)은 씬마다 `subject_prompt`(SUBJECT 단락)·`camera_target`(착지)·
`red_prompt`(info 씬의 계측 기하)를 **영어**로 쓴다. 계약은 `specs/schema/promptplan.schema.json`
(`schemas/promptplan.py`) — ASCII·길이·라벨 포함·착지에 워크 단어 금지·씬 id 일치. 코드는 FORMAT·
STAGING·CAMERA 워크·NEGATIVE를 어휘에서 조립해 앞뒤에 붙인다 (`visual_rules.build_video_prompt`).
**연출은 여전히 씬 계약의 것이다** (ADR-0033 §3) — 세션은 그것을 서술로 구현할 뿐 바꾸지 못한다.

## 실패는 보고·중단이다

세션 산출이 계약을 어기면 `prompts.json`을 쓰지 않고 멈춘다 (ADR-0044 — 재생성 루프 없음).
세션 출력 원본은 `logs/`에 남는다. 지난 실행의 `prompts.json`이 남아 있으면 지운다 — 깨진 계약
파일이 남으면 `[7]`이 그대로 진행해 돈을 쓴다.

## 길이를 쓰지 않는다

클립 길이는 `[7]`이 세 언어의 실측에서 정한다 (ADR-0056 결정 2). FORMAT 절의 초 수는
`{seconds}` 자리표시자로 남고 `[7]`이 그때 채운다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Paths, write_text
from ..jsonio import dump_json
from ..llm.base import LLMClient
from ..runstate import RunState
from ..schemas import promptplan
from ..schemas import refs as refs_schema
from ..schemas import vocab
from ..schemas.scenes import validate_scenes
from ..schemas.visual_rules import (
    ANNOTATIONS,
    ASPECT_RATIO,
    BASE_STYLE,
    CAMERA_PROMPTS,
    COMPOSITION,
    FRAMINGS,
    FROM_DEFAULT,
    RESOLUTION,
    STAGINGS,
    MJPromptError,
    build_mj_prompt,
    build_video_prompt,
    mj_subject_budget,
    negative_items,
    resolve_framing,
    resolve_staging,
    schema_errors,
)
from .contract import SceneContractNotFound, load_scene_contract
from .session import ScriptSessionError, ask_json, load_prompt

log = logging.getLogger(__name__)

STAGE = "5-prompt"
PROMPT_FILE = "05-prompt.md"
PROMPTS_FILE = "prompts.json"
REFS_FILE = refs_schema.RECORD_FILE
SCRIPT_FILE = "script.md"
FACTCHECK_FILE = "factcheck.md"

#: 세션 상한(초). 씬 16개 × 영어 단락이라 `[3s]`보다 길다.
TIMEOUT = 900


class PromptStageError(Exception):
    """입력 부재·계약 위반 — 보고·중단."""


@dataclass
class PromptResult:
    topic: str
    slug: str
    run_id: str
    prompts_path: Path | None = None
    prompts: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: bool = False
    #: `subject_anchor`가 비어 있지 않은 씬 수 (ADR-0028 관측). 앵커는 이제 프롬프트에
    #: 실리지 않고(ADR-0060 결정 5) 세션이 영어로 푼다 — 씬 계약에서 센다.
    anchored_scenes: int = 0
    #: `refs.json`의 서술을 세션에 준 씬 수 (ADR-0030 관측).
    described_scenes: int = 0
    #: 세션 실행 메타 (session_id·turns·시간).
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.errors and self.prompts_path is not None

    @property
    def scene_count(self) -> int:
        return len(self.prompts["scenes"]) if self.prompts else 0

    @property
    def scale_counts(self) -> dict[str, int]:
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
        """구도를 씬이 고르지 않아 기본값으로 떨어진 씬 수 (ADR-0033 되돌릴 조건의 관측)."""
        return self._defaulted("framing_source")

    @property
    def default_staged_scenes(self) -> int:
        return self._defaulted("staging_source")

    @property
    def info_scenes(self) -> int:
        if not self.prompts:
            return 0
        return sum(1 for s in self.prompts["scenes"] if s["has_info"])

    @property
    def summary(self) -> str:
        if self.errors:
            return f"[5] {self.topic} — 샷 서술 계약 위반 {len(self.errors)}건, {PROMPTS_FILE}을 쓰지 않았다"
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
        raise PromptStageError(f"{what}이 없다: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PromptStageError(f"{what}을 읽을 수 없다: {path} — {exc}") from exc


# --- 세션 입력 -----------------------------------------------------------------


def scene_brief(scene: dict[str, Any]) -> dict[str, Any]:
    """씬 하나를 세션에 보여 줄 모양 — 계약 값 + 그 값의 **어휘 문구**(세션이 구현할 연출).

    구도·카메라·무대·주석 문구는 어휘에서 온다 (ADR-0034). 세션은 이 문구가 말하는 연출을
    서술로 구현하고, 값을 바꾸지 못한다.
    """
    token, framing_source = resolve_framing(scene)
    staging, staging_source = resolve_staging(scene)
    brief: dict[str, Any] = {
        "scene_id": scene["scene_id"],
        "text": scene.get("text", ""),
        "beat": scene.get("beat"),
        "visual_goal": scene.get("visual_goal", ""),
        "subject": scene.get("subject", ""),
        "subject_anchor": list(scene.get("subject_anchor") or []),
        "subject_scale": scene.get("subject_scale"),
        "staging": {"value": staging, "phrase": STAGINGS[staging], "source": staging_source},
        "framing": {"value": token, "shot": FRAMINGS[token].shot, "source": framing_source},
        "camera": {"value": scene["camera"], "phrase": CAMERA_PROMPTS[scene["camera"]]},
    }
    info = scene.get("info") or None
    if info:
        brief["info"] = {
            "labels": list(info.get("labels", [])),
            "target": info.get("target", ""),
            "annotation": {
                "value": info.get("annotation"),
                "phrase": ANNOTATIONS.get(str(info.get("annotation")), ""),
            },
        }
        # 정보를 지는 구도 장치 (ADR-0075 결정 5). 선택이라 없으면 싣지 않는다 —
        # 그때는 지금까지대로 표시만으로 간다 (D-3).
        device = str(info.get("device") or "")
        if device:
            brief["info"]["device"] = {
                "value": device,
                "phrase": vocab.info_device_phrase(device),
            }
    shot2 = scene.get("shot2") or None
    if shot2:
        brief["shot2"] = {
            "framing": {"value": shot2["framing"], "shot": FRAMINGS[shot2["framing"]].shot},
            "camera": {"value": shot2["camera"], "phrase": CAMERA_PROMPTS[shot2["camera"]]},
        }
    if scene.get("cast"):
        brief["cast"] = list(scene["cast"])
    return brief


def format_scenes(contract: dict[str, Any]) -> str:
    return "\n".join(
        json.dumps(scene_brief(scene), ensure_ascii=False) for scene in contract["scenes"]
    )


def format_characters(contract: dict[str, Any]) -> str:
    characters = contract.get("characters") or []
    if not characters:
        return ""
    body = "\n".join(json.dumps(c, ensure_ascii=False) for c in characters)
    return (
        "## 인물 (characters — `cast` 씬의 SUBJECT에 `appearance`를 그대로 싣는다, ADR-0051)\n\n"
        + body
    )


def format_refs(refs: dict[str, Any] | None, contract: dict[str, Any]) -> tuple[str, int]:
    """`(절 텍스트, 서술이 있는 씬 수)`. 없으면 빈 절."""
    if not refs:
        return "", 0
    lines = []
    for scene in contract["scenes"]:
        sid = scene["scene_id"]
        description = refs_schema.description_of(refs, sid)
        if description:
            lines.append(f"- 씬 {sid}: {description}")
    if not lines:
        return "", 0
    return (
        "## 실사 참조 서술 (refs.json — [4]가 실물 사진을 보고 적은 형태. SUBJECT의 형태 근거로 쓴다)\n\n"
        + "\n".join(lines),
        len(lines),
    )


#: `mj_subject` 절의 본문. 단어 수만 치환한다 — 예산은 어휘에서 계산해 넣는다
#: (`mj_subject_budget`, ADR-0034: 숫자를 프롬프트에 적어 넣지 않는다).
MJ_BLOCK = """## `mj_subject` — CLEAN 정지 이미지용 한 줄 (영어, **{low}~{high}단어**, 모든 씬)

이 라인은 씬마다 **정지 이미지를 먼저 그리고** 영상 모델이 그 사이를 잇는다 (ADR-0070·0071). 그 이미지를 사는 프롬프트의 **소재부**를 쓴다.

- **서술 문장이 아니라 명사구 나열이다.** `subject_prompt`를 줄이는 것이 아니라 같은 씬을 다른 문법으로 쓰는 것이다: 무엇이, 어떤 재질로, 어떻게 놓였는지를 쉼표로 잇는다
- **콜론·세미콜론·줄바꿈을 쓰지 마라** — 그 문자를 구분자로 읽지 않는 모델이라 라벨이 화면 지시로 섞인다
- **스타일·화풍·조명·카메라를 쓰지 마라.** 스타일 문자열은 코드가 뒤에 붙인다 — 그 앞에 스타일 낱말이 또 있으면 소재가 밀린다
- **글자·숫자·라벨·빨강을 쓰지 마라.** 이 이미지는 글자가 없는 CLEAN이고, 계측 표시는 다음 단계가 그 위에 얹는다
- 단어 수가 {low}보다 적으면 소재를 잃고 {high}보다 많으면 스타일 절이 꼬리에서 무시된다. 기계 검사가 반려한다

"""


def format_mj_block(line: str | None) -> str:
    """`mj_subject` 절 — 프레임을 입력으로 받는 라인에서만 붙는다 (ADR-0071).

    라인 이름으로 분기하지 않는다 — 어휘가 `style_in_frames`로 말한다 (ADR-0034).
    """
    if not line or not vocab.style_in_frames(line):
        return ""
    low, high = mj_subject_budget(line)
    return MJ_BLOCK.format(low=low, high=high)


def build_session_prompt(
    *,
    topic: str,
    script_text: str,
    factcheck: str | None,
    contract: dict[str, Any],
    refs: dict[str, Any] | None,
    line: str | None = None,
) -> tuple[str, int]:
    """세션 프롬프트 전문. `(prompt, 참조 서술 씬 수)`."""
    refs_block, described = format_refs(refs, contract)
    subject_min, subject_max = promptplan.length_limits(promptplan.SUBJECT_FIELD)
    target_min, target_max = promptplan.length_limits(promptplan.CAMERA_TARGET_FIELD)
    red_min, red_max = promptplan.length_limits(promptplan.RED_FIELD)
    prompt = load_prompt(PROMPT_FILE).substitute(
        topic=topic,
        script=script_text.strip(),
        factcheck=(factcheck or "(없음 — 팩트체크가 없어 형태 근거는 대본 머리뿐이다)").strip(),
        scenes=format_scenes(contract),
        characters=format_characters(contract),
        refs=refs_block,
        subject_min=subject_min, subject_max=subject_max,
        target_min=target_min, target_max=target_max,
        red_min=red_min, red_max=red_max,
        mj_block=format_mj_block(line),
    )
    return prompt, described


# --- 세션 산출 → prompts.json ---------------------------------------------------


def build_prompts(
    contract: dict[str, Any],
    plan: dict[str, Any],
    *,
    source_script: str,
    line: str | None = None,
) -> dict[str, Any]:
    """씬 계약 + 세션 단락 → prompts.json 문서. `plan`은 `promptplan.validate_promptplan`을 통과한 것.

    **프레임을 받는지는 라인이 아니라 씬이 정한다** (ADR-0075 결정 3). 프레임 라인의
    일반 씬은 프레임이 그림을 지므로 영상 프롬프트가 카메라 워크 구절 하나이고 MJ 한 줄
    (`mj_image_prompt`)을 함께 나른다. 같은 라인의 `info` 씬은 텍스트→영상이라 프레임이
    없으므로 **전체 골격 + STYLE 절**(`ttv_style`)을 받고 MJ를 타지 않는다.

    어느 라인이 그런지는 어휘가 정하고 여기서 라인 이름을 분기하지 않는다 (ADR-0034).
    """
    planned = {int(e["scene_id"]): e for e in plan["scenes"]}
    #: 영상 프롬프트의 STYLE 절이 쓰는 룩. 프레임 라인이라도 `info` 씬은 이것을 받는다.
    video_look = (
        vocab.line_style(str(line), engine=vocab.TTV_ENGINE) if line else BASE_STYLE
    )
    #: MJ 한 줄이 쓰는 룩 — 프레임을 받는 씬이 있는 라인에만 있다.
    mj_look = (
        vocab.line_style(str(line), engine=vocab.MJ_ENGINE)
        if line and vocab.style_in_frames(str(line))
        else None
    )
    out_scenes: list[dict[str, Any]] = []
    for scene in contract["scenes"]:
        sid = int(scene["scene_id"])
        entry = planned[sid]
        token, framing_source = resolve_framing(scene)
        staging, staging_source = resolve_staging(scene)
        info = scene.get("info") or None
        # 이 씬이 프레임을 받는가 — 라인이 프레임 라인이고 `info`가 없을 때만이다.
        scene_frames = bool(line) and vocab.scene_takes_frames(
            str(line), has_info=info is not None
        )
        try:
            prompt_text, negative_text = build_video_prompt(
                subject_prompt=str(entry[promptplan.SUBJECT_FIELD]),
                staging=staging,
                camera=scene["camera"],
                camera_target=str(entry.get(promptplan.CAMERA_TARGET_FIELD, "")),
                red_prompt=str(entry[promptplan.RED_FIELD]) if info else None,
                frames=scene_frames,
                style="" if scene_frames else video_look,
            )
        except ValueError as exc:
            raise PromptStageError(f"씬 {sid}: {exc}") from exc
        record: dict[str, Any] = {
            "scene_id": sid,
            "beat": scene["beat"],
            "subject_scale": scene["subject_scale"],
            "camera": scene["camera"],
            "staging": staging,
            "staging_source": staging_source,
            "framing": token,
            "framing_source": framing_source,
            "has_info": info is not None,
            "video_prompt": prompt_text,
            "negative_prompt": negative_text,
            # 세션 단락을 조립본 옆에 그대로 싣는다 (ADR-0067) — `[7]`의 고쳐쓰기 재생성이
            # 부분만 고쳐 같은 골격에 다시 얹으려면 부분이 남아 있어야 한다. 판단은 없다.
            promptplan.SUBJECT_FIELD: str(entry[promptplan.SUBJECT_FIELD]),
            promptplan.CAMERA_TARGET_FIELD: str(
                entry.get(promptplan.CAMERA_TARGET_FIELD, "")
            ),
        }
        if info:
            record[promptplan.RED_FIELD] = str(entry[promptplan.RED_FIELD])
            # 정보를 지는 구도 장치 (ADR-0075 결정 5) — 씬 계약의 값을 그대로 나른다.
            # `[7]`의 검수가 "그 장치가 실제로 보이는가"를 볼 수 있는 자리다.
            if info.get("device"):
                record["info_device"] = str(info["device"])
        if entry.get(promptplan.SHOT2_FIELD):
            record[promptplan.SHOT2_FIELD] = str(entry[promptplan.SHOT2_FIELD])
        if entry.get(promptplan.MJ_SUBJECT_FIELD):
            record[promptplan.MJ_SUBJECT_FIELD] = str(entry[promptplan.MJ_SUBJECT_FIELD])
            # MJ 한 줄을 여기서 완성해 싣는다 (ADR-0075 결정 3) — 예산·방언 검사가 재는
            # 대상은 조립본이고, 지금까지는 `mj_subject`만 재고 최종 문자열은 아무도
            # 안 쟀다. `[6]`의 고쳐쓰기는 원료를 고쳐 이 함수와 같은 조립을 다시 한다.
            if scene_frames and mj_look:
                try:
                    record["mj_image_prompt"] = build_mj_prompt(
                        subject=str(entry[promptplan.MJ_SUBJECT_FIELD]),
                        mj_style=mj_look,
                        negatives=negative_items(has_info=False),
                    )
                except MJPromptError as exc:
                    raise PromptStageError(f"씬 {sid}: MJ 한 줄이 계약을 어겼다 — {exc}") from exc
        if scene.get("cast"):
            record["cast"] = list(scene["cast"])
        out_scenes.append(record)
    return {
        "run_id": contract["run_id"],
        "topic": contract["topic"],
        "source_script": source_script,
        **({"line": str(line)} if line else {}),
        "style": {
            # 이 run이 실제로 그리는 룩 — 정본은 여전히 vocab이고 여기는 기록이다.
            # 엔진마다 쓰는 말이 다르므로 둘이다 (ADR-0075 결정 7): `base_style`은 영상
            # 프롬프트의 STYLE 절이 받은 것, `mj_style`은 MJ 한 줄이 받은 것이다.
            "base_style": video_look,
            **({"mj_style": mj_look} if mj_look else {}),
            "composition": COMPOSITION,
            "aspect_ratio": ASPECT_RATIO,
            "resolution": RESOLUTION,
        },
        "scenes": out_scenes,
    }


def _anchored_scenes(contract: dict[str, Any]) -> int:
    return sum(
        1 for s in contract["scenes"]
        if any(str(a).strip() for a in (s.get("subject_anchor") or []))
    )


def _load_refs(run_dir: Path, run_id: str) -> tuple[dict[str, Any] | None, list[str]]:
    path = run_dir / REFS_FILE
    if not path.exists():
        return None, []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, [f"{REFS_FILE}을 읽을 수 없어 서술 없이 간다: {exc}"]
    errors = refs_schema.schema_errors(document)
    if errors:
        return None, [f"{REFS_FILE}이 계약을 어겨 서술 없이 간다: {errors[0]}"]
    if document.get("run_id") != run_id:
        return None, [f"{REFS_FILE}의 run_id({document.get('run_id')})가 다르다 — 서술 없이 간다"]
    return document, []


def _resolve_line(paths: Paths, slug: str, line: str | None) -> str | None:
    """사람이 고른 영상 라인 (`judgment/human.json`, ADR-0059). `--line`이 이긴다.

    **못 읽어도 멈추지 않는다** — 라인이 바꾸는 것은 STYLE 절 유무와 `mj_subject`뿐이고,
    판정 파일이 아직 없는 상태에서 `[5]`를 돌려 보는 것은 정상이다. 그때는 어휘의 기본
    라인으로 간다 (`read_video_line`이 이미 그렇게 한다) — 값이 어휘 밖일 때만 멈춘다.
    """
    from ..judgment import JudgmentError, read_video_line

    if line:
        vocab.require("video_line", line)
        return line
    try:
        return read_video_line(paths, slug)
    except JudgmentError as exc:
        raise PromptStageError(str(exc)) from exc


def run_prompt_stage(
    slug: str,
    *,
    llm: LLMClient,
    paths: Paths | None = None,
    force: bool = False,
    timeout: int = TIMEOUT,
    line: str | None = None,
) -> PromptResult:
    paths = paths or Paths.from_env()

    try:
        contract, contract_path = load_scene_contract(paths, slug)
    except SceneContractNotFound as exc:
        raise PromptStageError(str(exc)) from exc

    errors, scene_warnings = validate_scenes(contract)
    if errors:
        raise PromptStageError(
            f"{contract_path}가 씬 계약을 어겼다 ({len(errors)}건): " + "; ".join(errors)
        )

    run_id = contract["run_id"]
    topic = contract["topic"]
    run_dir = paths.run_dir(run_id)
    prompts_path = run_dir / PROMPTS_FILE
    state = RunState.load_or_create(run_dir, run_id, topic=topic, slug=slug)

    if state.is_done(STAGE) and not force and prompts_path.exists():
        previous = _load_json(prompts_path, PROMPTS_FILE)
        log.info("[%s] 이미 완료된 단계라 스킵한다 (run_id=%s)", STAGE, run_id)
        return PromptResult(
            topic=topic, slug=slug, run_id=run_id, skipped=True,
            prompts_path=prompts_path, prompts=previous,
            anchored_scenes=_anchored_scenes(contract),
        )

    script_path = paths.topic_dir(slug) / SCRIPT_FILE
    if not script_path.exists():
        raise PromptStageError(f"대본이 없다: {script_path}")
    script_text = script_path.read_text(encoding="utf-8")
    factcheck_path = paths.topic_dir(slug) / FACTCHECK_FILE
    factcheck = factcheck_path.read_text(encoding="utf-8") if factcheck_path.exists() else None

    state.mark_running(STAGE)
    if prompts_path.exists():
        prompts_path.unlink()

    refs_doc, refs_warnings = _load_refs(run_dir, run_id)
    warnings = list(scene_warnings) + refs_warnings
    if factcheck is None:
        warnings.append(f"{FACTCHECK_FILE}이 없어 형태 근거 없이 돈다 — 세션은 대본 머리만 본다 (D-3)")

    resolved_line = _resolve_line(paths, slug, line)
    prompt, described = build_session_prompt(
        topic=topic, script_text=script_text, factcheck=factcheck,
        contract=contract, refs=refs_doc, line=resolved_line,
    )
    try:
        plan, meta = ask_json(llm, prompt, label=STAGE, tools=(), timeout=timeout)
    except ScriptSessionError as exc:
        state.mark_failed(STAGE, str(exc))
        raise PromptStageError(str(exc)) from exc

    plan_errors = promptplan.validate_promptplan(plan, contract, line=resolved_line)
    if plan_errors:
        for error in plan_errors:
            log.error("[%s] %s", STAGE, error)
        state.mark_failed(STAGE, f"샷 서술 계약 위반 {len(plan_errors)}건", errors=plan_errors, **meta)
        return PromptResult(
            topic=topic, slug=slug, run_id=run_id, errors=plan_errors, warnings=warnings, meta=meta,
            anchored_scenes=_anchored_scenes(contract), described_scenes=described,
        )

    try:
        document = build_prompts(
            contract, plan, source_script=contract_path.relative_to(paths.root).as_posix(),
            line=resolved_line,
        )
    except PromptStageError as exc:
        state.mark_failed(STAGE, str(exc))
        raise

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
        prompts_path=prompts_path, prompts=document, warnings=warnings, meta=meta,
        anchored_scenes=_anchored_scenes(contract), described_scenes=described,
    )
    state.mark_done(
        STAGE,
        output=prompts_path.relative_to(paths.root).as_posix(),
        source_script=document["source_script"],
        scenes=len(document["scenes"]),
        info_scenes=result.info_scenes,
        anchored_scenes=result.anchored_scenes,
        described_scenes=described,
        default_framed_scenes=result.default_framed_scenes,
        default_staged_scenes=result.default_staged_scenes,
        warnings=warnings,
        **meta,
    )
    return result
