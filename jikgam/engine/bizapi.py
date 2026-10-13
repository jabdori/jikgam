"""업무 API 클라이언트(domain/org_a/api.md, bizapi/README.md).

- 모든 호출에 X-Session-ID 헤더로 상담 세션을 밝힌다. 본인 확인 상태는 서버가 세션별로 둔다.
- 쓰기 호출의 멱등 키는 (세션, 도구, 대상)에서 정해진다. 같은 대상에 대한 같은 쓰기는 재시도나 턴 재실행에서도 같은 키를
  쓰므로, 서버는 새 접수를 만들지 않고 처음 결과를 돌려준다.
- 재시도 예산(api.md 오류 표): TIMEOUT·UNAVAILABLE·INTERNAL과 연결 오류는 1회 다시 보내고, 그래도 실패하면 실패를 위로 알린다.
  쓰기도 같은 멱등 키로 다시 보내므로 중복 접수가 생기지 않는다. 그 밖의 오류는 다시 보내지 않는다.
"""

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Any

import httpx

WRITE_TOOLS = frozenset({"create_cancellation", "create_refund_request", "register_refund_account", "create_handoff"})
READ_TOOLS = frozenset({"verify_identity", "get_customer", "list_subscriptions", "list_payments", "get_usage",
                        "list_incidents", "quote_refund"})
RETRYABLE = frozenset({"TIMEOUT", "UNAVAILABLE", "INTERNAL"})


def idempotency_key(session: str, tool: str, target: str) -> str:
    return "idem-" + hashlib.sha256(f"{session}|{tool}|{target}".encode()).hexdigest()[:32]


def mask_args(args: dict[str, Any]) -> dict[str, Any]:
    """로그용. 인증번호와 계좌번호를 가린다(가상 데이터라도 실제 운영과 같은 습관을 둔다)."""
    out = dict(args)
    if "otp" in out:
        out["otp"] = "*" * len(str(out["otp"]))
    if "account" in out:
        digits = str(out["account"]).replace("-", "")
        out["account"] = "*" * (len(digits) - 3) + digits[-3:]
    return out


@dataclass
class ToolResult:
    tool: str
    args: dict[str, Any]
    ok: bool
    data: dict[str, Any] | None = None
    status: int | None = None
    error_code: str | None = None
    error: dict[str, Any] | None = None  # 오류 본문의 error 객체(article_ids, quote, receipt 포함)
    attempts: int = 1
    latency_ms: float = 0.0  # 마지막 시도가 아니라 첫 전송부터 마지막 응답까지(재시도 대기 포함)
    idempotency_key: str | None = None

    @property
    def exhausted(self) -> bool:
        """재시도 예산을 다 쓰고도 일시 장애로 실패했는가."""
        return not self.ok and self.error_code in RETRYABLE


class BizApiClient:
    def __init__(self, base_url: str, session_id: str, timeout_s: float = 5.0, retry_budget: int = 1,
                 retry_delay_s: float = 0.2, transport: httpx.AsyncBaseTransport | None = None):
        if not 1 <= len(session_id) <= 128:
            raise ValueError("세션 ID는 1~128자여야 한다")
        self.session_id = session_id
        self.retry_budget = retry_budget
        self.retry_delay_s = retry_delay_s
        self.http = httpx.AsyncClient(base_url=base_url, headers={"X-Session-ID": session_id}, timeout=timeout_s,
                                      transport=transport)

    async def call(self, tool: str, args: dict[str, Any], target: str | None = None) -> ToolResult:
        """도구 하나를 호출한다. 쓰기 도구는 target으로 멱등 키를 만들어 args에 넣는다."""
        if tool not in WRITE_TOOLS | READ_TOOLS:
            raise ValueError(f"알 수 없는 도구: {tool}")
        body = dict(args)
        key = None
        if tool in WRITE_TOOLS:
            if not target:
                raise ValueError(f"쓰기 도구 {tool}에는 멱등 키 대상이 필요하다")
            key = idempotency_key(self.session_id, tool, target)
            body["idempotency_key"] = key
        t0 = time.perf_counter()
        attempts = 0
        while True:
            attempts += 1
            result = await self._once(tool, body)
            if result.ok or result.error_code not in RETRYABLE or attempts > self.retry_budget:
                break
            await asyncio.sleep(self.retry_delay_s)
        result.attempts = attempts
        result.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        result.idempotency_key = key
        return result

    async def _once(self, tool: str, body: dict[str, Any]) -> ToolResult:
        try:
            r = await self.http.post(f"/v1/{tool}", json=body)
        except httpx.ConnectTimeout as e:  # 연결 자체가 안 됨(Windows에서는 닫힌 포트도 이렇게 나온다)
            return ToolResult(tool, body, False, error_code="UNAVAILABLE",
                              error={"code": "UNAVAILABLE", "message": f"ConnectTimeout: {e}"})
        except httpx.TimeoutException as e:
            return ToolResult(tool, body, False, error_code="TIMEOUT", error={"code": "TIMEOUT", "message": str(e)})
        except httpx.TransportError as e:
            return ToolResult(tool, body, False, error_code="UNAVAILABLE",
                              error={"code": "UNAVAILABLE", "message": f"{type(e).__name__}: {e}"})
        try:
            data = r.json()
        except ValueError:
            return ToolResult(tool, body, False, status=r.status_code, error_code="INTERNAL",
                              error={"code": "INTERNAL", "message": f"JSON이 아닌 응답: {r.text[:200]}"})
        if r.status_code == 200:
            return ToolResult(tool, body, True, data=data, status=200)
        err = data.get("error") if isinstance(data, dict) else None
        if not isinstance(err, dict) or "code" not in err:
            err = {"code": "INTERNAL", "message": f"HTTP {r.status_code}: 오류 형식이 아님"}
        return ToolResult(tool, body, False, status=r.status_code, error_code=err["code"], error=err)

    async def aclose(self) -> None:
        await self.http.aclose()
