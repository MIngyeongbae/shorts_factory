# ADR-0087: `local` 라인의 고증용 실물 참조는 **로컬 SDXL + IP-Adapter가 그린 first frame**이 진다 — 노브는 `weight`가 아니라 `start_at`이고, **씬이 고른다**

- 상태: **되돌림** (2026-09-02, 사람 — *"영상에 문제가 있으면 나머지 쇼츠는 2부 진행을 멈춰"*). `local`의 `reference_frames`를 껐다. **코드·템플릿·어휘는 남긴다** — 되돌린 이유가 설계가 아니라 **엔진**이라서다(맥락 6). 승인은 2026-09-01, 사람 — `structure` 모드는 **뺀다**, 나머지는 승인. "승인 — structure 빼고 (identity + none)")
- 날짜: 2026-09-01
- 관련 스펙: `specs/05-pipeline.md`(`[6] frames`·`[7] videogen` I/O), `specs/03-visual-rules.md`,
  `specs/schema/vocab.json`(`meta.video_line.local`·`meta.style`·새 `$defs.reference_mode`),
  `specs/schema/scene.schema.json`(`$defs.scene`)
- 관련 ADR: **확장** — ADR-0077(실물 사진이 MJ 참조가 된다: `art`만), ADR-0071(`[6]`이 프레임을
  만든다), ADR-0075 결정 1·2(프레임을 받는지는 씬이 정한다). **유지** — ADR-0059(라인은 사람이
  고른다), ADR-0044(판정은 사람 판독, 자동 재생성 루프 없음), ADR-0034(값의 출처는 하나).
  **부분 개정** — ADR-0086(전역 `base_style`이 모든 엔진에 통한다는 전제)

## 맥락

### 1. 사람 지시가 있고, `local`에는 그 경로가 없다

> *"실물 이미지가 프롬프트에 필요하다면 정확히 나오는 것이지 실물 이미지만으로 영상을 채우고
> 싶지 않아. 실제와 벗어나거나 비슷하지만 다른 이미지를 보여주면 사람들이 반감을 가진다."*
> (2026-08-31)

`art`는 이미 한다 — `[6]`이 `refs/`의 실물을 MJ `--oref --ow 25`로 붙인다 (ADR-0077).
**`local`에는 없다**: H3에는 참조 입력이 없고(`/object_info` 확인 — optional IMAGE 둘은
`first_frame`·`last_frame`뿐), `[6] frames`는 `style_in_frames`가 참인 라인에서만 돈다
(`stages/frames.py:766`). 그래서 `local` 편은 참조를 못 받는다.

### 2. 실측 2026-09-01 — 65장, 4소재, 2시드

로컬 GPU(4080 SUPER)에 RealVisXL V5.0 + IP-Adapter Plus SDXL + CLIP-ViT-H를 깔고
**실제 run의 `[5]` 산출(`subject_prompt`)과 `[4]`의 `reference_ok` 사진**으로 쟀다.
합성 프롬프트를 쓰지 않았다 — 프로브가 실제 산출과 다른 것을 재면 값이 없다.
장당 12~20초(832×1472, 28스텝), 모델 로드 뒤 7~8초, **변동비 0**. 그림은
`reports/ipadapter-probe/`에 컨택트시트로 있다.

**(a) 구도의 주인을 정하는 것은 `start_at`이다** — `weight`가 아니다. 4소재 × 2시드 전부 일관:

| `start_at` | 관측 |
|---|---|
| 0.0~0.2 | **참조가 구도째 가져간다.** 자유의 여신상 씬에서 프롬프트의 붓이 사라지고 조각상 사진이 재현됐고, 프레넬 씬에서 배가 사라지고 등대만 남았다 |
| **0.35~0.5** | **구도·동작은 프롬프트, 재질·정체성은 참조.** 설계가 원한 분업이 이 구간에서만 성립한다 |

가장 깨끗한 표본이 여신상이다: 참조가 없으면 **초록 페인트 벽**을 그리고, `w0.7 s0.4`면 같은 붓·
같은 구도인데 표면이 **주름진 동판 녹청**으로 바뀐다. 사람이 말한 *"필요하다면 정확히 나오는 것"*이
이 그림이다.

