"""§8의 조건을 재계산하고 원문 없이 수치만 보고한다(val은 개발 확인용)."""

import argparse
import json
import random
from pathlib import Path
from fractions import Fraction

from jikgam.ingest.corpus import ROOT
from jikgam.v2.stats import bootstrap_ci, paired_recall_diff

B = "b-W4000-C800-raw"
CJ = "c-jev-W4000-C800-raw-t0.65"
CQ = "c-qwen-W4000-C800-raw-t0.55"
CONFIGS = {
    "R0": "R0-article256", "R0-원형": "R0-article0", "R1": "fixed256--none--bm25",
    "R2": f"{B}--full--bm25", "R3": f"{B}--full--snow+rrf", "R4": f"{B}--full--snow+rrf+jev8",
    "R4c-Jev": f"{CJ}--full--snow+rrf+jev8", "R4c-Qwen": f"{CQ}--full--snow+rrf+jev8",
    "R4 원래 규칙": f"{B}--full--gemini+w0.3+rel+jev8", "R4 KURE-v1": f"{B}--full--kure1+rrf+jev8",
    "R4 KURE-v2": f"{B}--full--kure2+rrf+jev8", "R4 gemini RRF": f"{B}--full--gemini+rrf+jev8",
    "(a)+구간 정보": "fixed256--full--snow+rrf+jev8", "구간 정보 없는 하이브리드": f"{B}--none--snow+rrf+jev8",
    "고객 표현 뺀 구간 정보": f"{B}--noexpr--snow+rrf+jev8",
}
CHUNKS = ("article256", "article0", "fixed256", B, CJ, CQ)
GROUPS = ("법령", "고시", "표준약관", "넘나듦")
MISSING = "판정 불가(입력 없음)"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def numeric(value):
    """허용한 지표 안에서도 글 값은 버려 원문이 보고서에 섞이지 않게 한다."""
    if isinstance(value, dict):
        return {k: numeric(v) for k, v in value.items() if not isinstance(v, str)}
    if isinstance(value, list):
        return [numeric(v) for v in value if not isinstance(v, str)]
    return value if value is None or isinstance(value, (int, float)) else None


def recall(per_q: dict) -> dict:
    items = [v for values in per_q.values() for v in values]
    return {"questions": len(per_q), "evidence_items": len(items),
            "items@2000": round(sum(items) / len(items), 4) if items else None,
            "complete@2000": round(sum(all(v) for v in per_q.values()) / len(per_q), 4) if per_q else None}


def per_question(run: dict | None, kind: str = "single") -> dict:
    return (run or {}).get("per_question@2000", {}).get(kind, {})


def group_recall(run: dict | None, groups: dict[str, str], ids: set[str] | None = None) -> dict:
    per = {q: vs for kind in ("single", "multi") for q, vs in per_question(run, kind).items()
           if ids is None or q in ids}
    return {g: recall({q: vs for q, vs in per.items() if groups.get(q) == g}) for g in GROUPS}


def metric(run, kind="single", key="items@2000"):
    if key == "items@2000":
        items = [v for values in per_question(run, kind).values() for v in values]
        if items:
            return sum(items) / len(items)
    return (run or {}).get("metrics", {}).get(kind, {}).get(key)


def latency(run, key):
    return (run or {}).get("latency_ms", {}).get(key)


def paired(a, b):
    qa, qb = per_question(a), per_question(b)
    # stats 함수는 교집합을 쓰므로, 빠진 질문을 좋은 결과로 잘못 판정하지 않도록 먼저 확인한다.
    if not qa or qa.keys() != qb.keys() or any(not qa[q] or len(qa[q]) != len(qb[q]) for q in qa):
        return None
    return paired_recall_diff(qa, qb, n=10000, seed=20261016)


def condition(label, value, op, threshold, ci=None, need_ci=False) -> dict:
    available = value is not None and threshold is not None and (not need_ci or ci is not None)
    ok = False
    if available:
        ok = {"<": value < threshold, ">": value > threshold,
              "<=": value <= threshold, ">=": value >= threshold}[op]
    return {"condition": label, "value": value, "ci95": ci,
            "criterion": {"operator": op, "threshold": threshold},
            "result": ("만족" if ok else "불만족") if available else MISSING}


