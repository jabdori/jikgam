"""평가 묶음의 리뷰 지적을 합성 자료로 재현한다. 실제 test·check는 읽지 않는다."""

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from jikgam.decision.types import DecisionResult
from jikgam.ingest.corpus import Doc, Line
from jikgam.ingest.window import CallLog, Config, Judge
from jikgam.v2 import answer, boundary_runs, embed, evaluate, freeze, report, retry, stage


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def row(qid, bad=0, good=0, kind="single", rep=0):
    return {"id": qid, "type": kind, "group": "법령", "rep": rep,
            "claims": [{"supported": False, "correctness": "unknown"}] * bad
                      + [{"supported": True, "correctness": "match"}] * good}


def test_claim_ratio_pools_single_none_and_repetition_claim_counts():
    a = [row("a", bad=1), row("b", good=9)]
    b = [row("a", good=1), row("b", bad=2, good=7)]
    assert answer.answer_metrics(a)["unsupported_claim_rate"] == 0.1
    assert answer.answer_metrics(b)["unsupported_claim_rate"] == 0.2
    assert report.answer_comparison(b, a, "unsupported_claim_rate")["diff"] == 0.1
    repeated = [row("a", bad=1), row("a", good=9, rep=1)]
    assert answer.answer_metrics(repeated)["unsupported_claim_rate"] == 0.1


def test_none_hallucination_fails_protection_without_changing_single_accuracy():
    a = [row("a", good=1), row("n", kind="none")]
    b = [row("a", good=1), row("n", bad=1, kind="none")]
    comp = report.answer_comparison(b, a, "unsupported_claim_rate")
    assert comp["diff"] == 0.5
    conditions = report.judge_conditions({}, {}, {}, {}, {"R0": a, "R4": b})["8.2"]
    quality = {c["condition"]: c for c in conditions[-2:]}
    assert quality["R4 − R0 근거 없는 주장 비율"]["result"] == "불만족"
    assert quality["R4 − R0 정답률"]["value"] == 0
    assert answer.answer_metrics([row("a"), row("n", bad=1, kind="none"), row("m", good=9, kind="multi")])["hold_rate"] == 1


@pytest.mark.parametrize("supported,correctness,held", [(1, 0, [False, True]), (0, 1, [True, False]), (0.8, 0.8, [False, False])])
def test_human_agreement_gates_each_condition_independently(supported, correctness, held):
    conditions = {"8.2": [{"condition": c, "result": "만족"} for c in ("R4 − R0 근거 없는 주장 비율", "R4 − R0 정답률")]}
    report.apply_claim_check(conditions, {"supported_agreement": supported, "correctness_agreement": correctness, "both_agreement": 0})
    assert [c["result"] == "판정 보류" for c in conditions["8.2"]] == held


def test_stage_without_marker_reruns_and_hashes_all_artifacts(tmp_path):
    put(tmp_path / "config/v2/freeze-0025.json", {})
    (tmp_path / "result").write_text("partial", encoding="utf-8")
    code = "from pathlib import Path; p=Path('count'); n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n)); Path('result').write_text(str(n))"
    command = [sys.executable, "-c", code]
    run = lambda: stage.run_stage("fake", "result", command, tmp_path, head="fixed", expected={"q": "single"})
    assert run() is True
    assert (tmp_path / "count").read_text() == "1"
    assert run() is False
    assert stage.run_stage("fake", "result", command, tmp_path, head="fixed", expected={"q": "single"}, always=True) is True
    (tmp_path / "result").write_text("changed", encoding="utf-8")
    assert run() is True
    assert (tmp_path / "count").read_text() == "3"
    marker = json.loads((tmp_path / "outputs/logs/v2-test/done/fake.json").read_text())
    assert marker["command"] == command and marker["HEAD"] == "fixed" and marker["expected_questions"] == 1
    assert marker["artifacts"][0]["sha256"] == hashlib.sha256(b"3").hexdigest()
    put(tmp_path / "config/v2/freeze-0025.json", {"changed": 1})
    assert run() is True


def test_failed_stage_has_no_completion_marker(tmp_path):
    put(tmp_path / "config/v2/freeze-0025.json", {})
    import subprocess
    with pytest.raises(subprocess.CalledProcessError):
        stage.run_stage("bad", "result", [sys.executable, "-c", "raise SystemExit(1)"], tmp_path, head="fixed", expected={})
    assert not (tmp_path / "outputs/logs/v2-test/done/bad.json").exists()


