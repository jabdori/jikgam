"""개발용 v2 로그에서 최종 법령 거르기 구성을 (m3)와 (m4) 중에서 고른다(docs/v2-law-final.md의 사전 규칙).

측정 방식은 jikgam/v2/law_meta.py와 같고, 그 실행의 구간 메타데이터(results/v2/law-meta/dev-v2/segments.json)와 (m3)의 Jev 주제
답(저장소 밖 호출 기록)을 그대로 다시 쓴다. 새 유료 호출은 (m4)의 Jev 주제 판단뿐이다.

- m3: 조직 업종 + Jev 객관식 주제 하나(확률 0.5 이상일 때만). law_meta와 같다
- m4: 조직 업종 + Jev 주제별 예/아니오(주제 20개를 한 요청에 질문 20개로). 확률 0.5 이상인 주제를 모두 남기고, 구간에 그 가운데
  하나라도 붙어 있으면 남긴다. 하나도 없으면 주제로 거르지 않는다. 객관식은 확률의 합이 1이라 0.5 이상이 많아야 하나이므로
  "0.5 이상인 주제를 모두"를 하려면 주제마다 따로 물어야 한다
- 참고(고르는 규칙에 쓰지 않음) m3_prev, m4_prev: 주제를 그 검색 시점의 대화가 아니라 직전 판단의 대화(같은 세션에서 앞선,
  대화가 다른 마지막 검색 시점)로 정한 것. 주제 질문을 엔진의 기존 판별 요청에 더하면 검색 때 쓸 수 있는 것은 직전 판단이다
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

from jikgam.engine.law import bundle, load_law
from jikgam.engine.org import ROOT
from jikgam.sim.metrics import percentile_inf
from jikgam.v2.e2e_queries import found
from jikgam.v2.law_link import Calls, events
from jikgam.v2.law_meta import JEV_THRESHOLD, TOPIC_TASK, topic_request, with_reserved
from jikgam.v2.law_variants import ranking

KS = (8, 16)
CONFIGS = ("a", "m3", "m4", "m3_prev", "m4_prev")


def multi_topic_request(dialogue_text: str, values: dict) -> tuple[dict, dict, dict[str, str]]:
    keys = {f"t{i + 1}": v["value"] for i, v in enumerate(values["주제"])}
    state = {"task": TOPIC_TASK.replace("주제 하나를 고른다", "주제를 모두 고른다"), "dialogue": dialogue_text}
    questions = {k: {"type": "noul", "instructions": f"지금 대화가 '{v['value']}' 주제를 다루는가? ({v['definition']})"}
                 for k, v in zip(keys, values["주제"])}
    return state, questions, keys


async def multi_all(jobs: list[tuple[str, dict, dict]], calls: Calls) -> None:
    from jikgam.decision.registry import make_client
    from jikgam.v2.retry import decide

    todo = [j for j in jobs if not calls.done(j[0])]
    if not todo:
        return
    jev = make_client("jev")
    try:
        await decide(jev, {"warm": "예열"}, {"w": {"type": "noul", "instructions": "예열인가?"}})  # 측정에서 뺀다
        for i, (key, state, questions) in enumerate(todo, 1):
            r = await decide(jev, state, questions)
            calls.add({"key": key, "kind": "topics", "answers": {q: (r.answers.get(q) or {}).get("noul") for q in questions}
                       if not r.error else None, "latency_ms": r.latency_ms if not r.error else None, "error": r.error,
                       "tokens": r.input_tokens, "cost_usd": r.cost_usd})
            if i % 50 == 0:
                print(f"주제 여러 개 {i}/{len(todo)}", flush=True)
    finally:
        await jev.aclose()


def run(run_dirs: list[Path], name: str, only_estimate: bool) -> dict:
    meta_dir = ROOT / "results/v2/law-meta" / name
    values = json.loads((ROOT / "results/v2/law-meta/values.json").read_text(encoding="utf-8"))
    seg = json.loads((meta_dir / "segments.json").read_text(encoding="utf-8"))
    org_ind = set(json.loads((meta_dir / "report.json").read_text(encoding="utf-8"))["org_industries"])
    m3_calls = Calls(ROOT / "data/v2/local/law-meta" / name / "calls.jsonl")
    rows = list(events(run_dirs, set()))
    dialogues = sorted({r["dialogue"] for r in rows})
    jobs = []
    for d in dialogues:
        state, questions, _ = multi_topic_request(d, values)
        jobs.append((Calls.key("topics", d), state, questions))
    from jikgam.decision.registry import load_config

    chars = sum(len(json.dumps(s, ensure_ascii=False)) + len(json.dumps(q, ensure_ascii=False)) for _, s, q in jobs)
    est = {"m4_calls": len(jobs), "m4_chars": chars, "est_cost_usd": round(chars * load_config()["jev"]["price_input_per_mtok"] / 1e6, 3)}
    if only_estimate:
        return {"events": len(rows), "estimate": est}
    local = ROOT / "data/v2/local/law-final" / name
    local.mkdir(parents=True, exist_ok=True)
    calls = Calls(local / "calls.jsonl")
    asyncio.run(multi_all(jobs, calls))
    keys = {f"t{i + 1}": v["value"] for i, v in enumerate(values["주제"])}

    def m3_topics(d: str) -> set[str] | None:
        row = m3_calls.rows.get(Calls.key("topic", d)) or {}
        if row.get("error") or not row.get("choice"):
            return None
        p = float((row.get("probabilities") or {}).get(row["choice"], 0.0))
        return {keys[row["choice"]]} if p >= JEV_THRESHOLD else None

    def m4_topics(d: str) -> set[str] | None:
        row = calls.rows.get(Calls.key("topics", d)) or {}
        if row.get("error") or not row.get("answers"):
            return None
        chosen = {keys[q] for q, p in row["answers"].items() if p is not None and p >= JEV_THRESHOLD}
        return chosen or None

    law = bundle(load_law("r4"))
    stats = {c: {"items": 0, **{f"found@{k}": 0 for k in KS}, "topic_filtered_events": 0, "gold_dropped_items": 0,
                 "gold_dropped_events": 0} for c in CONFIGS}
    sess, cache, prev_dialogue = {}, {}, {}
    chosen_counts = []
    for r in rows:
        if r["query"] not in cache:
            cache[r["query"]] = ranking(law, r["query"])
        ranked = cache[r["query"]]
        # 직전 판단의 대화: 같은 세션에서 앞선, 대화가 다른 마지막 검색 시점
        hist = prev_dialogue.setdefault(r["session"], [])
        before = next((d for d in reversed(hist) if d != r["dialogue"]), None)
        if not hist or hist[-1] != r["dialogue"]:
            hist.append(r["dialogue"])
        topics = {"m3": m3_topics(r["dialogue"]), "m4": m4_topics(r["dialogue"]),
                  "m3_prev": m3_topics(before) if before else None, "m4_prev": m4_topics(before) if before else None}
        if topics["m4"]:
            chosen_counts.append(len(topics["m4"]))
        keep_ind = {c for c in ranked if org_ind & set(seg[c]["적용 업종"])}
        s = sess.setdefault(r["session"], {"gold": r["gold"], **{c: set() for c in CONFIGS}})
        for c in CONFIGS:
            st = stats[c]
            if c == "a":
                order = ranked
            else:
                t = topics[c]
                keep = keep_ind & {x for x in ranked if t & set(seg[x]["주제"])} if t else keep_ind
                if t:
                    st["topic_filtered_events"] += 1
                order = with_reserved([x for x in ranked if x in keep], ranked)
                dropped = sum(1 for ok, all_ok in zip(found(r["gold"], sorted(keep)), found(r["gold"], ranked)) if all_ok and not ok)
                st["gold_dropped_items"] += dropped
                st["gold_dropped_events"] += dropped > 0
            st["items"] += len(r["gold"])
            for k in KS:
                st[f"found@{k}"] += sum(found(r["gold"], order[:k]))
            s[c].update(order[:8])
    sessions = {c: {"items": 0, "found@8": 0} for c in CONFIGS}
    for s in sess.values():
        for c in CONFIGS:
            sessions[c]["items"] += len(s["gold"])
            sessions[c]["found@8"] += sum(found(s["gold"], sorted(s[c])))
    for block in (stats, sessions):
        for st in block.values():
            for k in KS:
                if f"found@{k}" in st:
                    st[f"rate@{k}"] = round(st[f"found@{k}"] / st["items"], 4)
    for c in CONFIGS:
        stats[c]["topic_filtered_rate"] = round(stats[c]["topic_filtered_events"] / len(rows), 4)
    lat = [calls.rows[k]["latency_ms"] for k, *_ in jobs if calls.rows[k].get("latency_ms") is not None]
    m3_lat = [m3_calls.rows[Calls.key("topic", d)]["latency_ms"] for d in dialogues
              if m3_calls.rows.get(Calls.key("topic", d), {}).get("latency_ms") is not None]
    pick = "m4" if stats["m4"]["found@8"] > stats["m3"]["found@8"] else "m3"  # 사전 규칙: 높은 쪽, 같으면 m3
    report = {"profile": "r4", "events": len(rows), "estimate": est, "configs": stats, "sessions_top8": sessions,
              "m4_topics_per_dialogue_mean": round(sum(chosen_counts) / max(len(chosen_counts), 1), 2),
              "jev_latency_ms": {"m3": {"p50": round(percentile_inf(m3_lat, 50), 1), "p95": round(percentile_inf(m3_lat, 95), 1)},
                                 "m4": {"p50": round(percentile_inf(lat, 50), 1), "p95": round(percentile_inf(lat, 95), 1)}},
              "m4_calls": len(jobs), "m4_failed": sum(1 for k, *_ in jobs if calls.rows[k].get("error")),
              "m4_cost_usd": round(sum(calls.rows[k].get("cost_usd") or 0 for k, *_ in jobs), 4), "final": pick}
    dest = ROOT / "results/v2/law-final" / name
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dirs", nargs="+", type=Path)
    ap.add_argument("--name", default="dev-v2")
    ap.add_argument("--estimate", action="store_true")
    args = ap.parse_args()
    out = run(args.run_dirs, args.name, args.estimate)
    print(json.dumps(out if args.estimate else {"final": out["final"], **{c: (s["rate@8"], s["rate@16"]) for c, s in out["configs"].items()}},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
