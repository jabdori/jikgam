"""두 구성(R0와 R4 또는 R5)의 §8.4 수치 보고. 주장 글과 실패 사유 원문은 결과 폴더에 쓰지 않는다.

R5 확정 측정(docs/evaluation-v2-r5.md)의 주 지표인 법령 근거 포함률(정답 법령 항목 단위)도 함께 낸다.
"""

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
from jikgam.sim.scenario import load_scenarios
from jikgam.v2 import answer, e2e_claims
from jikgam.v2.e2e_contract import legacy_contract, verify_comparison

SEED = 20261016
REPS = 10_000
OTHERS = ("r4", "r5")  # R0와 비교하는 구성


def mean(values):
    values = [x for x in values if x is not None]
    return sum(values) / len(values) if values else None


def ratio(a, b):
    return a / b if b else None


def claim_counts(events, articles, directory, model):
    total = unsupported = missing = failed = 0
    for row in e2e_claims.exposed_inputs(events, articles):
        if row.get("excluded"):
            continue
        if row.get("input_missing"):
            missing += 1
            continue
        path = directory / f"{e2e_claims.fingerprint(row, model)}.json"
        if not path.exists():
            missing += 1
            continue
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            rec = {}
        if not e2e_claims.cache_valid(rec):
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
    cached = [e2e_claims.read_cache(p) for p in directory.glob("*.json")]
    for s in run["sessions"]:
        r = s["result"]
        events = read_log(run_dir / f"{s['session']}.jsonl")
        total, unsupported, missing, failed = claim_counts(events, articles, directory, model)
        legacy = [c for rec in cached if rec.get("session") == s["session"]
                  and rec.get("definition") != e2e_claims.DEFINITION and not rec.get("error")
                  and e2e_claims.cache_valid(rec)
                  for c in rec.get("claims") or []]
        input_missing = sum(r["input_missing"] for r in e2e_claims.exposed_inputs(events))
        rows.append({"scenario": s["scenario"], "run": s.get("run", 1),
                     "execution_contract": run["summary"].get("contract") or legacy_contract(run["summary"]),
                     "private_log": original_path(run_dir / f"{s['session']}.jsonl").parent != run_dir,
                     "valid": r["compliance"]["valid"], "pass": r["pass"], "claims": total,
                     "unsupported": unsupported, "missing": missing, "claim_failures": failed,
                     "input_missing": input_missing, "legacy_claims": len(legacy),
                     "excluded_fixed_suggestions": sum(bool(x.get("excluded")) for x in e2e_claims.exposed_inputs(events)),
                     "legacy_unsupported": sum(not c["supported"] for c in legacy),
                     "ai_error": bool((s.get("ai") or {}).get("error")),
                     "items": {n: v["ok"] for n, v in r["items"].items()},
                     "law_items": (r["aux"].get("law") or {}).get("law_items", 0),
                     "law_found": (r["aux"].get("law") or {}).get("law_found", 0),
                     "aux": r["aux"], "metrics": s["metrics"]})
    summary = run["summary"]
    selection = (summary.get("contract") or {}).get("arguments", {}).get("scenarios", summary.get("scenarios", "all"))
    if isinstance(selection, str) and selection != "all":
        selection = selection.split(",")
    planned = {s["id"] for s in load_scenarios(ROOT / summary["scenario_file"])
               if selection == "all" or s["id"] in selection}
    seen = {(r["scenario"], r["run"]) for r in rows}
    for sid in sorted(planned):
        for k in range(summary.get("run_start", 1), summary.get("run_start", 1) + summary["runs"]):
            if (sid, k) not in seen:
                rows.append({"scenario": sid, "run": k, "valid": None, "pass": None, "claims": 0,
                             "unsupported": 0, "missing": 0, "planned_missing": True, "law_items": 0, "law_found": 0,
                             "execution_contract": summary.get("contract") or legacy_contract(summary),
                             "items": {n: None for n in range(1, 11)}, "aux": {"precision": None},
                             "metrics": metrics.session_metrics([])})
    cost = {k: sum((s["costs"].get(k + "_usd") or 0) for s in run["sessions"])
            for k in ("customer", "decision", "generation")}
    cost["judge"] = sum((s.get("ai") or {}).get("cost_usd", 0) for s in run["sessions"])
    cost["claims"] = sum(rec.get("usage", {}).get("cost_usd", 0) for rec in cached)
    cost["total"] = sum(cost.values())
    return rows, cost


