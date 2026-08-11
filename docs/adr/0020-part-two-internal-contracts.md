# ADR-0020: 2부 내부 계약을 세 파일로 나누고 값의 출처를 하나로 고정한다

- 상태: 승인
- 날짜: 2026-08-11
- 관련 스펙: specs/05-pipeline.md, specs/02-beat-schema.md, specs/03-visual-rules.md
- 선행: ADR-0017(1부↔2부 경계), ADR-0018·0019(스펙 03 룰)

## 맥락

`[3. tts+sync]`와 `[5. prompt]`가 구현되면서 2부 안에 계약 파일이 셋 생겼다.
스펙 05는 이 파일들의 **이름과 생산자만** 적고 있고 스키마도, 누가 무엇을 읽는지도
정의한 적이 없다. `[6]`·`[7]`·`[8]`·`[9]`를 병렬로 만들려면 그게 먼저 확정돼야 한다 —
네 단계가 같은 파일을 서로 다르게 가정하면 합칠 때 드러난다.

실물(후버댐 27씬)로 세 파일의 필드를 대조했다.

| 파일 | 생산자 | 최상위 | 씬 항목 |
|---|---|---|---|
| `prompts.json` | `[5]` | `run_id` `topic` `source_script` `style` `scenes` | `scene_id` `beat` `subject_scale` `camera` `motion` `framing` `framing_source` `prompt` `negative_prompt` `overlays` |
| `timing.json` | `[3]` | `run_id` `topic` `engine` `tempo` `raw_duration` `total_duration` `audio` `warnings` `cues` | `cues[]`: `scene_id` `text` `start` `end` |
| `scenes.timed.json` | `[3]` | `run_id` `topic` `total_duration` `scenes` | `scene_id` `beat` `text` `start` `end` `subject` `subject_scale` `camera` `motion` `notes` |

**`timing.json`의 `cues`가 `scenes.timed.json`의 완전 부분집합이다.** 필드 4개가
전부 들어 있고 27씬 전부 값도 같다(`start`/`end`/`text`). 같은 숫자를 두 파일이 들고 있으면
언젠가 갈라지고, 갈라진 쪽을 읽은 단계만 싱크가 어긋난다.

