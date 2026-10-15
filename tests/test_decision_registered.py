"""관리 콘솔에서 등록한 모델(ADR 0021, ADR 0007 확장): 설정 폴더 합치기, OpenAI 호환 어댑터, 부하 측정 실행기."""

import asyncio
import json

import httpx
import pytest

from jikgam.decision import registry
from jikgam.decision.openai_compat import OpenAICompatJudgeClient
from jikgam.eval import load as load_mod

QUESTIONS = {"applies:A-01": {"type": "noul", "instructions": "적용되는가?"},
             "intent": {"type": "choice", "instructions": "요청", "criteria": {"refund": "환불", "cancel": "해지"}}}


def test_registered_models_are_merged(tmp_path):
    base = tmp_path / "decision_models.toml"
    base.write_text('[jev]\nkind = "systemone"\nurl = "https://x.invalid"\nmodel = "jev-1"\n', encoding="utf-8")
    d = tmp_path / "decision_models.d"
    d.mkdir()
    (d / "new.toml").write_text('["new"]\nkind = "openai"\nurl = "https://y.invalid/v1"\nmodel = "gen-1"\n', encoding="utf-8")
    cfg = registry.load_config(base, d)
    assert set(cfg) == {"jev", "new"} and cfg["new"]["registered_file"].endswith("new.toml")
    (d / "dup.toml").write_text('[jev]\nkind = "rules"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        registry.load_config(base, d)


def test_openai_compatible_client_parses_answers_and_cost():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["format"] = body["response_format"]["type"]
        seen["keys"] = sorted(body["response_format"]["json_schema"]["schema"]["properties"])
        content = json.dumps({"applies__A-01": {"p_true": 0.8}, "intent": {"probabilities": {"refund": 3, "cancel": 1}}})
        return httpx.Response(200, json={"id": "r1", "model": "gen-1-2026", "usage": {"prompt_tokens": 1000, "completion_tokens": 50},
                                         "choices": [{"message": {"content": content}}]})

    async def go():
        c = OpenAICompatJudgeClient("gen", "https://api.example.invalid/v1/", "gen-1", "KEY", 2.0, 8.0,
                                    transport=httpx.MockTransport(handler))
        try:
            return await c.decide({"conversation": []}, QUESTIONS)
        finally:
            await c.aclose()

    res = asyncio.run(go())
    assert seen["url"] == "https://api.example.invalid/v1/chat/completions" and seen["auth"] == "Bearer KEY"
    assert seen["format"] == "json_schema" and seen["keys"] == ["applies__A-01", "intent"]  # 원격 키 변환(ADR 0007)
    assert res.error is None and res.model == "gen-1-2026"
    assert res.answers["applies:A-01"] == {"type": "noul", "noul": 0.8}
    assert res.answers["intent"]["choice"] == "refund" and res.answers["intent"]["probabilities"]["refund"] == 0.75
    assert res.cost_usd == pytest.approx((1000 * 2.0 + 50 * 8.0) / 1e6)


def test_openai_compatible_client_reports_http_errors():
    async def go():
        c = OpenAICompatJudgeClient("gen", "https://api.example.invalid/v1", "gen-1", None, 0, 0,
                                    transport=httpx.MockTransport(lambda r: httpx.Response(429, text="slow down")))
        try:
            return await c.decide({}, QUESTIONS)
        finally:
            await c.aclose()

    res = asyncio.run(go())
    assert res.error.startswith("HTTP 429") and res.answers == {}


def test_load_runner_writes_raw_and_meta(tmp_path):
    out = asyncio.run(load_mod.run("rules", 2, 0.3, tmp_path, registry.ROOT / "data/questions.json"))
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (out / "raw.jsonl").read_text(encoding="utf-8").splitlines()]
    assert meta["backend"] == "rules" and meta["concurrency"] == 2 and meta["split"] == "val"
    assert rows and {r["worker"] for r in rows} == {0, 1} and all(r["error"] is None for r in rows)
