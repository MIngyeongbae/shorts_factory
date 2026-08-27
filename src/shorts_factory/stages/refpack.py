"""[4. refpack] — 씬별 실사 참조를 모은다. ADR-0030.

specs/05-pipeline.md:
    [4. refpack] → refs/{scene_id}/*.jpg + refs.json  (씬별 실사 참조: 사진 + 서술)

## 왜 있는가

ADR-0028이 텍스트로 대상을 고정하는 데까지 갔지만 **텍스트 지시에는 상한이 있다.**
고유명사가 통하는 것은 모델이 그것을 이미 알 때뿐이고, 이 채널의 소재는 대부분
그렇지 않다 — 판별 기준이 "문제 해결 서사"라 오히려 덜 알려진 구조물이 자주 온다.
`[0b] research`가 웹을 뒤질 때 **글의 근거만 모으고 그림의 근거는 버리고 있었다.**

## 경계 (ADR-0017)

입력은 씬 계약(`runs/{run_id}/scenes.json`) 하나이고 **읽기 전용**이다. 산출은
`runs/{run_id}/` 아래뿐이다 — `refs.json`과 내려받은 파일. `topics/` 아래에 아무것도
쓰지 않는다. `[3]`·`[5]`와 선후가 없다.

## 이 단계가 판단하지 않는 것

- **무엇을 그릴지.** `subject`·`subject_anchor`가 `[3s]`에서 이미 정해졌다.
  `description`은 **그것이 실제로 어떻게 생겼는지**를 더할 뿐이고, 겹치면 `subject`가
  이긴다 (ADR-0020·0030)
- **첨부해도 되는지.** 세션은 라이선스를 **보고**하고 대조는 `_apply_license()`가 한다.
  저작권이 걸린 축이라 편마다 흔들리면 안 된다

## 강등 사다리: `첨부+서술 → 서술 → 없음`

두 경로를 한 몸으로 만들지 않은 것이 ADR-0030의 요점이다. 사진을 못 찾아도, 라이선스가
불명이라 첨부가 막혀도, 내려받기가 실패해도 **서술 경로는 그대로 산다.** 세 경우 다
경고만 남기고 단계는 끝까지 돈다 (specs/05 D-5).

**비어 있는 씬은 경고하지 않는다** (D-3). 도해 씬과 실물이 없는 개념 씬에는 참조가
없는 것이 정상이고, 부재를 경고로 만들면 편마다 경고가 쏟아져 진짜 경고가 묻힌다.

## 세션에 웹 도구를 준다

지금까지 세션에 읽기 도구만 준 것과 다르다 (ADR-0011). `WebSearch`로 찾고 `WebFetch`로
출처 페이지의 라이선스를 확인한다 — 검색 결과 요약만으로는 라이선스가 확인되지 않고,
확인 못 한 것을 `public-domain`으로 적으면 첨부 대조가 무의미해진다.

**파일을 쓰는 것은 여전히 오케스트레이터다.** 세션은 URL과 서술을 JSON으로 돌려주고
내려받기는 여기 Python이 한다.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import Paths, write_text
from ..jsonio import dump_json
from ..llm.base import LLMClient
from ..runstate import RunState
from .contract import SceneContractNotFound, load_scene_contract
from ..schemas import refs as refs_schema
from ..schemas.scenes import validate_scenes
from .session import ScriptSessionError, ask_json, load_prompt

log = logging.getLogger(__name__)

STAGE = "4-refpack"
PROMPT = "13-refpack.md"
REFS_FILE = refs_schema.RECORD_FILE
REFS_DIR = "refs"

#: 세션에 주는 도구. `[4]`가 웹 도구를 받는 첫 단계다 (ADR-0030).
TOOLS: tuple[str, ...] = ("WebSearch", "WebFetch")

#: 세션 상한(초). 씬마다 검색이 돌아 생성만 하는 단계보다 길다.
TIMEOUT = 1800

#: 사진 하나를 내려받는 상한(초)과 크기(바이트). 참조는 프롬프트 입력이라 원본
#: 해상도가 필요 없고, 상한이 없으면 잘못 걸린 주소 하나가 디스크를 먹는다.
DOWNLOAD_TIMEOUT = 60
MAX_IMAGE_BYTES = 20 * 1024 * 1024

#: 내려받기 사이 간격(초). **실측으로 들어온 값이다** — 첫 실세션에서 위키미디어가
#: 연속 요청을 429로 막아 24씬 중 6장만 받았다. 참조 수집은 급할 일이 아니고
#: 상대 서버의 한도가 우리 벽시계보다 비싸다.
DOWNLOAD_INTERVAL = 1.5

#: 내려받기 신원. 위키미디어는 무엇이 왜 받아 가는지 밝히는 UA를 요구하고,
#: **연락처(URL 또는 메일)가 없으면 429로 끊는다** (실측 2026-08-27 — QR 편 `[4]`가
#: 사진 17장을 전부 429로 잃었다. 같은 순간 같은 파일이 연락처를 단 UA에는 200으로 왔다).
#: 설명만으로는 통과하지 못하므로 프로젝트 저장소 주소를 붙인다 — **사람의 메일 주소를
#: 쓰지 않는다.** 요청 헤더는 외부로 나가는 자리다.
USER_AGENT = (
    "shorts-factory/0.1 (knowledge-shorts pipeline; reference photos for image prompts; "
    "+https://github.com/MIngyeongbae/shorts_factory)"
)

#: 주소에서 확장자를 못 읽었을 때. 참조 사진은 대부분 JPEG다.
DEFAULT_EXT = ".jpg"
KNOWN_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")

#: `(url, timeout) -> (status, bytes)`. 유일한 HTTP 경계다 — 테스트가 여기를 바꿔 낀다.
Fetch = Callable[[str, int], "tuple[int, bytes]"]


class RefpackStageError(Exception):
    """`[4]`가 결과를 낼 수 없는 경우. 씬 단위 실패는 여기로 오지 않는다."""


def urllib_fetch(url: str, timeout: int) -> tuple[int, bytes]:
    """stdlib GET. 상한을 넘는 응답은 읽다가 끊는다."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(MAX_IMAGE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        # 상태 코드를 살려 돌려준다 — 429는 호출자가 사유로 남길 값이지 예외가 아니다.
        return exc.code, b""


