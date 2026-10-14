"""판별 데이터 분할 하나에 여러 백엔드를 돌려 원본 로그를 남긴다. 표는 report.py가 이 로그로만 만든다.

    uv run python -m jikgam.eval.run_decisions --split val --backends jev,clef,clef-flash,gemini-flash,gemini-pro,rules,majority

측정 절차(docs/evaluation.md §3):
- 백엔드마다 측정 데이터가 아닌 고정 요청으로 한 번 예열하고, 그 호출은 기록하지 않는다.
- 반복 회차 → 사례 → 백엔드 순으로 번갈아 호출한다. 네트워크 변동이 특정 백엔드에 몰리지 않게 하기 위해서다.
- 동시 실행 1. 한 연결을 유지한다(keep-alive).
- 429·5xx·연결 오류는 2·4·8초 뒤 최대 3번 다시 보내고, 지연은 마지막 시도만 기록한다.
"""

import argparse
import asyncio
import hashlib
import json
import platform
import socket
import statistics
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from importlib import metadata
from urllib.parse import urlsplit

import httpx

from jikgam.decision import llm
from jikgam.decision.registry import ROOT, load_config, make_client
from jikgam.decision.types import DecisionClient, DecisionResult
from jikgam.eval.questions import load_bank, render

KST = timezone(timedelta(hours=9))
RETRYABLE = ("HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504", "HTTP 529", "ConnectError", "ReadTimeout",
             "ConnectTimeout", "RemoteProtocolError", "ReadError")
WARMUP_STATE = {"conversation": [{"speaker": "customer", "text": "예열용 요청입니다.", "final": True}]}
WARMUP_QUESTIONS = {"warm": {"type": "noul", "instructions": "고객이 인사를 했는가?"}}
GEMINI_HOST = "generativelanguage.googleapis.com"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def tcp_connect_ms(host: str, n: int = 5) -> float:
    """TLS 없이 TCP 연결 시간의 중앙값. 네트워크 조건 기록용. host는 "이름"(443 포트) 또는 "이름:포트"."""
    name, _, port = host.partition(":")
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        with socket.create_connection((name, int(port or 443)), timeout=5):
            samples.append((time.perf_counter() - t0) * 1000)
    return round(statistics.median(samples), 1)


def host_key(url: str) -> str:
    """원격 API는 기존 기록과 같게 호스트 이름만, 443이 아닌 포트(로컬 서빙)는 "호스트:포트"."""
    u = urlsplit(url)
    port = u.port or (443 if u.scheme == "https" else 80)
    return u.hostname if port == 443 else f"{u.hostname}:{port}"


def network_conditions(hosts: list[str]) -> dict:
    trace = httpx.get("https://www.cloudflare.com/cdn-cgi/trace", timeout=10).text
    kv = dict(line.split("=", 1) for line in trace.strip().splitlines())
    return {"cloudflare_colo": kv.get("colo"), "cloudflare_loc": kv.get("loc"),
            "tcp_connect_ms_median": {h: tcp_connect_ms(h) for h in hosts}}


