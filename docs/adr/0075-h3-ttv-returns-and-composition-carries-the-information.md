# ADR-0075: `art` 라인의 `info` 씬을 H3 텍스트→영상으로 돌린다 — 정보는 표시가 아니라 **구도**가 지고, 룩은 앵커의 회화체다

- 상태: 승인
- 날짜: 2026-08-26
- 관련 스펙: specs/03-visual-rules.md (「베이스 스타일」·「스타일을 무엇이 지는가」·「프롬프트 골격」), specs/05-pipeline.md (`[6]`·`[7]`·「비용 기준선」), specs/02-beat-schema.md (`info`), specs/schema/vocab.json (`style.base_style`·`video_line.art`·`camera._info_still`·`annotation`), specs/schema/scene.schema.json (`info`)

## 맥락

ADR-0069~0072가 쌓아 만든 `art` 라인이 2026-08-26에 **처음으로 한 편을 끝까지 냈다.**
그 결과물을 사람이 보고 **`info` 씬의 경로만 잘라 내는 결정**을 내렸다. 라인 자체도,
`local`·`api` 라인도 그대로 간다 — 이 ADR이 손대는 것은 `art` 라인 안에서 `info` 씬이
그려지는 방식 하나다.

### 1. 기계 지표는 좋았고 사람 눈은 반대였다 (사람 판독 2026-08-26)

`japan-5060hz` 18씬(`info` **12씬**)을 `art` 라인으로 완주한 실측:

| | 값 |
|---|---:|
| `[6] frames` (MJ CLEAN 18장 + NB2 INFO 12장) | 39분 · 강등 0 · 사분면 교체 2 |
| `[7] videogen` (일반 씬 MJ endImage + `info` 씬 H3 fl2v) | 79분 · 호출 26회(150초) · 강등 3 |
| 합계 벽시계 | **118분** |
| NB2 종량 호출 | `info` 씬 수만큼 = **12회** |

**18씬 중 15씬이 무강등이다.** 직전 `local` 라인 실행이 17씬을 재사용으로 때우고 멈춘 것과
비교하면 지표는 완승이다. 그런데 사람 판독은 정반대였다:

> *"결과가 많이 아쉽네. (…) 그냥 h3가 잡을 때가 훨씬 질이 좋았어."* (사람, 2026-08-26)

**강등 개수는 품질의 대리 지표가 아니었다.** `art` 라인의 강등이 적은 이유는 그림이 좋아서가
아니라 정지 이미지가 검수 기준(표시가 대상을 실제로 가리키는가)을 쉽게 통과하기 때문이다.
ADR-0044 이래 우리는 LLM 채점 게이트를 두지 않고 **사람 판독**을 최종 심급으로 삼아 왔다
(스펙 05 「확정된 결정」). 그 심급이 답을 냈다.

**폐기 대상은 라인이 아니라 경로다** (사람 정정 2026-08-26):

> *"art 라인 폐기는 아니야 인포그래피 그림 빼고는 그대로 mj로 돌고 인포만 h3로 돌아 그게
> art라인이야, h3 같은 로컬만 도는 라인, 유료 라인은 유효해"*

즉 **일반 씬의 MJ는 그대로 좋았다.** 무너진 것은 `info` 씬을 [MJ CLEAN → NB2가 빨간 표시
편집 → H3 first/last 보간]으로 만드는 경로 하나다.

### 2. `info` 씬의 정보를 **빨간 줄 하나가 전부 지고 있었다** (사람 관찰 + 계약 확인)

> *"인포가 너무 빨간 줄 밖에 없어. 정말 인포그래피로 여러가지 정보를 시각적으로 표현해줬으면
> 좋겠는데."* (사람, 2026-08-26)

계약을 열어 보면 관찰이 정확하다. `vocab.json`의 `annotation` 어휘는 **세 종류뿐**이고
(`dimension`·`arrow`·`leader`) 셋 다 문구가 `pure red glowing … small red label box with
white text`로 끝난다. `_closing`은 한술 더 떠 **"그 빨간 표시가 프레임에서 유일한 채도 높은
빨강이자 유일한 글자"**라고 못박는다.

