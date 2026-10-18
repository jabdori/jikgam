"""엔드투엔드 판정·집계·원문 검사·미공개 잠금의 회귀 검사. 모두 합성 입력이다."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from jikgam.sim import metrics, runner
from jikgam.v2 import e2e_claims as claims, e2e_report as report, freeze, terms_scan
from jikgam.v2 import e2e_logs, e2e_run


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def events(full=True):
    values = [{"type": "utterance.end", "text": "질문"},
            {"type": "suggestion.created", "suggestion_id": "s1", "text": "주장", "cited_articles": ["A"]},
            {"type": "suggestion.exposed", "suggestion_id": "s1"},
            {"type": "suggestion.exposed", "suggestion_id": "s1"},
            {"type": "suggestion.created", "suggestion_id": "s2", "text": "미노출", "cited_articles": []}]
    if full:
        values[1].update(basis_version=1, generation_request_id="g1")
        values.insert(1, {"type": "generation.request", "version": 1, "request_id": "g1",
                          "input": {"articles": [{"id": "A", "text": "근거"}, {"id": "B", "text": "미인용 근거"}],
                                    "quotes": {"환불": 100}, "system_facts": {"계정": "활성"},
                                    "plan": {"action": "answer"}, "conversation": [{"text": "질문"}]}})
    return values


def row(scenario="가", valid=True, passed=True, total=10, unsupported=1, run=1):
    return {"scenario": scenario, "run": run, "valid": valid, "pass": passed, "claims": total,
            "unsupported": unsupported, "missing": 0, "items": {i: passed for i in range(1, 11)},
            "aux": {"precision": 0.5, "law": {"law_precision": 0.25, "law_tokens": 50}},
            "metrics": metrics.session_metrics([])}


def test_claims_use_full_generation_input_and_only_exposed_distinct_suggestions():
    es = events()
    result = list(claims.exposed_inputs(es, {"A": {"text": "현재 근거"}}))
    assert len(result) == 1
    assert json.loads(result[0]["evidence"]) == es[1]["input"]
    assert "미인용 근거" in result[0]["evidence"] and "활성" in result[0]["evidence"]
    assert result[0]["question"] == "질문"
    assert "정답" not in result[0]
    assert "correctness" not in claims.SYSTEM and "correctness" not in str(claims.SCHEMA)


def test_claims_mark_previous_logs_without_full_input_instead_of_rebuilding_articles():
    result = next(claims.exposed_inputs(events(full=False), {"A": {"text": "근거"}}))
    assert result["input_missing"] and not result["evidence"]


def test_claims_match_request_id_even_when_a_later_request_has_the_same_version():
    es = events()
    es.insert(2, {"type": "generation.request", "version": 1, "request_id": "g2",
                  "input": {"articles": []}, "feedback": "다시 생성"})
    assert next(claims.exposed_inputs(es))["generation_input"] == es[1]["input"]


def test_claim_cache_is_bound_to_prompt_model_answer_and_evidence(tmp_path):
    source = next(claims.exposed_inputs(events(), {"A": {"text": "근거"}}))
    key = claims.fingerprint(source, "모델")
    assert key != claims.fingerprint({**source, "evidence": "다른 근거"}, "모델")
    assert key != claims.fingerprint(source, "다른 모델")
    assert claims.local_dir("개발", tmp_path) == tmp_path / "data/v2/local/e2e/개발"
    with pytest.raises(ValueError):
        claims.local_dir("../탈출", tmp_path)


def test_claim_counts_missing_or_error_cannot_become_zero_unsupported(tmp_path):
    es, articles = events(), {"A": {"text": "근거"}}
    source = next(claims.exposed_inputs(es, articles))
    path = tmp_path / f"{claims.fingerprint(source, '모델')}.json"
    assert report.claim_counts(es, articles, tmp_path, "모델") == (0, 0, 1, 0)
    put(path, {"claims": [], "error": "실패"})
    assert report.claim_counts(es, articles, tmp_path, "모델") == (0, 0, 1, 1)
    put(path, {"claims": [{"supported": True}, {"supported": False}], "error": None})
    assert report.claim_counts(es, articles, tmp_path, "모델") == (2, 1, 0, 0)


def test_pooled_claim_rate_and_invalid_run_exclusion():
    rows = [row(total=1, unsupported=1), row("나", total=9, unsupported=0),
            row("다", valid=False, total=100, unsupported=100)]
    assert report.rates(rows) == (1, 0.1)
    s = report.summarize(rows, {})
    assert s["valid"] == 2 and s["invalid"] == 1
    assert s["law_precision"] == 0.25 and s["law_tokens"] == {"mean": 50, "sum": 100}
    rows[0]["missing"] = 1
    assert report.rates(rows)[1] is None


def test_bootstrap_samples_scenario_clusters_with_all_repeats_and_same_draw():
    a = [row(run=i, passed=bool(i % 2)) for i in (1, 2, 3)]
    b = [row(run=i, passed=True, total=100, unsupported=20) for i in (1, 2, 3)]
    result = report.paired_bootstrap(a, b, reps=25)
    assert result["seed"] == 20261016
    assert result["ci95"]["pass_delta"] == [pytest.approx(1 / 3)] * 2
    assert result["ci95"]["unsupported_delta"] == [pytest.approx(0.1)] * 2
    assert report.paired_bootstrap(a, a, reps=25)["ci95"]["pass_delta"] == [0, 0]
    assert report.paired_bootstrap(a, b, reps=25) == result
    with pytest.raises(ValueError, match="반복"):
        report.paired_bootstrap(a, b[:1], reps=25)


def test_bootstrap_and_conditions_do_not_hide_missing_judgments():
    a, b = [row()], [row()]
    b[0]["missing"] = 1
    result = report.compare(a, b, [{}, {}])
    assert result["conditions"]["unsupported_not_higher"] is None
    assert result["bootstrap"]["ci95"]["unsupported_delta"] is None
    b[0]["valid"] = None
    assert report.compare(a, b, [{}, {}])["conditions"]["pass_noninferior"] is None


def test_refresh_and_stale_reuse_v1_definition_and_json_is_strict():
    # 집계 함수 자체에 v1 계산 결과를 주고 +∞ 보존을 검사한다.
    r = row()
    r["metrics"]["refresh"] = [{"latency_ms": None}, {"latency_ms": 200}]
    value = report.summarize([r], {})
    assert value["refresh_ms"] == metrics.latency_stats([None, 200])
    clean = report.serializable(value)
    assert clean["refresh_ms"]["p95"] == "∞"
    json.dumps(clean, allow_nan=False)


def test_terms_scan_matches_25_character_substrings_and_escaped_json(tmp_path):
    original = "가나다라마바사아자차카타파하거너더러머버서어저처커터퍼허"
    docs = {"약관": SimpleNamespace(lines=[SimpleNamespace(text=original), SimpleNamespace(text="짧음")])}
    needles = terms_scan.fragments(docs)
    assert len(needles) == len(original) - 24
    put(tmp_path / "결과.json", {"text": "접두" + original[2:27] + "접미"})
    result = terms_scan.scan([tmp_path], needles)
    assert result == [{"file": str(tmp_path / "결과.json"), "count": 1}]
    assert original not in str(result)
    (tmp_path / "결과.json").write_text(json.dumps({"text": original[2:27]}, ensure_ascii=True), encoding="utf-8")
    assert terms_scan.scan([tmp_path], needles)[0]["count"] == 1
    with pytest.raises(FileNotFoundError):
        terms_scan.scan([tmp_path / "없음"], needles)


def setup_frozen(root, monkeypatch):
    target = root / "data/scenarios/org_a_v2_holdout.jsonl"
    put(target, {"합성": True})
    (root / "docs/adr").mkdir(parents=True)
    (root / "docs/adr/0027-설정-고정.md").write_text("합성 고정 기록", encoding="utf-8")
    names = [target.relative_to(root).as_posix()]
    monkeypatch.setattr(freeze, "e2e_input_paths", lambda root: names)
    return target, names


def test_holdout_lock_requires_adr_and_manifest_before_opening_scenario(tmp_path, monkeypatch):
    target, paths = setup_frozen(tmp_path, monkeypatch)
    for adr in (None, "../../탈출", "0027"):
        with pytest.raises(SystemExit):
            freeze.require_e2e_frozen(target, adr, tmp_path)
    freeze.write_manifest(tmp_path, "0027", paths)
    freeze.require_e2e_frozen(target, "0027", tmp_path)
    put(target, {"합성": False})
    with pytest.raises(SystemExit, match="해시"):
        freeze.require_e2e_frozen(target, "0027", tmp_path)


def test_holdout_lock_rejects_incomplete_manifest_and_wrong_adr(tmp_path, monkeypatch):
    target, paths = setup_frozen(tmp_path, monkeypatch)
    manifest = freeze.write_manifest(tmp_path, "0027", paths=[])
    with pytest.raises(SystemExit, match="입력이 빠졌"):
        freeze.require_e2e_frozen(target, "0027", tmp_path)
    manifest["adr"] = "0025"
    put(tmp_path / "config/v2/freeze-0027.json", manifest)
    with pytest.raises(SystemExit, match="올바르지"):
        freeze.require_e2e_frozen(target, "0027", tmp_path)


def test_runner_lock_runs_before_any_org_or_model_loading(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "load_org", lambda _: pytest.fail("잠금 전에 조직을 읽었습니다"))
    args = SimpleNamespace(scenario_file="data/scenarios/org_a_v2_holdout.jsonl", org="org_a", frozen=None)
    with pytest.raises(SystemExit, match="--frozen"):
        asyncio.run(runner.run(args))
    freeze.require_e2e_frozen(tmp_path / "data/scenarios/org_a_v2.jsonl", None, tmp_path)


def test_freeze_inputs_exclude_qa_and_reference_files(tmp_path):
    put(tmp_path / "data/v2/extracted/summary.json", {"docs": {"d": {"output": "data/v2/local/d.jsonl"}}})
    paths = freeze.e2e_input_paths(tmp_path)
    assert "data/v2/local/d.jsonl" in paths
    assert "jikgam/sim/runner.py" not in paths  # 존재하는 런타임 파일만 수집한다.
    assert not any("/qa/" in p or "/check/" in p or "reference-" in p for p in paths)


def test_claim_runner_bounds_concurrency_reuses_success_and_accounts_failed_retry(tmp_path, monkeypatch):
    from jikgam.sim.gemini import JsonReply

    summary = {"run_id": "합성", "sessions": [{"session": "세션", "scenario": "개발"}]}
    put(tmp_path / "summary.json", summary)
    source = list(claims.exposed_inputs(events(), {"A": {"text": "근거"}}))[0]
    inputs = [{**source, "suggestion_id": str(i)} for i in range(3)]
    monkeypatch.setattr(claims, "article_map", lambda _: {})
    monkeypatch.setattr(claims, "read_log", lambda _: [])
    monkeypatch.setattr(claims, "exposed_inputs", lambda *_: iter(inputs))
    dest = tmp_path / "비공개"
    monkeypatch.setattr(claims, "local_dir", lambda _: dest)

    class Client:
        model = "합성 모델"
        calls = active = maximum = 0
        closed = False

        async def ask(self, *_):
            self.calls += 1
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return JsonReply({"claims": []}, self.model, cost_usd=0.01)

        async def aclose(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr(claims.GeminiJson, "from_config", lambda *_a, **_k: client)
    assert asyncio.run(claims.run(tmp_path, 1, concurrency=2)) == pytest.approx(0.03)
    assert client.maximum == 2 and client.closed
    assert asyncio.run(claims.run(tmp_path, 1)) == 0
    assert client.calls == 3
    key = claims.fingerprint(inputs[0], client.model)
    path = dest / f"{key}.json"
    put(path, {"claims": None, "error": "실패", "usage": {"cost_usd": 0.05}})
    with pytest.raises(RuntimeError, match="retry-failed"):
        asyncio.run(claims.run(tmp_path, 1))
    assert client.calls == 3
    assert asyncio.run(claims.run(tmp_path, 1, retry_failed=True)) == pytest.approx(0.01)
    assert json.loads(path.read_text(encoding="utf-8"))["usage"]["cost_usd"] == pytest.approx(0.06)
    assert asyncio.run(claims.run(tmp_path, 1)) == 0


def test_claim_runner_budget_stop_does_not_start_paid_calls(tmp_path, monkeypatch):
    put(tmp_path / "summary.json", {"run_id": "합성", "sessions": [{"session": "세션", "scenario": "개발"}]})
    monkeypatch.setattr(claims, "article_map", lambda _: {})
    monkeypatch.setattr(claims, "read_log", lambda _: [])
    monkeypatch.setattr(claims, "exposed_inputs", lambda *_: iter([{"suggestion_id": "합성"}]))
    monkeypatch.setattr(claims, "local_dir", lambda _: tmp_path / "비공개")

    class Client:
        model = "합성 모델"

        async def ask(self, *_):
            pytest.fail("예산 확인 전에 호출했습니다")

        async def aclose(self):
            pass

    monkeypatch.setattr(claims.GeminiJson, "from_config", lambda *_a, **_k: Client())
    with pytest.raises(RuntimeError, match="예산 부족"):
        asyncio.run(claims.run(tmp_path, 0.19))


def test_public_copy_merges_overlapping_fragments_preserves_original_and_is_idempotent(tmp_path):
    run_dir = tmp_path / "results/실행"
    put(run_dir / "summary.json", {"run_id": "실행", "ended": "완료"})
    original = "가나다라마바사아자차카타파하거너더러머버서어저처커터퍼허"
    log = run_dir / "세션.jsonl"
    log.write_text(json.dumps({"type": "agent.say", "text": "앞" + original + "뒤"}, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    before = log.read_bytes()
    needles = {original[i:i + 25] for i in range(len(original) - 24)}
    out = e2e_logs.publish(run_dir, needles, tmp_path)
    assert out["redacted_regions"] == 1 and out["redacted_fragments"] == len(needles)
    assert json.loads(log.read_text(encoding="utf-8"))["text"] == "앞" + e2e_logs.MASK + "뒤"
    assert e2e_logs.original_path(log, tmp_path).read_bytes() == before
    assert e2e_logs.read_log(log, tmp_path)[0]["text"] == "앞" + original + "뒤"
    assert terms_scan.scan([run_dir], needles) == []
    manifest = json.loads((run_dir / e2e_logs.MANIFEST).read_text(encoding="utf-8"))
    assert manifest["files"][log.name]["sha256"] == e2e_logs.sha(before)
    assert e2e_logs.publish(run_dir, needles, tmp_path) == out
    private = e2e_logs.original_path(log, tmp_path)
    private.write_text("변조", encoding="utf-8")
    with pytest.raises(ValueError, match="해시"):
        e2e_logs.read_text(log, tmp_path)
    private.unlink()
    with pytest.raises(FileNotFoundError, match="원본이 필요"):
        e2e_logs.read_text(log, tmp_path)


def test_no_redaction_can_fall_back_to_identical_copy_and_running_logs_are_rejected(tmp_path):
    run_dir = tmp_path / "results/실행"
    put(run_dir / "summary.json", {"run_id": "실행", "ended": None})
    with pytest.raises(ValueError, match="진행 중"):
        e2e_logs.publish(run_dir, set(), tmp_path)
    put(run_dir / "summary.json", {"run_id": "실행", "ended": "완료"})
    e2e_logs.publish(run_dir, set(), tmp_path)
    original = e2e_logs.original_path(run_dir / "summary.json", tmp_path)
    original.unlink()
    assert e2e_logs.original_path(run_dir / "summary.json", tmp_path) == run_dir / "summary.json"


def test_single_command_runs_archives_and_checks_without_overwriting_existing_run(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(e2e_run, "ROOT", tmp_path)
    monkeypatch.setattr(e2e_run, "OUT_DIR", tmp_path / "results")
    monkeypatch.setattr(e2e_run, "load_fragment_sets", lambda: (set(), set()))

    def execute(cmd, **kwargs):
        calls.append("실행")
        assert cmd[-2:] == ["--run-id", "새실행"]
        put(tmp_path / "results/새실행/summary.json", {"run_id": "새실행", "ended": "완료"})
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(e2e_run.subprocess, "run", execute)
    monkeypatch.setattr(e2e_run, "publish", lambda *_: calls.append("원본·사본·검사"))
    e2e_run.run(["--", "--policy", "C", "--run-id", "새실행"])
    assert calls == ["실행", "원본·사본·검사"]
    with pytest.raises(SystemExit, match="덮어쓸"):
        e2e_run.run(["--run-id", "새실행"])


def test_single_command_failure_still_archives_and_missing_summary_is_not_success(tmp_path, monkeypatch):
    monkeypatch.setattr(e2e_run, "ROOT", tmp_path)
    monkeypatch.setattr(e2e_run, "OUT_DIR", tmp_path / "results")
    monkeypatch.setattr(e2e_run, "load_fragment_sets", lambda: (set(), set()))
    monkeypatch.setattr(e2e_run.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(returncode=0))
    with pytest.raises(RuntimeError, match="요약이 없습니다"):
        e2e_run.run(["--run-id", "빈실행"])
    calls = []

    def failed(*_a, **_kw):
        put(tmp_path / "results/실패실행/summary.json", {"run_id": "실패실행", "ended": "중단"})
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(e2e_run.subprocess, "run", failed)
    monkeypatch.setattr(e2e_run, "publish", lambda *_a: calls.append("원본 보존"))
    with pytest.raises(SystemExit) as result:
        e2e_run.run(["--run-id", "실패실행"])
    assert result.value.code == 1 and calls == ["원본 보존"]


def test_rejudgment_preserves_previous_private_bytes_and_updates_hash(tmp_path, monkeypatch):
    from jikgam.ingest import corpus

    monkeypatch.setattr(corpus, "load_docs", lambda *_: {})
    run_dir = tmp_path / "results/실행"
    put(run_dir / "summary.json", {"run_id": "실행", "ended": "완료"})
    path = run_dir / "세션.judge.json"
    put(path, {"error": "항목 누락", "cost_usd": 0.1})
    before = path.read_bytes()
    e2e_logs.publish(run_dir, set(), tmp_path)
    e2e_logs.store_json(path, {"error": None, "cost_usd": 0.2}, tmp_path)
    assert json.loads(e2e_logs.read_text(path, tmp_path)) == {"error": None, "cost_usd": 0.2}
    histories = list((tmp_path / "data/v2/local/e2e/실행/logs/history").glob("*"))
    assert len(histories) == 1 and histories[0].read_bytes() == before


def test_judge_session_accumulates_prior_cost_and_usage(tmp_path, monkeypatch):
    from jikgam.sim import judge

    path = tmp_path / "세션.judge.json"
    put(path, {"cost_usd": 0.1, "usage": [{"cost_usd": 0.1}]})
    monkeypatch.setattr(judge, "read_log", lambda _: [])

    async def fake_judge(*_):
        return {"cost_usd": 0.2, "usage": [{"cost_usd": 0.2}]}

    monkeypatch.setattr(judge, "ai_judge", fake_judge)
    saved = []
    monkeypatch.setattr(judge, "store_json", lambda path, value: saved.append((path, value)))
    rec = asyncio.run(judge.judge_session({"id": "개발"}, tmp_path, "세션", object()))
    assert rec["cost_usd"] == pytest.approx(0.3)
    assert len(rec["usage"]) == 2 and rec["previous_judgments"] == 1
    assert saved == [(path, rec)]
