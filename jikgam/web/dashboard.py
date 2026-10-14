"""평가 대시보드 자료. 결과 폴더의 원본 로그에서 화면을 열 때 계산한다. 손으로 적은 수치는 없다.

(a) 판별 모델 비교: <results>/decisions/val/<run_id>/의 raw.jsonl, meta.json과 정답 파일(meta의 data_file).
    jikgam.eval.report의 load, decisions, counts_by_rep, fmt_acc, stability와 jikgam.eval.metrics의 ece, percentile로
    계산한다. 요약 줄의 계산은 report.render와 같다(지연은 성공한 호출 전부, 비용·입력 토큰은 성공한 호출의 평균).
(b) 시나리오 정책 비교: <results>/engine/scenarios/<run_id>/의 summary.json, 세션 로그, *.sim.jsonl, *.judge.json.
    jikgam.sim.report.load_run(코드 판정은 로그에서 다시 계산하고 AI 판정은 저장한 파일을 쓴다)과
    jikgam.sim.metrics.aggregate로 계산하며, 유효·무효 실행의 구분은 sim.report와 같다(무효 = 고객 AI 지시 위반).
    실행 이름에서 회차 표시(-r<숫자>)를 뺀 이름이 같은 실행을 한 묶음으로 합친다(dev3-r1-C, dev3-r2-C → dev3-C).
    묶음의 수치는 묶인 실행의 세션을 모두 모아 같은 함수로 다시 계산한다(실행별 값의 평균이 아니다).

보류용 시나리오 실행(summary.json의 scenario_file에 holdout이 들어감)은 엔진 설정을 고정하기 전에는 열람 금지다
(data/scenarios/HOLDOUT.md). 기본으로는 시나리오 파일을 읽지 않고 목록에 잠금으로만 표시하며, allow_holdout일 때만 계산한다.

결과는 실행 폴더 파일들의 수정 시각과 크기로 캐시한다. 진행 중인 실행(summary.json에 ended가 없음)도 그때까지의 세션으로
계산하고 진행 중으로 표시한다.
"""

import math
import re
from pathlib import Path
from typing import Any

from jikgam.eval import report as er
from jikgam.eval.metrics import ece, percentile
from jikgam.sim import judge as jd
from jikgam.sim import metrics as mt
from jikgam.sim import report as sr

NOTE = "합성 데이터와 AI 고객으로 만든 결과이며 운영 성과가 아니다. 모든 수치는 화면을 열 때 원본 로그에서 다시 계산한 값이고, " \
       "독립 검토 전의 실행이 섞여 있을 수 있다(docs/evaluation.md §6)."
RUN_SUFFIX = re.compile(r"-r\d+(?=-|$)")
_cache: dict[tuple[str, str], tuple[tuple, Any]] = {}


def jsonable(x: Any) -> Any:
    """NaN은 None, +∞는 "inf"로 바꾼다(JSON에 NaN·Infinity를 넣지 않는다)."""
    if isinstance(x, float):
        if math.isnan(x):
            return None
        if math.isinf(x):
            return "inf" if x > 0 else "-inf"
        return x
    if isinstance(x, dict):
        return {str(k): jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    return x


def _stamp(run_dir: Path) -> tuple:
    return tuple(sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in run_dir.iterdir() if p.is_file()))


def _cached(kind: str, run_dir: Path, compute) -> Any:
    key = (kind, str(run_dir.resolve()))
    stamp = _stamp(run_dir)
    hit = _cache.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    value = compute(run_dir)
    _cache[key] = (stamp, value)
    return value


# --- (a) 판별 모델 비교 ---------------------------------------------------------------------------


