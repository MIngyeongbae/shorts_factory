# 03. 비트 → 시각 연출 룰 테이블

이미지 프롬프트·오버레이·전환은 이 테이블로 결정한다. LLM이나 개발자가 임의 판단하지 않는다.

## 베이스 스타일 (전 씬 공통)

- photorealistic 3D 디오라마/조감 렌더 스타일, 자연광
- 9:16 구도, 피사체는 중앙~상단 1/3에 배치 (하단 1/3은 자막 영역으로 비움)
- 우하단 반짝이 파티클(✦) 오버레이 — 후처리 공통 레이어

## 비트별 룰

| beat | 구도 | 오버레이 | 카메라 기본값 |
|---|---|---|---|
| `hook_fact` | 드론 뷰/광각 전경 | 없음 또는 장소명 라벨 | slow_zoom_in |
| `hook_twist` | 전경 유지 or 피사체 클로즈업 | 빨간 크레용 X (부정 대상 위) | static |
| `context` | 조감 디오라마 | 빨간 측정선/영역 표시 | pan 또는 tilt |
| `context_number` | 조감 or 대상 클로즈업 | **대형 빨간 숫자 텍스트** (후처리 합성) | slow_zoom_in |
| `failed_solution` | 해법 대상 미디엄 샷 | 빨간 라벨 박스 (지도핀 스타일) | static |
| `failure_reason` | 실패 결과 묘사 (무너짐, 넘침 등) | 빨간 크레용 X | slow_zoom_in |
| `dilemma_peak` | 문제 상황 와이드 뷰 | 빨간 X 대형 | static |
| `turning_point` | 핵심 피사체 정면, 대칭 구도 | 빨간 크레용 X → 사라짐 | slow_zoom_in |
| `solution_step` | **단면(cross-section) 컷** 우선 고려 | 빨간 치수선/화살표 | tilt_down 또는 slow_zoom_in |
| `solution_number` | 해결책 디테일 클로즈업 | 대형 빨간 숫자 텍스트 (후처리 합성) | static |
| `present_link` | 현재 실사풍 전경 | 장소명 라벨 박스 | slow_zoom_out |
| `ending_echo` | 훅과 동일/유사 구도 재사용 | 없음 | slow_zoom_out |

## 텍스트 2계층 원칙 (ADR-0002)

- **레이어 A (이미지 생성 단계)**: 분위기용 어노테이션(측정선, 낙서풍 표시). 텍스트 정확도 불요. 깨진 글자 허용.
- **레이어 B (후처리 합성)**: 자막, 대형 숫자, 장소명 라벨 등 **정확해야 하는 모든 한국어 텍스트**. FFmpeg drawtext 또는 ASS 자막으로 합성.

## 모션별 어노테이션 규칙 (ADR-0006)

- `motion: kenburns` 씬: 베이스 이미지에 편집 2-pass로 레이어 A 어노테이션 적용 후 zoompan
- `motion: kling` 씬: **클린 이미지**(어노테이션 없음)를 Kling에 입력. 필요한 어노테이션은 생성된 클립 위 후처리 오버레이로 합성 (i2v 왜곡 방지)

## 전환 규칙

- 기본: 크로스 디졸브 0.4~0.6초
- `turning_point` 진입 시: 디졸브 없이 컷 + 오디오 룰의 차임 동기화
- 하드컷은 `hook_twist`, `dilemma_peak` 진입 시에만 허용

## 자막 스타일

- 위치: 하단 중앙, 세로 기준 화면 72~82% 지점
- 폰트: 굵은 고딕 (Pretendard Bold 계열), 흰색 + 검정 외곽선 3px
- 1줄 최대 18자, 2줄 초과 금지