def test_bad_marker_or_changed_upstream_input_requires_rerun(tmp_path, monkeypatch):
    put(tmp_path / "config/v2/freeze-0025.json", {})
    source = tmp_path / "upstream"
    source.write_text("original", encoding="utf-8")
    monkeypatch.setattr(stage, "source_paths", lambda root, command: [source])
    command = [sys.executable, "-c", "from pathlib import Path; Path('result').write_text('done')"]
    run = lambda: stage.run_stage("source", "result", command, tmp_path, head="fixed", expected={})
    assert run() is True and run() is False
    source.write_text("changed", encoding="utf-8")
    assert run() is True
    (tmp_path / "outputs/logs/v2-test/done/source.json").write_text("{", encoding="utf-8")
    assert run() is True


def test_stage_rejects_partial_trace_and_wrong_answer_repetition(tmp_path):
    result, trace = tmp_path / "result.json", tmp_path / "trace.jsonl"
    put(result, {"per_question@2000": {"single": {"a": [True], "b": [False]}}})
    put(trace, {"id": "a"})
    with pytest.raises(ValueError):
        stage.validate([result, trace], ["python", "-m", "jikgam.v2.run"], {"a": "single", "b": "single"})
    put(result, {"questions": 1, "reps": 3})
    put(trace, {"id": "a", "rep": 0})
    with pytest.raises(ValueError):
        stage.validate([result, trace], ["python", "-m", "jikgam.v2.answer", "--reps", "1"], {"a": "single"})


def test_prepare_has_no_gemini_calls_and_runtime_cache_does_not_affect_freeze(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "CONFIGS", {"a": "chunks--full--snow+rrf", "b": "chunks--full--gemini+rrf"})
    monkeypatch.setattr(embed, "CACHE", tmp_path / "data/v2/local/emb")
    monkeypatch.setattr(embed, "corpus_inputs", lambda *args: (["id"], ["합성 원문"], ["합성 제목"]))
    built = []

    def build(key, ids, texts, titles, tag):
        assert key != "gemini"
        built.append(key)
        p = embed.cache_path(key, texts, tag)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"vector")

    monkeypatch.setattr(embed, "EmbeddingIndex", build)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(empty_cache=lambda: None)))
    out = freeze.prepare()
    assert built == ["snow"] and len(out["missing_gemini"]) == 1
    p = Path(out["missing_gemini"][0])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"existing-gemini")
    assert freeze.prepare()["missing_gemini"] == [] and built == ["snow"]
    monkeypatch.setattr(freeze, "input_paths", lambda root: sorted(p.relative_to(root).as_posix() for p in (root / "data/v2/local/emb").rglob("*") if p.is_file()))
    freeze.write_manifest(tmp_path)
    command = [sys.executable, "-c", "from pathlib import Path; Path('result').write_text('done')"]
    assert stage.run_stage("cache", "result", command, tmp_path, head="fixed", expected={})
    put(tmp_path / "data/v2/local/query-emb/derived.json", {"derived": 1})
    assert freeze.verify_manifest(tmp_path) == []
    assert stage.run_stage("cache", "result", command, tmp_path, head="fixed", expected={}) is False


def test_frozen_index_does_not_create_missing_corpus_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(embed, "CACHE", tmp_path)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace())
    with pytest.raises(FileNotFoundError, match="freeze prepare"):
        embed.EmbeddingIndex("snow", ["a"], ["합성"], ["제목"], "tag", allow_build=False)
    assert list(tmp_path.iterdir()) == []


class Decisions:
    def __init__(self, errors):
        self.errors, self.calls = errors, 0

    async def decide(self, state, questions):
        self.calls += 1
        err = self.calls <= self.errors
        return DecisionResult("fake", "fake", {} if err else {q: {"type": "noul", "noul": 0.9} for q in questions},
                              input_tokens=3, cost_usd=0.01, error="HTTP 503" if err else None)


@pytest.mark.parametrize("errors,attempts", [(2, 3), (10, 4)])
def test_decision_retry_delays_attempts_and_all_attempt_costs(errors, attempts, monkeypatch):
    delays = []
    async def sleep(delay):
        delays.append(delay)
    monkeypatch.setattr(retry.asyncio, "sleep", sleep)
    client = Decisions(errors)
    out = asyncio.run(retry.decide(client, {}, {"q": {"type": "noul"}}))
    assert delays == [2, 4, 8][:attempts - 1]
    assert out.meta["attempts"] == attempts and out.input_tokens == attempts * 3
    assert out.cost_usd == pytest.approx(attempts * 0.01)
    assert bool(out.error) == (errors >= 4)


