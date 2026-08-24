"""자막 폰트의 세로 메트릭 정규화 (ADR-0064).

    python tools/normalize_font_metrics.py            # 검사만 (기본)
    python tools/normalize_font_metrics.py --write    # 제자리에서 고쳐 쓴다

## 왜 필요한가

**ASS의 `Fontsize`는 em 크기가 아니라 줄 상자 높이다.** libass는 폰트의
`OS/2.usWinAscent + usWinDescent`가 그 값이 되도록 글리프를 축소한다:

    실제 em = Fontsize ÷ ((usWinAscent + usWinDescent) ÷ unitsPerEm)

그래서 같은 `Fontsize` 44가 폰트마다 다른 크기로 그려진다. 실측(ADR-0064 §3):
Do Hyeon 1.000 → 44px, LINE Seed JP 1.343 → 32.8px, Google Sans **2.402 → 18.3px**.
Google Sans는 데바나가리·에티오피아까지 덮느라 줄 상자가 2.4em이다.

`subtitle-style.json`의 `_geometry`는 **글자 폭 = 폰트 크기 × ratio**를 전제한다. 그 전제가
참이려면 줄 상자 합이 1.000em이어야 한다. 그래서 계약을 비트는 대신 **폰트를 계약에 맞춘다.**

## 무엇을 바꾸는가

세로 메트릭 6개 숫자뿐이다. 글리프 윤곽·cmap·커닝·`name` 테이블(저작권·라이선스 포함)은
건드리지 않는다. 기준값 0.8/0.2는 Do Hyeon 원본 값이다 — 이미 계약과 맞는 폰트에 맞췄다.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 정규화 목표 (unitsPerEm 대비). 합 = 1.000이라 ASS `Fontsize` = em이 된다 (ADR-0064 「결정」).
ASCENT_RATIO = 0.8
DESCENT_RATIO = 0.2

#: 이 도구가 다루는 폰트 파일. `.ttc`는 컬렉션이라 손대지 않는다 (지금 리포에 없다).
SUFFIXES = (".ttf", ".otf")


def target(upem: int) -> tuple[int, int]:
    """그 폰트의 목표 (ascent, descent절댓값)."""
    return round(upem * ASCENT_RATIO), round(upem * DESCENT_RATIO)


def read_metrics(font) -> dict[str, int]:
    os2, hhea, head = font["OS/2"], font["hhea"], font["head"]
    return {
        "upem": head.unitsPerEm,
        "winAscent": os2.usWinAscent,
        "winDescent": os2.usWinDescent,
        "typoAscender": os2.sTypoAscender,
        "typoDescender": os2.sTypoDescender,
        "typoLineGap": os2.sTypoLineGap,
        "hheaAscent": hhea.ascent,
        "hheaDescent": hhea.descent,
        "hheaLineGap": hhea.lineGap,
    }


def wanted(upem: int) -> dict[str, int]:
    asc, desc = target(upem)
    return {
        "winAscent": asc, "winDescent": desc,
        "typoAscender": asc, "typoDescender": -desc, "typoLineGap": 0,
        "hheaAscent": asc, "hheaDescent": -desc, "hheaLineGap": 0,
    }


def apply(font, upem: int) -> None:
    asc, desc = target(upem)
    os2, hhea = font["OS/2"], font["hhea"]
    os2.usWinAscent, os2.usWinDescent = asc, desc
    os2.sTypoAscender, os2.sTypoDescender, os2.sTypoLineGap = asc, -desc, 0
    hhea.ascent, hhea.descent, hhea.lineGap = asc, -desc, 0


def main() -> int:
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        print("fonttools가 없다. `pip install -e .[dev]` 또는 `pip install fonttools`.")
        return 2

    write = "--write" in sys.argv
    fonts_dir = ROOT / "assets" / "fonts"
    files = sorted(p for p in fonts_dir.glob("*") if p.suffix.lower() in SUFFIXES)
    if not files:
        print(f"{fonts_dir}/에 폰트가 없다.")
        return 0

    off = 0
    for path in files:
        font = TTFont(path)
        now = read_metrics(font)
        upem = now.pop("upem")
        want = wanted(upem)
        diff = {k: (now[k], v) for k, v in want.items() if now[k] != v}
        ratio = (now["winAscent"] + now["winDescent"]) / upem
        if not diff:
            print(f"  OK   {path.name}  win비 {ratio:.3f}")
            continue
        off += 1
        print(f"  {'고침' if write else '어긋남'} {path.name}  win비 {ratio:.3f} → 1.000  (upem {upem})")
        for key, (was, now_want) in diff.items():
            print(f"         {key}: {was} → {now_want}")
        if write:
            apply(font, upem)
            font.save(path)
        font.close()

    if off and not write:
        print(f"\n{off}개가 정규화 안 됐다 (ADR-0064). 고치려면 --write.")
        return 1
    print(f"\n{'고쳤다' if write and off else '전부 정규화됨'} — 폰트 {len(files)}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