즉 우리 계약에서 `info` 씬이란 **평범한 그림 + 빨간 주석**이었다. 그림 자체가 정보를 지는
경로가 계약 어디에도 없었다. 스펙 03이 *"SUBJECT는 설명의 무대다 — 비교면 나란히 놓고,
단면이면 절단면에 무엇이 보이는지"*라고 이미 적어 두었지만, **고를 어휘가 없어** 세션이
그것을 실행할 손잡이가 없었다.

### 3. 새 앵커 14장이 그 답을 들고 있다 (실측 2026-08-26)

사람이 `엥커래퍼런스/`에 MJ 이미지 14장을 넣었다. 파일명이 `framing` 어휘 토큰과 1:1로
대응한다(`aerial_drone_establishing`·`cross-section_cutaway`·`frontal_close-up`·
`explanatory_diagram`·`wide_view_of_the_whole_problem` …) — **구도 어휘 전체를 한 소재
(파나마 운하·송유관)로 훑은 세트**다. 전수 판독 결과:

**(가) 빨간 지시선·라벨 상자가 14장 중 0장이다.** 유일한 붉은 선형 그래픽은 박물관 장의
벽화 지도에 그려진 항로선인데, 이것은 오버레이가 아니라 **그림 속 지도에 원래 그려져 있는
것**이다. 그런데도 14장 전부가 무언가를 설명한다.

**(나) 정보를 구도가 지는 장치가 11종 나온다.** 절단면을 색·명도로 분리하고 흐름선이 그 면을
가로지른다(절벽 단면) / 벽화 지도 + 항로선 + 실물 범선 모형 + 액자 도판을 한 방에 배치한다
(Drake Passage) / 드러난 갯벌 침식 무늬가 옛 수위선을 그리고 줄 선 배가 정체를 세게 한다
(Gatun Lake) / 갑문 속 배가 끼워맞춤 여유를 보인다 / 준설선이 소실점까지 늘어서 규모를 주고
계단형 벤치가 단면 프로파일을 노출한다(1880 굴착) / 전신주가 일정 간격으로 거리를 센다
(Tapline) / 인물은 항상 작고 오직 축척용이다.

**(다) 14장 전부 그림이다.** 사실:그림 비의 무게중심이 **대략 3:7**이고, 사진처럼 읽히는 4장은
전부 원거리 + 대기 헤이즈가 많은 장이다 — 같은 화법이 거리에 따라 다르게 읽히는 것이지
실사 장이 따로 있는 것이 아니다.

**(라) 결정적 반례**: `Gatun_Lake` 장은 프롬프트에 **`present-day_photoreal`이 명시돼 있는데도**
결과가 회화다. 2026-08-26 오전에 고른 `art` 라인 룩
(`photorealistic 3D render, physically based materials … ray traced global illumination`,
47단어)은 **이 앵커 세트가 실제로 그리는 것과 다른 물건**이다.

### 4. 정지 문구는 MJ의 병 때문에 생긴 약이다 (ADR-0072 결정 4)

`camera._info_still`(*"Absolutely no motion … This is a still photograph held on screen"*)이
들어간 이유는 하나다: **MJ `endImage`가 그림을 가만히 두지 못해** 지시선이 가리키던 지점이
프레임 사이에서 이동하고 표시가 어긋났다. ADR-0071의 역할 분담(표시의 정확성은 정지 이미지가
지고 영상 모델은 잇기만 한다)이 그 처방의 근거였다.

**`info` 씬이 텍스트→영상으로 가면 그 원인이 사라진다.** 지킬 정지 이미지가 없으므로 지킬
정합도 없고, `[3s]`가 씬마다 고른 카메라 워크(ADR-0049)를 정지 문구가 덮을 이유가 없다.
사람이 같은 말을 했다:

> *"MJ 가 그림을 가만히 두지 못해서 정지시켰는데 카메라샷이라던가 그림 움직이지 말라고했던
> 프롬프트는 H3로가면 제약 조건이니깐 빼버리고."*

### 5. 깊이 어휘 — 걷어내면 안 되는 것 하나

`vocab.json` `_base_style` 주석이 *"회화 매체 어휘로 쓰면 깊이 단서가 없어 영상 모델이 카메라를
못 민다 (실측 2026-08-25)"*고 경고한다. 이 경고를 무시하면 안 되지만, **앵커가 처방을 같이
들고 있다**: Tapline·Panama·1880 굴착은 전부 회화인데 깊이가 산다. 방법은 3D 렌더 어휘가
아니라 **층층 대기원근(멀수록 밝아지고 파래진다) + 강한 1점 수렴 + 조밀한 전경 앵커**다.
따라서 회화로 가되 **깊이 어휘를 명시적으로 싣는다.**