def test_window_retries_before_falling_back(monkeypatch):
    async def sleep(delay):
        pass
    monkeypatch.setattr(retry.asyncio, "sleep", sleep)
    doc = Doc("d", "합성", "법률", True, [Line(1, "본문", 1, "text", "합성", 0)])
    async def run():
        judge = object.__new__(Judge)
        judge.cfg, judge.sem, judge.decider = Config("jev", 4000, 800, "raw"), asyncio.Semaphore(4), Decisions(2)
        probs, log = await judge.judge_window(doc, "", 1, 1, 1, 0)
        assert probs == {1: 0.9} and log.requests == 3 and log.attempts == 3 and log.error is None
        assert log.successful_requests == 1
        judge.decider = Decisions(10)
        probs, log = await judge.judge_window(doc, "", 1, 1, 1, 0)
        assert probs == {1: 0.0} and log.requests == 4 and log.error
        assert log.successful_requests == 0
    asyncio.run(run())


@pytest.mark.parametrize("errors,attempts", [(1, 2), (10, 4)])
def test_hybrid_retries_before_reranking_or_retaining_fusion_order(monkeypatch, errors, attempts):
    from jikgam.v2.hybrid import Hybrid, HybridConfig, Trace
    async def sleep(delay):
        pass
    monkeypatch.setattr(retry.asyncio, "sleep", sleep)
    hy = object.__new__(Hybrid)
    hy.cfg, hy.jev = HybridConfig(rerank=2), Decisions(errors)
    hy.cs = SimpleNamespace(docs={"d": SimpleNamespace(title="합성")},
                            by_id={"a": SimpleNamespace(doc="d"), "b": SimpleNamespace(doc="d")}, text=lambda c: "합성")
    ranked, tr = [("a", 2.0), ("b", 1.0)], Trace()
    out = asyncio.run(hy.rerank("합성 질문", ranked, tr))
    assert tr.rerank_attempts == attempts
    assert tr.rerank_cost_usd == pytest.approx(attempts * 0.01)
    assert bool(tr.rerank_error) == (errors >= 4)
    assert out == ranked if errors >= 4 else out == [("a", 0.9), ("b", 0.9)]


def test_gemini_embedding_retries_connections_and_http_errors(monkeypatch):
    delays, calls = [], []
    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("synthetic")
        return httpx.Response(503) if len(calls) == 2 else httpx.Response(200, json={"embeddings": [{"values": [1.0]}]})
    encoder = embed.Encoder("gemini")
    encoder.http = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(embed.time, "sleep", delays.append)
    assert encoder._gemini_batch([{"content": {"parts": [{"text": "합성"}]}}]) == [[1.0]]
    assert delays == [2, 4] and encoder.requests == 3 and encoder.retries == 2
    encoder.http.close()


def test_gemini_embedding_all_failed_attempts_are_recorded(monkeypatch):
    encoder = embed.Encoder("gemini")
    encoder.http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    monkeypatch.setattr(embed.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="HTTP 503"):
        encoder._gemini_batch([{"content": {"parts": [{"text": "합성"}]}}])
    assert encoder.requests == 4 and encoder.retries == 3
    encoder.http.close()


@pytest.mark.parametrize("method,prob", [("c-jev", 0.64996), ("c-qwen", 0.54996)])
def test_repeat_rounds_probabilities_and_applies_cmax(tmp_path, monkeypatch, method, prob):
    put(tmp_path / "data/v2/boundary/segments.json", {"segments": [{"id": "s", "doc": "d", "start": 1, "end": 5}]})
    doc = Doc("d", "합성", "법률", True, [Line(i, "본문", 1, "text", "합성", i * 3) for i in range(1, 6)])
    class FakeJudge:
        def __init__(self, cfg):
            pass
        async def judge_window(self, *args):
            return {2: prob}, CallLog("d", 0, "judge")
        async def aclose(self):
            pass
    monkeypatch.setattr(boundary_runs, "ROOT", tmp_path)
    monkeypatch.setattr(boundary_runs, "require_qwen", lambda: None)
    monkeypatch.setattr(boundary_runs, "load_docs", lambda only: {"d": doc})
    monkeypatch.setattr(boundary_runs, "chunk_starts", lambda name: {"d": [1, 2, 4]})
    monkeypatch.setattr(boundary_runs.chunking, "line_tokens", lambda d: [400] * 5)
    monkeypatch.setattr(boundary_runs, "Judge", FakeJudge)
    out = asyncio.run(boundary_runs.run(method))
    assert out["judgment_runs"] == [{"s": [2]}, {"s": [2]}]
    assert out["runs"] == [{"s": [2, 4]}, {"s": [2, 4]}]
    assert out["agreement"]["final"]["run1_vs_original"]["f1"] == 1


