"""클립 풀 — 기본 풀 하나와 언어별 겹풀 (ADR-0095).

`[7]`이 `clips/`(기본 풀, 전 씬)를 만든 뒤 판정에서 고른 ko 아닌 언어마다 씬의 절반을
새 시드로 다시 사서 `clips.{lang}/`에 둔다. 어느 씬이 누구 것인지는 계약값이다 —
`specs/schema/channel-look.json` `meta.clip_scenes` (ko `all` · ja `odd` · en `even`). 코드는
언어 이름을 손에 들지 않는다 (ADR-0034 §3).

`[9]`·`[9t]`의 조회는 **겹풀 먼저, 없으면 기본 풀**이고 파일 존재로 가른다 — 기록이 아니라
파일이 정본이다. 겹풀이 통째로 없는 옛 편은 지금까지와 완전히 같이 돈다 (specs/05 D-4).
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from ..schemas import vocab
from .timeline import CLIPS_DIR

#: `clip_scenes`의 값. `all`은 기본 풀이고 겹풀이 없다.
ALL, ODD, EVEN = "all", "odd", "even"
_VALUES = (ALL, ODD, EVEN)

#: 검수 기록·재료의 이름 줄기 — `clip_review.json` / `clip_review/`, 겹풀은 `.{lang}`이 붙는다.
REVIEW_STEM = "clip_review"

_CLIP_SCENES: dict[str, str] = {
    lang: str(value)
    for lang, value in vocab.CHANNEL_LOOK["meta"]["clip_scenes"].items()
    if not lang.startswith("_")
}


def clip_scenes_for(lang: str) -> str:
    """그 언어의 겹풀이 새로 사는 씬 — `all`·`odd`·`even`. 모르는 언어는 `all`(겹풀 없음)."""
    value = _CLIP_SCENES.get(lang, ALL)
    if value not in _VALUES:
        raise ValueError(
            f"channel-look.json meta.clip_scenes.{lang}={value!r}는 계약 밖이다 "
            f"(가능: {', '.join(_VALUES)})"
        )
    return value


def overlay_languages() -> tuple[str, ...]:
    """겹풀을 지는 언어 — 계약을 되짚는 자리이고 배치를 정하지 않는다."""
    return tuple(lang for lang in _CLIP_SCENES if clip_scenes_for(lang) != ALL)


def overlay_scene_ids(lang: str, scene_ids: Sequence[int]) -> list[int]:
    """그 언어의 겹풀에 들어갈 씬. `all`이면 빈 목록이다."""
    value = clip_scenes_for(lang)
    if value == ALL:
        return []
    parity = 1 if value == ODD else 0
    return sorted(int(sid) for sid in scene_ids if int(sid) % 2 == parity)


def overlay_dir(lang: str) -> str:
    """`clips.{lang}` — run 디렉터리 상대."""
    return f"{CLIPS_DIR}.{lang}"


def overlay_record(lang: str) -> str:
    return f"{CLIPS_DIR}.{lang}.json"


def overlay_review(lang: str) -> str:
    return f"{REVIEW_STEM}.{lang}.json"


def overlay_review_dir(lang: str) -> str:
    return f"{REVIEW_STEM}.{lang}"


def clip_source_dir(run_dir: Path, lang: str, scene_id: int) -> str:
    """이 언어가 이 씬의 클립을 읽을 디렉터리 — 겹풀에 파일이 있으면 그것, 없으면 기본 풀."""
    if lang and (Path(run_dir) / overlay_dir(lang) / f"{scene_id}.mp4").exists():
        return overlay_dir(lang)
    return CLIPS_DIR


__all__ = [
    "ALL",
    "EVEN",
    "ODD",
    "REVIEW_STEM",
    "clip_scenes_for",
    "clip_source_dir",
    "overlay_dir",
    "overlay_languages",
    "overlay_record",
    "overlay_review",
    "overlay_review_dir",
    "overlay_scene_ids",
]
