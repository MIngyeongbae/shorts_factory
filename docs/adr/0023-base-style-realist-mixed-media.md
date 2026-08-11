# ADR-0023: 베이스 스타일은 펜선·수채 위의 극사실 건축 렌더링이다

- 상태: 승인
- 날짜: 2026-08-11
- 관련 스펙: specs/03-visual-rules.md
- 관련 ADR: ADR-0002(2계층 텍스트), ADR-0005(모델), ADR-0006(모션), ADR-0019(레이어 A 폐기), ADR-0021(호출 경로)

## 맥락

기존 베이스 스타일은 `photorealistic 3D 디오라마/조감 렌더 스타일, 자연광`이었다.
원본 채널(신비한 건축사전) 3편 실측에서 가져온 값이다 (docs/reference-analysis.md).

사람이 다른 룩을 원했다. 레퍼런스 이미지 14장을 받아 공통점을 뽑았다 —
**펜/라이너 선 + 투명 수채 워시, 흰 종이, 작도선을 지우지 않고 남김, 중앙만 밀도 있게
그리고 가장자리로 갈수록 맨 종이로 사라짐, 부분 채색.** "색연필"이라 불렀지만 실제
기법은 색연필이 아니라 펜선 + 수채였다. 프롬프트 문구가 결과를 가르므로 이 구분이 중요하다.

### 실호출 6회로 확인했다 (편당 $0.101, 합계 $0.606)

앵커 3장(`assets/style_anchors/`)을 붙이고 **실제 대본의 씬을 그대로** 뽑았다.

| 탐침 | 씬 | 확인한 것 |
|---|---|---|
| 01 | 후버 3 `section_diagram` | **단면도가 성립한다.** 4번째 앵커가 필요 없다 |
| 02 | 피사 1 `drone_wide` | 조감 성립. `subject`가 특정하지 않으면 일반 건물이 나온다 → ADR-0022 |
| 03 | 후버 7 `detail_closeup` | 근접 성립 (최다 빈도 구도) |
| 04 | 후버 27 `present_wide` | **이름을 대면 실물이 나온다** (아치 곡률·협곡·발전소·송전탑) |
| 05·06 | 03·04와 동일 씬 | 사실주의 비중을 올린 A/B. **채택** |

**05·06에서 재질이 살아났다** — 콘크리트 골재 알갱이, 거푸집 나뭇결, 협곡 암반 층리,
저수지 수위선(bathtub ring). 03·04는 같은 대상이 뭉개진 워시 덩어리였다.

**6장 전부에서 하단이 비었다.** 스펙 03의 "하단 1/3은 자막 영역으로 비움"이 이 기법에서
특히 잘 맞는다 — 원래 여백으로 끝내는 그림이라 억지로 비우지 않아도 비워진다.
**6장 전부 글자가 없다.** 낙서 금지 네거티브가 통했다.

## 결정

베이스 스타일을 아래로 교체한다. **실호출로 검증된 문구 그대로다.**

```
photorealistic architectural rendering on white paper, mixed media:
the focal subject rendered with true-to-life detail, materials and lighting;
colour laid in as controlled watercolour washes over fine graphite and
technical-pen underdrawing, with construction and guide lines left visible;
realism dominant over painterly looseness;
detail and colour fall away toward the edges into bare paper
```

**자막 색을 뒤집는다.** 흰 종이 위에 흰 글자는 외곽선만 남는다.
`흰 글자 + 검정 외곽선` → **`검정 글자 + 흰 외곽선`**. 굵기·크기·위치는 그대로다
(40px / 1줄 22자 / 2줄 / 하단 72~82%).

**앵커는 3장으로 확정한다** (`assets/style_anchors/`). 손글씨 낙서가 들어간 4장과
흰 종이 전제가 깨지는 야경 2장을 뺐고, 거리별로 하나씩 남겼다 —
`anchor-01-monument-frontal`(단일 구조물 근경) ·
`anchor-02-cluster-midrange`(중경·지형) · `anchor-03-aerial-wide`(광역 조감).

**네거티브에 "읽을 수 있는 글자·손글씨 낙서 금지"를 추가한다.** 레퍼런스 일부에
필기체 텍스처가 있었고, 그대로 두면 모델이 한국어를 흉내 내다 깨진 글자를 만든다 (ADR-0002).

## 검토한 대안

| 대안 | 장점 | 단점 | 탈락 사유 |
|---|---|---|---|
| **기존 포토리얼 3D 디오라마 유지** | 원본 채널 실측값, 검증된 포맷 | 차별화가 없다 | 사람이 다른 룩을 택했다. 채널 정체성은 사람 판단 영역이다 |
| **색연필 그대로 해석** | 사람이 처음 말한 단어 | 레퍼런스와 다르다 | "colored pencil"은 왁스질감의 불투명한 스트로크를 낸다. 레퍼런스는 펜선 + 투명 수채였다 |
| **수채 비중을 더 높인 03·04 버전** | 색이 풍부하고 부드럽다 | 재질이 뭉개진다 | 골재·나뭇결·암반 층리가 사라진다. **소재가 구조물·공법이라 재질이 곧 설명이다** (ADR-0022) |
| 야경 앵커 포함 | 밤 장면 표현 가능 | 흰 종이 전제가 깨진다 | 짙은 남색 풀블리드가 스타일을 어두운 쪽으로 끌고 간다. 밤 장면이 필요해지면 그때 재검토 |

## 결과

**바뀌는 것**

- `specs/03-visual-rules.md` — 베이스 스타일, 자막 색
- `src/shorts_factory/schemas/visual_rules.py` — `BASE_STYLE`
- `src/shorts_factory/video/subtitles.py` — `PRIMARY_COLOUR` / `OUTLINE_COLOUR` 반전
- `assets/style_anchors/` — 앵커 3장 (커밋 대상. `.gitignore`에 예외가 있다)

**바뀌지 않는 것**

- **구도 토큰 21개 전부 유지.** `drone_wide`·`subject_closeup`·`section_diagram` 등은
  카메라 프레이밍이지 렌더 스타일이 아니다. 다시 만들 필요가 없었다
- 9:16 / 2K / 하단 1/3 비움 / 자막 크기·줄수·위치
- 비용 (ADR-0021의 편당 $2.53~2.73)

## 되돌릴 조건

- **`[7] motion`에서 Kling i2v가 선을 흔들면**(frame-to-frame line boiling) 재검토한다.
  가는 펜선 + 작도선 + 수채 번짐은 i2v 시간축 안정성에 가장 불리한 조합이다.
  **아직 측정하지 못했다.** Ken Burns는 정지 이미지라 영향이 없으므로, 최악의 경우
  ADR-0006의 하이브리드 비율을 Ken Burns 쪽으로 옮기는 것이 먼저다
- **씬 간 색 온도 편차**가 한 편에서 튀면 팔레트를 프롬프트에 못박는다.
  탐침 4장에서 회백 → 테라코타 → 회청 → 파스텔로 흔들렸다. 피사체 색을 따라간 것이라
  스타일 불일치는 아니지만, 100초에 25~27장이 연달아 지나가므로 관측 대상이다
- **`sparkle_particles` 오버레이가 흰 배경에서 안 보일 가능성이 크다.** 남은 레이어 B
  오버레이 2종 중 하나다. **측정하지 않았으므로 지금 지우지 않는다** — `[8] overlay`를
  만들 때 실물로 확인하고 그때 결정한다