async def call(client: DecisionClient, state: dict, questions: dict) -> tuple[DecisionResult, int]:
    for attempt in range(1, 5):
        res = await client.decide(state, questions)
        if not (res.error and res.error.startswith(RETRYABLE)) or attempt == 4:
            return res, attempt
        await asyncio.sleep(2 ** attempt)
    raise AssertionError


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="val", choices=["train", "val", "test"])
    ap.add_argument("--backends", required=True)
    ap.add_argument("--reps", type=int, default=5, help="판별 모델·로컬 기준선 반복 횟수")
    ap.add_argument("--llm-reps", type=int, default=3, help="생성형 LLM 반복 횟수")
    ap.add_argument("--limit", type=int, default=0, help="앞 N건만(점검용)")
    ap.add_argument("--out", default="results/decisions")
    ap.add_argument("--frozen", help="test 평가 때 설정을 고정한 ADR 경로. meta.json에 경로와 해시를 남긴다")
    ap.add_argument("--questions", default="data/questions.json",
                    help="질문 세트 파일. 빌드 파일의 질문 ID를 이 세트의 문구로 다시 만든다(문구 실험용)")
    args = ap.parse_args()

    if args.split == "test":
        # docs/evaluation.md §8: 설정을 ADR로 고정한 뒤 한 번만 평가한다. 고정 ADR이 없으면 돌리지 않는다.
        if not args.frozen or not (ROOT / args.frozen).is_file() or "/adr/" not in args.frozen.replace("\\", "/"):
            sys.exit("test 분할은 설정 고정 ADR(--frozen docs/adr/<번호>.md)을 줄 때만 평가한다(docs/evaluation.md §8).")

    cfg_all = load_config()
    names = args.backends.split(",")
    data_path = ROOT / f"data/build/{args.split}.jsonl"
    rows = [json.loads(line) for line in data_path.read_text(encoding="utf-8").splitlines()]
    if args.limit:
        rows = rows[: args.limit]
    q_version, bank = load_bank(ROOT / args.questions)
    reps = {n: args.llm_reps if cfg_all[n]["kind"] == "gemini" else args.reps for n in names}

    started = datetime.now(KST)
    run_id = started.strftime("%Y%m%dT%H%M%S")
    out_dir = ROOT / args.out / args.split / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    hosts = sorted({host_key(cfg_all[n]["url"]) for n in names if cfg_all[n]["kind"] == "systemone"} |
                   {GEMINI_HOST for n in names if cfg_all[n]["kind"] == "gemini"})
    meta = {
        "run_id": run_id, "split": args.split, "started_kst": started.isoformat(timespec="seconds"),
        "git_commit": git("rev-parse", "HEAD"), "git_dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
        "data_file": str(data_path.relative_to(ROOT)).replace("\\", "/"),
        "data_sha256_16": hashlib.sha256(data_path.read_bytes()).hexdigest()[:16], "cases": len(rows),
        "questions_version": q_version, "questions_file": args.questions,
        "questions_file_sha256_16": hashlib.sha256((ROOT / args.questions).read_bytes()).hexdigest()[:16],
        "llm_prompt_version": llm.PROMPT_VERSION,
        "frozen_adr": args.frozen, "frozen_adr_sha256_16": (hashlib.sha256((ROOT / args.frozen).read_bytes()).hexdigest()[:16]
                                                            if args.frozen else None),
        "backends": {n: {k: v for k, v in cfg_all[n].items() if k != "api_key_env"} for n in names},
        "reps": reps, "concurrency": 1,
        "client": {"python": platform.python_version(), "httpx": metadata.version("httpx"),
                   "os": f"{platform.system()} {platform.release()} {platform.version()}"},
        "network_before": network_conditions(hosts),
    }

    clients = {n: make_client(n) for n in names}
    for n, c in clients.items():
        if cfg_all[n]["kind"] in ("rules", "majority"):  # 연결이 없는 로컬 기준선은 예열하지 않는다
            continue
        res, _ = await call(c, WARMUP_STATE, WARMUP_QUESTIONS)
        print(f"예열 {n}: {res.latency_ms:.0f} ms {res.error or ''}", flush=True)

    raw_path = out_dir / "raw.jsonl"
    with raw_path.open("w", encoding="utf-8", newline="\n") as f:
        for rep in range(max(reps.values())):
            for row in rows:
                state = json.loads(row["state"])
                questions = render(list(json.loads(row["questions"])), bank)
                for n in names:
                    if rep >= reps[n]:
                        continue
                    t_start = datetime.now(KST).isoformat(timespec="milliseconds")
                    res, attempts = await call(clients[n], state, questions)
                    rec = {"rep": rep, "case": row["id"], "backend": n, "model": res.model, "t_start_kst": t_start,
                           "latency_ms": round(res.latency_ms, 1), "attempts": attempts,
                           "input_tokens": res.input_tokens, "output_tokens": res.output_tokens,
                           "cost_usd": res.cost_usd, "error": res.error, "answers": res.answers, "meta": res.meta}
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    f.flush()
                    print(f"rep{rep} {row['id']} {n}: {res.latency_ms:.0f} ms{' 오류 ' + res.error[:80] if res.error else ''}",
                          flush=True)
    for c in clients.values():
        await c.aclose()

    meta["finished_kst"] = datetime.now(KST).isoformat(timespec="seconds")
    meta["network_after"] = network_conditions(hosts)
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
                                       newline="\n")
    print(f"원본 로그: {raw_path}")


if __name__ == "__main__":
    asyncio.run(main())
