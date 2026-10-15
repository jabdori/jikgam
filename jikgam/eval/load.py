"""부하 측정: 판별 백엔드 하나에 동시 요청 여러 개를 정한 시간 동안 보내 지연과 오류율을 잰다(관리 콘솔 벤치마크 "부하",
ADR 0021).

    uv run python -m jikgam.eval.load --backend jev --concurrency 10 --duration-s 300

- 요청은 판별 데이터 val 분할의 사례를 차례로 돌려 쓴다(test 분할은 쓰지 않는다). 답의 정답 여부는 보지 않는다.
- 동시 작업 N개가 각자 응답을 받으면 바로 다음 요청을 보낸다. 시간이 다 되면 새 요청을 보내지 않고 남은 응답만 기다린다.
- 지연은 클라이언트에서 잰 요청 전송부터 응답까지이며, 재시도하지 않는다(오류는 오류로 센다).
- 결과: <out>/<run_id>/raw.jsonl(호출마다 한 줄)과 meta.json. 표는 jikgam.web.data.benchmarks가 이 로그로만 만든다.
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jikgam.decision.registry import ROOT, load_config, make_client
from jikgam.eval.questions import load_bank, render

KST = timezone(timedelta(hours=9))


async def run(backend: str, concurrency: int, duration_s: float, out: Path, questions_file: Path) -> Path:
    cfg = load_config()[backend]
    rows = [json.loads(line) for line in (ROOT / "data/build/val.jsonl").read_text(encoding="utf-8").splitlines() if line]
    _, bank = load_bank(questions_file)
    cases = [(r["id"], json.loads(r["state"]), render(list(json.loads(r["questions"])), bank)) for r in rows]
    started = datetime.now(KST)
    run_id = started.strftime("%Y%m%dT%H%M%S")
    out_dir = out / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    client = make_client(backend)
    next_case = 0
    deadline = time.perf_counter() + duration_s
    raw = (out_dir / "raw.jsonl").open("w", encoding="utf-8", newline="\n")

    async def worker(w: int) -> None:
        nonlocal next_case
        while time.perf_counter() < deadline:
            cid, state, questions = cases[next_case % len(cases)]
            next_case += 1
            t0 = time.perf_counter()
            res = await client.decide(state, questions)
            raw.write(json.dumps({"worker": w, "case": cid, "t_offset_s": round(t0 - (deadline - duration_s), 3),
                                  "latency_ms": round(res.latency_ms, 1), "error": res.error, "model": res.model,
                                  "input_tokens": res.input_tokens, "cost_usd": res.cost_usd}, ensure_ascii=False) + "\n")
            await asyncio.sleep(0)  # 네트워크를 기다리지 않는 로컬 기준선도 다른 작업에 차례를 넘긴다

    try:
        await asyncio.gather(*(worker(i) for i in range(concurrency)))
    finally:
        raw.close()
        await client.aclose()
    meta = {"run_id": run_id, "backend": backend, "kind": cfg.get("kind"), "model": cfg.get("model"),
            "concurrency": concurrency, "duration_s": duration_s, "split": "val",
            "started_kst": started.isoformat(timespec="seconds"),
            "finished_kst": datetime.now(KST).isoformat(timespec="seconds")}
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return out_dir


def main() -> None:
    ap = argparse.ArgumentParser(description="판별 백엔드 부하 측정")
    ap.add_argument("--backend", required=True)
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--duration-s", type=float, default=300.0)
    ap.add_argument("--out", type=Path, default=ROOT / "results/load")
    ap.add_argument("--questions", type=Path, default=ROOT / "data/questions.json")
    args = ap.parse_args()
    if args.concurrency < 1 or args.duration_s <= 0:
        raise SystemExit("--concurrency는 1 이상, --duration-s는 0보다 커야 한다")
    sys.stdout.reconfigure(encoding="utf-8")
    out_dir = asyncio.run(run(args.backend, args.concurrency, args.duration_s, args.out, args.questions))
    print(f"결과: {out_dir}")


if __name__ == "__main__":
    main()
