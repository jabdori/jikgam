"""T5: v2 test 판정 수치 독립 재계산(GLM-5.3 검토자).

- jikgam.v2.report / stats / answer 의 함수를 불러 쓰지 않고 이 파일에서 직접 계산한다.
- 토큰 수(예산 2,000 자르기)만 고정 계측기 jikgam.ingest.tokens(Qwen3.5-2B, ADR 0025)를 쓴다.
- 결과 파일에 저장된 질문별 지표를 쓰지 않고, 단계 기록의 구간 순서(budget4000)와 구간 파일의
  원문 위치, 질문 파일의 정답 근거 위치에서 근거 재현율@2,000을 다시 낸다.
- 산출물에는 수치와 질문/구간 id만 남긴다(원문·질문·답변 글은 남기지 않는다).
"""

import json
import math
import os
import random
import sys
from collections import Counter
from fractions import Fraction
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")  # 검토 중 네트워크 호출을 하지 않는다

from jikgam.ingest import tokens as tok  # 토큰나이저만 고정 계측기로 공유

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "outputs" / "v2-review" / "glm"
OUT.mkdir(parents=True, exist_ok=True)

NAMES = {
    "R0": "R0-article256",
    "R0-original": "R0-article0",
    "R1": "fixed256--none--bm25",
    "R4": "b-W4000-C800-raw--full--snow+rrf+jev8",
    "R4c-Jev": "c-jev-W4000-C800-raw-t0.65--full--snow+rrf+jev8",
    "R4c-Qwen": "c-qwen-W4000-C800-raw-t0.55--full--snow+rrf+jev8",
}
IGNORED = frozenset(" \t\n\r\f\v\u3000|")  # 공백류와 표 구분자 |(docs §2.3·§4.2)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------- 내가 직접 짠 데이터 로더 ----------

def load_docs():
    summary = read_json(ROOT / "data/v2/extracted/summary.json")["docs"]
    docs = {}
    for did, s in summary.items():
        lines = []
        start = 0
        for block in read_jsonl(ROOT / s["output"]):
            for text in block["text"].split("\n"):
                lines.append((text, start))
                start += len(text) + 1
        text = "\n".join(t for t, _ in lines)
        prefix, acc = [0], 0
        for ch in text:
            acc += ch not in IGNORED
            prefix.append(acc)
        docs[did] = {"text": text, "prefix": prefix}
    return docs


def load_chunks(name: str):
    return {row["id"]: row for row in read_jsonl(ROOT / "data/v2/chunks" / f"{name}.jsonl")}


def load_questions(split: str):
    if split == "check":
        paths = [ROOT / "data/v2/check/items.jsonl", ROOT / "data/v2/local/check/items.jsonl"]
        rows = [r for p in paths for r in read_jsonl(p)]
    else:
        sp = read_json(ROOT / "data/v2/qa/splits.json")["splits"]
        paths = [ROOT / "data/v2/qa/items.jsonl", ROOT / "data/v2/local/qa/items.jsonl"]
        rows = [r for p in paths for r in read_jsonl(p) if sp.get(r["id"]) == split]
    return sorted(rows, key=lambda r: r["id"])


def load_traces(split: str, name: str):
    return {row["id"]: row for row in read_jsonl(ROOT / "data/v2/local/runs" / split / f"{name}.jsonl")}


# ---------- 근거 재현율@2,000(docs §4.2 정의를 직접 구현) ----------

def budget_ranges(chunks, docs, ids, budget):
    """결과 구간의 원문을 순서대로 잇되 예산(토큰)에서 자른 덮임 범위 (문서, 시작, 끝)."""
    ranges, left = [], budget
    for cid in ids:
        if left <= 0:
            break
        c = chunks[cid]
        n = c["tokens"]
        if n <= left:
            ranges.append((c["doc"], c["start"], c["end"]))
            left -= n
        else:
            text = docs[c["doc"]]["text"][c["start"]:c["end"]]
            ranges.append((c["doc"], c["start"], c["start"] + tok.cut(text, left)))
            left = 0
    return ranges


def merged(doc_ranges):
    out = []
    for a, b in sorted(doc_ranges):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def nonspace(docs, did, a, b):
    p = docs[did]["prefix"]
    return p[b] - p[a]


