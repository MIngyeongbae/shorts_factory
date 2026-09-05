# ADR-0086: 채널 룩을 **실사에 가까운 3D 렌더**로 바꾸고, 배경 없는 `studio` 무대를 **계측 씬 전용**으로 좁힌다

- 상태: **승인** (2026-08-31, 사람 — "승인할게")
- 날짜: 2026-08-31
- 관련 스펙: `specs/03-visual-rules.md`, `specs/schema/vocab.json`(`meta.style`·`meta.staging`·`meta.video_line.local`), `specs/schema/beat-defaults.json`, `src/shorts_factory/prompts/03-scenetable.md`
- 관련 ADR: **부분 개정** — ADR-0083(`local`이 `art`의 회화체 `ttv_style`을 그대로 진다),
  ADR-0075 결정 7(라인이 자기 룩을 진다). **유지** — ADR-0034(출처는 하나),
  ADR-0044(판정은 사람 판독), ADR-0075 결정 2(정보는 표시가 아니라 구도가 진다)

## 맥락

### 1. 사람 판독이 회화체를 기각했다 (2026-08-31)

쇼츠 36편을 드라이브에서 본 사람의 판단이다:

> *"지금 영상의 그림체가 너무 그림체야. 거의 실사에 가까운 그림으로 그렸으면 좋겠고
> 배경이 다 없네."* / *"배경이 보통 있는게 더 그림이 좋아보이던데"*

ADR-0044가 정한 대로 **판정은 사람 판독이 한다.** 이 프로젝트에 LLM 채점 게이트가 없는
이유가 그것이고, 룩은 기계 지표로 판별되는 값이 아니다.

### 2. "그림체"는 우연이 아니라 계약 문자열이다

지금 도는 `local` 라인은 `ttv_style`을 갖고 있고(ADR-0083), 그 문자열은 `art.ttv_style`과
**글자 그대로 같다**:

> *"The whole clip is a **painted illustration** — gouache and watercolour laid over a fine
> graphite underdrawing, opaque body colour in the lights and transparent washes in the
> darks. …"*

즉 **지금 쓰는 두 라인이 다 회화체이고, 전역 `style.base_style`(세미 스타일라이즈드 3D)은
어느 라인도 안 쓴다.** 사람이 본 36편은 전부 이 문자열의 산출물이다.

### 3. 극사실 문자열은 2026-08-26에 **의도적으로** 걷어냈다 — 그 근거는 `local`에 안 걸린다

`vocab.json` `_mj_style` 주석과 `specs/03-visual-rules.md`(54~58줄)에 근거가 남아 있다:
앵커 세트 14장이 전부 회화이고 사실:그림 무게중심이 3:7이며 **프롬프트에 `photoreal`을
명시한 장조차 결과가 회화로 나왔다**는 실측 때문에 `photorealistic 3D render ·
physically based materials · ray traced`를 뺐다.

**그 실측은 MJ + 앵커 참조 경로의 것이다.** `local` 라인은 앵커도 `--sref`도 프레임도 받지
않고 **문자열 하나가 룩을 정한다**(ADR-0075 결정 3 — 프레임이 없으면 프롬프트가 전체 골격).
그러므로 "앵커가 회화라 계약이 렌더를 말하면 둘이 싸운다"는 탈락 사유는 `local`에 적용되지
않는다. 이 ADR이 되돌리는 것은 **`local`의 룩**이고, `art`는 건드리지 않는다.

### 4. 배경이 없는 이유도 계약이다 — 그런데 **절반은 그럴 이유가 있다**

`staging.studio.phrase`가 문자 그대로 배경을 지운다:

> *"…on a flat neutral pale gray studio ground with soft contact shadows,
> **clean empty studio space around it**, seen from a low three-quarter angle."*

run 24개 399씬 실측:

| 무대 | `info`(계측 표시) | 씬 | 비율 |
|---|---|---|---|
| `studio` | **있음** | 154 | 38.6% |
| `studio` | 없음 | **80** | **20.1%** |
| `location` | 있음 | 30 | 7.5% |
| `location` | 없음 | 135 | 33.8% |

`studio`가 **58.6%**(234/399)인데, **그중 154씬은 계측 표시가 붙는 씬**이다 — 빈 바닥은 거기서
값을 한다(스펙 03: *"프로브에서 치수선·라벨이 가장 잘 붙은 무대"*). 배경이 없을 이유가 없는
것은 **나머지 80씬(전체의 20.1%)** 이다. 그러므로 무대를 통째로 바꿀 일이 아니라 **고르는
기준을 좁히는** 문제다.

