"""음성 파이프라인 조립: 입력 → VAD(Silero) → STT(Gemini, VERBATIM) → EngineBridge → TTS(Gemini) → 출력."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.services.google.tts import GeminiTTSService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport

from jikgam.decision.registry import secrets

from .bridge import EngineBridge, SessionLike
from .config import VoiceConfig
from .stt import make_stt

IN_RATE = 16000  # STT 입력
OUT_RATE = 24000  # Gemini TTS 출력


@dataclass
class VoicePipeline:
    worker: PipelineWorker
    bridge: EngineBridge
    pipeline: Pipeline


def make_tts(cfg: VoiceConfig, api_key: str) -> GeminiTTSService:
    # api_key를 주면 Gemini API(google-genai) 경로를 쓴다. Google Cloud TTS 경로는 이 키로 쓸 수 없다(ADR 0008)
    return GeminiTTSService(api_key=api_key, settings=GeminiTTSService.Settings(
        model=cfg.tts.model, voice=cfg.tts.voice, language=Language(cfg.tts.language)))


def build_voice_pipeline(session: SessionLike, cfg: VoiceConfig, transport: BaseTransport, *,
                         marker_for: Callable[[float | None, float], dict[str, Any] | None] | None = None
                         ) -> VoicePipeline:
    env = secrets()
    vad = VADProcessor(vad_analyzer=SileroVADAnalyzer(params=VADParams(
        confidence=cfg.vad.confidence, start_secs=cfg.vad.start_secs, stop_secs=cfg.vad.stop_secs,
        min_volume=cfg.vad.min_volume)))
    stt = make_stt(cfg.stt, env[cfg.stt.api_key_env])
    bridge = EngineBridge(session, final_wait_ms=cfg.channel.final_wait_ms,
                          play_start_timeout_ms=cfg.channel.play_start_timeout_ms, marker_for=marker_for,
                          name="engine-bridge")
    tts = make_tts(cfg, env[cfg.tts.api_key_env])
    pipeline = Pipeline([transport.input(), vad, stt, bridge, tts, transport.output()])
    worker = PipelineWorker(pipeline, params=PipelineParams(audio_in_sample_rate=IN_RATE, audio_out_sample_rate=OUT_RATE,
                                                            enable_metrics=True),
                            idle_timeout_secs=None, cancel_on_idle_timeout=False, enable_rtvi=False)
    return VoicePipeline(worker, bridge, pipeline)
