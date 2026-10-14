"""음성 시나리오 실행 보고서(docs/evaluation-voice.md §4·§5). 원본 로그와 저장한 AI 판정 파일만으로 만든다.

    uv run python -m jikgam.voice.report results/voice/scenarios/<A 실행> results/voice/scenarios/<C 실행> [--json out.json]

- 판정·무효 실행은 채팅 보고서(jikgam/sim/report.py)와 같은 함수(jikgam.sim.judge.evaluate)로 다시 계산한다. 무효
  실행(고객 AI가 지시를 어김)은 통과율과 지표에서 빼고 따로 센다. 전체 실행 기준 값도 함께 낸다.
- 지표는 세션 로그에서 jikgam.voice.metrics.session_summary로 다시 계산한다.
- 성공 기준(§5)은 정책 A와 C가 모두 있을 때만 판정한다. 이 판정은 측정과 분리된 검토자의 대조 전 값이다.
"""

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

from jikgam.engine.events import read_log
from jikgam.engine.org import ROOT
from jikgam.sim import judge as jd
from jikgam.sim.metrics import latency_stats
from jikgam.sim.scenario import load_scenarios

from .metrics import normalize_markers, session_summary


def _ms(x: float | None) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    return "∞" if math.isinf(x) else f"{x:,.0f}"


def load_runs(run_dirs: list[Path]) -> list[dict[str, Any]]:
    out = []
    for run_dir in run_dirs:
        summary = jd.load_summary(run_dir)
        scenarios = {s["id"]: s for s in load_scenarios(ROOT / summary["scenario_file"])}
        for sess in summary["sessions"]:
            sid = sess["session"]
            events = normalize_markers(read_log(run_dir / f"{sid}.jsonl"))  # 조건 변경 표시는 줄의 마지막 확정 발화
            jp = jd.judge_path(run_dir, sid)
            ai = json.loads(jp.read_text(encoding="utf-8")) if jp.exists() else None
            result = jd.evaluate(scenarios[sess["scenario"]], events, ai, jd.sim_end_of(run_dir, sid))
            out.append({**sess, "run_id": summary["run_id"], "policy": summary["policy"], "result": result,
                        "voice": session_summary(events)})
    return out


def _stats(values: list[float | None]) -> dict[str, Any]:
    s = latency_stats(values)
    return {"events": s["events"], "p50": s["p50"], "p95": s["p95"], "misses": s["misses"]}


