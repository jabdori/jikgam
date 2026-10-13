"""run_decisions.py가 남긴 원본 로그(raw.jsonl, meta.json)와 정답 파일만으로 결과 표를 만든다.
같은 로그로 다시 돌리면 같은 표가 나온다.

    uv run python -m jikgam.eval.report results/decisions/val/<run_id>
    uv run python -m jikgam.eval.report <run_dir> <run_dir> ... --out <report.md>   # 여러 실행을 한 표로
"""

import argparse
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

from jikgam.decision.registry import ROOT
from jikgam.decision.types import label_and_prob
from jikgam.eval.metrics import balanced_accuracy, ece, percentile

QTYPES = ["intent", "condition_changed", "applies", "sufficient", "next_info", "suggestion_valid", "action"]
NOUL = ["condition_changed", "applies", "sufficient", "suggestion_valid"]
LENGTH_BINS = [(0, 2200, "≤2,200자"), (2201, 2600, "2,201~2,600자"), (2601, 10**9, ">2,600자")]


def load(run_dir: Path):
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    data_path = ROOT / meta["data_file"]
    if hashlib.sha256(data_path.read_bytes()).hexdigest()[:16] != meta["data_sha256_16"]:
        raise SystemExit(f"정답 파일이 측정 때와 다르다: {data_path}")
    cases = {}
    for line in data_path.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        cases[r["id"]] = {"gold": {q: g["label"] for q, g in json.loads(r["gold"]).items()},
                          "tags": r["tags"], "state_chars": len(r["state"])}
    raw = [json.loads(line) for line in (run_dir / "raw.jsonl").read_text(encoding="utf-8").splitlines()]
    return meta, cases, raw


def decisions(raw: list[dict], cases: dict) -> list[dict]:
    """호출 기록을 판단 단위(사례 × 질문 × 회차)로 편다. 답이 없으면 오답으로 센다."""
    out = []
    for rec in raw:
        case = cases[rec["case"]]
        for qid, gold in case["gold"].items():
            ans = rec["answers"].get(qid)
            label, prob = (label_and_prob(ans)[:2]) if ans else (None, None)
            out.append({"backend": rec["backend"], "rep": rec["rep"], "case": rec["case"], "qid": qid,
                        "qtype": qid.split(":")[0], "gold": gold, "pred": label, "prob": prob,
                        "correct": label == gold, "tags": case["tags"], "state_chars": case["state_chars"]})
    return out


def counts_by_rep(ds: list[dict]) -> tuple[float, str]:
    """(회차 평균 정확도, '정답 수/전체' 문자열). 회차마다 다르면 '최소~최대/전체'."""
    by_rep = defaultdict(lambda: [0, 0])
    for d in ds:
        by_rep[d["rep"]][0] += d["correct"]
        by_rep[d["rep"]][1] += 1
    if not by_rep:
        return math.nan, "-"
    accs = [c / n for c, n in by_rep.values()]
    cs = sorted(c for c, _ in by_rep.values())
    n = next(iter(by_rep.values()))[1]
    s = f"{cs[0]}/{n}" if cs[0] == cs[-1] else f"{cs[0]}~{cs[-1]}/{n}"
    return sum(accs) / len(accs), s


def pct(x: float) -> str:
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:.1f}%"


def fmt_acc(ds: list[dict]) -> str:
    acc, s = counts_by_rep(ds)
    return "-" if s == "-" else f"{pct(acc)} ({s})"


def stability(ds: list[dict]) -> float:
    labels = defaultdict(set)
    reps = {d["rep"] for d in ds}
    if len(reps) < 2:
        return math.nan
    for d in ds:
        labels[(d["case"], d["qid"])].add(d["pred"])
    return sum(len(v) == 1 for v in labels.values()) / len(labels)


def conditions(meta: dict) -> list[str]:
    net = meta["network_before"]
    return [f"- 시각(KST): {meta['started_kst']} ~ {meta.get('finished_kst', '?')}",
            f"- 코드: `{meta['git_commit'][:12]}`{' (커밋 안 된 변경 있음)' if meta['git_dirty'] else ''}",
            f"- 데이터: `{meta['data_file']}` sha256 앞 16자리 `{meta['data_sha256_16']}`, 사례 {meta['cases']}건",
            f"- 질문 세트 `{meta['questions_version']}`, LLM 프롬프트 `{meta['llm_prompt_version']}`",
            "- 반복: " + ", ".join(f"{b} {n}회" for b, n in meta["reps"].items()) + f", 동시 실행 {meta['concurrency']}",
            f"- 네트워크: Cloudflare 접속 지점 {net['cloudflare_colo']} ({net['cloudflare_loc']}), TCP 연결 중앙값 "
            + (", ".join(f"{h} {ms}ms" for h, ms in net["tcp_connect_ms_median"].items()) or "-"),
            f"- 클라이언트: Python {meta['client']['python']}, httpx {meta['client']['httpx']}, {meta['client']['os']}"]


