"""AI 고객 시나리오 실행기.

    uv run python -m jikgam.sim.runner --org org_a --scenarios s01,s20 --policy C --runs 1
    uv run python -m jikgam.sim.runner --org org_a --scenarios all --policy A --runs 3 --bizapi-url http://127.0.0.1:8082

정책(docs/evaluation.md §3.4)은 세션을 만드는 함수다. 세션은 엔진 Session과 같은 꼴이면 된다:
`factory(org, session_id, log_dir)` → `await start()`(있으면), `await handle(event) -> list[dict]`, `await aclose()`,
이벤트 로그는 `<log_dir>/<session_id>.jsonl`.
- A: 개선형 RAG 기준선(jikgam.baselines.rag.RagSession)
- B: 엔진 + LLM 판단(판별 백엔드 gemini-flash)
- C: 엔진 + 조직 기본 판별 모델(org_a는 jev), 확정 발화
- D: 미확정 자막 선판단은 음성 런타임이 필요해 이 채팅 실행기에서는 돌리지 않는다.

시나리오마다:
1. 모의 업무 API를 초기화하고(/_admin/reset), 시나리오의 장애 주입(mid_call_events의 inject)을 시작 시점에 건다.
   주입은 도구의 동작에 대한 사건이라 고객 발화 시점과 무관하다(s19: 본인 확인 전에는 quote_refund를 부르지 않는다).
2. 세션을 열고 start()(엔진의 판별 백엔드 예열)를 마친 뒤 첫 메시지를 보낸다.
3. 첫 고객 메시지는 시나리오 opening 그대로, 이후는 AI 고객이 쓴다. 사건을 실행한 메시지에는 시나리오 파일의
   표시(condition_change, invalidates_prior, event)를 marker로 붙인다(docs/engine.md §2).
4. 보통은 고객이 엔진 응답을 받은 뒤에 다음 메시지를 보낸다. condition_change 사건은 빠른 정정으로 보낸다
   (docs/evaluation.md §3.3 보완, 2026-10-14): 고객 AI가 직전 메시지와 사건 메시지를 한 번에 쓰고, 실행기는 직전
   메시지를 보낸 뒤 fast_delay_ms(1,000ms) 뒤에 엔진 응답을 기다리지 않고 사건 메시지를 보낸다. 두 handle()은
   동시에 도는 asyncio 작업이고, 그 사이 엔진이 내는 응답은 엔진이 노출로 기록한다. 두 응답이 모두 끝난 뒤 고객 AI가
   다음 메시지를 쓴다.
5. 고객이 done을 내면 그 메시지까지 보내고 끝낸다. 엔진이 상담사에게 넘기거나(handoff) 최대 메시지 수에 닿아도 끝낸다.

반복: --runs N이면 시나리오마다 N회 돈다. 회차 번호는 세션 ID(`<시나리오>-r<회차>-<run_id>`), 시뮬레이터 기록,
summary.json에 남긴다. 고객 AI의 seed는 (시나리오, 회차)에서 정해진다(기록용, 같은 출력을 보장하지 않는다).

결과: <out-dir>/<run_id>/<세션>.jsonl(엔진 이벤트), <세션>.sim.jsonl(시뮬레이터 기록), <세션>.judge.json(AI 판정),
summary.json. 보고서는 `uv run python -m jikgam.sim.report <run_dir>`.
"""

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass, field, replace
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
from .scenario import (customer_events, file_sha256, followup_events, injections, load_scenarios, marker,
                       registered_phones, scenario_path)

DEFAULT_MAX_TURNS = 16  # 고객 메시지 수 상한(opening 포함). 빠른 정정 두 메시지는 상한을 하나 넘을 수 있다
FAST_DELAY_MS = 1000.0  # 빠른 정정: 직전 메시지를 보낸 뒤 사건 메시지를 보낼 때까지(docs/evaluation.md §3.3 보완)
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
    from jikgam.baselines.rag import RagSession
    return RagSession(org, session_id, log_dir)