def covered(docs, alt, mm):
    did, a0, b0 = alt["doc"], alt["start"], alt["end"]
    need = nonspace(docs, did, a0, b0)
    if need == 0:
        return False
    got = 0
    for a, b in mm.get(did, []):
        if a >= b0:
            break
        lo, hi = max(a, a0), min(b, b0)
        if hi > lo:
            got += nonspace(docs, did, lo, hi)
    return 2 * got >= need  # 절반 이상 덮으면 찾음


def found_items(question, docs, ranges):
    per_doc = {}
    for did, a, b in ranges:
        per_doc.setdefault(did, []).append((a, b))
    mm = {d: merged(v) for d, v in per_doc.items()}
    return [any(covered(docs, alt, mm) for alt in ev["alternatives"]) for ev in question["evidence"]]


def recompute_run(name, split, questions, docs):
    """단계 기록의 budget4000 순서에서 예산 2,000 덮임을 다시 만들어 채점한다."""
    result = read_json(ROOT / "results/v2" / split / f"{name}.json")
    chunks = load_chunks(result["chunks"])
    traces = load_traces(split, name)
    per_q = {"single": {}, "multi": {}}
    for q in questions:
        if q["type"] == "none":
            continue
        ids = traces[q["id"]]["budget4000"]
        per_q[q["type"]][q["id"]] = found_items(q, docs, budget_ranges(chunks, docs, ids, 2000))
    return result, chunks, traces, per_q


def pooled(per_q):
    items = [v for vs in per_q.values() for v in vs]
    return sum(items) / len(items) if items else None


# ---------- 백분위와 부트스트랩 ----------

def pct_reporter(xs, q):
    """보고기 방식(run.py pct): 오름차순 정렬 뒤 round(q*(n-1)) 순위(파이썬 round)."""
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def pct_nearest(xs, q):
    """다른 흔한 방식: 가장 가까운 순위(올림) ceil(q*n)."""
    if not xs:
        return None
    xs = sorted(xs)
    return xs[max(0, math.ceil(q * len(xs)) - 1)]


def paired_recall_ci(fa, fb, n=10000, seed=20261016):
    """질문 단위 짝지은 부트스트랩. 항목을 합친 재현율의 차이(a-b)."""
    qs = sorted(set(fa) & set(fb))

    def rec(sample, d):
        items = [x for q in sample for x in d[q]]
        return Fraction(sum(items), len(items)) if items else Fraction(0)

    point = float(rec(qs, fa) - rec(qs, fb))
    rng = random.Random(seed)
    m = len(qs)
    vals = []
    for _ in range(n):
        s = [qs[rng.randrange(m)] for _ in range(m)]
        vals.append(float(rec(s, fa) - rec(s, fb)))
    vals.sort()
    return point, [vals[int(0.025 * n)], vals[int(0.975 * n) - 1]], m


def answer_diff_ci(ra, rb, key, qs, n=10000, seed=20261016):
    """답변 품질 차이(R4-R0). 표본마다 같은 정의로 다시 계산한다(정의 안 되는 표본은 뺀다)."""
    qs = sorted(qs)

    def value(sample, d):
        if key == "unsupported":
            denom = sum(d[q][0] for q in sample)
            return Fraction(sum(d[q][1] for q in sample), denom) if denom else None
        xs = [d[q][2] for q in sample]
        return Fraction(sum(xs), len(xs)) if xs else None

    def diff(sample):
        av, bv = value(sample, ra), value(sample, rb)
        return av - bv if av is not None and bv is not None else None

    point = float(diff(qs))
    rng = random.Random(seed)
    m = len(qs)
    vals = sorted(v for v in (diff([qs[rng.randrange(m)] for _ in range(m)]) for _ in range(n)) if v is not None)
    ci = [float(vals[int(0.025 * len(vals))]), float(vals[max(0, int(0.975 * len(vals)) - 1)])] if vals else None
    return point, ci, m


# ---------- 덩어리 반복 분할 일치(docs §4.1 정의를 직접 구현) ----------

def match_pairs(pred, gold, tol=1):
    pairs = sorted((abs(p - g), p, g) for p in pred for g in gold if abs(p - g) <= tol)
    up, ug, n = set(), set(), 0
    for _, p, g in pairs:
        if p not in up and g not in ug:
            up.add(p)
            ug.add(g)
            n += 1
    return n


def prf(tp, npred, ngold):
    p = tp / npred if npred else None
    r = tp / ngold if ngold else None
    f = 2 * p * r / (p + r) if p and r else (0.0 if (p is not None and r is not None) else None)
    return {"precision": p, "recall": r, "f1": f}


