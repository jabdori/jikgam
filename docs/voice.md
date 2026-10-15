# 음성 연결(Pipecat)

상담 엔진(`jikgam/engine/`)을 고치지 않고 한국어 음성 파이프라인에 붙인 방식과 측정 방법이다. 결정은 [ADR 0008](adr/0008-음성-런타임과-STT-TTS.md), 서비스 조사는 [korean-stt-tts.md](research/korean-stt-tts.md), 측정 정의는 [evaluation.md](evaluation.md) §3을 따른다.

## 1. 구성

```
녹음(또는 마이크) → 입력 전송 → VAD(Silero) → STT(Gemini 3.5 Transcribe Live, VERBATIM, ko-KR)
  → EngineBridge ⇄ 엔진 Session(그대로)
  → TTS(Gemini 3.8 Flash-Lite TTS, Kore, ko-KR) → 출력 전송(재생)
```

| 모듈 | 하는 일 |
|---|---|
| `jikgam/voice/bridge.py` | 엔진 연결 FrameProcessor. 자막 → 엔진 입력 이벤트, 엔진 응답 → TTS, 끼어들기, 음성 노출 기록 |
| `jikgam/voice/stt.py` | Pipecat `GeminiSTTService`에 인식 모드 VERBATIM을 명시한 하위 클래스 |
| `jikgam/voice/pipeline.py` | 파이프라인 조립(VAD, STT, 연결, TTS, 전송) |
| `jikgam/voice/transport.py` | 로컬 시험 전송: 녹음을 실시간으로 넣고, 엔진 음성을 실시간으로 "재생"한다. 두 방향을 스테레오 WAV로 남긴다 |
| `jikgam/voice/customer_audio.py` | 개발용 재생의 고객 발화를 Gemini TTS로 녹음 파일로 만든다(`data/voice/`) |
| `jikgam/voice/replay.py` | 음성 재생 실행기(정책 C). 줄마다 녹음을 넣고 결과 로그와 측정값을 남긴다 |
| `jikgam/voice/metrics.py` | 음성 측정(로그만으로 계산) |
| `config/voice.toml` | STT·TTS 모델 ID(버전 고정), 목소리, VAD, 응답 재생 규칙 값, 단가 |

엔진은 런타임을 모른다. 연결 계층은 `Session.handle(event)`, `Session.version`, `Session.log.emit()`만 쓴다. 엔진 코드는 바꾸지 않았다.

## 2. 설치와 Windows

- `pipecat-ai[google]==1.12.0`(2026-09-26, 2026-10-14 기준 최신)을 `voice` 의존성 그룹에 고정했고 기본 그룹으로 설치한다(`uv sync`). 함께 설치된 `google-genai`는 2.30.1이다.
- 이 PC(Windows 11 Pro 22631, Python 3.12.11)에서 확인한 것: Pipecat 가져오기, Silero VAD(onnxruntime, 모델 파일은 패키지에 들어 있다), Gemini Live STT(WebSocket), Gemini TTS 스트리밍, `WorkerRunner`·`PipelineWorker` 실행과 종료, 끼어들기(`InterruptionFrame`)로 TTS·출력 버퍼 비우기. 런타임을 바꾸지 않았다.
- Windows에서 고려한 것:
  - 신호 처리: `WorkerRunner(handle_sigint=False)`로 쓴다. Pipecat은 Windows에서 `loop.add_signal_handler` 대신 `signal.signal`로 물러나지만, 실행기는 자기 종료 순서를 직접 다룬다.
  - 시계: Python 3.12의 Windows asyncio 시계는 해상도가 약 15.6ms다. 시험 전송은 조각(20ms)마다 엔진 시계로 기한을 누적해 기다려 평균 속도를 실시간으로 맞춘다(조각마다 최대 약 16ms 늦을 수 있다).
  - 세션을 닫을 때 asyncio Proactor가 `OSError: [WinError 10022]`(`_call_connection_lost`의 소켓 shutdown)를 콜백 예외로 찍는 경우가 있다(측정 실행 2회에서 각각 한 번). 소켓을 닫는 중에 나는 것이라 결과 로그와 측정값에는 영향이 없었다.
  - 로컬 장치 전송(`pipecat-ai[local]`, PyAudio)과 Pipecat Evals(`[evals]`, Kokoro·Moonshine 모델을 함께 받는다)는 설치하지 않았다. 녹음을 실제 STT에 흘리는 데는 자체 시험 전송(§1)이 더 작고 장치에 매이지 않는다.
