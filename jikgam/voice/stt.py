"""Gemini Transcribe Live STT를 VERBATIM 모드로 고정한 Pipecat 서비스.

Pipecat 1.12.0의 GeminiSTTService는 인식 모드(AudioTranscriptionConfig.mode) 설정을 노출하지 않는다(ADR 0008을 쓸 때 본
문서와 다르다). 서버 기본값이 VERBATIM이지만, SMART는 정정 발화("9월 … 아 아니다, 7월")를 지워 엔진이 조건 변경을 볼 수
없게 하므로(docs/research/korean-stt-tts.md) 연결 설정을 만드는 메서드만 덮어써 VERBATIM을 명시한다. 언어는 자동 감지
대신 config/voice.toml의 언어 하나를 힌트로 준다.
"""

from google.genai.types import AudioTranscriptionConfigMode, LiveConnectConfig
from pipecat.services.google.gemini_live.stt import GeminiSTTService
from pipecat.transcriptions.language import Language

from .config import SttConfig


class VerbatimGeminiSTT(GeminiSTTService):
    def _build_live_config(self) -> LiveConnectConfig:
        config = super()._build_live_config()
        config.input_audio_transcription.mode = AudioTranscriptionConfigMode.VERBATIM
        return config


def make_stt(cfg: SttConfig, api_key: str) -> VerbatimGeminiSTT:
    if cfg.mode != "VERBATIM":
        raise ValueError(f"STT 인식 모드는 VERBATIM이어야 한다: {cfg.mode}")
    return VerbatimGeminiSTT(api_key=api_key, audio_passthrough=False,
                             settings=VerbatimGeminiSTT.Settings(model=cfg.model, languages=[Language(cfg.language)]))
