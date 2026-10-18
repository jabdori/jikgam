"""R1 지적의 합성 실패 시나리오 회귀 검사. 유료 호출을 하지 않는다."""

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jikgam.sim import metrics, runner
from jikgam.sim.gemini import JsonReply
from jikgam.v2 import e2e_claims as claims, e2e_contract as contract, e2e_logs, e2e_queries, e2e_report as report
from jikgam.v2 import e2e_run, freeze, retry, terms_scan


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def row(sid="합성", valid=True, passed=True, n=10, bad=0, run=1):
    return {"scenario": sid, "run": run, "valid": valid, "pass": passed, "claims": n, "unsupported": bad,
            "missing": 0, "items": {i: passed for i in range(1, 11)}, "aux": {"precision": 1},
            "metrics": metrics.session_metrics([])}


def test_r1_01_improvement_requires_positive_lower_ci_even_at_sixteen_percent():
    a = [row(str(i), passed=False, run=k) for i in range(12) for k in range(1, 4)]
    b = [row(str(i), passed=i < 2, run=k) for i in range(12) for k in range(1, 4)]
    result = report.compare(a, b, [{}, {}])
    assert result["pass_delta"] == pytest.approx(1 / 6)
    assert result["bootstrap"]["ci95"]["pass_delta"][0] == 0
    assert result["conditions"]["pass_noninferior"] is True
    assert result["conditions"]["pass_improved"] is False
    assert report.compare(a, [row(str(i), run=k) for i in range(12) for k in range(1, 4)], [{}, {}])["conditions"]["pass_improved"] is True


def test_r1_01_missing_interval_defers_improvement(monkeypatch):
    monkeypatch.setattr(report, "paired_bootstrap", lambda *_: {"ci95": {"pass_delta": None}})
    result = report.compare([row(passed=False)], [row()], [{}, {}])
    assert result["conditions"]["pass_improved"] is None


def test_r1_02_unknown_compliance_cannot_dilute_claim_rate_or_confirm_any_condition():
    a = [row("가", n=10, bad=1), row("나", n=10, bad=1)]
    b = [row("가", n=10, bad=2), row("나", valid=None, n=100)]
    result = report.compare(a, b, [{}, {}])
    assert result["r0"]["unsupported_claim_rate"] == .1
    assert result["r4"]["unsupported_claim_rate"] == .2
    assert result["r4"]["valid"] == 1 and result["r4"]["invalid"] == 0
    assert result["r4"]["unknown_compliance"] == 1
    assert set(result["conditions"].values()) == {None}
    assert set(result["bootstrap"]["ci95"].values()) == {None}


def test_r1_02_planned_but_missing_sessions_are_unknown_not_silently_absent(tmp_path, monkeypatch):
    summary = {"run_id": "합성", "law": "r0", "runs": 2, "run_start": 1, "scenario_file": "합성.jsonl", "sessions": []}
    monkeypatch.setattr(report, "ROOT", tmp_path)
    monkeypatch.setattr(report, "load_run", lambda *_: {"summary": summary, "sessions": []})
    monkeypatch.setattr(report, "load_scenarios", lambda *_: [{"id": "가"}, {"id": "나"}])
    monkeypatch.setattr(claims, "local_dir", lambda *_: tmp_path / "캐시없음")
    rows, costs = report.collect(tmp_path)
    result = report.summarize(rows, costs)
    assert result["valid"] == 0 and result["invalid"] == 0
    assert result["missing_planned_runs"] == 4 and result["unknown_compliance"] == 4
    assert result["pass_rate"] is None


@pytest.mark.parametrize("value", [None, "false", 0, {}, []])
def test_r1_03_bad_supported_never_becomes_successful_zero_unsupported(tmp_path, monkeypatch, value):
    record = {"claims": [{"claim": "합성 주장", "supported": value, "reason": "판정 못함"}]}
    assert claims.validation_error(record)
    monkeypatch.setattr(claims, "exposed_inputs", lambda *_: iter([{"input_missing": False}]))
    monkeypatch.setattr(claims, "fingerprint", lambda *_: "합성")
    put(tmp_path / "합성.json", record)
    assert report.claim_counts([], {}, tmp_path, "모델") == (0, 0, 1, 1)


@pytest.mark.parametrize("data", [{}, {"claims": None}, {"claims": {}}, {"claims": [None]},
    {"claims": [{"supported": True, "reason": "이유"}]},
    {"claims": [{"claim": "문장", "supported": True}]},
    {"claims": [{"claim": 1, "supported": True, "reason": "이유"}]}])
