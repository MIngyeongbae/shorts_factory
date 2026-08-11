# ADR-0021: Nano Banana 2 호출은 Gemini REST를 stdlib로 직접 친다

- 상태: 승인
- 날짜: 2026-08-11
- 관련 스펙: specs/03-visual-rules.md, specs/05-pipeline.md
- 관련 ADR: ADR-0005(모델 선정), ADR-0008(SDK 의존성 회피 선례), ADR-0019(2-pass 폐기)

## 맥락

ADR-0005가 정한 것은 **모델**("Nano Banana 2 + 스타일 앵커, 2K, 4K 금지")이지 **호출
방법**이 아니다. `[6] imagegen`은 단계 로직(재시도·폴백·기록)까지 만들어 놓고
`imagegen/nano_banana.py`가 여섯 가지 미결정 항목을 열거하며 `ProviderNotConfigured`로
멈추는 상태다. 그 여섯을 정해야 실물이 된다.

**Nano Banana 2는 Google Gemini 이미지 모델이다.** 공식 문서와 실제 키 조회로 확인했다:

- 모델 ID `gemini-3.1-flash-image` (Lite: `gemini-3.1-flash-lite-image`, Pro: `gemini-3-pro-image`)
- 인증: Google AI Studio API 키. 문서의 curl 예시가 `$GEMINI_API_KEY`를 쓴다
- **이미지 생성은 유료 티어 필수**
- 파라미터: `response_format={"type":"image","aspect_ratio":"9:16","image_size":"2K"}`
- 레퍼런스 이미지: base64로 `input` 배열에 첨부

**계약값이 API 파라미터와 그대로 맞는다.** 실물 `prompts.json`의 `style` 블록은
`aspect_ratio: "9:16"`, `resolution: "2K"`인데 이게 API의 허용값(`9:16` / `2K`)과
문자열까지 일치한다. 변환 테이블이 필요 없다.

### 비용 — ADR-0005의 산정을 갱신한다

공식 단가는 `gemini-3.1-flash-image` **2K = $0.101/장**이다. ADR-0019가 2-pass를
없애 편당 호출이 씬 수와 같아졌으므로:

| | 씬 수 | 성공 시 | 전 씬 1회 재시도(최악) |
|---|---|---|---|
| 후버댐 | 27 | **$2.73** | $5.46 |
| 피사 | 25 | **$2.53** | $5.05 |

ADR-0005의 "편당 ~50호출 ≈ $2~2.5"는 **장당 단가를 낮게 잡고 호출 수를 높게 잡은
추정이었다.** 실제는 반대(장당 $0.101 / 호출 27회)인데 총액은 거의 같은 자리에
떨어진다. 편당 목표 ~$7(ADR-0017) 안이다.

### 앵커 상한이 3장이다 — ADR-0005 수정 대상

`gemini-3.1-flash-image`의 레퍼런스 상한은 **오브젝트 10 + 캐릭터 4 + 스타일 3**이다.
ADR-0005는 "스타일 앵커 이미지 **3~5장**"을 전제하는데 **5장은 넣을 수 없다.**

## 결정

**여섯 항목을 아래로 확정한다.**

| 항목 | 결정 |
|---|---|
| 1. 전송 경로 | **REST + stdlib `urllib`.** 새 의존성 0개 |
| 2. 모델 식별자 | `gemini-3.1-flash-image` (preview 접미사 없는 안정 ID) |
| 3. 인증 | `GEMINI_API_KEY` 환경변수. `.env`는 config.py의 stdlib 로더가 읽는다 |
| 4. 앵커 첨부 | base64 인라인, **상한 3장** |
| 5. 종횡비·해상도 | `response_format`에 `aspect_ratio`·`image_size`. 계약값을 그대로 넘긴다 |
| 6. 오류 매핑 | HTTP 429 → `ImageGenRateLimited`, 401/403 → `ProviderNotConfigured`, 타임아웃 → `ImageGenTimeout`, 그 외 → `ImageGenError` |

**SDK를 쓰지 않는 이유**가 이 결정의 핵심이다. 우리가 쓰는 엔드포인트는 **하나**이고,
`google-genai`는 `pydantic`·`google-auth`·`httpx` 등 전이 의존성을 끌고 온다. 현재
`requirements.txt`는 `jsonschema`·`pytest` **두 줄**이고, ADR-0008은 "LLM 호출은 claude
CLI 서브프로세스라 SDK 의존성이 없다"며 같은 선택을 이미 한 번 했다. 어댑터
인터페이스(`ImageClient`)가 호출 표면을 이미 좁게 잘라 뒀으므로, SDK가 필요해지면
`nano_banana.py` 한 파일만 바꾸면 된다.

**앵커는 3장으로 확정한다.** 상한이 3이므로 "3~5장"은 실현 불가능한 범위다.

## 검토한 대안

