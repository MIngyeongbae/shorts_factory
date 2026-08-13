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
| `vocab.json` | 닫힌 어휘 전부 — beat·subject_scale·camera·motion·구도 토큰·오버레이·전환·방언 + 검증된 스타일 문자열 | `schemas/vocab.py` → `scenes.py`·`visual_rules.py`, `[1s]` 세션 프롬프트 |
| `scene.schema.json` | 씬 계약(`06-script.json`)의 JSON Schema. 어휘는 `vocab.json`을 `$ref`한다 | `schemas/scenes.py`, 파생으로 `timed_scenes.py` |
| `script-rules.json` | 대본 결과 제약(분량 엔벨로프)과 시그니처 문구 | `schemas/script_rules.py` |
| `beat-defaults.json` | 비트별 연출 **기본값**. 지시가 아니라 폴백이고, ADR-0033을 되돌릴 자리다 | `schemas/visual_rules.py` |

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
