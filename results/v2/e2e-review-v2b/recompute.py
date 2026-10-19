"""R5 확정 측정(v2b) 독립 재계산. 보고기 코드(e2e_report, e2e_check)와 측정 보고를 쓰지 않는다.
고정 사본(v2-e2e-0030)에서 `uv run python <이 파일>`로 돌린다. 결과는 이 파일 옆 recompute.json에 쓴다."""

import hashlib
import json
import math
import random
import sys
from pathlib import Path

ROOT = Path.cwd()
OUT = Path(__file__).resolve().parent
sys.stdout.reconfigure(encoding="utf-8")

from jikgam.ingest import chunk as chunking  # noqa: E402
from jikgam.ingest.corpus import load_docs  # noqa: E402
from jikgam.sim import judge as J  # noqa: E402
from jikgam.sim.metrics import percentile_inf, refresh_latencies  # noqa: E402
from jikgam.sim.scenario import load_scenarios  # noqa: E402
from jikgam.v2 import e2e_claims as C  # noqa: E402

CFG = ("r0", "r5")
RUN = {c: f"holdout-v2b-{c}" for c in CFG}
SCN_FILE = ROOT / "data/scenarios/org_a_v2b_holdout.jsonl"
scenarios = {s["id"]: s for s in load_scenarios(SCN_FILE)}
SCN_IDS = sorted(scenarios)
assert len(SCN_IDS) == 24, len(SCN_IDS)
DOCS = load_docs()


def sha(b):
    return hashlib.sha256(b).hexdigest()