- ADR 0008을 쓸 때 본 문서와 다른 점: Pipecat 1.12.0의 `GeminiSTTService`는 인식 모드 설정을 노출하지 않는다. `google-genai`의 `AudioTranscriptionConfig.mode`로는 정할 수 있어, 연결 설정을 만드는 메서드만 덮어써 VERBATIM을 명시했다(`jikgam/voice/stt.py`).
- Gemini TTS 응답 형식(2026-10-14 호출): 한 번에 받으면 `audio/wav`(머리 포함), 스트리밍이면 `audio/l16; rate=24000; channels=1`이다. Pipecat의 Gemini API 경로는 스트리밍을 쓰므로 그대로 24kHz PCM으로 받는다.

## 3. 이벤트와 시계

모든 시각은 엔진 단조 시계(`jikgam.engine.events.now_ms`, ms)다. 연결 계층의 기록은 엔진과 같은 세션 로그(JSONL)에 남는다.

| 음성 쪽 사건 | 엔진 입력 / 로그 |
|---|---|
| 미확정 자막(`InterimTranscriptionFrame`) | `{"type": "utterance", "final": false}` → 엔진이 `stt.partial`을 남긴다. 버전은 오르지 않는다(engine.md §3.1) |
| 확정 자막(`TranscriptionFrame`) | 연결 계층이 `stt.final`(받은 시각, `t_end_ms`, `t_end_source`)을 남기고 `{"final": true, "t_end_ms": …}`를 별도 작업으로 넘긴다 → 엔진이 `utterance.end`(t_ms = t_end_ms)와 `version.bumped`를 남긴다 |
| VAD 시작·끝 | `vad.start`(발화 시작 추정), `vad.stop`(발화 종료 추정) |
| TTS 요청 | `tts.request` |
| 재생 시작(`BotStartedSpeakingFrame`) | `suggestion.exposed`(channel `voice`, start_ms) |
| 재생 끝 | `tts.playback_end`(end_ms, interrupted: barge_in·version·timeout·error) |
| 끼어들기 | `voice.barge_in`(trigger: vad·transcript, 재생한 시간) |
| 응답 보류·폐기 | `voice.reply_held`, `result.discarded`(stage `voice`) |
| 시험 입력 구간 | `voice.segment`(실행기. 녹음 위치로 계산한 기준 발화 시작·종료) |

- **발화 ID**: 연결 계층이 u1, u2, …를 매긴다. 확정 자막이 오기 전의 미확정 자막은 다음 확정 자막과 같은 ID다.
- **발화 종료(t_end_ms)**: VAD가 끝을 판정한 시각 − `stop_secs`(0.5초). VAD 프레임의 시각은 `time.time()`(초)이라, 받은 순간에 읽은 두 시계의 차로 엔진 시계에 옮긴다. 입력은 실시간으로 들어오고 VAD는 받은 즉시 판정하므로 판정 시각은 그 판정을 낸 오디오 조각이 들어온 시각과 같다고 본다. VAD 끝 판정 하나는 확정 발화 하나에만 쓴다. 쓸 판정이 없으면(STT가 VAD 끝점보다 먼저 확정) 확정 자막을 받은 시각을 쓰고 `t_end_source="final_received"`로 표시한다. 이 경우 발화 종료가 늦게 잡혀 지연이 짧게 계산된다.
- **발화 시작 추정**: VAD 시작 판정 시각 − `start_secs`(0.2초). 측정에는 쓰지 않는다(§5의 기준값을 쓴다).
- **노출 줄은 음성 계층만 남긴다**(2026-10-14 보완): 엔진 세션을 `Session(..., channel="voice")`로 만든다(`jikgam/voice/replay.py`). 채널이 `voice`면 엔진은 `agent.say`(응답을 채널에 넘긴 시각)만 남기고 `suggestion.exposed`를 남기지 않는다. 음성 노출은 재생 시작에 연결 계층이 남기는 `voice` 줄뿐이다. 엔진의 판단·도구·생성·제안 처리는 채널과 관계없이 같다. `EngineBridge`는 채널이 `voice`가 아닌 세션을 받지 않는다. 이전 실행(`results/voice/20261014-voice-r1`, `-r2`)의 로그에는 엔진이 남긴 `chat` 노출 줄이 함께 있다(그때는 엔진이 채널을 몰랐다). 지표 코드가 채널별로 세므로 그 결과의 `voice` 수치는 바뀌지 않는다.
- **조건 변경 표시**: 시험 실행기만 붙인다. 그 줄의 녹음 구간에서 처음 확정된 발화에 붙는다. 엔진은 읽지 않는다.

## 4. 응답 재생 규칙

