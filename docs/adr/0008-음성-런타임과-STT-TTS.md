# 0008. 음성 런타임은 Pipecat, STT·TTS는 Gemini

- 상태: 채택 (Windows에서 런타임 설치·실행은 3단계 시작 때 확인. 실패하면 새 ADR)
- 날짜: 2026-10-14
- 관련: `docs/research/korean-stt-tts.md`, `docs/design.md` §5.2, §8

## 맥락

음성 입력은 한국어 스트리밍 STT(미확정 자막 포함)가, 출력은 한국어 TTS가 필요하다. 설계 §8은 AI 고객의 음성이 실제 STT 경로를 통과해야 한다고 정했으므로, 녹음 파일을 실제 STT에 흘리는 자동 테스트 경로가 있어야 한다. 사용할 수 있는 자격 증명은 Gemini API 키와 Cloudflare 토큰뿐이다.

## 검토한 대안

2026-10-14 실제 호출로 확인했다.

- Google Cloud STT·TTS: 가진 키가 AI Studio 인증 키라 401(`API keys are not supported by this API`). 서비스 계정이 있어야 한다.
- Workers AI Deepgram nova-3·flux·aura: 한국어 미지원(`ko`를 거부하거나 의미 없는 출력).
- Workers AI Whisper(large-v3-turbo): 한국어 정확, 배치 전용이라 미확정 자막이 없다.
- Workers AI MeloTTS: 한국어 가능(`lang: "kr"`), 금액의 쉼표에서 숫자를 끊어 읽는 것으로 보인다.
- Gemini 3.5 Transcribe Live: 한국어 스트리밍, 약 0.5초 간격 미확정 자막, 발화 종료 후 1.8~2.0초에 확정.
- Gemini 3.8 Flash(-Lite) TTS: 한국어, 오디오 스트리밍, 예열 후 첫 오디오 0.7~0.8초.
- LiveKit Agents: 끼어들기 처리(재생 위치 기준 이력 자르기, 잘못된 끼어들기 복구)가 더 성숙하다. 그러나 단위 테스트는 텍스트 전용이고, 음성 입력 테스트는 LiveKit Cloud나 문서화되지 않은 내부 클래스에 기대야 한다.
- Pipecat: 평가 도구의 오디오 모드가 로컬에서 WAV 파일을 실제 STT로 흘리고, 끼어들기도 시나리오로 낼 수 있다. Gemini STT 서비스가 인식 모드 설정을 노출하고, 한국어 턴 감지 모델(Smart Turn)이 공개 가중치다.

## 결정

- 음성 런타임은 Pipecat으로 한다. 상담 엔진은 Pipecat의 프레임 처리기로 붙이고, 엔진 코드는 런타임에 의존하지 않게 둔다.
- STT는 Gemini 3.5 Transcribe Live를 VERBATIM 모드로 고정한다. 녹음 재생 회귀와 교차 확인용 배치 STT는 Workers AI whisper-large-v3-turbo를 쓴다.
- TTS는 Gemini 3.8 Flash-Lite TTS를 기본으로, 3.8 Flash TTS를 품질 대안으로 둔다.

## 근거

- 고른 기준은 한국어 스트리밍 STT·TTS 지원과 테스트 전송 경로다. 지금 자격 증명으로 한국어 미확정 자막을 주는 STT는 Gemini Transcribe Live뿐이고, 그 STT를 로컬 자동 테스트에 실제로 태울 수 있는 런타임은 Pipecat이다.
- Gemini Transcribe의 SMART 모드는 "구월 십오일 … 아 아니다, 칠월 십오일이었어요"를 "7월 15일에 결제한 …"으로 정리해 정정 발화 자체를 지웠다. 이 프로젝트의 핵심 지표가 정정 이후의 근거 갱신이므로, 인식기가 정정을 지우면 엔진은 변경을 볼 수 없다. 그래서 VERBATIM을 고정한다.

## 예상되는 결과

- STT와 TTS가 한 공급자(Google)에 몰린다. 두 번째 한국어 스트리밍 STT는 서비스 계정이나 Deepgram 키가 생기면 추가한다.
- 끼어들기 시 이미 말한 문장의 잘림은 문장 단위일 가능성이 크다. 무효 제안 노출 측정에서 음성 노출은 TTS 재생 시작 기준이므로 측정 정의에는 영향이 없다.
- 확인한 음성은 모두 깨끗한 합성 음성이다. 잡음, 8kHz 전화 음질, 겹치는 발화는 3단계 이후 따로 확인한다.
- Gemini TTS 가격은 2027-01-01에 두 배가 된다.
