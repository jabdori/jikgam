"""AI 고객 시나리오 실행기.

    uv run python -m jikgam.sim.runner --org org_a --scenarios s01,s20 --policy C --runs 1
    uv run python -m jikgam.sim.runner --org org_a --scenarios all --policy C --bizapi-url http://127.0.0.1:8082

정책(docs/evaluation.md §3.4)은 세션을 만드는 함수다. 세션은 엔진 Session과 같은 꼴이면 된다:
`factory(org, session_id, log_dir)` → `await handle(event) -> list[dict]`, `await aclose()`, 이벤트 로그는
`<log_dir>/<session_id>.jsonl`.
- B: 엔진 + LLM 판단(판별 백엔드 gemini-flash)
- C: 엔진 + 조직 기본 판별 모델(org_a는 jev), 확정 발화
- A: 개선형 RAG. jikgam.baselines.rag의 Session(org, session_id, log_dir)을 같은 꼴로 쓴다(별도 작업).
- D: 미확정 자막 선판단은 음성 런타임이 필요해 이 채팅 실행기에서는 돌리지 않는다.

시나리오마다:
1. 모의 업무 API를 초기화하고(/_admin/reset), 시나리오의 장애 주입(mid_call_events의 inject)을 시작 시점에 건다.
   주입은 도구의 동작에 대한 사건이라 고객 발화 시점과 무관하다(s19: 본인 확인 전에는 quote_refund를 부르지 않는다).
2. 첫 고객 메시지는 시나리오 opening 그대로, 이후는 AI 고객이 한 턴에 하나씩 쓴다. 사건을 실행한 메시지에는
   시나리오 파일의 표시(condition_change, invalidates_prior, event)를 marker로 붙인다(docs/engine.md §2).
3. 고객이 done을 내면 그 메시지까지 보내고 끝낸다. 엔진이 상담사에게 넘기거나(handoff) 최대 턴에 닿아도 끝낸다.
4. 고객이 엔진 응답을 받은 뒤에만 다음 메시지를 보낸다(엄격한 번갈아 말하기). 그래서 채팅에서는 응답 도중 새 발화가
   들어오는 경우가 없다.

결과: <out-dir>/<run_id>/<세션>.jsonl(엔진 이벤트), <세션>.sim.jsonl(시뮬레이터 기록), <세션>.judge.json(AI 판정),
summary.json. 보고서는 `uv run python -m jikgam.sim.report <run_dir>`.
"""

import argparse
import asyncio
import json
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

import httpx

from jikgam.decision.registry import load_config
from jikgam.engine import events as ev
from jikgam.engine.org import ROOT, OrgConfig, load_org

from . import customer as cust
from . import judge
from .gemini import GeminiJson
from .scenario import customer_events, file_sha256, injections, load_scenarios, marker, scenario_path

DEFAULT_MAX_TURNS = 16  # 고객 메시지 수 상한(opening 포함)
OUT_DIR = ROOT / "results/engine/scenarios"


class SessionLike(Protocol):
    async def handle(self, event: dict[str, Any]) -> list[dict[str, Any]]: ...

    async def aclose(self) -> None: ...


PolicyFactory = Callable[[OrgConfig, str, Path], SessionLike]


@dataclass(frozen=True)
class Policy:
    name: str
    description: str
    factory: PolicyFactory


def engine_policy(backend: str | None) -> PolicyFactory:
    def make(org: OrgConfig, session_id: str, log_dir: Path) -> SessionLike:
        from jikgam.engine.session import Session
        return Session(org, session_id, log_dir, backend)
    return make


def rag_policy(org: OrgConfig, session_id: str, log_dir: Path) -> SessionLike:
    try:
        from jikgam.baselines import rag
    except ImportError as e:
        raise SystemExit(f"정책 A는 jikgam.baselines.rag가 필요하다: {e}") from e
    return rag.Session(org, session_id, log_dir)


