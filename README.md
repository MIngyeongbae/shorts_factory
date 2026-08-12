# shorts-factory

Spec-Driven Development로 운영하는 AI 쇼츠 자동 생성 파이프라인.

- 시작점: `CLAUDE.md` (프로젝트 헌법) → `specs/` (스펙) → `docs/adr/` (결정 기록)
- Claude Code 커맨드: `/adr <주제>`, `/spec-check`

## 구현 현황

`specs/05-pipeline.md`의 1부 중 `[0a. topic]` ~ `[0b. research]`.

```
topics/backlog.md
  → [0a. topic]     topics/{slug}/ 생성, runs/{run_id}/topic.json
  → [0b. research]  01-research.md → 02-verify.md → 03-critique.md → 04-factsheet.json
                    (각각 독립 헤드리스 세션, ADR-0009)
```

이후 단계(`[1. script]` ~)는 미구현.

## 준비물

- Python 3.11+
- Claude Code CLI (`claude`)가 PATH에 있고 **구독 인증**이 되어 있을 것 (ADR-0008)

```bash
pip install -r requirements.txt
```

## 실행

```bash
# [0a] 백로그의 '후보' 항목을 토픽 패키지로 승격
python run.py topic --topic "한양도성 각자성석"

# [0b] 조사 → 검증 → 비판 → 팩트시트
python run.py research --slug hanyangdoseong-gakjaseongseok -v

# [0a] + [0b] 연속 실행
python run.py package --topic "한양도성 각자성석"

# [3] 확정 대본 → narration.wav + 실측 타임스탬프 (2부, 편당 과금)
python run.py tts --slug hubeodaem-konkeuriteu-naenggak
```

`[3]`은 `.env`의 `ELEVENLABS_API_KEY`·`ELEVEN_VOICE_ID`를 쓴다 (ADR-0004). 키 없이
경로만 확인하려면 `--provider fake`를 준다 — **무음 wav가 나오므로 기본값이 아니다.**

주요 옵션 (서브커맨드 앞뒤 어느 위치에서도 동작):

| 옵션 | 설명 |
|---|---|
| `--force` | 완료된 단계도 다시 실행 |
| `--only 02-verify` | `[0b]`의 서브스텝 하나만 실행 |
| `--model sonnet` | 헤드리스 세션 모델 지정 (기본: Claude Code 설정값) |
| `-v` | 디버그 로그 |

중간에 끊겨도 같은 `run_id`로 다시 실행하면 완료된 단계는 스킵된다 (`runs/{run_id}/state.json`).

## 종료 코드

| 코드 | 의미 |
|---|---|
| 0 | 성공 |
| 1 | 단계 실패 |
| 2 | `[0a]` 반려 (백로그 4조건 미충족) |
| 3 | `[0b]` 반려 (`verdict: fail`) |
| 4 | `[1]` 대본 후보가 구조 검사에 걸림 |
| 5 | `[2]` 검증 실패 (재생성 상한까지 못 고침) |
| 6 | `[6]` 이미지 생성 실패 |
| 7 | `[6]` 스타일 앵커 0장 차단 (`--allow-missing-anchors`로 해제) |
| 8 | `[7]` 클립 렌더 실패 |
| 9 | `[3]` TTS 호출·계약 실패 |
| 10 | `[3]` 총 길이 상한 초과 — 대본 축약이 필요하다 (1부 소관, ADR-0017) |
| 11 | `[3]` 키·`ELEVEN_VOICE_ID`·플랜 미비. **호출 전에 막히므로 과금이 없다** |
| 130 | 사용자 중단 (Ctrl+C) |

과금 단계(`[3]`·`[6]`)는 고칠 자리가 저마다 달라서 코드를 나눴다 — 11은 `.env`,
10은 1부 대본, 9는 호출 자체다.

## 산출물

```
topics/{slug}/          # 사람이 읽는 토픽 패키지 (specs/06)
├── 01-research.md      # 조사
├── 02-verify.md        # 검증
├── 03-critique.md      # 비판
├── 04-factsheet.json   # 팩트시트 (하류의 유일한 사실 원천, ADR-0007)
├── 05-candidates/
└── STATUS.md           # go / no-go / 보류 — go는 사람만 기록한다 (ADR-0009)

runs/{run_id}/          # 기계용 단계 간 계약 (ADR-0011)
├── topic.json
├── state.json
├── research.json
└── logs/
```

## 테스트

```bash
python -m pytest
```

네트워크와 구독 한도를 쓰지 않는다. LLM 호출은 `FakeLLMClient` 픽스처로 대체된다.