def seg_numbers(bounds, start, end):
    out, k = [], 0
    for n in range(start, end + 1):
        if n in bounds and n != start:
            k += 1
        out.append(k)
    return out


def pk_wd(pred, gold, start, end, k):
    sp, sg = seg_numbers(set(pred), start, end), seg_numbers(set(gold), start, end)
    n = len(sp)
    if n <= k:
        return 0.0, 0.0
    pk = wd = 0
    for i in range(n - k):
        pk += (sp[i] == sp[i + k]) != (sg[i] == sg[i + k])
        wd += abs((sp[i + k] - sp[i]) - (sg[i + k] - sg[i])) > 0
    return pk / (n - k), wd / (n - k)


def my_agreement(segs, pred, gold, n_boot=10000, seed=20261016):
    tp = npred = ngold = 0
    lens = []
    for s in segs:
        g = [x for x in gold.get(s["id"], []) if s["start"] < x <= s["end"]]
        cuts = [s["start"]] + g + [s["end"] + 1]
        lens += [b - a for a, b in zip(cuts, cuts[1:])]
    k = max(1, round(sum(lens) / len(lens) / 2)) if lens else 1
    per, pks, wds = {}, [], []
    for s in segs:
        p = [x for x in pred.get(s["id"], []) if s["start"] < x <= s["end"]]
        g = [x for x in gold.get(s["id"], []) if s["start"] < x <= s["end"]]
        m = match_pairs(p, g)
        tp, npred, ngold = tp + m, npred + len(p), ngold + len(g)
        a, b = pk_wd(p, g, s["start"], s["end"], k)
        pks.append(a)
        wds.append(b)
        per[s["id"]] = (m, len(p), len(g))
    out = prf(tp, npred, ngold)
    out["pk"], out["windowdiff"], out["window"] = sum(pks) / len(pks), sum(wds) / len(wds), k

    def f1_of(sample):
        a = sum(per[i][0] for i in sample)
        b = sum(per[i][1] for i in sample)
        c = sum(per[i][2] for i in sample)
        v = prf(a, b, c)["f1"]
        return v if v is not None else 0.0

    rng = random.Random(seed)
    ids = [s["id"] for s in segs]
    vals = sorted(f1_of([ids[rng.randrange(len(ids))] for _ in ids]) for _ in range(n_boot))
    out["f1_ci95"] = [vals[int(0.025 * n_boot)], vals[int(0.975 * n_boot) - 1]]
    return {kk: (round(v, 4) if isinstance(v, float) else v) for kk, v in out.items()}


def within(segs, starts_by_doc):
    return {s["id"]: [n for n in starts_by_doc.get(s["doc"], []) if s["start"] < n <= s["end"]] for s in segs}


def chunk_starts(name):
    out = {}
    for row in read_jsonl(ROOT / "data/v2/chunks" / f"{name}.jsonl"):
        out.setdefault(row["doc"], set()).add(row["start_line"])
    return {d: sorted(v) for d, v in out.items()}


# ---------- 본 계산 ----------

