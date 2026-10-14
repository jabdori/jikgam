"""조직 B(책솔): 설정 정합성, 어댑터를 거친 엔진 업무 API 클라이언트, 가짜 판별·생성으로 돌린 엔진 세션. docs/org-b.md.

조직 B 모의 업무 시스템(cmd/orgb)과 어댑터(cmd/orgbgw)를 빌드해 임의 포트에서 띄운다. 엔진은 어댑터 주소만 안다.
"""

import asyncio
import dataclasses
import shutil
import subprocess
import time
import tomllib
from pathlib import Path

import httpx
import pytest
from conftest import _free_port, inject_fault
from engine_fakes import EchoGenerator, ScriptedDecider, utt

from jikgam.engine import events as ev
from jikgam.engine.bizapi import BizApiClient
from jikgam.engine.org import load_org
from jikgam.engine.retrieval import parse_terms
from jikgam.engine.session import Session

ROOT = Path(__file__).resolve().parents[1]
ORG_B = load_org("org_b")
ADAPTER_REASONS = {"withdrawal", "annual_cancel", "duplicate"}  # 어댑터가 조직 B 환불 유형으로 바꾸는 공통 사유


@pytest.fixture(scope="session")
def orgb_url(tmp_path_factory):
    go = shutil.which("go")
    if go is None:
        pytest.skip("go가 없어 조직 B 모의 서버를 띄울 수 없다")
    d = tmp_path_factory.mktemp("orgb")
    for cmd in ("orgb", "orgbgw"):
        subprocess.run([go, "build", "-o", str(d / f"{cmd}.exe"), f"./cmd/{cmd}"], cwd=ROOT / "bizapi", check=True,
                       capture_output=True, timeout=300)
    bport, gport = _free_port(), _free_port()
    backend = subprocess.Popen([str(d / "orgb.exe"), "-addr", f"127.0.0.1:{bport}", "-data",
                                str(ROOT / "domain/org_b/customers.json")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    gateway = subprocess.Popen([str(d / "orgbgw.exe"), "-addr", f"127.0.0.1:{gport}", "-backend", f"http://127.0.0.1:{bport}"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{gport}"
    try:
        for _ in range(100):
            try:
                if httpx.post(f"{url}/_admin/reset", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("조직 B 어댑터가 뜨지 않았다")
        yield url
    finally:
        for p in (gateway, backend):
            p.terminate()
            p.wait(timeout=10)


@pytest.fixture
def orgb(orgb_url):
    httpx.post(f"{orgb_url}/_admin/reset", timeout=5).raise_for_status()
    return orgb_url


# --- 설정 정합성 -----------------------------------------------------------------------------


def test_org_b_config_refers_only_to_org_b_articles():
    articles = parse_terms(ORG_B.terms)
    p = ORG_B.policy
    used = set(p.approval_articles) | set(p.third_party_articles) | set(p.guardian_articles) | set(p.addon_articles)
    used |= set(p.compensation.articles) | {a for r in ORG_B.retrieval.rules for a in r.add}
    assert used and used <= set(articles)
    refs = {r for a in articles.values() for r in a.refs}
    assert refs <= set(articles)
    assert not any(a.startswith(("A-", "T-")) for a in articles)


def test_org_b_quote_reasons_are_supported_by_adapter():
    p = ORG_B.policy
    reasons = {r for rs in p.quote_reasons.values() for r in rs} | set(p.quote_priority) | set(p.refundable_reasons)
    assert reasons <= ADAPTER_REASONS
    assert not p.addon_plans and not p.guardian_reasons and not p.deduction_reasons


def test_org_b_question_set_changes_only_wording():
    import json
    a = json.loads((ROOT / "data/question_sets/q-2026.10.14-mix.json").read_text(encoding="utf-8"))["questions"]
    b = json.loads(ORG_B.question_set.read_text(encoding="utf-8"))["questions"]
    assert set(a) == set(b)
    changed = {(q, k) for q in a for k, v in (a[q].get("criteria") or {}).items() if b[q]["criteria"][k] != v}
    assert changed == {("intent", "plan_change"), ("next_info", "plan_type"), ("next_info", "usage"), ("action", "handoff")}
    assert all(a[q]["instructions"] == b[q]["instructions"] for q in a)


def test_org_a_config_has_no_duplicate_window_key():
    """조직 A는 같은 날 기준(기본 0)이다. 조직 A 설정 파일을 바꾸지 않았는지 본다."""
    raw = tomllib.loads((ROOT / "config/orgs/org_a.toml").read_text(encoding="utf-8"))
    assert "duplicate_window_days" not in raw["policy"]
    assert load_org("org_a").policy.duplicate_window_days == 0


# --- 엔진 업무 API 클라이언트 → 어댑터 → 조직 B -----------------------------------------------


def test_engine_client_works_unchanged_through_adapter(orgb):
    async def go():
        c = BizApiClient(orgb, "t-b-client", retry_delay_s=0.0)
        v = await c.call("verify_identity", {"phone": "010-5000-0103", "otp": "000103"})
        assert v.ok and v.data["verified"] and v.data["customer_id"] == "M1003"
        q = await c.call("quote_refund", {"customer_id": "M1003", "subscription_id": "PS1003", "reason": "annual_cancel",
                                          "request_date": "2026-10-13"})
        assert q.ok and q.data["refund_amount"] == 107910 and q.data["approval_required"]
        r = await c.call("create_refund_request", {"customer_id": "M1003", "payment_id": "CH1003-1",
                                                   "quote_id": q.data["quote_id"]}, target="refund:CH1003-1")
        assert r.ok and r.data["status"] == "pending_approval" and r.idempotency_key.startswith("idem-")
        again = await c.call("create_refund_request", {"customer_id": "M1003", "payment_id": "CH1003-1",
                                                       "quote_id": q.data["quote_id"]}, target="refund:CH1003-1")
        assert again.data["refund_id"] == r.data["refund_id"]  # 엔진 멱등 키 → 조직 B Idempotency-Key 헤더
        h = await c.call("quote_refund", {"customer_id": "M1008", "subscription_id": "PS1008", "reason": "annual_cancel",
                                          "request_date": "2026-10-13"})
        assert (h.error_code, h.error["quote"]["handoff_reason"]) == ("HANDOFF_REQUIRED", "dispute")
        await c.aclose()
    asyncio.run(go())


def test_engine_retry_budget_sees_translated_timeout(orgb):
    inject_fault(orgb, "quote_refund", "TIMEOUT", 2)  # 어댑터 → 조직 B UPSTREAM_TIMEOUT 2회

    async def go():
        c = BizApiClient(orgb, "t-b-retry", retry_delay_s=0.0)
        r = await c.call("quote_refund", {"customer_id": "M1001", "subscription_id": "PS1001", "reason": "withdrawal",
                                          "request_date": "2026-10-13"})
        await c.aclose()
        return r
    r = asyncio.run(go())
    assert (r.ok, r.error_code, r.attempts, r.exhausted) == (False, "TIMEOUT", 2, True)


# --- 엔진 세션(가짜 판별·생성) → 어댑터 → 조직 B ----------------------------------------------


def _run(org, url, sid, tmp_path, dec, lines):
    async def go():
        s = Session(org, sid, tmp_path, decision_client=dec, generator=EchoGenerator(), bizapi=BizApiClient(url, sid))
        outs = [await s.handle(utt(f"u{i}", t)) for i, t in enumerate(lines, 1)]
        await s.aclose()
        return s, outs
    return asyncio.run(go())


def _tools(out):
    return [e["tool"] for e in out if e["type"] == ev.TOOL_CALL]


def test_b01_withdrawal_on_day_8(orgb, tmp_path):
    dec = ScriptedDecider(intents=[("환불", "refund")])
    s, outs = _run(ORG_B, orgb, "t-b01", tmp_path, dec, [
        "지난 5일에 결제된 이번 달 이용권 환불받고 싶어요. 한 권도 안 읽었어요.", "010-5000-0101이요.",
        "인증번호는 공공공일공일이에요.", "네, 그렇게 진행해 주세요."])
    sel = [e for e in outs[2] if e["type"] == ev.QUOTE_SELECTED][0]
    assert sel["chosen"] == "withdrawal" and sel["tried"]["withdrawal"]["refund_amount"] == 11900  # 14일 청약철회
    assert _tools(outs[3]) == ["create_refund_request", "create_cancellation"]
    assert s.state.facts.receipts["cancel:PS1001"]["last_usable_date"] == "2026-10-13"


def test_b03_duplicate_on_different_days_needs_window(orgb, tmp_path):
    lines = ["이번 달 이용권 요금이 중복으로 결제된 것 같아요. 환불해 주세요.", "010-5000-0106입니다.",
             "인증번호 공공공일공육이요.", "네, 중복된 것만 환불해 주세요."]
    dec = ScriptedDecider(intents=[("중복", "refund")])
    s, outs = _run(ORG_B, orgb, "t-b03", tmp_path, dec, lines)
    sel = [e for e in outs[2] if e["type"] == ev.QUOTE_SELECTED][0]
    assert sel["chosen"] == "duplicate"
    assert _tools(outs[3]) == ["create_refund_request"]  # 중복 결제는 이용권을 유지한다(제10조 제3항)
    assert s.state.facts.receipts["refund:CH1006-4"]["amount"] == 11900
    # 엔진 변경 전(같은 날 기준, duplicate_window_days=0)에는 중복 견적을 묻지 않아 환불 불가로 끝났다
    httpx.post(f"{orgb}/_admin/reset", timeout=5).raise_for_status()
    same_day = dataclasses.replace(ORG_B, policy=dataclasses.replace(ORG_B.policy, duplicate_window_days=0))
    s0, outs0 = _run(same_day, orgb, "t-b03-same-day", tmp_path, ScriptedDecider(intents=[("중복", "refund")]), lines)
    sel0 = [e for e in outs0[2] if e["type"] == ev.QUOTE_SELECTED][0]
    assert "duplicate" not in sel0["tried"] and sel0["chosen"] is None
    assert not [t for o in outs0 for t in _tools(o) if t.startswith("create_refund")]