또 하나: **`staging` 빈칸은 399씬 중 0개**다. `beat-defaults.json`의 `fallback.staging`
(`studio`)은 **한 번도 발동한 적이 없다** — 실제 손잡이는 `[3s]` 세션 지시문
(`prompts/03-scenetable.md` 80줄)이다.

## 결정

**채널 룩을 실사에 가까운 3D 렌더로 바꾸고, `studio`를 계측·단면 씬 전용으로 좁힌다.**
① `meta.style.base_style`을 실사 3D 문자열로 교체하고, ② `meta.video_line.local`의
`ttv_style`을 **삭제해 전역 `base_style`로 되돌린다**(주석이 이미 *"이 키를 지우면 전역
base_style로 돌아간다"*고 적어 둔 경로다 — 출처가 하나가 된다, ADR-0034). ③ `[3s]`의 무대
선택 기준을 **`info`가 붙는 씬과 단면·도해 구도에만 `studio`**로 좁히고 나머지는 `location`을
기본으로 삼는다. ④ `beat-defaults.json`의 `fallback.staging`을 `location`으로 맞춘다.
⑤ **`art` 라인의 `mj_style`·`ttv_style`은 건드리지 않는다** — MJ 47단어 예산과 프레임 규약이
얽혀 있고 지금 기본 라인이 아니다. 다시 쓸 때 별도 ADR로 정한다.

## 검토한 대안

| 대안 | 장점 | 단점 | 탈락 사유 |
|---|---|---|---|
| **A. `local.ttv_style`만 실사로 바꾸고 전역 `base_style`은 둔다** | 변경 범위가 한 키 | 같은 룩이 두 곳에 적히고 `api` 라인은 옛 3D 문자열로 남는다 | **출처가 둘이 된다** (ADR-0034 §4 — 코드가 어느 쪽이 계약인지 모르게 된다). 주석이 안내하는 "키를 지우면 전역으로 회귀"가 더 싸다 |
| **B. `art`까지 같이 실사로 바꾼다** | 두 라인의 룩이 계속 일치한다 (ADR-0083의 취지 유지) | `mj_style`은 **47단어 예산 고정**이라 길이가 바뀌면 이미 만든 promptplan이 예산에서 떨어지고(ADR-0069), MJ는 앵커 실측에서 `photoreal`을 무시하고 회화를 냈다 | **근거가 반대 방향이다.** MJ 경로에서는 극사실 지시가 실측으로 기각됐다. 지금 안 쓰는 라인을 미검증 문자열로 바꾸는 비용이 이득보다 크다 |
| **C. `staging.studio.phrase`에 배경을 넣는다** | 무대 하나만 고치면 234씬이 다 바뀐다 | 계측 씬 154개에서 빈 바닥이 사라진다 | **`studio`의 존재 이유를 지운다** — 그 무대는 치수선·라벨이 가장 잘 붙어서 고른 것이다(스펙 03). 사람이 지적한 건 배경 없는 20.1%이지 계측 씬이 아니다 |
| **D. `beat-defaults.json`의 fallback만 `location`으로 바꾼다** | 한 줄 | `staging` 빈칸이 **399씬 중 0개**라 아무 일도 일어나지 않는다 | **실측이 무효를 증명한다.** 이 ADR에서는 방향을 맞추는 정합성 수정으로만 포함한다 |
| **E. 아무것도 안 바꾸고 앵커 세트를 다시 고른다** | 계약 변경 0 | 앵커는 룩의 *출처*일 뿐이고, 사람이 기각한 것은 그 출처에서 나온 **결과물**이다 | 사람이 이미 산출물 36편을 보고 판단했다. 앵커를 바꿔도 같은 판단을 다시 받게 된다 |

## 결과

### 바뀌는 것

- **`specs/schema/vocab.json`**
  - `meta.style.base_style` — 회화 아님·세미 스타일라이즈드 아님. 실사 3D로 교체:
    > `photorealistic 3D render with physically based materials and ray traced lighting. STYLE: real surface microdetail — grain, wear, weave and tool marks on every material, accurate reflection and roughness, natural daylight with a clear sun direction and true cast shadows, no sun disc in frame, shallow depth of field on foreground detail. COLOR IS RICH AND CLEAN: fully saturated, no gray wash and no desaturated grading`
  - `meta.style._role` — "fal 프로브 15클립이 이 문자열로 찍혔다"는 근거가 이 교체로 만료된다. 재검증 문장으로 갱신
  - `meta.video_line.local.ttv_style`·`_ttv_style` **삭제** → 전역 회귀
  - `meta.staging.studio.gloss` — "계측(`info`)이 붙는 씬과 단면·도해 구도"로 좁힘
- **`specs/schema/beat-defaults.json`** — `fallback.staging`: `studio` → `location` (정합성)
- **`src/shorts_factory/prompts/03-scenetable.md`** 80줄 — 무대 선택 기준 교체
- **`specs/03-visual-rules.md`** 32~58줄 — 룩 서술과 `studio` 설명 갱신, ADR-0083과의 관계 명기
- **코드 변경 없음** — 값은 전부 schema에서 로드된다 (ADR-0034)
- **비용 변경 없음** — 문자열 교체이고 `local` 라인은 변동비 0

### 적용 범위

**이미 만든 36편과 `[5]`를 이미 지난 3편(장경판전·각궁·비상구)은 그대로 둔다** (사람 결정,
2026-08-31 — *"이번 소재는 그냥 지금처럼 두고 다음소재부터 하자"*). `[7]`은 `[5]`가
`prompts.json`에 적어 둔 `style.base_style`을 읽으므로 어휘를 지금 고쳐도 그 세 편은
흔들리지 않는다.

**단 `자유의 여신상`은 새 룩으로 간다** (사람 결정, 2026-08-31 — 이 편만 `[5]` 전이라
재작업 비용이 0이다). **이 편이 ADR의 첫 프로브다.** 옛 편을 새 룩으로 다시 만들려면 `[5] --force` → `[7]` 재실행이고
편당 약 75분이다.

### 재검증 — 1차(기계 지표) 통과, **사람 판독 대기** (2026-08-31)

두 문자열 주석이 다 *"글자 하나를 바꾸면 재검증이 필요하다"*고 적어 두었다. 새 룩의 첫 편
(`liberty-patina`, run `20260831-liberty-patina`)을 **프로브로 취급**했다.

| 편 | 씬 | `studio` | `location` | `info` | **계측이 없는 `studio` 씬** |
|---|---|---|---|---|---|
| janggyeong-panjeon (옛 룩) | 18 | 11 (61.1%) | 7 | 7 | **6** |
| exit-sign-pictogram (옛 룩) | 17 | 10 (58.8%) | 7 | 5 | **5** |
| gakgung-horn-bow (옛 룩) | 18 | 9 (50.0%) | 9 | 8 | 1 |
| **liberty-patina (새 룩)** | 16 | **5 (31.2%)** | **11 (68.8%)** | 5 | **0** |

- **무대 기준이 먹었다.** 새 룩 편의 `studio` 5씬은 **전부 계측 씬**이다 — 빈 배경이 남은
  자리가 그 값을 하는 씬뿐이다. 이 ADR이 노린 상태다
- **되돌릴 조건 2 미발동** — `info` 강등 **0**. 기준선(2026-08-29~30 `art` 완주 8편의 info
  강등 0)과 같다. 실사 재질·피사계심도가 빨간 계측 표시를 깎지 않았다
- **되돌릴 조건 3 미발동** — `[7]` 재생성률 **1.00회/씬**(16씬/16호출). 기준선 1.13보다 낮다.
  `location` 비중이 68.8%로 올랐는데도 검수 실패가 늘지 않았다
- `[5]`가 `prompts.json`에 적은 STYLE 절이 새 실사 문자열임을 확인했다

**남은 것은 사람 판독이다** — 기계 지표는 "표시가 살아 있는가"만 말하고, 룩 자체의 판정은
ADR-0044대로 사람이 한다. 되돌릴 조건 1이 그 자리다.

### 되돌릴 조건

1. **사람 판독이 실사 룩도 기각하면.** 그때는 문자열이 아니라 앵커·라인 선택으로 올라간다
2. **`info` 씬의 계측 표시가 눈에 띄게 약해지면.** 기준선은 2026-08-29~30 배치 `art` 완주
   8편의 **info 강등 0**이다. 실사 재질·피사계심도가 빨간 치수선의 판독을 깎으면
   ADR-0075 결정 2(정보는 구도가 진다)가 흔들린다 — `studio` 씬만 회화체로 되돌리는 것이
   그때의 후퇴선이다
3. **`location` 비중이 오르며 `[7]` 재생성률이 오르면.** 현재 실측 **1.13회/씬**(62씬/70회)이
   기준선이다. 배경을 그리는 씬이 늘어 이 값이 올라가면 GPU 시간이 그만큼 늘어난다
