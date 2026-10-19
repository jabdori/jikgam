"""개발용 v2 로그에서 법령 검색 변형(검색 범위, 검색어)을 오프라인 비교한다. 유료 호출이 없다.

엔진과 고정 설정은 바꾸지 않는다. 로그의 대화로 고정 커밋의 검색을 이 PC에서 다시 돌려, 변형마다 정답 법령 항목이 상위
k개(8, 16) 구간에 덮이는 비율을 본다. 단위와 덮음 판정은 jikgam/v2/e2e_queries.py와 같다(근거를 고른 검색 이벤트 × 정답 법령
항목, judge의 법령 덮음 기준).

- a 지금 그대로: 고정 규칙의 검색어(전체 고객 발화 + 의도·표현 확장, config/v2/law.toml query_scope = "all")
- b 적용 문서로 제한: 검색 결과에서 조직에 적용되는 문서(실행 인자로 넘기는 조직 설정 값) 밖의 구간을 빼고 상위 k개를 채운다
- c 판별 결과로 만든 검색어: 그 시점까지의 판별 결과만 쓴다. 요청(직전 근거 판단의 intent)을 조직의 intent_terms 낱말로,
  확인된 사실을 업무 응답의 범주·제목 낱말(e2e_queries.confirmed_terms와 같되, 식별자인 이름 필드는 뺀다)로 바꾼다.
  아직 판별 결과가 없으면(세션의 첫 검색) a를 쓴다
- d = b + c
- 보조 c_intent: c에서 확인된 사실 낱말을 빼고 요청 낱말만 쓴다(사실 낱말의 효과를 따로 보려고)

문서 형식이나 법령 번호 체계를 보는 규칙은 없다. 구간의 문서는 구간 메타데이터에서 읽는다.

    uv run --group embed python -m jikgam.v2.law_variants results/engine/scenarios/dev-v2-r0-{1,2,3} \\
        results/engine/scenarios/dev-v2-r4-{1,2,3} --documents ecom-act,ecom-decree,...
"""

import argparse
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from jikgam.engine.law import bundle, load_law
from jikgam.engine.org import ROOT, load_org
from jikgam.engine.retrieval import ArticleIndex, RetrievalContext
from jikgam.sim.scenario import load_scenarios
from jikgam.v2.e2e_logs import read_log
from jikgam.v2.e2e_queries import confirmed_terms, found
from jikgam.v2.search import rank_r0

VARIANTS = ("a", "b", "c", "d", "c_intent")
KS = (8, 16)
SCENARIOS = "data/scenarios/org_a_v2.jsonl"


def ranking(law, query: str) -> list[str]:
    """검색어 하나의 전체 순위(엔진의 법령 검색은 이 앞 8개를 쓴다)."""
    if law.cfg.mode == "r0":
        return [c for c, _ in rank_r0(law.cs, query)]
    return [c for c, _ in law.hybrid.retrieve(query)[0]]


def in_scope(law, ids: list[str], documents: frozenset[str]) -> list[str]:
    """적용 문서 안의 구간만 순서대로 남긴다."""
    return [c for c in ids if law.cs.by_id[c].doc in documents]


def fact_terms(data) -> set[str]:
    """업무 응답에서 확인된 사실의 범주·제목 낱말(e2e_queries.confirmed_terms). 식별자인 이름 필드(고객 이름 등)는 뺀다."""
    out = set()
    if isinstance(data, dict):
        out.update(confirmed_terms({k: v for k, v in data.items() if k != "name" and not isinstance(v, (dict, list))}))
        for v in data.values():
            if isinstance(v, (dict, list)):
                out.update(fact_terms(v))
    elif isinstance(data, list):
        for v in data:
            out.update(fact_terms(v))
    return out


def decision_query(intent_terms: str, facts: set[str]) -> str:
    """판별 결과만으로 만든 검색어: 요청 낱말 + 확인된 사실 낱말(정렬해 늘 같은 문자열)."""
    return " ".join(x for x in [intent_terms, *sorted(facts)] if x).strip()


def frozen_query(org, customer: list[str], intent_label: str | None) -> str:
    """고정 규칙의 법령 검색어(e2e_queries의 all-expanded와 같다)."""
    dummy = SimpleNamespace(cfg=replace(org.retrieval, query_turns=max(len(customer), 1)))
    return ArticleIndex.query_text(dummy, RetrievalContext(tuple(customer), intent=intent_label))


