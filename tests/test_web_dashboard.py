"""평가 대시보드 집계(jikgam/web/dashboard.py)를 작은 손 로그로 확인한다.

수치는 기존 보고서 함수와 같아야 한다: 판별 실행은 jikgam.eval.report.render의 요약 줄, 시나리오 실행은
jikgam.sim.report.render의 통과·지표 줄과 비교한다.
"""

import hashlib
import json
import math
from pathlib import Path

from sim_logs import LogBuilder, mark

from jikgam.eval import report as er
from jikgam.sim import report as sr
from jikgam.web import dashboard as db


# --- 판별 실행 fixture ------------------------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def make_decision_run(tmp: Path, run_id: str = "20990101T000000") -> Path:
    data = tmp / "data" / "val.jsonl"
    cases = [
        {"id": "c1", "group": "g1", "split": "val", "tags": ["colloquial"], "state": "x" * 1000,
         "gold": json.dumps({"intent": {"label": "refund"}, "applies:A-05": {"label": "true"}})},
        {"id": "c2", "group": "g2", "split": "val", "tags": ["negation"], "state": "y" * 2500,
         "gold": json.dumps({"intent": {"label": "cancel"}, "applies:A-07": {"label": "false"}})},
    ]
    _write(data, "\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n")
    sha = hashlib.sha256(data.read_bytes()).hexdigest()[:16]
    run = tmp / "results" / "decisions" / "val" / run_id
    meta = {"run_id": run_id, "split": "val", "started_kst": "2099-01-01T00:00:00+09:00", "git_commit": "abc123def456",
            "git_dirty": False, "data_file": str(data), "data_sha256_16": sha, "cases": 2, "questions_version": "q-test",
            "llm_prompt_version": "p-test", "backends": {"fast": {"kind": "systemone", "model": "fast-1"},
                                                         "maj": {"kind": "majority"}},
            "reps": {"fast": 2, "maj": 1}, "concurrency": 1, "client": {"python": "3.12", "httpx": "0.28", "os": "test"},
            "network_before": None, "finished_kst": "2099-01-01T00:01:00+09:00"}
    _write(run / "meta.json", json.dumps(meta, ensure_ascii=False))

    def choice(label, p):
        other = {"refund": "cancel", "cancel": "refund"}[label]
        return {"type": "choice", "choice": label, "probabilities": {label: p, other: 1 - p}}

    raw = [
        # fast 0회차: 4개 중 4개, 1회차: c2의 applies를 틀림, 두 번째 호출은 오류
        {"rep": 0, "case": "c1", "backend": "fast", "model": "fast-1", "latency_ms": 100.0, "attempts": 1,
         "input_tokens": 1000, "cost_usd": 0.001, "error": None,
         "answers": {"intent": choice("refund", 0.9), "applies:A-05": {"type": "noul", "noul": 0.8}}},
        {"rep": 0, "case": "c2", "backend": "fast", "model": "fast-1", "latency_ms": 200.0, "attempts": 2,
         "input_tokens": 2000, "cost_usd": 0.003, "error": None,
         "answers": {"intent": choice("cancel", 0.7), "applies:A-07": {"type": "noul", "noul": 0.2}}},
        {"rep": 1, "case": "c1", "backend": "fast", "model": "fast-1", "latency_ms": 300.0, "attempts": 1,
         "input_tokens": 1000, "cost_usd": 0.001, "error": None,
         "answers": {"intent": choice("refund", 0.6), "applies:A-05": {"type": "noul", "noul": 0.55}}},
        {"rep": 1, "case": "c2", "backend": "fast", "model": "fast-1", "latency_ms": 0.0, "attempts": 4,
         "input_tokens": 0, "cost_usd": 0.0, "error": "HTTP 500", "answers": {}},
        {"rep": 0, "case": "c1", "backend": "maj", "model": "maj", "latency_ms": 0.0, "attempts": 1, "input_tokens": 0,
         "cost_usd": 0.0, "error": None, "answers": {"intent": choice("refund", 0.6), "applies:A-05": {"type": "noul", "noul": 0.6}}},
        {"rep": 0, "case": "c2", "backend": "maj", "model": "maj", "latency_ms": 0.0, "attempts": 1, "input_tokens": 0,
         "cost_usd": 0.0, "error": None, "answers": {"intent": choice("refund", 0.6), "applies:A-07": {"type": "noul", "noul": 0.6}}},
    ]
    _write(run / "raw.jsonl", "\n".join(json.dumps(r, ensure_ascii=False) for r in raw) + "\n")
    return run