def test_r1_03_all_required_types_are_checked(data):
    assert claims.validation_error(data)
    assert claims.validation_error({"claims": []}) is None


def test_r1_03_schema_errors_retry_same_input_at_two_four_eight_seconds(monkeypatch):
    waits, calls = [], []
    async def sleep(delay):
        waits.append(delay)
    class Client:
        async def ask(self, *args, **kwargs):
            calls.append((args, kwargs))
            return JsonReply({"claims": [{"claim": "주장", "supported": None, "reason": "이유"}]}, "합성", cost_usd=.01)
    monkeypatch.setattr(retry.asyncio, "sleep", sleep)
    reply = asyncio.run(retry.ask_json(Client(), "같은 지시", "같은 입력", claims.SCHEMA, validate=claims.validation_error))
    assert waits == [2, 4, 8] and len(calls) == 4
    assert all(call == calls[0] for call in calls)
    assert reply.error and reply.attempts == 4 and reply.cost_usd == pytest.approx(.04)


def test_r1_03_schema_failure_is_cached_and_empty_array_is_success(tmp_path, monkeypatch):
    put(tmp_path / "summary.json", {"run_id": "합성", "sessions": [{"session": "세션", "scenario": "개발"}]})
    source = {"input_missing": False, "question": "질문", "evidence": "근거", "answer": "답"}
    monkeypatch.setattr(claims, "read_log", lambda *_: [])
    monkeypatch.setattr(claims, "exposed_inputs", lambda *_: iter([source]))
    monkeypatch.setattr(claims, "local_dir", lambda *_: tmp_path / "비공개")
    monkeypatch.setattr(retry, "DELAYS", (0, 0, 0))
    class Client:
        model = "합성"
        data = {"claims": [{"claim": "주장", "supported": None, "reason": "이유"}]}
        async def ask(self, *_):
            return JsonReply(self.data, self.model)
        async def aclose(self):
            pass
    client = Client()
    monkeypatch.setattr(claims.GeminiJson, "from_config", lambda *_a, **_k: client)
    with pytest.raises(RuntimeError, match="주장 판정 실패"):
        asyncio.run(claims.run(tmp_path, 1))
    cache = next((tmp_path / "비공개").glob("*.json"))
    assert claims.read_cache(cache)["error"]
    client.data = {"claims": []}
    asyncio.run(claims.run(tmp_path, 1, retry_failed=True))
    assert claims.cache_valid(claims.read_cache(cache))


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    org = {"org": "org_a", "terms": "domain/org_a/terms.md", "question_set": "questions/main.json",
           "extra_question_set": "questions/extra.json", "decision_backend": "jev"}
    p = tmp_path / "config/orgs/org_a.toml"
    p.parent.mkdir(parents=True)
    p.write_text("\n".join(f'{k} = "{v}"' for k, v in org.items()), encoding="utf-8")
    for name in ("domain/org_a/terms.md", "questions/main.json", "questions/extra.json", "domain/org_a/customers.json",
                 "domain/org_a/customers_holdout.json", "domain/org_a/customers_v2_holdout.json",
                 "data/scenarios/org_a_v2_holdout.jsonl"):
        put(tmp_path / name, {"합성": True})
    (tmp_path / "docs/adr").mkdir(parents=True)
    (tmp_path / "docs/adr/0027-합성.md").write_text("합성", encoding="utf-8")
    args = runner.argument_parser().parse_args(["--policy", "C", "--law", "r0", "--frozen", "0027",
                                              "--scenario-file", "data/scenarios/org_a_v2_holdout.jsonl"])
    monkeypatch.setattr(freeze, "e2e_input_paths", lambda *_: [])
    c = contract.build_contract(args, tmp_path)
    freeze.write_manifest(tmp_path, "0027", [], c)
    return tmp_path, args, c


def test_r1_04_copied_holdout_cannot_use_original_manifest(frozen):
    root, args, _ = frozen
    copied = root / "data/scenarios/copied/org_a_v2_holdout.jsonl"
    put(copied, {"다른": True})
    with pytest.raises(SystemExit, match="정확한 저장소 경로"):
        freeze.require_e2e_frozen(copied, "0027", root, args)


@pytest.mark.parametrize("field,value", [("policy", "B"), ("runs", 3), ("run_start", 2), ("max_turns", 20),
    ("fast_delay_ms", 50), ("decision_backend", "다른"), ("customer_model", "다른"), ("judge_model", "다른"),
    ("scenarios", "s1"), ("customers", ["domain/다른.json"])])
