# 03. 비트 → 시각 연출 룰 테이블

이미지 프롬프트·오버레이·전환은 이 테이블로 결정한다. LLM이나 개발자가 임의 판단하지 않는다.

## 베이스 스타일 (전 씬 공통)

- photorealistic 3D 디오라마/조감 렌더 스타일, 자연광
- 9:16 구도, 피사체는 중앙~상단 1/3에 배치 (하단 1/3은 자막 영역으로 비움)
- 우하단 반짝이 파티클(✦) 오버레이 — 후처리 공통 레이어

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

- 기본: 크로스 디졸브 0.4~0.6초
- `turning_point` 진입 시: 디졸브 없이 컷 + 오디오 룰의 차임 동기화
- 하드컷은 `hook_twist`, `dilemma_peak` 진입 시에만 허용

## 자막 스타일

- 위치: 하단 중앙, 세로 기준 화면 72~82% 지점
- 폰트: 굵은 고딕 (Pretendard Bold 계열), 흰색 + 검정 외곽선 3px
- 1줄 최대 18자, 2줄 초과 금지