def decision_run(run_dir: Path) -> dict[str, Any]:
    """실행 하나의 요약. report.render의 요약 표와 질문 유형별 정확도 표에 해당한다."""
    try:
        meta, cases, raw = er.load(run_dir)
    except SystemExit as e:  # 정답 파일이 측정 때와 다르면 report.load가 SystemExit을 낸다
        return {"run_id": run_dir.name, "error": str(e)}
    except (OSError, ValueError, KeyError) as e:
        return {"run_id": run_dir.name, "error": f"{type(e).__name__}: {e}"}
    ds = er.decisions(raw, cases)
    backends = list(meta["backends"])
    rows = []
    for b in backends:
        calls = [r for r in raw if r["backend"] == b]
        ok = [r for r in calls if not r["error"]]
        lat = [r["latency_ms"] for r in ok]
        mine = [d for d in ds if d["backend"] == b]
        conf = [d for d in mine if d["prob"] is not None]
        acc, counts = er.counts_by_rep(mine)
        cfg = meta["backends"][b]
        rows.append({
            "backend": b, "kind": cfg.get("kind"), "config_model": cfg.get("model"), "served": cfg.get("served"),
            "models": sorted({r["model"] for r in ok}) or sorted({r["model"] for r in calls}),
            "reps": meta["reps"].get(b), "accuracy": acc, "counts": counts, "accuracy_text": er.fmt_acc(mine),
            "ece": ece([d["prob"] for d in conf], [d["correct"] for d in conf]),
            "p50_ms": percentile(lat, 50), "p95_ms": percentile(lat, 95), "latency_n": len(lat),
            "input_tokens": sum(r["input_tokens"] for r in ok) / len(ok) if ok else math.nan,
            "cost_per_request": sum(r["cost_usd"] for r in ok) / len(ok) if ok else math.nan,
            "errors": sum(1 for r in calls if r["error"]), "calls": len(calls),
            "retries": sum(r["attempts"] > 1 for r in calls), "stability": er.stability(mine),
        })
    present = [q for q in er.QTYPES if any(d["qtype"] == q for d in ds)]
    qtypes = []
    for q in present:
        cells = {}
        for b in backends:
            sub = [d for d in ds if d["backend"] == b and d["qtype"] == q]
            acc, counts = er.counts_by_rep(sub)
            cells[b] = {"accuracy": acc, "counts": counts, "text": er.fmt_acc(sub)}
        n = sum(1 for d in ds if d["backend"] == backends[0] and d["qtype"] == q and d["rep"] == ds[0]["rep"]) if ds else 0
        qtypes.append({"qtype": q, "n": n, "cells": cells})
    net = meta.get("network_before")
    return {
        "run_id": meta["run_id"], "split": meta["split"], "started": meta.get("started_kst"),
        "finished": meta.get("finished_kst"), "questions_version": meta.get("questions_version"),
        "llm_prompt_version": meta.get("llm_prompt_version"), "git_commit": meta.get("git_commit"),
        "git_dirty": meta.get("git_dirty"), "data_file": meta.get("data_file"), "data_sha256_16": meta.get("data_sha256_16"),
        "cases": meta.get("cases"), "decisions_per_rep": len(ds) // max(sum(meta["reps"].values()), 1),
        "reconstructed": bool(meta.get("reconstructed")),
        "network": None if net is None else {"colo": net.get("cloudflare_colo"), "loc": net.get("cloudflare_loc"),
                                             "tcp_ms": net.get("tcp_connect_ms_median")},
        "concurrency": meta.get("concurrency"), "backends": rows, "qtypes": qtypes,
    }


def decision_runs(results_root: Path, split: str = "val") -> list[dict[str, Any]]:
    base = results_root / "decisions" / split
    if not base.is_dir():
        return []
    dirs = sorted(d for d in base.iterdir() if d.is_dir() and (d / "meta.json").exists() and (d / "raw.jsonl").exists())
    return [_cached("decision", d, decision_run) for d in dirs]


# --- (b) 시나리오 정책 비교 -----------------------------------------------------------------------


def is_holdout(summary: dict[str, Any]) -> bool:
    return "holdout" in str(summary.get("scenario_file", ""))


def group_key(run_id: str) -> str:
    return RUN_SUFFIX.sub("", run_id)