def test_all_failed_repeat_cannot_be_saved(tmp_path, monkeypatch):
    put(tmp_path / "data/v2/boundary/segments.json", {"segments": [{"id": "s", "doc": "d", "start": 1, "end": 2}]})
    doc = Doc("d", "합성", "법률", True, [Line(i, "본문", 1, "text", "합성", i) for i in (1, 2)])
    class FailedJudge:
        def __init__(self, cfg):
            pass
        async def llm_window(self, *args):
            return [], "", CallLog("d", 0, "judge", error="failed")
        async def aclose(self):
            pass
    monkeypatch.setattr(boundary_runs, "ROOT", tmp_path)
    monkeypatch.setattr(boundary_runs, "load_docs", lambda only: {"d": doc})
    monkeypatch.setattr(boundary_runs, "chunk_starts", lambda name: {"d": [1]})
    monkeypatch.setattr(boundary_runs.chunking, "line_tokens", lambda d: [1, 1])
    monkeypatch.setattr(boundary_runs, "Judge", FailedJudge)
    with pytest.raises(RuntimeError, match="모두 실패"):
        asyncio.run(boundary_runs.run("b"))
    assert not (tmp_path / "results").exists()


@pytest.mark.parametrize("failure", ["authentication", "hash", "inference", None])
def test_qwen_preflight_rejects_authentication_hash_and_inference_failures(tmp_path, monkeypatch, failure):
    from jikgam.decision import registry
    config = tmp_path / "config/decision_models.toml"
    config.parent.mkdir()
    config.write_text('["q35-2b-ko-direct"]\nurl="http://fake/v1/systemone"\napi_key_env="FAKE_KEY"\nmodel="fake"\nweights_sha256_16="expected"\n')
    monkeypatch.setattr(registry, "secrets", lambda: {"FAKE_KEY": "synthetic-key"})
    def handler(request):
        assert request.headers["Authorization"] == "Bearer synthetic-key"
        if failure == "authentication" or (failure == "inference" and request.method == "POST"):
            return httpx.Response(401)
        if request.method == "GET":
            return httpx.Response(200, json={"weights_sha256_16": "wrong" if failure == "hash" else "expected"})
        return httpx.Response(200, json={"answers": {"ready": {"type": "noul", "noul": 1.0}}})
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    if failure is None:
        boundary_runs.require_qwen(tmp_path)
    else:
        with pytest.raises(SystemExit, match="인증"):
            boundary_runs.require_qwen(tmp_path)


def test_server_info_reuses_original_weights_hash_and_requires_authentication(tmp_path):
    from jikgam.train.serve_direct import authorize, model_info
    (tmp_path / "model.safetensors").write_bytes(b"backbone")
    (tmp_path / "joint_head.safetensors").write_bytes(b"head")
    assert model_info(tmp_path, "fake")["weights_sha256_16"] == hashlib.sha256(b"backbonehead").hexdigest()[:16]
    authorize("Bearer synthetic-key", "synthetic-key")
    with pytest.raises(PermissionError):
        authorize("Bearer wrong", "synthetic-key")
    with pytest.raises(PermissionError):
        authorize("Bearer ", "")


def test_word_overlap_excludes_none_and_uses_first_alternative_and_all_evidence_items():
    docs = {"a": SimpleNamespace(text="가나다"), "b": SimpleNamespace(text="다라")}
    ev = lambda d, a, b: {"doc": d, "start": a, "end": b}
    qs = [{"id": "q", "type": "single", "group": "법령", "question": "가나 다라", "evidence": [
        {"alternatives": [ev("a", 0, 3), ev("b", 0, 2)]}]},
        {"id": "m", "type": "multi", "group": "넘나듦", "question": "가나다라", "evidence": [
            {"alternatives": [ev("a", 0, 2)]}, {"alternatives": [ev("b", 0, 2)]}]},
        {"id": "n", "type": "none", "group": "법령", "question": "없는 글", "evidence": []}]
    out = evaluate.word_overlap(qs, docs)
    assert out["excluded_none"] == 1 and out["all"] == {"questions": 2, "overlap": 0.8333}
    assert out["by_type"]["single"]["overlap"] == 0.6667 and out["by_group"]["넘나듦"]["overlap"] == 1