**(b) `weight`는 참조가 형태 어휘를 얼마나 넣는지를 정한다.** 0.3은 재질·색만 옮기고 형태 교정이
거의 없다. **worklog (59)가 적어 둔 "저가중 0.3~0.5"는 약하다** — 분업이 되는 조합은
**`weight 0.7` + `start_at 0.35~0.4`**다.

**(c) 최적 구간이 소재 유형마다 갈린다.**

| 소재 유형 | 최적 | 근거 |
|---|---|---|
| 재질·정체성이 문제 (녹청·등대 불빛·놋 저울) | `start_at` 0.4 | 구도를 지키며 실물 재질로 교정 |
| **형태·배치가 곧 정보 (특허 도면)** | `start_at` 0.0 | 0.4는 옷본 그림에 머물고, 0.0에서야 특허 시트 구조가 나온다 |

worklog (59)가 열어 둔 *"씬마다 가중치를 달리할지"*의 답은 **그렇다**이고, **갈리는 축은
`weight`가 아니라 `start_at`**이다.

### 3. 같은 프로브가 참조와 무관한 사고를 둘 드러냈다

- **ADR-0086의 `base_style`이 스틸에서 극단적 근접 질감으로 끈다.** 같은 프롬프트에서 FORMAT 절만
  빼면 살창 구조가 나오고, 붙이면 평평한 판벽 텍스처가 된다. 그 문자열은 *"real surface
  microdetail … on every material"* + *"shallow depth of field on foreground detail"*이라
  **영상 모델을 보고 쓴 계약**이고, 스틸 엔진에 그대로 통한다는 근거가 없었다 (ADR-0086은 fal 프로브
  15클립과 여신상 1편으로 승인됐다 — 둘 다 영상이다). **범인이 둘 중 어느 절인지는 이때 안 갈렸다**
  — 승인 뒤 실측 5가 갈랐고, 답은 하나뿐이다.
- **SDXL이 단어를 문자 그대로 읽는다** — `fastener teeth`가 **해부학적 이빨**로, 프롬프트를 줄이면
  **사람 입**으로 나왔다. `[5]` 지시문이 이미 아는 `fly`·`whip`·`balance pan` 계열인데, 이번엔
  검사기가 아니라 **이미지 모델이 충돌한다**. 지금 지시문의 회피 규칙은 영상 모델을 보고 쓴 것이다.

### 4. 붙일 범위와 설치 실측

- `reference_ok` 사진이 **229장**, `reference_ok` + `subject_prompt`가 둘 다 있는 씬이 **157개**다
  (전 run 스캔, 2026-09-01). worklog (59)의 "4편 70씬 중 42씬(60%)"과 같은 크기다.
- 설치 완료(2026-09-01): RealVisXL V5.0 fp16 6.94GB(`ComfyUI-Shared/models/checkpoints`),
  CLIP-ViT-H 2.53GB(`clip_vision`), `ip-adapter-plus_sdxl_vit-h` 848MB(**인스턴스 자체
  `models/ipadapter`** — `ipadapter`는 Desktop의 extra search path 26개에 없다),
  `ComfyUI_IPAdapter_plus` 노드(pip 의존성 0). 셋 다 `/object_info`에서 잡히는 것을 확인했다.
- **VRAM**: SDXL 경로가 약 8GB다. H3가 물고 있으면 여유가 3.1GB라 **스왑이 생긴다** — `[6]`을
  `[7]`보다 먼저 통째로 돌리면 스왑은 편당 1회다.

### 5. 승인 뒤 실측 (2026-09-01) — `frame_style`을 못박았다. **범인은 `microdetail` 한 절이다**

부록이 `frame_style`을 `"…(실편 프로브로 확정한다)"` 자리표시자로 남겼으므로 승인 직후 확정했다.
하니스는 `reports/ipadapter-probe/fstyle.py`(`main` 1라운드 / `r2` 2라운드), 그림은
`reports/ipadapter-probe/fstyle-*`·`fstyle2-*`. **참조를 실제로 붙인 상태**(승인값 identity =
`w0.7 / start_at 0.4`)에서 쟀다 — `fmt` 프로브는 참조 없이 쟀는데 실전은 항상 참조가 붙는다.

1라운드(3소재 × 4변종 12장)에서 판독이 갈렸다: 짧은 photoreal 태그는 구조를 살렸지만 여신상에서
**전경의 손을 잃었다**. 공통 원인 후보가 *"sharp throughout / no depth of field blur"*라, 2라운드는
`base_style`에서 **두 절을 하나씩만 빼서**(2소재 × 4변종 × 2시드 16장) 갈랐다.

