"""스펙 간 일관성 감사 — 값·어휘·단계명·폐기된 전제를 전수 대조한다 (ADR-0034).

`/spec-check`는 변경분(diff)이 스펙과 맞는지 본다. 이 도구는 **문서끼리 어긋났는지**를
본다. 축이 다르다.

    python tools/spec_audit.py          # 요약
    python tools/spec_audit.py -v       # 위치까지

종료 코드는 항상 0이다. 이 도구는 **판정이 아니라 관측**이다.

## 무엇을 보는가

ADR-0034가 만든 규칙 하나를 기계로 지킨다 — **값은 `specs/schema/`에 한 번만 적는다.**
문서나 코드가 그 값을 옮겨 적으면 `mj_video` 사고(ADR-0025 §3이 승인한 enum 값이 스펙
에도 코드에도 없었다)가 재발한다. 그래서 "갈렸는가"가 아니라 "옮겨 적었는가"를 본다.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "specs" / "schema"
VERBOSE = "-v" in sys.argv


def load() -> dict[str, str]:
    docs = {f"specs/{p.name}": p.read_text(encoding="utf-8") for p in sorted((ROOT / "specs").glob("*.md"))}
    docs["CLAUDE.md"] = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    docs["topics/backlog.md"] = (ROOT / "topics/backlog.md").read_text(encoding="utf-8")
    # src 전체를 본다 — schemas/만 보던 시절에 video/·stages/의 손 복사가 통과했다
    # (kenburns.py의 camera 7값 전량이 실례다. 2026-08-19 감사).
    src = ROOT / "src/shorts_factory"
    for p in sorted(src.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        rel = p.relative_to(src).as_posix()
        key = f"schemas/{p.name}" if rel.startswith("schemas/") else f"src/{rel}"
        docs[key] = p.read_text(encoding="utf-8")
    for p in sorted((src / "prompts").glob("*.md")):
        docs[f"prompts/{p.name}"] = p.read_text(encoding="utf-8")
    for p in sorted(SCHEMA_DIR.glob("*.json")):
        docs[f"specs/schema/{p.name}"] = p.read_text(encoding="utf-8")
    return docs


def load_json(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


#: 어휘 목록을 통째로 들고 있어도 되는 파일. 나머지가 들고 있으면 손 복사다.
#:
#: `vocab.json`은 출처이고, `beat-defaults.json`은 비트별 기본값이라 구조상 어휘를
#: 전부 참조한다 (ADR-0033의 되돌릴 자리).
VOCAB_HOLDERS = {"specs/schema/vocab.json", "specs/schema/beat-defaults.json"}

#: 옛 단계명. "옛 [1. script]"처럼 의도적으로 회고하는 문장은 제외한다.
STALE_STAGES = {r"(?<!옛 )\[1\.\s*script\]": "[1. script] → [1a]/[1s]/[1w] (ADR-0029)"}

#: 새 계약 파일이 도달해야 하는 문서.
REACH = {
    # 1부 (ADR-0049). 옛 단계 계약(07-outline 등)은 옛 산출물에만 남고(ADR-0036)
    # 새 계약이 아니므로 여기서 뺐다. sceneplan.schema.json은 [3s]의 세션 산출
    # 계약(씬 연출표)으로 복귀했다 (ADR-0049 §5, worklog 23).
    "script.md": ("specs/01-script-template.md", "specs/05-pipeline.md",
                  "specs/06-topic-research.md"),
    "scenes.json": ("specs/05-pipeline.md",),
    "sceneplan.schema.json": ("specs/05-pipeline.md", "schemas/sceneplan.py"),
    "factcheck.md": ("specs/05-pipeline.md", "specs/06-topic-research.md"),
    "seed.md": ("specs/06-topic-research.md",),
    "subtitle-style.json": ("specs/03-visual-rules.md",),
    "refs.json": ("specs/05-pipeline.md",),
    "refs.schema.json": ("specs/05-pipeline.md", "schemas/refs.py"),
    # 2부 (ADR-0056). 이미지 단계 계약(image_source 등)은 단계와 함께 지웠다.
    "clips.json": ("specs/05-pipeline.md",),
    "clip_review.json": ("specs/05-pipeline.md",),
    "script.ja.md": ("specs/01-script-template.md", "specs/05-pipeline.md"),
    "scenes.timed.{lang}.json": ("specs/05-pipeline.md", "specs/04-audio-rules.md"),
    "ending.json": ("specs/05-pipeline.md",),
    "ending.schema.json": ("specs/05-pipeline.md", "schemas/ending.py"),
    "specs/schema/": ("CLAUDE.md", "specs/05-pipeline.md"),
}

#: ADR-0033이 폐기한 전제. 스펙 재작성이 끝났으므로 0이어야 한다.
#: 이력은 ADR에 있다 — 스펙은 지금 상태만 적는다.
RETIRED = {
    "고정 단 구성 (ADR-0033)": r"7단",
    "옛 도메인 제한 (ADR-0033)": r"건축·토목|문화기술사",
    "소재 조건 게이트 (ADR-0033)": r"4조건",
    "비트 룰 테이블 강제 (ADR-0001 폐기)": r"룰 테이블",
}


def report(title: str, rows: list[str]) -> int:
    print(f"\n{'=' * 4} {title}")
    if not rows:
        print("   문제 없음")
        return 0
    for r in rows:
        print(f"   {r}")
    return len(rows)


#: 산문이 값을 하나씩 설명하는 것과 **목록을 옮겨 적은 것**을 가르는 길이.
#: 표의 한 행이나 인라인 enum은 짧고, 각 값을 설명하는 문단은 길다.
INLINE_LIST_MAX = 200


def check_vocab_copies(docs: dict[str, str]) -> list[str]:
    """어휘 목록을 통째로 옮겨 적은 곳. ADR-0034 §3이 금지한 손 동기화다.

    코드·스키마는 파일 전체에서 본다 — 거기서 전 값이 나오면 그건 목록이다.
    마크다운은 **한 줄** 안에 전 값이 있을 때만 잡는다. 여러 문단에 걸쳐 값을 하나씩
    설명하는 산문은 복사가 아니라 설명이고, 그걸 막으면 스펙이 값을 언급조차 못 한다.
    """
    vocab = load_json("vocab.json")
    rows: list[str] = []
    for name, block in vocab["$defs"].items():
        values = block["enum"]
        if len(values) < 3:
            continue
        patterns = [re.compile(rf"\b{re.escape(v)}\b") for v in values]
        for doc, text in docs.items():
            if doc in VOCAB_HOLDERS:
                continue
            if doc.endswith(".md"):
                where = [
                    line for line in text.splitlines()
                    if len(line) <= INLINE_LIST_MAX and all(p.search(line) for p in patterns)
                ]
                hit = bool(where)
            else:
                # 그 축을 vocab에서 확인하는 코드는 통과 — 전값 분기(디스패처·렌더러)는
                # 정당하되, 어휘와 갈리면 로드 시점에 터지는 장치가 있어야 한다.
                # 장치 없이 값만 다 들고 있는 파일이 이 규칙이 잡는 손 복사다.
                aware = re.search(rf'vocab\.(?:require|values|meta)\(\s*"{name}"', text)
                hit = not aware and all(p.search(text) for p in patterns)
            if hit:
                rows.append(
                    f"⚠ {doc}: {name} 어휘 {len(values)}개를 한자리에 옮겨 적었다 — "
                    "specs/schema/vocab.json이 출처다"
                )
    return rows


def check_ref_targets(docs: dict[str, str]) -> list[str]:
    """`$ref`가 가리키는 어휘가 실재하는가."""
    vocab = load_json("vocab.json")
    rows: list[str] = []
    for doc, text in docs.items():
        if not doc.startswith("specs/schema/"):
            continue
        for name in set(re.findall(r"vocab\.json#/\$defs/(\w+)", text)):
            if name not in vocab["$defs"]:
                rows.append(f"⚠ {doc}: vocab.json에 '{name}' 어휘가 없는데 $ref가 가리킨다")
    return rows


def check_limit_copies(docs: dict[str, str]) -> list[str]:
    """분량 엔벨로프를 문서·코드가 옮겨 적었는가.

    두 값이 공존하면 코드가 어느 쪽이 계약인지 모른다 (CLAUDE.md의 경고 그대로).
    생 실측값은 `docs/reference-analysis.md`에 있고 그 파일은 대조 대상이 아니다.
    """
    limits = load_json("script-rules.json")["limits"]
    rows: list[str] = []
    for key, value in limits.items():
        if isinstance(value, list):
            low, high = value
            pattern = re.compile(rf"{low}\s*~\s*{high}")
            shown = f"{low}~{high}"
        else:
            # 스칼라(line_chars_max 등)는 "43자"처럼 단위가 붙은 꼴만 잡는다 —
            # 맨 숫자를 잡으면 무관한 수까지 걸린다. subtitles.py의 43이 실례다
            # (2026-08-19 감사: [low, high]만 보던 규칙이 스칼라를 건너뛰었다).
            pattern = re.compile(rf"\b{value}\s*자")
            shown = str(value)
        for doc, text in docs.items():
            if doc.startswith("specs/schema/"):
                continue
            if pattern.search(text):
                rows.append(f"⚠ {doc}: {key}({shown})를 옮겨 적었다 — script-rules.json이 출처다")
    return rows


#: `$defs` 축 이름과 `meta` 절 이름이 다른 곳. 이름을 맞추는 것은 어휘 변경이라
#: ADR이 필요하므로 여기서 받아 준다 (stages/session.py의 주석과 같은 판단).
META_ALIAS = {"overlay": "overlay_type"}

#: enum이 아닌 meta 절 — 값 목록이 아니라 설정 묶음이다.
META_CONFIG = {"style"}


def check_meta_alignment() -> list[str]:
    """`$defs` enum과 `meta` 항목이 같은 값 집합인가.

    `$defs`에만 값을 늘리면 `meta`를 읽는 코드(`build_negative`, `[7]`의
    `video_prompt`)가 KeyError로 죽는다 — 두 목록의 동치를 검사하는 곳이 여기뿐이다
    (2026-08-19 감사 C5). meta 항목 중 dict가 아닌 키(`default` 같은 설정)는 값이
    아니므로 세지 않는다.
    """
    vocab = load_json("vocab.json")
    rows: list[str] = []
    for meta_name, items in vocab.get("meta", {}).items():
        if meta_name in META_CONFIG or not isinstance(items, dict):
            continue
        axis = META_ALIAS.get(meta_name, meta_name)
        block = vocab["$defs"].get(axis)
        if block is None:
            rows.append(f"⚠ vocab.json: meta.{meta_name}에 대응하는 $defs.{axis}가 없다")
            continue
        entries = {k for k, v in items.items() if isinstance(v, dict) and not k.startswith("_")}
        enum = set(block["enum"])
        for v in sorted(enum - entries):
            rows.append(
                f"⚠ vocab.json: $defs.{axis} 값 '{v}'가 meta.{meta_name}에 없다 — "
                "meta를 읽는 코드가 KeyError로 죽는다"
            )
        for v in sorted(entries - enum):
            rows.append(f"⚠ vocab.json: meta.{meta_name} 항목 '{v}'가 $defs.{axis} enum에 없다")
    return rows


def main() -> None:
    docs = load()
    problems = 0

    problems += report("어휘를 옮겨 적은 곳 (ADR-0034 §3)", check_vocab_copies(docs))
    problems += report("$defs와 meta의 값 집합 (감사 C5)", check_meta_alignment())
    problems += report("$ref가 가리키는 어휘", check_ref_targets(docs))
    problems += report("분량 값을 옮겨 적은 곳", check_limit_copies(docs))

    rows = []
    for key, targets in REACH.items():
        missing = [t for t in targets if key not in docs.get(t, "")]
        if missing:
            rows.append(f"⚠ {key} 가 없다: {', '.join(missing)}")
    problems += report("새 계약 파일이 도달하지 않은 문서", rows)

    rows = []
    for pat, fix in STALE_STAGES.items():
        for doc, text in docs.items():
            n = len(re.findall(pat, text))
            if n:
                rows.append(f"⚠ {doc}: {n}회 — {fix}")
    problems += report("옛 단계명", rows)

    rows = []
    for name, pat in RETIRED.items():
        where = {d: len(re.findall(pat, t)) for d, t in docs.items() if re.search(pat, t)}
        if where:
            rows.append(f"⚠ {name}: " + ", ".join(f"{d}({n})" for d, n in sorted(where.items())))
    problems += report("폐기된 전제의 잔존", rows)

    print(f"\n{'=' * 4} 합계 {problems}건")


if __name__ == "__main__":
    main()
