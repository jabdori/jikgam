import asyncio
import json

from jikgam.engine.org import ROOT
from jikgam.sim import customer as cust
from jikgam.sim.gemini import JsonReply
from jikgam.sim.scenario import (customer_view, followup_events, injections, load_scenarios, marker, registered_phones,
                                 scenario_path, sms_codes)

SCENARIOS = {s["id"]: s for s in load_scenarios(scenario_path("org_a"))}
REGISTERED = registered_phones(ROOT / "domain/org_a/customers.json")


def test_customer_view_never_contains_expected_title_tags_or_flags():
    for s in SCENARIOS.values():
        view = customer_view(s)
        assert set(view) <= {"caller", "persona", "goal", "opening", "known_to_customer", "reveal_rules",
                             "mid_call_events"}
        text = cust.user_message(s, [{"speaker": "customer", "text": s["opening"]}], 2, set(), REGISTERED)
        assert s["expected"]["outcome"] not in text and s["title"] not in text
        for item in s["expected"].get("forbidden", []) + s["expected"].get("must_say", []):
            assert item not in text, (s["id"], item)
        assert "invalidates_prior" not in text and "condition_change" not in text and "inject" not in text


def test_condition_change_events_are_sent_as_followups():
    assert followup_events(SCENARIOS["s20"]) == {0}
    assert followup_events(SCENARIOS["s03"]) == {0}  # invalidates_prior가 거짓이어도 조건 변경이면 빠른 정정
    assert followup_events(SCENARIOS["s07"]) == frozenset()
    assert [e["send_as_followup"] for e in customer_view(SCENARIOS["s20"])["mid_call_events"]] == [True]
    assert [e["send_as_followup"] for e in customer_view(SCENARIOS["s07"])["mid_call_events"]] == [False, False]
    n = sum(len(followup_events(s)) for s in SCENARIOS.values())
    assert n == 8  # docs/evaluation.md §3.2: 8개 시나리오, 8개 이벤트


def test_injection_events_go_to_runner_not_customer():
    s19 = SCENARIOS["s19"]
    assert injections(s19) == [(0, {"tool": "quote_refund", "code": "TIMEOUT", "count": 1000})]
    assert [e["index"] for e in customer_view(s19)["mid_call_events"]] == [1]
    assert all(not injections(s) for sid, s in SCENARIOS.items() if sid != "s19")


def test_marker_and_sms_codes_only_for_registered_numbers():
    assert marker(SCENARIOS["s20"], 0) == {"condition_change": True, "invalidates_prior": True, "event": "s20#0"}
    assert marker(SCENARIOS["s03"], 0)["invalidates_prior"] is False
    assert len(REGISTERED) == 16 and "010-0000-0001" in REGISTERED
    assert sms_codes(SCENARIOS["s01"], REGISTERED) == {"010-0000-0001": "0001"}
    assert sms_codes(SCENARIOS["s09"], REGISTERED) == {"010-0000-0009": "0009"}  # 엄마가 가진 딸 휴대폰
    assert sms_codes(SCENARIOS["s25"], REGISTERED) == {}  # 등록되지 않은 번호로는 문자가 오지 않는다
    text = cust.user_message(SCENARIOS["s25"], [], 2, set(), REGISTERED)
    assert '"sms": {}' in text


def test_check_event_rejects_unknown_repeated_and_injection_indices():
    s19 = SCENARIOS["s19"]
    assert cust.check_event(s19, 1, set()) == (1, None)
    assert cust.check_event(s19, None, set()) == (None, None)
    assert cust.check_event(s19, 0, set())[1]["reason"] == "고객이 실행할 사건 번호가 아니다"
    assert cust.check_event(s19, 1, {1})[1]["reason"] == "이미 실행한 사건이다"
    assert cust.check_event(s19, True, set())[0] is None


def test_check_turn_followup_protocol():
    s20, s07 = SCENARIOS["s20"], SCENARIOS["s07"]
    t = cust.check_turn(s20, {"say": "네 알겠어요", "followup_say": "아 그냥 해지할게요", "executed_event": 0,
                              "done": False}, set(), {})
    assert (t.say, t.followup_say, t.executed_event, t.fast_missing, t.notes) == \
        ("네 알겠어요", "아 그냥 해지할게요", 0, None, [])
    # 빠른 정정 사건인데 메시지가 하나뿐: 그 메시지로 실행하고 미적용으로 남긴다
    t = cust.check_turn(s20, {"say": "아 그냥 해지할게요", "followup_say": "", "executed_event": 0, "done": False},
                        set(), {})
    assert (t.say, t.followup_say, t.executed_event, t.fast_missing) == ("아 그냥 해지할게요", None, 0, 0)
    t = cust.check_turn(s20, {"say": "", "followup_say": "아 그냥 해지할게요", "executed_event": 0, "done": False},
                        set(), {})
    assert (t.say, t.followup_say, t.fast_missing) == ("아 그냥 해지할게요", None, 0)
    # 빠른 정정 사건이 아닌데 두 번째 메시지가 있으면 이어 붙인다
    t = cust.check_turn(s07, {"say": "네네", "followup_say": "돈은 언제 들어와요?", "executed_event": 0, "done": False},
                        set(), {})
    assert (t.say, t.followup_say, t.executed_event, t.fast_missing) == ("네네 돈은 언제 들어와요?", None, 0, None)
    assert t.notes
    # 사건이 없는 평소 메시지
    t = cust.check_turn(s20, {"say": "네", "followup_say": "", "executed_event": None, "done": True}, set(), {})
    assert (t.say, t.followup_say, t.executed_event, t.done) == ("네", None, None, True)
    assert cust.check_turn(s20, {"say": 1, "followup_say": "", "done": False}, set(), {}).error


class FakeClient:
    model = "fake-flash"

    def __init__(self, reply):
        self.reply = reply
        self.user = None
        self.seed = None

    async def ask(self, system, user, schema, seed=None):
        self.user, self.seed = user, seed
        return self.reply

    async def aclose(self):
        pass


def test_gemini_customer_parses_output_and_sends_seed():
    s20 = SCENARIOS["s20"]
    client = FakeClient(JsonReply({"say": " 네 ", "followup_say": " 아 그냥 해지할게요 ", "executed_event": 0,
                                   "done": False}, "m", cost_usd=0.001))
    c = cust.GeminiCustomer(s20, client, REGISTERED, seed=7)
    t = asyncio.run(c.next_turn([{"speaker": "customer", "text": "hi"}, {"speaker": "agent", "text": "네"}], 4, set()))
    assert (t.say, t.followup_say, t.executed_event, t.done, t.error) == ("네", "아 그냥 해지할게요", 0, False, None)
    assert client.seed == 7
    payload = json.loads(client.user.removeprefix("```json\n").removesuffix("\n```"))
    assert payload["progress"] == {"next_message_no": 4, "executed_events": [], "remaining_events": [0],
                                   "sms": {"010-0000-0016": "인증번호 0016"}}
    assert [x["speaker"] for x in payload["conversation"]] == ["고객", "상담원"]
    bad = cust.GeminiCustomer(s20, FakeClient(JsonReply(None, "m", error="HTTP 500: x")), REGISTERED)
    assert asyncio.run(bad.next_turn([], 2, set())).error == "HTTP 500: x"
    odd = cust.GeminiCustomer(s20, FakeClient(JsonReply({"say": "네", "followup_say": "", "executed_event": 0,
                                                         "done": False}, "m")), REGISTERED)
    t = asyncio.run(odd.next_turn([], 5, {0}))
    assert t.executed_event is None and t.rejected_event["index"] == 0