def totals(rows):
    valid = [r for r in rows if r["valid"] is True]
    return {"valid": len(valid), "passed": sum(r["pass"] is True for r in valid),
            "claims": sum(r["claims"] for r in valid), "unsupported": sum(r["unsupported"] for r in valid),
            "missing": sum(r["missing"] for r in valid),
            "law_items": sum(r.get("law_items", 0) for r in valid), "law_found": sum(r.get("law_found", 0) for r in valid)}


def rates(rows):
    t = totals(rows)
    return ratio(t["passed"], t["valid"]), (None if t["missing"] else ratio(t["unsupported"], t["claims"]))


def law_rate(rows):
    """법령 근거 포함률: 유효 실행의 정답 법령 항목 중 상담 근거(노출 제안 인용 합집합)에 든 비율."""
    t = totals(rows)
    return ratio(t["law_found"], t["law_items"])


def summarize(rows, cost):
    t = totals(rows)
    valid = [r for r in rows if r["valid"] is True]
    agg = metrics.aggregate([r["metrics"] for r in valid])
    return {**t, "invalid": sum(r["valid"] is False for r in rows), "unknown_compliance": sum(r["valid"] is None for r in rows),
            "missing_planned_runs": sum(r.get("planned_missing", False) for r in rows),
            "excluded_fixed_suggestions": sum(r.get("excluded_fixed_suggestions", 0) for r in valid),
            "claim_status": "입력 없음(이전 정의)" if any(r.get("input_missing", 0) for r in valid)
                            else ("판정 완료" if not t["missing"] else "미판정 제안 있음"),
            "generation_inputs_missing": sum(r.get("input_missing", 0) for r in valid),
            "legacy_cited_articles": {
                "label": "인용 조항만 준 이전 판정",
                "claims": sum(r.get("legacy_claims", 0) for r in valid),
                "unsupported": sum(r.get("legacy_unsupported", 0) for r in valid),
                "rate": ratio(sum(r.get("legacy_unsupported", 0) for r in valid),
                              sum(r.get("legacy_claims", 0) for r in valid))},
            "unknown_pass": sum(r["pass"] is None for r in valid),
            "ai_judge_errors": sum(r.get("ai_error", False) for r in rows),
            "unknown_items": {str(n): sum(r["items"][n] is None for r in valid) for n in range(1, 11)},
            "claim_judge_failures": sum(r.get("claim_failures", 0) for r in valid),
            "log_sources": {"private_sessions": sum(r.get("private_log", False) for r in rows),
                            "public_sessions": sum(not r.get("private_log", False) for r in rows)},
            "pass_rate": rates(rows)[0], "unsupported_claim_rate": rates(rows)[1],
            "law_evidence": {"items": t["law_items"], "found": t["law_found"], "rate": law_rate(rows)},
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


def paired_bootstrap(r0, other_rows, reps=REPS, seed=SEED, other="r4"):
    ids = sorted({r["scenario"] for r in r0} | {r["scenario"] for r in other_rows})
    if not ids or {r["scenario"] for r in r0} != {r["scenario"] for r in other_rows}:
        raise ValueError("두 구성은 같은 시나리오 묶음이어야 합니다")
    keys = lambda rows: sorted((r["scenario"], r["run"]) for r in rows)
    if keys(r0) != keys(other_rows):
        raise ValueError("두 구성의 시나리오별 반복이 다릅니다")
    groups = [{sid: [r for r in rows if r["scenario"] == sid] for sid in ids} for rows in (r0, other_rows)]
    rng = random.Random(seed)
    names = ("pass", "unsupported", "law")
    draws = {f"{n}_{k}": [] for n in names for k in ("r0", other, "delta")}
    complete_claims = not any(totals(rows)["missing"] for rows in (r0, other_rows))
    known = not any(r["valid"] is None for rows in (r0, other_rows) for r in rows)
    known_pass = known and not any(r["pass"] is None for rows in (r0, other_rows) for r in rows if r["valid"] is True)
    usable = {"pass": known_pass, "unsupported": known and complete_claims, "law": known}
    for _ in range(reps):
        # 한 번 뽑은 시나리오 묶음을 세 지표에 같이 쓴다(docs/evaluation-v2-r5.md §5)
        chosen = rng.choices(ids, k=len(ids))
        picked = [[r for sid in chosen for r in g[sid]] for g in groups]
        values = [(*rates(rows), law_rate(rows)) for rows in picked]
        for i, name in enumerate(names):
            if not usable[name]:
                continue
            a, b = values[0][i], values[1][i]
            if a is not None:
                draws[f"{name}_r0"].append(a)
            if b is not None:
                draws[f"{name}_{other}"].append(b)
            if a is not None and b is not None:
                draws[f"{name}_delta"].append(b - a)
    return {"reps": reps, "seed": seed, "scenarios": len(ids),
            "ci95": {k: [metrics.percentile_inf(v, q) for q in (2.5, 97.5)] if v else None
                     for k, v in draws.items()}, "effective_draws": {k: len(v) for k, v in draws.items()}}


def compare(r0, other_rows, costs, other="r4"):
    if other not in OTHERS:
        raise ValueError(f"R0와 비교할 구성은 {OTHERS} 중 하나입니다")
    contract_status = verify_comparison(r0, other_rows)
    a, b = summarize(r0, costs[0]), summarize(other_rows, costs[1])
    delta = b["pass_rate"] - a["pass_rate"] if None not in (a["pass_rate"], b["pass_rate"]) else None
    unsupported = None if None in (a["unsupported_claim_rate"], b["unsupported_claim_rate"]) else (
        b["unsupported_claim_rate"] <= a["unsupported_claim_rate"])
    known = not any(s["unknown_compliance"] or s["unknown_pass"] for s in (a, b))
    p95 = b["refresh_ms"]["p95"]
    bootstrap = paired_bootstrap(r0, other_rows, REPS, SEED, other)
    ci = bootstrap["ci95"]["pass_delta"]
    compliance_known = not any(s["unknown_compliance"] for s in (a, b))
    law_a, law_b = a["law_evidence"]["rate"], b["law_evidence"]["rate"]
    law_delta = law_b - law_a if None not in (law_a, law_b) else None
    law_ci = bootstrap["ci95"].get("law_delta")
    return {"r0": a, other: b, "other": other, "pass_delta": delta, "law_evidence_delta": law_delta,
            "bootstrap": bootstrap, "contract_status": contract_status,
            "conditions": {"pass_noninferior": delta >= -0.05 if delta is not None and known else None,
                           "pass_improved": delta >= 0.10 and ci[0] > 0 if delta is not None and known and ci else None,
                           "unsupported_not_higher": unsupported if compliance_known else None,
                           "refresh_p95_le_500": p95 <= 500 if compliance_known and not math.isnan(p95) else None,
                           # 주 지표(R5 확정 측정): 코드로 계산하므로 사람 확인으로 보류하지 않는다
                           "law_evidence_better": (law_delta >= 0.10 and law_ci[0] > 0)
                           if law_delta is not None and compliance_known and law_ci else None}}


def serializable(value):
    """표준 JSON에 없는 무한대는 문자열, 측정 없음은 null로 구분한다."""
    if isinstance(value, float) and not math.isfinite(value):
        return "∞" if math.isinf(value) else None
    if isinstance(value, dict):
        return {k: serializable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [serializable(v) for v in value]
    return value


def reflect_check(report, check):
    """일치율 미달 조건만 보류한다. 자동 계산 결과는 비교용으로 보존한다."""
    report["human_check"] = check
    report["automated_conditions"] = dict(report["conditions"])
    deferred = []
    if check["status"] == "확인 완료":
        if check["claims"]["agreement"] < 0.80:
            deferred.append("unsupported_not_higher")
        if check["scenario_items"]["agreement"] < 0.80:
            deferred += ["pass_noninferior", "pass_improved"]
    elif check["status"] != "미확인":
        deferred = ["unsupported_not_higher", "pass_noninferior", "pass_improved"]
    report["deferred_conditions"] = deferred
    for key in deferred:
        report["conditions"][key] = None
    return report


def render(report):
    other = report.get("other", "r4")
    lines = ["| 구성 | 통과/유효 | 무효 | 항목 1~10 실패 | 근거 없는 주장 | 근거 갱신 p50/p95(ms) | 비용($) |",
             "|---|---:|---:|---|---:|---:|---:|"]
    for key in ("r0", other):
        s = report[key]
        rate = s["unsupported_claim_rate"]
        claim_text = (f"{s['unsupported']}/{s['claims']} ({rate:.2%})" if rate is not None
                      else f"{s['claim_status']}(완료 주장 {s['claims']}, 미판정 제안 {s['missing']})")
        lines.append(f"| {key.upper()} | {s['passed']}/{s['valid']} | {s['invalid']} | "
                     f"{','.join(str(x) for x in s['failures'].values())} | "
                     f"{claim_text} | "
                     f"{s['refresh_ms']['p50']}/{s['refresh_ms']['p95']} | {s['cost_usd']['total']:.4f} |")
    lines += ["", "| 구성 | 무효 노출/분모 | 미재확인 | 근거 정밀도 | 법령 정밀도 | 법령 토큰 평균/합계 | 미판정 제안/실패 판정 |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for key in ("r0", other):
        s = report[key]
        channels = s["stale"]["by_channel"].values()
        lines.append(f"| {key.upper()} | {sum(c['invalid'] for c in channels)}/{sum(c['denominator'] for c in channels)} | "
                     f"{sum(c['unconfirmed'] for c in channels)} | {s['precision']} | {s['law_precision']} | "
                     f"{s['law_tokens']['mean']}/{s['law_tokens']['sum']} | {s['missing']}/{s['claim_judge_failures']} |")
    lines += ["", "| 구성 | 법령 근거 포함(항목) | 포함률 |", "|---|---:|---:|"]
    for key in ("r0", other):
        law = report[key].get("law_evidence") or {}
        lines.append(f"| {key.upper()} | {law.get('found')}/{law.get('items')} | {law.get('rate')} |")
    lines += ["", f"법령 근거 포함률 차이({other.upper()}−R0): {report.get('law_evidence_delta')}",
              f"통과율 차이({other.upper()}−R0): {report['pass_delta']}",
              f"시나리오 단위 짝지은 부트스트랩 95% 구간: {report['bootstrap']['ci95']}",
              f"판정 조건: {report['conditions']}",
              f"입력 로그 출처: R0 {report['r0']['log_sources']}, {other.upper()} {report[other]['log_sources']}"]
    lines += [f"실행 계약: {report.get('contract_status', '미확인')}",
              f"준수 미판정: R0 {report['r0']['unknown_compliance']}, {other.upper()} {report[other]['unknown_compliance']}",
              f"주장 판정 제외 고정 제안: R0 {report['r0']['excluded_fixed_suggestions']}, "
              f"{other.upper()} {report[other]['excluded_fixed_suggestions']}"]
    check = report.get("human_check")
    if check:
        lines += ["", f"사람 확인: {check['status']}"]
        if check["status"] == "확인 완료":
            lines += ["| 사람 확인 | 일치/표본 | 일치율 |", "|---|---:|---:|"]
            for key, title in (("claims", "주장 근거"), ("scenario_items", "시나리오 항목")):
                c = check[key]
                lines.append(f"| {title} | {c['matched']}/{c['sampled']} | {c['agreement']:.2%} |")
        if report.get("deferred_conditions"):
            lines.append("판정 보류(일치율 0.80 미만 또는 입력 변경): " + ", ".join(report["deferred_conditions"]))
    if any(report[k]["legacy_cited_articles"]["claims"] for k in ("r0", other)):
        lines += ["", "인용 조항만 준 이전 판정(새 정의와 직접 비교하지 않음)"]
        for key in ("r0", other):
            legacy = report[key]["legacy_cited_articles"]
            lines.append(f"- {key.upper()}: {legacy['unsupported']}/{legacy['claims']} ({legacy['rate']})")
    return "\n".join(lines) + "\n"


def run_law(run_dir, root=ROOT):
    from jikgam.v2.e2e_logs import read_text

    return json.loads(read_text(Path(run_dir) / "summary.json", root)).get("law")


def write_report(r0_dir, other_dir, name, root=ROOT):
    """두 번째 실행의 구성(r4 또는 r5)은 실행 요약의 법령 프로필로 정한다."""
    from jikgam.v2.e2e_check import load_check, public_path

    public_path(name, root)
    other = run_law(other_dir, root)
    if other not in OTHERS:
        raise ValueError(f"{Path(other_dir).name}: R0와 비교할 구성({', '.join(OTHERS)})의 결과가 아닙니다")
    r0, c0 = collect(r0_dir, "r0")
    rows, cost = collect(other_dir, other)
    report = serializable(reflect_check(compare(r0, rows, [c0, cost], other),
                                       load_check(name, r0_dir, other_dir, root)))
    dest = root / "results/v2/e2e" / name
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1, allow_nan=False) + "\n",
                                      encoding="utf-8", newline="\n")
    (dest / "report.md").write_text(render(report), encoding="utf-8", newline="\n")
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("r0", type=Path)
    ap.add_argument("other", type=Path, help="R4 또는 R5 실행 폴더")
    ap.add_argument("--name", required=True)
    args = ap.parse_args()
    print(render(write_report(args.r0, args.other, args.name)))


if __name__ == "__main__":
    main()