def policy_stats(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [x for x in sessions if x["result"]["compliance"]["valid"] is not False]
    invalid = [x for x in sessions if x["result"]["compliance"]["valid"] is False]
    passed = [x for x in valid if x["result"]["pass"] is True]
    undetermined = [x for x in valid if x["result"]["pass"] is None]

    def voice_of(group: list[dict[str, Any]]) -> dict[str, Any]:
        stale = {"invalid": 0, "denominator": 0, "unconfirmed": 0, "cases": []}
        for x in group:
            v = x["voice"]["stale_voice"]
            for k in ("invalid", "denominator", "unconfirmed"):
                stale[k] += v[k]
            stale["cases"] += [{**c, "session": x["session"]} for c in v["cases"] if c["kind"] == "invalid"]
        refresh = [r for x in group for r in x["voice"]["refresh"]]
        audio = [r for x in group for r in x["voice"]["new_condition_audio"]]
        # 끼어들기는 재생이 시작된 답을 끊은 것만 센다. 재생 전 취소(늦게 온 미확정 자막)는 따로 센다(§4 보완)
        barge = [b for x in group for b in x["voice"]["barge_ins"] if b["started"]]
        cancelled = sum(not b["started"] for x in group for b in x["voice"]["barge_ins"])
        played = [c["played_ms"] for c in stale["cases"] if c.get("played_ms") is not None]
        return {
            "stale": {**{k: stale[k] for k in ("invalid", "denominator", "unconfirmed")},
                      "rate": stale["invalid"] / stale["denominator"] if stale["denominator"] else None,
                      "played_ms_total": round(sum(played), 1), "played_ms": played, "cases": stale["cases"]},
            "refresh": _stats([r["latency_ms"] for r in refresh]),
            "refresh_from_speech_end": _stats([r["latency_from_speech_end_ms"] if r["t1"] is not None else None
                                               for r in refresh]),
            "new_condition_audio": _stats([r["from_speech_end_ms"] for r in audio]),
            "barge_ins": {"count": len(barge), "cancelled_before_audio": cancelled,
                          "detect": _stats([b["detect_ms"] for b in barge if b["detect_ms"] is not None]),
                          "stop": _stats([b["stop_ms"] for b in barge if b["stop_ms"] is not None]),
                          "new_audio": _stats([b["new_audio_ms"] for b in barge])},
        }

    cost: dict[str, float] = {}
    for x in sessions:
        for k, v in x["costs"].items():
            if k.endswith("usd") and k not in ("decision_usd", "generation_usd"):
                cost[k] = cost.get(k, 0.0) + (v or 0)
    return {
        "sessions": len(sessions), "valid": len(valid), "invalid": len(invalid), "passed": len(passed),
        "undetermined": len(undetermined), "pass_rate": len(passed) / len(valid) if valid else None,
        "invalid_runs": [{"session": x["session"], "reasons": x["result"]["compliance"]["reasons"]} for x in invalid],
        "end_reasons": {r: sum(x["end_reason"] == r for x in sessions) for r in sorted({x["end_reason"] for x in sessions})},
        "notes": [{"session": x["session"], "notes": x["notes"]} for x in sessions if x.get("notes")],
        "valid_runs": voice_of(valid), "all_runs": voice_of(sessions),
        "cost_usd": {k: round(v, 4) for k, v in cost.items()}, "cost_usd_total": round(sum(cost.values()), 4),
        "per_run": {r: {"passed": sum(x["result"]["pass"] is True for x in valid if x["run"] == r),
                        "valid": sum(x["run"] == r for x in valid)}
                    for r in sorted({x["run"] for x in sessions})},
    }


def verdict(a: dict[str, Any], c: dict[str, Any]) -> list[dict[str, Any]]:
    """docs/evaluation-voice.md §5. 유효 실행 기준."""
    ra, rc = a["valid_runs"]["refresh"]["p95"], c["valid_runs"]["refresh"]["p95"]
    sa, sc = a["valid_runs"]["stale"]["rate"], c["valid_runs"]["stale"]["rate"]
    pa, pc = a["pass_rate"], c["pass_rate"]
    rows = [
        {"condition": "근거 갱신: C p95 500ms 이하, A p95의 1/10 이하",
         "value": f"C {_ms(rc)}ms, A {_ms(ra)}ms, 비 {rc / ra:.3f}" if ra and not math.isinf(ra) else f"C {_ms(rc)}ms, A {_ms(ra)}ms",
         "ok": bool(rc <= 500 and (math.isinf(ra) or rc <= ra / 10))},
        {"condition": "이미 틀린 안내: C 무효 음성 노출률 5% 이하, A보다 10%p 이상 낮음",
         "value": f"C {c['valid_runs']['stale']['invalid']}/{c['valid_runs']['stale']['denominator']}, "
                  f"A {a['valid_runs']['stale']['invalid']}/{a['valid_runs']['stale']['denominator']}",
         "ok": sc is not None and sa is not None and sc <= 0.05 and sa - sc >= 0.10},
        {"condition": "업무 성공 보호: C 통과율이 A보다 5%p 넘게 낮지 않음",
         "value": f"C {c['passed']}/{c['valid']}, A {a['passed']}/{a['valid']}",
         "ok": pc is not None and pa is not None and pc >= pa - 0.05},
    ]
    return rows


def render(stats: dict[str, dict[str, Any]], verdicts: list[dict[str, Any]] | None) -> str:
    out = []
    for p, s in stats.items():
        v = s["valid_runs"]
        out += [f"=== 정책 {p}: 세션 {s['sessions']}, 유효 {s['valid']}, 무효 {s['invalid']}, 통과 {s['passed']}/{s['valid']}"
                f" (판정 불가 {s['undetermined']}), 회차별 {s['per_run']}",
                f"  끝난 이유 {s['end_reasons']}",
                f"  무효 음성 노출 {v['stale']['invalid']}/{v['stale']['denominator']}, 미재확인 {v['stale']['unconfirmed']}, "
                f"재생된 시간 합 {v['stale']['played_ms_total'] / 1000:.1f}초",
                f"  근거 갱신 p50/p95 {_ms(v['refresh']['p50'])}/{_ms(v['refresh']['p95'])}ms (이벤트 {v['refresh']['events']}, "
                f"미갱신 {v['refresh']['misses']}), 기준 발화 종료부터 {_ms(v['refresh_from_speech_end']['p50'])}/"
                f"{_ms(v['refresh_from_speech_end']['p95'])}ms",
                f"  새 조건 첫 음성 p50/p95 {_ms(v['new_condition_audio']['p50'])}/{_ms(v['new_condition_audio']['p95'])}ms "
                f"(이벤트 {v['new_condition_audio']['events']}, 미응답 {v['new_condition_audio']['misses']})",
                f"  끼어들기 {v['barge_ins']['count']}회(재생 전 취소 {v['barge_ins']['cancelled_before_audio']}회), "
                f"멈춤 p50 {_ms(v['barge_ins']['stop']['p50'])}ms, "
                f"새 음성 p50 {_ms(v['barge_ins']['new_audio']['p50'])}ms",
                f"  비용 {s['cost_usd']} 합 ${s['cost_usd_total']:.4f}"]
        for r in s["invalid_runs"]:
            out.append(f"  무효 실행 {r['session']}: {r['reasons']}")
    if verdicts:
        out.append("=== 성공 기준(docs/evaluation-voice.md §5, 유효 실행, 독립 검토 전)")
        for r in verdicts:
            out.append(f"  {'통과' if r['ok'] else '미달'}  {r['condition']}: {r['value']}")
    return "\n".join(out)


def _clean(x: Any) -> Any:
    if isinstance(x, float) and (math.isinf(x) or math.isnan(x)):
        return str(x)
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_clean(v) for v in x]
    return x


def main() -> None:
    ap = argparse.ArgumentParser(description="음성 시나리오 실행 보고서")
    ap.add_argument("run_dirs", nargs="+", type=Path)
    ap.add_argument("--json", type=Path, help="보고 값을 JSON으로도 쓴다")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    sessions = load_runs(args.run_dirs)
    stats = {p: policy_stats([x for x in sessions if x["policy"] == p]) for p in sorted({x["policy"] for x in sessions})}
    verdicts = verdict(stats["A"], stats["C"]) if {"A", "C"} <= set(stats) else None
    print(render(stats, verdicts))
    if args.json:
        args.json.write_text(json.dumps(_clean({"runs": [str(d).replace("\\", "/") for d in args.run_dirs],
                                                "policies": stats, "verdict": verdicts}), ensure_ascii=False, indent=1)
                             + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
