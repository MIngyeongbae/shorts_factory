# specs/schema — 기계가 읽는 계약

스펙 문서에서 enum·스키마·어휘를 코드로 **손 복사하던 것**을 그만두기 위해 만든 폴더다
(ADR-0034 §3).

```
specs/01-script-template.md      ← 왜 그런가.  사람이 읽는다
specs/schema/script-rules.json   ← 무엇인가.  코드가 로드한다
```

## 파일

| 파일 | 무엇 | 읽는 곳 |
|---|---|---|
| `vocab.json` | 닫힌 어휘 전부 — beat·subject_scale·camera·구도 토큰·전환·무대(staging)·계측 표시(annotation)·단위(unit) + 스타일 문자열과 **프롬프트 영어 문구** (ADR-0056) | `schemas/vocab.py` → `scenes.py`·`visual_rules.py`, `[3s]` 세션 프롬프트, `[5]` 프롬프트 골격 |
| `scene.schema.json` | 씬 계약(`scenes.json`)의 JSON Schema. 어휘는 `vocab.json`을 `$ref`한다 | `schemas/scenes.py`, 파생으로 `timed_scenes.py` |
| `sceneplan.schema.json` | `[3s]` 세션 산출(씬 연출표) — 실측 줄 위의 연출·무대·계측 표시·인물 선택 (ADR-0049 §5) . 필드 정의는 `scene.schema.json`을 `$ref`한다 | `schemas/sceneplan.py` |
| `promptplan.schema.json` | `[5]` 세션 산출(씬별 샷 서술) — SUBJECT 단락·카메라 착지·RED 기하, 영어·ASCII·길이 (ADR-0060). 골격 조립은 코드가 `vocab.json`에서 | `schemas/promptplan.py` |
| `script-rules.json` | 대본 결과 제약(분량 엔벨로프·검사 값) + 언어별 로케일 블록 자리 (ADR-0056) | `schemas/script_rules.py` |
| `subtitle-style.json` | 번인 자막의 렌더 값 (레이어 B, ADR-0002) | `video/subtitles.py` |
| `speech-rules.json` | 발화형 — TTS에 보낼 때 숫자·단위를 그 언어가 읽는 대로 펴는 로케일별 수사표·단위 사전 (ADR-0063). 자막은 원문을 쓴다 | `schemas/speech_rules.py` → `tts/speech.py` |
| `beat-defaults.json` | 비트별 연출 **기본값**. 지시가 아니라 폴백이고, ADR-0033을 되돌릴 자리다 | `schemas/visual_rules.py` |
| `refs.schema.json` | `[4]` 산출(`refs.json`) — 씬별 실사 참조(서술)와 게시 가능 라이선스 (ADR-0030·0055) | `schemas/refs.py`, `[8]` |
| `ending.schema.json` | `[8]` 산출(`ending.json`) — 엔딩 실사 컷 계약 (ADR-0055) | `schemas/ending.py` |

씬 연출표에서 `scenes.json`으로 **어느 필드가 그대로 건너가는지는 아무 데도 손으로
적지 않는다** — `sceneplan.schema.json`과 `scene.schema.json`의 교집합이고
`sceneplan.carried_fields()`가 계산한다 (ADR-0034).

## 규칙

1. **값은 여기 한 번만 적는다.** 스펙 문서는 이 파일을 *참조*하고 값을 다시 적지 않는다.
   문서에 표로 옮겨 적는 순간 `mj_video` 사고가 재발한다 (ADR-0025 §3이 승인한 enum 값이
   스펙에도 코드에도 없었다).
2. **코드는 선언하지 않고 로드한다.** `MOTIONS = (...)` 같은 손 복사를 만들지 않는다.
3. 값을 바꾸는 것은 스펙 변경이다. **ADR을 먼저 쓴다** (CLAUDE.md 절대 원칙 2·4).
4. `_role`·`_note`로 시작하는 키는 사람에게 하는 설명이고 계약이 아니다. 코드는 `_`로
   시작하는 키를 무시한다.

## 왜 JSON인가

마크다운 표를 파싱하는 안은 탈락했다 — 표 서식이 계약이 되면 문서 한 줄을 고칠 때마다
파서가 깨진다. 산문과 데이터를 한 파일에 섞지 않는다 (ADR-0034 검토한 대안).