## 결정

**(1) `art` 라인의 `info` 씬을 H3 텍스트→영상으로 돌린다.** `video_line.art.info_provider`를
`comfy-h3-fl2v`(first/last) → **`comfy-h3`(TTV)**로 바꾼다. 그 씬은 프레임을 받지 않으므로
`[6]`이 만들 것이 없다. **일반 씬은 지금 그대로다** — MJ CLEAN 한 장을 `first_frame`으로
받는 `mj-endimage`. `local`·`api` 라인은 이 ADR이 손대지 않는다.

**(2) NB2 편집 경로를 폐기한다.** `[6] frames`는 **CLEAN만 만들고, 그것도 `info`가 없는
씬에서만** 만든다. INFO 산출·NB2 호출·INFO 검수 세션이 통째로 빠진다. `[6]`이 `info` 씬을
건너뛴 것은 강등이 아니라 **정상**이다 (`demoted_info`가 아니다).

**(3) 프롬프트를 엔진별로 나눈다 — 계약의 필드까지** (사람 지시 2026-08-26:
*"MJ로 보낼 프롬프트랑 h3에서 돌 프롬프트랑 나눠야겠어"*).

지금은 라인 단위 스위치(`style_in_frames`)가 프롬프트 골격을 통째로 정하고 `prompt` 한
필드가 그 결과를 담는다. `art` 라인이 **엔진 둘을 쓰게 되면서 그 한 필드가 씬마다 다른
물건**이 됐다. 규약이 엔진의 것이므로 계약에서 나눈다:

| 소비자 | 엔진 | 규약 | 필드 |
|---|---|---|---|
| `[6]` (일반 씬 CLEAN) | MJ imagine | **명사구 나열 한 줄** + `--ar`/`--no`, 47단어 예산 (ADR-0027·0069) | `mj_image_prompt` **(신설)** |
| `[7]` 일반 씬 | MJ `endImage` (프레임 입력) | **카메라 워크 구절 하나** (ADR-0072 결정 3) | `video_prompt` |
| `[7]` `info` 씬 | H3 **TTV** (프레임 없음) | **전체 골격** — FORMAT·STAGING·SUBJECT·CAMERA·RED·NEGATIVE | `video_prompt` |

- **`prompt` → `video_prompt`로 이름을 바꾼다.** 프롬프트가 둘이 된 이상 `prompt`는 "무엇의
  프롬프트인가"를 말하지 않는다. `prompt_shot2`도 `video_prompt_shot2`로 따라간다
- **`mj_image_prompt`는 `info`가 없는 씬에만 있다.** `info` 씬은 MJ를 안 타므로 이 필드가
  없는 것이 정상이다 (결정 1·2)
- **완성본은 `[5]`가 싣고, 원료도 함께 남는다.** 세션 단락(`subject_prompt`·`camera_target`·
  `red_prompt`·`mj_subject`)은 지금처럼 그대로 실린다 — `[6]`의 `15-cleanfix`와 `[7]`의
  `17-clipfix`가 **원료를 고쳐 다시 조립**하는 사다리를 쓰기 때문이다 (ADR-0067). 조립은
  코드가 어휘에서 로드해 한다 (ADR-0034)
- **검사가 각각 붙는다.** `mj_image_prompt`는 MJ 방언·예산 검사, `video_prompt`는 골격·ASCII·
  라벨 포함·착지 금지어 검사다. 지금은 `mj_subject` 하나에 예산 검사가 걸려 있어 **최종
  전송 문자열은 아무도 안 재고 있었다**

`info` 씬은 프레임이 없으므로 **스타일을 말로 해야 한다** — STYLE 절이 되살아난다.
ADR-0070의 "스타일 낱말을 실으면 중간 프레임이 무너진다"는 실측은 **프레임이 그림을 지는
경우**의 것이라 여기 적용되지 않는다 (지킬 프레임이 없다). `[7]`이 그 씬에만 FORMAT의 초
수를 채운다.

**(4) `camera._info_still`을 폐기한다.** `info` 씬도 `[3s]`가 고른 카메라 워크를 그대로 쓴다.
정지 문구와 그 분기를 계약·코드에서 걷어낸다.