def test_metadata_cost_is_reported_separately_and_adds_separate_local_cost(tmp_path):
    put(tmp_path / f"data/v2/meta/{report.B}.meta.json", {"cost_usd": 0.1})
    put(tmp_path / f"data/v2/local/meta/{report.B}.meta.json", {"cost_usd": 0.2})
    assert report.metadata_costs(tmp_path)[report.B]["cost_usd"] == 0.3


def test_run_adds_all_group_metrics_and_overlap_without_model_calls(tmp_path, monkeypatch):
    from jikgam.ingest.chunk import Chunk
    from jikgam.v2 import run
    doc = Doc("d", "합성", "법률", True, [Line(1, "본문", 1, "text", "가나다라", 0)])
    chunk = Chunk("a", "d", 1, 1, 0, 4, 4)
    qs = [{"id": "q", "type": "single", "group": "법령", "question": "가나다라", "evidence": [
        {"alternatives": [{"doc": "d", "start": 0, "end": 4}]}]}]
    monkeypatch.setattr(run, "load_docs", lambda: {"d": doc})
    monkeypatch.setattr(run.chunking, "load", lambda name: [chunk])
    monkeypatch.setattr(run, "load_questions", lambda *a, **kw: qs)
    monkeypatch.setattr(run, "rank_r0", lambda *a: [("a", 1.0)])
    monkeypatch.setattr(run, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(run, "RUNS", tmp_path / "runs")
    args = SimpleNamespace(split="val", frozen=None, chunks="fake", meta="none", r0=True, embed=None, fusion="rrf", related=False, rerank=0)
    out = asyncio.run(run.run(args))
    group = out["by_group"]["법령"]
    for key in ("items@1000", "items@2000", "items@4000", "complete@2000", "recall@5", "recall@10", "ndcg@10"):
        assert group[key] == 1
    assert "latency_ms" in group and out["word_overlap"]["all"]["overlap"] == 1
    put(tmp_path / "results/v2/val/R0-article256.json", out)
    copied = report.build_report(tmp_path, "val")
    assert copied["retrieval"]["R0"]["by_group"]["법령"] == group
    put(tmp_path / "results/v2/check/R0-article256.json", out)
    checked = report.build_report(tmp_path, "test")
    assert checked["check"]["R0"]["by_group"]["법령"] == group
    assert checked["word_overlap"]["check"] == out["word_overlap"]


@pytest.mark.parametrize("errors,attempts", [(2, 3), (10, 4)])
def test_v2_json_generation_and_judgment_retry_all_failures_with_fixed_delays(monkeypatch, errors, attempts):
    from jikgam.sim.gemini import JsonReply
    delays = []
    async def sleep(delay):
        delays.append(delay)
    monkeypatch.setattr(retry.asyncio, "sleep", sleep)
    class FakeJson:
        calls = 0
        async def ask(self, *args, **kwargs):
            self.calls += 1
            return JsonReply(None if self.calls <= errors else {"answer": "합성"}, "fake",
                             input_tokens=2, output_tokens=1, cost_usd=0.01,
                             error="WriteError" if self.calls <= errors else None)
    result = asyncio.run(answer.generate(FakeJson(), "합성 질문", "합성 근거", 1))
    assert result["usage"]["attempts"] == attempts
    assert result["usage"]["input_tokens"] == attempts * 2
    assert result["usage"]["cost_usd"] == pytest.approx(attempts * 0.01)
    assert delays == [2, 4, 8][:attempts - 1]


def test_apply_refreshes_the_durable_report(tmp_path):
    from jikgam.v2 import claim_check
    from tests.v2_claim_fixtures import bind_review_inputs
    chosen = [{"sample_id": "a", "supported": True, "correctness": "match"}]
    sample = {"split": "val", "items": chosen, "sample_digest": "synthetic"}
    result = {"split": "val", "sample_digest": "synthetic", "items": {
        "a": {"supported": True, "correctness": "unknown", "done": True}}}
    bind_review_inputs(tmp_path, sample, result)
    put(tmp_path / "data/v2/local/review/claims_val_sample.json", sample)
    put(tmp_path / "user.json", result)
    report.write_report(tmp_path, "val")
    claim_check.apply(tmp_path / "user.json", "val", tmp_path)
    refreshed = json.loads((tmp_path / "outputs/v2-smoke/report-val.json").read_text(encoding="utf-8"))
    quality = refreshed["conditions"]["8.2"][-2:]
    assert quality[0]["human_check"] == "사람 확인 완료" and quality[1]["result"] == "판정 보류"