| 변종 | 살창 (구조가 정보) | 여신상 (재질이 정보) |
|---|---|---|
| A `base_style` 현행 | ✗ 극단 근접 — 문 일부만 | ○ 손·붓 유지 |
| B FORMAT 절 없음 | ○ 문 전체 구조 | ○ |
| **E `microdetail` 절만 제거** | **○ 문 전체 구조** | **○ 손·붓 유지** |
| F `shallow DoF` 절만 제거 | ✗ A와 같이 뭉갠다 | ✗ **손이 사라진다** |

두 소재·두 시드에서 일관된다. **`real surface microdetail - grain, wear, weave and tool marks on
every material`이 단독 범인이고, `shallow depth of field on foreground detail`은 무죄를 넘어
남겨야 하는 절이다** — 빼면 전경 피사체를 잃는다. 맥락 3이 둘을 함께 지목한 것은 여기서 정정된다.

E를 고른 이유는 B(FORMAT 절 제거)와 결과가 같으면서 **`base_style`의 어휘를 그대로 쓰기 때문**이다
— 결정 5의 단서(*"둘이 어긋나면 프레임과 클립의 그림체가 갈린다"*)를 지키는 쪽이 E다. 그래서
`frame_style`은 **`base_style`에서 그 한 절을 뺀 문자열**이고, 부록 (1)에 전문을 적었다.

**곁가지**: 1라운드의 지퍼 씬은 네 변종 전부 실패했다(놋쇠 부품이 천에 박힌 물체). 맥락 3의
`fastener teeth` 단어 충돌이라 `frame_style` 소관이 아니고, 이 ADR이 닫지 않는 것에 그대로 남는다.

### 6. 되돌린 실측 (2026-09-02) — **H3가 준 프레임을 이어 그리지 않는다**

첫 실편(`goryeo-celadon`)을 사람이 보고 *"사진이 한 프레임 정도씩 아주 조금씩 들어가는 게
있다"*고 했다. 원본(ComfyUI 출력, 24fps)을 재니 그 말 그대로였다:

| | 프레임 0→1 | 그 뒤 |
|---|---|---|
| 씬 5 원본 | **64.8** | 0.1~0.4 |

**준 그림이 1프레임만 나오고 버려진다.** 30fps로 늘어나며 2~4프레임이 되어 "사진이 잠깐
들어갔다 사라지는" 것으로 보인다. 편 전체에서 그런 씬이 14개 중 9개였고, 튀는 지점은
0.10~1.33초로 제각각이었다.

**대조 실험이 결론을 못박았다** (`reports/ipadapter-probe/h3_frame_effect.py`, 같은 씬·같은
프롬프트·같은 시드):

| | 결과 |
|---|---|
| A `first_frame` 줌 | **5초 내내 정지.** f0·f1·f8·f20이 전부 같은 그림 |
| B `first_frame` 없음 | 정상 움직임(프레임 간 3.3~3.9). **게다가 씬 계약에 더 맞았다** — 씬 5는 "깨진 청자 파편"인데 B가 깨진 파편을 그렸고 `[6]` 그림은 멀쩡한 사발이었다 |

끊긴 뒤의 내용이 준 그림과 **닮지도 않았다**(붉은 암반 → 베이지 판, 사발 → 다른 작업장).
즉 프레임은 영상에 **아무 기여를 못 한다.**

**`[6]`은 죄가 없다.** 참조 사진이 붙은 씬의 스틸은 설계대로 나왔다(매병의 비색·상감이 실물
계열). 실패한 것은 **결정 6의 전제** — "H3에 first만 주면 거기서 출발한다"가 틀렸다.
`comfy-h3-fl2v`(first·last 둘 다)가 도는 자리와 달리 first 하나로는 조건이 안 걸린다.

**되돌린 뒤 실측 (2026-09-02) — 되돌릴 조건 3도 걸려 있었다.** 프레임을 끄고 돌린 4편의
`[7]` 벽시계가 55~75분인데, 프레임을 쓴 `goryeo-celadon`은 **193분**이었다 (거의 3배).

| 편 | `[7]` | 편 전체(`[7]`+`[8]`+`[9]`) |
|---|---|---|
| `korean-dolmen` | 55분 | 59분 |
| `hoover-dam-cooling` | 63분 | 68분 |
| `brooklyn-bridge-wire` | 65분 | 68분 |
| `harrison-longitude` | 75분 | 79분 |
| **`goryeo-celadon`** (프레임 씀) | **193분** | — |