def session_stats(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """load_run 세션 목록의 판정·지표·비용. 계산과 유효 실행 구분은 sim.report.render와 같다."""
    valid = [x for x in sessions if x["result"]["compliance"]["valid"] is not False]
    invalid = [x for x in sessions if x["result"]["compliance"]["valid"] is False]
    passed = [x for x in valid if x["result"]["pass"] is True]
    failed = [x for x in valid if x["result"]["pass"] is False]
    undetermined = [x for x in valid if x["result"]["pass"] is None]
    item_fail = {n: sum(x["result"]["items"][n]["ok"] is False for x in valid) for n in jd.ITEMS}
    agg_valid = mt.aggregate([x["metrics"] for x in valid])
    agg_all = mt.aggregate([x["metrics"] for x in sessions])
    stale = agg_valid["stale"]
    cost = {"customer": 0.0, "decision": 0.0, "generation": 0.0, "judge": 0.0}
    for x in sessions:
        c = x.get("costs") or {}
        cost["customer"] += c.get("customer_usd") or 0
        cost["decision"] += c.get("decision_usd") or 0
        cost["generation"] += c.get("generation_usd") or 0
        cost["judge"] += (x.get("ai") or {}).get("cost_usd") or 0
    by_scenario: dict[str, list[str]] = {}
    for x in sessions:
        r = x["result"]
        mark = "무효" if r["compliance"]["valid"] is False else {True: "통과", False: "실패", None: "판정 불가"}[r["pass"]]
        by_scenario.setdefault(x["scenario"], []).append(mark)
    last_fail = [x for x in valid if x["result"]["aux"]["item10_last"]["ok"] is False]
    fast = [f for x in sessions for f in x.get("fast_corrections") or []]
    return {
        "sessions": len(sessions), "valid": len(valid), "invalid": len(invalid), "invalid_scenarios": [x["scenario"] for x in invalid],
        "passed": len(passed), "failed": len(failed), "undetermined": len(undetermined),
        "item_fail": {str(n): item_fail[n] for n in jd.ITEMS}, "item_names": {str(n): jd.ITEM_NAMES[n] for n in jd.ITEMS},
        "item10_last_fail": len(last_fail),
        "refresh_valid": agg_valid["refresh"]["stats"], "refresh_all": agg_all["refresh"]["stats"],
        "stale": {"by_channel": {ch: {k: c[k] for k in ("invalid", "denominator", "unconfirmed")}
                                 for ch, c in stale["by_channel"].items()},
                  "events_with_invalid": stale["events_with_invalid"], "events_total": stale["events_total"]},
        "cost": {**cost, "engine": cost["decision"] + cost["generation"], "total": sum(cost.values())},
        "fast_corrections": len(fast),
        "cancelled": sum(x["counts"]["task.cancelled"] for x in sessions),
        "discarded": sum(x["counts"]["result.discarded"] for x in sessions),
        "by_scenario": by_scenario,
    }


def _run_info(run_dir: Path, summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": summary.get("run_id", run_dir.name), "dir": run_dir.name, "policy": summary.get("policy"),
        "policy_description": summary.get("policy_description"), "decision_backend": summary.get("decision_backend"),
        "decision_model": summary.get("decision_model"), "generator_model": summary.get("generator_model"),
        "scenario_file": summary.get("scenario_file"), "holdout": is_holdout(summary),
        "set_label": "보류용 시나리오" if is_holdout(summary) else "개발용 시나리오",
        "runs": summary.get("runs"), "started": summary.get("started"), "ended": summary.get("ended"),
        "in_progress": summary.get("ended") is None, "git_commit": (summary.get("git") or {}).get("commit"),
        "git_dirty": (summary.get("git") or {}).get("dirty"), "fast_delay_ms": summary.get("fast_delay_ms"),
        "customer_model": summary.get("customer_model"), "judge_model": summary.get("judge_model"),
        "sessions_listed": len(summary.get("sessions") or []), "group": group_key(summary.get("run_id", run_dir.name)),
    }


def scenario_run(run_dir: Path, allow_holdout: bool = False) -> dict[str, Any]:
    """실행 하나: 정보, 세션 목록(load_run 결과), 통계. 보류용이고 allow_holdout이 아니면 시나리오 파일을 읽지 않는다."""
    try:
        summary = jd.load_summary(run_dir)
    except (OSError, ValueError) as e:
        return {"info": {"run_id": run_dir.name, "dir": run_dir.name, "group": group_key(run_dir.name)},
                "error": f"{type(e).__name__}: {e}"}
    info = _run_info(run_dir, summary)
    if info["holdout"] and not allow_holdout:
        return {"info": info, "locked": True}
    try:
        run = sr.load_run(run_dir)
    except Exception as e:  # 진행 중인 실행의 반쯤 쓴 줄 등. 화면에 오류로 보이고 다른 실행은 계속 보인다
        return {"info": info, "error": f"{type(e).__name__}: {e}"}
    return {"info": info, "sessions": run["sessions"], "stats": session_stats(run["sessions"])}


def scenario_runs(results_root: Path, allow_holdout: bool = False) -> list[dict[str, Any]]:
    base = results_root / "engine" / "scenarios"
    if not base.is_dir():
        return []
    dirs = sorted(d for d in base.iterdir() if d.is_dir() and (d / "summary.json").exists())
    return [_cached(f"scenario:{allow_holdout}", d, lambda p: scenario_run(p, allow_holdout)) for d in dirs]


def scenario_groups(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """같은 묶음 이름·정책·시나리오 세트의 실행을 합친다. 잠긴 실행과 오류 난 실행은 합치지 않고 따로 센다."""
    groups: dict[tuple, dict[str, Any]] = {}
    for r in runs:
        info = r["info"]
        key = (info.get("group"), info.get("policy"), info.get("holdout"))
        g = groups.setdefault(key, {"group": info.get("group"), "policy": info.get("policy"),
                                    "holdout": info.get("holdout"), "set_label": info.get("set_label"),
                                    "runs": [], "locked": [], "errors": [], "sessions": []})
        if r.get("locked"):
            g["locked"].append(info["run_id"])
        elif r.get("error"):
            g["errors"].append({"run_id": info["run_id"], "error": r["error"]})
        else:
            g["runs"].append(info)
            g["sessions"] += r["sessions"]
    out = []
    for g in groups.values():
        sessions = g.pop("sessions")
        prefix = g["group"] or ""
        if g["policy"] and prefix.endswith(f"-{g['policy']}"):
            prefix = prefix[: -len(g["policy"]) - 1]
        g["prefix"] = prefix
        g["label"] = f"{prefix} · 정책 {g['policy']}" if g["policy"] else prefix
        g["in_progress"] = any(i["in_progress"] for i in g["runs"])
        g["stats"] = session_stats(sessions) if sessions else None
        out.append(g)
    return sorted(out, key=lambda g: (bool(g["holdout"]), g["group"] or "", g["policy"] or ""))


def dashboard(results_root: Path, allow_holdout: bool = False) -> dict[str, Any]:
    runs = scenario_runs(results_root, allow_holdout)
    run_rows = [{"info": r["info"], "locked": bool(r.get("locked")), "error": r.get("error"),
                 "stats": r.get("stats")} for r in runs]
    return jsonable({
        "note": NOTE, "results_root": str(results_root), "allow_holdout": allow_holdout,
        "decision_runs": decision_runs(results_root),
        "scenario_groups": scenario_groups(runs), "scenario_runs": run_rows,
    })