def answer_rows(root, split, name):
    path = root / "data/v2/local/answers" / split / f"{name}.jsonl"
    if not path.exists():
        return []
    # 주장 글과 답변은 읽은 즉시 버리고 판정 레이블만 유지한다.
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            rows.append({"id": row["id"], "type": row["type"], "group": row["group"],
                         "claims": [{"supported": c["supported"], "correctness": c["correctness"]}
                                    for c in row.get("claims", [])]})
    return rows


def answer_comparison(a: list[dict], b: list[dict], key: str):
    from jikgam.v2.answer import answer_metrics

    def by_question(rows):
        grouped = {}
        for row in rows:
            if row["type"] in (("single", "none") if key == "unsupported_claim_rate" else ("single",)):
                grouped.setdefault(row["id"], []).append(row)
        out = {q: answer_metrics(rs) for q, rs in grouped.items()}
        if key == "accuracy":
            for q, rs in grouped.items():
                out[q][key] = Fraction(sum(any(c["correctness"] == "match" for c in r["claims"])
                                          and not any(c["correctness"] == "contradict" for c in r["claims"]) for r in rs), len(rs))
        return out

    aa, bb = by_question(a), by_question(b)
    if not aa or aa.keys() != bb.keys():
        return None

    def value(sample, data):
        if key == "unsupported_claim_rate":
            denom = sum(data[q]["claims"] for q in sample)
            return Fraction(sum(data[q]["unsupported_claims"] for q in sample), denom) if denom else None
        xs = [data[q][key] for q in sample if data[q][key] is not None]
        return sum(xs) / len(xs) if xs else None

    def diff(sample):
        av, bv = value(sample, aa), value(sample, bb)
        return av - bv if av is not None and bv is not None else None

    qs = sorted(aa)
    point = diff(qs)
    if point is None:
        return None
    rng = random.Random(20261016)
    vals = [diff([rng.choice(qs) for _ in qs]) for _ in range(10000)]
    vals = sorted(v for v in vals if v is not None)  # 주장이 전혀 없는 재표본은 비율을 정의할 수 없다.
    ci = [float(vals[int(0.025 * len(vals))]), float(vals[max(0, int(0.975 * len(vals)) - 1)])] if vals else None
    return {"diff": float(point), "ci95": ci,
            "questions": len(qs)}


def judge_conditions(runs, checks, answers, chunks, rows) -> dict:
    r0, r4 = runs.get("R0"), runs.get("R4")
    d0, d1 = paired(r4, r0), paired(r4, runs.get("R1"))
    dc = paired(checks.get("R4"), checks.get("R0"))
    out = {"8.1": [condition("R0 정확도 병목", metric(r0), "<", 0.80),
                   condition("R0 지연 병목(ms)", latency(r0, "retrieval_p95"), ">", 250)],
           "8.2": [condition("R4 − R0 재현율", d0 and d0["diff"], ">=", 0.10, d0 and d0["ci95"], True),
                   condition("R4 − R0 구간 아래 끝", d0 and d0["ci95"][0], ">", 0, d0 and d0["ci95"], True),
                   condition("R4 − R1 구간 아래 끝", d1 and d1["ci95"][0], ">", 0, d1 and d1["ci95"], True),
                   condition("점검용 R4 − R0 재현율", dc and dc["diff"], ">", 0, dc and dc["ci95"]),
                   condition("R4 검색 p95(ms)", latency(r4, "retrieval_p95"), "<=", 250),
                   condition("R4 리랭킹 p95(ms)", latency(r4, "rerank_p95"), "<=", 250)]}
    for key, label, op in (("unsupported_claim_rate", "근거 없는 주장 비율", "<="), ("accuracy", "정답률", ">=")):
        comp = answer_comparison(rows.get("R4", []), rows.get("R0", []), key)
        out["8.2"].append(condition(f"R4 − R0 {label}", comp and comp["diff"], op, 0, comp and comp["ci95"], True))
    base = chunks.get(B) or {}
    for label, stem in (("R4c-Jev", "c-jev-W4000-C800-raw"), ("R4c-Qwen", "c-qwen-W4000-C800-raw")):
        d = paired(runs.get(label), r4)
        c = chunks.get(stem) or {}
        cost = c.get("calls", {}).get("total_cost_usd")
        bcost = base.get("calls", {}).get("total_cost_usd")
        sec, bsec = c.get("seconds_wall"), base.get("seconds_wall")
        out[f"8.3 {label}"] = [
            condition(f"{label} − R4 재현율", d and d["diff"], ">=", -0.03, d and d["ci95"], True),
            condition(f"{label} 분할 비용(USD)", cost, "<=", bcost / 5 if bcost is not None else None),
            condition(f"{label} 분할 시간(초)", sec, "<=", bsec)]
    out["8.4"] = "판정하지 않음(ADR 0025 범위 조정)"
    return out


