"""터미널 채팅과 고정 대화 재생.

    uv run python -m jikgam.engine.chat --org org_a
    uv run python -m jikgam.engine.chat --org org_a --replay data/replays/s01.jsonl --reset-bizapi
    uv run python -m jikgam.engine.chat --org org_a --policy A --replay data/replays/s01.jsonl --reset-bizapi

--policy는 docs/evaluation.md §3.4의 실행 정책이다. C(기본): 엔진 + 조직 설정의 판별 백엔드. B: 엔진 + gemini-flash 판단.
A: 개선형 RAG 기준선(jikgam/baselines/rag.py). --backend로 판별 백엔드를 직접 고르면 정책 이름 대신 그 값을 쓴다(A에는
쓸 수 없다).

재생 파일은 한 줄에 고객 발화 하나다: {"text": "...", "marker": {...}, "faults": [...]}.
- marker는 조건 변경 표시(docs/engine.md §2)이며 엔진은 읽지 않고 로그에만 남긴다.
- faults(선택)는 그 줄을 보내기 직전에 모의 업무 API에 넣을 장애 목록이다. 항목은 bizapi/README.md의 /_admin/faults
  본문과 같다: {"tool": "quote_refund", "code": "TIMEOUT", "count": 20}. 로컬 주소에만 보내고, 넣은 내용은 세션 로그에
  fault.injected로 남긴다. 시나리오의 "[주입]" 이벤트(s19)를 재생하려는 것이다.
재생이 끝나면 이벤트 로그만으로 대화, 도구 호출, 제안 생성·무효화, 턴별 단계 지연, 호출 비용을 출력한다.
로그는 --log-dir/<run_id>/<세션 ID>.jsonl에 남는다. 세션을 연 직후 start()로 판별 백엔드를 예열한다(warmup 이벤트).

업무 API(bizapi)는 따로 띄워 둔다: cd bizapi && go run ./cmd/bizapi. 주소는 조직 설정의 bizapi_url이고,
환경 변수 JIKGAM_BIZAPI_URL로 바꿀 수 있다.
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import events as ev
from .org import ROOT, OrgConfig, load_org
from .session import Session

POLICY_BACKENDS = {"B": "gemini-flash"}  # 정책 B는 엔진 구조 그대로 판별만 gemini-flash로 묻는다


def _local(url: str) -> None:
    """관리 엔드포인트에 인증이 없으므로 로컬 주소에만 보낸다."""
    if urlsplit(url).hostname not in ("127.0.0.1", "localhost"):
        raise SystemExit(f"로컬 주소가 아니라 관리 요청을 보내지 않는다: {url}")


def reset_bizapi(url: str) -> None:
    """모의 업무 API의 세션·접수 내역·장애 주입을 비운다."""
    _local(url)
    httpx.post(f"{url}/_admin/reset", timeout=5).raise_for_status()


def inject_faults(url: str, faults: list[dict]) -> None:
    _local(url)
    for f in faults:
        if set(f) - {"tool", "code", "count"} or "tool" not in f or "code" not in f:
            raise ValueError(f"faults 항목은 tool, code, count만 쓴다: {f}")
        httpx.post(f"{url}/_admin/faults", json=f, timeout=5).raise_for_status()


def _fmt_args(args: dict) -> str:
    keep = ("customer_id", "subscription_id", "payment_id", "reason", "phone", "from", "to", "bank", "holder")
    return ", ".join(f"{k}={args[k]}" for k in keep if k in args)


def report(log: list[dict]) -> str:
    """이벤트 로그로 재생 결과를 글로 만든다. 엔진(정책 B·C)과 RAG 기준선(정책 A) 로그 모두에 쓴다."""
    lines = ["대화:"]
    for e in log:
        if e["type"] == ev.UTTERANCE_END and e.get("speaker") == "customer":
            mark = "  [조건 변경 표시]" if (e.get("marker") or {}).get("condition_change") else ""
            lines.append(f"  고객({e['utt_id']})> {e['text']}{mark}")
        elif e["type"] == ev.AGENT_SAY:
            lines.append(f"  상담({e['suggestion_id'].rsplit('-', 1)[-1]})> {e['text']}")
        elif e["type"] == ev.FAULT_INJECTED:
            lines.append(f"  [장애 주입] {e['faults']}")
    lines.append("도구 호출:")
    turn = None
    calls = {e["call_id"]: e for e in log if e["type"] == ev.TOOL_CALL}
    for e in log:
        if e["type"] == ev.UTTERANCE_END and e.get("speaker") == "customer":
            turn = e["utt_id"]
        elif e["type"] == ev.TOOL_RESULT:
            res = "ok" if e["ok"] else e["error_code"]
            call = calls[e["call_id"]]
            lines.append(f"  {turn} {e['tool']}({_fmt_args(call['args'])}) → {res} "
                         f"[{e['latency_ms']:.0f} ms, 시도 {e['attempts']}]")
    lines.append("제안:")
    for e in log:
        if e["type"] == ev.SUGGESTION_CREATED:
            prop = f" 접수안={e['proposal']['writes']}" if e.get("proposal") else ""
            amt = f" 금액={e['amount']}(확정={e['amount_final']})" if e.get("amount") is not None else ""
            lines.append(f"  생성 {e['suggestion_id']} v{e['basis_version']} {e['rule']}/{e['action']} "
                         f"intent={e['intent']} 근거={e['cited_articles']}{amt}{prop}")
        elif e["type"] == ev.SUGGESTION_INVALIDATED:
            lines.append(f"  무효 {e['suggestion_id']} (기준 v{e['basis_version']} → v{e['invalidated_version']}) "
                         f"{e['reasons']}")
    lines.append("턴별 지연(ms, utterance.end 기준):")
    lines.append("  발화  표시  근거갱신  응답   검색   판별(회별)                 도구(개수,합)  생성(회별)   폐기/취소")
    for t in ev.turn_latencies(log):
        mark = "변경" if (t["marker"] or {}).get("condition_change") else "    "
        tools = f"{len(t['tool_ms'])},{sum(ms for _, ms in t['tool_ms']):.0f}"
        dec = ",".join(f"{x:.0f}" for x in t["decision_ms"])
        gen = ",".join(f"{x:.0f}" for x in t["generation_ms"])
        lines.append(f"  {t['utt_id']:4s}  {mark}  {t['evidence_ms'] if t['evidence_ms'] is not None else '미갱신':>8}  "
                     f"{t['say_ms'] if t['say_ms'] is not None else '-':>6}  {t['retrieval_ms']:5.1f}  {dec:26s} "
                     f"{tools:>12s}  {gen:12s} {t['discarded']}/{t['cancelled']}")
    warm = [e for e in log if e["type"] == ev.WARMUP]
    if warm:
        w = warm[0]
        lines.append(f"예열: {w['backend']} {w['latency_ms']:.0f} ms, ${w.get('cost_usd') or 0:.6f}"
                     + (f", 오류 {w['error']}" if w.get("error") else ""))
    dcost = sum(e.get("cost_usd") or 0 for e in log if e["type"] in (ev.DECISION_RESPONSE, ev.WARMUP))
    gcost = sum(e.get("cost_usd") or 0 for e in log if e["type"] == ev.GENERATION_RESPONSE)
    ndec = sum(e["type"] == ev.DECISION_RESPONSE for e in log)
    ngen = sum(e["type"] == ev.GENERATION_RESPONSE for e in log)
    lines.append(f"호출: 판별 {ndec}회(+예열 {len(warm)}회) ${dcost:.5f}, 생성 {ngen}회 ${gcost:.5f}")
    errors = [e for e in log if e["type"] == ev.ENGINE_ERROR]
    if errors:
        lines.append(f"엔진 오류 {len(errors)}건: " + "; ".join(e["error"] for e in errors))
    return "\n".join(lines)


def session_cost(log: list[dict]) -> float:
    """판별(예열, 정책 D 선판단 포함)과 생성 호출 비용 합(USD)."""
    return sum(e.get("cost_usd") or 0 for e in log
               if e["type"] in (ev.DECISION_RESPONSE, ev.WARMUP, ev.DECISION_PREJUDGE, ev.GENERATION_RESPONSE))


async def replay(session, path: Path, bizapi_url: str | None = None) -> None:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for i, row in enumerate(rows, 1):
        if row.get("faults"):
            if bizapi_url is None:
                raise ValueError(f"{path.name} {i}번째 줄: faults를 넣을 업무 API 주소가 없다")
            inject_faults(bizapi_url, row["faults"])
            session.log.emit(ev.FAULT_INJECTED, session.version, before_utt=f"u{i}", faults=row["faults"])
        event = {"type": "utterance", "utt_id": f"u{i}", "speaker": "customer", "text": row["text"], "final": True}
        if row.get("marker"):
            event["marker"] = row["marker"]
        await session.handle(event)


async def interactive(session) -> None:
    print("빈 줄이나 /q로 끝낸다.")
    n = 0
    while True:
        try:
            text = (await asyncio.to_thread(input, "고객> ")).strip()
        except EOFError:
            break
        if not text or text == "/q":
            break
        n += 1
        out = await session.handle({"type": "utterance", "utt_id": f"u{n}", "speaker": "customer", "text": text,
                                    "final": True})
        for e in out:
            if e["type"] == ev.AGENT_SAY:
                print(f"상담> {e['text']}")


def make_session(policy: str, backend: str | None, org: OrgConfig, sid: str, out_dir: Path):
    """실행 정책에 맞는 세션. A는 RAG 기준선, B·C는 같은 엔진에 판별 백엔드만 다르다."""
    if policy == "A":
        if backend:
            raise SystemExit("--policy A(RAG)에는 판별 백엔드가 없다. --backend를 빼라")
        from jikgam.baselines.rag import RagSession
        return RagSession(org, sid, out_dir)
    return Session(org, sid, out_dir, backend or POLICY_BACKENDS.get(policy))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", default="org_a")
    ap.add_argument("--policy", choices=("A", "B", "C"), default="C",
                    help="실행 정책(docs/evaluation.md §3.4). A=개선형 RAG, B=엔진+gemini-flash 판단, C=엔진+조직 설정 판별")
    ap.add_argument("--backend", help="판별 백엔드(config/decision_models.toml 이름). 생략하면 정책과 조직 설정을 따른다")
    ap.add_argument("--replay", nargs="+", type=Path, help="고정 대화 파일(JSONL). 여러 개면 파일마다 세션 하나")
    ap.add_argument("--log-dir", type=Path, default=ROOT / "results/engine/dev")
    ap.add_argument("--run-id", default=datetime.now(ev.KST).strftime("%Y%m%dT%H%M%S"))
    ap.add_argument("--reset-bizapi", action="store_true", help="재생 전마다 로컬 모의 업무 API를 초기화한다")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    org = load_org(args.org)
    out_dir = args.log_dir / args.run_id
    total = 0.0
    for path in args.replay or [None]:
        if args.reset_bizapi:
            reset_bizapi(org.bizapi_url)
        sid = f"{path.stem if path else 'chat'}-{args.policy}-{args.run_id}"
        session = make_session(args.policy, args.backend, org, sid, out_dir)
        try:
            await session.start()
            if path:
                await replay(session, path, org.bizapi_url)
            else:
                await interactive(session)
        finally:
            await session.aclose()
        log = ev.read_log(out_dir / f"{sid}.jsonl")
        total += session_cost(log)
        if path:
            engine = "RAG" if args.policy == "A" else f"판별 {session.backend}"
            print(f"=== {path.name} (세션 {sid}, 정책 {args.policy}, {engine})")
            print(report(log))
            print()
    print(f"이벤트 로그: {out_dir}")
    print(f"호출 비용 합: ${total:.5f}")


if __name__ == "__main__":
    asyncio.run(main())
