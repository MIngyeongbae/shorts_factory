# shorts-factory

Spec-Driven Development로 운영하는 AI 쇼츠 자동 생성 파이프라인.

- 시작점: `CLAUDE.md` (프로젝트 헌법) → `specs/` (스펙) → `docs/adr/` (결정 기록)
- Claude Code 커맨드: `/adr <주제>`, `/spec-check`

## 구현 현황

`specs/05-pipeline.md`의 1부 `[0]`~`[2l]`(ADR-0049·0056)와 2부 `[3]`·`[3s]`·`[4]`·`[5]`·`[7]`·`[8]`·`[9]`.
**2부는 ADR-0056(2026-08-22 승인)으로 갈아엎는 중이다** — 아래 표가 현재 위치다.

```
topics/backlog.md (소재 + 시드 기사 URL)
  → [0. seed]      topics/{slug}/ + seed.md + runs/{run_id}/  (기계 단계, LLM 0회)
  → [1. draft]     script.md   시드 기사 → 통짜 내레이션. 직후 기계 엔벨로프 검사 (LLM 0회)
  → [2. factcheck] factcheck.md + script.md 정정   대본이 쓴 주장만 검증 (스펙 06)
  → [2l. localize] script.ja.md + script.en.md   줄 1:1 번안 — 한 세션에 두 언어. 직후 줄 수·빈 줄 검사 (구현됨)
  → 사람 게이트    script.md를 읽고 STATUS.md에 go / no-go (ADR-0009) — ko의 go가 셋의 go다
  → [3. tts]       narration.{lang}.wav + scenes.timed.{lang}.json — 언어당 1회, ko 필수 (구현됨)
  → [3s. scenetable] scenes.json — ko 실측 위에서 1회. staging·ASCII 라벨·annotation (구현됨)
  → [4. refpack]   refs.json (서술 경로)
  → [5. prompt]    prompts.json — 영상 프롬프트 골격, 영어 문장은 전부 vocab.json (구현됨)
  → [7. videogen]  clips/ + clips.json + clip_review.json — 씬당 클립 1개, 어댑터는 human.json의
                   video_line (로컬 ComfyUI+H3 기본 / Omni / MJ 예약 — ADR-0059), 끝 프레임 OCR +
                   씬당 비전 검수, 강등 사다리 (구현됨 — 실편 관통은 아직. 오프라인 관통은 `--provider fake`)
  → [8. ending]    엔딩 실사 컷 (구현됨, 실편 관통은 아직)
  → [9. assemble]  timeline.{lang}.mp4 — 언어당 1회, 언어별 트림·자막·나레이션 (구현됨)
```

| ADR-0056 코드 단계 | 상태 |
|---|---|
| **A1** 삭제(`[6]`·`[6r]`·`[6i]`·NB2·MJ 영상·켄번스·기록 스키마 3개) · `transport.py` 공용화 · 로더(`staging`·`annotation`·`unit`·`video_prompt`) · `[5]` 재작성 · `[3s]` 템플릿 | **완료** (837 tests) |
| **A2** `videogen/omni.py`(Gemini Interactions API, 프로브 `runs/20260822-omni-api-probe/`) · `[7] videogen`(씬당 1클립·OCR·씬당 비전 검수·강등) · `[3]`·`[9]` 언어 루프 · CLI | **완료** (960 tests). 실편 관통·Omni `duration` 필드의 효과 실측은 첫 실편에서 |
| **B** `[2l] localize` + `script-rules.json` 로케일 로더 | **완료**. 언어별 엔벨로프(`locales.{lang}.limits`)는 블록이 있으면 읽고 없으면 줄 수 일치만 — 초 상한은 `[3]`이 실측으로 본다 (값은 실편 5편 뒤, ADR-0056 되돌릴 조건 4) |

**MJ 이미지 어댑터(`imagegen/base·midjourney·fake`)는 휴면 코드다** — 어느 단계도 부르지
않고 계약 테스트만 돈다 (ADR-0056 결정 1). `[10] mix`는 여전히 미구현이다.

## 준비물

- Python 3.11+
- Claude Code CLI (`claude`)가 PATH에 있고 **구독 인증**이 되어 있을 것 (ADR-0008)

```bash
pip install -r requirements.txt
```

## 실행

