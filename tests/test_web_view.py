"""상담 콘솔의 이벤트 → 화면 메시지 옮기기와 화면 상태(jikgam/web/view.py)."""

import asyncio

from engine_fakes import EchoGenerator, ScriptedDecider, utt
from sim_logs import LogBuilder, mark

from jikgam.engine import events as ev
from jikgam.engine.bizapi import ToolResult
from jikgam.engine.org import load_org
from jikgam.engine.session import Session
from jikgam.web.view import ConsoleView, ui_message

ORG = load_org("org_a")


def view_of(events):
    v = ConsoleView({"A-05": "청약철회"}, deduction_reasons=ORG.policy.deduction_reasons)
    for e in events:
        v.apply(e)
    return v


def test_every_event_type_maps_to_a_message_with_common_fields():
    types = [getattr(ev, k) for k in dir(ev) if k.isupper() and isinstance(getattr(ev, k), str) and "." in getattr(ev, k)]
    types += [ev.WARMUP, "unknown.type"]
    for t in types:
        e = {"t_ms": 1.0, "type": t, "version": 2, "suggestion_id": "x-s1", "call_id": "c1", "reasons": [],
             "sufficient": None}
        m = ui_message(e)
        assert m["type"] == t and m["t_ms"] == 1.0 and m["version"] == 2
        assert isinstance(m["kind"], str) and isinstance(m["text"], str) and m["text"]
    assert ui_message({"t_ms": 0, "type": "unknown.type", "version": 0})["kind"] == "other"


def test_message_kinds_and_korean_labels():
    m = ui_message({"t_ms": 5, "type": ev.SUGGESTION_INVALIDATED, "version": 3, "suggestion_id": "sess-s2",
                    "basis_version": 2, "invalidated_version": 3,
                    "reasons": ["condition_changed=true", "intent_changed:cancel->refund", "caller_changed:owner->third_party"]})
    assert m["kind"] == "invalidated" and "s2" in m["text"]
    assert m["reasons"] == ["고객 조건 변경 (condition_changed=true)", "요청 변경: 해지 → 환불 (intent_changed)",
                            "통화자 변경: 본인 → 제3자 (caller_changed)"]
    u = ui_message({"t_ms": 1, "type": ev.UTTERANCE_END, "version": 0, "utt_id": "u4", "speaker": "customer",
                    "text": "아 그냥 해지할게요", "marker": mark("s20#0")})
    assert u["kind"] == "utterance" and "[조건 변경 표시]" in u["text"]
    p = ui_message({"t_ms": 1, "type": ev.POLICY_DECIDED, "version": 4, "rule": "identity_required", "action": "ask",
                    "ask": "phone"})
    assert p["kind"] == "policy" and "본인 확인 요청" in p["text"] and "휴대폰 번호" in p["text"]
    d = ui_message({"t_ms": 1, "type": ev.RESULT_DISCARDED, "version": 5, "stage": "generation", "started_version": 4})
    assert d["kind"] == "discarded" and "생성" in d["text"] and "v4" in d["text"]
    c = ui_message({"t_ms": 1, "type": ev.TASK_CANCELLED, "version": 5, "stage": "decision", "started_version": 4})
    assert c["kind"] == "cancelled"


def test_view_tracks_suggestion_status_invalidation_and_supersede():
    b = LogBuilder(session="x")
    b.utt("해지하고 싶어요")
    b.suggest("x-s1", text="번호를 알려 주세요")
    b.utt("아 환불도요", marker=mark("t#0"))
    b._emit(ev.SUGGESTION_INVALIDATED, suggestion_id="x-s1", basis_version=1, invalidated_version=2,
            reasons=["condition_changed=true"])
    v = view_of(b.events)
    snap = v.snapshot()
    assert snap["current"]["status"] == "invalidated" and snap["current"]["invalidated_version"] == 2
    assert snap["current"]["reasons"][0]["label"].startswith("고객 조건 변경")
    assert snap["pending"] is True  # 버전 2인데 마지막 제안은 v1 기준
    b.suggest("x-s2", text="환불 견적을 볼게요")
    snap = view_of(b.events).snapshot()
    assert [s["status"] for s in snap["suggestions"]] == ["invalidated", "active"]
    assert snap["pending"] is False
    b.suggest("x-s3", text="다음 안내")
    assert [s["status"] for s in view_of(b.events).snapshot()["suggestions"]] == ["invalidated", "superseded", "active"]
    transcript = [(t["speaker"], t["text"]) for t in snap["transcript"]]
    assert transcript[0] == ("customer", "해지하고 싶어요") and transcript[1] == ("engine", "번호를 알려 주세요")


