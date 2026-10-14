"""음성 런타임 설정(config/voice.toml)을 읽는다."""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jikgam.engine.org import ROOT

CONFIG = ROOT / "config/voice.toml"


@dataclass(frozen=True)
class SttConfig:
    model: str
    mode: str
    language: str
    api_key_env: str
    price_usd_per_min: float


@dataclass(frozen=True)
class TtsConfig:
    model: str
    voice: str
    language: str
    api_key_env: str
    price_input_per_mtok: float = 0.0
    price_audio_per_mtok: float = 0.0


@dataclass(frozen=True)
class VadConfig:
    confidence: float
    start_secs: float
    stop_secs: float
    min_volume: float


@dataclass(frozen=True)
class ChannelConfig:
    final_wait_ms: float
    play_start_timeout_ms: float


@dataclass(frozen=True)
class VoiceConfig:
    stt: SttConfig
    tts: TtsConfig
    customer_tts: TtsConfig
    vad: VadConfig
    channel: ChannelConfig

    def log(self) -> dict[str, Any]:
        """세션 로그(voice.session)에 남길 값. 키는 남기지 않는다."""
        return {"stt": {"model": self.stt.model, "mode": self.stt.mode, "language": self.stt.language},
                "tts": {"model": self.tts.model, "voice": self.tts.voice, "language": self.tts.language},
                "vad": vars(self.vad), "channel": vars(self.channel)}


def _pick(cls, d: dict[str, Any]):
    return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def load_voice_config(path: Path = CONFIG) -> VoiceConfig:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    cfg = VoiceConfig(_pick(SttConfig, raw["stt"]), _pick(TtsConfig, raw["tts"]), _pick(TtsConfig, raw["customer_tts"]),
                      _pick(VadConfig, raw["vad"]), _pick(ChannelConfig, raw["channel"]))
    if cfg.stt.mode != "VERBATIM":  # docs/research/korean-stt-tts.md: SMART는 정정 발화를 지운다
        raise ValueError(f"STT 인식 모드는 VERBATIM이어야 한다: {cfg.stt.mode}")
    return cfg