def test_r1_04_each_changed_argument_is_refused(frozen, field, value):
    root, args, _ = frozen
    if field == "customers":
        put(root / value[0], {"다른": True})
    setattr(args, field, value)
    with pytest.raises(SystemExit, match="실행 계약"):
        freeze.require_e2e_frozen(root / args.scenario_file, "0027", root, args)


def test_r1_04_org_selected_files_and_law_only_difference(frozen):
    root, args, c = frozen
    assert "questions/main.json" in c["files"] and "domain/org_a/terms.md" in c["files"]
    assert freeze.require_e2e_frozen(root / args.scenario_file, "0027", root, args) == c
    args.law = "r4"
    assert freeze.require_e2e_frozen(root / args.scenario_file, "0027", root, args) == c
    (root / "questions/main.json").write_text("수정", encoding="utf-8")
    with pytest.raises(SystemExit, match="실행 계약"):
        freeze.require_e2e_frozen(root / args.scenario_file, "0027", root, args)


def test_r1_04_compare_rejects_different_contracts(frozen):
    _, _, c = frozen
    a, b = row(), row()
    a["execution_contract"] = c
    b["execution_contract"] = copy.deepcopy(c)
    b["execution_contract"]["arguments"]["runs"] = 3
    with pytest.raises(ValueError, match="계약"):
        report.compare([a], [b], [{}, {}])


def test_r1_04_selected_alternate_org_and_customer_are_hashed(frozen):
    root, args, original = frozen
    config = root / "config/orgs/org_a.toml"
    other = root / "config/orgs/다른.toml"
    other.write_text(config.read_text(encoding="utf-8").replace("domain/org_a/terms.md", "other/terms.md")
                     .replace("questions/main.json", "other/questions.json"), encoding="utf-8")
    for path in ("other/terms.md", "other/questions.json", "other/customers.json"):
        put(root / path, {"합성": True})
    args.org = str(other)
    args.customers = ["other/customers.json"]
    selected = contract.build_contract(args, root)
    assert {"other/terms.md", "other/questions.json", "other/customers.json"} <= set(selected["files"])
    assert selected != original


def test_r1_04_managed_server_builds_records_and_stops_without_paid_call(frozen, monkeypatch):
    root, args, c = frozen
    args.run_id = "합성빌드"
    (root / "bizapi").mkdir()
    calls = []
    class Process:
        pid = 123
        stopped = False
        def poll(self):
            return None
        def terminate(self):
            self.stopped = True
        def wait(self, timeout=None):
            return 0
    process = Process()
    def build(command, **kwargs):
        calls.append((command, kwargs))
        Path(command[3]).write_bytes(b"synthetic binary")
    monkeypatch.setattr(e2e_run.subprocess, "run", build)
    monkeypatch.setattr(e2e_run.subprocess, "Popen", lambda *a, **kw: (calls.append((a, kw)) or process))
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
    monkeypatch.setattr(e2e_run, "verify_live_instance", lambda *_: None)
    with e2e_run.managed_bizapi(args, c, root, port=0x7FFF) as (url, path):
        args.bizapi_url = url
        record = e2e_run.verify_bizapi_record(path, args, c, root)
        assert record["build_command"][:2] == ["go", "build"]
        assert record["data_files"] == {p: c["files"][p] for p in c["arguments"]["bizapi_data"]}
        assert not process.stopped
        changed = copy.deepcopy(record)
        changed["data_files"] = {}
        put(path, changed)
        with pytest.raises(ValueError, match="고객"):
            e2e_run.verify_bizapi_record(path, args, c, root)
    assert process.stopped and len(calls) == 2


def test_r1_04_wrapper_and_runner_refuse_changed_contract_before_launch(frozen, monkeypatch):
    root, args, _ = frozen
    args.runs = 2
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(runner, "load_org", lambda *_: pytest.fail("거절 전에 조직 로드"))
    with pytest.raises(SystemExit, match="계약"):
        asyncio.run(runner.run(args))
    monkeypatch.setattr(e2e_run, "ROOT", root)
    monkeypatch.setattr(e2e_run, "OUT_DIR", root / "results")
    monkeypatch.setattr(e2e_run.subprocess, "run", lambda *_a, **_k: pytest.fail("거절 전에 프로세스 실행"))
    with pytest.raises(SystemExit, match="계약"):
        e2e_run.run(["--policy", "C", "--law", "r0", "--frozen", "0027", "--runs", "2", "--run-id", "거절",
                     "--scenario-file", args.scenario_file])