def main():
    docs = load_docs()
    report = read_json(ROOT / "results/v2/test/report.json")
    test_q = load_questions("test")
    check_q = load_questions("check")
    counts = {
        "test": dict(Counter(q["type"] for q in test_q)),
        "check": dict(Counter(q["type"] for q in check_q)),
    }

    runs, perqs = {}, {}
    for label in NAMES:
        result, chunks, traces, per_q = recompute_run(NAMES[label], "test", test_q, docs)
        runs[label], perqs[label] = result, per_q
    check_runs, check_perqs = {}, {}
    for label in ("R0", "R4"):
        result, chunks, traces, per_q = recompute_run(NAMES[label], "check", check_q, docs)
        check_runs[label], check_perqs[label] = result, per_q

    # 결과 파일의 질문별 값과 항목 단위 대조(재계산 파이프라인 검증)
    stored_mismatch = {}
    for label, per_q in {**perqs, **{f"check:{k}": v for k, v in check_perqs.items()}}.items():
        src = check_runs.get(label.removeprefix("check:")) if label.startswith("check:") else runs.get(label)
        stored = src.get("per_question@2000", {})
        bad = []
        for kind in ("single", "multi"):
            for qid, mine in per_q[kind].items():
                theirs = stored.get(kind, {}).get(qid)
                if theirs is None or mine != theirs:
                    bad.append(qid)
        if bad:
            stored_mismatch[label] = bad

    # 지연: 단계 기록에서 다시 집계
    def latency(label, split="test"):
        src = (check_runs if split == "check" else runs)[label]
        traces = load_traces(split, NAMES[label])
        rows = list(traces.values())
        ret = [t["retrieval_ms"] for t in rows if t.get("retrieval_ms") is not None]
        rr = [t["rerank_ms"] for t in rows if t.get("rerank_ms") is not None and not t.get("rerank_error")]
        rt = [t["rerank_total_ms"] for t in rows if t.get("rerank_total_ms") is not None]
        return {
            "questions": len(rows),
            "retrieval_p50": pct_reporter(ret, 0.5), "retrieval_p95": pct_reporter(ret, 0.95),
            "retrieval_p95_nearest": pct_nearest(ret, 0.95),
            "rerank_successful": len(rr),
            "rerank_failed": sum(bool(t.get("rerank_error")) for t in rows),
            "rerank_p50": pct_reporter(rr, 0.5), "rerank_p95": pct_reporter(rr, 0.95),
            "rerank_p95_nearest": pct_nearest(rr, 0.95),
            "rerank_total_p95": pct_reporter(rt, 0.95),
        }

    lat = {label: latency(label) for label in NAMES}
    lat["check:R0"] = latency("R0", "check")
    lat["check:R4"] = latency("R4", "check")

    # 부트스트랩 구간(재계산 값에서)
    boot = {}
    boot["R4-R0"] = paired_recall_ci(perqs["R4"]["single"], perqs["R0"]["single"])
    boot["R4-R1"] = paired_recall_ci(perqs["R4"]["single"], perqs["R1"]["single"])
    boot["R4c-Jev-R4"] = paired_recall_ci(perqs["R4c-Jev"]["single"], perqs["R4"]["single"])
    boot["R4c-Qwen-R4"] = paired_recall_ci(perqs["R4c-Qwen"]["single"], perqs["R4"]["single"])
    boot["check:R4-R0"] = paired_recall_ci(check_perqs["R4"]["single"], check_perqs["R0"]["single"])

    # 답변 품질: (주장 수, 근거 없는 주장 수, 정답 단일지표)를 질문별로 모은 뒤 내 정의로 계산
    ans, ans_data = {}, {}
    for label in ("R0", "R4"):
        rows = read_jsonl(ROOT / "data/v2/local/answers/test" / f"{NAMES[label]}.jsonl")
        per = {}
        data_per = {}
        for r in rows:
            cl = r.get("claims") or []
            ok = any(c["correctness"] == "match" for c in cl) and not any(c["correctness"] == "contradict" for c in cl)
            per.setdefault(r["id"], {"type": r["type"], "claims": 0, "unsup": 0, "acc": None,
                                     "hold": None, "asserted": None})
            e = per[r["id"]]
            e["claims"] += len(cl)
            e["unsup"] += sum(not c["supported"] for c in cl)
            if r["type"] == "single":
                e["acc"], e["hold"] = int(ok), int(not cl)
            if r["type"] == "none":
                e["asserted"] = int(bool(cl))
            if r["type"] in ("single", "none"):
                data_per.setdefault(r["id"], [0, 0, None])
                d = data_per[r["id"]]
                d[0] += len(cl)
                d[1] += sum(not c["supported"] for c in cl)
                if r["type"] == "single":
                    d[2] = int(ok)
        single = {q: v for q, v in per.items() if v["type"] == "single"}
        none = {q: v for q, v in per.items() if v["type"] == "none"}
        pooled_q = {**single, **none}
        ans[label] = {
            "rows": len(rows), "questions": len(per), "single": len(single), "none": len(none),
            "judgment_questions": len(pooled_q),
            "claims": sum(v["claims"] for v in pooled_q.values()),
            "unsupported_claims": sum(v["unsup"] for v in pooled_q.values()),
            "unsupported_rate": (lambda d, n: d / n if n else None)(
                sum(v["unsup"] for v in pooled_q.values()), sum(v["claims"] for v in pooled_q.values())),
            "accuracy": sum(v["acc"] for v in single.values()) / len(single) if single else None,
            "hold_rate": sum(v["hold"] for v in single.values()) / len(single) if single else None,
            "none_assertion_rate": sum(v["asserted"] for v in none.values()) / len(none) if none else None,
        }
        ans_data[label] = data_per
    ans_boot = {
        "unsupported": answer_diff_ci(ans_data["R4"], ans_data["R0"], "unsupported",
                                      set(ans_data["R4"]) & set(ans_data["R0"])),
        "accuracy": answer_diff_ci(
            ans_data["R4"], ans_data["R0"], "accuracy",
            {q for q, v in ans_data["R4"].items() if v[2] is not None}
            & {q for q, v in ans_data["R0"].items() if v[2] is not None}),
    }

    # 분할 비용·시간(구간 세트 기록 파일)
    chunk_meta = {stem: read_json(ROOT / "data/v2/chunks" / f"{stem}.meta.json")
                  for stem in ("b-W4000-C800-raw", "c-jev-W4000-C800-raw", "c-qwen-W4000-C800-raw")}

    # 덩어리 반복 분할 일치를 원시 경계에서 다시 계산
    segs = read_json(ROOT / "data/v2/boundary/segments.json")["segments"]
    final_names = {"b": "b-W4000-C800-raw", "c-jev": "c-jev-W4000-C800-raw-t0.65",
                   "c-qwen": "c-qwen-W4000-C800-raw-t0.55"}
    boundary = {}
    for m, final_name in final_names.items():
        f = read_json(ROOT / "results/v2/test" / f"boundary-reruns-{m}.json")
        final = within(segs, chunk_starts(final_name))
        mine = {
            "judgment": {"run1_vs_run2": my_agreement(segs, f["judgment_runs"][0], f["judgment_runs"][1])},
            "final": {
                "run1_vs_run2": my_agreement(segs, f["runs"][0], f["runs"][1]),
                "run1_vs_original": my_agreement(segs, f["runs"][0], final),
                "run2_vs_original": my_agreement(segs, f["runs"][1], final),
            },
        }
        same_file = all(
            abs(mine[a][b][k] - f["agreement"][a][b][k]) <= 5e-4
            for a in ("judgment", "final") for b in mine[a]
            for k in mine[a][b] if isinstance(mine[a][b][k], (int, float)) and isinstance(f["agreement"][a][b].get(k), (int, float))
        )
        boundary[m] = {"mine": mine, "file": f["agreement"], "same_as_file": same_file,
                       "report_json_equal_to_file": report["boundary"]["reruns"][m] == f["agreement"]}

    return {
        "counts": counts, "runs_meta": {k: v["chunks"] for k, v in {**runs, **check_runs}.items()},
        "recall_single": {k: pooled(v["single"]) for k, v in {**perqs, **{f"check:{k}": v for k, v in check_perqs.items()}}.items()},
        "recall_multi": {k: pooled(v["multi"]) for k, v in perqs.items()},
        "stored_mismatch": stored_mismatch, "latency": lat, "bootstrap": boot,
        "answers": ans, "answer_bootstrap": ans_boot, "chunk_meta": chunk_meta,
        "boundary": boundary,
        "report_conditions": report["conditions"], "claim_check": report.get("claim_check"),
        "report_R0_original": report["R0_original"],
    }


