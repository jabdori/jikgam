"""고정한 창·앞 맥락으로 덩어리만 두 번 다시 판단한다(§4.1, 보고용)."""

import argparse
import asyncio
import json
import time
import tomllib
from dataclasses import asdict

from jikgam.ingest import chunk as chunking
from jikgam.ingest.corpus import ROOT, load_docs
from jikgam.ingest.window import Config, Judge, apply_cmax, context_text, summarize_calls, windows_from
from jikgam.v2.stats import boundary_agreement

REPS = 2
METHODS = {"b": ("llm", None), "c-jev": ("jev", 0.65), "c-qwen": ("qwen", 0.55)}


def selected_windows(lt: list[int], segments: list[dict], W: int = 4000) -> list[tuple[int, int, int, int]]:
    """판단 구역 [s, jend]가 덩어리 (start, end]와 겹치는 창만 고른다."""
    out, s, k = [], 1, 0
    while s <= len(lt):
        jend, wend = windows_from(lt, s, W)
        if any(s <= seg["end"] and jend > seg["start"] for seg in segments):
            out.append((s, jend, wend, k))
        s, k = jend + 1, k + 1
    return out


def chunk_starts(name: str, root=ROOT) -> dict[str, list[int]]:
    out = {}
    path = root / "data/v2/chunks" / f"{name}.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            out.setdefault(row["doc"], set()).add(row["start_line"])
    return {doc: sorted(starts) for doc, starts in out.items()}


def within_segments(segments: list[dict], starts: dict[str, list[int]]) -> dict[str, list[int]]:
    return {s["id"]: [n for n in starts.get(s["doc"], []) if s["start"] < n <= s["end"]] for s in segments}


def require_qwen(root=ROOT) -> None:
    """설정에 고정한 로컬 서버가 연결되는지 확인한다. 자동 대체는 하지 않는다."""
    import httpx
    from jikgam.decision.registry import secrets

    cfg = tomllib.loads((root / "config/decision_models.toml").read_text(encoding="utf-8"))
    url = cfg["q35-2b-ko-direct"]["url"]
    try:
        model = cfg["q35-2b-ko-direct"]
        key = secrets()[model["api_key_env"]]
        with httpx.Client(headers={"Authorization": f"Bearer {key}"}, timeout=120.0) as client:
            info = client.get(url.rsplit("/", 1)[0] + "/info")
            info.raise_for_status()
            if info.json().get("weights_sha256_16") != model["weights_sha256_16"]:
                raise ValueError("가중치 해시 불일치")
            reply = client.post(url, json={"model": model["model"], "state": {"probe": "준비 확인"},
                                           "questions": {"ready": {"type": "noul", "instructions": "준비 확인 요청인가?"}}})
            reply.raise_for_status()
            answer = reply.json().get("answers", {}).get("ready", {})
            if answer.get("type") != "noul" or not isinstance(answer.get("noul"), (int, float)):
                raise ValueError("인증된 판별 요청 실패")
    except (httpx.HTTPError, ValueError, KeyError):
        raise SystemExit(f"q35-2b-ko-direct 서버에 연결할 수 없습니다: {url}\n"
                         "인증·고정 가중치 해시·작은 판별 요청이 모두 성공해야 합니다.\n"
                         "WSL2 서버 실행: bash jikgam/train/run_wsl.sh serve_direct --run q35-2b-d-mix --port 8893") from None


