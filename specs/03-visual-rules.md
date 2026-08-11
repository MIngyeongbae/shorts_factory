# 03. 비트 → 시각 연출 룰 테이블

이미지 프롬프트·오버레이·전환은 이 테이블로 결정한다. LLM이나 개발자가 임의 판단하지 않는다.

## 베이스 스타일 (전 씬 공통)

**펜선·수채 위의 극사실 건축 렌더링** (ADR-0023). 실호출 6회로 검증한 문구 그대로 쓴다.

```
photorealistic architectural rendering on white paper, mixed media:
the focal subject rendered with true-to-life detail, materials and lighting;
colour laid in as controlled watercolour washes over fine graphite and
technical-pen underdrawing, with construction and guide lines left visible;
realism dominant over painterly looseness;
detail and colour fall away toward the edges into bare paper
```

- 흰 종이, 작도선을 지우지 않고 남긴다, 가장자리로 갈수록 맨 종이로 사라진다
- **사실주의가 우세하다.** 재질이 곧 설명이다 — 콘크리트 골재, 거푸집 나뭇결,
  암반 층리가 보여야 한다. 수채 워시로 뭉개면 그 설명이 사라진다 (ADR-0022)
- 9:16 구도, 피사체는 중앙~상단 1/3에 배치 (하단 1/3은 자막 영역으로 비움).
  이 기법은 원래 여백으로 끝나므로 하단이 저절로 비워진다
- **읽을 수 있는 글자·손글씨 낙서를 그리지 않는다.** 정확해야 하는 한국어는 전부
  레이어 B다 (ADR-0002). 네거티브 프롬프트에 명시한다
- 스타일 앵커 3장을 모든 호출에 첨부한다 (`assets/style_anchors/`, ADR-0005·0021)
- 우하단 반짝이 파티클(✦) 오버레이 — 후처리 공통 레이어.
  **흰 배경에서 보이는지 아직 확인하지 않았다** — `[8] overlay`에서 실물로 판단한다

## 구도

구도는 **비트 단독이 아니라 `(beat × subject_scale)`로 결정한다** (ADR-0018). `subject_scale`은
씬 계약의 필드이고 `[1. script]`가 `subject`와 함께 채운다 (스펙 02).

| subject_scale | 뜻 |
|---|---|
| `wide` | 대상 전체·부지·전경. 기본값 |
| `close` | 표면·끝단·접합면·계측기 등 근접 디테일 |
| `diagram` | 단면도·평면도·도해·일러스트·기록 노트 |

비트만으로 구도를 정하던 이전 표는 12행 중 3행이 택일("전경 유지 **or** 클로즈업")로 남아
값을 정하지 못했고, 실측 52씬 중 9씬에서 룰이 지시한 광각과 `subject`가 어긋났다.
그 택일과 어긋남은 같은 공백이었다 — 자세한 근거는 ADR-0018.

### 비트 × 스케일 → 구도 토큰

| beat | `wide` | `close` | `diagram` |
|---|---|---|---|
| `hook_fact` | `drone_wide` | `subject_closeup` | `section_diagram` |
| `hook_twist` | `@prev` † | `subject_closeup` | `section_diagram` |
| `context` | `aerial_diorama` | `subject_closeup` | `section_diagram` |
| `context_number` | `aerial_diorama` | `subject_closeup` | `section_diagram` |
| `failed_solution` | `attempt_medium` | `detail_closeup` | `solution_diagram` |
| `failure_reason` | `failure_result_wide` | `failure_closeup` | `failure_diagram` |
| `dilemma_peak` | `problem_wide` | `problem_closeup` | `problem_diagram` |
| `turning_point` | `frontal_symmetric` | `frontal_closeup` | `frontal_diagram` |
| `solution_step` | `solution_medium` | `detail_closeup` | `cross_section` |
| `solution_number` | `solution_medium` | `detail_closeup` | `cross_section` |
| `present_link` | `present_wide` | `present_closeup` | `present_section` |
| `ending_echo` | `@hook` ‡ | `subject_closeup` | `section_diagram` |

† `@prev` = 앞 씬의 구도를 그대로 잇는다 ("전경 유지"). 이미지 재사용이 아니라 구도만 잇는다.
‡ `@hook` = 첫 `hook_fact` 씬의 구도를 다시 쓴다 (수미상관).

**참조가 성립하지 않는 경우** — `@prev`/`@hook`은 다른 씬을 가리키므로 스케일이 어긋나면
쓸 수 없다. 다음 순서로 푼다.

1. 가리키는 씬의 `subject_scale`이 이 씬과 **같으면** 그 씬의 구도 토큰을 쓴다
2. 다르거나, 가리킬 씬이 없으면 (`hook_twist`가 첫 씬, `hook_fact`가 없는 대본)
   → `hook_twist`는 `drone_wide`, `ending_echo`는 `present_wide`를 쓴다

### 구도 토큰 → 샷