def test_facts_come_from_tool_results_and_claims_are_separate():
    b = LogBuilder(session="x")
    b.utt("6월 30일에 9,900원 결제됐어요")
    b._emit(ev.CLAIMS_EXTRACTED, utt_id="u1", keys={"payment_date": "2026-06-30", "payment_amount": "9900원"},
            phones=[], otps=[], accounts=0)
    b.utt("아 7월 15일이었어요")
    b._emit(ev.CLAIMS_EXTRACTED, utt_id="u2", keys={"payment_date": "2026-07-15"}, phones=["010-0000-0001"],
            otps=["****"], accounts=0)
    b.verify()
    b.call("list_payments", {"customer_id": "C001"}, data={"payments": [{"payment_id": "P001", "date": "2026-07-15",
                                                                         "amount": 9900, "method": "card"}]})
    b.call("quote_refund", {"customer_id": "C001", "subscription_id": "S001", "reason": "withdrawal"},
           ok=False, error_code="NOT_ELIGIBLE")
    b.call("quote_refund", {"customer_id": "C001", "subscription_id": "S001", "reason": "annual_cancel"},
           data={"eligible": True, "refund_amount": 52000, "approval_required": True, "article_ids": ["A-08"]})
    b._emit(ev.QUOTE_SELECTED, chosen="annual_cancel")
    snap = view_of(b.events).snapshot()
    f = snap["facts"]
    assert f["identity"]["verified"] and f["identity"]["customer_id"] == "C001"
    assert f["payments"][0]["payment_id"] == "P001" and f["subscriptions"] is None  # 조회 전은 None
    quotes = {q["reason"]: q for q in f["quotes"]}
    assert quotes["withdrawal"]["status"] == "not_eligible"
    assert quotes["annual_cancel"]["status"] == "eligible" and quotes["annual_cancel"]["refund_amount"] == 52000
    assert quotes["annual_cancel"]["chosen"] and quotes["annual_cancel"]["approval_required"]
    assert quotes["annual_cancel"]["kind"] == "refund"
    claims = [(c["key"], c["value"], c["utt_id"], c["superseded"]) for c in snap["claims"]]
    assert ("payment_date", "2026-06-30", "u1", True) in claims  # 이후 다시 말한 진술
    assert ("payment_date", "2026-07-15", "u2", False) in claims
    assert ("otp", "****", "u2", False) in claims
    # 고객 진술은 시스템 사실에 섞이지 않는다
    assert "2026-06-30" not in str(f)


def test_discarded_lookup_result_is_not_a_fact():
    b = LogBuilder(session="x")
    b.utt("조회해 주세요")
    b.call("list_payments", {"customer_id": "C001"}, data={"payments": [{"payment_id": "P9"}]})
    b._emit(ev.RESULT_DISCARDED, stage="tool", started_version=1, tool="list_payments")
    snap = view_of(b.events).snapshot()
    assert snap["facts"]["payments"] is None
    assert snap["tools"][0]["discarded"] and snap["discards"][0]["kind"] == "discarded"


def test_evidence_panel_with_probabilities_and_rag_without():
    b = LogBuilder(session="x")
    b.utt("청약철회 되나요")
    b._emit(ev.RETRIEVAL_DONE, started_version=1, duration_ms=0.3, candidates=["A-05", "A-07"],
            sources={"A-05": "bm25", "A-07": "ref:A-05"})
    b._emit(ev.EVIDENCE_UPDATED, articles=["A-05"], candidates=["A-05", "A-07"], applies={"A-05": 0.91, "A-07": 0.2},
            sufficient=True, next_info="none", action="answer", intent="refund", caller="owner")
    e = view_of(b.events).snapshot()["evidence"]
    assert e["has_probabilities"] and e["intent_label"] == "환불" and e["caller_label"] == "본인"
    rows = {c["id"]: c for c in e["candidates"]}
    assert rows["A-05"] == {"id": "A-05", "title": "청약철회", "p": 0.91, "selected": True, "source": "bm25"}
    assert rows["A-07"]["selected"] is False
    rag = LogBuilder(session="r")
    rag.utt("청약철회 되나요")
    rag._emit(ev.EVIDENCE_UPDATED, articles=["A-05", "A-99"], candidates=["A-05"], sufficient=None, next_info=None,
              action="answer", intent=None)
    e = view_of(rag.events).snapshot()["evidence"]
    assert not e["has_probabilities"] and [c["id"] for c in e["candidates"]] == ["A-05", "A-99"]
    assert all(c["p"] is None for c in e["candidates"])


class HandoffBiz:
    async def call(self, tool, args, target=None):
        return ToolResult(tool, args, True, data={"handoff_id": "HO-7", "reason": args["reason"],
                                                  "expected_wait_minutes": 3}, status=200)

    async def aclose(self):
        pass


def test_view_over_real_engine_session_log(tmp_path):
    """엔진 세션(가짜 판별·생성)이 남긴 로그로 화면 상태를 만들고, 인계 요청까지 반영되는지 본다."""

    async def go():
        s = Session(ORG, "t-view", tmp_path, decision_client=ScriptedDecider(intents=[("궁금", "other")]),
                    generator=EchoGenerator(), bizapi=HandoffBiz())
        await s.start()
        await s.handle(utt("u1", "가입 전에 환불 규정이 궁금해요"))
        await s.request_handoff()
        await s.aclose()

    asyncio.run(go())
    log = ev.read_log(tmp_path / "t-view.jsonl")
    v = view_of(log)
    snap = v.snapshot()
    assert snap["meta"]["policy"] == "C" and snap["meta"]["session"] == "t-view"
    assert snap["warmup"]["backend"] == "fake"
    assert snap["handoff"]["requested"] and snap["handoff"]["receipt"]["id"] == "HO-7"
    assert snap["current"]["rule"] == "handoff" and snap["current"]["action_label"] == "상담사 인계"
    assert snap["policy"]["rule"] == "handoff"
    assert [m["kind"] for m in map(ui_message, log)].count("suggestion") == 2
    assert snap["n_events"] == len(log) and snap["pending"] is False


def test_deduction_quote_is_not_labelled_as_refund():
    b = LogBuilder(session="x")
    b.utt("연 구독으로 바꾸고 싶어요")
    b.call("quote_refund", {"customer_id": "C016", "subscription_id": "S016", "reason": "plan_change"},
           data={"eligible": True, "refund_amount": 6380})
    snap = view_of(b.events).snapshot()
    (q,) = snap["facts"]["quotes"]
    assert "plan_change" in ORG.policy.deduction_reasons
    assert q["kind"] == "deduction" and q["status_label"] == "차감액(환불 아님)"
    assert "환불" not in snap["tools"][0]["summary"]
