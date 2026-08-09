# 05. 파이프라인 단계 정의 및 I/O 계약

각 단계는 독립 실행 가능한 모듈이며, 파일 기반 JSON으로 통신한다. 중간 산출물은 `runs/{run_id}/` 아래에 저장되어 어느 단계에서든 재시작 가능하다.

## 단계 목록

```
=== 1부: 토픽 패키지 생산 (한계비용 0, 완전 자동, 일 1~5개) ===

topics/backlog.md
  → [0a. topic]     → topics/{slug}/ 폴더 생성 (소재 4조건 체크, 스펙 06)
  → [0b. research]  → 01-research.md → 02-verify.md → 03-critique.md → 04-factsheet.json
                       (각각 독립 헤드리스 세션, ADR-0009)
  → [1. script]     → 05-candidates/*.json (대본+비트 태그 후보 3~5개, 팩트시트 그라운딩)
  → [1c. critique2] → 03-critique.md 갱신 (대본 후보 공격)
  → [1b. score]     → 06-script.json     (훅 스코어링: 비판 반영 채점 → 상위 1개)
  → [2. validate]   → 06-script.json     (구조·스키마·그라운딩 검증, 최대 3회 재생성)
  → [2b. judge]     → judgment/ai.json   (AI 심사관 판정: go/revise/no_go, 스펙 07)
                       revise 시 fix_directives 주입 → [1. script] 재생성 → 재심사 (상한 2회)

=== 판단 게이트 (ADR-0009 → ADR-0010 이양 경로) ===
  → 자율성 레벨에 따라: L0~L1 사람 판정(judgment/human.json, AI 판정 비공개 상태에서),
    L2 확신 구간 AI 전결 + 에스컬레이션, L3 AI 전결 + 샘플 감사. go 시에만 2부 진입

=== 2부: 영상 생산 (편당 ~$7, go 승인분만) ===
  → [3. tts+sync]   → narration.wav + timing.json + scenes.json 갱신
                       (ElevenLabs with-timestamps 문자 정렬 → 문장 경계 실측, ADR-0004)
  → [5. prompt]     → prompts.json       (씬별 이미지 프롬프트, 스펙 03 룰 적용)
  → [6. imagegen]   → images/{scene_id}.png
  → [7. motion]     → clips/{scene_id}.mp4  (이미지→비디오 or Ken Burns)
  → [8. overlay]    → 대형 텍스트·라벨·파티클 합성 (레이어 B)
  → [9. assemble]   → timeline.mp4       (FFmpeg: 디졸브 + 자막 번인)
  → [10. mix]       → final.mp4          (TTS + SFX + BGM, 스펙 04)
  → [11. report]    → report.md          (실패 씬, 재시도 이력, 소요 시간/비용)
```

## 핵심 계약: scenes.json

`02-beat-schema.md`의 씬 객체 배열 + 메타:

```json
{
  "run_id": "20260807-baekak",
  "topic": "경복궁 뒷산 벌목 금지",
  "total_duration": 98.4,
  "scenes": [ { "...": "씬 객체" } ]
}
```

## 단계 간 계약 파일 (runs/{run_id}/)

토픽 패키지(`topics/{slug}/`)는 사람이 읽는 산출물이고, 단계 간 기계 계약은 run 디렉터리에 둔다 (ADR-0011).

| 파일 | 생산자 | 용도 |
|---|---|---|
| `topic.json` | [0a] | 소재명·슬러그·백로그 4조건. 하류 단계의 입력 |
| `state.json` | 전 단계 | 단계 상태(pending/running/done/failed/blocked). 재시작 시 완료 단계 스킵 판단 |
| `research.json` | [0b] | 팩트시트 사본 (스펙 06) |
| `logs/*.json` | LLM 어댑터 | 헤드리스 세션 응답 원본 |

