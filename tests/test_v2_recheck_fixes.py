"""재확인 N1~N4의 합성 입력을 실제 계산 경로로 검증한다."""

import asyncio
import copy
import json
import subprocess
from types import SimpleNamespace

import pytest

from jikgam.decision.types import DecisionResult
from jikgam.ingest.corpus import Doc, Line
from jikgam.ingest.window import CallLog
from jikgam.v2 import boundary_runs, claim_check, report, retry, run, stage, stats
from jikgam.v2.hybrid import Hybrid, HybridConfig, Trace
from tests.v2_claim_fixtures import bind_review_inputs


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize("all_failed", [False, True])
def test_rerank_preserves_success_call_latency_and_records_total_time(monkeypatch, all_failed):
    clock = [0.0]
    monkeypatch.setattr(retry.time, "perf_counter", lambda: clock[0])
    async def sleep(delay):
        clock[0] += delay
    monkeypatch.setattr(retry.asyncio, "sleep", sleep)
    class Client:
        calls = 0
        async def decide(self, state, questions):
            self.calls += 1
            clock[0] += 0.1
            return DecisionResult("fake", "fake", {"c1": {"type": "noul", "noul": 1.0}},
                                  latency_ms=100.0, error="503" if all_failed or self.calls == 1 else None)
    hy = object.__new__(Hybrid)
    hy.cfg, hy.jev = HybridConfig(rerank=1), Client()
    hy.cs = SimpleNamespace(by_id={"a": SimpleNamespace(doc="d")}, docs={"d": SimpleNamespace(title="합성")}, text=lambda c: "합성")
    trace = Trace()
    asyncio.run(hy.rerank("합성 질문", [("a", 1)], trace))
    assert trace.rerank_ms == (None if all_failed else 100.0)
    assert trace.rerank_total_ms == pytest.approx(14400 if all_failed else 2200)


def test_latency_p95_uses_successful_single_calls_and_reports_excluded_failures():
    direct = {"retrieval_ms": 1, "rerank_ms": 100, "rerank_total_ms": 100, "rerank_error": None}
    repeated = {**direct, "rerank_total_ms": 2200}
    failed = {**direct, "rerank_ms": None, "rerank_total_ms": 14400, "rerank_error": "failed"}
    metrics = run.latency_summary([direct] * 164 + [repeated] * 10 + [failed] * 3)
    assert metrics["rerank_p50"] == metrics["rerank_p95"] == 100
    assert metrics["rerank_total_p95"] == 2200
    assert metrics["rerank_failed_questions"] == 3 and metrics["rerank_successful_questions"] == 174
    conditions = report.judge_conditions({"R4": {"latency_ms": metrics}}, {}, {}, {}, {})["8.2"]
    assert next(c for c in conditions if c["condition"] == "R4 리랭킹 p95(ms)")["result"] == "만족"
    only_failed = run.latency_summary([failed] * 3)
    assert only_failed["rerank_p95"] is None and only_failed["rerank_total_p95"] == 14400


def review_fixture(root):
    sample = {"split": "val", "items": [{"sample_id": "claim-01", "supported": True, "correctness": "match"}]}
    result = {"split": "val", "items": {"claim-01": {"supported": True, "correctness": "match", "done": True}}}
    bind_review_inputs(root, sample, result)
    put(root / "data/v2/local/review/claims_val_sample.json", sample)
    put(root / "user.json", result)
    return sample, result


@pytest.mark.parametrize("changed", ["answers", "runs"])
def test_changed_answer_or_trace_invalidates_review_and_old_download(tmp_path, changed):
    sample, result = review_fixture(tmp_path)
    summary = claim_check.apply(tmp_path / "user.json", "val", tmp_path)
    assert summary["valid"] and summary["sample_digest"] == sample["sample_digest"]
    assert claim_check.validated_review(tmp_path, "val") is not None
    path = tmp_path / f"data/v2/local/{changed}/val/{claim_check.RUNS[0]}.jsonl"
    row = json.loads(path.read_text(encoding="utf-8"))
    if changed == "answers":
        row["claims"][0]["claim"] = "같은 위치의 다른 합성 주장"
    else:
        row["budget4000"] = ["different-evidence"]
    put(path, row)
    assert claim_check.validated_review(tmp_path, "val") is None
    assert report.build_report(tmp_path, "val")["claim_check"]["status"] == "사람 확인 전"
    with pytest.raises(ValueError, match="표본 입력"):
        claim_check.apply(tmp_path / "user.json", "val", tmp_path)
    updated = copy.deepcopy(sample)
    updated["source_hashes"] = claim_check.input_hashes(tmp_path, "val")
    if changed == "answers":
        updated["items"][0]["claim_sha256"] = claim_check.text_hash(row["claims"][0]["claim"])
    updated["sample_digest"] = claim_check.sample_digest(updated["items"], updated["source_hashes"])
    assert updated["sample_digest"] != sample["sample_digest"]
    with pytest.raises(ValueError, match="표본이 다릅니다"):
        claim_check.agreement(updated, result)
    item = {"sample_id": "claim-01", "question": "합성", "evidence": "합성", "gold": "합성", "claim": "합성"}
    old_page = claim_check.render_page([item], "val", sample["sample_digest"])
    new_page = claim_check.render_page([item], "val", updated["sample_digest"])
    assert sample["sample_digest"] in old_page and sample["sample_digest"] not in new_page


