import asyncio
from dataclasses import replace

from engine_fakes import EchoGenerator, ScriptedDecider

from jikgam.baselines.rag import FINAL, LLMStep, RagSession
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
        {"after_turn": 1, "event": "전화번호를 말한다", "condition_change": False, "invalidates_prior": False},
        {"after_turn": 0, "event": "[주입] quote_refund TIMEOUT", "condition_change": False, "invalidates_prior": False,
         "inject": {"tool": "quote_refund", "code": "TIMEOUT", "count": 1000}},
    ],
}
# 조건 변경 사건 하나(빠른 정정으로 보낸다)
FAST = {"id": "t02", "opening": "다음 결제가 언제인지 알고 싶어요.", "reveal_rules": [],
        "mid_call_events": [{"after_turn": 1, "event": "번복: 그냥 해지할게요", "condition_change": True,
                             "invalidates_prior": True}]}


class ScriptedCustomer:
    model = "scripted"
    prompt_version = "test"

    def __init__(self, turns):
        self.turns = list(turns)
        self.seen = []

    async def next_turn(self, conversation, turn_no, executed):
        self.seen.append((turn_no, sorted(executed), [t["speaker"] for t in conversation]))
        t = self.turns.pop(0)
        if isinstance(t, CustomerTurn):
            return t
        say, idx, done = t
        return CustomerTurn(say, idx, done, {"cost_usd": 0.001})


class SlowGenerator(EchoGenerator):
    """취소될 수 있게 asyncio.sleep으로 기다린 뒤 답한다(진행 중인 생성 흉내)."""

    def __init__(self, delay_s: float):
        super().__init__()
        self.delay_s = delay_s

    async def generate(self, payload, feedback=None):
        await asyncio.sleep(self.delay_s)
        return await super().generate(payload, feedback)


class SlowLLM:
    """정책 A용 가짜 LLM. 매 발화에 delay_s 뒤 final_answer 하나로 답한다."""

    name = model = "slow-llm"

    def __init__(self, delay_s: float):
        self.delay_s = delay_s
        self.n = 0

    async def step(self, system, contents, tools):
        await asyncio.sleep(self.delay_s)
        self.n += 1
        call = {"name": FINAL, "args": {"say": f"답 {self.n}", "cited_articles": ["A-04"], "amount": None,
                                        "amount_final": False, "action": "answer"}}
        return LLMStep({"role": "model", "parts": [{"functionCall": call}]}, [call], self.model, latency_ms=1.0)

    async def aclose(self):
        pass


def engine_policy(url: str, generator=None) -> runner.Policy:
    def make(org, sid, log_dir):
        return Session(org, sid, log_dir, decision_client=ScriptedDecider(intents=[("환불", "cancel_refund")]),
                       generator=generator or EchoGenerator(), bizapi=BizApiClient(url, sid))
    return runner.Policy("T", "테스트 엔진", make)


def rag_policy(url: str, delay_s: float) -> runner.Policy:
    def make(org, sid, log_dir):
        return RagSession(org, sid, log_dir, llm=SlowLLM(delay_s), bizapi=BizApiClient(url, sid))
    return runner.Policy("TA", "테스트 RAG", make)


def test_run_scenario_marks_events_injects_faults_and_writes_logs(bizapi, tmp_path):
    org = replace(ORG, bizapi_url=bizapi)
    customer = ScriptedCustomer([("010-0000-0001이에요.", 0, False), ("인증번호는 공공공일이요.", None, False),
                                 ("네 알겠습니다, 나중에 다시 연락할게요.", None, True)])
    sess = asyncio.run(runner.run_scenario(SCN, engine_policy(bizapi), org, tmp_path, "t01-r2", customer, run=2,
                                           seed=123))
    assert sess["end_reason"] == "customer_done" and sess["customer_turns"] == 4
    assert sess["executed_events"] == [0] and sess["missing_events"] == [] and sess["fast_corrections"] == []
    assert (sess["run"], sess["customer_seed"]) == (2, 123)
    assert sess["injections"] == [{"event": "t01#1", "tool": "quote_refund", "code": "TIMEOUT", "count": 1000}]
    assert abs(sess["costs"]["customer_usd"] - 0.003) < 1e-12
    assert sess["costs"]["warmup_calls"] == 1  # 세션을 연 뒤 start()로 예열했다
    assert customer.seen[0] == (2, [], ["customer", "agent"])  # 고객은 엔진 응답을 받은 뒤에 쓴다
    log = ev.read_log(tmp_path / "t01-r2.jsonl")
    utts = [e for e in log if e["type"] == "utterance.end"]
    assert [u["utt_id"] for u in utts] == ["u1", "u2", "u3", "u4"]
    assert utts[1]["marker"] == {"condition_change": False, "invalidates_prior": False, "event": "t01#0"}
    assert all(u["marker"] is None for u in utts if u["utt_id"] != "u2")
    quotes = [e for e in log if e["type"] == "tool.result" and e["tool"] == "quote_refund"]
    assert quotes and all(e["error_code"] == "TIMEOUT" and e["attempts"] == 2 for e in quotes)
    sim = ev.read_log(tmp_path / "t01-r2.sim.jsonl")
    assert [e["type"] for e in sim][:2] == ["sim.start", "sim.inject"]
    assert sim[0]["run"] == 2 and sim[0]["customer_seed"] == 123
    assert sim[-1]["type"] == "sim.end" and sim[-1]["reason"] == "customer_done"
    turns = [e for e in sim if e["type"] == "customer.turn"]
    assert turns[0]["llm"] == {"source": "opening"} and turns[1]["executed_event"] == 0