- 소스 카드(`knowledge/`)는 run·토픽에 종속되지 않는 **누적 자산**이라 run 디렉터리에 두지 않는다 (ADR-0012)
- `slug`: 음운 변화 미적용 로마자 표기, `[a-z0-9-]` 최대 60자 (ADR-0011)
- `run_id`: `YYYYMMDD-{slug}`
- 헤드리스 세션에는 읽기 도구만 준다. 산출물 파일은 오케스트레이터가 쓴다 (ADR-0011)

## 단계별 규칙

- **[0b. research]**: 스펙 06의 사료 소스 우선순위로 웹 검색·수집. verdict: fail 시 백로그 반려하고 종료. 팩트시트는 사람 검수 지점 (유일한 수동 게이트).
- **[1. script]**: 팩트시트 그라운딩 (ADR-0007). 팩트시트에 없는 수치·연도·인명 사용 금지.
- **[1b. score]**: shorts-hook-scorer 루브릭(hook_strength / info_density / standalone) 기반 LLM 채점. 텍스트 생성 비용은 무시 가능한 수준이므로 후보 수는 비용이 아니라 채점 신뢰도로 결정. 전 후보가 기준 미달이면 주제 자체를 반려하고 리포트. 추후 실제 조회수 데이터로 루브릭 보정(피드백 루프)을 v2 과제로 둔다.
- **[2. validate]**: 스펙 01의 검증 5항목 + 스펙 02 스키마 검증 + 그라운딩 검증(대본 숫자 전수 추출 → 팩트시트 대조, ADR-0007). 실패 시 실패 사유를 프롬프트에 피드백하여 재생성 (최대 3회, 초과 시 중단·리포트).
- **[3. tts+sync]**: 대본 전체 단일 호출 + with-timestamps. 문자 정렬에서 씬(자막 줄) 경계 start/end 추출해 scenes.json 갱신 — 문장 경계를 뽑은 뒤 각 줄의 마지막 문장 끝만 취한다 (ADR-0013). 실측-추정 오차 씬당 ±1.5초 초과 시 경고, 총 길이 100초 초과 시 대본 축약 재생성 트리거. atempo 1.1 적용 후 타임스탬프도 1/1.1 스케일 보정.
- **[6. imagegen]**: 씬당 1회 재시도. 2회 실패 시 인접 씬 이미지 재사용(카메라 워크만 변경)으로 폴백하고 리포트에 기록.
- **[7. motion]**: `motion` 필드 분기 (ADR-0006). `kenburns` → FFmpeg zoompan(camera 값 적용). `kling` → v2.6 Turbo급 i2v 5초 무음, 2회 실패 시 kenburns 강등. 클립 길이 = 씬 길이 + 디졸브 겹침 0.6초.
- **[9. assemble]**: 자막은 timing.json 기준 ASS 생성 후 번인. 싱크 오차 ±200ms 이내 검증.

## 실패 정책

- 단계 실패 = 해당 run 디렉토리에 상태 기록 후 종료. 같은 run_id로 재실행 시 완료된 단계는 스킵.
- 씬 단위 실패는 폴백 처리하고 파이프라인은 계속 진행 (스펙 00 성공 기준 5).

## 확정된 결정

- 판단 게이트 = 섀도우 심사관 + 자율성 사다리 L0→L3 (ADR-0010)
- LLM 단계 실행 = Claude Code 헤드리스, 구독 플랜 (ADR-0008)
- 대본 = 팩트시트 그라운딩 (ADR-0007)
- TTS = ElevenLabs 본인 목소리 클로닝 (ADR-0004)
- 이미지 = Nano Banana 2 + 스타일 앵커 (ADR-0005)
- 모션 = Kling v2.6급 / Ken Burns 하이브리드, motion 필드 분기 (ADR-0006)

## 비용 기준선

편당 생성 비용 ~$7 (이미지 ~$2.5 + Kling ~$4.5, 재시도 반영). 품질 반복 포함 실질 2~3배 가정 시 편당 2~9만 원. 손익분기: 쇼츠 RPM 0.05~0.2원/조회 기준 편당 10만~180만 조회 (docs/reference-analysis.md 참고). 단, 수익 모델은 평균이 아닌 멱법칙(소수 편의 대박) 기준으로 평가한다.