POLICIES = {
    "A": Policy("A", "개선형 RAG 기준선(jikgam.baselines.rag.RagSession)", rag_policy),
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
    """판별(예열 포함)·생성 호출 수와 비용. 정책 A는 판별이 없고 생성이 도구 루프 단계마다 남는다."""
    events = ev.read_log(log_path) if log_path.exists() else []
    dec = [e for e in events if e["type"] in (ev.DECISION_RESPONSE, ev.WARMUP, ev.DECISION_PREJUDGE)]
    gen = [e for e in events if e["type"] == ev.GENERATION_RESPONSE]
    return {"decision_calls": sum(e["type"] == ev.DECISION_RESPONSE for e in dec),
            "warmup_calls": sum(e["type"] == ev.WARMUP for e in dec),
            "prejudge_calls": sum(e["type"] == ev.DECISION_PREJUDGE for e in dec),
            "decision_usd": sum(e.get("cost_usd") or 0 for e in dec),
            "generation_calls": len(gen), "generation_usd": sum(e.get("cost_usd") or 0 for e in gen),
            "engine_errors": sum(e["type"] == ev.ENGINE_ERROR for e in events)}


def customer_seed(scenario_id: str, run: int) -> int:
    """(시나리오, 회차)에서 정하는 고객 AI seed(31비트)."""
    return int(hashlib.sha256(f"{scenario_id}|{run}".encode()).hexdigest()[:8], 16) & 0x7FFFFFFF


@dataclass
class Message:
    text: str
    event: int | None = None  # 이 메시지로 실행한 사건 번호


@dataclass
class _Run:
    """한 세션의 진행 상태."""

    conversation: list[tuple[float, int, dict[str, str]]] = field(default_factory=list)  # (시각, 순번, 메시지)
    executed: set[int] = field(default_factory=set)
    fast: list[dict[str, Any]] = field(default_factory=list)  # 빠른 정정 기록
    fast_missing: list[int] = field(default_factory=list)
    turn: int = 0

    def add(self, t: float, speaker: str, text: str) -> None:
        self.conversation.append((t, len(self.conversation), {"speaker": speaker, "text": text}))

    def ordered(self) -> list[dict[str, str]]:
        return [m for _, _, m in sorted(self.conversation)]


async def _send(scn: dict[str, Any], session: SessionLike, log: SimLog, st: _Run, msgs: list[Message], done: bool,
                info: dict[str, Any], fast_delay_ms: float) -> tuple[list[list[dict[str, Any]]], str | None]:
    """메시지 하나는 응답까지 기다리고, 둘이면 빠른 정정으로 보낸다. (handle 결과 목록, 엔진 예외)."""
    tasks: list[asyncio.Task] = []
    sent: list[float] = []
    for k, m in enumerate(msgs):
        # 간격은 엔진과 같은 시계(now_ms)로 잰다. asyncio의 시계는 Windows에서 해상도가 거칠어(약 15ms) 한 번의 sleep이
        # 기한보다 일찍 끝날 수 있으므로 기한에 닿을 때까지 다시 잔다.
        while k > 0 and (remain := sent[0] + fast_delay_ms - ev.now_ms()) > 0:
            await asyncio.sleep(remain / 1000)
        st.turn += 1
        utt_id = f"u{st.turn}"
        event: dict[str, Any] = {"type": "utterance", "utt_id": utt_id, "speaker": "customer", "text": m.text,
                                 "final": True}
        mark = None
        if m.event is not None:
            mark = marker(scn, m.event)
            event["marker"] = mark
            st.executed.add(m.event)
        t = ev.now_ms()
        sent.append(t)
        log.emit("customer.turn", turn=st.turn, utt_id=utt_id, text=m.text, executed_event=m.event, marker=mark,
                 done=done and k == len(msgs) - 1, fast_followup=k > 0, pair_size=len(msgs), **(info if k == 0 else {}))
        st.add(t, "customer", m.text)
        tasks.append(asyncio.create_task(session.handle(event)))
    if len(msgs) > 1:
        st.fast.append({"event": f"{scn['id']}#{msgs[-1].event}", "gap_ms": round(sent[1] - sent[0], 1),
                        "first_done_before_followup": tasks[0].done()})
    results = await asyncio.gather(*tasks, return_exceptions=True)
    outs: list[list[dict[str, Any]]] = []
    error = None
    for k, r in enumerate(results):
        if isinstance(r, BaseException):
            error = f"{type(r).__name__}: {r}"
            log.emit("engine.exception", utt_id=f"u{st.turn - len(msgs) + k + 1}", error=error)
            outs.append([])
            continue
        outs.append(r)
        says = [e for e in r if e["type"] == "agent.say"]
        log.emit("engine.reply", utt_id=f"u{st.turn - len(msgs) + k + 1}", returned_ms=ev.now_ms(),
                 says=[e["text"] for e in says], actions=[e.get("action") for e in r if e["type"] == "suggestion.created"],
                 cancelled=sum(e["type"] == "task.cancelled" for e in r),
                 discarded=sum(e["type"] == "result.discarded" for e in r))
        for e in says:
            st.add(float(e["t_ms"]), "agent", e["text"])
    return outs, error


async def run_scenario(scn: dict[str, Any], policy: Policy, org: OrgConfig, run_dir: Path, session_id: str,
                       customer: cust.Customer, max_turns: int = DEFAULT_MAX_TURNS, run: int = 1,
                       seed: int | None = None, fast_delay_ms: float = FAST_DELAY_MS) -> dict[str, Any]:
    """시나리오 하나를 한 번 돌린다. 세션 요약을 돌려준다."""
    reset_bizapi(org.bizapi_url)
    log = SimLog(run_dir / f"{session_id}.sim.jsonl", session_id)
    log.emit("sim.start", scenario=scn["id"], policy=policy.name, run=run, customer_model=customer.model,
             customer_prompt=customer.prompt_version, customer_seed=seed, max_turns=max_turns,
             fast_delay_ms=fast_delay_ms, followup_events=sorted(followup_events(scn)), bizapi_url=org.bizapi_url)
    injected = []
    for idx, inj in injections(scn):
        resp = inject_fault(org.bizapi_url, inj)
        injected.append({"event": f"{scn['id']}#{idx}", **inj})
        log.emit("sim.inject", event=f"{scn['id']}#{idx}", applied="start", response=resp, **inj)
    t_start = ev.now_ms()
    session = policy.factory(org, session_id, run_dir)
    st = _Run()
    customer_usd = 0.0
    end_reason, error = "runner_error", None
    msgs, done = [Message(scn["opening"])], False
    info: dict[str, Any] = {"llm": {"source": "opening"}}
    try:
        start = getattr(session, "start", None)
        if start is not None:
            await start()
        while True:
            outs, error = await _send(scn, session, log, st, msgs, done, info, fast_delay_ms)
            if error:
                end_reason = "engine_exception"
                break
            if done:
                end_reason = "customer_done"
                break
            if any(handed_off(o) for o in outs):
                end_reason = "handoff"
                break
            if st.turn >= max_turns:
                end_reason = "max_turns"
                break
            ct = await customer.next_turn(st.ordered(), st.turn + 1, st.executed)
            customer_usd += ct.usage.get("cost_usd") or 0
            if ct.error:
                end_reason, error = "customer_error", ct.error
                log.emit("customer.error", turn=st.turn + 1, error=ct.error, llm=ct.usage)
                break
            if ct.fast_missing is not None:
                st.fast_missing.append(ct.fast_missing)
            if not ct.say:
                if ct.done:
                    log.emit("customer.done", turn=st.turn + 1, llm=ct.usage, rejected_event=ct.rejected_event)
                    end_reason = "customer_done"
                else:
                    end_reason, error = "customer_error", "빈 메시지"
                    log.emit("customer.error", turn=st.turn + 1, error=error, llm=ct.usage)
                break
            if ct.followup_say:
                msgs = [Message(ct.say), Message(ct.followup_say, ct.executed_event)]
            else:
                msgs = [Message(ct.say, ct.executed_event)]
            done = ct.done
            info = {"llm": ct.usage, "rejected_event": ct.rejected_event, "notes": ct.notes}
    finally:
        await session.aclose()
        missing = [i for i, _ in customer_events(scn) if i not in st.executed]
        log.emit("sim.end", reason=end_reason, error=error, customer_turns=st.turn, executed_events=sorted(st.executed),
                 missing_events=missing, fast_corrections=st.fast, fast_missing=st.fast_missing,
                 customer_usd=customer_usd, duration_ms=round(ev.now_ms() - t_start, 1))
        log.close()
    return {"scenario": scn["id"], "session": session_id, "run": run, "customer_seed": seed, "end_reason": end_reason,
            "error": error, "customer_turns": st.turn, "executed_events": sorted(st.executed),
            "missing_events": missing, "fast_corrections": st.fast, "fast_missing": st.fast_missing,
            "injections": injected, "duration_ms": round(ev.now_ms() - t_start, 1),
            "costs": {"customer_usd": customer_usd, **engine_costs(run_dir / f"{session_id}.jsonl")}}


# --- 실행 ---------------------------------------------------------------------------------------


def git_commit() -> dict[str, Any]:
    """실행 시점 커밋과 작업 사본 변경 여부. results/는 빼고 본다(앞선 실행의 커밋 전 결과 폴더가 변경으로 잡히지 않게)."""
    def git(*a: str) -> str:
        return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, check=False).stdout.strip()
    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain", "--", ".", ":(exclude)results"))}