없앨 수 있는가를 확인했다. `cues`의 유일한 예정 소비자는 `[9. assemble]`의 자막(ASS)
생성인데, **`[9]`는 어차피 `scenes.timed.json`을 읽어야 한다** — 스펙 03의 전환 규칙이
비트에 걸려 있기 때문이다("`turning_point` 진입 시 디졸브 없이 컷", "하드컷은
`hook_twist`·`dilemma_peak` 진입 시에만"). `beat`은 `timing.json`에 없다.

또 하나 확정할 것이 있다. `[5]`는 `06-script.json`만 읽고 네트워크도 API 키도 쓰지 않는다.
**`[3]`에 의존하지 않는다.** 스펙 05의 그림이 `[3] → [5]` 순서로 그려져 있어 마치
선후가 있는 것처럼 읽히는데, 실제로는 나란히 돌 수 있다. 외부 의존(ElevenLabs 키)이
아직 없는 지금 이건 실용적인 차이다 — `[5] → [6]`까지는 TTS 없이 간다.

## 결정

세 파일에 **서로 겹치지 않는 역할**을 주고, 값 하나의 출처를 하나로 고정한다.

| 파일 | 역할 | 소비자 |
|---|---|---|
| `scenes.timed.json` | **씬의 유일한 출처.** 대본 속성(`beat`·`text`·`subject`·`subject_scale`·`camera`·`motion`)과 실측 시각(`start`/`end`) | `[7]`(클립 길이·카메라·모션), `[9]`(전환·자막) |
| `prompts.json` | **씬별 이미지 지시.** 시간 정보를 담지 않는다 | `[6]`(`prompt`·`negative_prompt`·`style`), `[8]`(`overlays`) |
| `timing.json` | **`[3]`의 실행 기록.** 엔진 메타·배속·원속 길이·오디오 길이·경고 | `[11]`(리포트), 사람(디버깅) |

이에 따라 **`timing.json`에서 `cues`를 뺀다.** 씬의 시각을 읽는 곳은 `scenes.timed.json`
하나다. `timing.json`은 하류 단계가 판단 근거로 읽는 계약이 아니라 **무슨 일이
일어났는지의 기록**이고, 그래서 길이 초과로 멈춰 `scenes.timed.json`을 쓰지 않는
경우에도 남는다(호출은 편당 과금이다 — 무엇이 얼마나 넘쳤는지는 이 파일에서 읽는다).
`total_duration`을 남기는 이유도 그것뿐이다.

`prompts.json`은 지금 모양을 그대로 동결한다. 다음 셋을 계약 문구로 못 박는다.

1. **`subject`는 한국어 그대로 프롬프트에 들어가고 번역하지 않는다.** 번역하면 `[1]`이
   고른 피사체가 `[5]`의 창의적 판단으로 바뀐다(ADR-0001·0014 위반). 대신 그 한국어가
   화면에 글자로 그려지지 않도록 프롬프트에 명시한다(ADR-0002)
2. **`framing_reuse_of`는 이미지를 재사용하라는 뜻이 아니다.** 구도만 같다는 표시다 —
   그 씬의 `subject`는 가리키는 씬과 다르다. `[6]`이 이걸 캐시 힌트로 읽으면 안 된다
3. **씬당 베이스 이미지는 1장, 호출 1회다** (ADR-0019로 2-pass가 사라졌다).
   `overlays`는 전부 레이어 B라 `[6]`이 아니라 `[8]` 소관이다

`prompts.json`에 `camera`·`motion`이 `scenes.timed.json`과 겹치는 것은 **남긴다.**
Ken Burns 씬의 구도는 카메라 워크와 무관하지 않기 때문이다(`pan_right`면 오른쪽에
볼 것이 있어야 한다 — ADR-0006의 zoompan). 지금 프롬프트가 그걸 쓰지는 않지만
`[6]`·`[7]` 실측 후 쓰게 될 자리라, 잘라 냈다가 되돌리는 쪽이 비싸다. **다만 값을 고치는
곳은 `06-script.json` 하나다** — 두 파일 다 거기서 복사해 온다.

`[3]`과 `[5]`는 **선후가 없다.** 둘 다 `06-script.json`만 읽는다. 스펙 05의 단계 그림에
그 사실을 적는다.

## 검토한 대안

| 대안 | 장점 | 단점 | 탈락 사유 |
|---|---|---|---|
| `cues`를 남기고 `[9]`가 `timing.json`만 읽게 한다 | `[9]`가 파일 하나만 연다 | 전환 규칙이 `beat`에 걸려 있어 불가능하다. `beat`까지 `cues`에 복사하면 씬 계약을 통째로 두 번 쓰게 된다 | 스펙 03 전환 규칙과 모순 |
| `timing.json`을 없애고 `scenes.timed.json`에 엔진 메타를 합친다 | 파일 2개로 줄어든다 | 길이 초과로 멈출 때 `scenes.timed.json`을 **일부러 안 쓰는데**(하류가 계약 파일이 있다는 이유로 진행해 버리는 것을 막는다, ADR-0017) 그러면 편당 과금한 호출의 기록이 사라진다 | 실패 경로에서 증거를 잃는다 |
| 세 파일을 하나로 합친다 | 조인이 없다 | `[5]`가 `[3]`을 기다리게 된다. TTS 키가 없는 지금 `[6]`까지 못 간다 | 두 단계의 독립성을 잃는다 |
| 스키마를 JSON Schema 파일로 따로 빼서 공유한다 | 언어 중립 | 지금 소비자가 전부 같은 파이썬 패키지 안에 있다. 스키마는 이미 `schemas/`에 코드로 있고 산출물 검증에 쓰인다 | 얻는 것 없이 중복 |

## 결과

**스펙**

- `specs/05-pipeline.md` — "단계 간 계약 파일" 표에 2부 파일 3개 추가(역할·소비자),
  단계 그림에 `[3]`∥`[5]` 병렬 표기, `[9]`가 `scenes.timed.json`을 읽는다는 규칙 명시

**코드**

- `stages/tts.py` — `build_timing`에서 `cues` 제거. `timing.json`은 기록 전용이 된다
- `tests/test_tts_stage.py` — `cues` 관련 검사를 `scenes.timed.json` 쪽으로 옮긴다
- `schemas/visual_rules.py`의 `PROMPTS_SCHEMA`는 그대로. `timing.json`은 스키마를
  만들지 않는다 — 계약이 아니라 기록이라 `additionalProperties: false`로 조일 이유가 없다

**비용**

- 없음. 파일 크기가 줄고 조인 지점이 하나 사라진다

**되돌릴 조건**

- `[9]`를 만들다 `scenes.timed.json`만으로 자막을 못 만드는 사정이 나오면 `cues`가
  아니라 **씬 계약에 필드를 추가**하는 쪽을 먼저 검토한다 (출처는 계속 하나)
- `timing.json`을 하류 단계가 판단 근거로 읽기 시작하면 그 순간 기록이 아니라 계약이다.
  그때는 스키마를 만들고 이 ADR을 대체한다