def test_run_scenario_stops_at_max_turns(bizapi, tmp_path):
    org = replace(ORG, bizapi_url=bizapi)
    customer = ScriptedCustomer([("음...", None, False)] * 5)
    scn = {**SCN, "mid_call_events": []}
    sess = asyncio.run(runner.run_scenario(scn, engine_policy(bizapi), org, tmp_path, "t02-r1", customer, max_turns=3))
    assert sess["end_reason"] == "max_turns" and sess["customer_turns"] == 3


def _fast_customer():
    return ScriptedCustomer([CustomerTurn("네 알겠어요", 0, False, {"cost_usd": 0.0}, followup_say="아 그냥 해지할게요"),
                             ("네 감사합니다", None, True)])


def test_fast_correction_engine_cancels_in_flight_pipeline(bizapi, tmp_path):
    """빠른 정정: 두 번째 메시지는 첫 응답을 기다리지 않고 간격 뒤에 간다. 엔진은 첫 파이프라인을 취소하고 지난 버전
    제안을 내지 않으므로 무효 노출이 없다."""
    org = replace(ORG, bizapi_url=bizapi)
    customer = _fast_customer()
    sess = asyncio.run(runner.run_scenario(FAST, engine_policy(bizapi, SlowGenerator(0.4)), org, tmp_path, "t02-c",
                                           customer, fast_delay_ms=80))
    assert sess["end_reason"] == "customer_done" and sess["customer_turns"] == 4 and sess["executed_events"] == [0]
    (fc,) = sess["fast_corrections"]
    assert fc["event"] == "t02#0" and fc["gap_ms"] >= 80 and fc["first_done_before_followup"] is False
    log = ev.read_log(tmp_path / "t02-c.jsonl")
    u2, u3 = [e for e in log if e["type"] == "utterance.end" and e["utt_id"] in ("u2", "u3")]
    assert u2["marker"] is None and u3["marker"]["event"] == "t02#0"
    assert u3["t_ms"] - u2["t_ms"] >= 80
    v2 = next(e["to"] for e in log if e["type"] == "version.bumped" and e.get("utt_id") == "u2")
    assert any(e["type"] == "task.cancelled" and e["started_version"] == v2 for e in log)
    assert not any(e["type"] == "suggestion.created" and e["basis_version"] == v2 for e in log)
    st = mt.stale_exposure(log)["by_channel"]["chat"]
    assert st["invalid"] == 0 and st["denominator"] >= 1
    (r,) = mt.refresh_latencies(log)
    assert r["latency_ms"] is not None
    # 고객 AI가 다음 턴에 본 대화: 연달아 보낸 두 메시지 뒤에 엔진 답
    assert customer.seen[1][2][-3:] == ["customer", "customer", "agent"]


def test_fast_correction_rag_exposes_stale_answer(bizapi, tmp_path):
    """정책 A는 진행 중인 생성을 취소하지 않으므로 정정 전 발화에 대한 답이 정정 뒤에 노출된다(무효 노출)."""
    org = replace(ORG, bizapi_url=bizapi)
    sess = asyncio.run(runner.run_scenario(FAST, rag_policy(bizapi, 0.3), org, tmp_path, "t02-a", _fast_customer(),
                                           fast_delay_ms=80))
    assert sess["end_reason"] == "customer_done" and sess["fast_corrections"][0]["first_done_before_followup"] is False
    log = ev.read_log(tmp_path / "t02-a.jsonl")
    st = mt.stale_exposure(log)
    chat = st["by_channel"]["chat"]
    assert chat["invalid"] == 1 and chat["denominator"] >= 2
    assert st["events"] == [{"event": "t02#0", "invalid_exposures": 1}]
    (r,) = mt.refresh_latencies(log)
    assert r["latency_ms"] is not None and r["latency_ms"] >= 300  # 앞 답을 기다린 뒤에 새 답을 만든다


def test_fast_missing_is_recorded(bizapi, tmp_path):
    org = replace(ORG, bizapi_url=bizapi)
    customer = ScriptedCustomer([CustomerTurn("아 그냥 해지할게요", 0, True, {"cost_usd": 0.0}, fast_missing=0)])
    sess = asyncio.run(runner.run_scenario(FAST, engine_policy(bizapi), org, tmp_path, "t02-m", customer))
    assert sess["fast_missing"] == [0] and sess["fast_corrections"] == [] and sess["executed_events"] == [0]
    sim_end = ev.read_log(tmp_path / "t02-m.sim.jsonl")[-1]
    assert sim_end["fast_missing"] == [0]


def test_handed_off_select_and_seed():
    assert runner.handed_off([{"type": "suggestion.created", "action": "handoff"}])
    assert runner.handed_off([{"type": "tool.result", "tool": "create_handoff", "ok": True}])
    assert not runner.handed_off([{"type": "tool.result", "tool": "create_handoff", "ok": False}])
    rows = [{"id": "s01"}, {"id": "s02"}]
    assert runner.select(rows, "all") == rows and runner.select(rows, "s02") == [{"id": "s02"}]
    assert runner.customer_seed("s20", 1) == runner.customer_seed("s20", 1) != runner.customer_seed("s20", 2)
    assert 0 <= runner.customer_seed("s20", 3) < 2 ** 31
    assert set(runner.POLICIES) == {"A", "B", "C"}
    assert runner.customers_file(ORG).name == "customers.json" and runner.customers_file(ORG).exists()