def test_r1_05_checked_fallback_is_excluded_but_old_missing_input_is_not_reclassified():
    events = [{"type": "suggestion.created", "suggestion_id": "고정", "source": "checked_fallback"},
              {"type": "suggestion.exposed", "suggestion_id": "고정"},
              {"type": "suggestion.created", "suggestion_id": "과거", "fallback": True, "text": "예전"},
              {"type": "suggestion.exposed", "suggestion_id": "과거"}]
    rows = list(claims.exposed_inputs(events))
    assert rows[0]["excluded"] and rows[1]["input_missing"]


def test_r1_05_fixed_only_run_needs_no_paid_client(tmp_path, monkeypatch):
    put(tmp_path / "summary.json", {"run_id": "합성", "sessions": [{"session": "세션", "scenario": "개발"}]})
    monkeypatch.setattr(claims, "read_log", lambda *_: [])
    monkeypatch.setattr(claims, "exposed_inputs", lambda *_: iter([{"excluded": True, "input_missing": False}]))
    monkeypatch.setattr(claims.GeminiJson, "from_config", lambda *_a, **_k: pytest.fail("고정 문구에 판정 클라이언트 생성"))
    assert asyncio.run(claims.run(tmp_path, 0)) == 0


def test_r1_05_engine_error_has_source_and_is_not_legacy_missing_input(tmp_path, monkeypatch):
    from engine_fakes import EchoGenerator, NoBizApi, ScriptedDecider, utt
    from jikgam.engine.org import load_org
    from jikgam.engine.session import Session
    async def go():
        s = Session(load_org("org_a"), "합성오류", tmp_path, decision_client=ScriptedDecider(),
                    generator=EchoGenerator(), bizapi=NoBizApi())
        def broken(*_):
            raise RuntimeError("합성 오류")
        monkeypatch.setattr(s, "_generation_payload", broken)
        events = await s.handle(utt("u1", "가입 전에 약관 질문"))
        await s.aclose()
        return events
    events = asyncio.run(go())
    created = next(e for e in events if e["type"] == "suggestion.created")
    assert created["source"] == "engine_error" and created["generation_request_id"] is None
    values = list(claims.exposed_inputs(events))
    assert values[0]["excluded"] and not values[0]["input_missing"]
    assert report.claim_counts(events, {}, tmp_path, "합성") == (0, 0, 0, 0)


def test_r1_06_short_unique_lines_whitespace_changes_and_source_objects(tmp_path):
    short = "이문장은고유약관에만존재하는줄"
    long = "표준약관고유문장의공백과줄바꿈을바꾸어공개파일에넣어도찾습니다"
    common = "공개문서에도있는공유약관설명문장입니다"
    docs = {"game-terms": SimpleNamespace(lines=[SimpleNamespace(text=s) for s in (short, long, common)]),
            "law": SimpleNamespace(text=common)}
    unique, shared = terms_scan.fragment_sets(docs)
    assert short in unique and common in shared
    text = "앞 " + " \n".join(long) + " 뒤 " + " ".join(short)
    data = {"articles": [{"doc": "game-terms", "id": "조항", "text": "짧은본문", "title": "제목"}], "say": text}
    path = tmp_path / "공개.json"
    put(path, data)
    assert terms_scan.scan([path], unique)
    hidden, n, _ = e2e_logs.hide_value(data, unique)
    assert hidden["articles"][0]["text"] == e2e_logs.MASK and hidden["articles"][0]["id"] == "조항"
    assert hidden["say"].startswith("앞 ") and hidden["say"].endswith(e2e_logs.MASK) and n == 3
    put(path, hidden)
    assert not terms_scan.scan([path], unique)


def test_r1_07_actual_law_query_and_recorded_candidates_are_verified():
    seen = []
    law = SimpleNamespace(search=lambda q: (seen.append(q) or ["현재근거"], 0))
    event = {"version": 2, "law_profile": "r4", "query": "조직약관검색어", "law_query": "실제법령검색어",
             "law_candidates": ["현재근거"]}
    assert e2e_queries.current_candidates(event, "r4", law, {}) == "실제법령검색어"
    assert seen == ["실제법령검색어"]
    with pytest.raises(ValueError, match="후보"):
        e2e_queries.current_candidates({**event, "law_candidates": ["다른근거"]}, "r4", law, {})
    with pytest.raises(ValueError, match="프로필"):
        e2e_queries.current_candidates(event, "r0", law, {})
