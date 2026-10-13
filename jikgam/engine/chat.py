"""터미널 채팅과 고정 대화 재생.

    uv run python -m jikgam.engine.chat --org org_a
    uv run python -m jikgam.engine.chat --org org_a --replay data/replays/s01.jsonl --reset-bizapi

재생 파일은 한 줄에 고객 발화 하나다: {"text": "...", "marker": {...}}. marker는 조건 변경 표시(docs/engine.md §2)이며
엔진은 읽지 않고 로그에만 남긴다. 재생이 끝나면 이벤트 로그만으로 대화, 도구 호출, 제안 생성·무효화, 턴별 단계 지연,
호출 비용을 출력한다. 로그는 --log-dir/<run_id>/<세션 ID>.jsonl에 남는다.

업무 API(bizapi)는 따로 띄워 둔다: cd bizapi && go run ./cmd/bizapi
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
from .org import ROOT, load_org
from .session import Session


def reset_bizapi(url: str) -> None:
    """모의 업무 API의 세션·접수 내역을 비운다. 관리 엔드포인트에 인증이 없으므로 로컬 주소에만 보낸다."""
    if urlsplit(url).hostname not in ("127.0.0.1", "localhost"):
        raise SystemExit(f"로컬 주소가 아니라 초기화하지 않는다: {url}")
    httpx.post(f"{url}/_admin/reset", timeout=5).raise_for_status()


def _fmt_args(args: dict) -> str:
    keep = ("customer_id", "subscription_id", "payment_id", "reason", "phone", "from", "to", "bank", "holder")
    return ", ".join(f"{k}={args[k]}" for k in keep if k in args)


def report(log: list[dict]) -> str:
    """이벤트 로그로 재생 결과를 글로 만든다."""
    lines = ["대화:"]
    for e in log:
        if e["type"] == ev.UTTERANCE_END and e.get("speaker") == "customer":
            mark = "  [조건 변경 표시]" if (e.get("marker") or {}).get("condition_change") else ""
            lines.append(f"  고객({e['utt_id']})> {e['text']}{mark}")
        elif e["type"] == ev.AGENT_SAY:
            lines.append(f"  상담({e['suggestion_id'].rsplit('-', 1)[-1]})> {e['text']}")
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
    dcost = sum(e.get("cost_usd") or 0 for e in log if e["type"] == ev.DECISION_RESPONSE)
    gcost = sum(e.get("cost_usd") or 0 for e in log if e["type"] == ev.GENERATION_RESPONSE)
    ndec = sum(e["type"] == ev.DECISION_RESPONSE for e in log)
    ngen = sum(e["type"] == ev.GENERATION_RESPONSE for e in log)
    lines.append(f"호출: 판별 {ndec}회 ${dcost:.5f}, 생성 {ngen}회 ${gcost:.5f}")
    errors = [e for e in log if e["type"] == ev.ENGINE_ERROR]
    if errors:
        lines.append(f"엔진 오류 {len(errors)}건: " + "; ".join(e["error"] for e in errors))
    return "\n".join(lines)


async def replay(session: Session, path: Path) -> None:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for i, row in enumerate(rows, 1):
        event = {"type": "utterance", "utt_id": f"u{i}", "speaker": "customer", "text": row["text"], "final": True}
        if row.get("marker"):
            event["marker"] = row["marker"]
        await session.handle(event)


async def interactive(session: Session) -> None:
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


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", default="org_a")
    ap.add_argument("--backend", help="판별 백엔드(config/decision_models.toml 이름). 생략하면 조직 설정을 따른다")
    ap.add_argument("--replay", nargs="+", type=Path, help="고정 대화 파일(JSONL). 여러 개면 파일마다 세션 하나")
    ap.add_argument("--log-dir", type=Path, default=ROOT / "results/engine/dev")
    ap.add_argument("--run-id", default=datetime.now(ev.KST).strftime("%Y%m%dT%H%M%S"))
    ap.add_argument("--reset-bizapi", action="store_true", help="재생 전마다 로컬 모의 업무 API를 초기화한다")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    org = load_org(args.org)
    out_dir = args.log_dir / args.run_id
    for path in args.replay or [None]:
        if args.reset_bizapi:
            reset_bizapi(org.bizapi_url)
        sid = f"{path.stem if path else 'chat'}-{args.run_id}"
        session = Session(org, sid, out_dir, args.backend)
        try:
            if path:
                await replay(session, path)
            else:
                await interactive(session)
        finally:
            await session.aclose()
        if path:
            print(f"=== {path.name} (세션 {sid}, 판별 {session.backend})")
            print(report(ev.read_log(out_dir / f"{sid}.jsonl")))
            print()
    print(f"이벤트 로그: {out_dir}")


if __name__ == "__main__":
    asyncio.run(main())
