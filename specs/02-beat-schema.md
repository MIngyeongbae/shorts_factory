# 02. 씬 비트 태깅 스키마

대본의 각 자막 줄(=씬)에 붙는 태그 체계. 이 태그가 하위 파이프라인(시각 룰, 오디오 룰)의 유일한 분기 기준이다.

씬의 단위는 문장이 아니라 자막 줄이다 (ADR-0013). 한 씬의 `text`에 문장이 1~3개 들어갈 수 있고, 그 경우 비트는 그 줄을 대표하는 하나만 붙인다.

## 비트 타입 (beat)

| beat | 의미 | 대응 템플릿 단계 |
|---|---|---|
| `hook_fact` | 충격 사실 제시 | 1 |
| `hook_twist` | "~가 아닙니다" 반전 | 1 |
| `context` | 배경 설명 | 2 |
| `context_number` | 핵심 숫자 포함 배경 | 2, 6 |
| `failed_solution` | 실패하는 해법 제시 | 3, 4 |
| `failure_reason` | 실패 이유 설명 | 3, 4 |
| `dilemma_peak` | "환장할 노릇이죠" 절정 | 4 |
| `turning_point` | "발상을 뒤집습니다" | 5 |
| `solution_step` | 해결책 단계 설명 | 6 |
| `solution_number` | 해결책 내 핵심 숫자 | 6 |
| `present_link` | 현재와 연결 | 7 |
| `ending_echo` | 수미상관 마무리 | 7 |

## 씬 JSON 스키마

```json
{
  "scene_id": 14,
  "beat": "turning_point",
  "text": "그래서 발상을 뒤집습니다.",
  "est_start": 44.0,
  "est_end": 45.8,
  "emphasis": {
    "type": "big_red_text",
    "value": "발상"
  },
  "visual_goal": "뒤집기의 대상 — 본문이 '발상'이라고만 부르고 넘어가는 그 공법",
  "subject": "성벽 축조 현장",
  "subject_scale": "wide",
  "camera": "slow_zoom_in",
  "motion": "kenburns",
  "notes": ""
}
```

## 필드 규칙

- `scene_id`: 1부터 연번. 자막 줄 순서와 일치 (ADR-0013).
- `beat`: 위 테이블의 값만 허용 (enum).
- `est_start`/`est_end`: TTS 생성 전 추정치. **이 값은 갱신되지 않는다** (ADR-0017).
  `[3. tts+sync]`가 실측값을 `runs/{run_id}/scenes.timed.json`에 **새로 쓰며**, 그 파일에서
  필드명은 `start`/`end`다. 추정(`est_*`)과 실측(`start`/`end`)은 파일 단위로 분리된다.
- `emphasis`: 화면 강조 요소. `type`은 `03-visual-rules.md`의 오버레이 타입 enum. 숫자 비트는 필수, 그 외 옵션.
- `visual_goal`: **이 그림이 지는 설명** (한국어 한 구절). `[1. script]`가 쓴다 (ADR-0022).
  - 글만으로는 90~102초 안에 전달할 양에 한계가 있다. 그림이 설명의 일부를 지면
    **같은 분량 안에 더 많은 사실이 들어간다.** 분량 기준(스펙 01)은 바뀌지 않는다 —
    바뀌는 것은 그 분량 안의 밀도다
  - **`text`가 이미 말한 것을 되풀이하면 그 그림은 하는 일이 없다.** 본문이 말하지
    않고 넘어가는 것, 말로는 길어지는 것을 그림이 진다
  - 비어 있거나 `text`와 거의 같으면 검증에서 걸린다
- `subject`: `visual_goal`을 달성할 **핵심 피사체** (한국어). 이미지 프롬프트의 재료다.
  - **`visual_goal`이 먼저다.** 무엇을 설명할지 정하고 그것을 담을 피사체를 고른다
  - **실존 구조물은 이름을 적는다.** `subject`는 화면에 나오지 않으므로 이름을 적어도
    반전이 새지 않는다 — 숨겨야 하는 것은 내레이션이지 그림 지시가 아니다
  - 본문과 어긋나지 않는다. 본문이 "무너집니다"면 무너진 것을 적는다
  - 숫자 비트는 **숫자가 가리키는 대상**을 적는다. 숫자 자체는 레이어 B다 (ADR-0002)
  - **분위기용 금지.** 사람이 서 있는 것, 펼쳐 둔 노트처럼 설명을 지지 않는 것은 쓰지 않는다
- `subject_scale`: `wide` | `close` | `diagram`. `subject`를 화면에 담는 크기다.
  `beat`와 함께 구도를 결정한다 (`03-visual-rules.md`, ADR-0018). **연출이 아니라 피사체
  서술이므로 `[1. script]`가 `subject`와 함께 쓴다** (ADR-0014).
  - `wide`: 대상 전체·부지·전경 (기본값 성격)
  - `close`: 표면·끝단·접합면·계측기 등 근접 디테일
  - `diagram`: 단면도·평면도·도해·일러스트·기록 노트
- `camera`: `slow_zoom_in` | `slow_zoom_out` | `tilt_down` | `tilt_up` | `pan_left` | `pan_right` | `static` 만 허용. 복합 카메라 워크 금지 (AI 영상 왜곡 방지).
- `motion`: `kenburns`(기본) | `kling`. `kling`은 유체 모션(물·비·안개·불)이 서사상 필요한 씬에만, 편당 최대 10씬 (ADR-0006).

## 산출 방식

대본 생성 LLM이 대본과 태그를 **한 번에** 출력한다 (별도 분류 단계 없음 — ADR-0003).
