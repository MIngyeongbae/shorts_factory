# 07. 판단 자율성 (Human in the loop → on the loop)

> **보류 (ADR-0049).** 판정은 사람이 `script.md`를 읽고 한다 — AI 심사관·자율성
> 사다리·revise 루프는 구현하지 않는다 (LLM 채점 게이트는 실측으로 반증됐다:
> 호르무즈 편 채점기는 병을 정확히 보고도 pass를 냈다). 이 문서에서 지금 유효한
> 것은 **판정 스키마의 human.json**뿐이다 — `decision: go`가 2부 진입 게이트다
> (스펙 05 경계). 나머지는 장래 재검토용 설계 기록으로 남긴다.

go/no-go 판단을 사람에서 AI 심사관으로 단계적으로 이양하는 설계. 원칙: **자율성은 스위치가 아니라 다이얼이며, 이양의 전제는 판단의 데이터화다.**

## 판정 스키마 (사람·AI 공용)

사람과 AI 심사관은 동일한 스키마로 판정을 기록한다. `topics/{slug}/judgment/` 아래에 `human.json`, `ai.json`으로 분리 저장.

```json
{
  "judge": "human | ai",
  "decision": "go | revise | no_go",
  "video_line": "vocab.json video_line 중 하나 — 비우면 기본 라인 (사람만 적는다, ADR-0059 결정 2)",
  "confidence": 0.82,
  "scores": {
    "hook": 2, "narrative": 4, "grounding": 5, "freshness": 3, "retention_risk": 2
  },
  "reason_code": "hook_no_twist",
  "notes": "자유 서술 (보조)",
  "fixable": true,
  "fix_directives": [
    {"target": "scene 1-2", "instruction": "팩트시트 f03 근거로 '~가 아니다' 반전 구조로 재작성"}
  ],
  "judged_at": "2026-08-07T21:00:00+09:00"
}
```

### video_line — 영상 라인 (ADR-0059)

`decision: go`와 같은 자리에서 **같은 사람이** 2부의 영상 라인을 고른다 — 어느 라인이 있고 기본이
무엇이며 각 라인이 어느 어댑터로 가는지는 `specs/schema/vocab.json`의 `video_line`(`$defs` enum +
`meta`)이 정본이다. 비우면 기본 라인이고, 어휘 밖의 값이면 `[3s]`·`[7]`이 **멈춘다** — 오타가
기본 라인으로 조용히 내려가면 유료·무료가 뒤바뀐다. `ai.json`에는 이 필드가 없다 — 라인은 소재
유형 규칙으로 자동 선택하지 않는다 (ADR-0059 검토한 대안).

### reason_code enum (no_go / revise 시 필수)

`hook_no_twist` | `hook_weak` | `dilemma_forced` | `numbers_thin` | `too_complex_for_90s` |
`overlap_reference_channel` | `grounding_doubt` | `present_link_weak` | `boring_solution` | `other`

- enum이어야 집계·보정이 가능하다. `other`는 반복 등장 시 enum 추가 검토 (스펙 수정)
- scores 축은 5개 고정 (1~5점): hook(훅 강도), narrative(서사 완결성), grounding(사실 신뢰),
  freshness(레퍼런스 채널 대비 신선도), retention_risk(이탈 위험, 높을수록 안전)

## 자율성 사다리

| 레벨 | 판단 주체 | 승격 조건 | 강등 조건 |
|---|---|---|---|
| L0 | 사람 100% | — | — |
| L1 섀도우 | 사람 결정. AI는 병행 판정만 기록 | 즉시 시작 | — |
| L2 조건부 자율 | AI가 확신 구간 전결, 중간 구간은 사람 에스컬레이션. 자동 처리분 20% 샘플 감사 | 판정 쌍 ≥50건 AND false-go율 ≤5% AND scores 축별 상관 확인 | 샘플 감사 불일치 >10% |
| L3 on the loop | AI 전결. 사람은 10~20% 샘플 감사 + 성과 대시보드 감시 | L2 감사 무결 3주 AND AI 판정과 실측 조회수의 상관 검증 | 감사 불일치 초과 또는 발행 성과 연속 하락 (concept drift) |

### 규칙

- **앵커링 방지 (L1)**: 사람은 AI 판정(ai.json)을 보지 않고 독립 판정한다. 도구가 사람 판정 저장 전까지 ai.json을 노출하지 않는다
- **오류 비용 비대칭**: 승격 지표는 전체 일치율이 아니라 **false-go율** (AI go, 사람 no-go). false-no-go는 기회비용일 뿐이므로 관대하게 취급
- **신뢰도 라우팅 (L2)**: AI confidence ≥ 상위 임계값이면 자동 go, ≤ 하위 임계값이면 자동 no-go, 중간이면 에스컬레이션. 임계값은 L1 판정 쌍 데이터로 산출
- **강등은 자동**: 조건 충족 시 사람 개입 없이 한 레벨 하강하고 리포트에 기록

## revise 루프 (수정 제안)

1. decision=revise 시 fix_directives(씬 단위 구체 지시)를 **[1a. outline]부터** 제약으로 주입해 재생성 (ADR-0029). 심사관이 무는 것은 서사·훅이라 문장만 고쳐서는 답이 안 나온다
2. 재생성본은 재심사. **수정 상한 2회** — 초과 시 no_go 확정 (무한 루프 방지 + "수정 불가 소재" 신호)
3. 수정 유효성 기록: 재심사 scores 상승 여부 → 심사관의 수정 제안 능력 자체를 평가하는 데이터

## 데이터 축적과 보정

- (human.json, ai.json) 쌍 → 일치율·false-go율 대시보드, L2 임계값 산출
- reason_code 분포 → 대본 프롬프트·스코어러 루브릭·소재 관측 지표의 보정 입력
- 발행 후 실측 조회수 → **최종 진실**. L3부터 심사관의 보정 기준을 사람 모방에서 실측 예측으로 전환한다.
  사람 판단도 프록시이며, 실측과 충돌하면 실측이 우선