def question_groups(root, split):
    """질문 글은 반환하지 않는다. val에서 점검용 파일은 열지 않는다."""
    sp = read_json(root / "data/v2/qa/splits.json")
    if not sp:
        return {}
    out = {}
    for path in (root / "data/v2/qa/items.jsonl", root / "data/v2/local/qa/items.jsonl"):
        if path.exists():
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        row = json.loads(line)
                        if sp["splits"].get(row["id"]) == split:
                            out[row["id"]] = row["group"]
    return out


def boundary_report(root, split):
    if split != "test":
        return {"status": "test에서만 계산"}
    from jikgam.v2.boundary_runs import chunk_starts, within_segments
    from jikgam.v2.stats import boundary_agreement

    segments = read_json(root / "data/v2/boundary/segments.json")
    if not segments:
        return {"status": MISSING}
    segs = segments["segments"]
    refs = {who: read_json(root / "data/v2/boundary" / f"reference-{who}.json") for who in ("astra", "sol")}
    out = {}
    for who, ref in refs.items():
        out[who] = {}
        if ref is None:
            out[who] = {"status": MISSING}
            continue
        gold = ref["boundaries"]
        for name in ("fixed256", "article256", B, CJ, CQ):
            if (root / "data/v2/chunks" / f"{name}.jsonl").exists():
                pred = within_segments(segs, chunk_starts(name, root))
                out[who][name] = boundary_agreement(segs, pred, gold)
            else:
                out[who][name] = {"status": MISSING}
    if all(refs.values()):
        out["astra_vs_sol"] = boundary_agreement(segs, refs["astra"]["boundaries"], refs["sol"]["boundaries"])
    out["reruns"] = {m: (read_json(root / "results/v2/test" / f"boundary-reruns-{m}.json") or {}).get("agreement")
                     for m in ("b", "c-jev", "c-qwen")}
    return out


def apply_claim_check(conditions, summary):
    """각 품질 조건은 해당 판정의 사람 일치율로 따로 보류한다."""
    keys = ("sampled", "reviewed", "supported_matches", "correctness_matches", "both_matches",
            "supported_agreement", "correctness_agreement", "both_agreement")
    human = {k: (summary or {}).get(k) for k in keys}
    statuses = []
    for row in conditions["8.2"]:
        if row["condition"] in ("R4 − R0 근거 없는 주장 비율", "R4 − R0 정답률"):
            key = "supported_agreement" if row["condition"] == "R4 − R0 근거 없는 주장 비율" else "correctness_agreement"
            rate = human[key]
            valid = isinstance(rate, (int, float)) and not isinstance(rate, bool) and 0 <= rate <= 1
            row["human_check"] = ("판정 보류" if rate < 0.80 else "사람 확인 완료") if valid else "사람 확인 전"
            statuses.append(row["human_check"])
            if valid and rate < 0.80:
                row["result_before_human_check"] = row["result"]
                row["result"] = "판정 보류"
    human["status"] = "판정 보류" if "판정 보류" in statuses else ("사람 확인 완료" if statuses and all(s == "사람 확인 완료" for s in statuses) else "사람 확인 전")
    return human


