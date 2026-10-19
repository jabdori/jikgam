"""개발용 v2 로그에서 유사도 대신 Jev 판단으로 법령 구간을 연결하는 구성을 오프라인 비교한다.

엔진과 고정 설정은 바꾸지 않는다. 측정 단위는 jikgam/v2/law_variants.py와 같다: 엔진이 실제로 법령 검색을 한 시점(근거를 고른
retrieval.done)마다 그때까지의 대화로 R4 검색을 다시 돌려, 상위 8개(와 16위 안) 구간이 정답 법령 항목을 덮는지 본다. 기준과
합격선은 docs/v2-law-link.md에 실행 전에 적었다. 유료 호출은 Jev뿐이다.

- s1 약관-법령 연결표: 상담 전에 조직 A 약관 조항마다 R4 법령 구간을 16개씩 묶어 "이 구간이 이 조항의 근거이거나 같은 사안을
  다루는가"를 Jev로 묻고, 확률이 LINK_THRESHOLD 이상이면 연결한다. 상담 중에는 그 검색 시점에 쓸 수 있는 직전 근거 판단에서
  고른 약관 조항(applies 0.5 이상)의 연결 구간을 지금 검색 순서대로 먼저 넣고, 남은 자리를 지금 검색 순위로 채운다
- s2 문서 고르기: 검색 시점마다 대화와 10개 문서의 제목·요약만 보여 주고 문서마다 "지금 대화에 적용되는가"를 Jev로 묻는다.
  확률이 DOC_THRESHOLD 이상인 문서 안에서만 지금 검색 순위를 쓴다
- s3 = s1 + s2: 연결 구간을 먼저 넣고 남은 자리를 s2의 문서 안 검색 순위로 채운다
- 보조 s1_same: s1을 같은 턴의 근거 판단(검색 뒤에 나온 것)으로 연결한다. 엔진에서는 판별을 한 번 더 해야 하는 형태라 판정에 쓰지 않는다

판단 결과가 없으면(세션의 첫 검색, 고른 조항이나 문서가 없음) 지금 검색 순위를 쓰고 그 수를 센다.
"""

import argparse
import asyncio
import hashlib
import json
import random
import time
from pathlib import Path

from jikgam.engine.law import bundle, load_law
from jikgam.engine.org import ROOT, load_org
from jikgam.engine.retrieval import parse_terms
from jikgam.sim.metrics import percentile_inf
from jikgam.sim.scenario import load_scenarios
from jikgam.v2.e2e_logs import read_log
from jikgam.v2.e2e_queries import found
from jikgam.v2.law_rewrite import dialogue
from jikgam.v2.law_variants import SCENARIOS, frozen_query, in_scope, ranking

KS = (8, 16)
CONFIGS = ("a", "s1", "s2", "s3", "s1_same")
BATCH = 16
LINK_THRESHOLD = 0.5
DOC_THRESHOLD = 0.5
SAMPLE_SEED = 20261019
SAMPLE_SIZE = 20
EST_TOKENS_PER_CHAR = 1.0
LINK_TASK = "약관 조항과 법령·고시·표준약관 구간을 비교한다. 구간마다 그 구간이 이 약관 조항의 근거이거나 같은 사안을 다루는지 판단한다."
DOC_TASK = "고객센터 상담 대화에 적용되는 문서를 고른다. 문서마다 제목과 요약만 보고, 그 문서가 지금 대화에 적용되는지 판단한다."


def link_batches(articles: dict, law) -> list[tuple[str, list[str]]]:
    ids = [c.id for c in law.cs.chunks]
    return [(a, ids[i:i + BATCH]) for a in articles for i in range(0, len(ids), BATCH)]


def link_request(article, law, seg_ids: list[str]) -> tuple[dict, dict]:
    cs = law.cs
    state = {"task": LINK_TASK, "article": {"id": article.id, "title": article.title, "text": article.text},
             "candidates": [{"id": f"c{i + 1}", "document": cs.docs[cs.by_id[c].doc].title, "text": cs.text(cs.by_id[c])}
                            for i, c in enumerate(seg_ids)]}
    questions = {f"c{i + 1}": {"type": "noul", "instructions": f"구간 c{i + 1}은 이 약관 조항의 근거이거나 같은 사안을 다루는가?"}
                 for i in range(len(seg_ids))}
    return state, questions


