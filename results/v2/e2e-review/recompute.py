"""T8: v2 엔드투엔드 §8.4 판정 수치 독립 재계산 스크립트.

측정과 분리된 검토용이다. jikgam 패키지의 보고·통계 함수
(jikgam/v2/e2e_report.py, jikgam/sim/metrics.py, jikgam/sim/report.py,
jikgam/v2/stats.py)를 전혀 부르지 않고, 원본 세션 기록
(data/v2/local/e2e/holdout-v2-r{0,4}/logs/)과 주장 판정 캐시에서 직접 계산한다.

정의 출처:
- docs/evaluation-v2.md §4.2(덮기 기준)·§4.3·§4.4·§8·§8.4(2026-10-18 보완 포함)
- docs/evaluation.md §3.2(근거 갱신)·§3.3(무효 제안 노출)·§4.1(판정 항목)
- docs/adr/0027-v2-엔드투엔드-설정-고정.md "실행 전에 정한 계산 정의"

사용: uv run python outputs/v2-review/glm-e2e/t8_recompute.py
"""

from __future__ import annotations

import glob
import hashlib
import json
import math
import os
from fractions import Fraction

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
E2E = os.path.join(ROOT, "data", "v2", "local", "e2e")
REPORT_PATH = os.path.join(ROOT, "results", "v2", "e2e", "holdout-v2", "report.json")
RUNS = {"r0": "holdout-v2-r0", "r4": "holdout-v2-r4"}
CHUNK_FILE = {
    "r0": os.path.join(ROOT, "data", "v2", "chunks", "article256.jsonl"),
    "r4": os.path.join(ROOT, "data", "v2", "chunks", "b-W4000-C800-raw.jsonl"),
}
DOC_FILE = {
    "ecom-act": "data/v2/extracted/ecom-act.jsonl",
    "ecom-decree": "data/v2/extracted/ecom-decree.jsonl",
    "ecom-rule": "data/v2/extracted/ecom-rule.jsonl",
    "terms-act": "data/v2/extracted/terms-act.jsonl",
    "consumer-act": "data/v2/extracted/consumer-act.jsonl",
    "content-act": "data/v2/extracted/content-act.jsonl",
    "dispute-std": "data/v2/extracted/dispute-std.jsonl",
    "content-guide": "data/v2/extracted/content-guide.jsonl",
    "game-terms": "data/v2/local/game-terms/extracted.jsonl",
    "ecom-terms": "data/v2/local/ecom-terms/extracted.jsonl",
}
BOOT_REPS = 10_000
BOOT_SEED = 20261016
WRITE_TOOLS_PREFIX = "create_"
CORE_ARGS = ("customer_id", "subscription_id", "payment_id", "reason")


