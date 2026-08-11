"""[7. motion] — 베이스 이미지 + 씬 계약 → 씬마다 클립 하나.

specs/05-pipeline.md:
    [7. motion] → clips/{scene_id}.mp4

    "`motion` 필드 분기 (ADR-0006). `kenburns` → FFmpeg zoompan(camera 값 적용,
     파라미터는 스펙 03의 '카메라 워크 파라미터' 표). `kling` → v2.6 Turbo급 i2v
     5초 무음, 2회 실패 시 kenburns 강등. 클립 길이 = 씬 길이 + 디졸브 겹침 0.6초.
     클립의 로컬 0초가 씬의 `start`이고 0.6초는 전부 꼬리다 (ADR-0024)."

## 입력 (ADR-0020)

| 파일 | 무엇을 읽는가 |
|---|---|
| `runs/{run_id}/scenes.timed.json` | `scene_id`·`beat`·`start`·`end`·`camera`·`motion` |
| `runs/{run_id}/images/{scene_id}.*` | `[6]`의 베이스 이미지. 확장자는 프로바이더가 정한다 (ADR-0021) |

**`images.json`을 읽지 않는다** (ADR-0024). `[6]`이 폴백으로 같은 그림을 두 씬에 썼다는
사실은 기록이 아니라 **파일 내용**에서 안다 — 바이트 해시가 같으면 2회차부터 스펙 03의
역방향 워크를 쓴다. 이렇게 두면 `images.json`은 계약이 아니라 기록으로 남고
(ADR-0020), `[6]` 폴백이 아닌 경로로 생긴 중복도 같이 잡힌다.

`prompts.json`도 열지 않는다. 오버레이는 `[8]` 소관이고(ADR-0019) 프롬프트는 이미
그림이 됐다.

## 클립 길이는 여기서 계산하지 않는다

`video/timeline.py`의 `build_timeline`이 씬 계약에서 만든 `clip_length`를 그대로 쓴다.
`[9]`가 조립할 때 쓰는 것과 **같은 함수의 같은 값**이다 — 두 단계가 각자 계산하면
언젠가 갈라지고, 갈라진 쪽만 싱크가 어긋난다.

## 강등 사다리

`kling` → `kenburns` → `static`. 아래로만 내려간다.

- **`kling`은 아직 구현되지 않았다.** 키 출처가 확인되지 않아 호출 경로를 모른다.
  그 씬은 `kenburns`로 강등하고 기록·경고에 남긴다. 조용히 처리하면 i2v를 만들었다고
  착각하게 된다
- `kenburns` 렌더가 실패하면 `static`으로 한 번 더 내려간다. specs/05 실패 정책
  ("씬 단위 실패는 폴백 처리하고 파이프라인은 계속 진행")에서 파생한 규칙이다 —
  스펙에 적힌 문장은 아니다. 아니라고 보면 `FALLBACK_CAMERA`를 지우면 된다

## 이어받기

Ken Burns는 과금이 없지만 27씬이 2분쯤 걸린다. 씬별로 (이미지 해시 + 적용 카메라 +
프레임 수)를 기록해 두고 그대로면 건너뛴다. `[6]`의 `digest`와 같은 장치다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Paths, write_text
from ..jsonio import dump_json
from ..runstate import RunState
from ..schemas.timed_scenes import validate_timed_scenes
from ..video.ffmpeg import DEFAULT_FFMPEG, FFmpegError, relative_path, run_ffmpeg
from ..video.kenburns import (
    CLIP_CRF,
    TRAVEL,
    KenBurnsError,
    build_command,
    frame_count,
    resolve_camera,
)
from ..video.timeline import Segment, TimelineError, build_timeline

log = logging.getLogger(__name__)

STAGE = "7-motion"

#: 1부↔2부 경계면 파일 (ADR-0017). `--slug`로 run을 찾을 때만 연다.
SCRIPT_FILE = "06-script.json"

TIMED_SCENES_FILE = "scenes.timed.json"
IMAGES_DIR = "images"

#: 산출물 — specs/05가 못박은 경로. `[8]`은 여기를 덮어쓰지 않는다 (ADR-0024)
CLIPS_DIR = "clips"

#: 이 단계의 **실행 기록**. `images.json`이 `[6]`에 대해 갖는 역할과 같다 (ADR-0020).
RECORD_FILE = "clips.json"

#: 클립 하나 렌더 타임아웃(초). 실측 3.9초/클립이라 넉넉하다.
TIMEOUT = 300

#: 렌더가 실패했을 때 마지막으로 내려가는 자리 (위 docstring "강등 사다리").
FALLBACK_CAMERA = "static"

KENBURNS = "kenburns"
KLING = "kling"

#: 씬 하나의 결과 상태.
RENDERED = "rendered"
CACHED = "cached"
FAILED = "failed"

REUSABLE = (RENDERED, CACHED)

#: 같은 그림이 몇 번 나오면 카메라로 가릴 문제가 아닌가 (스펙 03 역방향 워크).
DUPLICATE_ALARM = 3


class MotionStageError(Exception):
    pass


@dataclass
class MotionResult:
    run_id: str
    topic: str
    run_dir: Path
    clips_dir: Path
    record_path: Path | None = None
    scenes: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False

    def count(self, status: str) -> int:
        return sum(1 for s in self.scenes if s["status"] == status)

    @property
    def scene_count(self) -> int:
        return len(self.scenes)

    @property
    def demoted(self) -> int:
        """`kling`을 요청받았지만 `kenburns`로 내려간 씬."""
        return sum(1 for s in self.scenes if s.get("demoted_from"))

    @property
    def reversed_count(self) -> int:
        return sum(1 for s in self.scenes if s.get("camera_reversed"))

    @property
    def passed(self) -> bool:
        return bool(self.scenes) and self.count(FAILED) == 0

    @property
    def summary(self) -> str:
        tail = " (스킵)" if self.skipped else ""
        parts = [
            f"렌더 {self.count(RENDERED)}",
            f"이어받기 {self.count(CACHED)}",
            f"실패 {self.count(FAILED)}",
        ]
        extra = ""
        if self.demoted:
            extra += f" / kling→kenburns 강등 {self.demoted}"
        if self.reversed_count:
            extra += f" / 역방향 워크 {self.reversed_count}"
        return (
            f"[7] {self.topic} — {self.scene_count}씬 ({' · '.join(parts)})"
            f"{extra} → {CLIPS_DIR}/{tail}"
        )


def _load_json(path: Path, what: str) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MotionStageError(f"{what}을(를) 읽을 수 없다: {path} — {exc}") from exc


def resolve_run_id(
    paths: Paths, *, run_id: str | None = None, slug: str | None = None
) -> str:
    """`--run-id`를 그대로 쓰거나, `--slug`면 경계면 파일에서 `run_id`를 읽는다 (ADR-0017)."""
    if run_id:
        return run_id
    if not slug:
        raise MotionStageError("run_id 또는 slug 중 하나는 있어야 한다")

    script_path = paths.topic_dir(slug) / SCRIPT_FILE
    if not script_path.exists():
        raise MotionStageError(
            f"대본이 없다: {script_path}. --run-id로 run을 직접 지정할 수도 있다."
        )
    resolved = _load_json(script_path, f"씬 계약({SCRIPT_FILE})").get("run_id")
    if not resolved:
        raise MotionStageError(f"{script_path}에 run_id가 없다")
    return resolved


def _load_timed_scenes(path: Path) -> tuple[dict[str, Any], list[str]]:
    if not path.exists():
        raise MotionStageError(
            f"{TIMED_SCENES_FILE}이 없다: {path}. [3. tts+sync]를 먼저 실행하라 — "
            "총 길이가 102초를 넘어 멈춘 run에는 이 파일이 일부러 없다 (ADR-0017)."
        )
    data = _load_json(path, TIMED_SCENES_FILE)
    errors, warnings = validate_timed_scenes(data)
    if errors:
        listed = "\n".join(f"  - {e}" for e in errors[:5])
        raise MotionStageError(f"{path}이(가) 씬 계약을 위반한다:\n{listed}")
    return data, warnings


def find_image(images_dir: Path, scene_id: int) -> Path:
    """`images/{scene_id}.*` 한 장. 확장자는 프로바이더가 정한다 (ADR-0021).

    같은 씬에 확장자만 다른 파일이 둘 있으면 어느 것이 최신인지 알 수 없다. 조용히
    하나를 고르면 지난 프로바이더의 그림으로 영상을 만들게 되므로 멈춘다.
    """
    found = sorted(p for p in images_dir.glob(f"{scene_id}.*") if p.is_file())
    if not found:
        raise FileNotFoundError(f"{IMAGES_DIR}/{scene_id}.* 가 없다")
    if len(found) > 1:
        raise MotionStageError(
            f"씬 {scene_id}의 이미지가 {len(found)}장이다 "
            f"({', '.join(p.name for p in found)}). 씬 하나에 그림 하나다 — "
            f"쓰지 않는 확장자를 지워라"
        )
    return found[0]


def _collect_images(images_dir: Path, scenes: list[dict[str, Any]]) -> dict[int, Path]:
    """전 씬의 이미지를 먼저 모은다. 하나라도 없으면 렌더를 시작하기 전에 멈춘다.

    27씬 중 20개를 만든 뒤 21번에서 그림이 없다는 것을 아는 것보다, 2분을 쓰기 전에
    아는 편이 낫다. `[6]`이 폴백까지 실패했다는 뜻이기도 하다.
    """
    if not images_dir.is_dir():
        raise MotionStageError(
            f"{images_dir}가 없다. [6. imagegen]을 먼저 실행하라 (specs/05)."
        )
    found: dict[int, Path] = {}
    missing: list[int] = []
    for scene in scenes:
        try:
            found[scene["scene_id"]] = find_image(images_dir, scene["scene_id"])
        except FileNotFoundError:
            missing.append(scene["scene_id"])
    if missing:
        listed = ", ".join(str(i) for i in missing[:8])
        raise MotionStageError(
            f"이미지가 없는 씬이 {len(missing)}개다 ({listed}"
            f"{' …' if len(missing) > 8 else ''}). "
            f"[6. imagegen]이 {images_dir}에 씬마다 한 장씩 놓는다 (specs/05)."
        )
    return found


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _plan_scene(
    scene: dict[str, Any],
    segment: Segment,
    image: Path,
    *,
    seen: dict[str, int],
    warnings: list[str],
) -> dict[str, Any]:
    """씬 하나를 어떻게 그릴지 정한다. 렌더는 하지 않는다 (검사 가능한 순수 판단).

    `seen`은 이미지 해시 → 지금까지 나온 횟수다. 씬 순서대로 채워지므로 앞 씬이
    원본 워크를 갖고 뒤 씬이 역방향을 받는다.
    """
    scene_id = scene["scene_id"]
    image_digest = _digest(image.read_bytes())
    occurrence = seen.get(image_digest, 0) + 1
    seen[image_digest] = occurrence

    camera = scene["camera"]
    used_camera = resolve_camera(camera, occurrence)
    if occurrence >= DUPLICATE_ALARM:
        warnings.append(
            f"씬 {scene_id}: 같은 그림이 {occurrence}번째다 ({image.name}). "
            "카메라 워크로 가릴 문제가 아니라 [6]이 그 씬들을 못 만든 것이다"
        )
    elif occurrence > 1:
        warnings.append(
            f"씬 {scene_id}: 앞 씬과 같은 그림이라 카메라를 "
            f"{camera} → {used_camera}로 뒤집었다 (스펙 03 역방향 워크)"
        )

    motion = scene["motion"]
    demoted_from = None
    if motion == KLING:
        # 강등을 조용히 하지 않는다 (위 docstring "강등 사다리").
        demoted_from = KLING
        warnings.append(
            f"씬 {scene_id}: motion=kling인데 i2v 경로가 아직 없다 → kenburns로 강등"
        )

    frames = frame_count(segment.clip_length)
    return {
        "scene_id": scene_id,
        "beat": scene["beat"],
        "motion": motion,
        "motion_used": KENBURNS,
        "demoted_from": demoted_from,
        "camera": camera,
        "camera_used": used_camera,
        "camera_reversed": used_camera != camera,
        "image": f"{IMAGES_DIR}/{image.name}",
        "image_digest": image_digest,
        "occurrence": occurrence,
        "clip_length": segment.clip_length,
        "frames": frames,
        "file": f"{CLIPS_DIR}/{segment.clip_name}",
    }


def _plan_digest(plan: dict[str, Any]) -> str:
    """이어받기 판정용 지문. 그림·카메라·프레임 수가 그대로면 같은 클립이 나온다."""
    return _digest(
        f"{plan['image_digest']}|{plan['camera_used']}|{plan['frames']}|{CLIP_CRF}".encode()
    )


def _render_scene(
    plan: dict[str, Any],
    *,
    image: Path,
    clip_path: Path,
    run_dir: Path,
    ffmpeg: str,
    runner: Any,
    timeout: int,
) -> dict[str, Any]:
    """클립 하나. 실패하면 `static`으로 한 번 더 내려간다 (강등 사다리).

    계약된 워크가 이미 `static`이면 사다리에 아래가 없다 — 같은 명령을 두 번 돌리지
    않는다.
    """
    ladder = [plan["camera_used"]]
    if FALLBACK_CAMERA not in ladder:
        ladder.append(FALLBACK_CAMERA)

    errors: list[str] = []
    for attempts, camera in enumerate(ladder, start=1):
        cmd = build_command(
            relative_path(image, run_dir),
            relative_path(clip_path, run_dir),
            camera=camera,
            frames=plan["frames"],
            executable=ffmpeg,
        )
        try:
            run_ffmpeg(
                cmd, cwd=run_dir, produces=clip_path, runner=runner, timeout=timeout
            )
        except (FFmpegError, KenBurnsError) as exc:
            errors.append(f"{camera}: {exc}")
            log.warning(
                "[%s] 씬 %s 렌더 실패 (%s): %s", STAGE, plan["scene_id"], camera, exc
            )
            continue

        rendered = {**plan, "camera_used": camera}
        return {
            **rendered,
            "status": RENDERED,
            #: 역방향 워크가 걸렸는가 (스펙 03)와 사다리를 내려갔는가는 다른 일이다.
            "camera_fallback": camera != plan["camera_used"],
            "attempts": attempts,
            "errors": errors,
            "digest": _plan_digest(rendered),
        }

    return {
        **plan,
        "status": FAILED,
        "camera_fallback": False,
        "attempts": len(ladder),
        "errors": errors,
        "digest": None,
    }


def _load_previous(record_path: Path) -> dict[int, dict[str, Any]]:
    if not record_path.exists():
        return {}
    try:
        data = json.loads(record_path.read_text(encoding="utf-8"))
        return {int(entry["scene_id"]): entry for entry in data.get("scenes", [])}
    except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError):
        log.warning("[%s] %s를 읽을 수 없어 이어받기 없이 진행한다", STAGE, record_path)
        return {}


def _record_document(
    document: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    warnings: list[str],
) -> dict[str, Any]:
    """clips.json — `[7]`의 실행 기록. 계약이 아니라 기록이라 스키마로 조이지 않는다."""
    return {
        "run_id": document["run_id"],
        "topic": document["topic"],
        "source_scenes": TIMED_SCENES_FILE,
        "kenburns": {"travel": TRAVEL, "crf": CLIP_CRF},
        "scenes": records,
        "warnings": warnings,
    }


def run_motion_stage(
    run_id: str,
    *,
    paths: Paths | None = None,
    force: bool = False,
    ffmpeg: str = DEFAULT_FFMPEG,
    runner: Any = subprocess.run,
    timeout: int = TIMEOUT,
) -> MotionResult:
    paths = paths or Paths.from_env()
    run_dir = paths.run_dir(run_id)

    document, scene_warnings = _load_timed_scenes(run_dir / TIMED_SCENES_FILE)
    if document["run_id"] != run_id:
        # 계보는 run_id로 잇는다 (ADR-0017). 다른 대본의 시각으로 클립을 자르면
        # 길이가 어긋난 클립이 조용히 나온다.
        raise MotionStageError(
            f"{TIMED_SCENES_FILE}의 run_id({document['run_id']})가 "
            f"대상 run({run_id})과 다르다"
        )

    scenes: list[dict[str, Any]] = document["scenes"]
    topic = document["topic"]
    clips_dir = run_dir / CLIPS_DIR
    record_path = run_dir / RECORD_FILE

    state = RunState.load_or_create(run_dir, run_id, topic=topic)
    result = MotionResult(
        run_id=run_id, topic=topic, run_dir=run_dir, clips_dir=clips_dir
    )

    if state.is_done(STAGE) and not force and record_path.exists():
        previous = _load_previous(record_path)
        if previous and all(
            entry.get("status") in REUSABLE
            and entry.get("file")
            and (run_dir / entry["file"]).exists()
            for entry in previous.values()
        ):
            log.info("[%s] 이미 완료된 단계라 스킵한다 (run_id=%s)", STAGE, run_id)
            result.skipped = True
            result.record_path = record_path
            result.scenes = [previous[k] for k in sorted(previous)]
            return result

    try:
        timeline = build_timeline(scenes)
    except TimelineError as exc:
        state.mark_failed(STAGE, str(exc))
        raise MotionStageError(str(exc)) from exc

    images = _collect_images(run_dir / IMAGES_DIR, scenes)

    state.mark_running(STAGE)
    clips_dir.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = list(scene_warnings)
    previous = {} if force else _load_previous(record_path)
    seen: dict[str, int] = {}
    records: list[dict[str, Any]] = []

    def flush() -> None:
        """씬 하나가 끝날 때마다 기록을 갱신한다.

        아직 처리하지 않은 씬은 **지난 기록을 그대로 실어 둔다.** 3번 씬에서 죽었을 때
        처리한 것만 쓰면 4번 이후의 지문이 사라지고, 클립은 디스크에 멀쩡히 있는데
        다음 실행이 전부 다시 만든다 (`[6]`과 같은 이유, 여기선 돈이 아니라 시간이다).
        """
        done = {record["scene_id"]: record for record in records}
        merged = [
            entry
            for scene in scenes
            if (entry := done.get(scene["scene_id"]) or previous.get(scene["scene_id"]))
            is not None
        ]
        write_text(
            record_path,
            dump_json(_record_document(document, merged, warnings=warnings)),
        )

    for scene, segment in zip(scenes, timeline.segments):
        scene_id = scene["scene_id"]
        image = images[scene_id]
        try:
            plan = _plan_scene(scene, segment, image, seen=seen, warnings=warnings)
        except KenBurnsError as exc:
            state.mark_failed(STAGE, str(exc))
            flush()
            raise MotionStageError(f"씬 {scene_id}: {exc}") from exc

        clip_path = clips_dir / segment.clip_name
        prior = previous.get(scene_id)
        if (
            prior is not None
            and prior.get("status") in REUSABLE
            and prior.get("digest") == _plan_digest(plan)
            and clip_path.exists()
        ):
            records.append({**prior, "status": CACHED, "attempts": 0, "errors": []})
            flush()
            continue

        records.append(
            _render_scene(
                plan,
                image=image,
                clip_path=clip_path,
                run_dir=run_dir,
                ffmpeg=ffmpeg,
                runner=runner,
                timeout=timeout,
            )
        )
        flush()

    for warning in warnings:
        log.warning("[%s] %s", STAGE, warning)

    result.record_path = record_path
    result.scenes = records
    result.warnings = warnings

    info: dict[str, Any] = {
        "scene_count": len(records),
        "rendered": result.count(RENDERED),
        "cached": result.count(CACHED),
        "failed": result.count(FAILED),
        "demoted": result.demoted,
        "camera_reversed": result.reversed_count,
        "warnings": warnings,
        "outputs": [
            record_path.relative_to(paths.root).as_posix(),
            f"{run_dir.relative_to(paths.root).as_posix()}/{CLIPS_DIR}/",
        ],
    }

    if not result.passed:
        failed = [r["scene_id"] for r in records if r["status"] == FAILED]
        message = (
            f"클립을 만들지 못한 씬이 {len(failed)}개다 ({failed[:8]}). "
            f"[9]는 씬마다 클립 하나를 요구한다"
        )
        state.mark_failed(STAGE, message, **info)
        raise MotionStageError(message)

    state.mark_done(STAGE, **info)
    return result