**(5) 정보는 구도가 지고 표시는 보조다** (사람 선택 2026-08-26). `vocab.json`에 **정보 운반
장치 어휘 `info_device`**를 신설한다 — 앵커에서 뽑은 11종(절단면 색분리, 그림 속 지도 + 경로,
축척 모형, 셀 수 있는 반복, 축척 인물, 지형에 새겨진 전후, 끼워맞춤 비교, 정면 입면, 공정
사슬 한 프레임, 단면 프로파일 노출, 액자 도판 벽)이 출발 목록이다. `info` 씬은 그중 하나를
씬 계약에 싣고(`info.device`, **선택 필드** — 비면 지금과 같이 돈다), `[5]` 세션이 SUBJECT
단락에서 **그 장치를 실제로 무대에 세운다.** 빨간 표시는 그 위의 주석으로 격하된다.
`annotation` 어휘도 넓힌다(축척바·비교 괄호·콜아웃 원·강조 영역·흐름선).

**(6) 화면 라벨은 유지하되 짧게, 못 그리면 강등한다** (사람 선택 2026-08-26). `info.labels`
계약(ASCII·24자·최대 4개)과 `[7]`의 강등 사다리(RED 뺀 변종 → 미검수 채택)를 **그대로 둔다.**
NB2가 빠져 글자를 H3가 그리므로 품질이 내려가는 것은 감수한다 — 숫자는 내레이션·자막이
받친다(ADR-0002·0063). `[9]` 후처리 오버레이는 ADR-0071이 이미 기각한 경로라 되살리지 않는다.

**(7) 스타일 문자열도 엔진별로 나누고, 둘 다 앵커 기준 회화체로 다시 쓴다.**
`video_line.art.base_style` 하나를 **`mj_style`·`ttv_style` 둘로 쪼갠다** — 결정 3과 같은
이유다. 룩은 같은 앵커에서 나오지만 **쓰는 말이 엔진의 것**이다: MJ는 명사구 나열이고
콜론·세미콜론·줄바꿈을 구분자로 읽지 않으며(ADR-0027) 47단어 예산이 걸린다. H3 TTV는
서술형 절이고 그 예산이 없다. 전역 `style.base_style`과 `local`·`api` 라인은 건드리지 않는다.

`mj_style` — `[6]`의 MJ imagine 한 줄에 들어간다. **47단어**를 지킨다 (`mj_subject`가 이
길이의 나머지를 쓴다, ADR-0069):

```
painted illustration, gouache over graphite linework, ruled hairline detail on built
structures, muted earth and slate palette, one saturated accent, soft diffuse daylight,
no sun disc, three receding planes lightening and cooling with distance, dense foreground
against loose distant washes, full bleed edge to edge, paper grain
```

**정확히 47단어다** (옛 극사실 문자열도 47단어였다). 예산 안에 깊이 어휘를 넣느라 밀려난
것은 바위·수풀의 `broken scratchy contour` 하나다 — 앵커에서 더 진한 표식은 인공물 쪽의
`ruled hairline`이라 그쪽을 남겼고, 예산이 없는 `ttv_style`은 둘 다 들고 간다.

`ttv_style` — `[7]`의 H3 TTV FORMAT 절에 들어간다. 서술형이고 예산이 다르므로 명사구가
눌러 두었던 것을 문장으로 편다 (매체가 무엇 위에 얹히는지, 어느 면이 불투명하고 어느 면이
워시인지, 깊이를 무엇이 만드는지):

```
The whole clip is a painted illustration - gouache and watercolour laid over a fine
graphite underdrawing, opaque body colour in the lights and transparent washes in the
darks. Man-made structures carry ruled hairline linework; rock and foliage carry a broken
scratchy contour. The palette is muted earth and slate with exactly one saturated accent
reserved for the subject, and shadows are coloured rather than black. Light is soft
diffuse daylight with no sun disc and no specular gloss. Depth comes from three or more
receding planes that lighten and cool with distance, with dense foreground detail set
against loose distant washes. The frame is full bleed edge to edge over a fine paper grain.
```

