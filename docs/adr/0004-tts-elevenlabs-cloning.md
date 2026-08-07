# ADR-0004: TTS는 ElevenLabs 본인 목소리 클로닝을 사용한다 (IVC 개발 → PVC 운영)

- 상태: 승인
- 날짜: 2026-08-07
- 관련 스펙: specs/04-audio-rules.md, specs/05-pipeline.md

## 맥락

채널 차별화 요소로 운영자 본인 목소리를 사용하기로 결정. TTS 엔진은 (1) 한국어 클로닝 품질, (2) 문장별 타임스탬프 확보 방식, (3) 톤 연속성 제어 가능 여부가 선정 기준이다. ElevenLabs는 with-timestamps 엔드포인트가 문자 단위 정렬을 반환하므로 별도 forced alignment 단계가 불필요하고, request stitching으로 톤 연속성을 제어할 수 있다.

## 결정

- 엔진: ElevenLabs, 본인 목소리 클로닝
- 개발 단계: IVC(Instant Voice Clone)로 voice_id 확보 후 파이프라인 개발
- 운영 단계: PVC(Professional Voice Clone, Creator 플랜 이상)로 승격. **녹음은 반드시 쇼츠 내레이션 톤으로** 30분 이상 진행 (클론은 녹음 톤을 따라감)
- 생성 방식: 대본 전체를 단일 호출로 생성(request stitching 불요) + with-timestamps로 문자 단위 정렬 수신 → 문장 경계 타임스탬프 추출
- 배속: 원속 생성 후 FFmpeg atempo 1.1 후처리를 기본값으로. speed 파라미터 방식과 A/B 청취 후 확정
- voice_id는 환경변수로 주입 (IVC→PVC 교체 시 코드 무변경)

## 검토한 대안

| 대안 | 장점 | 단점 | 탈락 사유 |
|---|---|---|---|
| 문장별 개별 TTS 호출 | 씬 단위 재생성 용이 | 문장 간 톤 불연속 | 내레이션 품질이 채널 핵심 |
| 타사 TTS (Google/Azure/로컬) | 비용 | 클로닝 품질, 타임스탬프 정밀도 열위 | 본인 목소리 요구사항 |
| Whisper forced alignment로 싱크 | 엔진 독립적 | 파이프라인 단계 추가, 오차 누적 | with-timestamps로 불필요 |

## 결과

- 스펙 05의 [3. tts]와 [4. sync]를 단일 단계로 통합 (timing.json을 tts 단계가 직접 산출)
- 씬 단위 대본 수정 시 전체 재생성 필요 (비용: 편당 ~570자 → 수용 가능한 수준)
- 되돌릴 조건: 단일 호출 생성에서 특정 문장 발음 오류가 반복되어 부분 재생성 요구가 커지면 stitching 기반 문장별 생성으로 전환 재검토
