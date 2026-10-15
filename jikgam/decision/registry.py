"""설정 파일의 이름으로 판별 클라이언트를 만든다. 백엔드 교체는 설정 변경만으로 한다.

설정은 config/decision_models.toml과 config/decision_models.d/*.toml(관리 콘솔에서 등록한 모델, 파일 하나에 모델 하나)을
함께 읽는다(ADR 0021). 같은 이름이 두 곳에 있으면 오류다. kind는 systemone(Jev 형식 API, 판별 API와 로컬 서버),
openai(OpenAI 호환 생성형), gemini, rules, majority다.
"""

import os
import tomllib
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from .llm import GeminiJudgeClient
from .local import LocalClient, majority_fn
from .openai_compat import OpenAICompatJudgeClient
from .systemone import SystemOneClient
from .types import DecisionClient

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config/decision_models.toml"
REGISTERED = ROOT / "config/decision_models.d"


def load_config(path: Path = CONFIG, registered: Path | None = None) -> dict[str, dict[str, Any]]:
    cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    extra = registered if registered is not None else (REGISTERED if path == CONFIG else None)
    if extra is not None and extra.is_dir():
        for f in sorted(extra.glob("*.toml")):
            for name, entry in tomllib.loads(f.read_text(encoding="utf-8")).items():
                if name in cfg:
                    raise ValueError(f"판별 백엔드 이름이 겹친다: {name}({f.name})")
                cfg[name] = {**entry, "registered_file": f.relative_to(ROOT).as_posix() if f.is_relative_to(ROOT) else str(f)}
    return cfg


def secrets() -> dict[str, str]:
    """환경 변수가 .env보다 우선한다. 값은 로그나 결과 파일에 남기지 않는다."""
    env = {k: v for k, v in dotenv_values(ROOT / ".env").items() if v}
    env.update({k: v for k, v in os.environ.items() if k in env})
    return env


def make_client(name: str, cfg: dict[str, Any] | None = None) -> DecisionClient:
    cfg = cfg or load_config()[name]
    env = secrets()
    kind = cfg["kind"]
    if kind == "systemone":
        return SystemOneClient(name, cfg["url"].format(**env), cfg["model"], env.get(cfg.get("api_key_env", "")),
                               cfg["price_input_per_mtok"], cfg.get("price_output_per_mtok", 0.0),
                               envelope=cfg.get("envelope", "plain"))
    if kind == "gemini":
        return GeminiJudgeClient(name, cfg["model"], env[cfg["api_key_env"]], cfg["price_input_per_mtok"],
                                 cfg["price_output_per_mtok"], thinking_level=cfg.get("thinking_level"))
    if kind == "openai":
        key_env = cfg.get("api_key_env")
        return OpenAICompatJudgeClient(name, cfg["url"].format(**env), cfg["model"], env.get(key_env) if key_env else None,
                                       cfg.get("price_input_per_mtok", 0.0), cfg.get("price_output_per_mtok", 0.0))
    if kind == "rules":
        from .rules import decide
        return LocalClient(name, decide, "rules")
    if kind == "majority":
        return LocalClient(name, majority_fn(ROOT / "data/build/train.jsonl"), "majority-train")
    raise ValueError(f"알 수 없는 kind: {kind}")
