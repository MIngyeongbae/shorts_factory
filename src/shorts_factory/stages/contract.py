"""씬 계약의 소재지 — `runs/{run_id}/scenes.json` 하나다 (ADR-0052).

specs/05 계약 표: 씬 계약은 `[3s. scenetable]`의 산출물이고, 읽는 단계
(`[4]`·`[5]`·`[7]`·`[9]`)는 전부 여기서 찾는다. 옛 경로
(`topics/{slug}/06-script.json`)는 ADR-0052가 코드에서 삭제했다 — 갈림길이 없다.

slug→run 해석은 `runs/*/topic.json`(`find_run_for_slug`) 하나다.

## `[7]`·`[9]`의 시각·연출 병합 (`merge_scene_direction`)

`scenes.timed.{lang}.json`은 `text`+시각만 담는다 (specs/05 계약 표 — 대본 속성은
`scenes.json` 소관). 그래서 `[7]`·`[9]`는 시각을 실측 파일에서, 연출을 씬 계약에서
**메모리에서 합쳐** 읽는다 — 파일로 남기지 않는다. 남기면 같은 값이 두 파일에 있게 되고
갈라진 쪽을 읽은 단계만 어긋난다 (ADR-0020). 겹치는 필드는 실측이 이긴다 — 시각과
`text`의 정본은 그 언어의 `scenes.timed.{lang}.json`이다. 실측은 **언어당 하나**이고
(ADR-0056 결정 5) 씬 계약은 언어와 무관하게 하나다 — `load_timed_scenes(run_dir, lang)`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from ..config import Paths
from ..runstate import RunNotFound, find_run_for_slug
from ..schemas.timed_scenes import timed_scenes_path

#: 씬 계약 — `[3s. scenetable]`의 산출물 (ADR-0049 §5·0052).
SCENES_FILE = "scenes.json"

#: 실측 파일에 없는, 씬 계약에서만 오는 필드 중 **[7]·[9]가 읽지 않는 것.**
#: 이미지 지시는 병합본에도 싣지 않는다 — `scenes.timed.{lang}.json`의 DROPPED와 같은 이유다
#: (그림 쪽 소비자는 prompts.json을 읽는다, ADR-0020·0022).
_IMAGE_FIELDS = ("visual_goal", "framing")

#: 실측 파일이 정본인 필드 — 병합에서 씬 계약 쪽 값을 쓰지 않는다.
_MEASURED_FIELDS = ("est_start", "est_end")


class SceneContractNotFound(Exception):
    """씬 계약이 없다 — `[3s]`가 아직 안 돌았거나 run이 없다."""


class TimedScenesNotFound(Exception):
    """그 언어의 실측 파일이 없다 — `[3]`이 안 돌았거나 길이 초과로 멈췄다 (ADR-0017)."""


def load_timed_scenes(run_dir: Path, lang: str) -> dict[str, Any]:
    """그 언어의 `scenes.timed.{lang}.json` 문서 (병합 전). 없으면 `TimedScenesNotFound`."""
    path = timed_scenes_path(run_dir, lang)
    if not path.exists():
        raise TimedScenesNotFound(
            f"{path.name}이 없다: {path}. [3. tts+sync]를 먼저 실행하라 — "
            "총 길이가 상한을 넘어 멈춘 run에는 이 파일이 일부러 없다 (ADR-0017)."
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TimedScenesNotFound(f"{path.name}을 읽을 수 없다: {path} — {exc}") from exc


def load_merged_scenes(run_dir: Path, lang: str) -> dict[str, Any]:
    """그 언어의 실측 위에 씬 계약을 병합한 문서 — `[7]`·`[9]`가 읽는 모양 (메모리 전용)."""
    return merge_scene_direction(load_timed_scenes(run_dir, lang), run_dir)


def locate_scene_contract(paths: Paths, slug: str) -> Path:
    """씬 계약 파일의 경로: `runs/{run_id}/scenes.json`."""
    try:
        run_id, _contract = find_run_for_slug(paths, slug)
    except RunNotFound as exc:
        raise SceneContractNotFound(str(exc)) from exc

    path = paths.run_dir(run_id) / SCENES_FILE
    if not path.exists():
        raise SceneContractNotFound(
            f"씬 계약이 없다: {path}. [3s. scenetable]을 먼저 실행하라"
        )
    return path


def load_scene_contract(paths: Paths, slug: str) -> tuple[dict[str, Any], Path]:
    """`(씬 계약 문서, 경로)`. 파싱 실패는 위치를 담아 올린다."""
    path = locate_scene_contract(paths, slug)
    try:
        return json.loads(path.read_text(encoding="utf-8")), path
    except json.JSONDecodeError as exc:
        raise SceneContractNotFound(f"씬 계약을 읽을 수 없다: {path} — {exc}") from exc


def find_contract_for_run(paths: Paths, run_id: str) -> dict[str, Any]:
    """run_id → 씬 계약. `--slug` 없이 run만 아는 호출 경로가 쓴다."""
    path = paths.run_dir(run_id) / SCENES_FILE
    if not path.exists():
        raise SceneContractNotFound(
            f"씬 계약이 없다: {path}. [3s. scenetable]을 먼저 실행하라"
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SceneContractNotFound(f"씬 계약을 읽을 수 없다: {path} — {exc}") from exc


def merge_scene_direction(timed: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """실측(`timed`) 위에 `scenes.json`의 연출·대본 속성을 병합한다 (메모리 전용).

    `scenes.json` 부재는 실패다 (ADR-0052 — 폴백 경로가 없다).
    """
    contract_path = run_dir / SCENES_FILE
    if not contract_path.exists():
        raise SceneContractNotFound(
            f"씬 계약이 없다: {contract_path}. [3s. scenetable]을 먼저 실행하라"
        )

    try:
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SceneContractNotFound(
            f"씬 계약을 읽을 수 없다: {contract_path} — {exc}"
        ) from exc

    by_id = {scene.get("scene_id"): scene for scene in contract.get("scenes", [])}
    merged_scenes: list[dict[str, Any]] = []
    for scene in timed.get("scenes", []):
        source = by_id.get(scene.get("scene_id"), {})
        merged = {
            key: copy.deepcopy(value)
            for key, value in source.items()
            if key not in _IMAGE_FIELDS and key not in _MEASURED_FIELDS
        }
        merged.update(scene)  # 실측이 이긴다 — text·start·end의 정본
        merged_scenes.append(merged)

    out = dict(timed)
    out["scenes"] = merged_scenes
    if "characters" in contract:
        # [7]·[9]는 안 읽지만 지우지 않고 통과시킨다 (D-2).
        out["characters"] = copy.deepcopy(contract["characters"])
    return out
