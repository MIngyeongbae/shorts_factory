# 자막 폰트

레이어 B(번인 자막·엔딩 크레딧)가 쓰는 폰트다. **리포지토리가 폰트를 들고 있어야 렌더가
재현된다** (ADR-0002) — 없으면 libass가 시스템 폰트로 떨어져 룩이 편마다 바뀐다.
`[9] assemble`은 이 디렉터리를 `fontsdir=`로 넘기고, `[8] ending`은 여기서 파일 하나를 골라
`drawtext=fontfile=`로 넘긴다.

패밀리 이름의 정본은 `specs/schema/subtitle-style.json`이다 (`font_name` + `SUBTITLE_FONT_*`).
**여기 적힌 이름을 코드에 옮겨 적지 않는다** (ADR-0034).

| 언어 | 파일 | 패밀리 이름 (ASS `Fontname`) | 굵기 |
|---|---|---|---|
| ko | `DoHyeon-Regular.ttf` | `Do Hyeon` | Regular 하나뿐 — ASS `Bold=-1`은 libass 합성 볼드로 간다 |
| ja | `LINESeedJP-Bold.ttf` | `LINE Seed JP` | Bold(700), 실제 볼드 페이스 |
| en | `GoogleSans-Bold.ttf` | `Google Sans` | Bold(700), 실제 볼드 페이스 |

자막 스타일이 언제나 `Bold=-1`이라 **볼드 페이스만 넣는다** — Regular까지 넣으면 쓰이지도
않는 채로 리포지토리만 5MB 무거워진다. Do Hyeon은 애초에 단일 굵기다.

## ⚠ 이 파일들은 원본 배포본과 다르다 — 세로 메트릭을 정규화했다 (ADR-0064)

**ASS의 `Fontsize`는 em 크기가 아니라 줄 상자 높이다.** libass는 폰트의
`OS/2.usWinAscent + usWinDescent`가 그 값이 되도록 글리프를 축소하므로, 같은 44px이 폰트마다
다른 크기로 그려진다. 원본 그대로 넣었을 때의 실측:

| 폰트 | 원본 win 비 | ASS 44 → 실제 em |
|---|---|---|
| Do Hyeon | 1.000 | 44.0px |
| LINE Seed JP | 1.343 | 32.8px |
| Google Sans | **2.402** | **18.3px** |

그래서 세 파일 모두 `tools/normalize_font_metrics.py`로 **세로 메트릭 6개만** 다시 썼다
(`usWinAscent` 800 / `usWinDescent` 200 / `sTypo*` 800·−200·0 / `hhea` 800·−200·0,
unitsPerEm 1000). 합이 1.000em이라 **`Fontsize` = em**이 되고 `subtitle-style.json`의
`_geometry` 산수가 문자 그대로 참이 된다.

글리프 윤곽·cmap·커닝·`name` 테이블(저작권·라이선스 포함)은 **건드리지 않았다.** OFL 1.1은
개작·재배포를 허용하며(§2) 저작권 고지와 라이선스를 함께 배포할 것을 요구한다 — 아래 「출처와
라이선스」와 `OFL-*.txt`가 그 몫이다. 패밀리 이름도 그대로다 (셋 다 예약 폰트 이름 선언 없음).

**폰트를 새로 넣을 때는 반드시 통과시킨다:**

    python tools/normalize_font_metrics.py            # 검사 (어긋나면 종료 코드 1)
    python tools/normalize_font_metrics.py --write    # 제자리에서 고쳐 쓴다

## 출처와 라이선스

셋 다 SIL Open Font License 1.1이다 (Google Fonts 메타데이터 `license: ofl`,
`isOpenSource: true`). 아래 값은 **폰트 파일의 name 테이블에서 그대로 읽은 것**이고 정규화
뒤에도 그대로 남아 있다.

- **Do Hyeon** — `Version 1.001` · `Copyright 2018 The Do Hyeon Project Authors` ·
  라이선스 전문 `OFL-DoHyeon.txt`
  출처: `https://github.com/google/fonts/tree/main/ofl/dohyeon`
- **LINE Seed JP** — `Version 1.010` · `© LY Corporation` · 라이선스 전문 `OFL-LINESeedJP.txt`
  출처: `https://github.com/google/fonts/tree/main/ofl/lineseedjp`
- **Google Sans** — `Version 13.002;[5e3df34c1]` ·
  `Copyright 2025 The Google Sans Project Authors (github.com/googlefonts/googlesans)` ·
  name 레코드 14 = `https://openfontlicense.org`
  출처: `https://fonts.gstatic.com/s/googlesans/v70/` (css2 API가 주는 정적 인스턴스)
  **OFL 전문 파일이 없다** — 업스트림 저장소(`googlefonts/googlesans`)가 아직 비공개이고
  `google/fonts`에도 들어와 있지 않아, 다른 둘처럼 배포본에 딸린 `OFL.txt`를 받아올 데가
  없다. 폰트가 자기 name 테이블에서 가리키는 URL을 대신 적는다. 공개되면 전문을 받아 둔다.

## 글리프 커버리지 (2026-08-24 실측)

`topics/*/script*.md`의 발화 줄 전부(ko 422줄 / ja·en 67줄)를 세 폰트의 cmap에 넣어 봤다.

- ja·en — **결손 0**
- ko — **결손 1종**: `·` U+00B7 가운뎃점. Do Hyeon에 글리프가 없다
  (`topics/edo-night-soil/script.md` "상·중·하 등급" 줄). Do Hyeon의 한글은 2,437자로
  KS X 1001 상용 음절 범위이고 실사용 835자는 전부 덮는다 — 문제는 한글이 아니라 부호다.
  같이 없는 부호: `–` `—` `…`

## 가로 폭 (정규화 뒤, 44px 기준, 안전폭 1080 − 45×2 = 990px)

| 폰트 | 글자 폭 ÷ 폰트 크기 | 계약 가정 | 석빙고 실편 최장 줄 |
|---|---|---|---|
| Do Hyeon (한글) | **0.768** | 1.0 | 607px / 21자 |
| LINE Seed JP (가나·한자) | 1.000 | 1.0 | 924px / 21자 |
| Google Sans Bold (영문 실문장) | 0.501 | 0.5 (`locales.en`) | 906px / 41자 |

ja·en은 계약 그대로다. ko만 계약보다 **좁아서** 넘칠 일이 없다 — 22자 상한에서 743px이라
안전폭에 247px이 남는다. 대신 같은 44px에서 한국어 줄이 일본어 줄보다 눈에 띄게 짧게 찬다.

줄 간격은 정규화 뒤 `Fontsize × 1.0`이다(실측). 두 줄 자막의 잉크 사이 여백은 ko 10px ·
ja 3px · en 2px로 **겹치지는 않지만 en이 빡빡하다** — ADR-0064 되돌릴 조건 3.
