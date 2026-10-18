"""두 구성의 §8.4 수치 보고. 주장 글과 실패 사유 원문은 결과 폴더에 쓰지 않는다."""

import argparse
import json
import math
import random
from pathlib import Path

from jikgam.decision.registry import load_config
from jikgam.v2.e2e_logs import read_log, original_path
from jikgam.engine.org import ROOT
from jikgam.sim import metrics
from jikgam.sim.report import load_run
from jikgam.v2 import answer, e2e_claims

SEED = 20261016
REPS = 10_000


def mean(values):
    values = [x for x in values if x is not None]
    return sum(values) / len(values) if values else None


def ratio(a, b):
    return a / b if b else None


def claim_counts(events, articles, directory, model):
    total = unsupported = missing = failed = 0
    for row in e2e_claims.exposed_inputs(events, articles):
        path = directory / f"{e2e_claims.fingerprint(row, model)}.json"
        if not path.exists():
            missing += 1
            continue
        rec = json.loads(path.read_text(encoding="utf-8"))
        if rec.get("error") or not isinstance(rec.get("claims"), list):
            missing += 1
            failed += 1
            continue
        total += len(rec["claims"])
        unsupported += sum(c["supported"] is False for c in rec["claims"])
    return total, unsupported, missing, failed


def collect(run_dir, expected_law=None):
    run = load_run(run_dir)
    if expected_law and run["summary"].get("law") != expected_law:
        raise ValueError(f"{run_dir.name}: {expected_law.upper()} 구성의 결과가 아닙니다")
    directory = e2e_claims.local_dir(run["summary"]["run_id"])
    articles = e2e_claims.article_map(run["summary"])
    model = load_config()[answer.JUDGE_MODEL]["model"]
    rows = []
    for s in run["sessions"]:
        r = s["result"]
        events = read_log(run_dir / f"{s['session']}.jsonl")
        total, unsupported, missing, failed = claim_counts(events, articles, directory, model)
        rows.append({"scenario": s["scenario"], "run": s.get("run", 1),
                     "private_log": original_path(run_dir / f"{s['session']}.jsonl").parent != run_dir,
                     "valid": r["compliance"]["valid"], "pass": r["pass"], "claims": total,
                     "unsupported": unsupported, "missing": missing, "claim_failures": failed,
                     "ai_error": bool((s.get("ai") or {}).get("error")),
                     "items": {n: v["ok"] for n, v in r["items"].items()},
                     "aux": r["aux"], "metrics": s["metrics"]})
    cost = {k: sum((s["costs"].get(k + "_usd") or 0) for s in run["sessions"])
            for k in ("customer", "decision", "generation")}
    cost["judge"] = sum((s.get("ai") or {}).get("cost_usd", 0) for s in run["sessions"])
    cost["claims"] = sum(json.loads(p.read_text(encoding="utf-8"))["usage"]["cost_usd"]
                         for p in directory.glob("*.json"))
    cost["total"] = sum(cost.values())
    return rows, cost


def totals(rows):
    # 유효 여부가 미판정인 실행은 기존 v1 보고와 같이 두되 별도로 세어 판정 완료 여부를 드러낸다.
    valid = [r for r in rows if r["valid"] is not False]
    return {"valid": len(valid), "passed": sum(r["pass"] is True for r in valid),
            "claims": sum(r["claims"] for r in valid), "unsupported": sum(r["unsupported"] for r in valid),
            "missing": sum(r["missing"] for r in valid)}


def rates(rows):
    t = totals(rows)
    return ratio(t["passed"], t["valid"]), (None if t["missing"] else ratio(t["unsupported"], t["claims"]))


def summarize(rows, cost):
    t = totals(rows)
    valid = [r for r in rows if r["valid"] is not False]
    agg = metrics.aggregate([r["metrics"] for r in valid])
    return {**t, "invalid": len(rows) - t["valid"], "unknown_compliance": sum(r["valid"] is None for r in rows),
            "unknown_pass": sum(r["pass"] is None for r in valid),
            "ai_judge_errors": sum(r.get("ai_error", False) for r in rows),
            "unknown_items": {str(n): sum(r["items"][n] is None for r in valid) for n in range(1, 11)},
            "claim_judge_failures": sum(r.get("claim_failures", 0) for r in valid),
            "log_sources": {"private_sessions": sum(r.get("private_log", False) for r in rows),
                            "public_sessions": sum(not r.get("private_log", False) for r in rows)},
            "pass_rate": rates(rows)[0], "unsupported_claim_rate": rates(rows)[1],
            "claim_judgment_complete": t["missing"] == 0,
            "refresh_ms": agg["refresh"]["stats"],
            "stale": {"by_channel": {ch: {k: c[k] for k in ("invalid", "denominator", "unconfirmed")}
                                      for ch, c in agg["stale"]["by_channel"].items()},
                      "events_total": agg["stale"]["events_total"],
                      "events_with_invalid": agg["stale"]["events_with_invalid"]},
            "failures": {str(n): sum(r["items"][n] is False for r in valid) for n in range(1, 11)},
            "precision": mean([r["aux"]["precision"] for r in valid]),
            "law_precision": mean([(r["aux"].get("law") or {}).get("law_precision") for r in valid]),
            "law_tokens": {"mean": mean([(r["aux"].get("law") or {}).get("law_tokens") for r in valid]),
                           "sum": sum((r["aux"].get("law") or {}).get("law_tokens", 0) for r in valid)},
            "cost_usd": cost}