`[6]`이 쓴 3.5분은 그 차이의 일부일 뿐이다 — 큰 쪽은 **재생성**이다. 프레임을 문 편의 씬당
호출이 1.37회(19씬 26회)로 기준선 1.00을 넘었고, 그것이 되돌릴 조건 2가 말한 신호였다.
**조건 2와 3이 조건 1과 같은 방향을 가리켰다** — 되돌림은 화질 판정만이 아니라 비용에서도
맞는 결정이었다.

**되돌릴 조건 1이 걸렸다** (*"사람 판독이 참조 붙은 편을 기각하면"*). 사람 결정:

1. **`local`의 `reference_frames`를 끈다.** 어휘 한 줄이고 `[6]`은 다시 통째로 스킵된다.
2. **이미 영상을 만든 편**(`goryeo-celadon`·`silla-gold-crown`)**은 다시 만들지 않는다** —
   클립 **머리만 잘라** 쓴다. 잘라내면 그 그림이 화면에서 사라지므로 되돌린 것과 결과가
   같고, 재생성 3시간 × 2를 아낀다. 도구는 `tools/trim_frame_head.py`이고 자를 길이는
   **씬마다 실제로 튄 지점까지**다(1초 고정은 과하거나 모자란다). 원본은 `clips.pretrim/`.
3. **코드·템플릿·어휘 정의는 남긴다** — `comfy_sdxl.py`, `ComfyH3FirstClient`,
   `config/comfy/{sdxl-ipa,h3-f2v}.api.json`, `$defs.reference_mode`, `meta.style.frame_style`.
   프레임을 이어 그리는 엔진이 생기면 **어휘 한 줄로 다시 켜는 자리**다. 계약 테스트는
   `reference_line` 픽스처가 그 플래그를 켜서 기계를 계속 검증한다.

**다시 켜기 전에 넘어야 할 것**: 엔진이 first frame을 실제로 이어 그리는지를 **먼저**
재야 한다. 이 ADR은 그 검증 없이 붙였고 그것이 비용이었다 — 실편 한 편(3시간)과 배치 중단.

## 결정

1. **`local` 라인의 `info` 없는 씬도 `[6] frames`를 탄다.** 그 씬의 first frame은 MJ가 아니라
   **로컬 SDXL + IP-Adapter Plus**가 그리고, `[7]`이 그것을 `first_frame`으로 받아 H3가 잇는다.
   `info` 씬은 지금처럼 프레임 없이 텍스트→영상이다 (ADR-0075 결정 1 유지).
2. **참조는 `[4]`가 고른 `reference_ok` 첫 장**이다 — `art`와 같은 규칙이고 같은 함수
   (`frames.py:pick_reference`)를 쓴다. 참조가 없는 씬은 참조 없이 그린다(강등이 아니다).
3. **노브는 둘이고 어휘가 값을 진다** — `weight`와 **`start_at`**. 코드가 상수를 선언하지 않는다
   (ADR-0034).
4. **어느 값을 쓸지는 씬이 고른다.** 씬 계약에 `reference`(선택) 필드를 더하고 값은 닫힌 어휘에서
   고른다 (원칙 3). **어휘는 둘이다** — 사람이 `structure`를 뺐다 (승인 2026-09-01):
   - `identity` — **기본.** `weight 0.7 / start_at 0.4`. 재질·정체성만 참조가 지고 구도·동작은
     프롬프트가 지휘한다.
   - `none` — 참조를 안 붙인다. 실물이 없는 대상(가시 들판·컷 모델)과 **형태·배치가 곧 정보인
     씬**(도면·단면)이 여기다. 후자는 실측이 `start_at 0.0`을 가리켰지만 그 대가가 사실상 사진
     재현이라 사람이 그 모드를 안 받았다 — 그 씬은 참조 없이 가고 위험 ②가 남는다.
   ~~`structure` (`weight 0.7 / start_at 0.0`)~~ — **채택 안 함.** 근거는 아래 「사람이 정한
   지점」과 결과의 되돌릴 조건 4다.
   `[3s]`가 씬마다 고른다 — 이미 `framing`·`staging`·`camera`를 같은 방식으로 고른다.