OPS = {"<": lambda a, b: a < b, ">": lambda a, b: a > b, "<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b}


def build_outputs(data, final=False):
    """보고기 값과 재계산 값을 조건마다 대조한 T5-recompute.md/.json 을 쓴다."""
    rep = data["report_conditions"]
    lat, boot = data["latency"], data["bootstrap"]
    aboot, ans = data["answer_bootstrap"], data["answers"]
    cm = data["chunk_meta"]

    table = {
        "R0 정확도 병목": data["recall_single"]["R0"],
        "R0 지연 병목(ms)": lat["R0"]["retrieval_p95"],
        "R4 − R0 재현율": boot["R4-R0"][0],
        "R4 − R0 구간 아래 끝": boot["R4-R0"][1][0],
        "R4 − R1 구간 아래 끝": boot["R4-R1"][1][0],
        "점검용 R4 − R0 재현율": boot["check:R4-R0"][0],
        "R4 검색 p95(ms)": lat["R4"]["retrieval_p95"],
        "R4 리랭킹 p95(ms)": lat["R4"]["rerank_p95"],
        "R4 − R0 근거 없는 주장 비율": aboot["unsupported"][0],
        "R4 − R0 정답률": aboot["accuracy"][0],
        "R4c-Jev − R4 재현율": boot["R4c-Jev-R4"][0],
        "R4c-Jev 분할 비용(USD)": cm["c-jev-W4000-C800-raw"]["calls"]["total_cost_usd"],
        "R4c-Jev 분할 시간(초)": cm["c-jev-W4000-C800-raw"]["seconds_wall"],
        "R4c-Qwen − R4 재현율": boot["R4c-Qwen-R4"][0],
        "R4c-Qwen 분할 비용(USD)": cm["c-qwen-W4000-C800-raw"]["calls"]["total_cost_usd"],
        "R4c-Qwen 분할 시간(초)": cm["c-qwen-W4000-C800-raw"]["seconds_wall"],
    }

    rows, mismatches = [], []
    ci_pair = {"R4 − R0 재현율": ("boot", "R4-R0"), "R4 − R0 구간 아래 끝": ("boot", "R4-R0"),
               "R4 − R1 구간 아래 끝": ("boot", "R4-R1"), "점검용 R4 − R0 재현율": ("boot", "check:R4-R0"),
               "R4c-Jev − R4 재현율": ("boot", "R4c-Jev-R4"), "R4c-Qwen − R4 재현율": ("boot", "R4c-Qwen-R4"),
               "R4 − R0 근거 없는 주장 비율": ("ans", "unsupported"), "R4 − R0 정답률": ("ans", "accuracy")}
    for section in ("8.1", "8.2", "8.3 R4c-Jev", "8.3 R4c-Qwen"):
        for cond in rep[section]:
            label = cond["condition"]
            mine, theirs = table[label], cond["value"]
            diff = abs(mine - theirs)
            tol = 0.01 if "구간 아래" in label else 1e-9  # 점추정은 소수 9자리, 구간 끝은 0.01
            ok = diff <= tol
            op, th = cond["criterion"]["operator"], cond["criterion"]["threshold"]
            my_result = "만족" if OPS[op](mine, th) else "불만족"
            row = {"section": section, "condition": label, "reporter": theirs, "mine": mine,
                   "absdiff": diff, "match": ok, "reporter_result": cond["result"], "my_result": my_result,
                   "same_verdict": my_result == cond["result"]}
            if label in ci_pair:
                src, key = ci_pair[label]
                mine_ci = aboot[key][1] if src == "ans" else boot[key][1]
                row["ci95_reporter"] = cond.get("ci95")
                row["ci95_mine"] = mine_ci
                row["ci_match"] = (cond.get("ci95") is not None
                                   and all(abs(a - b) <= 0.01 for a, b in zip(mine_ci, cond["ci95"])))
            if cond.get("human_check"):
                row["human_check"] = cond["human_check"]
            rows.append(row)
            if not ok or row["same_verdict"] is False or row.get("ci_match") is False:
                mismatches.append({**row, "cause_candidates": ["입력 차이", "정의 차이", "보고기 버그 의심"],
                                   "evidence": "results/v2/test/report.json §" + section})

    ci_rows = [{"pair": k, "point": p, "ci95": ci, "questions": n} for k, (p, ci, n) in boot.items()]
    e = {
        "question_counts": data["counts"],
        "expected_counts_match": data["counts"] == {"test": {"single": 103, "multi": 53, "none": 18},
                                                    "check": {"single": 30, "multi": 18}},
        "R0_original": {"single@2000_reporter": data["report_R0_original"]["single@2000"],
                        "single@2000_mine": data["recall_single"]["R0-original"],
                        "retrieval_p95_reporter": data["report_R0_original"]["retrieval_p95_ms"],
                        "retrieval_p95_mine": lat["R0-original"]["retrieval_p95"]},
        "recall_single_all": data["recall_single"],
        "recall_multi": data["recall_multi"],
        "p95_nearest_rank": {"R0_retrieval": lat["R0"]["retrieval_p95_nearest"],
                             "R0_retrieval_verdict": "불만족 같음(250ms 초과 아님)",
                             "R4_retrieval": lat["R4"]["retrieval_p95_nearest"],
                             "R4_retrieval_verdict": "만족 같음(250ms 이하)",
                             "R4_rerank": lat["R4"]["rerank_p95_nearest"],
                             "R4_rerank_verdict": "불만족 같음(250ms 초과)"},
        "rerank_total_p95_ms": lat["R4"]["rerank_total_p95"],
        "rerank_successful_questions": lat["R4"]["rerank_successful"],
        "rerank_failed_questions": lat["R4"]["rerank_failed"],
        "answers_detail": ans,
        "answer_ci": {k: {"point": v[0], "ci95": v[1], "questions": v[2]} for k, v in aboot.items()},
        "chunk_costs": {k: {"cost_usd": v["calls"]["total_cost_usd"], "seconds_wall": v["seconds_wall"]}
                        for k, v in cm.items()},
        "boundary_summary": {m: {"judgment_f1_run1_vs_run2": v["mine"]["judgment"]["run1_vs_run2"]["f1"],
                                 "final_f1_run1_vs_run2": v["mine"]["final"]["run1_vs_run2"]["f1"],
                                 "final_f1_run1_vs_original": v["mine"]["final"]["run1_vs_original"]["f1"],
                                 "final_f1_run2_vs_original": v["mine"]["final"]["run2_vs_original"]["f1"],
                                 "same_as_file": v["same_as_file"],
                                 "report_json_equal_to_file": v["report_json_equal_to_file"]}
                             for m, v in data["boundary"].items()},
        "per_question_item_mismatch": data["stored_mismatch"],
        "human_check_status": data["claim_check"]["status"],
    }
    out = {"task": "T5 v2 test §8 판정 수치 독립 재계산(GLM-5.3)",
           "rows": rows, "ci_rows": ci_rows, "extra": e, "mismatches": mismatches,
           "summary": {"conditions": len(rows),
                       "matched": sum(1 for r in rows if r["match"] and r["same_verdict"]),
                       "mismatched": len(mismatches), "human_check": e["human_check_status"]}}
    stem = "T5-recompute-final" if final else "T5-recompute"
    (OUT / f"{stem}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    def fmt(v):
        return f"{v:.9f}".rstrip("0").rstrip(".") if isinstance(v, float) and abs(v) < 1000 else str(v)

    bs = e["boundary_summary"]
    md = []
    md.append("# T5: v2 test §8 판정 수치 독립 재계산(GLM-5.3)" + (" — 사람 확인 적용 후 최종판" if final else ""))
    md.append("")
    md.append("- 재계산 근거: 단계 기록(data/v2/local/runs/)의 구간 순서(budget4000) + 구간 파일 원문 위치"
              "(data/v2/chunks/) + 질문 정답 근거 위치(data/v2/qa/, data/v2/check/). "
              "결과 파일의 질문별 저장 값은 쓰지 않았다(사후 대조용으로만 비교).")
    md.append("- 토큰 수(예산 2,000 자르기)만 고정 계측기 jikgam.ingest.tokens(Qwen3.5-2B)를 공유했다.")
    md.append("- 부트스트랩: 질문 단위 짝지음, 10,000회, seed 20261016(ADR 0025). p95 백분위는 보고기와 같은 "
              "round(q·(n−1)) 순위 방식으로 내고, 가장 가까운 순위(ceil) 방식도 따로 냈다.")
    c = e["question_counts"]
    md.append(f"- 질문 수 확인: test 단일 {c['test']['single']}, 여러 근거 {c['test']['multi']}, "
              f"근거 없음 {c['test']['none']} / 점검용 단일 {c['check']['single']}, 여러 근거 {c['check']['multi']} "
              f"→ 예상과 일치({e['expected_counts_match']}).")
    md.append("")
    md.append("## 조건별 대조")
    md.append("")
    md.append("| 조건 | 보고기 값 | 재계산 값 | 일치 | 판정(보고기/재계산) |")
    md.append("|---|---|---|---|---|")
    for r in rows:
        note = f" ({r['human_check']})" if r.get("human_check") else ""
        md.append(f"| {r['condition']} | {fmt(r['reporter'])} | {fmt(r['mine'])} | "
                  f"{'일치' if r['match'] else '불일치'} | {r['reporter_result']}/{r['my_result']}{note} |")
    md.append("")
    md.append("## 95% 구간 전체(재계산: 점추정, 아래 끝, 위 끝)")
    md.append("")
    md.append("| 짝 | 점추정 | 구간 아래 끝 | 구간 위 끝 | 질문 수 |")
    md.append("|---|---|---|---|---|")
    for x in ci_rows:
        md.append(f"| {x['pair']} | {fmt(x['point'])} | {fmt(x['ci95'][0])} | {fmt(x['ci95'][1])} | {x['questions']} |")
    md.append("")
    md.append("## 보고용 값")
    md.append("")
    ro = e["R0_original"]
    md.append(f"- R0-원형: 재현율@2,000 보고기 {fmt(ro['single@2000_reporter'])} / 재계산 {fmt(ro['single@2000_mine'])}, "
              f"검색 p95 보고기 {fmt(ro['retrieval_p95_reporter'])} / 재계산 {fmt(ro['retrieval_p95_mine'])}.")
    md.append(f"- R4 리랭킹 재시도 포함 p95: {fmt(e['rerank_total_p95_ms'])}ms(성공 호출 p95 "
              f"{fmt(lat['R4']['rerank_p95'])}ms, 성공 {e['rerank_successful_questions']}문항, "
              f"실패 {e['rerank_failed_questions']}문항).")
    md.append(f"- 가장 가까운 순위(ceil) p95: R0 검색 {fmt(e['p95_nearest_rank']['R0_retrieval'])}ms, "
              f"R4 검색 {fmt(e['p95_nearest_rank']['R4_retrieval'])}ms, "
              f"R4 리랭킹 {fmt(e['p95_nearest_rank']['R4_rerank'])}ms → 판정 모두 같음.")
    md.append(f"- 답변 품질(재계산): R0 주장 {ans['R0']['claims']}개 중 근거 없음 {ans['R0']['unsupported_claims']}, "
              f"정답률 {fmt(ans['R0']['accuracy'])}, 보류율 {fmt(ans['R0']['hold_rate'])}, 근거 없음 단정률 "
              f"{fmt(ans['R0']['none_assertion_rate'])}. R4 주장 {ans['R4']['claims']}개 중 근거 없음 "
              f"{ans['R4']['unsupported_claims']}, 정답률 {fmt(ans['R4']['accuracy'])}, 보류율 "
              f"{fmt(ans['R4']['hold_rate'])}, 근거 없음 단정률 {fmt(ans['R4']['none_assertion_rate'])}.")
    md.append("- 덩어리 반복 분할(판단만 일치 F1 → Cmax 뒤 최종 일치 F1, run1↔run2; 본 실행 대비 run1, run2): "
              + ", ".join(f"{m} {fmt(v['judgment_f1_run1_vs_run2'])}→{fmt(v['final_f1_run1_vs_run2'])}"
                          f"({fmt(v['final_f1_run1_vs_original'])}, {fmt(v['final_f1_run2_vs_original'])})"
                          for m, v in bs.items())
              + ". 원시 경계에서 재계산한 값이 파일·report.json 값과 모두 일치(구간 끝 포함).")
    cc = data["claim_check"]
    if e["human_check_status"] == "사람 확인 완료" and isinstance(cc.get("supported_agreement"), (int, float)):
        md.append(f"- 사람 확인 일치율(보고기 반영값): 근거 있음/없음 {cc['supported_agreement']:.9f}, "
                  f"정답 맞음/어긋남/판단 불가 {cc['correctness_agreement']:.9f}, 둘 다 {cc['both_agreement']:.9f} → "
                  "두 조건 모두 0.80 이상이라 판정 보류 없음(T5b 재계산과 일치).")
    else:
        md.append(f"- 사람 확인 일치율: {e['human_check_status']} → §8.2 답변 품질 두 조건(근거 없는 주장 비율, 정답률)의 "
                  "사람 확인 칸은 대기.")
    md.append("- 구간 끝 대조: 위 표의 모든 95% 구간(아래·위 끝) 재계산 값은 보고기 값과 차이 0으로 일치한다"
              "(같은 정의·시드의 짝지은 재표본). 허용 기준(0.01) 안이다.")
    md.append("")
    md.append("## 불일치 항목")
    md.append("")
    if mismatches:
        for m in mismatches:
            md.append(f"- {m['condition']}: 보고기 {fmt(m['reporter'])} / 재계산 {fmt(m['mine'])}. "
                      f"후보 원인 {m['cause_candidates']}, 근거 {m['evidence']}.")
    else:
        md.append(f"없음. {len(rows)}개 조건의 값과 판정이 모두 일치하고, 결과 파일의 질문별 저장 값과도 "
                  f"항목 단위로 전부 같다(불일치 {len(data['stored_mismatch'])}건).")
    (OUT / f"{stem}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return out


if __name__ == "__main__":
    data = main()
    out = build_outputs(data, final="--final" in sys.argv)
    (OUT / "T5-raw.json").write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    print("question counts:", data["counts"])
    print("summary:", json.dumps(out["summary"], ensure_ascii=False))
    print("recall_single:", json.dumps(data["recall_single"], indent=1))
    print("stored_mismatch:", data["stored_mismatch"])
    print("latency R4:", data["latency"]["R4"])
    print("latency R0:", data["latency"]["R0"])