def jl(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ---------- 원본 해시 확인 ----------
hash_check = {}
for c in CFG:
    priv = ROOT / "data/v2/local/e2e" / RUN[c] / "logs"
    pub = ROOT / "results/engine/scenarios" / RUN[c]
    man = json.loads((pub / "private-logs.json").read_text(encoding="utf-8"))
    files = man["files"]
    on_disk = sorted(p.name for p in priv.iterdir() if p.is_file())
    bad_priv = [n for n, e in files.items() if not (priv / n).is_file() or sha((priv / n).read_bytes()) != e["sha256"]]
    bad_pub = [n for n, e in files.items() if not (pub / n).is_file()
               or sha((pub / n).read_bytes()) != e.get("public_sha256", e["sha256"])]
    hash_check[c] = {"manifest_files": len(files), "private_checked": len(files) - len(bad_priv),
                     "private_mismatch": bad_priv, "public_checked": len(files) - len(bad_pub),
                     "public_mismatch": bad_pub, "on_disk_not_in_manifest": [n for n in on_disk if n not in files],
                     "manifest_not_on_disk": [n for n in files if n not in on_disk]}
    assert not bad_priv, bad_priv

# ---------- 법령 근거 덮음(직접 구현) ----------
_chunk_cache = {}


def chunk_by_id(cid):
    name = cid.split(":")[0]
    if name not in _chunk_cache:
        _chunk_cache[name] = {x.id: x for x in chunking.load(name)}
    return _chunk_cache[name].get(cid)


def nonws_positions(doc, a, b):
    t = DOCS[doc].text
    return [i for i in range(a, min(b, len(t))) if not t[i].isspace()]


def law_found(law_items, cited, ignore_bar=False):
    """cited(인용 id 집합)의 법령 구간 합집합이 항목마다 대체 위치 하나를 공백 제외 글자 절반 이상 덮는가."""
    spans = {}
    for cid in cited:
        if cid.count(":") != 2:
            continue
        ch = chunk_by_id(cid)
        if ch is None:
            continue
        spans.setdefault(ch.doc, []).append((ch.start, ch.end))
    res = []
    for it in law_items:
        ok = False
        for alt in it["alternatives"]:
            t = DOCS[alt["doc"]].text
            pos = [i for i in range(alt["start"], min(alt["end"], len(t)))
                   if not t[i].isspace() and not (ignore_bar and t[i] == "|")]
            if not pos:
                continue
            sp = spans.get(alt["doc"], [])
            cov = sum(any(a <= i < b for a, b in sp) for i in pos)
            if cov * 2 >= len(pos):
                ok = True
                break
        res.append(ok)
    return res


# ---------- 세션별 계산 ----------
def load_cfg(c):
    priv = ROOT / "data/v2/local/e2e" / RUN[c] / "logs"
    summ = json.loads((priv / "summary.json").read_text(encoding="utf-8"))
    pubsum = json.loads((ROOT / "results/engine/scenarios" / RUN[c] / "summary.json").read_text(encoding="utf-8"))
    claim_dir = ROOT / "data/v2/local/e2e" / RUN[c]
    model = summ["judge_model"]
    sessions = []
    used_claim_files = set()
    for s in summ["sessions"]:
        sid, scn_id = s["session"], s["scenario"]
        scn = scenarios[scn_id]
        events = jl(priv / f"{sid}.jsonl")
        sim = jl(priv / f"{sid}.sim.jsonl")
        sim_end = next((e for e in reversed(sim) if e["type"] == "sim.end"), None)
        jpath = priv / f"{sid}.judge.json"
        ai = json.loads(jpath.read_text(encoding="utf-8")) if jpath.exists() else None
        ev = J.evaluate(scn, events, ai, sim_end)
        comp = ev["compliance"]
        valid = comp["valid"]
        # 노출 제안의 인용 합집합
        created = {e["suggestion_id"]: e for e in events if e["type"] == "suggestion.created"}
        exposed = [e["suggestion_id"] for e in events if e["type"] == "suggestion.exposed"]
        cited = set()
        for sg in exposed:
            cited |= set(created[sg].get("cited_articles") or [])
        law = scn["expected"].get("law_evidence") or []
        found = law_found(law, cited)
        found_nobar = law_found(law, cited, ignore_bar=True)
        # 주장 판정
        rows = list(C.exposed_inputs(events))
        claims = {"excluded": 0, "suggestions": 0, "judged": 0, "undecided": 0, "claims": 0, "unsupported": 0,
                  "cost": 0.0}
        for r in rows:
            if r.get("excluded"):
                claims["excluded"] += 1
                continue
            claims["suggestions"] += 1
            key = C.fingerprint(r, model)
            p = claim_dir / f"{key}.json"
            if not p.exists():
                claims["undecided"] += 1
                continue
            used_claim_files.add(p.name)
            rec = json.loads(p.read_text(encoding="utf-8"))
            claims["cost"] += rec.get("usage", {}).get("cost_usd", 0) or 0
            if rec.get("hash") != key or rec.get("suggestion_id") != r["suggestion_id"]:
                claims["undecided"] += 1
                continue
            if not C.cache_valid(rec):
                claims["undecided"] += 1
                continue
            claims["judged"] += 1
            claims["claims"] += len(rec["claims"])
            claims["unsupported"] += sum(1 for x in rec["claims"] if not x["supported"])
        # 갱신 지연
        lat = refresh_latencies(events)
        # 비용
        cost = {"customer": sum(((e.get("llm") or {}).get("cost_usd") or 0) for e in sim if e["type"] == "customer.turn"),
                "decision": sum(e.get("cost_usd") or 0 for e in events if e["type"] == "decision.response"),
                "warmup": sum(e.get("cost_usd") or 0 for e in events if e["type"] == "warmup"),
                "generation": sum(e.get("cost_usd") or 0 for e in events if e["type"] == "generation.response"),
                "judge": (ai or {}).get("cost_usd") or 0}
        sessions.append({
            "session": sid, "scenario": scn_id, "valid": valid, "pass": ev["pass"],
            "items": {n: ev["items"][n]["ok"] for n in J.ITEMS},
            "comp_reasons_n": len(comp["reasons"]),
            "law_items": len(law), "law_found": sum(found), "law_found_nobar": sum(found_nobar),
            "n_exposed": len(exposed), "claims": claims, "latencies": [x["latency_ms"] for x in lat],
            "cost": cost, "previous_judgments": (ai or {}).get("previous_judgments", 0),
            "judge_error": (ai or {}).get("error"), "has_judge": ai is not None,
            "scorer_law": ((ev["aux"].get("law") or {}).get("law_items"), (ev["aux"].get("law") or {}).get("law_found")),
        })
    all_claim_files = sorted(p.name for p in claim_dir.glob("*.json") if len(p.stem) == 64 and p.name != "summary.json")
    orphans = [n for n in all_claim_files if n not in used_claim_files]
    orphan_cost = 0.0
    for n in orphans:
        rec = json.loads((claim_dir / n).read_text(encoding="utf-8"))
        orphan_cost += rec.get("usage", {}).get("cost_usd", 0) or 0
    hist = sorted(p.name for p in (priv / "history").glob("*.judge.json")) if (priv / "history").exists() else []
    return {"sessions": sessions, "orphan_claim_files": len(orphans), "orphan_claim_cost": orphan_cost,
            "claim_files": len(all_claim_files), "history": hist,
            "summary_sessions": len(summ["sessions"]), "public_summary_sessions": len(pubsum["sessions"])}


data = {c: load_cfg(c) for c in CFG}


# ---------- 집계 ----------
def pct(a, b):
    return None if b == 0 else a / b


def agg(sessions):
    v = [s for s in sessions if s["valid"] is True]
    out = {"sessions": len(sessions), "valid": len(v),
           "invalid": sum(s["valid"] is False for s in sessions),
           "unjudged": sum(s["valid"] is None for s in sessions),
           "passed": sum(s["pass"] is True for s in v), "pass_failed": sum(s["pass"] is False for s in v),
           "pass_undetermined": sum(s["pass"] is None for s in v)}
    out["pass_rate"] = pct(out["passed"], out["valid"])
    out["item_fail_valid"] = {n: sum(s["items"][n] is False for s in v) for n in J.ITEMS}
    out["item_undetermined_valid"] = {n: sum(s["items"][n] is None for s in v) for n in J.ITEMS}
    out["item_fail_all"] = {n: sum(s["items"][n] is False for s in sessions) for n in J.ITEMS}
    out["law_items"] = sum(s["law_items"] for s in v)
    out["law_found"] = sum(s["law_found"] for s in v)
    out["law_found_nobar"] = sum(s["law_found_nobar"] for s in v)
    out["law_rate"] = pct(out["law_found"], out["law_items"])
    cl = [s["claims"] for s in v]
    out["claims"] = sum(x["claims"] for x in cl)
    out["unsupported"] = sum(x["unsupported"] for x in cl)
    out["unsupported_rate"] = pct(out["unsupported"], out["claims"])
    out["claim_suggestions"] = sum(x["suggestions"] for x in cl)
    out["claim_undecided"] = sum(x["undecided"] for x in cl)
    out["claim_excluded"] = sum(x["excluded"] for x in cl)
    out["claim_undecided_all_sessions"] = sum(s["claims"]["undecided"] for s in sessions)
    for tag, group in (("valid", v), ("all", sessions)):
        lat = [x for s in group for x in s["latencies"]]
        vals = [math.inf if x is None else x for x in lat]
        out[f"refresh_{tag}"] = {"events": len(vals), "misses": sum(math.isinf(x) for x in vals),
                                 "p50": percentile_inf(vals, 50), "p95": percentile_inf(vals, 95)}
    cost = {k: sum(s["cost"][k] for s in sessions) for k in ("customer", "decision", "warmup", "generation", "judge")}
    cost["claims"] = sum(s["claims"]["cost"] for s in sessions)
    cost["total"] = sum(cost.values())
    out["cost"] = cost
    return out


summary = {}
for c in CFG:
    a = agg(data[c]["sessions"])
    a["orphan_claim_files"] = data[c]["orphan_claim_files"]
    a["orphan_claim_cost"] = data[c]["orphan_claim_cost"]
    a["claim_files"] = data[c]["claim_files"]
    a["history_judge_files"] = data[c]["history"]
    a["scorer_mismatch_sessions"] = [s["session"] for s in data[c]["sessions"]
                                     if s["law_items"] and s["scorer_law"] != (s["law_items"], s["law_found"])]
    a["rejudged_sessions"] = [s["session"] for s in data[c]["sessions"] if s["previous_judgments"]]
    a["judge_error_sessions"] = [s["session"] for s in data[c]["sessions"] if s["judge_error"]]
    summary[c] = a

# ---------- 부트스트랩 ----------
per = {c: {} for c in CFG}
for c in CFG:
    for sc in SCN_IDS:
        v = [s for s in data[c]["sessions"] if s["scenario"] == sc and s["valid"] is True]
        per[c][sc] = {"n": len(v), "pass": sum(s["pass"] is True for s in v),
                      "cl": sum(s["claims"]["claims"] for s in v), "un": sum(s["claims"]["unsupported"] for s in v),
                      "li": sum(s["law_items"] for s in v), "lf": sum(s["law_found"] for s in v)}
rng = random.Random(20261016)
N = 10000
draws = {m: {"r0": [], "r5": [], "diff": []} for m in ("pass", "claims", "law")}
spec = {"pass": ("pass", "n"), "claims": ("un", "cl"), "law": ("lf", "li")}
for _ in range(N):
    pick = rng.choices(SCN_IDS, k=len(SCN_IDS))
    for m, (num, den) in spec.items():
        vals = {}
        for c in CFG:
            d = sum(per[c][s][den] for s in pick)
            vals[c] = None if d == 0 else sum(per[c][s][num] for s in pick) / d
        if vals["r0"] is None or vals["r5"] is None:
            continue
        draws[m]["r0"].append(vals["r0"])
        draws[m]["r5"].append(vals["r5"])
        draws[m]["diff"].append(vals["r5"] - vals["r0"])
boot = {}
for m in draws:
    boot[m] = {"used_draws": len(draws[m]["diff"]), "excluded_draws": N - len(draws[m]["diff"])}
    for k in ("r0", "r5", "diff"):
        xs = draws[m][k]
        boot[m][k] = [percentile_inf(xs, 2.5), percentile_inf(xs, 97.5)]

# ---------- 판정 ----------
def diff(m, key):
    a, b = summary["r0"][key], summary["r5"][key]
    return None if a is None or b is None else b - a


d_law, d_pass, d_claim = diff("law", "law_rate"), diff("pass", "pass_rate"), diff("claims", "unsupported_rate")
verdict = {
    "primary_law": {"r0": summary["r0"]["law_rate"], "r5": summary["r5"]["law_rate"], "diff": d_law,
                    "ci_diff": boot["law"]["diff"],
                    "met": d_law is not None and d_law >= 0.10 and boot["law"]["diff"][0] > 0},
    "pass_rate": {"r0": summary["r0"]["pass_rate"], "r5": summary["r5"]["pass_rate"], "diff": d_pass,
                  "ci_diff": boot["pass"]["diff"], "met": d_pass is not None and d_pass >= -0.05},
    "unsupported_claims": {"r0": summary["r0"]["unsupported_rate"], "r5": summary["r5"]["unsupported_rate"],
                           "diff": d_claim, "ci_diff": boot["claims"]["diff"],
                           "met": summary["r5"]["unsupported_rate"] <= summary["r0"]["unsupported_rate"]},
    "refresh_p95_r5": {"value": summary["r5"]["refresh_valid"]["p95"],
                       "met": summary["r5"]["refresh_valid"]["p95"] <= 500},
}
verdict["pass_rate"]["note"] = "점추정 -5%p 이상이면 나빠지지 않음"
cost_total = {k: sum(summary[c]["cost"][k] for c in CFG) for k in summary["r0"]["cost"]}

def clean(o):
    if isinstance(o, float) and math.isinf(o):
        return "inf"
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean(v) for v in o]
    return o

result = {"hash_check": hash_check, "summary": summary, "bootstrap": boot, "verdict": verdict, "cost_total": cost_total,
          "per_session": {c: [{k: s[k] for k in ("session", "scenario", "valid", "pass", "law_items", "law_found",
                                                  "law_found_nobar", "n_exposed", "previous_judgments")}
                              | {"claims": s["claims"]["claims"], "unsupported": s["claims"]["unsupported"],
                                 "claim_undecided": s["claims"]["undecided"]}
                              for s in data[c]["sessions"]] for c in CFG},
          "counts": {c: {"summary_sessions": data[c]["summary_sessions"],
                         "public_summary_sessions": data[c]["public_summary_sessions"]} for c in CFG}}
(OUT / "recompute.json").write_text(json.dumps(clean(result), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
print(json.dumps(clean({"summary": summary, "bootstrap": boot, "verdict": verdict, "hash": {c: {k: (v if not isinstance(v, list) else len(v)) for k, v in hash_check[c].items()} for c in CFG}}), ensure_ascii=False, indent=1))
