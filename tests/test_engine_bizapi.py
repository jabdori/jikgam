import asyncio

import pytest
from conftest import _free_port, inject_fault

from jikgam.engine.bizapi import BizApiClient, idempotency_key


def run(coro):
    return asyncio.run(coro)


async def _verified(url: str, session: str, phone: str = "010-0000-0001", otp: str = "0001") -> BizApiClient:
    c = BizApiClient(url, session, retry_delay_s=0.0)
    r = await c.call("verify_identity", {"phone": phone, "otp": otp})
    assert r.ok and r.data["verified"]
    return c


def test_session_header_scopes_identity(bizapi):
    async def go():
        a = await _verified(bizapi, "sess-a")
        b = BizApiClient(bizapi, "sess-b")
        q = await a.call("quote_refund", {"customer_id": "C001", "subscription_id": "S001", "reason": "withdrawal",
                                          "request_date": "2026-10-12"})
        assert q.ok and q.data["refund_amount"] == 9900
        args = {"customer_id": "C001", "payment_id": "P001", "quote_id": q.data["quote_id"]}
        r = await b.call("create_refund_request", args, target="refund:P001")
        assert (r.ok, r.error_code, r.attempts) == (False, "IDENTITY_REQUIRED", 1)  # 다시 보내지 않는 오류
        await a.aclose()
        await b.aclose()
    run(go())


def test_write_idempotency_key_from_session_tool_target(bizapi):
    async def go():
        c = await _verified(bizapi, "sess-idem")
        q = await c.call("quote_refund", {"customer_id": "C001", "subscription_id": "S001", "reason": "withdrawal",
                                          "request_date": "2026-10-12"})
        args = {"customer_id": "C001", "payment_id": "P001", "quote_id": q.data["quote_id"]}
        first = await c.call("create_refund_request", args, target="refund:P001")
        again = await c.call("create_refund_request", args, target="refund:P001")
        assert first.ok and again.ok
        assert first.data["refund_id"] == again.data["refund_id"]  # 같은 키 → 처음 결과
        assert first.idempotency_key == idempotency_key("sess-idem", "create_refund_request", "refund:P001")
        assert first.args["idempotency_key"] == first.idempotency_key
        # 같은 대상을 다른 세션(다른 키)으로 접수하면 서버가 기존 접수를 알려 준다
        other = await _verified(bizapi, "sess-other")
        dup = await other.call("create_refund_request", args, target="refund:P001")
        assert dup.error_code == "DUPLICATE_REQUEST" and dup.error["receipt"]["refund_id"] == first.data["refund_id"]
        for x in (c, other):
            await x.aclose()
    run(go())


def test_write_requires_target():
    async def go():
        c = BizApiClient("http://127.0.0.1:9", "s")
        with pytest.raises(ValueError):
            await c.call("create_cancellation", {"customer_id": "C001", "subscription_id": "S001"})
        await c.aclose()
    run(go())


def test_timeout_is_retried_once_then_succeeds(bizapi):
    inject_fault(bizapi, "quote_refund", "TIMEOUT", 1)

    async def go():
        c = BizApiClient(bizapi, "sess-retry", retry_delay_s=0.0)
        r = await c.call("quote_refund", {"customer_id": "C004", "subscription_id": "S004", "reason": "annual_cancel",
                                          "request_date": "2026-10-12"})
        assert r.ok and r.attempts == 2
        await c.aclose()
    run(go())


def test_retry_budget_exhausted_reports_failure(bizapi):
    inject_fault(bizapi, "quote_refund", "TIMEOUT", 5)

    async def go():
        c = BizApiClient(bizapi, "sess-fail", retry_delay_s=0.0)
        r = await c.call("quote_refund", {"customer_id": "C004", "subscription_id": "S004", "reason": "annual_cancel",
                                          "request_date": "2026-10-12"})
        assert (r.ok, r.error_code, r.attempts, r.exhausted) == (False, "TIMEOUT", 2, True)
        await c.aclose()
    run(go())


def test_unavailable_write_retry_keeps_same_key(bizapi):
    inject_fault(bizapi, "create_cancellation", "UNAVAILABLE", 1)

    async def go():
        c = await _verified(bizapi, "sess-w", "010-0000-0016", "0016")
        r = await c.call("create_cancellation", {"customer_id": "C016", "subscription_id": "S016"}, target="cancel:S016")
        assert r.ok and r.attempts == 2 and r.data["last_usable_date"] == "2026-11-01"
        again = await c.call("create_cancellation", {"customer_id": "C016", "subscription_id": "S016"}, target="cancel:S016")
        assert again.ok and again.data["cancellation_id"] == r.data["cancellation_id"]
        await c.aclose()
    run(go())


def test_connection_error_is_unavailable_after_retry():
    async def go():
        c = BizApiClient(f"http://127.0.0.1:{_free_port()}", "sess-down", retry_delay_s=0.0, timeout_s=1.0)
        r = await c.call("get_customer", {"customer_id": "C001"})
        assert (r.ok, r.error_code, r.attempts, r.exhausted) == (False, "UNAVAILABLE", 2, True)
        await c.aclose()
    run(go())


def test_not_eligible_carries_quote(bizapi):
    async def go():
        c = BizApiClient(bizapi, "sess-ne")
        r = await c.call("quote_refund", {"customer_id": "C010", "subscription_id": "S010", "reason": "withdrawal",
                                          "request_date": "2026-10-12"})
        assert r.error_code == "NOT_ELIGIBLE" and r.attempts == 1
        assert "A-06" in r.error["article_ids"] and r.error["quote"]["reason"] == "withdrawal"
        await c.aclose()
    run(go())