5. **`[6]`은 first frame용 스타일 문자열을 따로 쓴다.** 전역 `base_style`을 스틸에 그대로 싣지
   않는다 (맥락 3). `vocab.json meta.style`에 `frame_style`을 두고 `[6]`만 그것을 쓴다.
   `[7]`의 영상 프롬프트는 안 바뀐다. **값은 맥락 5가 못박았다** — `base_style`에서
   `real surface microdetail …` 한 절만 뺀 문자열이고, 나머지 어휘는 그대로 둔다(그래야 프레임과
   클립의 그림체가 안 갈린다). 전문은 부록 (1).
6. **`[7]`에 first-only 어댑터가 필요하다.** 지금 `comfy-h3-fl2v`는 first·last **둘 다** 요구하고
   없으면 멈춘다(`comfy_h3.py:_stage_frames`). `local`은 last가 없으므로 first만 받는 경로를
   추가한다 — 기존 `info` 씬 경로는 건드리지 않는다.
7. **`frames.json`의 주소는 로컬 경로를 허용한다.** 지금 계약은 *"MJ가 닿는 공개 https"*
   (specs/05)인데 `local`의 프레임은 R2에 올릴 이유가 없다 — 같은 기계의 ComfyUI가 읽는다.

**사람이 정한 지점 (2026-09-01)**: 결정 4의 `structure`는 **참조가 구도를 가져가는 모드**라,
사람이 말한 *"실물 이미지만으로 영상을 채우고 싶지 않아"*와 정면으로 부딪힌다. 실측은 도면류에서
그 모드라야 구조가 맞는다고 말했지만 **그 대가가 사실상 사진 재현**이라, 사람이 **빼기로 정했다**.
따라서 어휘는 `identity`·`none` 둘이고, 도면·단면 씬은 참조 없이 간다 — **위험 ②는 열린 채
남는다.** 되돌릴 조건 4가 그 자리다.

## 검토한 대안

| 대안 | 장점 | 단점 | 탈락 사유 |
|---|---|---|---|
| **ControlNet으로 형태를 묶는다** | 형태 재현이 가장 강하다 | 프레임이 사진의 윤곽에 묶여 프롬프트가 죽는다 | worklog (59)가 이미 기각했고 이번 실측이 방향을 확인했다 — IP-Adapter조차 `start_at 0.0`에서 프롬프트의 동작(붓·배)을 지웠다. 더 강한 구속은 사람이 원하지 않는 결과다. **도면 씬 보조로만 남긴다** |
| **Qwen-Image-Edit / FLUX Kontext** | 대상 동일성 보존이 강하다 | 저가중 다이얼이 없다. fp8도 12~20GB라 H3와 스왑 비용이 크다 | 이번 실측의 핵심 발견(`start_at`으로 구도와 재질을 가른다)이 **바로 그 다이얼**이다. 다이얼이 없으면 사람이 거부한 "사진 재현" 쪽으로 고정된다 |
| **참조를 안 붙이고 `[5]` 프롬프트만 강화한다** | 새 모델·새 단계가 없다 | — | **실측이 기각했다.** 참조 없는 장이 녹청을 초록 페인트 벽으로, 등대를 달로 그렸다. 프롬프트를 1021자 → 259자로 줄여도 안 고쳐졌고(`plen` 프로브), 오히려 단어 충돌이 심해졌다 |
| **`local`을 없애고 `art`(MJ `--oref`)로 흡수한다** | ADR-0077 경로를 그대로 쓴다 | MJ 정액에 묶이고 CF 캡차 이력이 있다(worklog 59). 변동비 0이 사라진다 | ADR-0059가 라인을 나눈 이유가 변동비다. 11차에서 MJ가 잠겨 `local`로 내려간 전례(ADR-0083)가 이 의존을 이미 비쌌다고 말한다 |
| **`weight`만 어휘에 두고 `start_at`은 고정한다** | 노브가 하나라 단순하다 | 소재 유형이 갈리는 축을 못 표현한다 | 실측 (c) — 도면 씬과 재질 씬의 최적이 `start_at`에서 갈린다. 고정하면 둘 중 하나를 버린다 |

## 결과

**바뀌는 것**

- 스펙: `specs/05-pipeline.md`의 `[6]`·`[7]` 절 — 프레임을 받는 조건이 `style_in_frames`
  하나가 아니게 되고, `frames.json` 주소 계약이 로컬 경로를 허용한다.