걷어내는 어휘 (양쪽 공통): `photorealistic` · `3D render` · `physically based materials` ·
`ray traced global illumination` — 앵커 14장 어디에도 대응이 없고, `photoreal`을 명시한 장조차
회화로 나왔다 (맥락 3-라). 살리되 표현을 바꾸는 것: `clean readable forms` →
`ruled hairline detail`, `ambient occlusion` → `soft diffuse daylight`,
`edge to edge with no blank margins` → `full bleed edge to edge`. **새로 들어가는 것은 깊이
어휘다** (`three receding planes …`, `dense foreground detail against loose distant washes`)
— 맥락 5의 처방이고, 두 문자열에 **같은 내용으로** 들어간다.

**(8) `assets/style_anchors/` 3장을 교체한다.** 지금 들어 있는 3장은 ADR-0023의 폐기된 룩
(펜선·수채 위 극사실)이 기준이던 시절의 것이다. 새 3장은 색 계열·질감·거리를 서로 벌려
고른다 — 댐 여수로(중성·자로 그은 선·근중경) / Tapline 사막 도로(온난·마른 붓·1점 수렴) /
파나마 조감(채도·식생·원경). NB2가 빠지면서 **앵커를 첨부로 소비하는 어댑터는 없어진다**
(MJ는 `requires_style_anchors = False`) — 이 3장의 역할은 `base_style` 문자열을 쓸 때와
사람이 룩을 판독할 때의 **기준**이다.

## 검토한 대안

| 대안 | 장점 | 단점 | 탈락 사유 |
|---|---|---|---|
| **(범위) `art` 라인을 통째로 폐기한다** | 라인이 둘로 줄어 계약·코드가 단순해진다. NB2·MJ 종량·정액이 다 빠진다 | **일반 씬의 MJ는 사람이 좋다고 한 부분이다.** 그림체·카메라 워크가 H3보다 낫고 로컬 GPU를 안 쓴다 | 사람 정정(2026-08-26): *"art 라인 폐기는 아니야 (…) 그대로 mj로 돌고 인포만 h3로 돌아"* |
| **(info) MJ + NB2를 유지하고 룩만 회화로 되돌린다** | 계약 변경이 `base_style` 한 줄. 표시 정확성(강등 3/18)을 지킨다 | 사람이 아쉬워한 것은 룩만이 아니라 `info` 씬 **결과물 전체**였다. MJ가 그림을 못 잡아 정지 문구까지 넣은 구조가 남는다 | 사람 판독이 "h3가 훨씬 낫다"였다. 지표가 아니라 **최종 심급**이 답했다 (ADR-0044) |
| **(info) `info` 씬도 MJ CLEAN을 받아 H3 first/last로 잇되 NB2만 뺀다** | `[6]`이 그대로 돌고 그림체가 일반 씬과 완전히 일치한다 | 표시를 아무도 안 그린다 — CLEAN에는 계측 표시가 없다. 결국 `info` 씬이 일반 씬과 구별되지 않는다 | 결정 5·6이 요구하는 "구도가 정보를 지고 표시가 보조"를 **둘 다** 잃는다 |
| **(인포) 빨간 표시 어휘만 넓힌다** | 변경 범위가 `annotation` 블록 하나. 기존 검수·강등 사다리가 그대로 간다 | 사람 불만의 뿌리는 표시의 **종류**가 아니라 정보를 표시**만** 진다는 것이었다. 축척바를 더해도 여전히 "평범한 그림 + 빨간 주석"이다 | 앵커 14장이 빨간 표시 0장으로 설명을 해낸다 — 반례가 계약 밖에 있었다 |
| **(인포) 표시를 통째로 없애고 구도만으로 간다** | 가장 깨끗하다. H3의 글자 약점을 완전히 우회하고 검수가 가벼워진다 | "듣는 숫자와 보는 숫자를 맞춘다"를 포기한다. 화면에서 수치를 짚어 줄 길이 사라진다 | 사람 선택이 "넣되 짧게, 못 그리면 강등"이었다 (2026-08-26) |
| **(라벨) `[9]` 후처리로 굽는다** | 글자가 100% 정확하다. 엔진과 무관하다 | 정지 오버레이라 그림과 겉돈다. 지시선 좌표를 말에서 못 얻어 아무 곳이나 가리켰다 (46 실측) | ADR-0071이 이미 기각한 경로다. 되살릴 근거가 새로 생기지 않았다 |
| **(룩) 3D 렌더 어휘를 남겨 깊이를 지킨다** | 2026-08-25 실측("회화 어휘 → 깊이 단서 없음")에 안전하다 | 앵커가 회화인데 계약이 렌더를 말하면 **둘이 싸운다**. 오늘 오전의 극사실 문자열이 정확히 그 상태였다 | 앵커 자체가 처방을 들고 있다 — 깊이는 렌더 어휘가 아니라 대기원근·1점 수렴·전경 앵커가 만든다 (Tapline·Panama·1880 굴착) |
| **(분리) 스타일 문자열 하나를 두 엔진이 공유한다** | 룩이 한 자리에서 오므로 두 엔진의 그림체가 어긋날 수 없다. 계약이 짧다 | **규약이 다르다.** MJ는 콜론·세미콜론·줄바꿈을 구분자로 안 읽어 서술형을 넣으면 화면 지시로 섞이고(ADR-0027), 반대로 명사구 나열을 H3 TTV에 넣으면 STYLE 절이 문장으로 안 선다. 47단어 예산도 MJ만의 것이다 | 사람 지시(2026-08-26): *"MJ로 보낼 프롬프트랑 h3에서 돌 프롬프트랑 나눠야겠어."* 한 문자열로는 한쪽이 반드시 규약 밖이다 |
| **(분리) 스타일만 나누고 조립은 소비 시점에 둔다** | 변경 범위가 `vocab` 블록 하나. `prompts.json` 모양이 안 바뀌어 기존 산출물이 산다 | **최종 전송 문자열을 아무도 안 잰다.** 지금도 예산 검사가 `mj_subject`에만 걸려 있어 조립 뒤 길이는 계약 밖이었다. 어느 문자열이 어느 엔진으로 가는지도 계약에서 안 보인다 | 사람 선택이 "산출물까지 엔진별로"였다 (2026-08-26). 계약이 정본이라는 원칙과도 맞는다 (ADR-0034) |
| **(룩) 47단어 예산을 푼다** | 깊이 어휘를 넉넉히 넣을 수 있다 | MJ는 일반 씬에서 계속 돌고 `mj_subject` 예산은 이 길이의 나머지다 (ADR-0069). 길이가 바뀌면 이미 만든 `promptplan`이 전부 예산에서 떨어진다 | **제약의 근거가 살아 있다.** 47단어 안에서 깊이 어휘가 들어가므로 풀 이유가 없다 |