def doc_request(dialogue_text: str, summaries: dict, doc_ids: list[str]) -> tuple[dict, dict]:
    state = {"task": DOC_TASK, "dialogue": dialogue_text,
             "documents": [{"id": f"d{i + 1}", "title": summaries[d]["title"], "summary": summaries[d]["summary"]}
                           for i, d in enumerate(doc_ids)]}
    questions = {f"d{i + 1}": {"type": "noul", "instructions": f"문서 d{i + 1}은 지금 대화에 적용되는가?"} for i in range(len(doc_ids))}
    return state, questions


def chars(state: dict, questions: dict) -> int:
    return len(json.dumps(state, ensure_ascii=False)) + len(json.dumps(questions, ensure_ascii=False))


def linked_first(linked: set[str], ranked: list[str], fill: list[str]) -> list[str]:
    """연결 구간을 지금 검색 순서대로 먼저 두고, 남은 자리를 fill(지금 검색 순위)로 채운다."""
    pos = {c: i for i, c in enumerate(ranked)}
    head = sorted(linked, key=lambda c: (pos.get(c, len(pos)), c))
    return head + [c for c in fill if c not in linked]


def events(run_dirs: list[Path], org_articles: set[str]):
    """근거를 고른 검색 이벤트마다 그 시점의 대화와, 직전(검색 전)·같은 턴(검색 뒤) 근거 판단에서 고른 약관 조항을 낸다."""
    scenarios = {s["id"]: s for s in load_scenarios(ROOT / SCENARIOS)}
    for directory in run_dirs:
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        if summary.get("scenario_file") != SCENARIOS or summary["law"] != "r4":
            raise ValueError("개발용 v2 시나리오의 R4 실행만 지원합니다")
        org = load_org("org_a")
        for sess in summary["sessions"]:
            gold = scenarios[sess["scenario"]]["expected"].get("law_evidence") or []
            if not gold:
                continue
            es = read_log(directory / f"{sess['session']}.jsonl")
            selected_by_version: dict[int, list[str]] = {}
            for e in es:
                if e["type"] == "evidence.updated":
                    selected_by_version[e["version"]] = sorted(a for a, p in (e.get("applies") or {}).items()
                                                               if a in org_articles and p >= org.applies_threshold)
            texts = {e["suggestion_id"]: e.get("text", "") for e in es if e["type"] == "suggestion.created"}
            customer, turns, intent_label, prev = [], [], None, None
            for e in es:
                if e["type"] == "utterance.end" and e.get("speaker", "customer") == "customer":
                    customer.append(e["text"])
                    turns.append(("customer", e["text"]))
                if e["type"] == "suggestion.exposed" and e.get("suggestion_id") in texts:
                    turns.append(("agent", texts[e["suggestion_id"]]))
                if e["type"] == "evidence.updated":
                    intent_label = e.get("intent")
                    prev = selected_by_version[e["version"]]
                if e["type"] == "retrieval.done" and e["version"] in selected_by_version:
                    yield {"run": summary["run_id"], "scenario": sess["scenario"], "session": sess["session"], "version": e["version"],
                           "gold": gold, "query": frozen_query(org, customer, intent_label), "dialogue": dialogue(turns),
                           "prev_selected": prev, "same_selected": selected_by_version[e["version"]],
                           "logged_candidates": e.get("law_candidates"), "logged_query": e.get("law_query")}


class Calls:
    """Jev 호출 기록(저장소 밖). 같은 요청은 다시 부르지 않는다."""

    def __init__(self, path: Path):
        self.path = path
        self.rows: dict[str, dict] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                self.rows[row["key"]] = row

    @staticmethod
    def key(kind: str, *parts: str) -> str:
        return hashlib.sha256("|".join([kind, *parts]).encode("utf-8")).hexdigest()

    def done(self, key: str) -> bool:
        return key in self.rows and not self.rows[key].get("error")

    def add(self, row: dict) -> None:
        self.rows[row["key"]] = row
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


async def call_all(jobs: list[tuple[str, dict, dict, dict]], calls: Calls, label: str) -> float:
    """jobs: (키, 상태, 질문, 함께 남길 값). 하나씩 차례로 부른다. 걸린 전체 시간(초)을 돌려준다."""
    from jikgam.decision.registry import make_client
    from jikgam.v2.retry import decide

    todo = [j for j in jobs if not calls.done(j[0])]
    if not todo:
        return 0.0
    jev = make_client("jev")
    t0 = time.perf_counter()
    try:
        await decide(jev, {"warm": "예열"}, {"w": {"type": "noul", "instructions": "예열인가?"}})  # 측정에서 뺀다
        t0 = time.perf_counter()
        for i, (key, state, questions, extra) in enumerate(todo, 1):
            r = await decide(jev, state, questions)
            calls.add({"key": key, **extra, "answers": {q: (r.answers.get(q) or {}).get("noul") for q in questions} if not r.error else None,
                       "latency_ms": r.latency_ms if not r.error else None, "error": r.error, "tokens": r.input_tokens,
                       "cost_usd": r.cost_usd})
            if i % 50 == 0:
                print(f"{label} {i}/{len(todo)}", flush=True)
    finally:
        await jev.aclose()
    return time.perf_counter() - t0