def build_report(root=ROOT, split="test") -> dict:
    runs = {label: read_json(root / "results/v2" / split / f"{name}.json") for label, name in CONFIGS.items()}
    checks = {label: read_json(root / "results/v2/check" / f"{name}.json") for label, name in CONFIGS.items()} if split == "test" else {}
    answers = {label: read_json(root / "results/v2" / split / f"answers-{CONFIGS[label]}.json") for label in ("R0", "R4")}
    rows = {label: answer_rows(root, split, CONFIGS[label]) for label in answers}
    names = (*CHUNKS, "c-jev-W4000-C800-raw", "c-qwen-W4000-C800-raw")
    chunks = {name: read_json(root / "data/v2/chunks" / f"{name}.meta.json") for name in names}
    groups = question_groups(root, split)
    passed = set((read_json(root / "data/v2/qa/reviewed_passed.json") or {}).get("ids", []))
    from jikgam.v2.answer import answer_metrics

    def report_run(run):
        if run is None:
            return {"status": MISSING}
        return {"by_type": numeric(run.get("metrics", {})), "latency_ms": numeric(run.get("latency_ms", {})),
                "rerank": numeric(run.get("rerank")),
                "by_group": numeric(run.get("by_group", {})),
                "reviewed_passed": {kind: recall({q: vs for q, vs in per_question(run, kind).items() if q in passed})
                                    for kind in ("single", "multi")},
                "reviewed_passed_by_group": group_recall(run, groups, passed)}

    conditions = judge_conditions(runs, checks, answers, chunks, rows)
    from jikgam.v2.claim_check import load_review

    review = load_review(root, split)
    human = apply_claim_check(conditions, review if review and review.get("valid") is True else None)
    if review and review.get("invalidation_reason"):
        human["invalidation_reason"] = review["invalidation_reason"]
    overlap = {split: next((numeric(r["word_overlap"]) for r in runs.values() if r and "word_overlap" in r), {})}
    if split == "test":
        overlap["check"] = next((numeric(r["word_overlap"]) for r in checks.values() if r and "word_overlap" in r), {})
    return {"split": split, "status": "판정" if split == "test" else "val 개발 확인용(판정 아님)",
            "conditions": conditions, "claim_check": human,
            "R0_original": {"single@2000": metric(runs["R0-원형"]),
                            "retrieval_p95_ms": latency(runs["R0-원형"], "retrieval_p95")},
            "retrieval": {label: report_run(run) for label, run in runs.items()},
            "check": {label: {"by_type": numeric((run or {}).get("metrics", {})),
                              "by_group": numeric((run or {}).get("by_group", {})),
                              "rerank": numeric((run or {}).get("rerank")),
                              "latency_ms": numeric((run or {}).get("latency_ms", {})),
                              "status": "보고" if run else MISSING} for label, run in checks.items()},
            "answers": {label: {"by_type": {t: answer_metrics([r for r in rs if r["type"] == t]) for t in ("single", "multi", "none")},
                                "judgment": answer_metrics([r for r in rs if r["type"] in ("single", "none")]),
                                "by_group": {g: answer_metrics([r for r in rs if r["group"] == g]) for g in GROUPS},
                                "reviewed_passed": answer_metrics([r for r in rs if r["id"] in passed])}
                        for label, rs in rows.items()},
            "chunks": {name: numeric({k: (meta or {}).get(k) for k in ("chunks", "tokens", "seconds_wall", "calls")})
                       for name, meta in chunks.items()},
            "qwen_gpu_seconds_wall": (chunks.get("c-qwen-W4000-C800-raw") or {}).get("seconds_wall"),
            "word_overlap": overlap,
            "metadata_costs": metadata_costs(root),
            "boundary": boundary_report(root, split),
            "notes": ["임베딩 지연 조건은 val 결과를 본 뒤 추가했다. 원래 규칙 구성은 보고만 한다.",
                      "단일 근거의 사람 검토 문제 비율은 24%, 여러 근거는 62%다. 합성 질문의 결과다.",
                      "엔드투엔드는 제외했다. v2 규모의 근거 갱신 시간은 재지 않았다.",
                      "Qwen GPU 시간은 동시 1 분할의 벽시계 시간이다.",
                      "구형 실행 파일에 by_group·word_overlap이 없으면 빈 값으로 둔다."]}