def run(run_dirs: list[Path], documents: frozenset[str], name: str) -> dict:
    scenarios = {s["id"]: s for s in load_scenarios(ROOT / SCENARIOS)}
    org = load_org("org_a")
    local = ROOT / "data/v2/local/law-variants" / name
    local.mkdir(parents=True, exist_ok=True)
    report: dict = {"documents": sorted(documents), "runs": [d.as_posix() for d in run_dirs], "paid_cost_usd": 0, "profiles": {}}
    with (local / "trace.jsonl").open("w", encoding="utf-8", newline="\n") as trace:
        for directory in run_dirs:
            summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
            profile = summary["law"]
            if summary.get("scenario_file") != SCENARIOS or profile not in ("r0", "r4"):
                raise ValueError("개발용 v2 시나리오의 R0·R4 실행만 지원합니다")
            law = bundle(load_law(profile))
            unknown = documents - {c.doc for c in law.cs.chunks}
            if unknown:
                raise ValueError(f"구간 세트에 없는 문서: {sorted(unknown)}")
            prof = report["profiles"].setdefault(profile, {
                "events": 0, "fallback_c": 0, "logged_query_checked": 0, "logged_query_same": 0, "logged_top8_same": 0,
                "variants": {v: {"items": 0, **{f"found@{k}": 0 for k in KS}} for v in VARIANTS},
                "sessions": {v: {"items": 0, **{f"found@{k}": 0 for k in KS}} for v in VARIANTS}})
            cache: dict[str, list[str]] = {}

            def ranked(q: str) -> list[str]:
                if q not in cache:
                    cache[q] = ranking(law, q)
                return cache[q]

            for sess in summary["sessions"]:
                gold = scenarios[sess["scenario"]]["expected"].get("law_evidence") or []
                if not gold:
                    continue
                es = read_log(directory / f"{sess['session']}.jsonl")
                evidence_versions = {e["version"] for e in es if e["type"] == "evidence.updated"}
                customer: list[str] = []
                facts: set[str] = set()
                intent_label, intent_terms = None, ""
                union = {v: {k: set() for k in KS} for v in VARIANTS}
                for e in es:
                    if e["type"] == "utterance.end" and e.get("speaker", "customer") == "customer":
                        customer.append(e["text"])
                    if e["type"] == "tool.result" and e.get("ok"):
                        if e.get("request_version") == e.get("version") or e.get("tool", "").startswith("create_"):
                            facts.update(fact_terms(e.get("data")))
                    if e["type"] == "evidence.updated":
                        intent_label = e.get("intent")
                        intent_terms = org.retrieval.intent_terms.get(intent_label, "")
                    if e["type"] != "retrieval.done" or e["version"] not in evidence_versions:
                        continue
                    base = frozen_query(org, customer, intent_label)
                    if summary["run_id"].endswith("-3"):  # 고정 규칙으로 돈 실행: 다시 만든 검색어와 후보가 기록과 같아야 한다
                        prof["logged_query_checked"] += 1
                        prof["logged_query_same"] += base == e.get("law_query")
                        prof["logged_top8_same"] += ranked(base)[:8] == e.get("law_candidates")
                    dq = decision_query(intent_terms, facts)
                    if not dq:
                        prof["fallback_c"] += 1
                    queries = {"a": base, "b": base, "c": dq or base, "d": dq or base, "c_intent": intent_terms or base}
                    prof["events"] += 1
                    row = {"profile": profile, "run": summary["run_id"], "scenario": sess["scenario"],
                           "version": e["version"], "queries": {"a": base, "c": dq or base, "c_intent": intent_terms or base},
                           "found": {}}
                    for v in VARIANTS:
                        ids = ranked(queries[v])
                        if v in ("b", "d"):
                            ids = in_scope(law, ids, documents)
                        stat = prof["variants"][v]
                        stat["items"] += len(gold)
                        row["found"][v] = {}
                        for k in KS:
                            hits = found(gold, ids[:k])
                            stat[f"found@{k}"] += sum(hits)
                            row["found"][v][k] = hits
                            union[v][k].update(ids[:k])
                    trace.write(json.dumps(row, ensure_ascii=False) + "\n")
                for v in VARIANTS:  # 세션 단위: 세션의 검색들 중 한 번이라도 상위 k개에 든 구간의 합집합
                    stat = prof["sessions"][v]
                    stat["items"] += len(gold)
                    for k in KS:
                        stat[f"found@{k}"] += sum(found(gold, sorted(union[v][k])))
                print(f"{summary['run_id']} {sess['scenario']}: 완료", flush=True)
    for prof in report["profiles"].values():
        for block in (prof["variants"], prof["sessions"]):
            for stat in block.values():
                for k in KS:
                    stat[f"rate@{k}"] = round(stat[f"found@{k}"] / stat["items"], 4) if stat["items"] else None
    dest = ROOT / "results/v2/law-variants" / name
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dirs", nargs="+", type=Path)
    ap.add_argument("--documents", required=True, help="조직에 적용되는 법령·고시·표준약관 문서 id(쉼표로 구분)")
    ap.add_argument("--name", default="dev-v2")
    args = ap.parse_args()
    out = run(args.run_dirs, frozenset(d.strip() for d in args.documents.split(",") if d.strip()), args.name)
    print(json.dumps({p: {v: (s["rate@8"], s["rate@16"]) for v, s in prof["variants"].items()}
                      for p, prof in out["profiles"].items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
