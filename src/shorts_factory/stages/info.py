"""[6i. info] — 인포씬의 CLEAN에 라벨·수치를 얹어 INFO 이미지를 만든다. ADR-0043.

specs/05-pipeline.md:
    [6i. info] → info/{scene_id}.jpg + info.json (인포씬만. CLEAN에 NB2 편집으로
    라벨·수치를 얹은 INFO 이미지 + 자소 대조 검수)

## 이 단계가 지키는 것

- **인포씬만** — `06-script.json`에 `info` 필드가 있는 씬. 없으면 단계 전체가
  스킵된다 (D-3: 선택적 입력의 부재는 경고가 아니다)
- **라벨은 계약 문자열 그대로** — 고치거나 요약하지 않는다 (ADR-0020). 문자열 속
  수치의 그라운딩은 `[1s]` 직후 검증과 `[2]` 게이트가 이미 봤다
- **배치·색·지시선은 지시하지 않는다** — 편집 모델(NB2) 재량이다 (ADR-0043)
- **검수 포함** — 비전 세션이 렌더된 글자를 계약 문자열과 자소 단위로 대조하고 구도
  이탈을 본다. `1間` 사고(`[6r]`이 못 거른 것)가 근거다. 실패 시 재생성 1회, 그래도
  실패면 **그 씬을 인포 없는 일반 영상으로 강등**하고 기록한다 (D-5)

## 강등은 파일의 부재로 전달된다

`[7]`은 `info/{scene_id}.jpg`가 **있으면** Veo, 없으면 일반 경로다 (specs/05).
그래서 강등한 씬의 INFO 파일은 지운다 — 검수에 떨어진 그림을 남겨 두면 그것이
영상의 끝 프레임이 된다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any, Protocol

from ..config import Paths, write_text
from ..imagegen.base import ImageGenError, ProviderNotConfigured
from ..jsonio import JSONExtractionError, dump_json, extract_json_object
from ..llm.base import LLMClient
from ..runstate import RunState
from .motion import find_image

log = logging.getLogger(__name__)

STAGE = "6i-info"

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
PROMPT = "14-inforeview.md"

SCRIPT_FILE = "06-script.json"
IMAGES_DIR = "images"
INFO_DIR = "info"
RECORD_FILE = "info.json"

#: 검수 세션의 도구 — 이미지를 직접 열어 본다 (`[6r]`과 같은 메커니즘, ADR-0031).
TOOLS: tuple[str, ...] = ("Read",)

#: 재생성 상한 (ADR-0043). 초과는 강등이다 — 판정↔재생성 루프에는 끝나는 조건이 없다.
MAX_REDO = 1

#: 씬 하나의 결과 상태.
GENERATED = "generated"      # 1회에 통과
REGENERATED = "regenerated"  # 재생성 후 통과
DEMOTED = "demoted"          # 검수·호출 실패 → 인포 없는 일반 영상으로
CACHED = "cached"

PASS, REDO = "pass", "redo"


class ImageEditor(Protocol):
    """`[6i]`가 요구하는 유일한 표면 — 지시 편집. `NanoBananaClient.edit`가 구현한다."""

    def edit(self, instruction: str, image_path: Path, *, timeout: int | None = None): ...


class InfoStageError(Exception):
    pass


@dataclass
class InfoResult:
    run_id: str
    topic: str
    run_dir: Path
    record_path: Path | None = None
    scenes: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False

    def count(self, status: str) -> int:
        return sum(1 for s in self.scenes if s["status"] == status)

    @property
    def demoted(self) -> int:
        return self.count(DEMOTED)

    @property
    def summary(self) -> str:
        tail = " (스킵)" if self.skipped else ""
        if not self.scenes:
            return f"[6i] {self.topic} — 인포씬 없음 → 단계 스킵 (D-3){tail}"
        return (
            f"[6i] {self.topic} — 인포씬 {len(self.scenes)}개 "
            f"(통과 {self.count(GENERATED)} · 재생성 통과 {self.count(REGENERATED)} · "
            f"이어받기 {self.count(CACHED)} · 강등 {self.demoted}) → {INFO_DIR}/{tail}"
        )


def _load_json(path: Path, what: str) -> dict[str, Any]:
    if not path.exists():
        raise InfoStageError(f"{what}이(가) 없다: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InfoStageError(f"{what}을(를) 읽을 수 없다: {path} — {exc}") from exc


def resolve_run_id(paths: Paths, slug: str) -> str:
    script = _load_json(paths.topic_dir(slug) / SCRIPT_FILE, f"씬 계약({SCRIPT_FILE})")
    run_id = script.get("run_id")
    if not run_id:
        raise InfoStageError(f"{SCRIPT_FILE}에 run_id가 없다")
    return str(run_id)


def _script_for_run(paths: Paths, run_id: str, slug: str | None) -> dict[str, Any]:
    if slug:
        return _load_json(paths.topic_dir(slug) / SCRIPT_FILE, f"씬 계약({SCRIPT_FILE})")
    for candidate in sorted(paths.topics.glob(f"*/{SCRIPT_FILE}")):
        try:
            script = json.loads(candidate.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if script.get("run_id") == run_id:
            return script
    raise InfoStageError(f"run_id={run_id}의 씬 계약을 찾지 못했다. --slug로 지정하라")


def info_scenes(script: dict[str, Any]) -> list[dict[str, Any]]:
    """`info` 필드가 있는 씬. **존재가 곧 인포씬이다** (ADR-0043)."""
    return [
        scene
        for scene in script.get("scenes", [])
        if isinstance(scene.get("info"), dict) and scene["info"].get("labels")
    ]


def edit_instruction(scene: dict[str, Any]) -> str:
    """CLEAN → INFO 편집 지시. 라벨은 계약 문자열 그대로다 (ADR-0020).

    배치·색·지시선은 적지 않는다 — 편집 모델 재량이다 (ADR-0043). 시각 표현을 계약하기
    시작하면 닫힌 어휘 원칙과 충돌하는 자유 기술이 생긴다.
    """
    listed = "\n".join(f"- {label}" for label in scene["info"]["labels"])
    return (
        "이 그림의 구도, 카메라, 조명, 재질, 배경, 피사체를 그대로 보존하라. "
        "장면을 새로 그리거나 회전·이동·확대·축소하지 마라.\n"
        "그 위에 공학 다큐멘터리 스타일의 인포그래픽을 얹어라 — 지시선과 화살표는 "
        "실제 구조와 현상에 닿게, 장면의 원근을 따르게 하라.\n"
        "화면에 넣을 텍스트는 아래 목록이 전부다. **한 글자도 바꾸거나 더하지 마라**:\n"
        f"{listed}\n"
        "이 목록 밖의 글자·숫자·로고·워터마크를 만들지 마라."
    )


def render_review_scenes(
    targets: list[dict[str, Any]], files: dict[int, str], clean: dict[int, Path],
) -> str:
    """검수 세션에 보여줄 목록 — 씬마다 CLEAN·INFO 경로와 계약 라벨."""
    blocks: list[str] = []
    for scene in targets:
        sid = scene["scene_id"]
        labels = "\n".join(f"  - `{label}`" for label in scene["info"]["labels"])
        blocks.append(
            "\n".join(
                [
                    f"## 씬 {sid}",
                    f"- CLEAN(원본): {IMAGES_DIR}/{clean[sid].name}",
                    f"- INFO(판정 대상): {files[sid]}",
                    "- 화면에 있어야 하는 라벨 (자소 단위로 대조하라):",
                    labels,
                ]
            )
        )
    return "\n\n".join(blocks)


def parse_reviews(
    payload: dict[str, Any], expected: set[int]
) -> dict[int, dict[str, Any]]:
    """검수 세션 출력 → `{scene_id: {verdict, reason}}`. 빠진 씬은 오류다."""
    reviews: dict[int, dict[str, Any]] = {}
    for item in payload.get("scenes") or []:
        if not isinstance(item, dict):
            continue
        sid = item.get("scene_id")
        verdict = item.get("verdict")
        if not isinstance(sid, int) or verdict not in (PASS, REDO):
            raise InfoStageError(f"판정을 읽을 수 없다: {item!r}")
        reviews[sid] = {"verdict": verdict, "reason": str(item.get("reason", ""))}
    missing = expected - set(reviews)
    if missing:
        raise InfoStageError(
            f"판정이 빠진 씬이 있다: {sorted(missing)} — 전 씬을 판정해야 한다"
        )
    return reviews


def _review(
    llm: LLMClient,
    *,
    topic: str,
    targets: list[dict[str, Any]],
    files: dict[int, str],
    clean: dict[int, Path],
    run_dir: Path,
    label: str,
    timeout: int | None,
) -> dict[int, dict[str, Any]]:
    prompt = Template((PROMPTS_DIR / PROMPT).read_text(encoding="utf-8")).safe_substitute(
        topic=topic,
        scenes=render_review_scenes(targets, files, clean),
    )
    session = llm.run(
        prompt,
        allowed_tools=TOOLS,
        timeout=timeout,
        label=label,
        add_dirs=(run_dir / INFO_DIR, run_dir / IMAGES_DIR),
    )
    try:
        payload = extract_json_object(session.text)
    except JSONExtractionError as exc:
        raise InfoStageError(f"검수 세션 출력이 JSON 객체가 아니다: {exc}") from exc
    return parse_reviews(payload, {s["scene_id"] for s in targets})


def run_info_stage(
    *,
    llm: LLMClient,
    editor: ImageEditor,
    run_id: str | None = None,
    slug: str | None = None,
    paths: Paths | None = None,
    force: bool = False,
    timeout: int | None = None,
) -> InfoResult:
    paths = paths or Paths.from_env()

    if not run_id:
        if not slug:
            raise InfoStageError("run_id나 slug 중 하나는 있어야 한다")
        run_id = resolve_run_id(paths, slug)

    run_dir = paths.run_dir(run_id)
    script = _script_for_run(paths, run_id, slug)
    topic = script.get("topic") or run_id
    record_path = run_dir / RECORD_FILE
    info_dir = run_dir / INFO_DIR
    result = InfoResult(run_id=run_id, topic=topic, run_dir=run_dir)

    seed: dict[str, Any] = {"topic": topic}
    if slug:
        seed["slug"] = slug
    state = RunState.load_or_create(run_dir, run_id, **seed)

    targets = info_scenes(script)
    if not targets:
        # D-3: 인포씬이 없는 편에서는 이 기능만 빠지고 파이프라인은 그대로 돈다.
        log.info("[%s] info 필드가 있는 씬이 없다 — 단계 스킵 (D-3)", STAGE)
        state.mark_done(STAGE, scene_count=0, note="인포씬 없음 (D-3)")
        return result

    if state.is_done(STAGE) and not force and record_path.exists():
        previous = _load_json(record_path, RECORD_FILE)
        log.info("[%s] 이미 완료된 단계라 스킵한다 (run_id=%s)", STAGE, run_id)
        result.skipped = True
        result.record_path = record_path
        result.scenes = previous.get("scenes", [])
        return result

    # CLEAN을 전부 먼저 모은다 — 없으면 호출 전에 멈춘다 ([7]의 _collect_images와 같은 이유).
    clean: dict[int, Path] = {}
    for scene in targets:
        try:
            clean[scene["scene_id"]] = find_image(run_dir / IMAGES_DIR, scene["scene_id"])
        except FileNotFoundError:
            raise InfoStageError(
                f"씬 {scene['scene_id']}의 CLEAN 이미지가 없다 ({IMAGES_DIR}/). "
                "[6. imagegen]을 먼저 실행하라 (specs/05)."
            ) from None

    state.mark_running(STAGE)
    info_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    records: dict[int, dict[str, Any]] = {}
    files: dict[int, str] = {}
    provider_down = False

    def demote(scene_id: int, reason: str, *, attempts: int, review: dict | None = None) -> None:
        """강등 — INFO 파일을 지워 `[7]`이 일반 경로를 타게 한다. 조용히 하지 않는다."""
        target = info_dir / f"{scene_id}.jpg"
        if target.exists():
            target.unlink()
        warnings.append(f"씬 {scene_id}: {reason} → 인포 없는 일반 영상으로 강등 (D-5)")
        records[scene_id] = {
            "scene_id": scene_id,
            "labels": next(s["info"]["labels"] for s in targets if s["scene_id"] == scene_id),
            "file": None,
            "status": DEMOTED,
            "attempts": attempts,
            "review": review,
        }

    def generate(scene: dict[str, Any], *, attempt: int) -> bool:
        nonlocal provider_down
        sid = scene["scene_id"]
        if provider_down:
            demote(sid, "편집 프로바이더가 이미 막혀 시도하지 않는다", attempts=attempt - 1)
            return False
        try:
            image = editor.edit(
                edit_instruction(scene), clean[sid], timeout=timeout
            )
        except ProviderNotConfigured as exc:
            provider_down = True
            demote(sid, f"편집 프로바이더 전체를 쓸 수 없다 ({exc})", attempts=attempt)
            return False
        except ImageGenError as exc:
            demote(sid, f"편집 호출 실패 ({exc})", attempts=attempt)
            return False

        target = info_dir / f"{sid}.jpg"
        target.write_bytes(image.data)
        files[sid] = f"{INFO_DIR}/{target.name}"
        records[sid] = {
            "scene_id": sid,
            "labels": scene["info"]["labels"],
            "file": files[sid],
            "status": GENERATED if attempt == 1 else REGENERATED,
            "attempts": attempt,
            "engine": image.meta,
            "review": None,
        }
        return True

    # 1차 생성 → 검수 → redo 씬만 재생성 → 재검수 (상한 MAX_REDO) → 잔여는 강등.
    pending = [s for s in targets if generate(s, attempt=1)]

    for round_no in range(1, MAX_REDO + 2):
        if not pending:
            break
        try:
            reviews = _review(
                llm,
                topic=topic, targets=pending, files=files, clean=clean,
                run_dir=run_dir, timeout=timeout,
                label=STAGE if round_no == 1 else f"{STAGE}.review{round_no}",
            )
        except InfoStageError as exc:
            # 검수가 깨지면 통과로 넘기지 않는다 — 검수 없는 생성 텍스트는 금지다 (ADR-0043).
            for scene in pending:
                demote(scene["scene_id"], f"검수 실패 ({exc})",
                       attempts=records[scene["scene_id"]]["attempts"])
            pending = []
            break

        redo: list[dict[str, Any]] = []
        for scene in pending:
            sid = scene["scene_id"]
            records[sid]["review"] = reviews[sid]
            if reviews[sid]["verdict"] == PASS:
                continue
            if round_no > MAX_REDO:
                demote(sid, f"재생성 후에도 검수 기각 ({reviews[sid]['reason']})",
                       attempts=records[sid]["attempts"], review=reviews[sid])
                continue
            if generate(scene, attempt=records[sid]["attempts"] + 1):
                redo.append(scene)
        pending = redo

    ordered = [records[s["scene_id"]] for s in targets if s["scene_id"] in records]
    document = {
        "run_id": run_id,
        "topic": topic,
        "source_script": SCRIPT_FILE,
        "scenes": ordered,
        "warnings": warnings,
    }
    write_text(record_path, dump_json(document))

    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)

    result.record_path = record_path
    result.scenes = ordered
    result.warnings = warnings

    state.mark_done(
        STAGE,
        scene_count=len(ordered),
        generated=result.count(GENERATED),
        regenerated=result.count(REGENERATED),
        demoted=result.demoted,
        warnings=warnings,
        outputs=[record_path.relative_to(paths.root).as_posix()],
    )
    return result