| 토큰 | 샷 |
|---|---|
| `drone_wide` | 드론 뷰/광각 전경 |
| `aerial_diorama` | 조감 디오라마 |
| `attempt_medium` | 해법 대상 미디엄 샷 |
| `solution_medium` | 해결책 미디엄 샷 |
| `failure_result_wide` | 실패 결과 와이드 (무너짐, 넘침 등) |
| `problem_wide` | 문제 상황 와이드 뷰 |
| `frontal_symmetric` | 핵심 피사체 정면, 대칭 구도 |
| `present_wide` | 현재 실사풍 전경 |
| `subject_closeup` | 피사체 클로즈업 |
| `detail_closeup` | 디테일 클로즈업 |
| `failure_closeup` | 실패 지점 클로즈업 |
| `problem_closeup` | 문제 지점 클로즈업 |
| `frontal_closeup` | 핵심 피사체 정면 클로즈업 |
| `present_closeup` | 현재 실사풍 클로즈업 |
| `section_diagram` | 단면/도해 컷 |
| `solution_diagram` | 해법 도해 |
| `failure_diagram` | 실패 메커니즘 도해 |
| `problem_diagram` | 문제 도해 |
| `frontal_diagram` | 핵심 도해 정면 |
| `cross_section` | 단면(cross-section) 컷 |
| `present_section` | 현재 실사풍 단면/절단면 |

## 비트별 오버레이·카메라

| beat | 오버레이 | 카메라 기본값 |
|---|---|---|
| `hook_fact` | 없음 | slow_zoom_in |
| `hook_twist` | 없음 | static |
| `context` | 없음 | pan 또는 tilt |
| `context_number` | **`big_red_text`** | slow_zoom_in |
| `failed_solution` | 없음 | static |
| `failure_reason` | 없음 | slow_zoom_in |
| `dilemma_peak` | 없음 | static |
| `turning_point` | 없음 | slow_zoom_in |
| `solution_step` | 없음 | tilt_down 또는 slow_zoom_in |
| `solution_number` | **`big_red_text`** | static |
| `present_link` | 없음 | slow_zoom_out |
| `ending_echo` | 없음 | slow_zoom_out |

## 오버레이 타입 enum

스펙 02의 `emphasis.type`이 참조하는 목록이다. **여기 없는 값은 쓰지 않는다.**

| type | 레이어 | 값(`value`) | 적용 범위 |
|---|---|---|---|
| `big_red_text` | B | 필수 — 표시할 숫자 문자열 | 씬별 (`context_number`, `solution_number`) |
| `sparkle_particles` | B | 없음 | 전 씬 공통 (우하단) |

**레이어 A(이미지 생성 단계 어노테이션)는 사용하지 않는다** (ADR-0019). 빨간 크레용 X,
빨간 X 대형, 빨간 측정선/영역, 빨간 라벨 박스, 빨간 치수선/화살표, 장소명 라벨은 원본
채널 분석에서 옮겨 온 것인데, 편당 이미지 호출을 2배로 만들고(씬의 절반이 2-pass 대상이었다)
문구 출처가 계약에 없었다. 자세한 근거는 ADR-0019.

따라서 **모든 베이스 이미지는 클린 이미지다.** 모션별 어노테이션 분기(ADR-0006의
kling/kenburns 조항)는 적용 대상이 없다.

## 텍스트 2계층 원칙 (ADR-0002)

- **레이어 A (이미지 생성 단계)**: 분위기용 어노테이션. 텍스트 정확도 불요.
  **현재 사용처 없음** (ADR-0019)
- **레이어 B (후처리 합성)**: 자막, 대형 숫자, 파티클 등 **정확해야 하는 모든 한국어 텍스트**.
  FFmpeg drawtext 또는 ASS 자막으로 합성

## 전환 규칙

| 진입 비트 | 전환 |
|---|---|
| `hook_twist` | 하드컷 |
| `turning_point` | 하드컷 + 오디오 룰의 차임 동기화 |
| `dilemma_peak` | 하드컷 |
| 그 외 전부 | 크로스 디졸브 0.6초 |

- 하드컷은 위 **3비트 진입 시에 쓴다.** 허가가 아니라 지시다 — 이 표 밖에서는 쓰지 않는다
- 디졸브 길이는 **0.6초 고정**이다. `[7]`의 클립 계약(클립 길이 = 씬 길이 + 겹침 0.6초)과
  맞물려 있어 이 값에서만 클립이 남김 없이 쓰인다. 0.4초로 두면 클립마다 0.2초가 버려진다
- 실측 결과 편당 하드컷 3~4회 / 전환 24~26회다 (피사 3/24, 후버댐 4/26). 나머지는 전부 디졸브다

## 자막 스타일

- 위치: 하단 중앙, 세로 기준 화면 72~82% 지점
- 폰트: 굵은 고딕 (Pretendard Bold 계열), **검정 + 흰색 외곽선 3px**
  - 베이스가 흰 종이라 흰 글자는 외곽선만 남는다. ADR-0023에서 뒤집었다.
    굵기·크기·위치는 그대로다
- 폰트 크기 **40px 고정** (1080×1920 기준). 큐마다 크기가 달라지지 않는다
- **1줄 최대 22자, 2줄 초과 금지**
- 이 세 값은 함께 정해졌다. 40px에서 22자는 880px로 가로 안전폭 960px 안에 들어가고,
  스펙 01이 허용하는 **가장 긴 큐(43자)도 22+21로 두 줄에 들어간다.**
  즉 정상 범위의 큐는 폰트를 줄일 일이 없다 — 큐별 폰트 축소 경로를 두지 않는다
- 22자 × 2줄에 들어가지 않는 큐는 **스펙 01의 43자를 넘겼다는 뜻이므로 실패로 보고한다.**
  자막 쪽에서 삼키지 않는다 (1부에서 고칠 문제다)