| 대안 | 장점 | 단점 | 탈락 사유 |
|---|---|---|---|
| **`google-genai` SDK** | API 형태 변경을 SDK가 흡수, 응답 파싱 자동 | 전이 의존성 다수(`pydantic`·`google-auth`·`httpx`), 의존성 2줄 → 10+줄 | 엔드포인트 **1개**를 위해 치르는 비용이 크다. ADR-0008이 같은 상황에서 이미 SDK를 뺐다 |
| **Vertex AI 경로** | GCP IAM·할당량 관리, 조직 과금 | GCP 프로젝트·서비스 계정·`google-cloud-aiplatform` 필요 | 1인 운영에 GCP 프로젝트 관리가 붙는다. AI Studio 키 한 줄이면 되는 일이다 |
| **`gemini-3-pro-image`(Pro)** | 품질 상위 | 2K $0.134/장 → 편당 $3.62 (+33%), **스타일 레퍼런스 항목이 문서에 없다** | ADR-0005가 이미 Pro 대비 ~95% 품질에 1/3 비용으로 NB2를 골랐다. 룩 일관성 수단이 불확실한 쪽으로 갈 이유가 없다 |
| **`gemini-3.1-flash-lite-image`** | 1K $0.0336/장 → 편당 $0.91로 최저, **레퍼런스 14장** | **2K 미지원**(0.5K/1K만) | ADR-0005가 "2K, 4K 금지"로 못박았다. Kling i2v 입력 화질 기준을 못 맞춘다 |
| **배치 API (50% 할인)** | 편당 $2.73 → $1.37 | 비동기 제출·폴링, 응답 지연 | 편당 $1.4 절감을 위해 단계 구조를 비동기로 바꿀 만하지 않다. 편수가 늘면 재검토(되돌릴 조건) |
| **`python-dotenv`** | 표준적 | 의존성 1줄 추가 | `.env` 파싱은 stdlib 15줄이다. 이 프로젝트의 의존성 정책과 안 맞는다 |

## 실호출 1회로 확인한 것 (2026-08-11, $0.101)

앵커 3장 + 실제 대본(후버댐 3번 씬, `section_diagram`)으로 한 번 쳤다.

- **유료 티어가 살아 있다.** 30.8초 / 2.6MB / 1536×2752 (9:16 2K)
- **`mime_type: image/png`은 400으로 거절당한다.** 서버 메시지 그대로:
  `"The value 'image/png' is not supported for 'response_format.mime_type'.
  Supported values: 'image/jpeg'."` → **산출물이 `images/{scene_id}.jpg`가 됐다.**
  PNG로 변환해도 손실 압축이 이미 일어난 뒤라 화질이 돌아오지 않고 의존성만 는다
- **응답에 `output_image` 편의 필드가 없다.** 이미지는 `steps[].content[]`로만 온다.
  최상위 키는 `created`·`id`·`model`·`object`·`service_tier`·`status`·`steps`·`updated`·`usage`.
  두 경로를 모두 읽도록 구현해 둬서 파싱이 그대로 통과했다 — 되돌릴 조건이 걸리지 않았다
- 남은 미확인은 **레이트리밋 응답 형태**뿐이다. 429를 아직 못 봤다

## 결과

**바뀌는 코드**

- `imagegen/nano_banana.py` — `generate()` 본문 구현. `UNDECIDED` 상수 제거
- `config.py` — `.env` 로더(stdlib) + `GEMINI_API_KEY` 조회
- `requirements.txt` — **변경 없음** (이 결정의 요점)
- 테스트 — HTTP 경계를 목으로 갈아끼워 검증. **실제 호출 0회**

**바뀌는 스펙·ADR**

ADR-0005의 두 줄을 고쳐야 한다:

```diff
-- 스타일 일관성: 승인된 **스타일 앵커 이미지 3~5장**을 리포지토리 에셋으로 관리하고
+- 스타일 일관성: 승인된 **스타일 앵커 이미지 3장**을 리포지토리 에셋으로 관리하고
   모든 생성 호출에 레퍼런스로 첨부. 앵커 변경은 스펙 03 수정으로 취급
+  (gemini-3.1-flash-image의 스타일 레퍼런스 상한이 3장이다 — ADR-0021)

-- 비용 산정: 편당 ~50호출(재시도·2-pass 포함) ≈ $2~2.5. NB Pro 전환 시 ~$6.7
+- 비용 산정: 편당 호출 = 씬 수(ADR-0019). 2K $0.101/장 → 27씬 $2.73,
+  전 씬 1회 재시도 최악 $5.46. NB Pro 전환 시 편당 $3.62 (ADR-0021)
```

**비용**

편당 $2.53~2.73 (재시도 없을 때). 편당 목표 ~$7 안이다.

**되돌릴 조건**

- **유료 티어가 아니면 첫 호출이 막힌다.** 모델 목록 조회는 무료 티어에서도 되므로
  키 검증만으로는 알 수 없다 — 첫 실제 호출이 유일한 확인 수단이다
- Gemini API 응답 형태가 바뀌어 파싱이 두 번 이상 깨지면 SDK로 간다. 파싱을
  `nano_banana.py` 한 곳에 몰아 두는 이유다
- 편수가 늘어 편당 $1.4 절감이 유의미해지면 배치 API를 재검토한다
- 앵커 3장으로 룩 일관성이 부족하면 `gemini-3-pro-image`(레퍼런스 방식 재조사 필요)
  또는 ADR-0005의 되돌릴 조건(FLUX 2)을 탄다