async def run(method: str, smoke: int | None = None) -> dict:
    who, theta = METHODS[method]
    cfg = Config(who, 4000, 800, "raw")
    if who == "qwen":
        require_qwen()
    segments = json.loads((ROOT / "data/v2/boundary/segments.json").read_text(encoding="utf-8"))["segments"]
    if smoke is not None:
        segments = segments[:smoke]
    docs = load_docs({s["doc"] for s in segments})
    original = chunk_starts(cfg.name)
    final_name = cfg.name if theta is None else f"{cfg.name}-t{theta:.2f}"
    final_starts = chunk_starts(final_name)
    final = within_segments(segments, final_starts)
    # 두 반복 모두 본 실행의 앞 맥락을 쓴다. 반복의 판단 결과는 다음 창 입력에 영향을 주지 않는다.
    jobs, lts = [], {}
    for did, doc in docs.items():
        lt = chunking.line_tokens(doc)
        lts[did] = lt
        segs = [s for s in segments if s["doc"] == did]
        for s, jend, wend, k in selected_windows(lt, segs, cfg.W):
            open_start = max((n for n in original[did] if n < s), default=1)
            ctx = context_text(doc, open_start, s, None, "raw", sum(lt[open_start - 1:s - 1]))
            jobs.append((doc, ctx, s, jend, wend, k))
    judge = Judge(cfg)
    calls, runs, judgment_runs, times = [], [], [], []
    t0 = time.perf_counter()

    async def one(job):
        if who == "llm":
            picked, _, log = await judge.llm_window(*job)
        else:
            probs, log = await judge.judge_window(*job)
            picked = [n for n, p in probs.items() if round(p, 4) >= theta]
        return job[0].id, picked, log

    try:
        for _ in range(REPS):
            started = time.perf_counter()
            bounds = {did: set() for did in docs}
            final_bounds = {did: set() for did in docs}
            opened, last = {}, {}
            for job, (did, picked, log) in zip(jobs, await asyncio.gather(*(one(job) for job in jobs))):
                _, _, s, jend, _, _ = job
                bounds[did].update(picked)
                if last.get(did) != s - 1:
                    opened[did] = max((n for n in final_starts[did] if n < s), default=1)
                final_bounds[did].update(picked)
                opened[did], _ = apply_cmax(lts[did], final_bounds[did], s, jend, opened[did], cfg.cmax)
                last[did] = jend
                calls.append(log)
            judgment_runs.append(within_segments(segments, {d: sorted(ns) for d, ns in bounds.items()}))
            runs.append(within_segments(segments, {d: sorted(ns) for d, ns in final_bounds.items()}))
            times.append(round(time.perf_counter() - started, 3))
    finally:
        await judge.aclose()
    if not calls or all(c.error and not c.successful_requests for c in calls):
        raise RuntimeError("반복 분할 호출이 모두 실패했습니다. 결과를 저장하지 않습니다")
    return {"method": method, "config": asdict(cfg), "theta": theta, "reps": REPS,
            "segments": len(segments), "runs": runs,
            "judgment_runs": judgment_runs,
            "agreement": {"judgment": {"run1_vs_run2": boundary_agreement(segments, judgment_runs[0], judgment_runs[1])},
                          "final": {"run1_vs_run2": boundary_agreement(segments, runs[0], runs[1]),
                          "run1_vs_original": boundary_agreement(segments, runs[0], final),
                          "run2_vs_original": boundary_agreement(segments, runs[1], final)}},
            "calls": summarize_calls(calls), "seconds_wall": round(time.perf_counter() - t0, 3),
            "seconds_by_run": times}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--method", choices=list(METHODS), required=True)
    ap.add_argument("--frozen")
    ap.add_argument("--smoke", type=int)
    ap.add_argument("--probe-qwen", action="store_true", help="서버 연결만 확인한다")
    args = ap.parse_args()
    if args.probe_qwen:
        require_qwen()
        return
    if args.smoke is not None and args.smoke < 1:
        ap.error("--smoke는 1 이상이어야 합니다")
    if args.smoke is None and args.frozen != "0025":
        ap.error("본 실행에는 --frozen 0025가 필요합니다")
    result = asyncio.run(run(args.method, args.smoke))
    out = ROOT / ("outputs/v2-smoke" if args.smoke is not None else "results/v2/test")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"boundary-reruns-{args.method}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"method": args.method, "segments": result["segments"], "calls": result["calls"],
                      "seconds_wall": result["seconds_wall"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