## 결과

**바뀌는 것**

- `specs/schema/vocab.json` — `video_line.art.base_style`을 **`mj_style`·`ttv_style` 둘로
  쪼개고 둘 다 회화체 + 깊이 어휘로 교체**, `video_line.art.info_provider` → `comfy-h3`,
  `style_in_frames`의 정의를 **씬 단위**로 좁힘, `camera._info_still`·`_info_still_note` 제거,
  **`info_device` 어휘 신설**, `annotation`에 표시 종류 추가
- `specs/schema/scene.schema.json` — `info.device` **선택 필드** 추가 (`info_device` 어휘 참조).
  비면 지금과 같이 돈다 — 기존 `scenes.json`이 깨지지 않는다 (D-3)
- `specs/schema/prompts.schema.json`·`promptplan.schema.json` — **`prompt` → `video_prompt`,
  `prompt_shot2` → `video_prompt_shot2` 이름 변경**, **`mj_image_prompt` 신설**(`info` 없는
  씬만). MJ 방언·47단어 예산 검사를 `mj_subject`가 아니라 **최종 전송 문자열**에 건다.
  **기존 `prompts.json`은 전부 무효가 된다** — `[5]`를 다시 돌려야 한다 (변동비 0)
- `specs/03-visual-rules.md` — 「베이스 스타일」의 `art` 룩이 회화체로, 「스타일을 무엇이
  지는가」가 **씬 단위 분기**로, 「프롬프트 골격」의 `info` 씬 정지 분기 제거, **「정보를
  무엇이 지는가」 절 신설**
- `specs/05-pipeline.md` — `[6]`이 **`info` 없는 씬의 CLEAN만** 만든다(INFO·NB2·INFO 검수
  삭제), `[7]`의 `info_provider` 서술과 프롬프트 규약 분기 정리, 「비용 기준선」에서 NB2 줄 삭제
