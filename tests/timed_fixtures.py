"""[7]·[9] 테스트 입력 — 실물 대본으로 만든 `scenes.timed.{lang}.json` + 껍데기 클립.

ElevenLabs 키가 없어도 `[7]`·`[9]`는 개발된다. `[3]`의 산출물 형태가
`schemas/timed_scenes.build_timed_scenes`로 정의돼 있어서, 1부 대본의 `est_*`를 실측
자리에 끼우면 계약을 그대로 만족하는 문서가 나오기 때문이다. 값이 추정이라는 사실은
`[9]`에게 보이지 않는다 — `[9]`가 보는 것은 "빈틈 없이 이어지는 씬 시각" 하나다.

언어는 **ko 필수 + ja·en 선택**이다 (ADR-0056 결정 5). `install_run(..., langs={"ja": 1.13})`
처럼 배율을 주면 ko 실측을 그 배율로 늘린 문서를 그 언어 파일로 놓는다 — 실측 n=1의
ja ×1.13 / en ×0.78이 출처다.

클립은 내용이 없는 파일이다. `[9]`가 클립에서 읽는 것은 존재 여부뿐이다 (길이는
계약으로 안다).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from conftest import HOOVER, PISA, load_script  # noqa: F401  (테스트가 재수출로 쓴다)
from shorts_factory.config import Paths, write_text
from shorts_factory.jsonio import dump_json
from shorts_factory.schemas.timed_scenes import build_timed_scenes, timed_scenes_path
from shorts_factory.video.fake import write_fake_clips


def timed_document(slug: str = PISA, *, scale: float = 1.0) -> dict[str, Any]:
    """씬 계약 → `[7]`·`[9]`가 읽는 병합본 모양의 문서. `scale`은 언어별 발화 길이 배율."""
    source = load_script(slug)
    boundaries = [
        (round(s["est_start"] * scale, 3), round(s["est_end"] * scale, 3))
        for s in source["scenes"]
    ]
    return build_timed_scenes(source, boundaries)


def line_timed_document(document: dict[str, Any]) -> dict[str, Any]:
    """병합본 → `[3]`이 실제로 쓰는 줄 경계 문서 (text+시각만, specs/05 계약 표).

    `title`은 있으면 나른다 — 언어별 제목 훅이 이 파일로 흐른다 (ADR-0065).
    """
    line = {
        "run_id": document["run_id"],
        "topic": document["topic"],
        "total_duration": document["total_duration"],
        "scenes": [
            {"scene_id": s["scene_id"], "text": s["text"], "start": s["start"], "end": s["end"]}
            for s in document["scenes"]
        ],
    }
    if document.get("title"):
        line["title"] = document["title"]
    return line


def install_run(
    paths: Paths,
    slug: str = PISA,
    *,
    clips: bool = True,
    document: dict[str, Any] | None = None,
    langs: Mapping[str, float] | None = None,
) -> tuple[str, dict[str, Any]]:
    """run 디렉터리에 `[7]`·`[9]`의 입력을 놓는다. `(run_id, ko 문서)`를 돌려준다.

    씬 계약(`scenes.json`)도 함께 놓는다 — 두 단계가 실측과 계약을 메모리 병합으로
    읽는다 (ADR-0052, stages/contract.py). 겹치는 필드는 실측 문서가 이기므로
    `document`를 변형한 테스트의 의도는 그대로 살아 있다.
    """
    document = document or timed_document(slug)
    run_id = document["run_id"]
    run_dir = paths.run_dir(run_id)
    contract = load_script(slug)
    contract["run_id"] = run_id
    write_text(
        run_dir / "topic.json",
        dump_json({"run_id": run_id, "slug": slug, "topic": contract["topic"]}),
    )
    write_text(run_dir / "scenes.json", dump_json(contract))
    write_text(timed_scenes_path(run_dir, "ko"), dump_json(document))
    for lang, scale in (langs or {}).items():
        scaled = timed_document(slug, scale=scale)
        scaled["run_id"] = run_id
        write_text(timed_scenes_path(run_dir, lang), dump_json(line_timed_document(scaled)))
    if clips:
        write_fake_clips(
            run_dir / "clips", [s["scene_id"] for s in document["scenes"]]
        )
    return run_id, document


def clips_dir(paths: Paths, run_id: str) -> Path:
    return paths.run_dir(run_id) / "clips"