@dataclass
class RefpackResult:
    topic: str
    slug: str
    run_id: str
    path: Path | None
    refs: dict[str, Any] | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False

    @property
    def valid(self) -> bool:
        return self.refs is not None and not self.errors

    @property
    def described(self) -> int:
        """서술이 채워진 씬 수. 강등 사다리의 **서술** 칸이 실제로 산 수다."""
        return sum(
            1
            for scene in (self.refs or {}).get("scenes", [])
            if str(scene.get("description") or "").strip()
        )

    @property
    def attached(self) -> int:
        """첨부 가능한 파일이 하나라도 있는 씬 수. **첨부** 칸이다."""
        return sum(
            1
            for scene in (self.refs or {}).get("scenes", [])
            if any(i.get("attachable") and i.get("file") for i in scene.get("images", []))
        )

    @property
    def summary(self) -> str:
        if self.refs is None:
            return f"[4] {self.topic} — 참조 수집 실패"
        total = len(self.refs.get("scenes", []))
        files = sum(
            1
            for scene in self.refs["scenes"]
            for image in scene.get("images", [])
            if image.get("file")
        )
        tail = " (스킵)" if self.skipped else ""
        return (
            f"[4] {self.topic} — {total}씬 (서술 {self.described} / 첨부 {self.attached} "
            f"/ 파일 {files}장) → {REFS_FILE}{tail}"
        )