- `specs/02-beat-schema.md` — `info.device` 설명
- `src/shorts_factory/prompts/05-prompt.md` — SUBJECT 지침에 정보 운반 장치를 싣는다
  (어휘에서 로드한다 — 프롬프트가 목록을 손으로 적지 않는다, ADR-0034)
- `schemas/visual_rules.py`·`stages/prompt.py`·`stages/frames.py`·`stages/videogen.py` —
  위 계약을 로드해 조립. 정지 문구 분기 제거, `style_in_frames` 분기를 씬 단위로
- `assets/style_anchors/` — 3장 교체 (커밋 대상. `.gitignore`에 예외가 있다)
- **`[5]`를 다시 돌려야 한다** (구독 세션, 변동비 0, 편당 6~10분). `japan-5060hz`는
  `[6]`·`[7]`도 다시 돈다

**비용**

- **변동비가 준다.** NB2 종량이 **0이 된다** (`japan-5060hz`에서 12회였다). MJ 이미지 잡도
  `info` 씬 수만큼 준다 — 같은 편에서 CLEAN 18장 → **6장**
- **로컬 GPU가 는다.** `info` 씬이 H3 TTV로 가므로 클립당 165~255초 직렬이 12씬분 붙는다
  (스펙 05 「비용 기준선」). MJ 영상 잡은 그만큼 줄어 벽시계 합계는 비슷하거나 조금 준다
- **`[7]` 검수 세션 수는 늘 수 있다** — 정지 이미지가 표시를 지지 않으므로 기각이 늘어난다.
  ADR-0072의 사다리(`review_ladder`, 시도마다 잣대를 낮춘다)가 그것을 닫는 장치다
- **`[6]` 검수 세션은 준다** — INFO 검수가 통째로 사라지고 CLEAN 게이트만 남는다

**되돌릴 조건**

1. **H3 TTV가 `info` 씬을 여전히 못 그리면** — worklog(45)의 실패(50Hz·60Hz 사인파를 같은
   파장으로 그린다, 좌/우안 지시선 반전)가 결정 5의 장치 어휘를 넣고도 재현되는지 첫 편에서
   본다. 재현되면 문제는 프롬프트가 아니라 엔진이고, `info` 씬만 `api`(Omni)로 보내는 길을
   잰다 — `info_provider`가 이미 씬 단위 분기라 자리는 있다
2. **회화 문자열에서 카메라가 안 밀리면** — 2026-08-25 실측이 되살아난 것이다. 깊이 어휘를
   더 세게 쓰기 전에 **앵커의 방법**을 먼저 확인한다: 1점 수렴 구도와 전경 앵커가 SUBJECT
   단락에 실제로 들어갔는지. 그래도 안 되면 렌더 어휘를 일부 되살린다
3. **일반 씬(MJ)과 `info` 씬(H3)의 그림체가 편 안에서 튀면** — 결정 7이 문자열을 둘로 나눴으므로
   **둘이 어긋날 자리가 생겼다.** 같은 앵커에서 썼어도 엔진이 다르면 결과가 갈린다.
   한 편을 사람이 통으로 보고 판단한다. 튀면 순서는 ① 두 문자열의 낱말을 맞춰 본다
   (한쪽에만 있는 어휘가 원인인지) → ② 그래도 튀면 `info` 씬도 MJ CLEAN을 first로 받는
   길(대안 표 3행)을 다시 잰다
4. **H3가 라벨 글자를 못 읽히게 그리면** — 결정 6의 강등 사다리가 얼마나 자주 발동하는지
   센다. `info` 씬의 절반을 넘으면 화면 라벨을 포기하는 쪽(대안 표 5행)을 다시 묻는다
5. **`info.device`가 세션에게 손잡이가 안 되면** — 씬 계약에 장치가 실려도 SUBJECT가 그것을
   무대에 못 세우면 어휘만 늘고 그림은 그대로다. `[5]` 산출을 사람이 읽어 확인한다
6. **MJ가 그림을 잡게 되면**(모델 갱신) — 결정 1·4를 재검토한다. 판정은 ADR-0072의 빨간
   화소 추이와 **사람 판독** 둘 다로 한다 — 이번에 지표만 봤다가 뒤집혔다
7. 앵커가 다시 바뀌면 — `base_style`은 앵커의 유도값이므로 함께 다시 쓴다. 글자 하나를
   바꾸면 재검증이 필요하다는 규칙은 그대로다 (스펙 03)