def paired_bootstrap(r0, r4, reps=REPS, seed=SEED):
    ids = sorted({r["scenario"] for r in r0} | {r["scenario"] for r in r4})
    if not ids or {r["scenario"] for r in r0} != {r["scenario"] for r in r4}:
        raise ValueError("두 구성은 같은 시나리오 묶음이어야 합니다")
    keys = lambda rows: sorted((r["scenario"], r["run"]) for r in rows)
    if keys(r0) != keys(r4):
        raise ValueError("두 구성의 시나리오별 반복이 다릅니다")
    groups = [{sid: [r for r in rows if r["scenario"] == sid] for sid in ids} for rows in (r0, r4)]
    rng = random.Random(seed)
    draws = {"pass_r0": [], "pass_r4": [], "pass_delta": [],
             "unsupported_r0": [], "unsupported_r4": [], "unsupported_delta": []}
    complete_claims = not any(totals(rows)["missing"] for rows in (r0, r4))
    for _ in range(reps):
        chosen = rng.choices(ids, k=len(ids))
        values = [rates([r for sid in chosen for r in g[sid]]) for g in groups]
        for i, name in enumerate(("pass", "unsupported")):
            if i == 1 and not complete_claims:
                continue
            a, b = values[0][i], values[1][i]
            if a is not None:
                draws[f"{name}_r0"].append(a)
            if b is not None:
                draws[f"{name}_r4"].append(b)
            if a is not None and b is not None:
                draws[f"{name}_delta"].append(b - a)
    return {"reps": reps, "seed": seed, "scenarios": len(ids),
            "ci95": {k: [metrics.percentile_inf(v, q) for q in (2.5, 97.5)] if v else None
                     for k, v in draws.items()}, "effective_draws": {k: len(v) for k, v in draws.items()}}


def compare(r0, r4, costs):
    a, b = summarize(r0, costs[0]), summarize(r4, costs[1])
    delta = b["pass_rate"] - a["pass_rate"] if None not in (a["pass_rate"], b["pass_rate"]) else None
    unsupported = None if None in (a["unsupported_claim_rate"], b["unsupported_claim_rate"]) else (
        b["unsupported_claim_rate"] <= a["unsupported_claim_rate"])
    known = not any(s["unknown_compliance"] or s["unknown_pass"] for s in (a, b))
    p95 = b["refresh_ms"]["p95"]
    return {"r0": a, "r4": b, "pass_delta": delta, "bootstrap": paired_bootstrap(r0, r4),
            "conditions": {"pass_noninferior": delta >= -0.05 if delta is not None and known else None,
                           "pass_improved": delta >= 0.10 if delta is not None and known else None,
                           "unsupported_not_higher": unsupported,
                           "refresh_p95_le_500": p95 <= 500 if not math.isnan(p95) else None}}


def serializable(value):
    """표준 JSON에 없는 무한대는 문자열, 측정 없음은 null로 구분한다."""
    if isinstance(value, float) and not math.isfinite(value):
        return "∞" if math.isinf(value) else None
    if isinstance(value, dict):
        return {k: serializable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [serializable(v) for v in value]
    return value


def render(report):
    lines = ["| 구성 | 통과/유효 | 무효 | 항목 1~10 실패 | 근거 없는 주장 | 근거 갱신 p50/p95(ms) | 비용($) |",
             "|---|---:|---:|---|---:|---:|---:|"]
    for key in ("r0", "r4"):
        s = report[key]
        rate = s["unsupported_claim_rate"]
        claim_text = (f"{s['unsupported']}/{s['claims']} ({rate:.2%})" if rate is not None
                      else f"미판정(완료 주장 {s['claims']}, 미판정 제안 {s['missing']})")
        lines.append(f"| {key.upper()} | {s['passed']}/{s['valid']} | {s['invalid']} | "
                     f"{','.join(str(x) for x in s['failures'].values())} | "
                     f"{claim_text} | "
                     f"{s['refresh_ms']['p50']}/{s['refresh_ms']['p95']} | {s['cost_usd']['total']:.4f} |")
    lines += ["", "| 구성 | 무효 노출/분모 | 미재확인 | 근거 정밀도 | 법령 정밀도 | 법령 토큰 평균/합계 | 미판정 제안/실패 판정 |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for key in ("r0", "r4"):
        s = report[key]
        channels = s["stale"]["by_channel"].values()
        lines.append(f"| {key.upper()} | {sum(c['invalid'] for c in channels)}/{sum(c['denominator'] for c in channels)} | "
                     f"{sum(c['unconfirmed'] for c in channels)} | {s['precision']} | {s['law_precision']} | "
                     f"{s['law_tokens']['mean']}/{s['law_tokens']['sum']} | {s['missing']}/{s['claim_judge_failures']} |")
    lines += ["", f"통과율 차이(R4−R0): {report['pass_delta']}",
              f"시나리오 단위 짝지은 부트스트랩 95% 구간: {report['bootstrap']['ci95']}",
              f"판정 조건: {report['conditions']}",
              f"입력 로그 출처: R0 {report['r0']['log_sources']}, R4 {report['r4']['log_sources']}"]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("r0", type=Path)
    ap.add_argument("r4", type=Path)
    ap.add_argument("--name", required=True)
    args = ap.parse_args()
    if Path(args.name).name != args.name or args.name in (".", ".."):
        raise SystemExit("보고서 이름에 경로를 쓸 수 없습니다")
    r0, c0 = collect(args.r0, "r0")
    r4, c4 = collect(args.r4, "r4")
    report = serializable(compare(r0, r4, [c0, c4]))
    dest = ROOT / "results/v2/e2e" / args.name
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1, allow_nan=False) + "\n",
                                      encoding="utf-8")
    table = render(report)
    (dest / "report.md").write_text(table, encoding="utf-8")
    print(table)


if __name__ == "__main__":
    main()