POLICIES = {
    "A": Policy("A", "개선형 RAG(jikgam.baselines.rag.Session)", rag_policy),
    "B": Policy("B", "엔진 + LLM 판단(판별 백엔드 gemini-flash)", engine_policy("gemini-flash")),
    "C": Policy("C", "엔진 + 판별 모델(조직 설정 기본 백엔드), 확정 발화", engine_policy(None)),
}


# --- 모의 업무 API 관리 ----------------------------------------------------------------------------


def _admin(url: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    """관리 엔드포인트에는 인증이 없으므로 로컬 주소에만 보낸다."""
    if urlsplit(url).hostname not in ("127.0.0.1", "localhost"):
        raise SystemExit(f"로컬 주소가 아니라 관리 요청을 보내지 않는다: {url}")
    r = httpx.post(f"{url}{path}", json=body, timeout=5)
    r.raise_for_status()
    return r.json()


def reset_bizapi(url: str) -> None:
    _admin(url, "/_admin/reset", {})


def inject_fault(url: str, inject: dict[str, Any]) -> dict[str, Any]:
    return _admin(url, "/_admin/faults", {"tool": inject["tool"], "code": inject["code"],
                                          "count": int(inject.get("count", 1))})


# --- 시뮬레이터 기록 -------------------------------------------------------------------------------


class SimLog:
    """시뮬레이터 자신의 기록(JSONL). 시각은 엔진과 같은 단조 시계(jikgam.engine.events.now_ms)다."""

    def __init__(self, path: Path, session: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.session = session
        self._f = path.open("a", encoding="utf-8", newline="\n")

    def emit(self, type_: str, **fields: Any) -> dict[str, Any]:
        t = ev.now_ms()
        row = {"t_ms": t, "wall": ev.wall_at(t), "session": self.session, "type": type_, **fields}
        self._f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        self._f.flush()
        return row

    def close(self) -> None:
        if not self._f.closed:
            self._f.close()


def handed_off(out: list[dict[str, Any]]) -> bool:
    return any((e["type"] == "suggestion.created" and e.get("action") == "handoff")
               or (e["type"] == "tool.result" and e.get("tool") == "create_handoff" and e.get("ok")) for e in out)


def engine_costs(log_path: Path) -> dict[str, Any]:
    events = ev.read_log(log_path) if log_path.exists() else []
    dec = [e for e in events if e["type"] == "decision.response"]
    gen = [e for e in events if e["type"] == "generation.response"]
    return {"decision_calls": len(dec), "decision_usd": sum(e.get("cost_usd") or 0 for e in dec),
            "generation_calls": len(gen), "generation_usd": sum(e.get("cost_usd") or 0 for e in gen),
            "engine_errors": sum(e["type"] == "engine.error" for e in events)}


async def run_scenario(scn: dict[str, Any], policy: Policy, org: OrgConfig, run_dir: Path, session_id: str,
                       customer: cust.Customer, max_turns: int = DEFAULT_MAX_TURNS) -> dict[str, Any]:
    """시나리오 하나를 한 번 돌린다. 세션 요약을 돌려준다."""
    reset_bizapi(org.bizapi_url)
    log = SimLog(run_dir / f"{session_id}.sim.jsonl", session_id)
    log.emit("sim.start", scenario=scn["id"], policy=policy.name, customer_model=customer.model,
             customer_prompt=customer.prompt_version, max_turns=max_turns, bizapi_url=org.bizapi_url)
    injected = []
    for idx, inj in injections(scn):
        resp = inject_fault(org.bizapi_url, inj)
        injected.append({"event": f"{scn['id']}#{idx}", **inj})
        log.emit("sim.inject", event=f"{scn['id']}#{idx}", applied="start", response=resp, **inj)
    t_start = ev.now_ms()
    session = policy.factory(org, session_id, run_dir)
    conversation: list[dict[str, str]] = []
    executed: set[int] = set()
    customer_usd = 0.0
    turn = 1
    end_reason, error = "runner_error", None
    say, event_idx, done = scn["opening"], None, False
    usage: dict[str, Any] = {"source": "opening"}
    rejected: dict[str, Any] | None = None
    try:
        while True:
            utt_id = f"u{turn}"
            event: dict[str, Any] = {"type": "utterance", "utt_id": utt_id, "speaker": "customer", "text": say,
                                     "final": True}
            mark = None
            if event_idx is not None:
                mark = marker(scn, event_idx)
                event["marker"] = mark
                executed.add(event_idx)
            log.emit("customer.turn", turn=turn, utt_id=utt_id, text=say, executed_event=event_idx, marker=mark,
                     done=done, llm=usage, rejected_event=rejected)
            conversation.append({"speaker": "customer", "text": say})
            sent = ev.now_ms()
            try:
                out = await session.handle(event)
            except Exception as e:  # 엔진 예외는 엔진 실패로 기록하고 세션을 끝낸다
                end_reason, error = "engine_exception", f"{type(e).__name__}: {e}"
                log.emit("engine.exception", utt_id=utt_id, error=error)
                break
            says = [e for e in out if e["type"] == "agent.say"]
            log.emit("engine.reply", utt_id=utt_id, sent_ms=sent, returned_ms=ev.now_ms(),
                     duration_ms=round(ev.now_ms() - sent, 1), says=[e["text"] for e in says],
                     actions=[e.get("action") for e in out if e["type"] == "suggestion.created"])
            conversation += [{"speaker": "agent", "text": e["text"]} for e in says]
            if done:
                end_reason = "customer_done"
                break
            if handed_off(out):
                end_reason = "handoff"
                break
            if turn >= max_turns:
                end_reason = "max_turns"
                break
            turn += 1
            ct = await customer.next_turn(conversation, turn, executed)
            customer_usd += ct.usage.get("cost_usd") or 0
            if ct.error:
                end_reason, error = "customer_error", ct.error
                log.emit("customer.error", turn=turn, error=ct.error, llm=ct.usage)
                turn -= 1
                break
            if not ct.say:
                if ct.done:
                    log.emit("customer.done", turn=turn, llm=ct.usage, rejected_event=ct.rejected_event)
                    end_reason = "customer_done"
                else:
                    end_reason, error = "customer_error", "빈 메시지"
                    log.emit("customer.error", turn=turn, error=error, llm=ct.usage)
                turn -= 1
                break
            say, event_idx, done, usage, rejected = ct.say, ct.executed_event, ct.done, ct.usage, ct.rejected_event
    finally:
        await session.aclose()
        missing = [i for i, _ in customer_events(scn) if i not in executed]
        log.emit("sim.end", reason=end_reason, error=error, customer_turns=turn, executed_events=sorted(executed),
                 missing_events=missing, customer_usd=customer_usd, duration_ms=round(ev.now_ms() - t_start, 1))
        log.close()
    return {"scenario": scn["id"], "session": session_id, "end_reason": end_reason, "error": error,
            "customer_turns": turn, "executed_events": sorted(executed), "missing_events": missing,
            "injections": injected, "duration_ms": round(ev.now_ms() - t_start, 1),
            "costs": {"customer_usd": customer_usd, **engine_costs(run_dir / f"{session_id}.jsonl")}}


# --- 실행 ---------------------------------------------------------------------------------------


def git_commit() -> dict[str, Any]:
    def git(*a: str) -> str:
        return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, check=False).stdout.strip()
    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}


