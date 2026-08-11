"""[9. assemble] 테스트 입력 — 실물 대본으로 만든 `scenes.timed.json` + 껍데기 클립.

ElevenLabs 키가 없어도 `[9]`는 개발된다. `[3]`의 산출물 형태가
`schemas/timed_scenes.build_timed_scenes`로 정의돼 있어서, 1부 대본의 `est_*`를 실측
자리에 끼우면 계약을 그대로 만족하는 문서가 나오기 때문이다. 값이 추정이라는 사실은
`[9]`에게 보이지 않는다 — `[9]`가 보는 것은 "빈틈 없이 이어지는 씬 시각" 하나다.

클립은 내용이 없는 파일이다. `[7. motion]`이 아직 없고, `[9]`가 클립에서 읽는 것은
존재 여부뿐이다 (길이는 계약으로 안다).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import HOOVER, PISA, load_script  # noqa: F401  (테스트가 재수출로 쓴다)
from shorts_factory.config import Paths, write_text
from shorts_factory.jsonio import dump_json
from shorts_factory.schemas.timed_scenes import build_timed_scenes
from shorts_factory.video.fake import write_fake_clips


def timed_document(slug: str = PISA) -> dict[str, Any]:
    """`06-script.json` → `[3]`이 냈을 모양의 `scenes.timed.json` 문서."""
    source = load_script(slug)
    boundaries = [(s["est_start"], s["est_end"]) for s in source["scenes"]]
    return build_timed_scenes(source, boundaries)


def install_run(
    paths: Paths,
    slug: str = PISA,
    *,
    clips: bool = True,
    document: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """run 디렉터리에 `[9]`의 입력을 놓는다. `(run_id, 문서)`를 돌려준다."""
    document = document or timed_document(slug)
    run_id = document["run_id"]
    run_dir = paths.run_dir(run_id)
    write_text(run_dir / "scenes.timed.json", dump_json(document))
    if clips:
        write_fake_clips(
            run_dir / "clips", [s["scene_id"] for s in document["scenes"]]
        )
    return run_id, document


def clips_dir(paths: Paths, run_id: str) -> Path:
    return paths.run_dir(run_id) / "clips"
