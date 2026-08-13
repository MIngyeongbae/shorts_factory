"""스펙 간 일관성 감사 — 값·enum·단계명·폐기된 전제를 전수 대조한다 (ADR-0034).

`/spec-check`는 변경분(diff)이 스펙과 맞는지 본다. 이 도구는 **문서끼리 어긋났는지**를
본다. 축이 다르다.

    python tools/spec_audit.py          # 요약
    python tools/spec_audit.py -v       # 위치까지

종료 코드는 항상 0이다. 이 도구는 **판정이 아니라 관측**이다 — 폐기된 전제의 잔존은
ADR-0033 재작성이 끝날 때까지 정상적으로 잡힌다.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERBOSE = "-v" in sys.argv


def load() -> dict[str, str]:
    docs = {f"specs/{p.name}": p.read_text(encoding="utf-8") for p in sorted((ROOT / "specs").glob("*.md"))}
    docs["CLAUDE.md"] = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    for p in sorted((ROOT / "src/shorts_factory/schemas").glob("*.py")):
        docs[f"schemas/{p.name}"] = p.read_text(encoding="utf-8")
    return docs


#: 값이 여러 문서에 흩어져 있고 **하나여야 하는** 것들.
#: 두 값이 공존하면 어느 쪽이 계약인지 코드가 알 수 없다.
SINGLE_VALUE = {
    "총 글자 수": r"545\s*~\s*5\d\d자",
    "발화 속도": r"5\.\d\s*~\s*6\.\d자/초",
    "총 길이": r"9\d\s*~\s*10\d초",
    "자막 줄 수": r"2\d\s*~\s*2\d줄",
    "전환점 누적": r"2\d0\s*~\s*3\d0자",
    "디졸브 길이": r"디졸브[^.\n]{0,6}(0\.\d)초",
}

#: 스펙과 코드에 **같이** 있어야 하는 어휘.
#:
#: 코드 쪽은 파일 하나가 아니라 `schemas/` **전체의 합집합**으로 본다. 값이 한 모듈에
#: 선언되고 다른 모듈이 import하는 것은 정상이고(`SUBJECT_SCALES`가 그렇다) 오히려
#: 권장 형태다 — 파일 단위로 검사하면 그 패턴이 전부 오탐이 된다.
ENUMS = {
    "motion": (r"\b(kenburns|kling|mj_video)\b", "specs/02-beat-schema.md"),
    "subject_scale": (r"\b(wide|close|diagram)\b", "specs/02-beat-schema.md"),
    "camera": (r"\b(slow_zoom_in|slow_zoom_out|tilt_down|tilt_up|pan_left|pan_right|static)\b",
               "specs/02-beat-schema.md"),
}

#: ADR-0033이 폐기한 전제. 스펙 재작성이 끝나면 0이 되어야 한다.
RETIRED = {
    "7단 구조 (ADR-0033)": r"7단",
    "건축·토목 도메인 (ADR-0033)": r"건축·토목|문화기술사",
    "소재 4조건 필수 (ADR-0033)": r"4조건",
    "비트 룰 테이블 강제 (ADR-0001 폐기)": r"룰 테이블",
}

#: 옛 단계명. "옛 [1. script]"처럼 의도적으로 회고하는 문장은 제외한다.
STALE_STAGES = {r"(?<!옛 )\[1\.\s*script\]": "[1. script] → [1a]/[1s]/[1w] (ADR-0029)"}

#: 새 계약 파일이 도달해야 하는 문서.
REACH = {
    "07-outline.json": ("specs/05-pipeline.md", "specs/06-topic-research.md"),
    "08-sceneplan.json": ("specs/05-pipeline.md", "specs/06-topic-research.md"),
    "refs.json": ("specs/05-pipeline.md",),
    "image_review.json": ("specs/05-pipeline.md",),
}


def report(title: str, rows: list[str]) -> int:
    print(f"\n{'=' * 4} {title}")
    if not rows:
        print("   문제 없음")
        return 0
    for r in rows:
        print(f"   {r}")
    return len(rows)


def main() -> None:
    docs = load()
    problems = 0

    rows = []
    for name, pat in SINGLE_VALUE.items():
        found: dict[str, list[str]] = {}
        for doc, text in docs.items():
            for m in re.finditer(pat, text):
                # 캡처 그룹이 있으면 그것이 값이다. 전체 매치를 키로 쓰면
                # "디졸브 0.6초"와 "디졸브 겹침 0.6초"가 다른 값으로 잡힌다.
                found.setdefault(m.group(1) if m.groups() else m.group(), []).append(doc)
        if len(found) > 1:
            rows.append(f"⚠ {name}: 값이 {len(found)}개 공존한다")
            for value, where in sorted(found.items()):
                rows.append(f"     {value:16} <- {', '.join(sorted(set(where)))}")
    problems += report("한 값이어야 하는데 여럿인 것", rows)

    rows = []
    for name, (pat, spec_doc) in ENUMS.items():
        in_spec = set(re.findall(pat, docs.get(spec_doc, "")))
        in_code: set[str] = set()
        for doc, text in docs.items():
            if doc.startswith("schemas/"):
                in_code |= set(re.findall(pat, text))
        if in_spec != in_code:
            rows.append(f"⚠ {name} 어휘가 갈렸다")
            rows.append(f"     스펙에만: {sorted(in_spec - in_code) or '-'}")
            rows.append(f"     코드에만: {sorted(in_code - in_spec) or '-'}")
    problems += report("스펙 ↔ 코드 어휘 불일치", rows)

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
            rows.append(f"· {name}: " + ", ".join(f"{d}({n})" for d, n in sorted(where.items())))
    report("폐기된 전제의 잔존 — ADR-0033 재작성 대기 (정상)", rows)

    print(f"\n{'=' * 4} 합계 {problems}건 (폐기 전제 잔존 제외)")


if __name__ == "__main__":
    main()