def select(scenarios: list[dict[str, Any]], spec: str) -> list[dict[str, Any]]:
    from jikgam.sim.scenario import normalize_selection
    try:
        want = normalize_selection(spec)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if want == "all":
        return scenarios
    by_id = {s["id"]: s for s in scenarios}
    unknown = [w for w in want if w not in by_id]
    if unknown:
        raise SystemExit(f"없는 시나리오: {unknown}")
    return [by_id[w] for w in want]


def write_summary(run_dir: Path, summary: dict[str, Any]) -> None:
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8",
                                          newline="\n")


def customers_file(org: OrgConfig) -> Path:
    """모의 업무 API 고객 데이터. 조직 약관 파일과 같은 폴더의 customers.json이다(domain/<org>/)."""
    return org.terms.parent / "customers.json"


async def run(args: argparse.Namespace) -> Path:
    from jikgam.v2.freeze import require_e2e_frozen
    from jikgam.sim.scenario import normalize_selection

    args.scenarios = normalize_selection(getattr(args, "scenarios", "all"))

    path = (ROOT / args.scenario_file) if args.scenario_file else scenario_path(args.org)
    contract = require_e2e_frozen(path, getattr(args, "frozen", None), ROOT, args)
    if contract is not None and not getattr(args, "bizapi_record", None):
        raise SystemExit("미공개 v2는 e2e_run이 빌드해 띄운 업무 API 기록이 필요합니다")
    if contract is None and getattr(args, "law", None):
        from jikgam.v2.e2e_contract import build_contract
        contract = build_contract(args, ROOT)
    bizapi_record = None
    if getattr(args, "bizapi_record", None):
        from jikgam.v2.e2e_run import verify_bizapi_record
        bizapi_record = verify_bizapi_record(args.bizapi_record, args, contract, ROOT)
    if args.policy == "D":
        raise SystemExit("정책 D(미확정 자막 선판단)는 음성 런타임이 필요해 채팅 실행기에서 돌리지 않는다")
    policy = POLICIES[args.policy]
    if args.decision_backend:
        if policy.name != "C":
            raise SystemExit("--decision-backend는 정책 C에만 쓴다(B는 gemini-flash로 정해져 있다)")
        policy = Policy("C", f"엔진 + 판별 모델({args.decision_backend}), 확정 발화", engine_policy(args.decision_backend))
    org = load_org(args.org)
    if args.bizapi_url:
        org = replace(org, bizapi_url=args.bizapi_url.rstrip("/"))
    if args.law:
        # v2: 법령·고시 묶음(config/v2/law.toml)을 켠 엔진. 정책 C에만 쓴다(docs/evaluation-v2.md §4.4)
        if policy.name != "C":
            raise SystemExit("--law는 정책 C에만 쓴다")
        from jikgam.engine.law import load_law

        org = replace(org, law=load_law(args.law))
    path = (ROOT / args.scenario_file) if args.scenario_file else scenario_path(org.org)
    chosen = select(load_scenarios(path), args.scenarios)
    # 보류용 평가는 보류용 고객 데이터도 모의 업무 API에 올리므로, 인증 문자 대상 번호도 그 파일까지 본다.
    customer_files = ([ROOT / c for c in contract["arguments"]["customers"]] if contract else
                      [ROOT / c for c in args.customers] if args.customers else [customers_file(org)])
    registered = frozenset().union(*(registered_phones(c) for c in customer_files))
    run_id = args.run_id or f"{datetime.now(ev.KST):%Y%m%dT%H%M%S}-{policy.name}"
    run_dir = args.out_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    models = load_config()
    backend = {"B": "gemini-flash", "C": args.decision_backend or org.decision_backend}.get(policy.name)
    summary: dict[str, Any] = {
        "run_id": run_id, "policy": policy.name, "policy_description": policy.description, "law": args.law,
        "decision_backend": backend, "decision_model": (models.get(backend) or {}).get("model") if backend else None,
        "generator": org.generator, "generator_model": models[org.generator]["model"], "org": org.org,
        "bizapi_url": org.bizapi_url, "scenario_file": str(path.relative_to(ROOT)).replace("\\", "/"),
        "scenario_sha256": file_sha256(path), "runs": args.runs, "run_start": args.run_start, "max_turns": args.max_turns,
        "scenarios": args.scenarios,
        "fast_delay_ms": args.fast_delay_ms,
        "customer_model": models[args.customer_model]["model"], "customer_prompt": cust.PROMPT_VERSION,
        "judge_model": None if args.no_judge else models[args.judge_model]["model"],
        "judge_prompt": None if args.no_judge else judge.JUDGE_PROMPT_VERSION,
        "git": git_commit(), "started": datetime.now(ev.KST).isoformat(timespec="seconds"), "ended": None,
        **({"frozen_adr": args.frozen,
            "frozen_sha256": file_sha256(ROOT / f"config/v2/freeze-{args.frozen}.json")}
           if getattr(args, "frozen", None) else {}),
        "note": ("보류용" if "holdout" in path.name else "개발용") + " 시나리오 결과(docs/evaluation.md §4.3). 합성 대화이며 운영 성과가 아니다.",
        "customer_files": [str(c.relative_to(ROOT)).replace("\\", "/") for c in customer_files],
        "sessions": [],
        **({"contract": contract} if contract is not None else {}),
        **({"frozen_verification": args.frozen_verification} if getattr(args, "frozen_verification", None) else {}),
        **({"bizapi_record": bizapi_record} if bizapi_record is not None else {}),
    }
    write_summary(run_dir, summary)
    judge_client = None if args.no_judge else GeminiJson.from_config(args.judge_model, retry_delays_s=())
    try:
        for k in range(args.run_start, args.run_start + args.runs):
            for scn in chosen:
                sid = f"{scn['id']}-r{k}-{run_id}"
                seed = customer_seed(scn["id"], k)
                customer = cust.make_customer(scn, registered, args.customer_model, seed)
                try:
                    sess = await run_scenario(scn, policy, org, run_dir, sid, customer, args.max_turns, k, seed,
                                              args.fast_delay_ms)
                finally:
                    await customer.aclose()
                if judge_client is not None:
                    rec = await judge.judge_session(scn, run_dir, sid, judge_client)
                    sess["costs"]["judge_usd"] = rec["cost_usd"]
                    sess["judge_error"] = rec["error"]
                summary["sessions"].append(sess)
                write_summary(run_dir, summary)
                c = sess["costs"]
                fast = ", ".join(f"{f['event']} {f['gap_ms']:.0f}ms" for f in sess["fast_corrections"])
                print(f"{sid}: {sess['end_reason']}, 고객 {sess['customer_turns']}턴, 사건 {sess['executed_events']}"
                      f"(미실행 {sess['missing_events']}), 빠른 정정 [{fast}]"
                      + (f" 미적용 {sess['fast_missing']}" if sess["fast_missing"] else "")
                      + f", 고객 ${c['customer_usd']:.4f} 판별 ${c['decision_usd']:.4f} 생성 ${c['generation_usd']:.4f}"
                        f" 판정 ${c.get('judge_usd', 0):.4f}", flush=True)
    finally:
        if judge_client is not None:
            await judge_client.aclose()
        summary["ended"] = datetime.now(ev.KST).isoformat(timespec="seconds")
        write_summary(run_dir, summary)
    return run_dir


