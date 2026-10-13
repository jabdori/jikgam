import asyncio
import json

from jikgam.sim import customer as cust
from jikgam.sim.gemini import JsonReply
from jikgam.sim.scenario import customer_view, injections, load_scenarios, marker, scenario_path, sms_codes

SCENARIOS = {s["id"]: s for s in load_scenarios(scenario_path("org_a"))}


def test_customer_view_never_contains_expected_title_tags_or_flags():
    for s in SCENARIOS.values():
        view = customer_view(s)
        assert set(view) <= {"caller", "persona", "goal", "opening", "known_to_customer", "reveal_rules",
                             "mid_call_events"}
        text = cust.user_message(s, [{"speaker": "customer", "text": s["opening"]}], 2, set())
        assert s["expected"]["outcome"] not in text and s["title"] not in text
        for item in s["expected"].get("forbidden", []) + s["expected"].get("must_say", []):
            assert item not in text, (s["id"], item)
        assert "invalidates_prior" not in text and "condition_change" not in text and "inject" not in text


def test_injection_events_go_to_runner_not_customer():
    s19 = SCENARIOS["s19"]
    assert injections(s19) == [(0, {"tool": "quote_refund", "code": "TIMEOUT", "count": 1000})]
    assert [e["index"] for e in customer_view(s19)["mid_call_events"]] == [1]
    assert all(not injections(s) for sid, s in SCENARIOS.items() if sid != "s19")


def test_marker_and_sms_codes():
    assert marker(SCENARIOS["s20"], 0) == {"condition_change": True, "invalidates_prior": True, "event": "s20#0"}
    assert marker(SCENARIOS["s03"], 0)["invalidates_prior"] is False
    assert sms_codes(SCENARIOS["s01"]) == {"010-0000-0001": "0001"}
    assert sms_codes(SCENARIOS["s09"]) == {"010-0000-0009": "0009"}  # 엄마가 가진 딸 휴대폰
    assert sms_codes(SCENARIOS["s25"]) == {"010-0000-0099": "0099"}  # 등록되지 않은 번호도 문자는 온다고 본다


def test_check_event_rejects_unknown_repeated_and_injection_indices():
    s19 = SCENARIOS["s19"]
    assert cust.check_event(s19, 1, set()) == (1, None)
    assert cust.check_event(s19, None, set()) == (None, None)
    assert cust.check_event(s19, 0, set())[1]["reason"] == "고객이 실행할 사건 번호가 아니다"
    assert cust.check_event(s19, 1, {1})[1]["reason"] == "이미 실행한 사건이다"
    assert cust.check_event(s19, True, set())[0] is None


class FakeClient:
    model = "fake-flash"

    def __init__(self, reply):
        self.reply = reply
        self.user = None

    async def ask(self, system, user, schema):
        self.user = user
        return self.reply

    async def aclose(self):
        pass


def test_gemini_customer_parses_and_validates_output():
    s20 = SCENARIOS["s20"]
    client = FakeClient(JsonReply({"say": " 아 그냥 해지할게요 ", "executed_event": 0, "done": False}, "m", cost_usd=0.001))
    c = cust.GeminiCustomer(s20, client)
    t = asyncio.run(c.next_turn([{"speaker": "customer", "text": "hi"}, {"speaker": "agent", "text": "네"}], 4, set()))
    assert (t.say, t.executed_event, t.done, t.error) == ("아 그냥 해지할게요", 0, False, None)
    payload = json.loads(client.user.removeprefix("```json\n").removesuffix("\n```"))
    assert payload["progress"] == {"next_message_no": 4, "executed_events": [], "remaining_events": [0],
                                   "sms": {"010-0000-0016": "인증번호 0016"}}
    assert [x["speaker"] for x in payload["conversation"]] == ["고객", "상담원"]
    bad = cust.GeminiCustomer(s20, FakeClient(JsonReply(None, "m", error="HTTP 500: x")))
    assert asyncio.run(bad.next_turn([], 2, set())).error == "HTTP 500: x"
    odd = cust.GeminiCustomer(s20, FakeClient(JsonReply({"say": "네", "executed_event": 0, "done": False}, "m")))
    t = asyncio.run(odd.next_turn([], 5, {0}))
    assert t.executed_event is None and t.rejected_event["index"] == 0