def select(scenarios: list[dict[str, Any]], spec: str) -> list[dict[str, Any]]:
    if spec == "all":
        return scenarios
    want = [s.strip() for s in spec.split(",") if s.strip()]
    by_id = {s["id"]: s for s in scenarios}
    unknown = [w for w in want if w not in by_id]
    if unknown:
        raise SystemExit(f"없는 시나리오: {unknown}")
    return [by_id[w] for w in want]


def write_summary(run_dir: Path, summary: dict[str, Any]) -> None:
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")


async def run(args: argparse.Namespace) -> Path:
    if args.policy == "D":
        raise SystemExit("정책 D(미확정 자막 선판단)는 음성 런타임이 필요해 채팅 실행기에서 돌리지 않는다")
    policy = POLICIES[args.policy]
    org = load_org(args.org)
    if args.bizapi_url:
        org = replace(org, bizapi_url=args.bizapi_url.rstrip("/"))
    path = scenario_path(org.org)
    chosen = select(load_scenarios(path), args.scenarios)
    run_id = args.run_id or f"{datetime.now(ev.KST):%Y%m%dT%H%M%S}-{policy.name}"
    run_dir = args.out_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    models = load_config()
    backend = "gemini-flash" if policy.name == "B" else org.decision_backend if policy.name == "C" else None
    summary: dict[str, Any] = {
        "run_id": run_id, "policy": policy.name, "policy_description": policy.description,
        "decision_backend": backend, "decision_model": (models.get(backend) or {}).get("model") if backend else None,
        "generator": org.generator, "generator_model": models[org.generator]["model"], "org": org.org,
        "bizapi_url": org.bizapi_url, "scenario_file": str(path.relative_to(ROOT)).replace("\\", "/"),
        "scenario_sha256": file_sha256(path), "runs": args.runs, "max_turns": args.max_turns,
        "customer_model": models[args.customer_model]["model"], "customer_prompt": cust.PROMPT_VERSION,
        "judge_model": None if args.no_judge else models[args.judge_model]["model"],
        "judge_prompt": None if args.no_judge else judge.JUDGE_PROMPT_VERSION,
        "git": git_commit(), "started": datetime.now(ev.KST).isoformat(timespec="seconds"), "ended": None,
        "note": "개발용 시나리오 결과(docs/evaluation.md §4.3). 합성 대화이며 운영 성과가 아니다.",
        "sessions": [],
    }
    write_summary(run_dir, summary)
    judge_client = None if args.no_judge else GeminiJson.from_config(args.judge_model)
    try:
        for k in range(1, args.runs + 1):
            for scn in chosen:
                sid = f"{scn['id']}-r{k}-{run_id}"
                customer = cust.make_customer(scn, args.customer_model)
                try:
                    sess = await run_scenario(scn, policy, org, run_dir, sid, customer, args.max_turns)
                finally:
                    await customer.aclose()
                sess["run"] = k
                if judge_client is not None:
                    rec = await judge.judge_session(scn, run_dir, sid, judge_client)
                    sess["costs"]["judge_usd"] = rec["cost_usd"]
                    sess["judge_error"] = rec["error"]
                summary["sessions"].append(sess)
                write_summary(run_dir, summary)
                c = sess["costs"]
                print(f"{sid}: {sess['end_reason']}, 고객 {sess['customer_turns']}턴, 사건 {sess['executed_events']}"
                      f"(미실행 {sess['missing_events']}), 고객 ${c['customer_usd']:.4f} 판별 ${c['decision_usd']:.4f} "
                      f"생성 ${c['generation_usd']:.4f} 판정 ${c.get('judge_usd', 0):.4f}", flush=True)
    finally:
        if judge_client is not None:
            await judge_client.aclose()
        summary["ended"] = datetime.now(ev.KST).isoformat(timespec="seconds")
        write_summary(run_dir, summary)
    return run_dir


def main() -> None:
    ap = argparse.ArgumentParser(description="AI 고객 시나리오 실행기")
    ap.add_argument("--org", default="org_a")
    ap.add_argument("--scenarios", default="all", help="쉼표로 구분한 시나리오 ID 또는 all")
    ap.add_argument("--policy", required=True, choices=["A", "B", "C", "D"])
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--run-id")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--bizapi-url", help="모의 업무 API 주소(생략하면 조직 설정의 bizapi_url)")
    ap.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    ap.add_argument("--customer-model", default=cust.DEFAULT_MODEL)
    ap.add_argument("--judge-model", default=judge.DEFAULT_JUDGE_MODEL)
    ap.add_argument("--no-judge", action="store_true", help="AI 판정을 건너뛴다(나중에 jikgam.sim.judge로 한다)")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    run_dir = asyncio.run(run(args))
    print(f"실행 폴더: {run_dir}\n보고서: uv run python -m jikgam.sim.report {run_dir}")


if __name__ == "__main__":
    main()
