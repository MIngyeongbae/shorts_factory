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
  → [1x. backfill-scale] → 06-script.json (subject_scale만 채움. ADR-0018 이전 대본 전용,
                       일회성 마이그레이션이라 정규 흐름에는 없다)
  → [2b. judge]     → judgment/ai.json   (AI 심사관 판정: go/revise/no_go, 스펙 07)
                       revise 시 fix_directives 주입 → [1. script] 재생성 → 재심사 (상한 2회)

=== 판단 게이트 (ADR-0009 → ADR-0010 이양 경로) ===
  → 자율성 레벨에 따라: L0~L1 사람 판정(judgment/human.json, AI 판정 비공개 상태에서),
    L2 확신 구간 AI 전결 + 에스컬레이션, L3 AI 전결 + 샘플 감사. go 시에만 2부 진입

=== 2부: 영상 생산 (편당 ~$7, go 승인분만) ===
  ┌ [3. tts+sync]   → narration.wav + timing.json + scenes.timed.json
  │                    (ElevenLabs with-timestamps 문자 정렬 → 문장 경계 실측, ADR-0004)
  └ [5. prompt]     → prompts.json       (씬별 이미지 프롬프트, 스펙 03 룰 적용)
    ↑ 둘은 선후가 없다. 각자 06-script.json만 읽는다 (ADR-0020).
      [5]→[6]은 TTS 없이 진행할 수 있다
  → [6. imagegen]   → images/{scene_id}.jpg  (Nano Banana 2가 JPEG만 준다 — ADR-0021)
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

### 2부 계약 파일 — 값 하나의 출처는 하나다 (ADR-0020)

| 파일 | 생산자 | 역할 | 읽는 단계 |
|---|---|---|---|
| `scenes.timed.json` | [3] | **씬의 유일한 출처.** 대본 속성(`beat`·`text`·`subject`·`subject_scale`·`camera`·`motion`) + 실측 시각(`start`/`end`) | [7] 클립 길이·카메라·모션, [9] 전환·자막 |
| `prompts.json` | [5] | **씬별 이미지 지시.** 시간 정보를 담지 않는다 | [6] `prompt`·`negative_prompt`·`style`, [8] `overlays` |
| `timing.json` | [3] | **[3]의 실행 기록.** 엔진 메타·배속·원속 길이·오디오 길이·경고. 계약이 아니라 기록이라 길이 초과로 멈출 때도 남는다 | [11] 리포트, 사람 |
| `images.json` | [6] | **[6]의 실행 기록.** 프로바이더·앵커 수·씬별 상태/재시도/폴백. 계약이 아니라 기록이다 | [11] 리포트, 사람 |
| `subtitles.ass` | [9] | 자막 파일. `ass` 필터의 입력이라 파일로 존재해야 한다 | [9] 번인 |

- 씬의 시각을 읽는 곳은 `scenes.timed.json` **하나뿐이다.** 같은 숫자를 두 파일이 들고 있으면 갈라지고, 갈라진 쪽을 읽은 단계만 싱크가 어긋난다
- `prompts.json`의 `beat`·`subject_scale`·`camera`·`motion`은 `06-script.json`에서 복사해 온 값이다. **고치는 곳은 `06-script.json` 하나다**
- `prompts.json`의 `framing_reuse_of`는 **이미지를 재사용하라는 뜻이 아니다.** 구도만 같고 그 씬의 `subject`는 가리키는 씬과 다르다 — [6]이 캐시 힌트로 읽으면 안 된다
- `prompts.json`의 `subject`는 한국어 그대로 프롬프트에 들어간다. 번역하지 않고(ADR-0001·0014), 대신 그 한국어를 화면에 글자로 그리지 말라고 명시한다 (ADR-0002)
- 씬당 베이스 이미지는 1장 = 호출 1회다 (ADR-0019). `overlays`는 전부 레이어 B라 [6]이 아니라 [8] 소관이다

- 소스 카드(`knowledge/`)는 run·토픽에 종속되지 않는 **누적 자산**이라 run 디렉터리에 두지 않는다 (ADR-0012)
- `slug`: 음운 변화 미적용 로마자 표기, `[a-z0-9-]` 최대 60자 (ADR-0011)
- `run_id`: `YYYYMMDD-{slug}`
- 헤드리스 세션에는 읽기 도구만 준다. 산출물 파일은 오케스트레이터가 쓴다 (ADR-0011)

### 1부 ↔ 2부 경계 (ADR-0017)

두 부는 아래 **두 파일로만** 만난다. 2부는 그 밖의 1부 산출물에 의존하지 않는다.

| 파일 | 역할 |
|---|---|
| `topics/{slug}/06-script.json` | 씬 계약. 1부의 최종 산출물이자 2부의 **읽기 전용** 입력 |
| `topics/{slug}/judgment/human.json` | 게이트. `decision: go`일 때만 2부 진입 (스펙 07 스키마) |

- **2부는 `06-script.json`을 수정하지 않는다.** 모든 2부 산출물은 `runs/{run_id}/` 아래에 쓴다
  (`narration.wav`, `timing.json`, `scenes.timed.json`, `prompts.json`, `images/`, `images.json`,
  `clips/`, `subtitles.ass`, `timeline.mp4`, `final.mp4`, `report.md`).
  `runs/*`는 `.gitignore` 대상이라 미디어가 커밋되지 않는다