def run(run_dirs: list[Path], summaries_path: Path, name: str, only_estimate: bool) -> dict:
    articles = parse_terms(ROOT / "domain/org_a/terms.md")
    law = bundle(load_law("r4"))
    summaries = json.loads(summaries_path.read_text(encoding="utf-8"))
    doc_ids = [d for d in law.cs.docs if d in summaries]
    if set(doc_ids) != {c.doc for c in law.cs.chunks}:
        raise ValueError("문서 요약이 R4 구간 세트의 문서와 다릅니다")
    rows = list(events(run_dirs, set(articles)))
    dialogues = sorted({r["dialogue"] for r in rows})
    link_jobs = []
    for a, seg_ids in link_batches(articles, law):
        state, questions = link_request(articles[a], law, seg_ids)
        link_jobs.append((Calls.key("link", a, *seg_ids), state, questions, {"kind": "link", "article": a, "segments": seg_ids}))
    doc_jobs = []
    for d in dialogues:
        state, questions = doc_request(d, summaries, doc_ids)
        doc_jobs.append((Calls.key("doc", d), state, questions, {"kind": "doc", "docs": doc_ids}))
    from jikgam.decision.registry import load_config

    price = load_config()["jev"]["price_input_per_mtok"]
    est = {"link_calls": len(link_jobs), "link_chars": sum(chars(s, q) for _, s, q, _ in link_jobs),
           "doc_calls": len(doc_jobs), "doc_chars": sum(chars(s, q) for _, s, q, _ in doc_jobs)}
    est["est_cost_usd"] = round((est["link_chars"] + est["doc_chars"]) * EST_TOKENS_PER_CHAR * price / 1e6, 3)
    if only_estimate:
        return {"events": len(rows), "estimate": est}
    local = ROOT / "data/v2/local/law-link" / name
    local.mkdir(parents=True, exist_ok=True)
    calls = Calls(local / "calls.jsonl")
    link_wall = asyncio.run(call_all(link_jobs, calls, "연결표"))
    doc_wall = asyncio.run(call_all(doc_jobs, calls, "문서 고르기"))
    # 연결표
    links: dict[str, dict[str, float]] = {a: {} for a in articles}
    link_rows = [calls.rows[k] for k, *_ in link_jobs]
    for row in link_rows:
        if row.get("error"):
            continue
        for i, seg in enumerate(row["segments"]):
            p = row["answers"].get(f"c{i + 1}")
            if p is not None and p >= LINK_THRESHOLD:
                links[row["article"]][seg] = round(float(p), 4)
    # 문서 고르기
    picked: dict[str, list[str]] = {}
    doc_lat = []
    doc_failed = 0
    for key, _, _, extra in doc_jobs:
        row = calls.rows[key]
        d = next(x for x in dialogues if Calls.key("doc", x) == key)
        if row.get("error"):
            doc_failed += 1
            picked[d] = []
            continue
        doc_lat.append(row["latency_ms"])
        picked[d] = [doc for i, doc in enumerate(extra["docs"]) if (row["answers"].get(f"d{i + 1}") or 0.0) >= DOC_THRESHOLD]
    # 평가
    stats = {c: {"items": 0, **{f"found@{k}": 0 for k in KS}} for c in CONFIGS}
    sess_union = {}
    fallback = {"s1": 0, "s2": 0, "s1_same": 0}
    merge_ms: list[float] = []
    checked = same = 0
    cache: dict[str, list[str]] = {}
    for r in rows:
        if r["query"] not in cache:
            cache[r["query"]] = ranking(law, r["query"])
        ranked = cache[r["query"]]
        if r["run"].endswith("-3"):
            checked += 1
            same += ranked[:8] == r["logged_candidates"]
        t0 = time.perf_counter()
        linked = {s for a in (r["prev_selected"] or []) for s in links.get(a, {})}
        s1 = linked_first(linked, ranked, ranked) if linked else ranked
        merge_ms.append((time.perf_counter() - t0) * 1000)
        if not linked:
            fallback["s1"] += 1
        docs = frozenset(picked.get(r["dialogue"]) or [])
        if not docs:
            fallback["s2"] += 1
        s2 = in_scope(law, ranked, docs) if docs else ranked
        s3 = linked_first(linked, ranked, s2) if linked else s2
        linked_same = {s for a in r["same_selected"] for s in links.get(a, {})}
        if not linked_same:
            fallback["s1_same"] += 1
        s1s = linked_first(linked_same, ranked, ranked) if linked_same else ranked
        orders = {"a": ranked, "s1": s1, "s2": s2, "s3": s3, "s1_same": s1s}
        su = sess_union.setdefault(r["session"], {"gold": r["gold"], **{c: set() for c in CONFIGS}})
        for c, ids in orders.items():
            stats[c]["items"] += len(r["gold"])
            for k in KS:
                stats[c][f"found@{k}"] += sum(found(r["gold"], ids[:k]))
            su[c].update(ids[:8])
    sessions = {c: {"items": 0, "found@8": 0} for c in CONFIGS}
    for su in sess_union.values():
        for c in CONFIGS:
            sessions[c]["items"] += len(su["gold"])
            sessions[c]["found@8"] += sum(found(su["gold"], sorted(su[c])))
    for block in (stats, sessions):
        for s in block.values():
            for k in KS:
                if f"found@{k}" in s:
                    s[f"rate@{k}"] = round(s[f"found@{k}"] / s["items"], 4)
    # 사람이 확인할 무작위 연결 20개(저장소에는 id와 위치만, 본문은 저장소 밖)
    all_links = sorted((a, s, p) for a, segs in links.items() for s, p in segs.items())
    sample = random.Random(SAMPLE_SEED).sample(all_links, min(SAMPLE_SIZE, len(all_links)))
    cs = law.cs
    sample_rows = [{"article": a, "article_title": articles[a].title, "segment": s, "document": cs.docs[cs.by_id[s].doc].title,
                    "lines": f"{cs.by_id[s].start_line}~{cs.by_id[s].end_line}", "p": p} for a, s, p in sample]
    with (local / "links-sample.md").open("w", encoding="utf-8", newline="\n") as f:
        for i, x in enumerate(sample_rows, 1):
            f.write(f"## {i}. {x['article']} {x['article_title']} ↔ {x['segment']} ({x['document']} {x['lines']}줄, p={x['p']})\n\n"
                    f"약관 조항:\n\n{articles[x['article']].text}\n\n법령 구간:\n\n{cs.text(cs.by_id[x['segment']])}\n\n"
                    f"판단(예/아니오/모름): \n\n")
    lat = lambda xs: {"p50": round(percentile_inf(xs, 50), 3), "p95": round(percentile_inf(xs, 95), 3)} if xs else None  # noqa: E731
    report = {"profile": "r4", "runs": [d.as_posix() for d in run_dirs], "events": len(rows), "estimate": est,
              "logged_top8_checked": checked, "logged_top8_same": same,
              "link_table": {"articles": len(articles), "segments": len(cs.chunks), "pairs": len(articles) * len(cs.chunks),
                             "calls": len(link_rows), "failed": sum(1 for x in link_rows if x.get("error")),
                             "links": len(all_links), "articles_with_links": sum(1 for v in links.values() if v),
                             "cost_usd": round(sum(x.get("cost_usd") or 0 for x in link_rows), 4),
                             "wall_s": round(link_wall, 1),
                             "request_ms": lat([x["latency_ms"] for x in link_rows if x.get("latency_ms") is not None])},
              "doc_pick": {"calls": len(doc_jobs), "failed": doc_failed, "latency_ms": lat(doc_lat),
                           "cost_usd": round(sum(calls.rows[k].get("cost_usd") or 0 for k, *_ in doc_jobs), 4),
                           "docs_picked_mean": round(sum(len(v) for v in picked.values()) / max(len(picked), 1), 2)},
              "s1_merge_ms": lat(merge_ms), "fallback": fallback, "configs": stats, "sessions_top8": sessions,
              "links_sample": sample_rows}
    dest = ROOT / "results/v2/law-link" / name
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "links.json").write_text(json.dumps(links, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    (dest / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dirs", nargs="+", type=Path)
    ap.add_argument("--summaries", type=Path, default=ROOT / "results/v2/law-link/doc-summaries.json")
    ap.add_argument("--name", default="dev-v2")
    ap.add_argument("--estimate", action="store_true")
    args = ap.parse_args()
    out = run(args.run_dirs, args.summaries, args.name, args.estimate)
    print(json.dumps(out if args.estimate else {c: (s["rate@8"], s["rate@16"]) for c, s in out["configs"].items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