- 어휘: `vocab.json`에 `$defs.reference_mode`(닫힌 어휘 **2** — 사람이 `structure`를 뺐다), `meta.reference_mode`(모드별
  `weight`·`start_at`), `meta.style.frame_style`, `meta.video_line.local`에 프레임 스위치.
- 계약: `scene.schema.json`에 선택 필드 `reference`. **`[3s]` 지시문**이 그것을 고르게 된다.
- 코드: `[6]`에 로컬 어댑터 1개, `[7]`에 first-only 어댑터 1개. **기존 `art` 경로는 안 건드린다**
  (단계 독립 6원칙).
- 비용: **변동비 0 유지.** 벽시계가 씬당 12~20초 + 편당 스왑 1회 늘고, 디스크 10.3GB를 이미 썼다.

**되돌릴 조건**

1. **사람 판독이 참조 붙은 편을 기각하면** — ADR-0044대로 이 자리가 판정이다. 특히 `identity`
   모드가 "비슷하지만 다른 이미지"를 만들면 목적 자체가 실패한 것이다.
2. `[7]`의 씬당 재생성률이 기준선(`local` 1.00, `art` 1.13회/씬)보다 **뚜렷이 오르면** — 프레임이
   H3에 안 맞는다는 신호다.
3. `[6]` 스왑으로 편당 벽시계가 **30분 넘게** 늘면 — 변동비 0의 이점이 시간으로 상쇄된다.
4. **도면·단면 씬이 사람 판독에서 계속 지면** — `structure`를 뺀 대가가 위험 ②이고, 그 씬은
   지금 참조 없이 간다. 실편에서 그 씬만 반복해서 기각되면 사람에게 `structure` 재상정 또는
   `[3s]`가 **글자·도면 없는 구도를 고르게 하는** 쪽(아래 「닫지 않는 것」의 시사) 중 하나를 묻는다.

**이 ADR이 닫지 않는 것**

- **특허 도면류의 가짜 글자**는 참조로 안 고쳐졌다(전 변종에서 읽을 수 없는 손글씨가 나왔다).
  1만 년 편 석비와 같은 위험 ②이고 여전히 열려 있다 — 그 씬은 `reference: none`으로 두고
  `[3s]`가 글자 없는 구도를 고르는 편이 낫다는 것이 실측의 시사다.
- **SDXL의 단어 충돌**(`teeth`)은 `[6]`용 프롬프트 지시문의 문제다. 이 ADR은 스타일 문자열만
  가르고 지시문은 안 건드린다 — 실편에서 재현 빈도를 보고 별건으로 판단한다.

## 부록 — 적용한 스펙 수정 (승인 2026-09-01. `structure` 제외 반영, `frame_style` 확정)

값의 출처를 하나로 두는 순서(ADR-0034)대로 **어휘 → 계약 → 스펙 문장 → 코드**다.

### (1) `specs/schema/vocab.json` — 어휘 3건 추가

```diff
   "$defs": {
+    "reference_mode": {
+      "enum": ["identity", "none"]
+    },
```

```diff
   "meta": {
+    "reference_mode": {
+      "_role": "실물 참조 사진을 first frame에 어떻게 물릴지 (ADR-0087). `[3s]`가 씬마다 고르고 `[6]`이 그 값을 IP-Adapter에 건다. **노브가 둘인 이유는 실측이다** (2026-09-01, 65장/4소재/2시드): 구도의 주인을 정하는 것은 `weight`가 아니라 `start_at`이고, 최적 구간이 소재 유형마다 갈린다. 참조 사진이 없는 씬에서는 어떤 값이든 참조 없이 그린다.",
+      "_default": "identity",
+      "identity": {
+        "gloss": "재질·정체성만 참조가 지고 구도·동작은 프롬프트가 지휘한다. 기본값",
+        "weight": 0.7,
+        "start_at": 0.4
+      },
+      "none": {
+        "gloss": "참조를 안 붙인다. 실물이 없는 대상(가시 들판·컷 모델)과 형태·배치가 곧 정보인 씬(도면·단면)",
+        "weight": 0.0,
+        "start_at": 0.0
+      }
+    },
```