def argument_parser():
    ap = argparse.ArgumentParser(description="AI 고객 시나리오 실행기")
    ap.add_argument("--org", default="org_a")
    ap.add_argument("--scenarios", default="all", help="쉼표로 구분한 시나리오 ID 또는 all")
    ap.add_argument("--policy", required=True, choices=["A", "B", "C", "D"])
    ap.add_argument("--runs", type=int, default=1, help="시나리오별 반복 횟수(docs/evaluation.md §3.4 보완: 3)")
    ap.add_argument("--scenario-file", help="시나리오 파일(저장소 기준 경로). 생략하면 data/scenarios/<org>.jsonl")
    ap.add_argument("--customers", action="append",
                    help="인증 문자를 줄 등록 번호를 읽을 고객 데이터(저장소 기준 경로, 여러 번 줄 수 있다). 생략하면 domain/<org>/customers.json")
    ap.add_argument("--bizapi-data", action="append", help="모의 업무 API에 순서대로 병합할 데이터 파일. 인증 대상 --customers와 별개")
    ap.add_argument("--run-start", type=int, default=1,
                    help="첫 회차 번호. 회차를 나눠 실행할 때 고객 AI seed가 회차마다 달라지게 한다(예: --runs 1 --run-start 2)")
    ap.add_argument("--run-id")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--bizapi-url", help="모의 업무 API 주소(생략하면 조직 설정의 bizapi_url이나 JIKGAM_BIZAPI_URL)")
    ap.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    ap.add_argument("--fast-delay-ms", type=float, default=FAST_DELAY_MS,
                    help="빠른 정정 간격(ms). docs/evaluation.md §3.3 보완의 값은 1,000이다")
    ap.add_argument("--customer-model", default=cust.DEFAULT_MODEL)
    ap.add_argument("--judge-model", default=judge.DEFAULT_JUDGE_MODEL)
    ap.add_argument("--no-judge", action="store_true", help="AI 판정을 건너뛴다(나중에 jikgam.sim.judge로 한다)")
    ap.add_argument("--decision-backend", help="정책 C의 판별 백엔드(config/decision_models의 이름). 생략하면 조직 설정 값")
    ap.add_argument("--law", choices=["r0", "r4", "r5"], help="v2 법령·고시 묶음(config/v2/law.toml의 프로필)")
    ap.add_argument("--frozen", help="미공개 v2 실행에 필요한 설정 고정 ADR 번호와 해시 목록")
    ap.add_argument("--bizapi-record", type=Path, help=argparse.SUPPRESS)
    return ap


def main() -> None:
    ap = argument_parser()
    args = ap.parse_args()
    if args.runs < 1:
        raise SystemExit("--runs는 1 이상이어야 한다")
    sys.stdout.reconfigure(encoding="utf-8")
    run_dir = asyncio.run(run(args))
    print(f"실행 폴더: {run_dir}\n보고서: uv run python -m jikgam.sim.report {run_dir}")


if __name__ == "__main__":
    main()
