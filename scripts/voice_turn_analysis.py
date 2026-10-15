"""음성 미공개 평가의 정책 D 분석: 잠정 확정, 다시 판단한 비용과 지연(docs/evaluation-voice.md §9.3의 보고 항목).

    uv run python scripts/voice_turn_analysis.py results/voice/scenarios/voice-holdout-C \
        results/voice/scenarios/voice-holdout-D --json results/voice/voice-holdout-turns.json

판정(§5)에 쓰지 않는다. 출력은 두 가지로 나눈다.
- registered: §9.3이 실행 전에 보고하기로 한 것. 잠정 확정 수와 그중 확정 자막이 달라 바꾼 수, 선판단 결과를 쓴 비율,
  판별 호출과 비용.
- post_hoc: 결과를 본 뒤(2026-10-15) 정한 분석. 바꾼 짝의 글자 유사도, 짝이 밀린 사례, 고객 발화마다 근거 판단과 첫 응답
  음성까지 걸린 시간, 잠정 문장으로 한 일 중 버려진 것.
문장 내용은 출력하지 않는다.

고객 발화 하나(turn)는 이렇게 본다.
- C: 고객 확정 자막(stt.final) 하나. 기준 시각은 그 t_end_ms(VAD 끝 판정)다.
- D: 잠정 확정(voice.provisional) 하나, 또는 잠정 확정 없이 들어온 확정 자막 하나다. 잠정 확정이 확정 자막과 달라 바뀌었으면
  바꾼 확정 자막이 만든 버전을 그 발화의 최종 버전으로 본다.
- 근거 판단까지: 기준 시각부터, 최종 버전 이상이고 다음 발화의 첫 버전보다 낮은 버전의 evidence.updated가 처음 기록될
  때까지. 그런 기록이 없으면 "판단 전에 다음 발화"로 센다.
- 첫 응답 음성까지: 기준 시각부터, 최종 버전 이상으로 만든 답의 재생이 다음 발화의 기준 시각 전에 처음 시작될 때까지. 없으면
  "다음 발화 전 응답 없음"으로 센다.
- 짝이 밀린 사례: 잠정 확정과 그 짝으로 처리된 확정 자막 사이에 다른 잠정 확정이 있었던 짝. STT가 VAD 두 구간을 확정 자막
  하나로 합치면 그 뒤의 확정 자막이 한 발화씩 앞 잠정 확정과 짝지어진다(jikgam/voice/bridge.py _on_final은 먼저 보낸
  잠정 확정부터 짝짓는다).
"""

import argparse
import difflib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

from jikgam.engine import events as ev
from jikgam.engine.events import read_log
from jikgam.engine.org import ROOT
from jikgam.sim import judge as jd
from jikgam.sim.metrics import percentile_inf
from jikgam.voice import bridge as vb

_STRIP = re.compile(r"[\s\W_]+", re.UNICODE)


def _norm(s: str | None) -> str:
    return _STRIP.sub("", s or "")