def test_decision_run_matches_report_render(tmp_path):
    run = make_decision_run(tmp_path)
    d = db.decision_run(run)
    rows = {b["backend"]: b for b in d["backends"]}
    fast = rows["fast"]
    assert fast["counts"] == "2~4/4" and abs(fast["accuracy"] - 0.75) < 1e-12  # (4/4 + 2/4) / 2
    assert fast["p50_ms"] == 200.0 and fast["errors"] == 1 and fast["calls"] == 4 and fast["retries"] == 2
    assert abs(fast["cost_per_request"] - 0.005 / 3) < 1e-12 and fast["latency_n"] == 3
    # report.render의 요약 줄과 같은 값
    meta, cases, raw = er.load(run)
    text = er.render([meta], cases, raw)
    line = next(x for x in text.splitlines() if x.startswith("| fast |"))
    cells = [c.strip() for c in line.strip("|").split("|")]
    assert cells[2] == fast["accuracy_text"]
    assert cells[3] == f"{fast['ece']:.3f}"
    assert cells[4] == f"{fast['p50_ms']:.0f}" and cells[5] == f"{fast['p95_ms']:.0f}"
    assert cells[7] == f"{fast['cost_per_request']:.6f}"
    assert cells[9] == f"{fast['errors']}/{fast['calls']}" and cells[10] == str(fast["retries"])
    assert rows["maj"]["kind"] == "majority" and rows["maj"]["counts"] == "2/4"
    q = {x["qtype"]: x for x in d["qtypes"]}
    assert set(q) == {"intent", "applies"} and q["intent"]["cells"]["maj"]["counts"] == "1/2"
    assert d["questions_version"] == "q-test" and d["decisions_per_rep"] == 4


def test_decision_run_with_changed_gold_file_is_reported_not_computed(tmp_path):
    run = make_decision_run(tmp_path)
    meta = json.loads((run / "meta.json").read_text(encoding="utf-8"))
    Path(meta["data_file"]).write_text("{}\n", encoding="utf-8")
    d = db.decision_run(run)
    assert "정답 파일이 측정 때와 다르다" in d["error"] and "backends" not in d


# --- 시나리오 실행 fixture ----------------------------------------------------------------------------

SCENARIO = {
    "id": "t01", "reveal_rules": [], "mid_call_events": [
        {"after_turn": 1, "event": "번복", "condition_change": True, "invalidates_prior": True}],
    "expected": {"intent": "cancel", "articles": ["A-07"], "action": "answer", "outcome": "해지",
                 "tool_calls": [], "forbidden": [], "must_say": []},
}


def engine_log(refresh_ms: float, stale: bool) -> list[dict]:
    """조건 변경 사건 하나가 있는 엔진 로그. stale이면 조건 변경 뒤에 지난 버전 제안이 노출된다."""
    b = LogBuilder(session="s", step=10)
    b.utt("해지 문의요", t=0.0)
    b.evidence(t=50.0)
    b.suggest("s-s1", t=60.0, cited=["A-07"])
    b.utt("아 환불도요", marker=mark("t01#0"), t=1000.0)
    if stale:
        b.suggest("s-s2", basis=1, t=1100.0, cited=["A-07"])
    b.evidence(t=1000.0 + refresh_ms)
    b.suggest("s-s3", t=1000.0 + refresh_ms + 5, cited=["A-07"])
    return b.events


def make_scenario_run(tmp: Path, run_id: str, policy: str, refresh_ms: float, stale: bool, judged: bool,
                      scenario_file: str | None = None) -> Path:
    scn_path = tmp / "data" / "scenarios.jsonl"
    _write(scn_path, json.dumps(SCENARIO, ensure_ascii=False) + "\n")
    run = tmp / "results" / "engine" / "scenarios" / run_id
    sid = f"t01-r1-{run_id}"
    events = engine_log(refresh_ms, stale)
    for e in events:
        e["session"] = sid
    _write(run / f"{sid}.jsonl", "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n")
    _write(run / f"{sid}.sim.jsonl", json.dumps({"type": "sim.end", "reason": "customer_done"}) + "\n")
    if judged:
        ai = {"verdict": {"forbidden": [], "action_content": {"matches": True, "reason": ""}, "must_say": [],
                          "reveal_rules": [], "mid_call_events": [{"index": 0, "status": "followed", "reason": ""}]},
              "cost_usd": 0.01}
        _write(run / f"{sid}.judge.json", json.dumps(ai, ensure_ascii=False))
    summary = {"run_id": run_id, "policy": policy, "policy_description": f"정책 {policy}", "decision_backend": "jev",
               "decision_model": "jev-1", "generator_model": "gem", "org": "org_a",
               "scenario_file": scenario_file or str(scn_path), "scenario_sha256": "0" * 64, "runs": 1,
               "fast_delay_ms": 1000.0, "customer_model": "cust", "customer_prompt": "cp", "judge_model": "judge",
               "judge_prompt": "jp", "git": {"commit": "f" * 40, "dirty": False}, "started": "2099-01-01T00:00:00+09:00",
               "ended": "2099-01-01T00:10:00+09:00", "note": "test",
               "sessions": [{"scenario": "t01", "session": sid, "run": 1, "end_reason": "customer_done", "error": None,
                             "customer_turns": 2, "executed_events": [0], "missing_events": [],
                             "fast_corrections": [{"event": "t01#0", "gap_ms": 1000.0, "first_done_before_followup": False}],
                             "fast_missing": [], "injections": [], "duration_ms": 2000.0,
                             "costs": {"customer_usd": 0.002, "decision_usd": 0.0005, "generation_usd": 0.004}}]}
    _write(run / "summary.json", json.dumps(summary, ensure_ascii=False))
    return run


