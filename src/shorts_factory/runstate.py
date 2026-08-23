"""run 실행 상태 (runs/{run_id}/state.json).

specs/05-pipeline.md 실패 정책:
- 단계 실패 = run 디렉터리에 상태 기록 후 종료
- 같은 run_id로 재실행 시 완료된 단계는 스킵
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
BLOCKED = "blocked"  # 스펙상 진입 금지 (예: verdict fail)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RunState:
    run_id: str
    path: Path
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load_or_create(cls, run_dir: Path, run_id: str, **seed: Any) -> "RunState":
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "state.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = {"run_id": run_id, "created_at": _now(), "stages": {}}
        data.setdefault("stages", {})
        for key, value in seed.items():
            data.setdefault(key, value)
        state = cls(run_id=run_id, path=path, data=data)
        state.save()
        return state

    def save(self) -> None:
        self.data["updated_at"] = _now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")

    # --- 단계 상태 ---------------------------------------------------------

    def stage(self, name: str) -> dict[str, Any]:
        return self.data["stages"].setdefault(name, {"status": PENDING})

    def status_of(self, name: str) -> str:
        return self.stage(name).get("status", PENDING)

    def is_done(self, name: str) -> bool:
        return self.status_of(name) == DONE

    def mark_running(self, name: str) -> None:
        entry = self.stage(name)
        entry["status"] = RUNNING
        entry["started_at"] = _now()
        entry.pop("error", None)
        self.save()

    def mark_done(self, name: str, **info: Any) -> None:
        entry = self.stage(name)
        entry["status"] = DONE
        entry["finished_at"] = _now()
        entry.pop("error", None)
        entry.update(info)
        self.save()

    def mark_failed(self, name: str, error: str, **info: Any) -> None:
        entry = self.stage(name)
        entry["status"] = FAILED
        entry["finished_at"] = _now()
        entry["error"] = error
        entry.update(info)
        self.save()

    def mark_blocked(self, name: str, reason: str, **info: Any) -> None:
        entry = self.stage(name)
        entry["status"] = BLOCKED
        entry["finished_at"] = _now()
        entry["reason"] = reason
        entry.update(info)
        self.save()

    def note(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()


class RunNotFound(Exception):
    """슬러그에 해당하는 run이 없다."""


def find_run_for_slug(paths, slug: str) -> tuple[str, dict[str, Any]]:
    """해당 슬러그의 가장 최근 run을 찾는다 (run_id가 날짜 프리픽스라 사전순=시간순).

    옛 stages/research.py에서 옮겨 왔다 (ADR-0049) — run 소속이라 여기가 자리다.
    """
    matches: list[tuple[str, dict[str, Any]]] = []
    for contract_path in sorted(paths.runs.glob("*/topic.json")):
        try:
            data = json.loads(contract_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("slug") == slug:
            matches.append((contract_path.parent.name, data))

    if not matches:
        raise RunNotFound(
            f"슬러그 '{slug}'에 해당하는 run이 없다. [0. seed]를 먼저 실행하라."
        )
    return matches[-1]
