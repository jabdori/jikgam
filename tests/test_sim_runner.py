import asyncio
from dataclasses import replace

from engine_fakes import EchoGenerator, ScriptedDecider

from jikgam.engine import events as ev
from jikgam.engine.bizapi import BizApiClient
from jikgam.engine.org import load_org
from jikgam.engine.session import Session
from jikgam.sim import metrics as mt
from jikgam.sim import runner
from jikgam.sim.customer import CustomerTurn

ORG = load_org("org_a")

SCN = {
    "id": "t01", "opening": "안녕하세요, 지난주 결제한 거 안 쓸 거라서 취소하고 환불받고 싶어요.",
    "reveal_rules": [],
    "mid_call_events": [
        {"after_turn": 1, "event": "전화번호를 말한다", "condition_change": True, "invalidates_prior": False},
        {"after_turn": 0, "event": "[주입] quote_refund TIMEOUT", "condition_change": False, "invalidates_prior": False,
         "inject": {"tool": "quote_refund", "code": "TIMEOUT", "count": 1000}},
    ],
}


class ScriptedCustomer:
    model = "scripted"
    prompt_version = "test"

    def __init__(self, turns):
        self.turns = list(turns)
        self.seen = []

    async def next_turn(self, conversation, turn_no, executed):
        self.seen.append((turn_no, sorted(executed), [t["speaker"] for t in conversation]))
        say, idx, done = self.turns.pop(0)
        return CustomerTurn(say, idx, done, {"cost_usd": 0.001})

    async def aclose(self):
        pass


def fake_policy(url: str) -> runner.Policy:
    def make(org, sid, log_dir):
        return Session(org, sid, log_dir, decision_client=ScriptedDecider(intents=[("환불", "cancel_refund")]),
                       generator=EchoGenerator(), bizapi=BizApiClient(url, sid))
    return runner.Policy("T", "테스트", make)


def test_run_scenario_marks_events_injects_faults_and_writes_logs(bizapi, tmp_path):
    org = replace(ORG, bizapi_url=bizapi)
    customer = ScriptedCustomer([("010-0000-0001이에요.", 0, False), ("인증번호는 공공공일이요.", None, False),
                                 ("네 알겠습니다, 나중에 다시 연락할게요.", None, True)])
    sess = asyncio.run(runner.run_scenario(SCN, fake_policy(bizapi), org, tmp_path, "t01-r1", customer))
    assert sess["end_reason"] == "customer_done" and sess["customer_turns"] == 4
    assert sess["executed_events"] == [0] and sess["missing_events"] == []
    assert sess["injections"] == [{"event": "t01#1", "tool": "quote_refund", "code": "TIMEOUT", "count": 1000}]
    assert abs(sess["costs"]["customer_usd"] - 0.003) < 1e-12
    assert customer.seen[0] == (2, [], ["customer", "agent"])  # 고객은 엔진 응답을 받은 뒤에 쓴다
    log = ev.read_log(tmp_path / "t01-r1.jsonl")
    utts = [e for e in log if e["type"] == "utterance.end"]
    assert [u["utt_id"] for u in utts] == ["u1", "u2", "u3", "u4"]
    assert utts[1]["marker"] == {"condition_change": True, "invalidates_prior": False, "event": "t01#0"}
    assert all(u["marker"] is None for u in utts if u["utt_id"] != "u2")
    quotes = [e for e in log if e["type"] == "tool.result" and e["tool"] == "quote_refund"]
    assert quotes and all(e["error_code"] == "TIMEOUT" and e["attempts"] == 2 for e in quotes)
    sim = ev.read_log(tmp_path / "t01-r1.sim.jsonl")
    assert [e["type"] for e in sim][:2] == ["sim.start", "sim.inject"]
    assert sim[-1]["type"] == "sim.end" and sim[-1]["reason"] == "customer_done"
    turns = [e for e in sim if e["type"] == "customer.turn"]
    assert turns[0]["llm"] == {"source": "opening"} and turns[1]["executed_event"] == 0
    rows = mt.refresh_latencies(log)
    assert [r["event"] for r in rows] == ["t01#0"] and rows[0]["latency_ms"] is not None


def test_run_scenario_stops_at_max_turns(bizapi, tmp_path):
    org = replace(ORG, bizapi_url=bizapi)
    customer = ScriptedCustomer([("음...", None, False)] * 5)
    scn = {**SCN, "mid_call_events": []}
    sess = asyncio.run(runner.run_scenario(scn, fake_policy(bizapi), org, tmp_path, "t02-r1", customer, max_turns=3))
    assert sess["end_reason"] == "max_turns" and sess["customer_turns"] == 3


def test_handed_off_and_select():
    assert runner.handed_off([{"type": "suggestion.created", "action": "handoff"}])
    assert runner.handed_off([{"type": "tool.result", "tool": "create_handoff", "ok": True}])
    assert not runner.handed_off([{"type": "tool.result", "tool": "create_handoff", "ok": False}])
    rows = [{"id": "s01"}, {"id": "s02"}]
    assert runner.select(rows, "all") == rows and runner.select(rows, "s02") == [{"id": "s02"}]