```diff
   "meta": { "style": {
+    "frame_style": "photorealistic 3D render with physically based materials and ray traced lighting. STYLE: accurate reflection and roughness, natural daylight with a clear sun direction and true cast shadows, no sun disc in frame, shallow depth of field on foreground detail. COLOR IS RICH AND CLEAN: fully saturated, no gray wash and no desaturated grading",
+    "_frame_style": "`[6]`이 first frame을 그릴 때만 쓰는 스타일 (ADR-0087 결정 5). **`base_style`에서 `real surface microdetail - grain, wear, weave and tool marks on every material, ` 한 절만 뺀 문자열이고 나머지는 글자 그대로 같다** — 실측 2026-09-01(ADR-0087 맥락 5, 16장/2소재/2시드): 그 절이 정지 이미지 모델을 극단적 근접 질감으로 끌어 살창이 평평한 판벽이 됐고, 빼면 구조가 돌아온다. **`shallow depth of field on foreground detail`은 빼면 안 된다** — 같은 실측에서 그 절만 뺀 변종이 전경의 손을 잃었다. ADR-0086은 영상(fal 15클립 + 여신상 1편)으로 승인된 문자열이라 스틸 적용이 미검증이었던 것이 원인이다. **`[7]`의 영상 프롬프트는 계속 `base_style`이다** — 둘이 어긋나면 프레임과 클립의 그림체가 갈리므로 어휘를 최대한 공유한다. `base_style`을 고치면 여기도 같이 본다",
```

```diff
   "meta": { "video_line": { "local": {
       "gloss": "로컬 GPU — ComfyUI + MiniMax H3. 변동비 0. 초기 기본 (테스트·초기 쇼츠)",
       "provider": "comfy-h3",
-      "shot2": false
+      "shot2": false,
+      "style_in_frames": false,
+      "reference_frames": true,
+      "_reference_frames": "true면 `info`가 없는 씬이 `[6]`에서 로컬 SDXL + IP-Adapter로 first frame을 받는다 (ADR-0087). `style_in_frames`와 다른 스위치다 — **스타일은 여전히 말이 지고**(`[7]`이 `base_style`을 싣는다) 프레임이 지는 것은 **실물의 형태·재질**뿐이다"
```

### (2) `specs/schema/scene.schema.json` — 씬 선택 필드 1개

```diff
   "$defs": { "scene": { "properties": {
       "info": { … },
+      "reference": { "$ref": "vocab.json#/$defs/reference_mode" },
```

`required`에 넣지 않는다 — 빈칸이면 `meta.reference_mode._default`(`identity`)다.

### (3) `specs/05-pipeline.md` — 두 문장

```diff
-- **씬이 프레임을 입력으로 받을 수 있다** (ADR-0070 — ADR-0075가 라인 단위에서 씬 단위로
-  좁혔다). `style_in_frames`가 참인 라인의 **`info`가 없는 씬**은 `[6] frames`가 만든
+- **씬이 프레임을 입력으로 받을 수 있다** (ADR-0070 — ADR-0075가 라인 단위에서 씬 단위로
+  좁혔고, ADR-0087이 이유를 둘로 갈랐다: **스타일이 프레임에 있는 경우**(`style_in_frames`)와
+  **실물 고증이 프레임에 있는 경우**(`reference_frames`)다). 둘 중 하나가 참인 라인의
+  **`info`가 없는 씬**은 `[6] frames`가 만든
```

`[7]` 절의 `frames.json` 주소 계약:

```diff
-**주소는 MJ가 닿는 공개 https다** — 로컬 경로가 아니다.
+**주소는 그 프레임을 읽을 쪽이 정한다** — MJ가 그리는 라인은 공개 https(R2)이고,
+`reference_frames` 라인은 같은 기계의 ComfyUI가 읽으므로 **로컬 경로다** (ADR-0087 결정 7).
```

### (4) 코드 (승인 뒤)

- `[6]`: 로컬 어댑터 1개 — `refs`의 `reference_ok` + 씬의 `reference` 모드 → SDXL+IP-Adapter.
  게이트를 `style_in_frames or reference_frames`로 넓힌다 (`frames.py:766`).
- `[7]`: first-only 어댑터 1개. **`comfy-h3-fl2v`(둘 다 요구)는 안 건드린다** — `info` 씬 경로다.
- `[3s]` 지시문: `reference`를 고르는 규칙 한 문단.
- 계약 테스트: 어휘 밖 `reference` 반려 / 참조 없는 씬이 멈추지 않는다 / `art` 경로 무변화.