def test_claim_text_changes_sample_identity_even_at_the_same_position():
    sources = {claim_check.RUNS[0]: [{"id": "q", "rep": 0, "claims": [
        {"claim": "첫 합성 주장", "supported": True, "correctness": "match"}]}]}
    before, _ = claim_check.pick_claims(sources)
    sources[claim_check.RUNS[0]][0]["claims"][0]["claim"] = "다른 합성 주장"
    after, _ = claim_check.pick_claims(sources)
    assert claim_check.sample_digest(before) != claim_check.sample_digest(after)
    assert "첫 합성 주장" not in json.dumps(before, ensure_ascii=False)


def test_answer_stage_marks_existing_review_invalid_without_deleting_it(tmp_path, monkeypatch):
    sample, _ = review_fixture(tmp_path)
    summary = claim_check.apply(tmp_path / "user.json", "val", tmp_path)
    put(tmp_path / f"results/v2/val/{claim_check.RUNS[0]}.json", {})
    put(tmp_path / "config/v2/freeze-0025.json", {})
    command = ["uv", "run", "python", "-m", "jikgam.v2.answer", "--run", claim_check.RUNS[0], "--split", "val"]
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(stage.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        stage.run_stage("answers", f"results/v2/val/answers-{claim_check.RUNS[0]}.json", command,
                        tmp_path, head="synthetic", expected={"q0": "single"})
    preserved = json.loads(claim_check.review_path(tmp_path, "val").read_text(encoding="utf-8"))
    assert preserved["valid"] is False and preserved["supported_agreement"] == summary["supported_agreement"]
    assert preserved["sample_digest"] == sample["sample_digest"]
    assert report.build_report(tmp_path, "val")["claim_check"]["status"] == "사람 확인 전"


def test_legacy_review_without_input_identity_is_pending(tmp_path):
    put(claim_check.review_path(tmp_path, "val"), {"supported_agreement": 1, "correctness_agreement": 1})
    assert report.build_report(tmp_path, "val")["claim_check"]["status"] == "사람 확인 전"


def test_c_repeat_uses_selected_theta_state_but_original_theta_context(tmp_path, monkeypatch):
    put(tmp_path / "data/v2/boundary/segments.json", {"segments": [{"id": "s", "doc": "d", "start": 16, "end": 32}]})
    doc = Doc("d", "합성", "법률", True, [Line(i, "본문", 1, "text", "합성", i * 3) for i in range(1, 33)])
    seen = []
    class FakeJudge:
        def __init__(self, cfg):
            pass
        async def judge_window(self, doc, ctx, s, jend, wend, k):
            seen.append((ctx, s, jend, wend, k))
            return {n: 0.6 for n in range(s, jend + 1)}, CallLog("d", k, "judge")
        async def aclose(self):
            pass
    original = [1, 4, 8, 12, 16, 20, 24, 28, 32]
    chosen = [1, 5, 9, 13, 17, 21, 25, 29]
    monkeypatch.setattr(boundary_runs, "ROOT", tmp_path)
    monkeypatch.setattr(boundary_runs, "load_docs", lambda only: {"d": doc})
    monkeypatch.setattr(boundary_runs, "chunk_starts", lambda name: {"d": chosen if name.endswith("-t0.65") else original})
    monkeypatch.setattr(boundary_runs.chunking, "line_tokens", lambda doc: [200] * 32)
    monkeypatch.setattr(boundary_runs, "Judge", FakeJudge)
    out = asyncio.run(boundary_runs.run("c-jev"))
    assert seen[0][1:] == (17, 32, 32, 1) and "16줄에서 시작" in seen[0][0]
    assert out["runs"] == [{"s": [17, 21, 25, 29]}, {"s": [17, 21, 25, 29]}]
    assert out["agreement"]["final"]["run1_vs_original"]["exact"]["f1"] == 1


def test_unrounded_claim_ratio_delta_cannot_pass_as_a_tie():
    def rows(total):
        return [{"id": "q", "type": "single", "claims": [{"supported": False, "correctness": "unknown"}]
                 + [{"supported": True, "correctness": "match"}] * (total - 1)}]
    a, b = rows(200), rows(199)
    compared = report.answer_comparison(b, a, "unsupported_claim_rate")
    assert compared["diff"] == pytest.approx(1 / 199 - 1 / 200)
    assert 0 < compared["diff"] < 0.00005
    condition = report.judge_conditions({}, {}, {}, {}, {"R0": a, "R4": b})["8.2"][-2]
    assert condition["result"] == "불만족" and condition["value"] > 0


def test_raw_recall_ci_and_thresholds_keep_tiny_positive_values():
    a, b = {"q": [True] + [False] * 49999}, {"q": [False] * 50000}
    compared = stats.paired_recall_diff(a, b, n=40)
    assert compared["diff"] == 0.00002 and compared["ci95"] == [0.00002, 0.00002]
    assert report.condition("구간 아래 끝", compared["ci95"][0], ">", 0, compared["ci95"], True)["result"] == "만족"
    assert report.condition("미세한 악화", 5e-13, "<=", 0)["result"] == "불만족"
    assert run.pct([250.000001], 0.95) > 250
    assert report.condition("지연", run.pct([250.000001], 0.95), "<=", 250)["result"] == "불만족"