def metadata_costs(root):
    out = {}
    for stem in ("fixed256", B, CJ, CQ, f"{B}--noexpr"):
        main = read_json(root / "data/v2/meta" / f"{stem}.meta.json") or {}
        local = read_json(root / "data/v2/local/meta" / f"{stem}.meta.json") or {}
        costs = [x["cost_usd"] for x in (main, local) if x.get("cost_usd") is not None]
        out[stem] = {"cost_usd": round(sum(costs), 6) if costs else None,
                     "recorded_cost_usd": main.get("cost_usd"), "additional_local_cost_usd": local.get("cost_usd")}
    return out


def markdown(report):
    def shown(value):
        if isinstance(value, float):
            return f"{value:.6g}"
        if isinstance(value, list):
            return "[" + ", ".join(shown(v) for v in value) + "]"
        return str(value)

    lines = [report["status"], "", "| 절 | 조건 | 값 | 95% 구간 | 기준 | 결과 |", "|---|---|---|---|---|---|"]
    for section, rows in report["conditions"].items():
        if isinstance(rows, str):
            lines.append(f"| {section} | {rows} | | | | |")
            continue
        for row in rows:
            rule = row["criterion"]
            lines.append(f'| {section} | {row["condition"]} | {shown(row["value"])} | {shown(row["ci95"])} | '
                         f'{rule["operator"]} {rule["threshold"]} | {row["result"]} |')
    lines += ["", f'R0-원형: {json.dumps(report["R0_original"], ensure_ascii=False)}', "",
              "| 구성 | 단일@2000 | 여러@2000 | 전체@2000 | 검색 p95(ms) | 리랭킹 p95(ms) | 재시도 포함 p95(ms) | 최종 실패 수 |",
              "|---|---|---|---|---|---|---|---|"]
    for label, run in report["retrieval"].items():
        vals = [run.get("by_type", {}).get(t, {}).get("items@2000") for t in ("single", "multi", "all")]
        lat = run.get("latency_ms", {})
        lines.append(f'| {label} | {shown(vals[0])} | {shown(vals[1])} | {shown(vals[2])} | {shown(lat.get("retrieval_p95"))} | '
                     f'{shown(lat.get("rerank_p95"))} | {shown(lat.get("rerank_total_p95"))} | {lat.get("rerank_failed_questions")} |')
    human = report["claim_check"]
    reason = f' · {human["invalidation_reason"]}' if human.get("invalidation_reason") else ""
    lines += ["", f'사람 확인: {human["status"]}{reason} · 검토 {human["reviewed"]} / {human["sampled"]} · '
              f'근거 판정 일치율 {human["supported_agreement"]} · 정답 판정 일치율 {human["correctness_agreement"]} · '
              f'둘 다 일치율 {human["both_agreement"]}',
              "", f'참고 경계: {report["boundary"].get("status", "report.json")}',
              f'Qwen GPU 분할 시간(초): {report["qwen_gpu_seconds_wall"]}',
              f'낱말 겹침: {json.dumps(report["word_overlap"], ensure_ascii=False)}',
              f'구간 정보 비용(USD): {json.dumps(report["metadata_costs"], ensure_ascii=False)}']
    return "\n".join(lines)


def write_report(root=ROOT, split="test"):
    report = build_report(root, split)
    path = root / ("results/v2/test/report.json" if split == "test" else "outputs/v2-smoke/report-val.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=["test", "val"], default="test")
    args = ap.parse_args()
    report = write_report(ROOT, args.split)
    print(markdown(report))


if __name__ == "__main__":
    main()