- 응답은 한 번에 하나씩 재생한다.
- **대화 버전**: TTS를 요청하기 직전에 세션 버전이 응답의 기준 버전과 다르면 말하지 않고 `result.discarded`(stage `voice`, reason `version`)를 남긴다. 재생이 시작된 순간 버전이 바뀌어 있으면 노출은 기록하고 곧바로 끊는다(interrupted `version`).
- **고객 차례**: VAD가 말하는 중이거나, 말이 끝났는데 그 말의 확정 자막이 아직 없으면(최대 `final_wait_ms` 3초) 새 응답을 시작하지 않는다. 확정 자막이 오면 엔진이 버전을 올리므로 붙잡은 응답은 위 규칙으로 버려진다. 고객이 정정을 말한 뒤 확정 자막이 오기 전(약 0.8초)에 지난 응답이 재생되는 것을 막는다. 확정 자막이 끝내 없으면(잡음) 3초 뒤 재생한다.
- **끼어들기**: 응답을 요청했거나 재생 중일 때 VAD가 발화 시작을 판정하거나 미확정 자막이 오면(먼저 오는 쪽) `broadcast_interruption()`으로 TTS 합성과 출력 버퍼를 비운다. 짧은 말("네")은 VAD 시작 판정(0.2초 연속 음성)보다 자막이 먼저 온다. 대화 버전은 엔진이 확정 발화를 받을 때 올린다.
- 미확정 자막 선판단(정책 D)은 처음에는 구현하지 않았다. 2026-10-15에 더했다(§7).

## 5. 측정

```sh
uv run python -m jikgam.voice.customer_audio --replays s01 s20 s21      # 고객 녹음(있으면 다시 만들지 않는다)
uv run python -m jikgam.voice.replay --replays s01 s20 s21 --bizapi-url http://127.0.0.1:8096
```

- 업무 API는 실행할 때마다 따로 띄운 로컬 서버를 쓴다(`--bizapi-url`, 실행마다 초기화). 정책은 C(엔진 + 조직 기본 판별 백엔드 Jev)다.
- 고객 녹음: `data/voice/<재생>/uNN.wav`(16kHz 모노). 문장을 Gemini 3.8 Flash-Lite TTS(Puck)로 읽혀 앞뒤 무음을 잘라 내고 앞 200ms·뒤 300ms 무음을 붙였다. `data/voice/manifest.json`에 파일별 음성 위치(10ms 프레임 RMS > −45 dBFS 기준), 가장 긴 쉼, sha256이 있다. 같은 문장도 다시 만들면 다른 음성이 나오므로 있는 파일은 다시 만들지 않는다.
- 줄을 넣는 때: 보통은 앞 줄에 대한 응답 재생이 끝나고 700ms 뒤. 조건 변경 줄(s20 u4, s21 u4)은 앞 줄에 대한 응답이 재생되기 시작하고 1,000ms 뒤, 즉 응답이 끝나기 전에 고객이 정정을 시작한다(끼어들기).
- **기준 시각**: 시험 입력은 녹음을 조각 경계에서 넣기 시작하므로, 파일 안 위치 x ms의 소리는 첫 조각을 넣은 시각 + x에 파이프라인에 들어간 것이다. 실행기는 이 값으로 기준 발화 시작·종료를 `voice.segment`에 남긴다.
- 측정값(`jikgam/voice/metrics.py`, 구간마다):

| 값 | 정의 |
|---|---|
| 첫 자막 | 첫 미확정 자막을 받은 시각 − 기준 발화 시작 |
| 확정 | 확정 자막을 받은 시각 − 기준 발화 종료(한 줄이 둘로 확정되면 마지막 것) |
| t_end 오차 | 엔진에 넘긴 t_end_ms − 기준 발화 종료 |
| 첫 음성 | 그 발화 뒤 첫 음성 노출(재생 시작) − 기준 발화 종료. 노출한 제안의 기준 버전이 그 발화가 만든 버전 이상 |
| 엔진 / TTS | 확정 자막 → agent.say / tts.request → 재생 시작 |
| 근거 갱신 | evaluation.md §3.2(`jikgam.sim.metrics.refresh_latencies`, t0 = utterance.end). 기준 발화 종료로 잰 값도 낸다 |
| 끼어들기 | 기준 발화 시작부터 감지(voice.barge_in)와 재생 멈춤(tts.playback_end)까지 |
| 무효 노출 | evaluation.md §3.3의 `voice` 채널 |

- 비용: 엔진(판별·생성)은 로그의 호출 비용이다. STT는 연결 시간 × 단가, TTS는 출력 오디오 길이 × 초당 25토큰(2026-10-14 측정) × 단가로 **추정**한다. 청구 금액이 아니다.
- 결과: `results/voice/<run_id>/`에 세션 로그(JSONL), 세션별 측정값(`*.metrics.json`), `summary.json`. 녹음(스테레오 WAV: 왼쪽 고객, 오른쪽 엔진)과 `pipecat.log`는 저장소에 넣지 않는다.