def render(metas: list[dict], cases: dict, raw: list[dict]) -> str:
    ds = decisions(raw, cases)
    backends = [b for m in metas for b in m["backends"]]
    by_b = {b: [d for d in ds if d["backend"] == b] for b in backends}
    calls = {b: [r for r in raw if r["backend"] == b] for b in backends}
    L = [f"# 판별 평가 결과: {metas[0]['split']} / " + ", ".join(m["run_id"] for m in metas), "",
         "`jikgam/eval/report.py`가 실행 폴더의 `raw.jsonl`, `meta.json`과 정답 파일로 만든다. 손으로 고치지 않는다.", ""]
    for m in metas:
        L += [f"## 실행 조건: {m['run_id']} ({', '.join(m['backends'])})", ""] + conditions(m) + [""]
    L += ["## 요약", "",
         "정확도는 회차별 정확도의 평균이고, 괄호는 회차별 정답 수(회차마다 다르면 최소~최대)/판단 수다. "
         "지연은 클라이언트에서 잰 요청 전송부터 응답 수신까지이며 성공한 모든 호출로 계산한다. 비용은 성공한 호출의 평균이다.", "",
         "| 백엔드 | 응답 모델 | 정확도 | ECE | p50 ms | p95 ms | 평균 입력 토큰 | 요청당 비용 USD | 1,000건당 USD | 오류 | 재시도 | 회차 간 라벨 일치 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for b in backends:
        ok = [r for r in calls[b] if not r["error"]]
        lat = [r["latency_ms"] for r in ok]
        conf = [d for d in by_b[b] if d["prob"] is not None]
        models = sorted({r["model"] for r in ok}) or sorted({r["model"] for r in calls[b]})
        cost = sum(r["cost_usd"] for r in ok) / len(ok) if ok else math.nan
        tin = sum(r["input_tokens"] for r in ok) / len(ok) if ok else math.nan
        L.append(f"| {b} | {', '.join(models)} | {fmt_acc(by_b[b])} | "
                 f"{ece([d['prob'] for d in conf], [d['correct'] for d in conf]):.3f} | "
                 f"{percentile(lat, 50):.0f} | {percentile(lat, 95):.0f} | {tin:.0f} | {cost:.6f} | {cost * 1000:.3f} | "
                 f"{sum(1 for r in calls[b] if r['error'])}/{len(calls[b])} | {sum(r['attempts'] > 1 for r in calls[b])} | "
                 f"{pct(stability(by_b[b]))} |")

    def table(title: str, keys: list, pick, note: str = "") -> None:
        L.extend(["", f"## {title}", ""] + ([note, ""] if note else []))
        L.append("| | " + " | ".join(backends) + " |")
        L.append("|---|" + "---|" * len(backends))
        for label, k in keys:
            L.append(f"| {label} | " + " | ".join(pick(b, k) for b in backends) + " |")

    present = [q for q in QTYPES if any(d["qtype"] == q for d in ds)]
    table("질문 유형별 정확도", [(q, q) for q in present],
          lambda b, q: fmt_acc([d for d in by_b[b] if d["qtype"] == q]))

    def noul_pick(b, q):
        sub = [d for d in by_b[b] if d["qtype"] == q]
        if not sub:
            return "-"
        pred_true = sum(d["pred"] == "true" for d in sub) / len(sub)
        return f"{pct(balanced_accuracy([d['pred'] for d in sub], [d['gold'] for d in sub]))} / 예측 true {pct(pred_true)}"

    gold_true = {q: [d for d in ds if d["qtype"] == q and d["backend"] == backends[0]] for q in NOUL}
    table("noul 질문: 균형 정확도 / 예측 true 비율", [(f"{q} (정답 true {pct(sum(d['gold'] == 'true' for d in gold_true[q]) / len(gold_true[q]))})", q)
                                              for q in NOUL if gold_true[q]], noul_pick,
          "균형 정확도는 true 재현율과 false 재현율의 평균이다. 모든 판단을 true로 내면 50%가 된다.")

    def ece_pick(b, q):
        sub = [d for d in by_b[b] if d["qtype"] == q and d["prob"] is not None]
        return f"{ece([d['prob'] for d in sub], [d['correct'] for d in sub]):.3f}" if sub else "-"

    table("질문 유형별 ECE", [(q, q) for q in present], ece_pick, "top-label ECE, 10개 등간격 구간, 모든 회차를 합쳐 계산한다.")

    bins = [(f"{name} ({sum(lo <= c['state_chars'] <= hi for c in cases.values())}건)", (lo, hi)) for lo, hi, name in LENGTH_BINS]
    table("상태 길이 구간별 정확도", bins,
          lambda b, k: fmt_acc([d for d in by_b[b] if k[0] <= d["state_chars"] <= k[1]]),
          "상태 길이는 내보내기 파일의 state JSON 문자 수다.")

    tags = sorted({t for c in cases.values() for t in c["tags"]})
    table("실패 유형 태그별 정확도", [(f"{t} ({sum(t in c['tags'] for c in cases.values())}건)", t) for t in tags],
          lambda b, t: fmt_acc([d for d in by_b[b] if t in d["tags"]]),
          "태그가 붙은 사례의 모든 판단을 센다. 한 사례가 여러 태그에 들어간다.")
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dirs", nargs="+")
    ap.add_argument("--out", help="여러 실행을 합칠 때 쓸 보고서 경로. 하나면 실행 폴더의 report.md")
    args = ap.parse_args()
    if len(args.run_dirs) > 1 and not args.out:
        raise SystemExit("여러 실행을 합칠 때는 --out을 지정한다")
    metas, raw, cases = [], [], None
    for d in args.run_dirs:
        meta, cases_d, raw_d = load(Path(d))
        if metas and (meta["data_sha256_16"] != metas[0]["data_sha256_16"] or set(meta["backends"]) & {b for m in metas for b in m["backends"]}):
            raise SystemExit(f"합칠 수 없는 실행: {d} (데이터가 다르거나 백엔드가 겹침)")
        metas.append(meta)
        raw += raw_d
        cases = cases_d
    text = render(metas, cases, raw)
    out = Path(args.out) if args.out else Path(args.run_dirs[0]) / "report.md"
    out.write_text(text, encoding="utf-8", newline="\n")
    print(text)


if __name__ == "__main__":
    main()