```bash
# [0] 백로그의 '후보' 항목을 토픽 폴더로 승격 (LLM 0회)
python run.py topic --topic "파나마 운하 산 위의 호수"
# --seed-url 로 백로그의 시드 컬럼을 덮어쓸 수 있다

# [1] 시드 기사 → 통짜 대본 script.md (헤드리스 1회)
python run.py draft --slug panama-unha-san-wiui-hosu -v

# [2] 대본이 쓴 주장만 검증·정정 → factcheck.md (헤드리스 1회)
python run.py factcheck --slug panama-unha-san-wiui-hosu -v

# [2l] 검증 끝난 정본 → script.ja.md + script.en.md, 줄 1:1 (헤드리스 1회, 두 언어를 한 세션에 — ADR-0056)
python run.py localize --slug panama-unha-san-wiui-hosu -v
python run.py localize --slug panama-unha-san-wiui-hosu --lang ja   # 한 언어만
# 이미 있는 script.{lang}.md는 덮어쓰지 않는다 (사람이 고쳤을 수 있다) — 다시 만들려면 그 파일을 지우고 돌린다

# [0]+[1]+[2]+[2l] 연속 실행 — 토픽당 LLM 세션 3회 (ADR-0049·0056)
python run.py part1 --topic "파나마 운하 산 위의 호수"

# [3] 확정 대본 → narration.{lang}.wav + 실측 타임스탬프 (2부, 언어당 과금 — ko 필수, ja·en은 대본이 있으면)
python run.py tts --slug hubeodaem-konkeuriteu-naenggak
python run.py tts --slug hubeodaem-konkeuriteu-naenggak --lang ko,ja   # 일부 언어만 (ko는 항상 든다)

# [3s] ko 실측 줄 경계 → 씬 계약 scenes.json (2부, 헤드리스 1회 — 새 편 전용)
python run.py scenetable --slug panama-unha-san-wiui-hosu -v

# [4] 씬별 실사 참조 → refs.json + refs/{scene_id}/ (2부, 과금 0)
python run.py refpack --slug hubeodaem-konkeuriteu-naenggak

# [5] 씬 계약 → 씬별 영상 프롬프트 prompts.json (2부, 과금 0)
python run.py prompt --slug hubeodaem-konkeuriteu-naenggak

# [7] 씬당 텍스트→영상 클립 + 끝 프레임 OCR + 씬당 비전 검수 (2부 — ADR-0056·0057·0059)
#     어댑터는 topics/{slug}/judgment/human.json 의 video_line 이 정한다. 비우면 로컬 라인(ComfyUI + H3,
#     변동비 0 — ComfyUI Desktop이 떠 있어야 한다). Omni 유료 라인은 video_line 또는 --provider omni
python run.py videogen --slug hubeodaem-konkeuriteu-naenggak
# --review ocr|none 으로 검수 세션을 끊고, --provider fake 로 네트워크 없이 배관만 관통한다 (testsrc 클립)

# [8] 실사 참조 → 엔딩 실사 컷 (2부, 과금 0 — ADR-0055. 쓸 사진 없으면 엔딩 없이 간다)
python run.py ending --slug hubeodaem-konkeuriteu-naenggak

# [9] 클립 + 언어별 실측 → timeline.{lang}.mp4 (2부, 언어당 1회 — 디졸브 + 자막 번인 + 나레이션)
python run.py assemble --slug hubeodaem-konkeuriteu-naenggak
python run.py assemble --slug hubeodaem-konkeuriteu-naenggak --lang ja
```

`[8]`은 `[4] refpack`이 내려받아 둔 사진만 쓴다 — 새로 수집하지 않는다. `[4]`를 안
돌린 편에서는 조용히 엔딩 없이 끝난다 (선택적 입력의 부재는 경고가 아니다).

`[3]`의 **엔진은 `--provider`가 고르고 기본은 타입캐스트다** (ADR-0081). 쓰는 것은
`.env`의 그 제공자 키와 **언어별** voice_id다 — 타입캐스트는 `TYPECAST_API_KEY` +
`TYPECAST_VOICE_ID_KO`·`_JA`·`_EN`, ElevenLabs(`--provider elevenlabs`)는
`ELEVENLABS_API_KEY` + `ELEVEN_VOICE_ID_KO`·`_JA`·`_EN`이다 (ADR-0004·0056·0081).
**ko는 접미사 없는 옛 이름(`TYPECAST_VOICE_ID`·`ELEVEN_VOICE_ID`)으로도 떨어진다.**
대본 파일이 있는 언어의 id가 비어 있으면 **어느 언어도 부르기 전에** 멈춘다. 키 없이
경로만 확인하려면 `--provider fake`를 준다 — **무음 wav가 나오므로 기본값이 아니다.**

`[7]`의 로컬 라인은 `.env`의 `COMFY_URL`(기본 `http://127.0.0.1:8188`)·`COMFY_H3_MEGAPIXELS`
(비우면 템플릿의 0.4)를, 유료 라인은 `GEMINI_API_KEY`(Omni Flash, 유료 티어)를 쓴다. 로컬 라인은
GPU 추론 중 Windows가 "유휴"로 보고 절전에 들어갈 수 있다 — `powercfg /change standby-timeout-ac 0`
(2026-08-23 BSOD 1회, worklog (35)). 끝 프레임 OCR 게이트는
PATH에 `tesseract`가 있을 때만 돌고, 없으면 건너뛰고 `clip_review.json`에 경고를 남긴다 —
비전 검수는 그대로 돈다. 언어별 자막 폰트는 `SUBTITLE_FONT_JA`·`SUBTITLE_FONT_EN`
환경변수로 덮어쓴다 (비우면 `subtitle-style.json`의 `font_name`).

주요 옵션 (서브커맨드 앞뒤 어느 위치에서도 동작):

