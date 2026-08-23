"""[7. videogen] — 씬당 텍스트→영상 클립 1개 + 검수 + 강등 사다리. ADR-0056.

specs/05-pipeline.md:
    [7. videogen] → clips/{scene_id}.mp4 + clips.json + clip_review.json
                    (씬당 텍스트→영상 클립 1개, Gemini Omni Flash. 길이 = 세 언어 중
                     최장 씬 + 0.6초를 3~10초 안에서 올림. 끝 프레임 OCR + 씬당 비전
                     검수 1회 → 재생성 1회 → 라벨 없는 재생성/인접 씬 재사용 강등)

## 입력 (전부 run 디렉터리, 읽기 전용)

| 파일 | 읽는 것 |
|---|---|
| `scenes.json` | 씬 계약 — `subject`·`subject_anchor`·`visual_goal`·`info` (검수 기준) |
| `prompts.json` | 씬별 `prompt`·`negative_prompt`·`has_info` (`[5]`) |
| `scenes.timed.{lang}.json` | 씬 길이 — **ko 필수, ja·en은 있는 것만** (D-3). 최장이 클립 길이를 정한다 |

## 클립 길이 (스펙 05 `[7]`)

`ceil(max_lang(end − start) + 0.6)`을 3~10초로 클램프한다. 0.6초는 디졸브 꼬리(ADR-0024)이고
값은 어휘(`transition.dissolve.seconds`)에서 온다. 10초를 넘는 씬은 10초로 만들고 경고한다 —
`[9]`가 마지막 프레임을 정지로 늘린다. 클립의 로컬 0초가 씬의 `start`이고 꼬리는 전부 뒤다.

## 검수 (ADR-0056 결정 6) — `--review full|ocr|none`

① **끝 프레임 OCR** (`video/ocr.py`): `info` 씬은 라벨이 전부 읽히고 계약에 없는 숫자가
   없어야, `info` 없는 씬은 글자가 없어야 통과다. tesseract가 없으면 이 게이트만 건너뛰고
   `clip_review.json`에 경고를 남긴다.
② **비전 세션 씬당 1회** (`prompts/16-clipreview.md`): 시작·중간·끝 프레임 3장 + 씬 계약의
   그림 필드를 주고 네 기준으로 판정한다. 나레이션 `text`는 주지 않는다 (ADR-0038). 씬당
   1세션·동시 실행 — `[6r]`이 24장을 한 세션에 넣어 5회 타임아웃한 교훈이다.

## 실패 사다리

    생성 → 검수 실패 → 재생성 1회 (같은 프롬프트)
      → info 씬이면 RED 절을 뺀 프롬프트로 재생성 (demoted_from: "info")
      → 그래도 실패 / 일반 씬이면 인접 씬 클립 재사용 (demoted_from: "video", source_scene)

**강등을 조용히 하지 않는다** — `clips.json`에 씬마다 `demoted_from`, `clip_review.json`에
시도마다 판정 사유. 인접 재사용은 **모든 씬의 생성이 끝난 뒤** 직렬로 푼다 (앞 씬이 아직
생성 중일 수 있다). 가까운 앞 씬 우선, 없으면 가까운 뒤 씬.

## 동시 제출·429·직렬 기록

워커 수는 어댑터의 `concurrency()`이고 `--jobs`가 이긴다. 429가 오면 **워커 1 + 백오프**로
내려간다. 기록은 씬 하나가 끝날 때마다 잠금 아래 직렬로 쓴다 — 재실행 시 `clips.json`에
`done`인 씬은 다시 사지 않는다 (ADR-0020의 목적).

## 프로바이더 전체 거절 (D-5)

`VideoProviderNotConfigured`(키·플랜·파라미터 거부)는 씬이 아니라 어댑터의 문제다 — 남은
씬을 제출하지 않고 멈춘다. 이미 산 클립과 기록은 남는다.
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from ..config import Paths, write_text
from ..jsonio import JSONExtractionError, dump_json, extract_json_object
from ..llm.base import LLMClient, LLMError
from ..runstate import RunState
from ..schemas.timed_scenes import PRIMARY_LANGUAGE, present_languages
from ..schemas.visual_rules import demote_info, fill_seconds
from ..video.clips import FRAME_POSITIONS, FRAME_SUFFIX, frame_command, frame_times, normalize_command
from ..video.ffmpeg import DEFAULT_FFMPEG, FFmpegError, relative_path, run_ffmpeg
from ..video.ocr import OCR, OCRError, detect_ocr, gate as ocr_gate
from ..video.timeline import CLIPS_DIR, DISSOLVE_SECONDS
from ..videogen.base import (
    VideoClient,
    VideoGenError,
    VideoGenRateLimited,
    VideoProviderNotConfigured,
    VideoRequest,
)
from .contract import (
    SceneContractNotFound,
    TimedScenesNotFound,
    find_contract_for_run,
    load_scene_contract,
    load_timed_scenes,
)
from .prompt import PROMPTS_FILE
from .session import load_prompt

log = logging.getLogger(__name__)

STAGE = "7-videogen"
PROMPT = "16-clipreview.md"

#: 이 단계의 **실행 기록** 둘 (specs/05 계약 표).
RECORD_FILE = "clips.json"
REVIEW_FILE = "clip_review.json"
#: 검수 재료 — 프로바이더 원본·정규화 후보·프레임. `clips/`에는 채택된 클립만 놓는다.
REVIEW_DIR = "clip_review"

REVIEW_NONE, REVIEW_OCR, REVIEW_FULL = "none", "ocr", "full"
REVIEW_MODES = (REVIEW_NONE, REVIEW_OCR, REVIEW_FULL)

#: 비전 세션의 도구 — 프레임을 직접 열어 본다 (`[8]`과 같은 메커니즘).
TOOLS: tuple[str, ...] = ("Read",)
#: 비전 세션 상한(초). 프레임 3장을 보는 일이라 짧다.
SESSION_TIMEOUT = 300

#: 같은 프롬프트로 시도하는 횟수 — 생성 1 + 재생성 1 (스펙 05 `[7]`).
ATTEMPTS_PER_PROMPT = 2
#: 429 재시도 상한과 백오프 밑(초).
RATE_LIMIT_RETRIES = 4
RATE_LIMIT_BACKOFF = 15.0

PASS, FAIL, ERROR = "pass", "fail", "error"
DONE, NEEDS_REUSE, FAILED = "done", "needs_reuse", "failed"
DEMOTED_INFO, DEMOTED_VIDEO = "info", "video"
VARIANT_VIDEO, VARIANT_INFO, VARIANT_NO_RED = "video", "info", "no_red"


class VideogenStageError(Exception):
    """`[7]`이 결과를 낼 수 없는 경우. 씬 하나의 실패는 여기로 오지 않는다."""


class ProviderRefused(VideogenStageError):
    """프로바이더 전체 거절 — 남은 씬을 시도하지 않고 멈췄다 (D-5)."""


def resolve_run_id(
    paths: Paths, *, run_id: str | None = None, slug: str | None = None
) -> str:
    """`--run-id`를 그대로 쓰거나, `--slug`면 씬 계약에서 `run_id`를 읽는다 (ADR-0017)."""
    if run_id:
        return run_id
    if not slug:
        raise VideogenStageError("run_id 또는 slug 중 하나는 있어야 한다")
    try:
        contract, _path = load_scene_contract(paths, slug)
    except SceneContractNotFound as exc:
        raise VideogenStageError(str(exc)) from exc
    resolved = contract.get("run_id")
    if not resolved:
        raise VideogenStageError(f"씬 계약에 run_id가 없다 (slug={slug})")
    return str(resolved)


# --- 클립 길이 -----------------------------------------------------------------


def clip_seconds(
    durations: Sequence[float], *, tail: float = DISSOLVE_SECONDS,
    low: int = VideoClient.min_seconds, high: int = VideoClient.max_seconds,
) -> tuple[int, str | None]:
    """`(요청 초, 클램프 방향)` — 세 언어 최장 + 꼬리를 정수로 올려 프로바이더 범위에 가둔다.

    범위는 어댑터의 것이다 (`VideoClient.min_seconds`·`max_seconds`, ADR-0059) — 기본 인자는
    인터페이스의 기본값이고, 단계는 `build_jobs(seconds_range=…)`로 실제 어댑터의 값을 넘긴다.
    """
    if not durations:
        raise VideogenStageError("씬 길이가 없다")
    needed = max(float(d) for d in durations) + tail
    seconds = math.ceil(round(needed, 6))
    if seconds > high:
        return high, "max"
    if seconds < low:
        return low, "min"
    return seconds, None


@dataclass
class SceneJob:
    scene_id: int
    prompt: str
    negative_prompt: str
    has_info: bool
    labels: list[str]
    seconds: int
    lang_seconds: dict[str, float]
    clamped: str | None
    review_fields: dict[str, Any]


def build_jobs(
    contract: dict[str, Any],
    prompts: dict[str, Any],
    timed_by_lang: dict[str, dict[str, Any]],
    *,
    seconds_range: tuple[int, int] = (VideoClient.min_seconds, VideoClient.max_seconds),
) -> tuple[list[SceneJob], list[str]]:
    """세 입력 → 씬별 작업. `{seconds}`를 채우고 길이를 정한다. `(jobs, warnings)`.

    `seconds_range`는 어댑터가 받는 클립 길이 범위다 (ADR-0059 — 라인마다 다르다).
    """
    warnings: list[str] = []
    low, high = seconds_range
    by_prompt = {int(s["scene_id"]): s for s in prompts.get("scenes", [])}
    contract_scenes = {int(s["scene_id"]): s for s in contract.get("scenes", [])}

    if set(by_prompt) != set(contract_scenes):
        raise VideogenStageError(
            f"{PROMPTS_FILE}의 씬({sorted(by_prompt)[:5]}…)과 씬 계약({sorted(contract_scenes)[:5]}…)이 다르다 — "
            "[5]를 다시 돌려라 (무료다)"
        )

    durations: dict[int, dict[str, float]] = {sid: {} for sid in contract_scenes}
    for lang, timed in timed_by_lang.items():
        scenes = timed.get("scenes", [])
        if len(scenes) != len(contract_scenes):
            raise VideogenStageError(
                f"scenes.timed.{lang}.json의 씬 수({len(scenes)})가 씬 계약({len(contract_scenes)})과 다르다 — "
                "[2l]의 줄 정렬이 깨졌거나 [3]이 다른 대본으로 돌았다"
            )
        for scene in scenes:
            sid = int(scene["scene_id"])
            if sid not in durations:
                raise VideogenStageError(f"scenes.timed.{lang}.json에 계약에 없는 씬 {sid}가 있다")
            durations[sid][lang] = round(float(scene["end"]) - float(scene["start"]), 3)

    jobs: list[SceneJob] = []
    for sid in sorted(contract_scenes):
        scene = contract_scenes[sid]
        entry = by_prompt[sid]
        seconds, clamped = clip_seconds(list(durations[sid].values()), low=low, high=high)
        if clamped == "max":
            longest = max(durations[sid].items(), key=lambda kv: kv[1])
            warnings.append(
                f"씬 {sid}: {longest[0]} {longest[1]:.1f}초 + 꼬리가 {high}초를 넘어 "
                f"{high}초로 만든다 — [9]가 마지막 프레임을 정지로 늘린다"
            )
        info = scene.get("info") or None
        labels = [str(label) for label in (info or {}).get("labels", [])]
        has_info = bool(entry.get("has_info")) or bool(info)
        jobs.append(SceneJob(
            scene_id=sid,
            prompt=fill_seconds(str(entry["prompt"]), seconds),
            negative_prompt=str(entry.get("negative_prompt") or ""),
            has_info=has_info,
            labels=labels,
            seconds=seconds,
            lang_seconds=durations[sid],
            clamped=clamped,
            review_fields={
                "subject": str(scene.get("subject") or ""),
                "subject_anchor": list(scene.get("subject_anchor") or []),
                "visual_goal": str(scene.get("visual_goal") or ""),
                "info": info,
            },
        ))
    return jobs, warnings


def nearest_source(scene_id: int, available: Sequence[int]) -> int | None:
    """인접 재사용의 출처 — 가까운 앞 씬 우선, 없으면 가까운 뒤 씬 (스펙 05 `[7]`)."""
    earlier = [sid for sid in available if sid < scene_id]
    if earlier:
        return max(earlier)
    later = [sid for sid in available if sid > scene_id]
    return min(later) if later else None


# --- 결과 ---------------------------------------------------------------------


@dataclass
class SceneOutcome:
    scene_id: int
    status: str = FAILED
    file: str | None = None
    seconds: int = 0
    lang_seconds: dict[str, float] = field(default_factory=dict)
    clamped: str | None = None
    has_info: bool = False
    attempts: list[dict[str, Any]] = field(default_factory=list)
    demoted_from: str | None = None
    source_scene: int | None = None
    rate_limited: int = 0
    warnings: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def generated(self) -> int:
        return sum(1 for a in self.attempts if a.get("request_id"))

    def clip_record(self, provider: str) -> dict[str, Any]:
        """`clips.json`의 씬 항목 — 생성 기록 (검수 사유는 `clip_review.json`에)."""
        return {
            "scene_id": self.scene_id,
            "status": self.status,
            "file": self.file,
            "provider": provider if self.source_scene is None else "reuse",
            "seconds": self.seconds,
            "lang_seconds": self.lang_seconds,
            "clamped": self.clamped,
            "attempts": self.generated,
            "wall_seconds": round(sum(a.get("wall_seconds", 0.0) for a in self.attempts), 1),
            "retries": max(0, self.generated - 1),
            "rate_limited": self.rate_limited,
            "demoted_from": self.demoted_from,
            "source_scene": self.source_scene,
            "request_ids": [a["request_id"] for a in self.attempts if a.get("request_id")],
        }

    def review_record(self) -> dict[str, Any]:
        """`clip_review.json`의 씬 항목 — 시도마다 OCR·비전 판정과 사유."""
        return {
            "scene_id": self.scene_id,
            "has_info": self.has_info,
            "final": (
                f"demoted_{self.demoted_from}" if self.demoted_from else
                (PASS if self.status == DONE else self.status)
            ),
            "attempts": [
                {k: v for k, v in attempt.items() if k != "wall_seconds"}
                for attempt in self.attempts
            ],
            "reasons": self.reasons,
            "warnings": self.warnings,
        }


@dataclass
class VideogenResult:
    run_id: str
    run_dir: Path
    topic: str
    provider: str
    review: str
    outcomes: list[SceneOutcome] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False
    record_path: Path | None = None
    review_path: Path | None = None

    @property
    def scene_count(self) -> int:
        return len(self.outcomes)

    @property
    def generated_clips(self) -> int:
        return sum(o.generated for o in self.outcomes)

    @property
    def purchased_seconds(self) -> int:
        return sum(o.seconds * o.generated for o in self.outcomes)

    def demoted(self, kind: str) -> int:
        return sum(1 for o in self.outcomes if o.demoted_from == kind)

    @property
    def passed(self) -> bool:
        return bool(self.outcomes) and all(o.status == DONE for o in self.outcomes)

    @property
    def summary(self) -> str:
        tail = " (스킵)" if self.skipped else ""
        return (
            f"[7] {self.topic} — {self.scene_count}씬 / 호출 {self.generated_clips}회 "
            f"({self.purchased_seconds}초, {self.provider}) / 검수 {self.review} / "
            f"강등 info {self.demoted(DEMOTED_INFO)} · video {self.demoted(DEMOTED_VIDEO)} "
            f"→ {CLIPS_DIR}/ + {RECORD_FILE}{tail}"
        )


# --- 실행기 ------------------------------------------------------------------


def _load_json(path: Path, what: str) -> dict[str, Any]:
    if not path.exists():
        raise VideogenStageError(f"{what}이(가) 없다: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise VideogenStageError(f"{what}을(를) 읽을 수 없다: {path} — {exc}") from exc


def render_review_prompt(
    *, topic: str, scene_id: int, fields: dict[str, Any], frames: Sequence[Path],
) -> str:
    """비전 세션 프롬프트. 나레이션 `text`는 넣지 않는다 (ADR-0038 — 그림 목표만 본다)."""
    info = fields.get("info") or None
    if info:
        labels = ", ".join(f'"{label}"' for label in info.get("labels", []))
        info_block = (
            f"- **계측 표시(info)**: `{info.get('annotation', '')}` — **{info.get('target', '')}**를 "
            f"재야 하고, 라벨은 {labels} 이다. 빨강은 이 표시에만 쓰여야 한다"
        )
    else:
        info_block = "- **계측 표시(info)**: 없다 — 이 씬에는 글자·숫자·빨간 표시가 **없어야** 한다"
    anchors = ", ".join(fields.get("subject_anchor") or []) or "(없음)"
    frame_lines = "\n".join(
        f"- `{path}` ({pos})" for pos, path in zip(FRAME_POSITIONS, frames)
    )
    return load_prompt(PROMPT).safe_substitute(
        topic=topic, scene_id=scene_id,
        subject=fields.get("subject") or "(없음)",
        anchors=anchors,
        visual_goal=fields.get("visual_goal") or "(없음)",
        info_block=info_block,
        frames=frame_lines,
    )


def parse_review(payload: dict[str, Any]) -> dict[str, Any]:
    """세션 출력 → `{verdict, reasons, target_pointed}`. 모르는 판정은 `fail`로 본다."""
    verdict = str(payload.get("verdict") or "").strip().lower()
    reasons = payload.get("reasons") or []
    if not isinstance(reasons, list):
        reasons = [str(reasons)]
    reasons = [str(r).strip() for r in reasons if str(r).strip()]
    if verdict not in (PASS, FAIL):
        reasons.append(f"알 수 없는 판정 '{verdict}' → fail로 본다")
        verdict = FAIL
    pointed = payload.get("target_pointed")
    if pointed is not None and not isinstance(pointed, bool):
        pointed = None
    return {"verdict": verdict, "reasons": reasons, "target_pointed": pointed}


class _Runner:
    """씬 작업을 도는 워커의 공유 상태. 단계 함수가 만들고 버린다."""

    def __init__(
        self, *, client: VideoClient, run_dir: Path, topic: str, review: str,
        ocr: OCR | None, llm: LLMClient | None, ffmpeg: str, runner: Callable[..., Any],
        video_timeout: int | None, session_timeout: int, sleep: Callable[[float], None],
        backoff: float, on_scene_done: Callable[[SceneOutcome], None],
    ) -> None:
        self.client = client
        self.run_dir = run_dir
        self.topic = topic
        self.review = review
        self.ocr = ocr
        self.llm = llm
        self.ffmpeg = ffmpeg
        self.runner = runner
        self.video_timeout = video_timeout
        self.session_timeout = session_timeout
        self._sleep = sleep
        self.backoff = backoff
        self.on_scene_done = on_scene_done
        self.stop = threading.Event()
        self.refusal: VideoProviderNotConfigured | None = None
        self._serial = False
        self._serial_lock = threading.Lock()
        self.review_dir = run_dir / REVIEW_DIR
        self.clips_dir = run_dir / CLIPS_DIR

    # --- 생성 -------------------------------------------------------------

    def _generate(self, job: SceneJob, attempt: dict[str, Any], prompt: str, negative: str) -> Path | None:
        """호출 1회 (429면 직렬 + 백오프로 재시도) → 원본 파일 경로. 실패면 None + 사유."""
        request = VideoRequest(
            scene_id=job.scene_id, prompt=prompt, negative_prompt=negative,
            seconds=job.seconds, label=f"scene {job.scene_id} attempt {attempt['attempt']}",
        )
        for retry in range(RATE_LIMIT_RETRIES + 1):
            if self.stop.is_set():
                attempt["error"] = "프로바이더 거절로 중단됐다"
                return None
            started = time.monotonic()
            lock = self._serial_lock if self._serial else None
            if lock:
                lock.acquire()
            try:
                clip = self.client.generate(request, timeout=self.video_timeout)
            except VideoGenRateLimited as exc:
                attempt["rate_limited"] = attempt.get("rate_limited", 0) + 1
                self._serial = True  # 스펙 05 — 429면 워커 1로 줄이고 백오프
                wait = self.backoff * (2 ** retry)
                log.warning("[%s] 씬 %d: 429 → 직렬 + %.0f초 대기 (%s)", STAGE, job.scene_id, wait, exc)
                attempt["wall_seconds"] = attempt.get("wall_seconds", 0.0) + (time.monotonic() - started)
                if retry == RATE_LIMIT_RETRIES:
                    attempt["error"] = f"429가 {RATE_LIMIT_RETRIES + 1}회 이어졌다: {exc}"
                    return None
                self._sleep(wait)
                continue
            except VideoProviderNotConfigured as exc:
                self.refusal = exc
                self.stop.set()
                attempt["error"] = f"프로바이더 거절: {exc}"
                return None
            except VideoGenError as exc:
                attempt["error"] = str(exc)
                attempt["wall_seconds"] = attempt.get("wall_seconds", 0.0) + (time.monotonic() - started)
                return None
            finally:
                if lock:
                    lock.release()

            attempt["wall_seconds"] = attempt.get("wall_seconds", 0.0) + (time.monotonic() - started)
            attempt["request_id"] = clip.request_id or f"{self.client.name}-{job.scene_id}-{attempt['attempt']}"
            attempt["provider"] = {**clip.meta, **{k: v for k, v in clip.raw.items() if k != "uri"}}
            raw_path = self.review_dir / f"{job.scene_id}-{attempt['attempt']}.raw.mp4"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(clip.data)
            return raw_path
        return None  # pragma: no cover

    def _normalize(self, job: SceneJob, attempt: dict[str, Any], raw_path: Path) -> Path | None:
        candidate = self.review_dir / f"{job.scene_id}-{attempt['attempt']}.mp4"
        cmd = normalize_command(
            relative_path(raw_path, self.run_dir), relative_path(candidate, self.run_dir),
            seconds=job.seconds, executable=self.ffmpeg,
        )
        try:
            run_ffmpeg(cmd, cwd=self.run_dir, produces=candidate, runner=self.runner)
        except FFmpegError as exc:
            attempt["error"] = f"정규화 실패: {exc}"
            return None
        return candidate

    def _frames(self, job: SceneJob, attempt: dict[str, Any], candidate: Path) -> list[Path] | None:
        frames: list[Path] = []
        for pos, at in frame_times(job.seconds).items():
            target = self.review_dir / f"{job.scene_id}-{attempt['attempt']}-{pos}{FRAME_SUFFIX}"
            cmd = frame_command(
                relative_path(candidate, self.run_dir), relative_path(target, self.run_dir),
                at=at, executable=self.ffmpeg,
            )
            try:
                run_ffmpeg(cmd, cwd=self.run_dir, produces=target, runner=self.runner)
            except FFmpegError as exc:
                attempt["error"] = f"프레임 추출 실패 ({pos}): {exc}"
                return None
            frames.append(target)
        return frames

    # --- 검수 -------------------------------------------------------------

    def _ocr(self, job: SceneJob, expect_labels: bool, end_frame: Path) -> dict[str, Any] | None:
        if self.review == REVIEW_NONE or self.ocr is None:
            return None
        try:
            text = self.ocr.read(end_frame)
        except OCRError as exc:
            return {"passed": True, "skipped": True, "reasons": [f"OCR 실행 실패 → 게이트 생략: {exc}"]}
        verdict = ocr_gate(text, job.labels if expect_labels else None)
        return verdict.record()

    def _vision(
        self, scene_id: int, fields: dict[str, Any], frames: Sequence[Path]
    ) -> dict[str, Any] | None:
        if self.review != REVIEW_FULL or self.llm is None:
            return None
        prompt = render_review_prompt(
            topic=self.topic, scene_id=scene_id, fields=fields, frames=frames,
        )
        last_error = ""
        for _try in range(2):
            try:
                # `ask_json`이 아니라 직접 부른다 — 프레임 디렉터리를 `add_dirs`로 열어 줘야
                # 세션이 Read로 그림을 본다 (`[8]`과 같은 메커니즘, ADR-0012의 add_dirs).
                result = self.llm.run(
                    prompt, allowed_tools=TOOLS, timeout=self.session_timeout,
                    label=f"{STAGE}:{scene_id}", add_dirs=(self.review_dir,),
                )
                payload = extract_json_object(result.text)
            except (JSONExtractionError, LLMError) as exc:
                last_error = str(exc)
                continue
            verdict = parse_review(payload)
            verdict["session"] = result.meta
            return verdict
        # 세션이 두 번 죽었다 — 돈을 더 쓰는 대신 경고로 남기고 통과시킨다 (검수 불능은 클립의 잘못이 아니다).
        return {"verdict": ERROR, "reasons": [f"비전 세션 실패 2회 → 검수 없이 통과: {last_error}"], "target_pointed": None}

    def _review_attempt(self, job: SceneJob, attempt: dict[str, Any], variant: str, candidate: Path) -> bool:
        """검수 ①②. 통과면 True. 기록은 `attempt`에 남긴다."""
        if self.review == REVIEW_NONE:
            attempt["passed"] = True
            return True
        frames = self._frames(job, attempt, candidate)
        if frames is None:
            attempt["passed"] = False
            return False
        attempt["frames"] = [relative_path(f, self.run_dir) for f in frames]

        expect_labels = variant == VARIANT_INFO
        ocr = self._ocr(job, expect_labels, frames[-1])
        attempt["ocr"] = ocr
        if ocr is not None and not ocr.get("passed", True):
            attempt["passed"] = False
            return False

        # RED 절을 뺀 변종은 계측 표시가 없는 씬으로 본다 — 라벨을 기대하지 않는다.
        fields = {**job.review_fields, "info": None} if variant == VARIANT_NO_RED else job.review_fields
        vision = self._vision(job.scene_id, fields, frames)
        attempt["vision"] = vision
        if vision is not None and vision["verdict"] == FAIL:
            attempt["passed"] = False
            return False
        if vision is not None and vision["verdict"] == ERROR:
            attempt.setdefault("warnings", []).extend(vision["reasons"])
        attempt["passed"] = True
        return True

    # --- 사다리 -----------------------------------------------------------

    def run_scene(self, job: SceneJob) -> SceneOutcome:
        outcome = SceneOutcome(
            scene_id=job.scene_id, seconds=job.seconds, lang_seconds=job.lang_seconds,
            clamped=job.clamped, has_info=job.has_info,
        )
        variants: list[tuple[str, str, str]] = [
            (VARIANT_INFO if job.has_info else VARIANT_VIDEO, job.prompt, job.negative_prompt)
        ]
        if job.has_info:
            try:
                variants.append((VARIANT_NO_RED, *demote_info(job.prompt, job.negative_prompt)))
            except ValueError as exc:
                outcome.warnings.append(f"RED 절 강등 변종을 만들 수 없다: {exc}")

        number = 0
        for variant, prompt, negative in variants:
            if variant == VARIANT_NO_RED:
                outcome.warnings.append(
                    "검수 2회 실패 → RED 절을 뺀 프롬프트로 재생성한다 (demoted_from: info — 틀린 숫자보다 없는 숫자가 낫다)"
                )
            for _ in range(ATTEMPTS_PER_PROMPT):
                if self.stop.is_set():
                    outcome.status = FAILED
                    outcome.reasons.append("프로바이더 거절로 중단")
                    self._finish(outcome)
                    return outcome
                number += 1
                attempt: dict[str, Any] = {"attempt": number, "variant": variant}
                outcome.attempts.append(attempt)
                raw = self._generate(job, attempt, prompt, negative)
                outcome.rate_limited += attempt.get("rate_limited", 0)
                if raw is None:
                    attempt["passed"] = False
                    if self.stop.is_set():
                        outcome.status = FAILED
                        outcome.reasons.append(attempt.get("error") or "프로바이더 거절")
                        self._finish(outcome)
                        return outcome
                    outcome.reasons.append(f"시도 {number}: {attempt.get('error')}")
                    continue
                candidate = self._normalize(job, attempt, raw)
                if candidate is None:
                    attempt["passed"] = False
                    outcome.reasons.append(f"시도 {number}: {attempt.get('error')}")
                    continue
                if self._review_attempt(job, attempt, variant, candidate):
                    target = self.clips_dir / f"{job.scene_id}.mp4"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(candidate, target)
                    outcome.file = relative_path(target, self.run_dir)
                    outcome.status = DONE
                    if variant == VARIANT_NO_RED:
                        outcome.demoted_from = DEMOTED_INFO
                    for warning in attempt.get("warnings", []):
                        outcome.warnings.append(f"시도 {number}: {warning}")
                    self._finish(outcome)
                    return outcome
                reasons = []
                if attempt.get("ocr") and not attempt["ocr"].get("passed", True):
                    reasons.extend(attempt["ocr"].get("reasons", []))
                if attempt.get("vision") and attempt["vision"].get("verdict") == FAIL:
                    reasons.extend(attempt["vision"].get("reasons", []))
                if attempt.get("error"):
                    reasons.append(attempt["error"])
                outcome.reasons.append(f"시도 {number} ({variant}): " + ("; ".join(reasons) or "검수 실패"))

        outcome.status = NEEDS_REUSE
        outcome.warnings.append("생성·검수가 전부 실패해 인접 씬 클립을 재사용한다 (demoted_from: video)")
        self._finish(outcome)
        return outcome

    def _finish(self, outcome: SceneOutcome) -> None:
        self.on_scene_done(outcome)


# --- 단계 ---------------------------------------------------------------------


def _existing_done(run_dir: Path, force: bool) -> dict[int, dict[str, Any]]:
    """지난 실행의 `clips.json`에서 `done`이고 파일이 남아 있는 씬 — 다시 사지 않는다."""
    if force:
        return {}
    path = run_dir / RECORD_FILE
    if not path.exists():
        return {}
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    done: dict[int, dict[str, Any]] = {}
    for entry in previous.get("scenes", []):
        if entry.get("status") == DONE and entry.get("file") and (run_dir / entry["file"]).exists():
            done[int(entry["scene_id"])] = entry
    return done


def _existing_review(run_dir: Path, force: bool) -> dict[int, dict[str, Any]]:
    if force:
        return {}
    path = run_dir / REVIEW_FILE
    if not path.exists():
        return {}
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {int(e["scene_id"]): e for e in previous.get("scenes", []) if "scene_id" in e}


def run_videogen_stage(
    run_id: str,
    *,
    client: VideoClient,
    paths: Paths | None = None,
    llm: LLMClient | None = None,
    review: str = REVIEW_FULL,
    ocr: OCR | None = None,
    detect: Callable[[], OCR | None] = detect_ocr,
    force: bool = False,
    jobs: int | None = None,
    video_timeout: int | None = None,
    session_timeout: int = SESSION_TIMEOUT,
    ffmpeg: str = DEFAULT_FFMPEG,
    runner: Callable[..., Any] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    backoff: float = RATE_LIMIT_BACKOFF,
) -> VideogenResult:
    if review not in REVIEW_MODES:
        raise VideogenStageError(f"review는 {'|'.join(REVIEW_MODES)} 중 하나다: {review!r}")
    if review == REVIEW_FULL and llm is None:
        raise VideogenStageError("--review full에는 비전 세션 클라이언트가 필요하다 (ocr·none은 없어도 된다)")

    paths = paths or Paths.from_env()
    run_dir = paths.run_dir(run_id)

    try:
        contract = find_contract_for_run(paths, run_id)
    except SceneContractNotFound as exc:
        raise VideogenStageError(str(exc)) from exc
    if contract.get("run_id") != run_id:
        raise VideogenStageError(
            f"scenes.json의 run_id({contract.get('run_id')})가 대상 run({run_id})과 다르다"
        )
    topic = str(contract.get("topic") or run_id)
    prompts = _load_json(run_dir / PROMPTS_FILE, f"[5]의 산출물({PROMPTS_FILE})")
    if prompts.get("run_id") != run_id:
        raise VideogenStageError(
            f"{PROMPTS_FILE}의 run_id({prompts.get('run_id')})가 대상 run({run_id})과 다르다"
        )

    langs = present_languages(run_dir)
    if PRIMARY_LANGUAGE not in langs:
        raise VideogenStageError(
            f"scenes.timed.{PRIMARY_LANGUAGE}.json이 없다: {run_dir}. [3. tts+sync]를 먼저 실행하라 — "
            "ko는 필수이고 ja·en은 있는 것만 본다 (D-3)"
        )
    try:
        timed_by_lang = {lang: load_timed_scenes(run_dir, lang) for lang in langs}
    except TimedScenesNotFound as exc:
        raise VideogenStageError(str(exc)) from exc
    for lang, timed in timed_by_lang.items():
        if timed.get("run_id") != run_id:
            raise VideogenStageError(
                f"scenes.timed.{lang}.json의 run_id({timed.get('run_id')})가 대상 run({run_id})과 다르다"
            )

    state = RunState.load_or_create(run_dir, run_id, topic=topic)
    record_path = run_dir / RECORD_FILE
    review_path = run_dir / REVIEW_FILE

    all_jobs, warnings = build_jobs(
        contract, prompts, timed_by_lang, seconds_range=(client.min_seconds, client.max_seconds),
    )
    done_before = _existing_done(run_dir, force)
    review_before = _existing_review(run_dir, force)

    if state.is_done(STAGE) and not force and record_path.exists() and len(done_before) == len(all_jobs):
        log.info("[%s] 이미 완료된 단계라 스킵한다 (run_id=%s)", STAGE, run_id)
        previous = json.loads(record_path.read_text(encoding="utf-8"))
        outcomes = []
        for entry in previous.get("scenes", []):
            outcomes.append(SceneOutcome(
                scene_id=int(entry["scene_id"]), status=entry.get("status", DONE),
                file=entry.get("file"), seconds=int(entry.get("seconds") or 0),
                lang_seconds=entry.get("lang_seconds") or {}, clamped=entry.get("clamped"),
                demoted_from=entry.get("demoted_from"), source_scene=entry.get("source_scene"),
                attempts=[{"request_id": rid} for rid in entry.get("request_ids", [])],
            ))
        return VideogenResult(
            run_id=run_id, run_dir=run_dir, topic=topic, provider=str(previous.get("provider") or client.name),
            review=str(previous.get("review") or review), outcomes=outcomes,
            warnings=previous.get("warnings", []), skipped=True,
            record_path=record_path, review_path=review_path,
        )

    state.mark_running(STAGE)

    ocr_backend = ocr if ocr is not None else (detect() if review != REVIEW_NONE else None)
    if review != REVIEW_NONE and ocr_backend is None:
        warnings.append(
            "tesseract가 PATH에 없어 끝 프레임 OCR 게이트를 건너뛴다 — 라벨 대조는 비전 검수만 본다 (ADR-0056 결정 6)"
        )

    pending = [job for job in all_jobs if job.scene_id not in done_before]
    if done_before:
        log.info("[%s] 지난 실행의 done 씬 %d개는 다시 사지 않는다", STAGE, len(done_before))

    outcomes: dict[int, SceneOutcome] = {}
    for sid, entry in done_before.items():
        outcomes[sid] = SceneOutcome(
            scene_id=sid, status=DONE, file=entry.get("file"), seconds=int(entry.get("seconds") or 0),
            lang_seconds=entry.get("lang_seconds") or {}, clamped=entry.get("clamped"),
            has_info=bool(review_before.get(sid, {}).get("has_info")),
            demoted_from=entry.get("demoted_from"), source_scene=entry.get("source_scene"),
            attempts=[
                {"request_id": rid, "attempt": i + 1, "variant": "previous", "passed": True}
                for i, rid in enumerate(entry.get("request_ids", []))
            ],
        )
    write_lock = threading.Lock()

    def write_records(extra_warnings: Sequence[str] = ()) -> None:
        ordered = [outcomes[sid] for sid in sorted(outcomes)]
        record = {
            "run_id": run_id,
            "topic": topic,
            "provider": client.name,
            "model": getattr(client, "model_id", None),
            "review": review,
            "ocr_backend": getattr(ocr_backend, "name", None),
            "languages": langs,
            "scenes": [o.clip_record(client.name) for o in ordered],
            "warnings": list(warnings) + list(extra_warnings),
        }
        review_doc = {
            "run_id": run_id,
            "topic": topic,
            "review": review,
            "ocr_backend": getattr(ocr_backend, "name", None),
            "scenes": [
                review_before[o.scene_id] if (o.scene_id in done_before and o.scene_id in review_before)
                else o.review_record()
                for o in ordered
            ],
            "warnings": list(warnings) + list(extra_warnings),
        }
        write_text(record_path, dump_json(record))
        write_text(review_path, dump_json(review_doc))

    def on_scene_done(outcome: SceneOutcome) -> None:
        with write_lock:
            outcomes[outcome.scene_id] = outcome
            write_records()

    runner_state = _Runner(
        client=client, run_dir=run_dir, topic=topic, review=review, ocr=ocr_backend, llm=llm,
        ffmpeg=ffmpeg, runner=runner, video_timeout=video_timeout, session_timeout=session_timeout,
        sleep=sleep, backoff=backoff, on_scene_done=on_scene_done,
    )
    (run_dir / REVIEW_DIR).mkdir(parents=True, exist_ok=True)
    (run_dir / CLIPS_DIR).mkdir(parents=True, exist_ok=True)
    write_records()

    workers = max(1, int(jobs)) if jobs else max(1, int(client.concurrency() or 1))
    log.info(
        "[%s] %d씬 제출 (워커 %d, 검수 %s, OCR %s) — 지난 done %d", STAGE, len(pending), workers,
        review, getattr(ocr_backend, "name", "없음"), len(done_before),
    )
    if pending:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(runner_state.run_scene, pending))

    # --- 인접 재사용은 전 씬이 끝난 뒤 직렬로 (앞 씬이 생성 중일 수 있다) ---
    reuse_warnings: list[str] = []
    real = sorted(sid for sid, o in outcomes.items() if o.status == DONE and o.source_scene is None)
    for sid in sorted(outcomes):
        outcome = outcomes[sid]
        if outcome.status != NEEDS_REUSE:
            continue
        source = nearest_source(sid, real)
        if source is None:
            outcome.status = FAILED
            outcome.reasons.append("재사용할 클립이 한 편에도 없다")
            continue
        target = run_dir / CLIPS_DIR / f"{sid}.mp4"
        shutil.copyfile(run_dir / outcomes[source].file, target)
        outcome.file = relative_path(target, run_dir)
        outcome.status = DONE
        outcome.demoted_from = DEMOTED_VIDEO
        outcome.source_scene = source
        reuse_warnings.append(f"씬 {sid}: 씬 {source}의 클립을 재사용했다 (demoted_from: video)")

    reuse_counts: dict[int, int] = {}
    for outcome in outcomes.values():
        if outcome.source_scene is not None:
            reuse_counts[outcome.source_scene] = reuse_counts.get(outcome.source_scene, 0) + 1
    for source, count in sorted(reuse_counts.items()):
        if count + 1 >= 3:
            reuse_warnings.append(
                f"씬 {source}의 클립이 {count + 1}회 나온다 — 영상 생성이 실패한 편이다 (스펙 03 「카메라」)"
            )
    with write_lock:
        write_records(reuse_warnings)
    warnings.extend(reuse_warnings)

    result = VideogenResult(
        run_id=run_id, run_dir=run_dir, topic=topic, provider=client.name, review=review,
        outcomes=[outcomes[sid] for sid in sorted(outcomes)], warnings=warnings,
        record_path=record_path, review_path=review_path,
    )
    for outcome in result.outcomes:
        for warning in outcome.warnings:
            log.warning("[%s] 씬 %d: %s", STAGE, outcome.scene_id, warning)
    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)

    info = {
        "scene_count": result.scene_count,
        "generated_clips": result.generated_clips,
        "purchased_seconds": result.purchased_seconds,
        "reused_from_previous_run": len(done_before),
        "demoted_info": result.demoted(DEMOTED_INFO),
        "demoted_video": result.demoted(DEMOTED_VIDEO),
        "rate_limited": sum(o.rate_limited for o in result.outcomes),
        "review": review,
        "ocr_backend": getattr(ocr_backend, "name", None),
        "provider": client.name,
        "warnings": warnings,
        "outputs": [
            p.relative_to(paths.root).as_posix() for p in (record_path, review_path)
        ],
    }

    if runner_state.refusal is not None:
        message = (
            f"프로바이더가 거절해 남은 씬을 시도하지 않았다 (D-5): {runner_state.refusal}. "
            f"산 클립 {result.generated_clips}개와 기록은 남아 있다 — 고치고 다시 돌리면 done 씬은 건너뛴다"
        )
        state.mark_failed(STAGE, message, **info)
        raise ProviderRefused(message)

    failed = [o.scene_id for o in result.outcomes if o.status != DONE]
    if failed:
        message = f"클립을 만들지 못한 씬이 있다: {failed[:8]}{' …' if len(failed) > 8 else ''}"
        state.mark_failed(STAGE, message, **info)
        raise VideogenStageError(message)

    state.mark_done(STAGE, **info)
    return result