def _load_json(path: Path, what: str) -> dict[str, Any]:
    if not path.exists():
        raise RefpackStageError(f"{what}이(가) 없다: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RefpackStageError(f"{what}을(를) 읽을 수 없다: {path} — {exc}") from exc


def resolve_run_id(paths: Paths, slug: str) -> str:
    """슬러그 → run_id. 경계면 파일에 적힌 값을 그대로 쓴다 (ADR-0017 "계보는 run_id").

    세션 로그를 어느 run 디렉터리에 남길지 정하려고 단계 실행 **전에** 필요하다.
    """
    try:
        script, _path = load_scene_contract(paths, slug)
    except SceneContractNotFound as exc:
        raise RefpackStageError(str(exc)) from exc
    run_id = script.get("run_id")
    if not run_id:
        raise RefpackStageError(f"씬 계약에 run_id가 없다 (slug={slug})")
    return str(run_id)


def format_scenes(script: dict[str, Any]) -> str:
    """씬을 조사 세션이 읽을 표로. **대본 문장(`text`)을 주지 않는다.**

    찾아야 할 것은 그림의 대상이고 그것은 `subject`·`subject_anchor`·`visual_goal`이
    이미 말하고 있다 (specs/05 D-6과 같은 태도 — 하류가 필요한 것을 상류가 깎지도,
    상류의 것을 하류가 끌어오지도 않는다).
    """
    lines: list[str] = []
    for scene in script.get("scenes", []):
        anchor = str(scene.get("subject_anchor") or "").strip()
        goal = str(scene.get("visual_goal") or "").strip()
        lines.append(
            f"## 씬 {scene.get('scene_id')} — 크기: `{scene.get('subject_scale')}`\n"
            f"- 피사체: {scene.get('subject')}\n"
            f"- 고정 명사: {anchor or '(없음)'}\n"
            f"- 그림 목표: {goal or '(없음)'}"
        )
    return "\n\n".join(lines)


def format_licenses() -> str:
    """라이선스 어휘를 프롬프트 목록으로. 값은 `refs.schema.json`에서 온다."""
    described = refs_schema.REFS_SCHEMA["$defs"]["license"]
    attachable = set(refs_schema.attachable_licenses())
    lines = []
    for value in described["enum"]:
        mark = " — 첨부 가능" if value in attachable else " — 서술 경로만"
        lines.append(f"- `{value}`{mark}")
    return "\n".join(lines)


def _ext_of(url: str) -> str:
    """주소에서 확장자. 질의 문자열이 붙어 있어도 경로만 본다."""
    path = urllib.parse.urlparse(url).path.lower()
    for ext in KNOWN_EXTS:
        if path.endswith(ext):
            return ext
    return DEFAULT_EXT


def _apply_license(image: dict[str, Any]) -> dict[str, Any]:
    """세션이 보고한 라이선스로 `attachable`을 **기계가** 채운다.

    세션이 `attachable`을 적어 보냈어도 버린다 — 판정 권한이 프롬프트로 새면 편마다
    기준이 흔들린다.
    """
    license_name = str(image.get("license") or "unknown")
    entry: dict[str, Any] = {
        "source_url": str(image.get("source_url") or "").strip(),
        "license": license_name,
        "attachable": refs_schema.is_attachable(license_name),
    }
    for key in ("credit", "shows"):
        value = str(image.get(key) or "").strip()
        if value:
            entry[key] = value
    # 참조 적합성은 **세션이 판정한다** (ADR-0077) — 라이선스와 달리 대조할 목록이 없고
    # 사진을 봐야 아는 값이다. `attachable`과 축이 다르므로 여기서 덮어쓰지 않는다.
    if image.get("reference_ok") is not None:
        entry["reference_ok"] = bool(image.get("reference_ok"))
        note = str(image.get("reference_note") or "").strip()
        if note:
            entry["reference_note"] = note
    return entry


def download_images(
    entries: list[dict[str, Any]],
    *,
    scene_id: int,
    run_dir: Path,
    fetch: Fetch,
    warnings: list[str],
    interval: float = DOWNLOAD_INTERVAL,
) -> list[dict[str, Any]]:
    """**쓰일 사진만** 내려받아 `file`을 채운다 — 재현용(`attachable`)이거나 참조용(`reference_ok`).

    ADR-0077 전에는 `attachable`만 받았다. 참조는 라이선스가 아니라 적합성이 고르므로
    (재현과 축이 다르다) PD가 아닌 사진도 참조로 쓰이면 실물이 있어야 한다.

    **실패는 강등이지 중단이 아니다** (specs/05 D-5). 못 받은 사진은 `attachable`·
    `reference_ok`가 함께 내려가고 서술 경로만 남는다 — 그 사진을 보고 쓴 `description`은
    그대로 살아 있다.
    """
    kept: list[dict[str, Any]] = []
    index = 0
    for entry in entries:
        if not (entry["attachable"] or entry.get("reference_ok")):
            kept.append(entry)
            continue
        if not entry["source_url"]:
            entry["attachable"] = False
            entry["reference_ok"] = False
            warnings.append(f"씬 {scene_id}: 주소가 비어 첨부하지 못했다")
            kept.append(entry)
            continue

        index += 1
        target = run_dir / REFS_DIR / str(scene_id) / f"{index:02d}{_ext_of(entry['source_url'])}"
        if interval > 0:
            time.sleep(interval)
        try:
            status, raw = fetch(entry["source_url"], DOWNLOAD_TIMEOUT)
            if status != 200:
                raise RefpackStageError(f"HTTP {status}")
            if not raw:
                raise RefpackStageError("빈 응답")
            if len(raw) > MAX_IMAGE_BYTES:
                raise RefpackStageError(f"{MAX_IMAGE_BYTES}바이트를 넘는다")
        except (RefpackStageError, OSError, urllib.error.URLError, ValueError) as exc:
            entry["attachable"] = False
            # 실물이 없으면 참조도 못 붙인다 (ADR-0077) — `[6]`이 파일을 올려야 주소가 생긴다.
            entry["reference_ok"] = False
            warnings.append(
                f"씬 {scene_id}: {entry['source_url']}을 내려받지 못해 서술만 쓴다 — {exc}"
            )
            kept.append(entry)
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        entry["file"] = target.relative_to(run_dir).as_posix()
        kept.append(entry)
    return kept


def build_refs(
    script: dict[str, Any],
    payload: dict[str, Any],
    *,
    run_dir: Path,
    fetch: Fetch | None,
    source_script: str = "",
    download_interval: float = DOWNLOAD_INTERVAL,
) -> tuple[dict[str, Any], list[str]]:
    """세션 출력 + 대본 → `refs.json` 문서. `(document, warnings)`.

    **대본에 없는 씬은 버린다.** 세션이 지어낸 `scene_id`를 통과시키면 소비 단계가
    없는 씬을 조회한다.
    """
    warnings: list[str] = []
    known = [int(s["scene_id"]) for s in script.get("scenes", [])]
    reported: dict[int, dict[str, Any]] = {}
    for entry in payload.get("scenes", []) or []:
        if not isinstance(entry, dict):
            continue
        try:
            # 세션이 `scene_id`를 문자열로 내는 일이 있다. 버리면 그 씬의 조사 결과가
            # 통째로 사라지는데, 형식 슬립이지 계약 위반이 아니다.
            reported[int(entry["scene_id"])] = entry
        except (KeyError, TypeError, ValueError):
            continue

    if not reported:
        # 씬 하나가 빈 것은 정상이지만(D-3) 전부가 비면 세션이 일을 안 한 것이다.
        warnings.append(
            "세션이 씬을 하나도 보고하지 않았다 — 참조 없이 [5]·[6]이 그대로 돈다"
        )

    stray = sorted(set(reported) - set(known))
    if stray:
        warnings.append(
            f"대본에 없는 씬을 보고해 버렸다: {', '.join(str(s) for s in stray)}"
        )

    limit = refs_schema.max_images_per_scene()
    scenes: list[dict[str, Any]] = []
    for scene_id in known:
        entry = reported.get(scene_id) or {}
        images = [
            _apply_license(image)
            for image in (entry.get("images") or [])
            if isinstance(image, dict)
        ][:limit]
        if fetch is not None:
            images = download_images(
                images,
                scene_id=scene_id,
                run_dir=run_dir,
                fetch=fetch,
                warnings=warnings,
                interval=download_interval,
            )
        else:
            # `--no-download`: 강등 사다리의 **서술** 칸으로 내려서 돈다.
            for image in images:
                image["attachable"] = False

        queries = [str(q).strip() for q in (entry.get("query") or []) if str(q).strip()]
        scenes.append(
            {
                "scene_id": scene_id,
                "query": queries,
                "description": str(entry.get("description") or "").strip(),
                "images": images,
            }
        )

    document = refs_schema.build_document(
        str(script["run_id"]), scenes, source_script=source_script
    )
    return document, warnings


def run_refpack_stage(
    slug: str,
    *,
    llm: LLMClient,
    paths: Paths | None = None,
    force: bool = False,
    fetch: Fetch | None = urllib_fetch,
    timeout: int = TIMEOUT,
    download_interval: float = DOWNLOAD_INTERVAL,
) -> RefpackResult:
    """`fetch=None`이면 내려받지 않고 서술만 쓴다 (`--no-download`)."""
    paths = paths or Paths.from_env()

    try:
        script, script_path = load_scene_contract(paths, slug)
    except SceneContractNotFound as exc:
        raise RefpackStageError(str(exc)) from exc

    # 읽기 전용 입력이지만 계약 위반은 여기서 막는다. 깨진 씬으로 검색어를 만들면
    # 세션 시간만 쓰고 쓸 수 없는 참조가 나온다.
    errors, _ = validate_scenes(script)
    if errors:
        raise RefpackStageError(
            f"{script_path}가 씬 계약을 어겼다 ({len(errors)}건): " + "; ".join(errors)
        )

    run_id = str(script["run_id"])
    topic = str(script["topic"])
    run_dir = paths.run_dir(run_id)
    path = run_dir / REFS_FILE
    state = RunState.load_or_create(run_dir, run_id, topic=topic, slug=slug)

    if state.is_done(STAGE) and not force and path.exists():
        existing = _load_json(path, REFS_FILE)
        log.info("[%s] 이미 완료된 단계라 스킵한다 (run_id=%s)", STAGE, run_id)
        return RefpackResult(
            topic=topic, slug=slug, run_id=run_id, path=path, refs=existing,
            errors=refs_schema.validate_refs(existing), skipped=True,
        )

    state.mark_running(STAGE)
    log.info(
        "[%s] 조사 세션 시작 — %d씬 (도구: %s)",
        STAGE, len(script.get("scenes", [])), ", ".join(TOOLS),
    )

    template = load_prompt(PROMPT)
    prompt = template.safe_substitute(
        topic=topic,
        scenes=format_scenes(script),
        licenses=format_licenses(),
        max_images=refs_schema.max_images_per_scene(),
    )

    try:
        payload, meta = ask_json(llm, prompt, label=STAGE, tools=TOOLS, timeout=timeout)
    except ScriptSessionError as exc:
        message = f"{exc} 원본은 {run_dir / 'logs'}에 있다."
        state.mark_failed(STAGE, message)
        raise RefpackStageError(message) from exc

    document, warnings = build_refs(
        script,
        payload,
        run_dir=run_dir,
        fetch=fetch,
        source_script=script_path.relative_to(paths.root).as_posix(),
        download_interval=download_interval,
    )
    write_text(path, dump_json(document))

    output_errors = refs_schema.validate_refs(document)
    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)

    result = RefpackResult(
        topic=topic, slug=slug, run_id=run_id, path=path, refs=document,
        errors=output_errors, warnings=warnings,
    )
    info = {
        "output": path.relative_to(paths.root).as_posix(),
        "scenes": len(document["scenes"]),
        "described": result.described,
        "attached": result.attached,
        "validation_errors": output_errors,
        "validation_warnings": warnings,
        **meta,
    }
    if output_errors:
        log.warning("[%s] 계약 위반 %d건 — 산출물은 남긴다", STAGE, len(output_errors))
        state.mark_failed(STAGE, f"계약 위반 {len(output_errors)}건", **info)
    else:
        state.mark_done(STAGE, **info)
    return result