def _sim(a: str | None, b: str | None) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def turns_of(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bumped = {e["utt_id"]: int(e["to"]) for e in events if e["type"] == ev.VERSION_BUMPED and e.get("utt_id")}
    turns: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    prov_seen: list[str] = []
    for i, e in enumerate(events):
        if e["type"] == vb.PROVISIONAL:
            u = e["utt_id"]
            turns[u] = {"kind": "provisional", "index": i, "t_end": float(e["t_end_ms"]), "text": e.get("text"),
                        "v_first": bumped.get(u), "v_final": bumped.get(u), "between": 0}
            order.append(u)
            prov_seen.append(u)
        elif e["type"] == vb.PROVISIONAL_DROPPED and e.get("utt_id") in turns:
            turns[e["utt_id"]]["kind"] = "unconfirmed"
        elif e["type"] == ev.STT_FINAL:
            p = e.get("provisional_utt_id")
            if p is not None and p in turns:
                t = turns[p]
                t["kind"] = "confirmed" if e.get("confirms_provisional") else "replaced"
                t["sim"] = _sim(t["text"], e.get("text"))
                t["final_lag_ms"] = float(e["t_ms"]) - t["t_end"]
                t["between"] = len(prov_seen) - 1 - prov_seen.index(p)
                if not e.get("confirms_provisional"):
                    t["v_final"] = bumped.get(e["utt_id"])
                    t["replaced_by"] = e["utt_id"]
            else:
                u = e["utt_id"]
                turns[u] = {"kind": "final", "index": i, "t_end": float(e["t_end_ms"]), "v_first": bumped.get(u),
                            "v_final": bumped.get(u)}
                order.append(u)
    out = [dict(turns[u], utt_id=u) for u in order if turns[u]["v_first"] is not None]
    out.sort(key=lambda t: t["index"])
    for t in out:
        t.pop("text", None)
    return out


def measure(events: list[dict[str, Any]], turns: list[dict[str, Any]]) -> None:
    """turn마다 근거 판단까지, 첫 응답 음성까지, 버려진 일을 붙인다."""
    evidence = [(float(e["t_ms"]), int(e["version"])) for e in events if e["type"] == ev.EVIDENCE_UPDATED]
    basis = {e["suggestion_id"]: int(e["basis_version"]) for e in events if e["type"] == ev.SUGGESTION_CREATED}
    exposed = [(float(e["start_ms"]), basis.get(e["suggestion_id"], -1), e["suggestion_id"]) for e in events
               if e["type"] == ev.SUGGESTION_EXPOSED and e.get("channel") == vb.CHANNEL]
    played = {e["suggestion_id"]: e for e in events if e["type"] == vb.PLAYBACK_END}
    utt_bumps = sorted(int(e["to"]) for e in events if e["type"] == ev.VERSION_BUMPED and e.get("utt_id"))
    for n, t in enumerate(turns):
        nxt = turns[n + 1] if n + 1 < len(turns) else None
        v_hi = nxt["v_first"] if nxt else math.inf
        t_hi = nxt["t_end"] if nxt else math.inf
        vf = t["v_final"]
        if vf is None:
            t["evidence_ms"] = t["first_audio_ms"] = None
            continue
        t1 = next((tm for tm, v in evidence if vf <= v < v_hi and tm >= t["t_end"]), None)
        t["evidence_ms"] = None if t1 is None else round(t1 - t["t_end"], 1)
        a = next((s for s, b, _ in exposed if b >= vf and t["t_end"] <= s < t_hi), None)
        t["first_audio_ms"] = None if a is None else round(a - t["t_end"], 1)
        if t["kind"] == "replaced":
            # 잠정 확정이 올린 버전부터 다음 발화가 버전을 올리기 전까지(그 사이 도구 결과가 올린 버전 포함)로 한 일.
            # 다른 고객 발화나 바꾼 확정 자막이 올린 버전의 일은 넣지 않는다
            lo = t["v_first"]
            hi = next((v for v in utt_bumps if v > lo), math.inf)
            gens = [e for e in events if e["type"] == ev.GENERATION_RESPONSE and lo <= int(e["request_version"]) < hi]
            t["wasted"] = {
                "generation_calls": len(gens), "generation_usd": sum(e.get("cost_usd") or 0 for e in gens),
                "generation_cancelled": sum(e["type"] == ev.TASK_CANCELLED and e.get("stage") == "generation"
                                            and lo <= int(e["started_version"]) < hi for e in events),
                "decision_cancelled": sum(e["type"] == ev.TASK_CANCELLED and e.get("stage") == "decision"
                                          and lo <= int(e["started_version"]) < hi for e in events),
                "played": [round(float(played[sid].get("played_ms") or 0), 1) for s, b, sid in exposed
                           if lo <= b < hi and sid in played]}
            redo = [e for e in events if e["type"] == ev.DECISION_RESPONSE and int(e["request_version"]) == vf]
            t["redo_decision_calls"] = len(redo)
            t["redo_decision_usd"] = sum(e.get("cost_usd") or 0 for e in redo)


def _dist(xs: list[float | None]) -> dict[str, Any]:
    vals = [x for x in xs if x is not None]
    return {"n": len(xs), "measured": len(vals), "none": len(xs) - len(vals),
            "p50": round(percentile_inf(vals, 50), 1) if vals else None,
            "p95": round(percentile_inf(vals, 95), 1) if vals else None}


def analyse(run_dir: Path) -> dict[str, Any]:
    summary = jd.load_summary(run_dir)
    all_turns, sessions, costs = [], 0, {"decision_calls": 0, "prejudge_calls": 0, "decision_usd": 0.0,
                                         "prejudge_usd": 0.0, "generation_calls": 0, "generation_usd": 0.0,
                                         "prejudged_used": 0}
    for sess in summary["sessions"]:
        events = read_log(run_dir / f"{sess['session']}.jsonl")
        sessions += 1
        ts = turns_of(events)
        measure(events, ts)
        for t in ts:
            t["session"] = sess["session"]
        all_turns += ts
        for e in events:
            if e["type"] == ev.DECISION_RESPONSE:
                costs["decision_calls"] += 1
                costs["decision_usd"] += e.get("cost_usd") or 0
                costs["prejudged_used"] += bool(e.get("prejudged"))
            elif e["type"] == ev.DECISION_PREJUDGE:
                costs["prejudge_calls"] += 1
                costs["prejudge_usd"] += e.get("cost_usd") or 0
            elif e["type"] == ev.GENERATION_RESPONSE:
                costs["generation_calls"] += 1
                costs["generation_usd"] += e.get("cost_usd") or 0
    kinds = {k: [t for t in all_turns if t["kind"] == k] for k in ("final", "confirmed", "replaced", "unconfirmed")}
    paired = kinds["confirmed"] + kinds["replaced"]
    rep = kinds["replaced"]
    prov_n = len(paired) + len(kinds["unconfirmed"]) + sum(1 for t in all_turns if t["kind"] == "provisional")
    registered = {
        "sessions": sessions, "customer_turns": len(all_turns),
        "provisional_sent": prov_n, "confirmed": len(kinds["confirmed"]), "replaced": len(rep),
        "unconfirmed": len(kinds["unconfirmed"]),
        "replaced_rate": round(len(rep) / len(paired), 4) if paired else None,
        "prejudged_used_of_decisions": round(costs["prejudged_used"] / costs["decision_calls"], 4)
        if costs["decision_calls"] else None,
        "prejudged_used_of_provisional": round(costs["prejudged_used"] / prov_n, 4) if prov_n else None,
        **{k: round(v, 5) if isinstance(v, float) else v for k, v in costs.items()},
        "decision_calls_per_turn": round((costs["decision_calls"] + costs["prejudge_calls"]) / len(all_turns), 2)
        if all_turns else None,
    }
    wasted = [t["wasted"] for t in rep if "wasted" in t]
    post_hoc = {
        "replaced_similarity": {k: sum(lo <= t["sim"] < hi for t in rep) for k, (lo, hi) in
                                {"0.9 이상": (0.9, 9), "0.7~0.9": (0.7, 0.9), "0.4~0.7": (0.4, 0.7),
                                 "0.4 미만": (0, 0.4)}.items()},
        "shifted_pairs": {"pairs": sum(t.get("between", 0) > 0 for t in paired),
                          "replaced": sum(t.get("between", 0) > 0 for t in rep),
                          "sessions": sorted({t["session"] for t in paired if t.get("between", 0) > 0})},
        "final_lag_ms": _dist([t.get("final_lag_ms") for t in paired]),
        "evidence_ms": {k: _dist([t["evidence_ms"] for t in v]) for k, v in kinds.items() if v},
        "first_audio_ms": {k: _dist([t["first_audio_ms"] for t in v]) for k, v in kinds.items() if v},
        "replaced_wasted": {
            "generation_calls": sum(w["generation_calls"] for w in wasted),
            "generation_usd": round(sum(w["generation_usd"] for w in wasted), 5),
            "generation_cancelled": sum(w["generation_cancelled"] for w in wasted),
            "decision_cancelled": sum(w["decision_cancelled"] for w in wasted),
            "played_replies": sum(len(w["played"]) for w in wasted),
            "played_ms_total": round(sum(sum(w["played"]) for w in wasted), 1),
            "redo_decision_calls": sum(t.get("redo_decision_calls", 0) for t in rep),
            "redo_decision_usd": round(sum(t.get("redo_decision_usd", 0) for t in rep), 5)},
    }
    return {"run_id": summary["run_id"], "policy": summary["policy"], "registered": registered, "post_hoc": post_hoc}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dirs", nargs="+", type=Path)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    out = [analyse((ROOT / d).resolve()) for d in args.run_dirs]
    text = json.dumps({"note": "판정에 쓰지 않는다. registered는 §9.3이 실행 전에 정한 보고 항목, post_hoc은 결과를 본 뒤 "
                               "정한 분석이다(2026-10-15).", "runs": out}, ensure_ascii=False, indent=1)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(text + "\n", encoding="utf-8", newline="\n")
    print(text)


if __name__ == "__main__":
    main()