- 계보는 `run_id`로 잇는다. 2부 산출물은 대본과 같은 `run_id`의 run 디렉터리에 놓인다

## 단계별 규칙

- **[0b. research]**: 스펙 06의 소스 등급(A~D)에 따라 웹 검색·수집. 1차 사료 필수 아님, 위키백과는 B등급 근거로 인정, 확정되지 않은 유력 학설은 `confidence: medium` (ADR-0016). 해외 소재는 영어 검색 병행. verdict: fail 시 백로그 반려하고 종료. 팩트시트는 사람 검수 지점 (유일한 수동 게이트).
- **[1. script]**: 팩트시트 그라운딩 (ADR-0007). 팩트시트에 없는 수치·연도·인명 사용 금지. 세션은 `beat`·`text`·`subject`·`subject_scale`만 출력하고 타임스탬프·카메라·오버레이·모션은 오케스트레이터가 룰 테이블로 채운다 (ADR-0014, ADR-0018). `subject_scale`은 연출이 아니라 피사체 서술이라 세션 몫이다. `confidence: low` 사실은 프롬프트에 주입하지 않는다. 검증 실패 시 이 단계는 재생성하지 않는다 — 후보를 쓰고 검증 결과를 함께 넘긴다. **후보 수는 `[1b. score]` 도입 전까지 1개다** (채점기 없이 여러 개를 만들면 고를 수단이 없다).
- **[1b. score]**: shorts-hook-scorer 루브릭(hook_strength / info_density / standalone) 기반 LLM 채점. 텍스트 생성 비용은 무시 가능한 수준이므로 후보 수는 비용이 아니라 채점 신뢰도로 결정. 전 후보가 기준 미달이면 주제 자체를 반려하고 리포트. 추후 실제 조회수 데이터로 루브릭 보정(피드백 루프)을 v2 과제로 둔다.
- **[2. validate]**: 스펙 01의 검증 5항목 + 스펙 02 스키마 검증 + 그라운딩 검증(대본 숫자 전수 추출 → 팩트시트 대조, ADR-0007). 실패 시 실패 사유를 프롬프트에 피드백하여 재생성 (최대 3회, 초과 시 중단·리포트).
- **[3. tts+sync]**: 대본 전체 단일 호출 + with-timestamps. 문자 정렬에서 씬(자막 줄) 경계 start/end 추출해 `runs/{run_id}/scenes.timed.json`을 **새로 쓴다**(ADR-0017. `06-script.json`은 건드리지 않는다) — 문장 경계를 뽑은 뒤 각 줄의 마지막 문장 끝만 취한다 (ADR-0013). 실측-추정 오차 씬당 ±1.5초 초과 시 경고. 총 길이 102초 초과 시 **대본 축약이 필요하다고 리포트하고 멈춘다** — 2부가 1부를 직접 다시 돌리지 않는다 (ADR-0017의 단방향 경계). 재생성은 사람이 1부를 다시 실행해 판단한다. atempo 1.1 적용 후 타임스탬프도 1/1.1 스케일 보정.
- **[1x. backfill-scale]**: `subject_scale`이 없는 옛 `06-script.json`에만 쓴다 (ADR-0018). 세션에 `subject`만 보여주고 `wide`/`close`/`diagram` 판정을 받아 그 필드 하나만 끼운다. **대본을 바꾸지 않는다** — 쓰기 전에 나머지 필드를 전부 대조하고 하나라도 다르면 파일을 쓰지 않고 실패한다. 전 씬에 값이 있으면 세션을 부르지 않는다. 새 토픽은 `[1]`이 직접 채우므로 이 단계를 타지 않는다.
- **[5. prompt]**: 구도는 `(beat × subject_scale)` 표에서만 나온다 (스펙 03, ADR-0018). 다른 씬을 가리키는 구도(`@prev`/`@hook`)는 가리키는 씬의 `subject_scale`이 같을 때만 잇고, 다르면 스케일별 기본값으로 떨어진다. 오버레이는 전부 레이어 B다 — 베이스 이미지는 전 씬 클린이고 2-pass 어노테이션 경로가 없다 (ADR-0019).
- **[6. imagegen]**: 씬당 1회 재시도. 2회 실패 시 인접 씬 이미지 재사용(카메라 워크만 변경)으로 폴백하고 리포트에 기록. 편당 베이스 호출 = 씬 수 (2-pass 없음, ADR-0019).
- **[7. motion]**: `motion` 필드 분기 (ADR-0006). `kenburns` → FFmpeg zoompan(camera 값 적용). `kling` → v2.6 Turbo급 i2v 5초 무음, 2회 실패 시 kenburns 강등. 클립 길이 = 씬 길이 + 디졸브 겹침 0.6초.
- **[9. assemble]**: 자막은 **scenes.timed.json** 기준 ASS 생성 후 번인 (ADR-0020 — 씬의 `text`·`start`·`end`를 읽는 곳은 이 파일 하나다). 전환 규칙(스펙 03)이 비트에 걸려 있어 같은 파일의 `beat`을 함께 본다. 싱크 오차 ±200ms 이내 검증.

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
