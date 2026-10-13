"""시뮬레이터 지표·판정 테스트용 손으로 만드는 엔진 이벤트 로그. 테스트에서만 쓴다."""

from typing import Any


class LogBuilder:
    """엔진 로그와 같은 모양의 이벤트를 시각 순서대로 쌓는다. 시각은 호출마다 step ms씩 늘어난다."""

    def __init__(self, session: str = "t", step: float = 10.0):
        self.session = session
        self.events: list[dict[str, Any]] = []
        self.t = 0.0
        self.step = step
        self.version = 0
        self.n_utt = 0
        self.n_call = 0

    def _emit(self, type_: str, t: float | None = None, version: int | None = None, **fields: Any) -> dict[str, Any]:
        self.t = self.t + self.step if t is None else t
        e = {"t_ms": self.t, "session": self.session, "type": type_,
             "version": self.version if version is None else version, **fields}
        self.events.append(e)
        return e

    def utt(self, text: str = "...", marker: dict[str, Any] | None = None, t: float | None = None) -> str:
        self.n_utt += 1
        uid = f"u{self.n_utt}"
        self._emit("utterance.end", t=t, utt_id=uid, speaker="customer", text=text, final=True, marker=marker)
        self.version += 1
        self._emit("version.bumped", cause="utterance", utt_id=uid, **{"from": self.version - 1, "to": self.version})
        return uid

    def bump_tool(self) -> None:
        self.version += 1
        self._emit("version.bumped", cause="tool", **{"from": self.version - 1, "to": self.version})

    def evidence(self, version: int | None = None, t: float | None = None) -> None:
        self._emit("evidence.updated", t=t, version=version)

    def policy(self, rule: str, writes: list[str] = (), agreed: bool = False, proposal: str | None = None) -> None:
        self._emit("policy.decided", rule=rule, action="answer", agreed=agreed, writes=list(writes), proposal=proposal)

    def suggest(self, sid: str, basis: int | None = None, action: str = "answer", amount: int | None = None,
                amount_final: bool = False, cited: list[str] = (), proposal: dict[str, Any] | None = None,
                intent: str | None = None, channel: str = "chat", t: float | None = None, text: str = "안내") -> None:
        b = self.version if basis is None else basis
        self._emit("suggestion.created", suggestion_id=sid, basis_version=b, text=text, action=action,
                   rule="proposal" if proposal else "decision", intent=intent, cited_articles=list(cited),
                   amount=amount, amount_final=amount_final, proposal=proposal)
        e = self._emit("agent.say", t=t, version=b, text=text, suggestion_id=sid)
        self.events.append({"t_ms": e["t_ms"], "session": self.session, "type": "suggestion.exposed", "version": b,
                            "suggestion_id": sid, "channel": channel, "start_ms": e["t_ms"], "end_ms": e["t_ms"]})

    def call(self, tool: str, args: dict[str, Any], ok: bool = True, data: dict[str, Any] | None = None,
             error_code: str | None = None, attempts: int = 1) -> None:
        self.n_call += 1
        cid = f"c{self.n_call}"
        self._emit("tool.call", request_version=self.version, call_id=cid, tool=tool, args=args)
        self._emit("tool.result", request_version=self.version, call_id=cid, tool=tool, ok=ok, error_code=error_code,
                   attempts=attempts, data=(data or {}) if ok else {"code": error_code, "message": "오류"})

    def verify(self, customer_id: str = "C001", phone: str = "010-0000-0001") -> None:
        self.call("verify_identity", {"phone": phone, "otp": "****"}, data={"verified": True, "customer_id": customer_id})


def mark(event: str, cc: bool = True, ip: bool = True) -> dict[str, Any]:
    return {"condition_change": cc, "invalidates_prior": ip, "event": event}
