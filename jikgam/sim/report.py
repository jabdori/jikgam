"""시나리오 실행 보고서. 원본 로그(엔진 이벤트, 시뮬레이터 기록)와 저장한 AI 판정 파일만으로 만든다.

    uv run python -m jikgam.sim.report results/engine/scenarios/<run_id> [<run_id> ...]

코드 판정과 지표는 보고서를 만들 때마다 로그에서 다시 계산한다. AI 판정은 <세션>.judge.json에 저장한 것을 쓴다
(없으면 그 항목은 판정 불가). 무효 실행(고객 AI가 지시를 어김)은 통과율과 지표에서 빼고 따로 센다.
"""

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

from jikgam.engine.events import read_log
from jikgam.engine.org import ROOT

from . import judge as jd
from . import metrics as mt
from .scenario import load_scenarios

RESULT = {True: "통과", False: "실패", None: "판정 불가"}


def _ms(x: float) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    return "∞(미갱신)" if math.isinf(x) else f"{x:,.0f}"


def load_run(run_dir: Path) -> dict[str, Any]:
    summary = jd.load_summary(run_dir)
    scenarios = {s["id"]: s for s in load_scenarios(ROOT / summary["scenario_file"])}
    sessions = []
    for sess in summary["sessions"]:
        sid = sess["session"]
        events = read_log(run_dir / f"{sid}.jsonl")
        jp = jd.judge_path(run_dir, sid)
        ai = json.loads(jp.read_text(encoding="utf-8")) if jp.exists() else None
        result = jd.evaluate(scenarios[sess["scenario"]], events, ai, jd.sim_end_of(run_dir, sid))
        sessions.append({**sess, "result": result, "metrics": mt.session_metrics(events), "ai": ai})
    return {"summary": summary, "sessions": sessions}