def test_scenario_run_stats_match_sim_report(tmp_path):
    run = make_scenario_run(tmp_path, "dev9-r1-C", "C", refresh_ms=200.0, stale=False, judged=True)
    r = db.scenario_run(run)
    s = r["stats"]
    assert r["info"]["group"] == "dev9-C" and r["info"]["set_label"] == "개발용 시나리오"
    assert s["refresh_valid"]["events"] == 1 and s["refresh_valid"]["p50"] == 200.0
    text = sr.render(sr.load_run(run))
    assert f"통과: 유효 실행 {s['valid']}개 중 {s['passed']}개 (판정 불가 {s['undetermined']}개)" in text
    assert f"채널 chat: 무효 노출 {s['stale']['by_channel']['chat']['invalid']} / 분모 " \
           f"{s['stale']['by_channel']['chat']['denominator']}" in text
    assert abs(s["cost"]["engine"] - 0.0045) < 1e-12 and abs(s["cost"]["judge"] - 0.01) < 1e-12
    assert s["fast_corrections"] == 1


def test_groups_merge_repetitions_and_recompute_from_sessions(tmp_path):
    make_scenario_run(tmp_path, "dev9-r1-C", "C", refresh_ms=200.0, stale=False, judged=True)
    r2 = make_scenario_run(tmp_path, "dev9-r2-C", "C", refresh_ms=400.0, stale=False, judged=False)
    make_scenario_run(tmp_path, "dev9-r1-A", "A", refresh_ms=3000.0, stale=True, judged=True)
    summary = json.loads((r2 / "summary.json").read_text(encoding="utf-8"))
    del summary["ended"]  # 진행 중인 실행(실행기가 세션마다 summary.json을 다시 쓴다)
    _write(r2 / "summary.json", json.dumps(summary, ensure_ascii=False))
    data = db.dashboard(tmp_path / "results")
    groups = {g["label"]: g for g in data["scenario_groups"]}
    assert set(groups) == {"dev9 · 정책 A", "dev9 · 정책 C"}
    c = groups["dev9 · 정책 C"]["stats"]
    assert len(groups["dev9 · 정책 C"]["runs"]) == 2 and c["sessions"] == 2
    assert groups["dev9 · 정책 C"]["in_progress"] and not groups["dev9 · 정책 A"]["in_progress"]
    assert c["refresh_valid"]["events"] == 2 and c["refresh_valid"]["p50"] == 300.0  # 두 세션의 값을 모아 다시 계산
    assert c["by_scenario"]["t01"][1] == "판정 불가"  # 판정 파일 없는 회차
    a = groups["dev9 · 정책 A"]["stats"]
    assert a["stale"]["by_channel"]["chat"]["invalid"] == 1 and a["stale"]["events_with_invalid"] == 1
    # JSON으로 보낼 수 있어야 한다(NaN·Infinity 없음)
    json.dumps(data, allow_nan=False)
    assert "운영 성과가 아니다" in data["note"]


def test_holdout_runs_are_locked_without_reading_scenarios(tmp_path):
    # 시나리오 파일 경로가 없는 파일이어도 잠긴 실행은 읽지 않으므로 오류가 나지 않는다
    make_scenario_run(tmp_path, "hold-r1-C", "C", refresh_ms=200.0, stale=False, judged=True,
                      scenario_file=str(tmp_path / "data" / "org_a_holdout.jsonl"))
    data = db.dashboard(tmp_path / "results")
    (g,) = data["scenario_groups"]
    assert g["holdout"] and g["locked"] == ["hold-r1-C"] and g["stats"] is None
    assert data["scenario_runs"][0]["locked"] and data["scenario_runs"][0]["stats"] is None
    # 허용하면 계산하려 하고, 파일이 없으니 오류로 보인다(다른 실행은 계속 보인다)
    data = db.dashboard(tmp_path / "results", allow_holdout=True)
    assert data["scenario_runs"][0]["error"] and not data["scenario_runs"][0]["locked"]


def test_group_key_and_jsonable():
    assert db.jsonable({"a": math.nan, "b": [math.inf, 1.5]}) == {"a": None, "b": ["inf", 1.5]}
    assert db.group_key("dev3-r12-C") == "dev3-C" and db.group_key("20261014T040727-C") == "20261014T040727-C"
    assert db.group_key("smoke-r2") == "smoke"
