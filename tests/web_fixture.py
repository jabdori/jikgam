"""웹 콘솔 테스트 자료(가상). 숫자 출처 검사(ADR 0022)가 두 벌을 렌더링해 비교하므로 variant 0과 1의 수치가 서로 겹치지
않게 고른다: 건수(0은 작은 수, 1은 큰 소수), 금액, 날짜, 시각 간격, 확률, 지연, 비용이 모두 다르다.

build(tmp, variant)는 저장소와 같은 모양의 작은 루트 폴더를 만들고 그 경로를 돌려준다. 회사·고객은 모두 가상이다.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

KST = timezone(timedelta(hours=9))

# variant → 값. 0과 1에서 같은 숫자가 나오지 않게 고른다.
P = {
    0: {"date": datetime(2026, 10, 14, 10, 0, tzinfo=KST), "step": 1301.7, "amount": 9900, "price": 9900,
        "outcomes": {"filed": 2, "answered": 1, "handoff": 1, "hold": 1, "dropped": 1}, "changed": 1, "wrong": 0, "wrong_each": 0,
        "feeds": 1, "articles": 4, "notices": 2, "window": 7, "effective": "2026-07-01", "revised": "2026-10-12",
        "acc": {"jev": (5, 5), "gemini-flash": (4, 5), "local-t": (3, 5)}, "lat": 101.5, "cost": 0.000113,
        "gold": ["T-01", "T-02", "T-03"], "probs": [0.81, 0.62, 0.27, 0.11], "dec_lat": 211.3, "reps": 2,
        "voice": {"passed": 7, "valid": 9, "p95": 1201.5, "stale": 0, "den": 6}, "load": (3, 41, 11.2),
        "changes": 1, "expansions": 2, "mult": 3, "hours": 4},
    1: {"date": datetime(2026, 11, 3, 15, 0, tzinfo=KST), "step": 2179.3, "amount": 13700, "price": 12900,
        "outcomes": {"filed": 11, "answered": 13, "handoff": 17, "hold": 19, "dropped": 23}, "changed": 7, "wrong": 7, "wrong_each": 3,
        "feeds": 8, "articles": 9, "notices": 5, "window": 21, "effective": "2026-09-15", "revised": "2026-11-01",
        "acc": {"jev": (5, 7), "gemini-flash": (6, 7), "local-t": (1, 7)}, "lat": 333.9, "cost": 0.000371,
        "gold": ["T-02", "T-04", "T-05", "T-06", "T-07"], "probs": [0.93, 0.74, 0.39, 0.16], "dec_lat": 487.9, "reps": 3,
        "voice": {"passed": 29, "valid": 37, "p95": 2333.3, "stale": 13, "den": 43}, "load": (17, 173, 29.7),
        "changes": 9, "expansions": 11, "mult": 15, "hours": 9},
}


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    _w(path, "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")


class Log:
    """엔진 이벤트 로그를 시각 순서로 쌓는다."""

    def __init__(self, session: str, start: datetime, step: float, org: str, voice: bool = False):
        self.events: list[dict[str, Any]] = []
        self.t = 1000.0
        self.step = step
        self.v = 0
        self.n_utt = 0
        self.n_sug = 0
        self.n_call = 0
        self.session = session
        self.start = start
        self.voice = voice
        self.emit("session.start", org=org, today="2026-10-12", decision_backend="jev", applies_threshold=0.5)
        if voice:
            self.emit("voice.session", policy="C", channel={"final_wait_ms": 3000})

    def emit(self, type_: str, t: float | None = None, **f: Any) -> dict[str, Any]:
        if t is None:
            self.t += self.step
            t = self.t
        wall = (self.start + timedelta(milliseconds=t)).isoformat(timespec="milliseconds")
        e = {"t_ms": round(t, 3), "wall": wall, "session": self.session, "type": type_, "version": self.v, **f}
        self.events.append(e)
        return e

    def utt(self, text: str, intent: str, marker: dict[str, Any] | None = None) -> int:
        self.n_utt += 1
        uid = f"u{self.n_utt}"
        if self.voice:
            t0 = self.t + self.step
            self.emit("voice.segment", line=self.n_utt, text=text, speech_start_ms=round(t0, 3),
                      speech_end_ms=round(t0 + self.step * 0.6, 3))
        u = self.emit("utterance.end", utt_id=uid, speaker="customer", text=text, final=True, marker=marker)
        self.v += 1
        self.emit("version.bumped", cause="utterance", utt_id=uid, **{"from": self.v - 1, "to": self.v})
        self.emit("decision.response", request_version=self.v, backend="jev", model="jev-x", latency_ms=200.0,
                  answers={"intent": {"label": intent, "p": 0.9}})
        return u["t_ms"]

    def tool(self, tool: str, args: dict[str, Any], data: dict[str, Any], ok: bool = True) -> None:
        self.n_call += 1
        cid = f"c{self.n_call}"
        self.emit("tool.call", request_version=self.v, call_id=cid, tool=tool, args=args)
        self.emit("tool.result", request_version=self.v, call_id=cid, tool=tool, ok=ok, data=data, error_code=None)

    def policy(self, rule: str, action: str, ask: str | None = None) -> None:
        self.emit("policy.decided", rule=rule, action=action, ask=ask)

    def say(self, text: str, cited: list[str], basis: int | None = None) -> str:
        self.n_sug += 1
        sid = f"{self.session}-s{self.n_sug}"
        b = self.v if basis is None else basis
        self.emit("suggestion.created", suggestion_id=sid, basis_version=b, text=text, cited_articles=cited, action="answer")
        e = self.emit("suggestion.exposed", suggestion_id=sid, channel="voice" if self.voice else "chat")
        e["start_ms"] = e["t_ms"]
        if self.voice:
            self.emit("tts.playback_end", suggestion_id=sid, start_ms=e["t_ms"], end_ms=round(self.t + self.step, 3),
                      played_ms=round(self.step, 1), interrupted=None)
        return sid


def call_log(session: str, outcome: str, i: int, p: dict[str, Any], org: str, changed: bool = False,
             voice: bool = False) -> list[dict[str, Any]]:
    start = p["date"] + timedelta(minutes=7 * i + 3)
    lg = Log(session, start, p["step"], org, voice)
    phone = f"010-0000-{1000 + i:04d}"
    lg.utt("해지하고 싶어요.", "cancel")
    if outcome == "dropped":
        lg.policy("identity_required", "ask", ask="phone")
        lg.say("휴대폰 번호를 알려 주시겠어요?", [])
        return lg.events
    if outcome == "hold":
        lg.policy("api_failure", "hold")
        lg.say("잠시 후 다시 확인해 드리겠습니다.", [])
        return lg.events
    lg.utt(f"{phone}이에요. 인증번호는 {1000 + i}이에요.", "cancel")
    lg.tool("verify_identity", {"phone": phone, "otp": "****"}, {"verified": True, "customer_id": f"T{i:03d}"})
    lg.tool("get_customer", {"customer_id": f"T{i:03d}"}, {"name": f"고객{i}"})
    lg.tool("list_subscriptions", {}, {"subscriptions": [{"plan": "monthly", "price": p["price"], "status": "active",
                                                          "next_billing": (p["date"] + timedelta(days=19)).date().isoformat()}]})
    lg.tool("list_payments", {}, {"payments": [{"date": (p["date"] - timedelta(days=3)).date().isoformat(), "method": "card"}]})
    lg.tool("get_usage", {}, {"usage": [{"date": (p["date"] - timedelta(days=2)).date().isoformat()}]})
    if outcome == "handoff":
        lg.tool("create_handoff", {"reason": "minor"}, {"handoff_id": "H1", "reason": "minor"})
        lg.policy("handoff", "handoff")
        lg.say("상담사에게 연결해 드리겠습니다.", ["T-03"])
        return lg.events
    if outcome == "answered":
        lg.tool("quote_refund", {}, {"eligible": False, "article_ids": ["T-02"]})
        lg.policy("not_eligible", "answer")
        lg.say("약관에 따라 환불 대상이 아닙니다.", ["T-02"])
        return lg.events
    # filed
    first = lg.say("해지를 진행해 드릴까요?", ["T-01"])
    if changed:
        before = lg.v
        lg.utt("아니요, 환불도 해 주세요.", "cancel_refund",
               marker={"condition_change": True, "invalidates_prior": True, "event": f"{session}#0"})
        for _ in range(p["wrong_each"]):  # 바뀌기 전 기준으로 만든 답이 바뀐 뒤에 나감(잘못된 안내)
            lg.say("해지만 진행하겠습니다.", ["T-01"], basis=before)
    _ = first
    lg.tool("quote_refund", {}, {"eligible": True, "article_ids": ["T-04"]})
    lg.tool("create_cancellation", {}, {"cancellation_id": "CN1", "last_usable_date": (p["date"] + timedelta(days=18)).date().isoformat()})
    lg.tool("create_refund_request", {}, {"refund_id": "RF1", "amount": p["amount"] + i, "status": "received"})
    lg.policy("received", "answer")
    lg.say("해지와 환불을 접수했습니다.", ["T-01", "T-04"])
    return lg.events


def terms(n_articles: int, p: dict[str, Any], name: str) -> str:
    lines = [f"# {name} 약관 (가상)", "", f"- 약관 버전: 2026.{p['window']:02d} (문서 개정 r1, {p['revised']}: 문구 정리)",
             f"- 시행일: {p['effective']}", "", "---", ""]
    for k in range(1, n_articles + 1):
        lines += [f"## [T-{k:02d}] 제{k}조 (조항 {k})", "<!-- type: general; refs: -->", "",
                  f"1. 조항 {k}의 첫째 항이다.", "2. 다음의 경우 상담사에게 연결한다.", "   - 미성년자 결제 취소", "   - 이의 제기", ""]
    return "\n".join(lines)


def org_toml(org: str, p: dict[str, Any], window: int, expansions: int) -> str:
    exp = "\n".join(f'[[retrieval.expansions]]\npattern = "말{k}"\nterms = "조항"' for k in range(expansions))
    notices = ", ".join(f'"안내 {k}"' for k in range(p["notices"]))
    return f'''org = "{org}"
terms = "domain/{org}/terms.md"
today = "2026-10-12"
bizapi_url = "http://127.0.0.1:65001"
decision_backend = "jev"
question_set = "data/question_sets/q-{org}.json"
required_notices = [{notices}]

[policy]
window_days = {window}
refundable_reasons = ["withdrawal"]
guardian_reasons = []
addon_plans = []

[policy.compensation]
min_hours = {p["hours"] + (1 if org.endswith("u") else 0)}
multiplier = {p["mult"]}
causes = ["company"]

[retrieval]
max_candidates = 8
{exp}
'''


def decision_run(root: Path, run_id: str, backends: list[str], p: dict[str, Any], alone: bool = False) -> None:
    n = p["acc"]["jev"][1]
    data = root / "data/build/test.jsonl"
    if not data.exists():
        _jsonl(data, [{"id": f"c{k}", "group": f"g{k}", "split": "test", "tags": [], "state": "x",
                       "gold": json.dumps({"applies:T-01": {"label": "true"}})} for k in range(n)])
    sha = hashlib.sha256(data.read_bytes()).hexdigest()[:16]
    kinds = {"jev": "systemone", "gemini-flash": "gemini", "local-t": "systemone"}
    meta = {"run_id": run_id, "split": "test", "data_file": "data/build/test.jsonl", "data_sha256_16": sha, "cases": n,
            "backends": {b: {"kind": kinds[b], "model": b} for b in backends}, "reps": {b: p["reps"] for b in backends}}
    _w(root / "results/decisions/test" / run_id / "meta.json", json.dumps(meta))
    raw = []
    for rep in range(p["reps"]):
        for k in range(n):
            for b in backends:
                right = k < p["acc"][b][0]
                lat = p["lat"] * (1 + 0.37 * k + 0.11 * rep) * (1 if b != "gemini-flash" else 9.7) * (1.7 if alone else 1)
                raw.append({"rep": rep, "case": f"c{k}", "backend": b, "model": b, "latency_ms": round(lat, 1), "attempts": 1,
                            "input_tokens": 1000, "cost_usd": p["cost"] * (11 if b == "gemini-flash" else 1), "error": None,
                            "answers": {"applies:T-01": {"type": "noul", "noul": 0.9 if right else 0.1}}})
    _jsonl(root / "results/decisions/test" / run_id / "raw.jsonl", raw)


def build(tmp: Path, variant: int) -> Path:
    p = P[variant]
    root = tmp / f"root{variant}"
    orgs = {"org_t": "가상 회사 티", "org_u": "가상 회사 유"}
    # 고객사 설정
    live = [f"results/voice/vrun-C/live{k}.jsonl" for k in range(p["feeds"])]
    _w(root / "config/web/tenants.toml", f'''[org_t]
name = "{orgs['org_t']}"
desc = "고객센터"
service = "가상 구독"
history = ["results/engine/scenarios/run-C", "results/voice/vrun-C"]
holdout_history = []
live = {json.dumps(live)}
voice = true
handoff_article = "T-01"
adapter = "bizapi/internal/orgt"

[org_u]
name = "{orgs['org_u']}"
desc = "고객센터"
service = "가상 대여"
history = ["results/engine/scenarios/run-U"]
holdout_history = []
live = []
voice = false
handoff_article = "T-01"
adapter = "bizapi/internal/orgu"
changes = "docs/org-u.md"
''')
    for org, window, exp in (("org_t", p["window"], p["expansions"]), ("org_u", p["window"] * 2, p["expansions"] * 3)):
        _w(root / f"config/orgs/{org}.toml", org_toml(org, p, window, exp))
        _w(root / f"domain/{org}/terms.md", terms(p["articles"], p, orgs[org]))
        _w(root / f"data/question_sets/q-{org}.json", "{}")
    (root / "bizapi/internal/orgt").mkdir(parents=True, exist_ok=True)
    (root / "bizapi/internal/orgu").mkdir(parents=True, exist_ok=True)
    kinds = [f"({c}) 분류{c}" for c in "abcdefgh"] if variant else ["(a) 일반화"]
    rows = "\n".join(f"| {k} | `jikgam/engine/file{k}.py` | 바꾼 것 {k} | 이유 {k} | **{kinds[k % len(kinds)]}** |"
                     for k in range(1, p["changes"] + 1))
    _w(root / "docs/org-u.md", f"# 조직 유\n\n## 5. 엔진 쪽에서 바꾼 것\n\n| # | 파일 | 바꾼 것 | 이유 | 분류 |\n|---|---|---|---|---|\n{rows}\n\n## 6. 다음\n")
    _w(root / "config/decision_models.toml", '''[jev]
kind = "systemone"
url = "https://api.example.invalid/v1/systemone"
model = "jev-x"
price_input_per_mtok = 0.04

["gemini-flash"]
kind = "gemini"
model = "gem-x"
price_input_per_mtok = 0.7
price_output_per_mtok = 3.0

["local-t"]
kind = "systemone"
url = "http://127.0.0.1:65002/v1/systemone"
model = "local-x"
price_input_per_mtok = 0.0
''')
    _w(root / "config/voice.toml", '[tts]\nmodel = "tts-x"\nvoice = "Voice1"\nlanguage = "ko-KR"\n')
    # 통화 기록: 채팅(org_t), 음성(org_t), 다른 고객사(org_u)
    i = 0
    changed_left = p["changed"]
    for outcome, n in p["outcomes"].items():
        for _ in range(n):
            i += 1
            voice = outcome == "filed" and i == 1
            ch = outcome == "filed" and changed_left > 0
            if ch:
                changed_left -= 1
            run = "results/voice/vrun-C" if voice else "results/engine/scenarios/run-C"
            sess = f"s{i:02d}-r1-run"
            _jsonl(root / run / f"{sess}.jsonl", call_log(sess, outcome, i, p, "org_t", ch, voice))
    for k in range(p["feeds"]):
        _jsonl(root / f"results/voice/vrun-C/live{k}.jsonl", call_log(f"live{k}", "filed", 50 + k, p, "org_t", k == 0, True))
    _jsonl(root / "results/engine/scenarios/run-U/b01-r1.jsonl", call_log("b01-r1", "answered", 1, p, "org_u"))
    # 관리 콘솔: 판별 실행(기준선과 함께, 로컬 모델 혼자), 부하, 음성 미공개 보고서, 리플레이용 음성 로그와 시나리오
    decision_run(root, "20990101T000000", ["jev", "gemini-flash", "local-t"], p)
    decision_run(root, "20990101T010000", ["local-t"], p, alone=True)
    conc, calls, per_s = p["load"]
    _w(root / "results/load/20990101T020000/meta.json", json.dumps({"run_id": "20990101T020000", "backend": "jev",
                                                                     "concurrency": conc, "duration_s": calls / per_s}))
    _jsonl(root / "results/load/20990101T020000/raw.jsonl",
           [{"latency_ms": p["lat"] + k * 3.1, "error": None if k % 9 else "HTTP 500", "cost_usd": p["cost"]} for k in range(calls)])
    v = p["voice"]
    _w(root / "results/voice/voice-holdout-report.json", json.dumps({"policies": {"C": {
        "passed": v["passed"], "valid": v["valid"], "valid_runs": {"refresh": {"p95": v["p95"], "misses": 0},
                                                                   "stale": {"invalid": v["stale"], "denominator": v["den"]}}}},
        "verdict": [{"policy": "C", "ok": True}]}))
    _jsonl(root / "data/scenarios/org_a.jsonl", [{"id": "s20", "title": "가상 시나리오", "expected": {"articles": p["gold"]}}])
    _jsonl(root / "results/voice/rp-C/s20-r1-rp-C.jsonl", replay_log(p))
    # 관리 콘솔 음성 시험: 시험용 고객(모의 업무 API 자료 모양), 분당 비용을 낼 음성 기록 요약, 끝난 시험 통화 하나
    day = p["date"].date()
    _w(root / "domain/org_a/customers.json", json.dumps({"customers": [{
        "id": "C001", "name": "가상 고객 하나", "phone": "010-0000-0001", "status": "active",
        "subscriptions": [{"plan": "monthly", "price": p["price"], "next_billing": (day + timedelta(days=p["window"])).isoformat()}],
        "payments": [{"date": day.isoformat(), "amount": p["amount"], "method": "card"}]}]}, ensure_ascii=False))
    _w(root / "results/voice/voice-barge-C/summary.json", json.dumps({"sessions": [
        {"cost_usd": {"engine": p["cost"] * p["mult"], "stt_estimate": p["lat"] / 10000}, "stt_stream_min": 1.0}]}))
    _jsonl(root / "results/voice/live/20990101-000000/voice-test-20990101-000000.jsonl", replay_log(p))
    return root


def replay_log(p: dict[str, Any]) -> list[dict[str, Any]]:
    """관리 콘솔 리플레이용 음성 로그: 판단 셋, 끼어들기 한 번, 무효가 된 답 하나."""
    lg = Log("s20-r1-rp-C", p["date"], p["step"], "org_a", voice=True)
    cands = ["T-01", "T-02", "T-03", "T-04", "T-05", "T-06", "T-07"]
    out = []
    for d in range(3):
        lg.utt(f"말 {d}", ["billing_inquiry", "cancel", "cancel_refund"][d])
        lg.events[-1]["latency_ms"] = p["dec_lat"] + d
        # 정답 조항이 늘 위쪽에 오도록(0: 정답 3개가 모두 상위 4위 안, 1: 상위 4위가 모두 정답) 확률을 준다
        probs = {a: round((p["probs"][(k + d) % 2] if a in p["gold"] else p["probs"][2 + (k + d) % 2]) - 0.01 * k, 2)
                 for k, a in enumerate(cands)}
        lg.emit("evidence.updated", candidates=cands, applies=probs, articles=[a for a, x in probs.items() if x >= 0.5],
                sufficient=d > 0, action="answer", intent=["billing_inquiry", "cancel", "cancel_refund"][d])
        sid = lg.say(f"답 {d}", [cands[d]])
        out.append(sid)
        if d == 1:
            lg.emit("voice.barge_in", suggestion_id=sid, started=True, played_ms=round(p["step"] * 0.4, 1),
                    speech_start_ms=round(lg.t, 3))
        if d > 0:
            lg.emit("suggestion.invalidated", suggestion_id=out[d - 1], basis_version=lg.v - 1, invalidated_version=lg.v,
                    reasons=["condition_changed=true"])
    return lg.events