def render(run: dict[str, Any]) -> str:
    s = run["summary"]
    sessions = run["sessions"]
    out = [f"=== 실행 {s['run_id']} · 정책 {s['policy']}: {s['policy_description']}",
           f"판별 {s.get('decision_backend')}({s.get('decision_model')}), 생성 {s.get('generator_model')}, "
           f"고객 AI {s['customer_model']}({s['customer_prompt']}), 판정 {s.get('judge_model')}({s.get('judge_prompt')})",
           f"커밋 {s['git']['commit'][:10]}{' (작업 사본 변경 있음)' if s['git']['dirty'] else ''}, 시나리오 "
           f"{s['scenario_file']} sha256 {s['scenario_sha256'][:16]}, 시작 {s['started']}, 끝 {s.get('ended')}",
           s.get("note", ""), ""]

    out.append("시나리오별 판정 (항목 번호는 docs/evaluation.md §4.1)")
    for x in sessions:
        r = x["result"]
        comp = r["compliance"]
        if comp["valid"] is False:
            status = "무효"
        else:
            status = RESULT[r["pass"]] + (" (준수 판정 없음)" if comp["valid"] is None else "")
        failed = [n for n, it in r["items"].items() if it["ok"] is False]
        unknown = [n for n, it in r["items"].items() if it["ok"] is None]
        out.append(f"- {x['scenario']} r{x.get('run', 1)} {status}  끝: {x['end_reason']}, 고객 {x['customer_turns']}턴"
                   + (f", 실패 항목 {failed}" if failed else "") + (f", 판정 불가 {unknown}" if unknown else ""))
        for n in failed + unknown:
            for reason in r["items"][n]["reasons"]:
                out.append(f"    {n}. {jd.ITEM_NAMES[int(n)]}: {reason}")
        if comp["valid"] is False:
            for reason in comp["reasons"]:
                out.append(f"    무효 사유: {reason}")
        if r["aux"]["engine_errors"]:
            out.append(f"    엔진 오류: {r['aux']['engine_errors']}")

    valid = [x for x in sessions if x["result"]["compliance"]["valid"] is not False]
    invalid = [x for x in sessions if x["result"]["compliance"]["valid"] is False]
    passed = [x for x in valid if x["result"]["pass"] is True]
    undetermined = [x for x in valid if x["result"]["pass"] is None]
    out.append("")
    out.append(f"통과: 유효 실행 {len(valid)}개 중 {len(passed)}개 (판정 불가 {len(undetermined)}개). "
               f"무효 실행 {len(invalid)}개: {[x['scenario'] for x in invalid]}")
    item_fail = {n: sum(x["result"]["items"][n]["ok"] is False for x in valid) for n in jd.ITEMS}
    out.append("항목별 실패 수(유효 실행): " + ", ".join(f"{n}.{jd.ITEM_NAMES[n]} {item_fail[n]}" for n in jd.ITEMS))
    aux = [x["result"]["aux"] for x in valid]
    precisions = [a["precision"] for a in aux if a["precision"] is not None]
    out.append(f"보조: intent 일치 {sum(a['intent_match'] for a in aux)}/{len(aux)}, 최종 안내 근거 정밀도 평균 "
               + (f"{sum(precisions) / len(precisions):.2f}({len(precisions)}개)" if precisions else "-")
               + f", 고객 턴 평균 {sum(a['customer_turns'] for a in aux) / max(len(aux), 1):.1f}")
    out.append(f"진단(판정 아님): 대화 전체 인용 합집합이 articles를 모두 포함 {sum(a['articles_in_any'] for a in aux)}"
               f"/{len(aux)} (10번은 최종 안내 하나만 본다)")

    out.append("")
    out.append("조건 변경부터 근거 갱신까지(docs/evaluation.md §3.2, ms, 미갱신은 +∞로 계산)")
    out.append("  대상          이벤트   p50      p95      미갱신")
    for label, group in (("유효 실행", valid), ("전체 실행", sessions)):
        agg = mt.aggregate([x["metrics"] for x in group])
        st = agg["refresh"]["stats"]
        out.append(f"  {label:10s}  {st['events']:6d}   {_ms(st['p50']):>7s}  {_ms(st['p95']):>7s}  {st['misses']}")
    for x in sessions:
        for r in x["metrics"]["refresh"]:
            lat = "미갱신" if r["latency_ms"] is None else f"{r['latency_ms']:,.1f}"
            flag = "" if x in valid else " (무효 실행)"
            out.append(f"    {r['event']:6s} {r['utt_id']:4s} v{r['version']:<3d} {lat} ms"
                       f"{' invalidates_prior' if r['invalidates_prior'] else ''}{flag}")

    out.append("")
    out.append("무효 제안 노출(docs/evaluation.md §3.3, 유효 실행)")
    agg = mt.aggregate([x["metrics"] for x in valid])
    st = agg["stale"]
    if not st["by_channel"]:
        out.append("  노출 없음")
    for ch, c in st["by_channel"].items():
        out.append(f"  채널 {ch}: 무효 노출 {c['invalid']} / 분모 {c['denominator']}, 미재확인 노출 {c['unconfirmed']}")
        for case in c["cases"]:
            out.append(f"    {case['kind']} {case['suggestion_id']} 기준 v{case['basis_version']} ← {case['events']}")
    out.append(f"  이벤트 단위: invalidates_prior 이벤트 {st['events_total']}개 중 무효 노출이 있었던 이벤트 "
               f"{st['events_with_invalid']}개")

    out.append("")
    cost = {"customer": 0.0, "decision": 0.0, "generation": 0.0, "judge": 0.0}
    for x in sessions:
        c = x["costs"]
        cost["customer"] += c.get("customer_usd") or 0
        cost["decision"] += c.get("decision_usd") or 0
        cost["generation"] += c.get("generation_usd") or 0
        cost["judge"] += (x["ai"] or {}).get("cost_usd") or 0
    total = sum(cost.values())
    out.append(f"비용(USD): 고객 AI {cost['customer']:.4f}, 엔진 판별 {cost['decision']:.4f}, 엔진 생성 "
               f"{cost['generation']:.4f}, 판정 {cost['judge']:.4f}, 합계 {total:.4f}")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="시나리오 실행 보고서")
    ap.add_argument("run_dirs", nargs="+", type=Path)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    for d in args.run_dirs:
        print(render(load_run(d)))
        print()


if __name__ == "__main__":
    main()
