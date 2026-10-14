# 한국어 STT·TTS와 음성 런타임 조사

> 이 문서는 한국어 음성 인식(STT)·음성 합성(TTS) 서비스와 음성 런타임(LiveKit, Pipecat)을 실제 호출과 공식 문서로 비교한 조사 기록이다.
> 음성 구성을 바꾸거나 [결정 기록 0008](../adr/0008-음성-런타임과-STT-TTS.md)의 근거를 확인하려는 사람이 읽으면 된다.

- 날짜: 2026-10-14 (호출은 01:58~02:15 KST, 서울)
- 결정: [ADR 0008](../adr/0008-음성-런타임과-STT-TTS.md)
- 표기: **[호출]** 실제 호출로 확인, **[문서]** 공식 문서나 소스로만 확인

## 자격 증명

- [호출] `GCP_API_KEY`는 AI Studio 인증 키라 Gemini API(`generativelanguage.googleapis.com`)에만 쓸 수 있다. Google Cloud TTS v1, Speech-to-Text v1·v2는 헤더와 쿼리 어느 쪽으로 보내도 401 `API keys are not supported by this API. Expected OAuth2 access token ...`을 반환했다. API 미사용 설정(403)이 아니라 키 종류 문제다.
- [문서] 2026-05-28 이후 새 AI Studio 키는 모두 서비스 계정에 묶인 인증 키이며 기본적으로 Gemini API로 제한된다(https://ai.google.dev/gemini-api/docs/api-key).
- [호출] Cloudflare 토큰은 계정 범위 토큰이다.

## 서비스별 결과

입력 음성은 7.6초. "예열"은 같은 연결로 3회 호출한 값이다. 지연은 표본이 작아 백분위수가 아니다.

### TTS

| 서비스 | 동작 | 한국어 | 스트리밍 | 지연 [호출] | 가격 [문서] |
|---|---|---|---|---|---|
| `gemini-3.8-flash-tts` | 예 | 예 | 오디오 출력 스트리밍(24kHz PCM), 텍스트는 한 번에 | 첫 오디오 예열 0.68~1.59초, 전체 2.5~2.8초 | 입력 $0.50/1M, 오디오 $9/1M(10초 약 $0.00225), 2027-01-01부터 두 배 |
| `gemini-3.8-flash-lite-tts` | 예 | 예 | 같음 | 첫 오디오 예열 0.67~0.82초, 전체 1.75~1.88초 | 입력 $0.50/1M, 오디오 $6/1M(10초 약 $0.0015), 2027-01-01부터 두 배 |
| `gemini-3.1-flash-tts-preview` | 예 | 예 | 확인 안 함 | 6.66초 | $1 / $20 |
| Google Cloud TTS | 아니오(401, 키 종류) | [문서] 예 | [문서] gRPC | - | $30/1M자 |
| Workers AI `melotts` | 예(`lang: "kr"`만, `"ko"`는 거부) | 예, 숫자 읽기 약함 | 아니오 | 예열 1.99~3.42초 | 오디오 1분 $0.0002 |
| Workers AI Deepgram Aura | 호출은 됨 | 아니오(의미 없는 음성) | - | - | - |

### STT

| 서비스 | 동작 | 한국어 | 스트리밍 | 지연 [호출] | 가격 [문서] |
|---|---|---|---|---|---|
| `gemini-3.5-transcribe` | 예 | 예 | 배치 | 예열 1.53~1.57초 | 약 $0.005/분 |
| `gemini-3.5-transcribe-live` (WebSocket) | 예 | 예 | 미확정 자막 약 0.5초 간격, 발화 시작·종료 이벤트 | 첫 미확정 자막 1.3~1.5초, 발화 종료 후 확정 1.8~2.0초 | 약 $0.009/분, 세션 최대 10분 |
| Google Cloud STT v1·v2 | 아니오(401, 키 종류) | [문서] 예 | [문서] gRPC | - | V2 $0.016/분 |
| Workers AI `whisper` | 예 | 예 | 배치 | 3.21초 | $0.0005/분 |
| Workers AI `whisper-large-v3-turbo` | 예 | 예 | 배치 | 1.03~4.82초(편차 큼) | $0.0005/분 |
| Workers AI Deepgram `nova-3` | 영어만 | 아니오(`ko` 거부, `multi`는 의미 없는 출력) | WebSocket | - | - |
| Workers AI Deepgram `flux` | 연결됨 | 아니오 | WebSocket | - | - |

가격 출처: https://ai.google.dev/gemini-api/docs/pricing, https://developers.cloudflare.com/workers-ai/platform/pricing/

## 인식 결과 원문 [호출]

TTS 입력: `구월 십오일에 결제한 구천구백 원 환불받고 싶어요. 아 아니다, 칠월 십오일이었어요.` (Gemini 3.8 Flash TTS 음성)

| STT | 결과 |
|---|---|
| gemini-3.5-transcribe (기본, VERBATIM) | `9월 15일에 결제한 9,900원 환불받고 싶어요. 아, 아니다. 7월 15일이었어요.` |
| gemini-3.5-transcribe, SMART | `7월 15일에 결제한 9,900원 환불받고 싶어요.` |
| transcribe-live, SMART | `7월 15일에 결제한 9,900원 환불받고 싶어요.` |
| transcribe-live, VERBATIM 또는 기본 | `9월 15일에 결제한 9,900원 환불받고 싶어요. 아, 아니다. 7월 15일이었어요.` |
| transcribe-live 미확정 자막(오디오 시작 후 초) | 1.52 `9월` → 2.07 `9월 15일에` → 4.15 `… 9,900원 환불` → 6.75 `… 아, 아니다.` → 7.81 `… 7월` → 8.44 `… 7월 15일이었어요.` → 확정 9.60 |
| whisper | `9월 15일에 결제한 9,900원 환불 받고 싶어요. 아, 아니다. 7월 15일이었어요.` |
| whisper-large-v3-turbo | `9월 15일에 결제한 9,900원 환불받고 싶어요. 아, 아니다, 7월 15일이었어요.` |
| nova-3 `multi` | `あ 兄 だ ちら し ぼ い 入れ やす さ よ` |

- SMART 모드는 정정 발화("9월", "아 아니다")를 지우고 정정된 날짜만 남긴다. 엔진이 정정 사건을 볼 수 없게 되므로 VERBATIM을 쓴다.
- 동작하는 한국어 STT는 모두 날짜와 금액을 숫자로 돌려준다. 날짜·금액 해석은 숫자 형태를 기본으로 한다.
- 엔진 출력 방향(`… 9,900원 중 환불 가능 금액은 6,600원입니다. 3개월 이용권은 2개 …`): Gemini TTS 음성은 그대로 인식됐다("2개"는 "두 개"로 읽음). MeloTTS 음성은 `9 900원`처럼 쉼표에서 끊겨 인식됐다(음성을 직접 듣지 않고 인식 결과로 추정).

## 음성 런타임 비교 [문서]

LiveKit Agents 1.8.6(2026-10-09), Pipecat 1.12.0(2026-09-26) 기준.

| 항목 | LiveKit Agents | Pipecat |
|---|---|---|
| Gemini Transcribe Live | `google.beta.GeminiSTT`, 미확정 자막, 인식 모드 설정 없음(서버 기본값 VERBATIM) | `GeminiSTTService`, 미확정 자막, `mode` 설정(기본 VERBATIM) |
| Gemini TTS | `google.beta.GeminiTTS` | `GeminiTTSService` |
| Workers AI 음성 | 플러그인 없음(직접 어댑터 필요) | 플러그인 없음(직접 어댑터 필요) |
| 실제 STT를 통과하는 로컬 자동 테스트 | 단위 테스트는 텍스트 전용. 음성 입력은 LiveKit Cloud(`lk agent debugger --audio`, Agent Simulations, 유료)나 문서화되지 않은 내부 클래스 필요 | Pipecat Evals 오디오 모드가 WAV 파일을 실제 STT로 실시간 전송. 끼어들기를 시나리오로 지정 가능 |
| 끼어들기 | 재생 위치 기준 이력 자르기, 잘못된 끼어들기 복구. 적응형은 Cloud 기능 | `InterruptionFrame`이 LLM, 취소 표시한 함수 호출, TTS 버퍼를 취소. 자르기는 문장 단위일 가능성 |
| 한국어 턴 감지 | `TurnDetector`(LiveKit 모델 라이선스, 전체 모델은 LiveKit Inference) | Smart Turn v3.2(BSD-2 공개 가중치) |
| 자체 엔진 연결 | `llm.LLM` 하위 클래스 또는 `llm_node` 재정의 | 사용자 정의 `FrameProcessor` |
| 라이선스 | Apache-2.0 | BSD-2-Clause |

출처:

- https://github.com/livekit/agents
- https://docs.livekit.io/testing/overview.md
- https://docs.livekit.io/testing/simulations.md
- https://github.com/pipecat-ai/pipecat
- https://docs.pipecat.ai/pipecat/evals/overview.md
- https://docs.pipecat.ai/pipecat/fundamentals/interruptions.md
- https://github.com/pipecat-ai/smart-turn

## 확인하지 않은 것

- 두 런타임 모두 이 PC(Windows)에서 실행해 보지 않았다.
- Pipecat Evals의 합성 사용자 음성에 한국어가 있는지. 없으면 Gemini TTS로 만든 WAV를 쓴다.
- 잡음, 8kHz 전화 음질, 겹치는 발화. 모든 입력은 깨끗한 합성 음성이었다.