## 6. 알려진 한계

- 고객 음성은 잡음 없는 합성 음성이다. 잡음, 8kHz 전화 음질, 실제 사람의 발화는 확인하지 않았다. 시험 전송에는 반향이 없어 엔진 음성이 STT로 다시 들어가지 않는다(실제 통화에는 반향 제거가 필요하다).
- 생성 문구는 채팅용 프롬프트(engine.md §6)로 만든다. 음성용 문체(짧은 문장, 숫자 읽기)는 다루지 않았다.
- VAD 끝점 0.5초는 확정 자막을 그만큼 늦춘다. 그래도 문장 사이 쉼에서 한 줄이 둘로 확정되는 일이 있다(2026-10-14 측정: s01 u1 2회 중 2회, s21 u1 2회 중 1회). 엔진은 둘을 따로 받는다. s01에서는 엔진이 앞 문장에 답한 응답이 고객이 뒷 문장을 말하는 동안 나와, §4의 고객 차례 규칙으로 붙잡혔다가 버려졌다.
- STT 결과와 엔진 진술 추출이 맞지 않는 경우(엔진은 고치지 않았다, 2026-10-14 측정):
  - TTS가 "010-0000-0001"을 "공일공에 …"로 읽고 STT가 `010에 0000에 0001`로 적었다(s01 u2, 2회 중 2회). 엔진의 전화번호 규칙(`claims._PHONE`)은 구분자로 `-`, 공백, `.`만 받아 번호를 찾지 못했고 본인 확인을 하지 못했다. 사람도 번호를 "에"로 끊어 읽으므로 진술 추출에서 다룰 일이다.
  - 숫자 오인식: s21 u2 "010-0000-0010"이 `0100000에 0110`, `010-0000-0110`으로, u3 인증번호 "공공일공이요"가 `001020`으로 적혔다(2회). 본인 확인이 실패했다.
  - 상호 "별담"이 `별다`, `별단`으로 적혔다. STT 적응 문구(adaptation phrases)를 쓰지 않았다.
  - s20은 2회 모두 번호가 `010-0000-0016`, 인증번호가 `0016`으로 적혀 본인 확인, 조회, 해지 접수까지 진행됐다.

## 7. 정책별 음성 계층(2026-10-15)

`docs/evaluation-voice.md`의 음성 평가를 위해 정책 A와 D를 음성으로 돌릴 수 있게 했다. 엔진 정책은 `jikgam/voice/replay.py`의 `make_voice_session`과 `voice_options`가 정한다.

| 정책 | 세션 | 음성 계층 |
|---|---|---|
| A | 개선형 RAG(`RagSession(channel="voice")`) | `stale_guard=False`: 버전이 지난 답 버리기·끊기와 확정 자막 기다리기를 끈다. 한 번에 하나씩 재생, 고객이 말하는 중엔 시작하지 않기, 끼어들기는 그대로 |
| C | 엔진 | §4 그대로 |
| D | 엔진 | C에 `provisional=True`를 더한다(아래) |

정책 D(`EngineBridge(provisional=True)`, `Session.prejudge`):

- 실시간(미확정) 자막이 바뀔 때마다 `Session.prejudge`가 그 자막을 마지막 고객 발화로 본 상태의 사본으로 첫 판별을 미리 돌린다(`decision.prejudge`). 세션 상태와 버전은 바뀌지 않는다. 확정 발화의 첫 판별이 같은 입력이면 그 결과를 쓴다(`decision.response`의 `prejudged`).
- VAD가 말 끝을 판정하면 마지막 실시간 자막으로 잠정 확정해 엔진에 넘긴다(`voice.provisional`). 그 자막이 250ms보다 먼저 온 것이면 다음 실시간 자막을 기다린다. 자막에 숫자가 있으면 잠정 확정하지 않고 확정 자막을 기다린다.
- 확정 자막이 오면 잠정 문장과 비교한다(`stt.final`의 `confirms_provisional`). 같으면 그대로 두고, 다르면 새 발화 ID로 넘겨 다시 판단한다.
- 잠정 확정 뒤 고객이 다시 말하기 전에 늦게 온 실시간 자막은 끝난 말의 것으로 보고 기록만 한다(끼어들기로 보지 않는다).

음성 시나리오 실행기(`jikgam/voice/sim.py`)는 AI 고객의 문장을 고객 목소리 TTS로 읽혀 넣고, 녹음과 그 시작 시각(`voice.recording`)을 남긴다. 실행과 보고는 `scripts/run_voice_eval.sh`, `jikgam/voice/report.py`다.
