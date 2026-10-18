"""개발용 로그의 모든 근거 이벤트에서 법령 검색어 규칙을 오프라인 비교한다."""

import argparse
import json
import re
from pathlib import Path

from jikgam.engine.law import bundle, load_law
from jikgam.engine.org import ROOT, load_org
from jikgam.sim import judge
from jikgam.sim.metrics import percentile_inf
from jikgam.sim.scenario import load_scenarios
from jikgam.v2.e2e_logs import read_log

RULES = ("current", "recent-two", "all-customer", "recent-two-context", "all-expanded", "recent-two-expanded")


def query_text(rule, current, customer_texts, intent="", fact_terms=()):
    """시나리오나 정답을 참조하지 않는 대화 일반 검색어 규칙이다."""
    if rule == "current":
        return current
    if rule == "all-customer":
        return " ".join(customer_texts)
    recent = " ".join(customer_texts[-2:])
    if rule == "recent-two":
        return recent
    if rule == "recent-two-context":
        return " ".join([recent, intent, *sorted(set(fact_terms))]).strip()
    raise ValueError(f"알 수 없는 검색어 규칙: {rule}")


def confirmed_terms(data):
    """업무 응답의 범주·제목에서 낱말을 가져온다. 식별자·수치·고객 진술은 쓰지 않는다."""
    wanted = {"type", "status", "reason", "cause", "plan_type", "plan_name", "name", "method", "payment_method"}
    out = set()
    if isinstance(data, dict):
        for key, value in data.items():
            if key in wanted and isinstance(value, str):
                out.add(value)
            if isinstance(value, (dict, list)):
                out.update(confirmed_terms(value))
    elif isinstance(data, list):
        for value in data:
            out.update(confirmed_terms(value))
    return out


def found(gold, ids):
    tools = judge._law_tools()
    coverage = tools["Coverage"]()
    for c in judge.law_chunks(set(ids)):
        coverage.add(c.doc, c.start, c.end)
    return tools["scorer"].found({"evidence": gold}, coverage)


def run(run_dirs, name):
    scenarios = {s["id"]: s for s in load_scenarios(ROOT / "data/scenarios/org_a_v2.jsonl")}
    org = load_org("org_a")
    from dataclasses import replace
    from types import SimpleNamespace
    from jikgam.engine.retrieval import ArticleIndex, RetrievalContext
    local = ROOT / "data/v2/local/e2e" / name
    local.mkdir(parents=True, exist_ok=True)
    trace = local / "trace.jsonl"
    reports = {}
    distributions = {}
    with trace.open("w", encoding="utf-8", newline="\n") as output:
        for directory in run_dirs:
            summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
            profile = summary["law"]
            law = bundle(load_law(profile))
            results = {rule: {"events": 0, "items": 0, "found": 0, "complete": 0} for rule in RULES}
            probabilities = []
            cache = {}
            retrieval_events = 0
            for sess in summary["sessions"]:
                customer, facts, intent, intent_label = [], set(), "", None
                gold = scenarios[sess["scenario"]]["expected"].get("law_evidence") or []
                es = read_log(directory / f"{sess['session']}.jsonl")
                evidence_versions = {e["version"] for e in es if e["type"] == "evidence.updated"}
                for e in es:
                    if e["type"] == "utterance.end" and e.get("speaker", "customer") == "customer":
                        customer.append(e["text"])
                    if e["type"] == "tool.result" and e.get("ok"):
                        if e.get("request_version") == e.get("version") or e.get("tool", "").startswith("create_"):
                            facts.update(confirmed_terms(e.get("data")))
                    if e["type"] == "evidence.updated":
                        intent_label = e.get("intent")
                        intent = org.retrieval.intent_terms.get(e.get("intent"), "")
                        # 후보에 든 개별 정답 구간을 선택하지 못한 확률만 보고한다.
                        for cid in e.get("candidates", []):
                            if any(found(gold, [cid])) and (p := e.get("applies", {}).get(cid)) is not None and p < 0.5:
                                probabilities.append(p)
                    if e["type"] != "retrieval.done":
                        continue
                    retrieval_events += 1
                    if e["version"] not in evidence_versions:
                        continue
                    for rule in RULES:
                        if rule in ("all-expanded", "recent-two-expanded"):
                            turns = max(len(customer), 1) if rule == "all-expanded" else 2
                            dummy = SimpleNamespace(cfg=replace(org.retrieval, query_turns=turns))
                            query = ArticleIndex.query_text(dummy, RetrievalContext(tuple(customer), intent=intent_label))
                        else:
                            query = query_text(rule, e["query"], customer, intent, facts)
                        if query not in cache:
                            cache[query] = law.search(query)[0]
                        ids = cache[query]
                        hits = found(gold, ids)
                        result = results[rule]
                        result["events"] += 1
                        result["items"] += len(hits)
                        result["found"] += sum(hits)
                        result["complete"] += all(hits)
                        output.write(json.dumps({"profile": profile, "scenario": sess["scenario"],
                                                 "session": sess["session"], "version": e["version"],
                                                 "rule": rule, "query": query, "candidates": ids,
                                                 "found": hits}, ensure_ascii=False) + "\n")
                print(f"{profile} {sess['scenario']}: 검색어 오프라인 비교 완료", flush=True)
            for result in results.values():
                result["coverage"] = result["found"] / result["items"] if result["items"] else None
            reports[profile] = results
            results["current"]["retrieval_events_total"] = retrieval_events
            results["current"]["canceled_before_evidence"] = retrieval_events - results["current"]["events"]
            distributions[profile] = {"count": len(probabilities), "min": min(probabilities, default=None),
                                      "p50": percentile_inf(probabilities, 50) if probabilities else None,
                                      "p95": percentile_inf(probabilities, 95) if probabilities else None,
                                      "max": max(probabilities, default=None),
                                      "bins": {f"{n / 10:.1f}~{(n + 1) / 10:.1f}":
                                               sum(n / 10 <= p < (n + 1) / 10 for p in probabilities) for n in range(5)}}
    adopted = [r for r in RULES if r != "current" and all(
        reports[p][r]["coverage"] > reports[p]["current"]["coverage"] for p in ("r0", "r4"))]
    result = {"profiles": reports, "law_applies_not_selected": distributions, "eligible_rules": adopted,
              "candidate_count": 8, "paid_cost_usd": 0}
    dest = ROOT / "results/v2/e2e" / name
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    lines = ["| 규칙 | R0 덮는 항목/전체 | R0 비율 | R4 덮는 항목/전체 | R4 비율 |", "|---|---:|---:|---:|---:|"]
    for rule in RULES:
        a, b = (reports[p][rule] for p in ("r0", "r4"))
        lines.append(f"| {rule} | {a['found']}/{a['items']} | {a['coverage']:.4f} | {b['found']}/{b['items']} | {b['coverage']:.4f} |")
    (dest / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_dirs", nargs="+", type=Path)
    ap.add_argument("--name", default="query-dev-v2-2")
    args = ap.parse_args()
    print(json.dumps(run(args.run_dirs, args.name), ensure_ascii=False))


if __name__ == "__main__":
    main()