| 옵션 | 설명 |
|---|---|
| `--force` | 완료된 단계도 다시 실행 |
| `--model sonnet` | 헤드리스 세션 모델 지정 (기본: Claude Code 설정값) |
| `-v` | 디버그 로그 |

중간에 끊겨도 같은 `run_id`로 다시 실행하면 완료된 단계는 스킵된다 (`runs/{run_id}/state.json`).

## 종료 코드

| 코드 | 의미 |
|---|---|
| 0 | 성공 |
| 1 | 단계 실패 |
| 2 | `[0]` 반려 |
| 3 | `[1]` 매체 부적합 반려 (`part1`에서. 사유는 script.md에 있다) |
| 4 | 1부 산출물이 기계 검사에 걸림 (산출물은 남는다 — 다시 돌릴지는 사람이 정한다, ADR-0044·0049) |
| 5 | `[2l]` 번안 검사 실패 — 줄 수 불일치·빈 줄·언어별 엔벨로프. **실패한 언어의 파일은 쓰지 않고** 통과한 언어는 남는다. ko 정본은 그대로다 (ADR-0056) |
| 6 | `[0f]` 시드 본문을 얻지 못함 (ADR-0061). **파이프라인을 세우지 않는다** — `part1`은 무시하고 `[1]`로 간다. 단계를 따로 부른 사람에게만 알린다 |
| 8 | `[7]` 단계 실패 — 입력 부재, 클립을 만들지 못한 씬 |
| 9 | `[3]` TTS 호출·계약 실패 (ja·en 줄 수 불일치 포함 — 호출 전에 막힌다) |
| 10 | `[3]` 어느 언어의 총 길이 상한 초과 — 그 언어의 대본 축약이 필요하다 (1부 소관, ADR-0017) |
| 11 | `[3]` 키·언어별 voice_id(`TYPECAST_VOICE_ID*` / `ELEVEN_VOICE_ID*`)·플랜 미비. **호출 전에 막히므로 과금이 없다** |
| 13 | `[4]` refpack 실패 — 입력 부재·계약 위반. 세션도 내려받기도 무료라 다시 돌리면 된다 |
| 15 | `[3s]` 연출표 실패 — 계약 위반이면 `scenes.json`을 쓰지 않는다 (ADR-0044) |
| 16 | `[8]` 엔딩 단계 실패 (판정 세션 출력이 JSON이 아님 등). **쓸 사진이 없는 것은 실패가 아니다** — 0으로 끝난다 |
| 17 | `[7]` 프로바이더 전체 거절 (키·플랜·파라미터) — 남은 씬을 시도하지 않고 멈췄다. 산 클립과 기록은 남고, 다시 돌리면 `done` 씬은 건너뛴다 |
| 130 | 사용자 중단 (Ctrl+C) |

과금 단계(`[3]`·`[7]`)는 고칠 자리가 저마다 달라서 코드를 나눴다 — 11·17은 `.env`(또는
플랜), 10은 1부 대본, 9·8은 호출 자체다. 옛 `[6]`·`[6i]`의 코드는 ADR-0056으로 단계와 함께
사라졌고 **6은 ADR-0061의 `[0f]`가 새로 받았다.** 7·12·14는 비어 있다 — 다시 쓰지 않는다.

## 산출물

```
topics/{slug}/          # 사람이 읽는 토픽 패키지 (specs/06, ADR-0049)
├── seed.md             # [0] 소재 + 시드 기사 URL
├── script.md           # [1] 대본 — 1부의 최종 산출물이자 2부의 읽기 전용 입력 (ADR-0017 개정)
├── factcheck.md        # [2] 주장별 판정·근거 URL·정정 내역 + 반전 노트
├── script.ja.md · script.en.md   # [2l] 번안 대본 — script.md와 줄 1:1. 없으면 그 언어의 쇼츠가 없을 뿐 (ADR-0056)
└── STATUS.md           # go / no-go / 보류 — go는 사람만 기록한다 (ADR-0009)

runs/{run_id}/          # 기계용 단계 간 계약 (ADR-0011)
├── topic.json
├── state.json
├── localize.json       # [2l] 언어별 결과 기록 (생성/기존 유지/실패) — 계약이 아니다
├── narration.{lang}.wav · timing.{lang}.json · scenes.timed.{lang}.json   # [3] 언어당 셋
├── scenes.json         # [3s] 씬 계약 (언어 무관)
├── refs/ refs.json     # [4] 씬별 실사 참조 (사진 + 서술)
├── prompts.json        # [5] 씬별 영상 프롬프트
├── clips/ clips.json clip_review.json clip_review/   # [7] 채택 클립 + 기록 + 검수 재료(원본·프레임)
├── ending/ ending.json # [8] 엔딩 실사 컷 + credits.txt (없는 편이 정상이다)
├── subtitles.{lang}.ass · timeline.{lang}.mp4        # [9] 언어당 둘
└── logs/
```

## 테스트

```bash
python -m pytest
```

네트워크와 구독 한도를 쓰지 않는다. LLM 호출은 `FakeLLMClient` 픽스처로 대체된다.