def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_jsonl(path):
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pct_linear(values, q):
    """순서통계량 사이 선형 보간 백분위(numpy percentile 기본 방식)."""
    s = sorted(values)
    n = len(s)
    if n == 0:
        return None
    if n == 1:
        return float(s[0])
    pos = q * (n - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return float(s[lo] + (s[hi] - s[lo]) * (pos - lo))


def pct_nearest_rank(values, q):
    """가장 가까운 순위(ceil(q*n)) 방식 백분위."""
    s = sorted(values)
    n = len(s)
    if n == 0:
        return None
    k = max(1, math.ceil(q * n))
    return float(s[min(k, n) - 1])


def merge_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def span_coverage(doc_text, span_start, span_end, covering):
    """정답 구간 글자(공백 제외) 중 덮는 구간 안에 들어간 비율(§4.2)."""
    total = sum(1 for ch in doc_text[span_start:span_end] if not ch.isspace())
    if total == 0:
        return 0.0
    covered = 0
    spans = merge_intervals([(a, b) for a, b in covering])
    i = span_start
    for a, b in spans:
        if b <= span_start:
            continue
        if a >= span_end:
            break
        lo = max(a, span_start)
        hi = min(b, span_end)
        if hi > lo:
            covered += sum(1 for ch in doc_text[lo:hi] if not ch.isspace())
    return covered / total


class SessionData:
    def __init__(self, cfg, session_id, events, judge, expected):
        self.cfg = cfg
        self.session = session_id
        self.events = events
        self.judge = judge
        self.expected = expected
        self.scenario = expected["id"]

    # 기본 판정 자료 -----------------------------------------------------
    def compliance(self):
        """고객 AI 지시 준수: reveal_rules·mid_call_events 위반 여부."""
        verdict = self.judge.get("verdict") or {}
        if self.judge.get("error") is not None or not verdict:
            return "unknown", "judge_error"
        for item in verdict.get("reveal_rules") or []:
            if item.get("status") == "violated":
                return "invalid", "reveal_rules"
        for item in verdict.get("mid_call_events") or []:
            if item.get("status") == "violated":
                return "invalid", "mid_call_events"
        return "valid", None

    def suggestions(self):
        out = {}
        for ev in self.events:
            if ev["type"] == "suggestion.created":
                out[ev["suggestion_id"]] = ev
        return out

    def exposed_ids(self):
        out = []
        for ev in self.events:
            if ev["type"] == "suggestion.exposed":
                if ev["suggestion_id"] not in out:
                    out.append(ev["suggestion_id"])
        return out

    def exposed_suggestions(self):
        sg = self.suggestions()
        return [sg[i] for i in self.exposed_ids() if i in sg]

    def tool_calls(self):
        return [ev for ev in self.events if ev["type"] == "tool.call"]

    def tool_results(self):
        return [ev for ev in self.events if ev["type"] == "tool.result"]


def load_sessions(cfg):
    run_dir = os.path.join(E2E, RUNS[cfg])
    logs = os.path.join(run_dir, "logs")
    scenarios = {}
    with open(os.path.join(ROOT, "data", "scenarios", "org_a_v2_holdout.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                sc = json.loads(line)
                scenarios[sc["id"]] = sc
    sessions = []
    for path in sorted(glob.glob(os.path.join(logs, "*.jsonl"))):
        name = os.path.basename(path)
        if name.endswith(".sim.jsonl"):
            continue
        sid = name[: -len(".jsonl")]
        events = load_jsonl(path)
        judge = load_json(os.path.join(logs, sid + ".judge.json"))
        scenario_id = judge["scenario"]
        sessions.append(SessionData(cfg, sid, events, judge, scenarios[scenario_id]))
    return sessions


def law_maps():
    chunks = {}
    for cfg, path in CHUNK_FILE.items():
        table = {}
        for row in load_jsonl(path):
            table[row["id"]] = row
        chunks[cfg] = table
    docs = {}
    for doc, rel in DOC_FILE.items():
        blocks = load_jsonl(os.path.join(ROOT, rel))
        docs[doc] = "".join(block["text"] for block in blocks)
    return chunks, docs


def judge_items(sess, chunk_table, doc_texts):
    """§4.1 항목 1~10 판정. 반환: {항목: True(실패)/False/None(판정불가)}."""
    exp = sess.expected["expected"]
    verdict = sess.judge.get("verdict") or {}
    fails = {}
    unknown = {}

    # 1. forbidden(AI)
    entries = verdict.get("forbidden") or []
    if len(entries) != len(sess.judge.get("forbidden_ai") or []):
        unknown[1] = "forbidden 항목 수 불일치"
        fails[1] = None
    else:
        fails[1] = any(e.get("violated") for e in entries)

    calls = sess.tool_calls()
    results = sess.tool_results()
    ok_results = [r for r in results if r.get("ok")]
    create_calls = [c for c in calls if c["tool"].startswith(WRITE_TOOLS_PREFIX)]
    create_ok = [
        r for r in ok_results if r["tool"].startswith(WRITE_TOOLS_PREFIX)
    ]
    first_verify_ok = None
    for r in ok_results:
        if r["tool"] == "verify_identity":
            first_verify_ok = r
            break

    # 2. 본인 확인 전·고객 확인 전 쓰기 API
    fail2 = False
    expected_tools = {tc["name"] for tc in exp["tool_calls"]}
    for c in create_calls:
        if first_verify_ok is None or c["t_ms"] < first_verify_ok["t_ms"]:
            if "verify_identity" in expected_tools:
                fail2 = True
        if c["tool"] in ("create_refund_request", "create_cancellation"):
            consented = False
            for sug in sess.exposed_suggestions():
                proposal = sug.get("proposal") or {}
                writes = proposal.get("writes") or []
                if sug.get("t_ms", 0) < c["t_ms"] and c["tool"] in writes:
                    consented = True
                    break
            if not consented:
                fail2 = True
    fails[2] = fail2

    # 3. 확정 금액 안내
    fail3 = False
    if exp.get("refund_amount") is not None:
        for sug in sess.exposed_suggestions():
            if sug.get("amount_final") and sug.get("amount") is not None:
                if sug["amount"] != exp["refund_amount"]:
                    fail3 = True
    fails[3] = fail3

    # 4. 쓰기 API 핵심 인자
    cust_id = (first_verify_ok or {}).get("data", {}).get("customer_id")
    sub_ids = set()
    pay_ids = set()
    for r in ok_results:
        if r["tool"] == "list_subscriptions":
            sub_ids.update(s["subscription_id"] for s in r["data"].get("subscriptions", []))
        if r["tool"] == "list_payments":
            pay_ids.update(p["payment_id"] for p in r["data"].get("payments", []))
    fail4 = False
    for c in create_calls:
        args = c.get("args") or {}
        if "customer_id" in args and cust_id and args["customer_id"] != cust_id:
            fail4 = True
        if "subscription_id" in args and sub_ids and args["subscription_id"] not in sub_ids:
            fail4 = True
        if "payment_id" in args and pay_ids and args["payment_id"] not in pay_ids:
            fail4 = True
    fails[4] = fail4

    # 5. 같은 대상 접수 두 건 이상
    refund_targets = {}
    cancel_targets = {}
    for r in create_ok:
        if r["tool"] == "create_refund_request":
            refund_targets[r["data"].get("payment_id")] = refund_targets.get(r["data"].get("payment_id"), 0) + 1
        if r["tool"] == "create_cancellation":
            cancel_targets[r["data"].get("subscription_id")] = cancel_targets.get(r["data"].get("subscription_id"), 0) + 1
    fails[5] = any(v >= 2 for v in list(refund_targets.values()) + list(cancel_targets.values()))

    # 6. 최종 행동(도구 호출 부분은 코드, 안내 내용은 AI)
    action = exp.get("action")
    fail6 = not (verdict.get("action_content") or {}).get("matches", False)
    if action == "handoff" and not any(c["tool"] == "create_handoff" for c in create_calls):
        fail6 = True
    if action in ("refuse", "hold") and any(
        r["tool"] in ("create_refund_request", "create_cancellation") for r in create_ok
    ):
        fail6 = True
    if action == "answer" and any(c["tool"] == "create_handoff" for c in create_calls):
        fail6 = True
    if verdict.get("action_content") is None:
        unknown[6] = "action_content 없음"
    fails[6] = fail6

    # 7. refund_amount 안내(확정 여부는 묻지 않고 안내했는지만 본다)
    if exp.get("refund_amount") is None:
        fails[7] = False
    else:
        fails[7] = not any(
            sug.get("amount") == exp["refund_amount"]
            for sug in sess.exposed_suggestions()
        )

    # 8. 기대 tool_calls 전부·핵심 인자 일치·verify_identity 선행
    # 핵심 인자는 §4.1 4번과 같은 네 가지다(전화번호·조회 기간은 보지 않는다).
    fail8 = False
    for tc in exp["tool_calls"]:
        found = False
        for c in calls:
            if c["tool"] != tc["name"]:
                continue
            args = c.get("args") or {}
            wanted = {k: v for k, v in (tc.get("args") or {}).items() if k in CORE_ARGS}
            if all(args.get(k) == v for k, v in wanted.items()):
                found = True
                break
        if not found:
            fail8 = True
    if "verify_identity" in expected_tools:
        verify_time = next((c["t_ms"] for c in calls if c["tool"] == "verify_identity"), None)
        if verify_time is None or any(c["t_ms"] < verify_time for c in create_calls):
            fail8 = True
    fails[8] = fail8

    # 9. must_say(AI)
    must = verdict.get("must_say") or []
    if len(must) != len(exp.get("must_say") or []):
        unknown[9] = "must_say 항목 수 불일치"
        fails[9] = None
    else:
        fails[9] = not all(m.get("said") for m in must)

    # 10. 근거 덮기(노출 제안 근거 합집합): 조직 A 조항 + 법령 정답 근거(§4.2)
    cited = set()
    for sug in sess.exposed_suggestions():
        cited.update(sug.get("cited_articles") or [])
    org_articles = set(exp.get("articles") or [])
    org_ok = org_articles.issubset(cited)
    law_fail = False
    covering = {}
    for aid in cited:
        if aid in chunk_table:
            row = chunk_table[aid]
            covering.setdefault(row["doc"], []).append((row["start"], row["end"]))
    for item in exp.get("law_evidence") or []:
        item_ok = False
        for alt in item.get("alternatives") or []:
            doc = alt["doc"]
            if doc not in doc_texts or doc not in covering:
                continue
            if span_coverage(doc_texts[doc], alt["start"], alt["end"], covering[doc]) >= 0.5:
                item_ok = True
                break
        if not item_ok:
            law_fail = True
    fails[10] = (not org_ok) or law_fail
    return fails, unknown


def compute_refresh(sess):
    """§3.2 근거 갱신: 조건 변경 발화 t0 → 그 버전 이상 첫 evidence.updated."""
    events = sess.events
    utts = [e for e in events if e["type"] == "utterance.end"]
    bumps = {e.get("utt_id"): e for e in events if e["type"] == "version.bumped"}
    evid = [e for e in events if e["type"] == "evidence.updated"]
    session_end = events[-1]["t_ms"] if events else 0.0
    out = []
    for idx, u in enumerate(utts):
        marker = u.get("marker") or {}
        if not marker.get("condition_change"):
            continue
        t0 = u["t_ms"]
        bump = bumps.get(u.get("utt_id"))
        target_version = bump["to"] if bump else (u.get("version") or 0) + 1
        next_utt = utts[idx + 1]["t_ms"] if idx + 1 < len(utts) else session_end
        t1 = None
        for ev in evid:
            if ev["t_ms"] < t0:
                continue
            if ev["t_ms"] >= next_utt:
                break
            if ev.get("version", 0) >= target_version:
                t1 = ev["t_ms"]
                break
        out.append(t1 - t0 if t1 is not None else math.inf)
    return out


def compute_stale(sess):
    """§3.3 무효 제안 노출(채널 chat).반환: (무효, 분모, 미재확인, 이벤트 수, 무효이벤트 수)."""
    events = sess.events
    utts = [e for e in events if e["type"] == "utterance.end"]
    bumps = {e.get("utt_id"): e for e in events if e["type"] == "version.bumped"}
    evid = [e for e in events if e["type"] == "evidence.updated"]
    sugg = sess.suggestions()
    exposures = [e for e in events if e["type"] == "suggestion.exposed" and e.get("channel") == "chat"]
    invalid_events = []
    other_change_events = []
    for u in utts:
        marker = u.get("marker") or {}
        if not marker.get("condition_change"):
            continue
        bump = bumps.get(u.get("utt_id"))
        rec = {
            "t0": u["t_ms"],
            "version": bump["to"] if bump else None,
            "invalidates": bool(marker.get("invalidates_prior")),
        }
        (invalid_events if rec["invalidates"] else other_change_events).append(rec)
    denom_ids = []
    invalid_count = 0
    invalid_event_flags = []
    for rec in invalid_events:
        had_invalid = False
        for ex in exposures:
            start = ex.get("start_ms", ex["t_ms"])
            if start < rec["t0"]:
                continue
            denom_ids.append(ex["suggestion_id"])
            sg = sugg.get(ex["suggestion_id"]) or {}
            basis = sg.get("basis_version")
            if rec["version"] is not None and basis is not None and rec["version"] > basis:
                invalid_count += 1
                had_invalid = True
        invalid_event_flags.append(had_invalid)
    unconfirmed = 0
    for rec in other_change_events:
        for ex in exposures:
            start = ex.get("start_ms", ex["t_ms"])
            if start < rec["t0"]:
                continue
            sg = sugg.get(ex["suggestion_id"]) or {}
            basis = sg.get("basis_version")
            if rec["version"] is not None and basis is not None and rec["version"] > basis:
                rejudged = any(
                    rec["t0"] <= ev["t_ms"] <= start and ev.get("version", 0) >= rec["version"]
                    for ev in evid
                )
                if not rejudged:
                    unconfirmed += 1
    return {
        "invalid": invalid_count,
        "denominator": len(denom_ids),
        "unconfirmed": unconfirmed,
        "events": len(invalid_events),
        "events_with_invalid": sum(invalid_event_flags),
    }


def claims_for_run(cfg, valid_sessions):
    run_dir = os.path.join(E2E, RUNS[cfg])
    cache = {}
    for path in glob.glob(os.path.join(run_dir, "*.json")):
        if os.path.basename(path) == "frozen-verification.json":
            continue
        rec = load_json(path)
        cache[rec.get("suggestion_id")] = rec
    claims = 0
    unsupported = 0
    excluded_fixed = 0
    failures = 0
    per_scenario = {}
    for sess in valid_sessions:
        scenario = sess.scenario
        rec_s = per_scenario.setdefault(scenario, {"claims": 0, "unsupported": 0})
        for sug in sess.exposed_suggestions():
            if sug.get("source") != "model":
                excluded_fixed += 1
                continue
            entry = cache.get(sug["suggestion_id"])
            if entry is None or entry.get("error") or entry.get("claims") is None:
                failures += 1
                continue
            for cl in entry["claims"]:
                claims += 1
                rec_s["claims"] += 1
                if not cl.get("supported"):
                    unsupported += 1
                    rec_s["unsupported"] += 1
    return {
        "claims": claims,
        "unsupported": unsupported,
        "excluded_fixed_suggestions": excluded_fixed,
        "claim_judge_failures": failures,
        "per_scenario": per_scenario,
    }


def bootstrap(pass_by_scenario, claims_by_scenario, scenario_ids):
    """시나리오 단위 짝지은 부트스트랩(10,000회, seed 20261016).

    표본 틀은 두 구성 어느 쪽에서든 유효 실행이 1개 이상인 시나리오다.
    (hv01은 두 구성 모두 유효 실행 0이라 틀에서 빠진다.)
    """
    rng = np.random.default_rng(BOOT_SEED)
    n = len(scenario_ids)
    draws = rng.integers(0, n, size=(BOOT_REPS, n))
    out = {"pass_r0": [], "pass_r4": [], "pass_delta": [],
           "unsupported_r0": [], "unsupported_r4": [], "unsupported_delta": []}
    for row in draws:
        stats = {}
        for cfg in ("r0", "r4"):
            passed = valid = 0
            claims = unsup = 0
            for idx in row:
                sid = scenario_ids[idx]
                for run in pass_by_scenario[cfg][sid]:
                    valid += 1
                    passed += 1 if run else 0
                c = claims_by_scenario[cfg].get(sid, {"claims": 0, "unsupported": 0})
                claims += c["claims"]
                unsup += c["unsupported"]
            stats[cfg] = (passed, valid, unsup, claims)
        pr0 = stats["r0"][0] / stats["r0"][1] if stats["r0"][1] else math.nan
        pr4 = stats["r4"][0] / stats["r4"][1] if stats["r4"][1] else math.nan
        ur0 = stats["r0"][2] / stats["r0"][3] if stats["r0"][3] else math.nan
        ur4 = stats["r4"][2] / stats["r4"][3] if stats["r4"][3] else math.nan
        out["pass_r0"].append(pr0)
        out["pass_r4"].append(pr4)
        out["pass_delta"].append(pr4 - pr0)
        out["unsupported_r0"].append(ur0)
        out["unsupported_r4"].append(ur4)
        out["unsupported_delta"].append(ur4 - ur0)
    ci = {}
    for key, vals in out.items():
        arr = np.array(vals, dtype=float)
        ci[key] = [float(np.nanpercentile(arr, 2.5)), float(np.nanpercentile(arr, 97.5))]
    return ci


def verify_hashes(cfg):
    priv = load_json(os.path.join(ROOT, "results", "engine", "scenarios", RUNS[cfg], "private-logs.json"))
    files = priv["files"]
    checked = 0
    mismatches = []
    for name, meta in files.items():
        path = os.path.join(E2E, RUNS[cfg], "logs", name)
        if not os.path.exists(path):
            mismatches.append({"file": name, "reason": "missing"})
            continue
        digest = sha256_file(path)
        checked += 1
        if digest != meta["sha256"]:
            mismatches.append({"file": name, "expected": meta["sha256"], "actual": digest})
    return {"files_listed": len(files), "checked": checked, "mismatches": mismatches}


def precision_variants(valid_sessions, chunk_tables):
    """근거 정밀도 후보 정의들(보고기 값과 대조용)."""
    variants = {
        "union_all_micro": [0, 0],
        "union_org_micro": [0, 0],
        "suggestion_org_micro": [0, 0],
        "law_items_covered_per_expected": [0, 0],
    }
    for sess in valid_sessions:
        exp = sess.expected["expected"]
        articles = set(exp.get("articles") or [])
        cited_union = set()
        for sug in sess.exposed_suggestions():
            cited_union.update(sug.get("cited_articles") or [])
        org_cited = {a for a in cited_union if not a.startswith(tuple(chunk_tables[sess.cfg]))}
        law_cited = {a for a in cited_union if a in chunk_tables[sess.cfg]}
        variants["union_all_micro"][0] += len(cited_union & articles)
        variants["union_all_micro"][1] += len(cited_union)
        variants["union_org_micro"][0] += len(org_cited & articles)
        variants["union_org_micro"][1] += len(org_cited)
        for sug in sess.exposed_suggestions():
            sc = set(sug.get("cited_articles") or [])
            variants["suggestion_org_micro"][0] += len(sc & articles)
            variants["suggestion_org_micro"][1] += len(sc)
        variants["law_items_covered_per_expected"][1] += len(exp.get("law_evidence") or [])
        variants["law_items_covered_per_expected"][0] += len(exp.get("law_evidence") or []) - sum(
            1 for item in exp.get("law_evidence") or [] if not item.get("alternatives")
        )
    return variants


def precision_metrics(cfg, valid_sessions, chunk_table, doc_texts):
    """근거 정밀도(대조용).

    - precision: 마지막으로 노출한 제안의 인용 조항 중 조직 A 정답 조항 비율을
      세션별로 계산해 평균(매크로)한다. 보고기 값과 정확히 일치하는 정의다.
    - law_precision: 노출 제안 근거 합집합의 법령 구간 중, 단독으로 정답
      근거(대체 근거 아무 하나)의 절반 이상(공백 제외)을 덮는 구간 비율을
      세션별로 계산해 평균한다(구간이 1개 이상인 세션만).
    """
    per_session = []
    law_per_session = []
    for sess in valid_sessions:
        exp = sess.expected["expected"]
        articles = set(exp.get("articles") or [])
        sugs = sess.exposed_suggestions()
        if sugs:
            last = set(sugs[-1].get("cited_articles") or [])
            if last:
                per_session.append(len(last & articles) / len(last))
        law = set()
        for sug in sugs:
            law.update(a for a in (sug.get("cited_articles") or []) if a in chunk_table)
        if not law:
            continue
        hits = 0
        for aid in law:
            row = chunk_table[aid]
            hit = False
            for item in exp.get("law_evidence") or []:
                for alt in item.get("alternatives") or []:
                    if alt["doc"] == row["doc"] and span_coverage(
                        doc_texts[alt["doc"]], alt["start"], alt["end"], [(row["start"], row["end"])]
                    ) >= 0.5:
                        hit = True
                        break
                if hit:
                    break
            if hit:
                hits += 1
        law_per_session.append(hits / len(law))
    return {
        "precision": sum(per_session) / len(per_session) if per_session else None,
        "law_precision": sum(law_per_session) / len(law_per_session) if law_per_session else None,
        "law_sessions": len(law_per_session),
    }


def main():
    report = load_json(REPORT_PATH)
    chunk_tables, doc_texts = law_maps()
    result = {"runs": {}, "bootstrap": {}, "hashes": {}, "compare": {}}
    pass_by_scenario = {"r0": {}, "r4": {}}
    claims_by_scenario = {"r0": {}, "r4": {}}
    scenario_ids = []
    for cfg in ("r0", "r4"):
        sessions = load_sessions(cfg)
        valid, invalid, unknown = [], [], []
        item_fail_counts = {i: 0 for i in range(1, 11)}
        unknown_items = {i: 0 for i in range(1, 11)}
        per_session = {}
        for sess in sessions:
            status, reason = sess.compliance()
            if status == "valid":
                valid.append(sess)
            elif status == "invalid":
                invalid.append((sess.session, reason))
            else:
                unknown.append(sess.session)
            if status == "valid":
                fails, unknowns = judge_items(sess, chunk_tables[cfg], doc_texts)
                per_session[sess.session] = {
                    "passed": not any(fails.values()),
                    "fails": {str(k): v for k, v in fails.items()},
                    "item10_org": not fails[10],
                }
                if unknowns:
                    unknown_items.update({k: unknown_items[k] + 1 for k in unknowns})
                for i, failed in fails.items():
                    if failed:
                        item_fail_counts[i] += 1
            else:
                per_session[sess.session] = {"passed": None, "fails": None, "item10_org": None}
        passed = [s for s in per_session.values() if s["passed"]]
        refresh_values = []
        for sess in valid:
            refresh_values.extend(compute_refresh(sess))
        misses = sum(1 for v in refresh_values if math.isinf(v))
        finite = [v for v in refresh_values if not math.isinf(v)]
        stale = {"invalid": 0, "denominator": 0, "unconfirmed": 0, "events": 0, "events_with_invalid": 0}
        for sess in valid:
            st = compute_stale(sess)
            for k in stale:
                stale[k] += st[k]
        claims = claims_for_run(cfg, valid)
        claims_by_scenario[cfg] = claims["per_scenario"]
        for sc in sorted({s.scenario for s in sessions}):
            runs = [
                per_session[s.session]["passed"]
                for s in sessions
                if s.scenario == sc and per_session[s.session]["passed"] is not None
            ]
            pass_by_scenario[cfg].setdefault(sc, runs)
        law_tokens = []
        for sess in valid:
            cited = set()
            for sug in sess.exposed_suggestions():
                cited.update(sug.get("cited_articles") or [])
            law_tokens.append(sum(chunk_tables[cfg][a]["tokens"] for a in cited if a in chunk_tables[cfg]))
        prec = precision_metrics(cfg, valid, chunk_tables[cfg], doc_texts)
        result["runs"][cfg] = {
            "sessions": len(sessions),
            "valid": len(valid),
            "invalid": len(invalid),
            "invalid_sessions": [s for s, _ in invalid],
            "unknown_compliance": len(unknown),
            "passed": len(passed),
            "pass_rate": len(passed) / len(valid) if valid else None,
            "item_fail_counts": {str(k): v for k, v in item_fail_counts.items()},
            "unknown_items": {str(k): v for k, v in unknown_items.items()},
            "claims": claims["claims"],
            "unsupported": claims["unsupported"],
            "unsupported_rate": claims["unsupported"] / claims["claims"] if claims["claims"] else None,
            "excluded_fixed_suggestions": claims["excluded_fixed_suggestions"],
            "claim_judge_failures": claims["claim_judge_failures"],
            "refresh": {
                "events": len(refresh_values),
                "misses": misses,
                "p50_linear": pct_linear(finite + [math.inf] * misses, 0.5) if not misses else math.inf,
                "p95_linear": math.inf if misses else pct_linear(finite, 0.95),
                "p50_nearest": math.inf if misses else pct_nearest_rank(finite, 0.5),
                "p95_nearest": math.inf if misses else pct_nearest_rank(finite, 0.95),
            },
            "stale": stale,
            "law_tokens": {"sum": sum(law_tokens), "mean": sum(law_tokens) / len(law_tokens) if law_tokens else None},
            "precision": prec["precision"],
            "law_precision": prec["law_precision"],
            "per_session": per_session,
        }
        result["hashes"][cfg] = verify_hashes(cfg)
    # 판정 조건
    r0 = result["runs"]["r0"]
    r4 = result["runs"]["r4"]
    result["pass_delta"] = r4["pass_rate"] - r0["pass_rate"]
    if not scenario_ids:
        scenario_ids.extend(
            sc
            for sc in sorted(set(pass_by_scenario["r0"]) | set(pass_by_scenario["r4"]))
            if pass_by_scenario["r0"].get(sc) or pass_by_scenario["r4"].get(sc)
        )
    result["bootstrap"] = bootstrap(pass_by_scenario, claims_by_scenario, scenario_ids)
    ci = result["bootstrap"]
    result["conditions"] = {
        "pass_noninferior": result["pass_delta"] >= -0.05,
        "pass_improved": result["pass_delta"] >= 0.10 and ci["pass_delta"][0] > 0,
        "unsupported_not_higher": r4["unsupported_rate"] <= r0["unsupported_rate"],
        "refresh_p95_le_500": (r4["refresh"]["p95_linear"] if not math.isinf(r4["refresh"]["p95_linear"]) else math.inf) <= 500,
    }
    # 보고기 값 대조
    compare = []

    def add(label, rep, mine, kind="point"):
        if rep is None or mine is None:
            match = rep == mine
        elif kind == "point":
            match = abs(float(rep) - float(mine)) < 1e-9
        else:
            match = abs(float(rep) - float(mine)) <= 0.01
        compare.append({"metric": label, "reporter": rep, "recomputed": mine, "match": match})

    for cfg in ("r0", "r4"):
        rep = report[cfg]
        mine = result["runs"][cfg]
        add(f"{cfg}.valid", rep["valid"], mine["valid"])
        add(f"{cfg}.passed", rep["passed"], mine["passed"])
        add(f"{cfg}.pass_rate", rep["pass_rate"], mine["pass_rate"])
        add(f"{cfg}.invalid", rep["invalid"], mine["invalid"])
        add(f"{cfg}.unknown_compliance", rep["unknown_compliance"], mine["unknown_compliance"])
        unknown_pass = len(unknown) + sum(
            1
            for s, v in mine["per_session"].items()
            if v["fails"] is not None and any(fv is None for fv in v["fails"].values())
        )
        add(f"{cfg}.unknown_pass", rep["unknown_pass"], unknown_pass)
        for i in range(1, 11):
            add(f"{cfg}.failures.{i}", rep["failures"][str(i)], mine["item_fail_counts"][str(i)])
            add(f"{cfg}.unknown_items.{i}", rep["unknown_items"][str(i)], mine["unknown_items"][str(i)])
        add(f"{cfg}.claims", rep["claims"], mine["claims"])
        add(f"{cfg}.unsupported", rep["unsupported"], mine["unsupported"])
        add(f"{cfg}.unsupported_rate", rep["unsupported_claim_rate"], mine["unsupported_rate"])
        add(f"{cfg}.excluded_fixed_suggestions", rep["excluded_fixed_suggestions"], mine["excluded_fixed_suggestions"])
        add(f"{cfg}.claim_judge_failures", rep["claim_judge_failures"], mine["claim_judge_failures"])
        add(f"{cfg}.refresh.events", rep["refresh_ms"]["events"], mine["refresh"]["events"])
        add(f"{cfg}.refresh.misses", rep["refresh_ms"]["misses"], mine["refresh"]["misses"])
        add(f"{cfg}.refresh.p50", rep["refresh_ms"]["p50"], mine["refresh"]["p50_linear"])
        add(f"{cfg}.refresh.p95", rep["refresh_ms"]["p95"], mine["refresh"]["p95_linear"])
        add(f"{cfg}.stale.chat.invalid", rep["stale"]["by_channel"]["chat"]["invalid"], mine["stale"]["invalid"])
        add(f"{cfg}.stale.chat.denominator", rep["stale"]["by_channel"]["chat"]["denominator"], mine["stale"]["denominator"])
        add(f"{cfg}.stale.chat.unconfirmed", rep["stale"]["by_channel"]["chat"]["unconfirmed"], mine["stale"]["unconfirmed"])
        add(f"{cfg}.stale.events_total", rep["stale"]["events_total"], mine["stale"]["events"])
        add(f"{cfg}.stale.events_with_invalid", rep["stale"]["events_with_invalid"], mine["stale"]["events_with_invalid"])
        add(f"{cfg}.law_tokens.sum", rep["law_tokens"]["sum"], mine["law_tokens"]["sum"])
        add(f"{cfg}.law_tokens.mean", rep["law_tokens"]["mean"], mine["law_tokens"]["mean"])
        compare.append({"metric": f"{cfg}.precision", "reporter": rep["precision"], "recomputed": mine["precision"], "match": rep["precision"] == mine["precision"], "kind": "report_only"})
        compare.append({
            "metric": f"{cfg}.law_precision",
            "reporter": rep["law_precision"],
            "recomputed": mine["law_precision"],
            "match": mine["law_precision"] is not None and abs(rep["law_precision"] - mine["law_precision"]) < 1e-3,
            "kind": "report_only_near",
            "abs_diff": abs(rep["law_precision"] - mine["law_precision"]),
        })
    add("pass_delta", report["pass_delta"], result["pass_delta"])
    for key in ("pass_r0", "pass_r4", "pass_delta", "unsupported_r0", "unsupported_r4", "unsupported_delta"):
        lo = report["bootstrap"]["ci95"][key]
        mine = result["bootstrap"][key]
        add(f"bootstrap.ci95.{key}.low", lo[0], mine[0], kind="ci")
        add(f"bootstrap.ci95.{key}.high", lo[1], mine[1], kind="ci")
    for key in ("pass_noninferior", "pass_improved", "unsupported_not_higher", "refresh_p95_le_500"):
        add(f"condition.{key}", report["conditions"][key], result["conditions"][key])
    result["compare"] = compare
    result["compare_summary"] = {
        "total": len(compare),
        "matched": sum(1 for c in compare if c["match"]),
        "mismatched": [c["metric"] for c in compare if not c["match"]],
    }
    out_json = os.path.join(HERE, "T8-recompute.json")
    slim = dict(result)
    slim["runs"] = {
        cfg: {k: v for k, v in result["runs"][cfg].items() if k != "per_session"}
        | {"per_session_pass": {s: v["passed"] for s, v in result["runs"][cfg]["per_session"].items()}}
        | {"per_session_fails": {s: v["fails"] for s, v in result["runs"][cfg]["per_session"].items()}}
        for cfg in ("r0", "r4")
    }
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(slim, fh, ensure_ascii=False, indent=1)
    print(json.dumps(result["compare_summary"], ensure_ascii=False))
    for row in compare:
        if not row["match"]:
            print("MISMATCH", row)


if __name__ == "__main__":
    main()
