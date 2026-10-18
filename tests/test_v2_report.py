"""고정 실행 묶음은 가짜 자료로 검증한다. test·점검용 질문은 읽지 않는다."""

import asyncio
import json
from pathlib import Path

import pytest

from jikgam.v2 import boundary_runs, freeze, report


def put(root, path, value):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def fake_run(found, total=100):
    return {"metrics": {"single": {"items@2000": found / total}},
            "per_question@2000": {"single": {f"q{i}": [i < found] for i in range(total)}},
            "latency_ms": {"retrieval_p95": 250, "rerank_p95": 250}}


@pytest.mark.parametrize("value,op,threshold,expected", [
    (0.80, "<", 0.80, "불만족"), (0.7999, "<", 0.80, "만족"),
    (250, ">", 250, "불만족"), (250.01, ">", 250, "만족"),
    (250, "<=", 250, "만족"), (250.01, "<=", 250, "불만족"),
    (0.10, ">=", 0.10, "만족"), (0.0999, ">=", 0.10, "불만족"),
    (-0.03, ">=", -0.03, "만족"), (-0.0301, ">=", -0.03, "불만족"),
    (1 / 5, "<=", 1 / 5, "만족"), (0.2001, "<=", 1 / 5, "불만족"),
    (0, ">", 0, "불만족"), (0.0001, ">", 0, "만족"),
])
def test_condition_inclusive_and_strict_boundaries(value, op, threshold, expected):
    assert report.condition("가짜 조건", value, op, threshold)["result"] == expected


def test_missing_input_is_not_zero_or_success():
    out = report.judge_conditions({}, {}, {}, {}, {})
    for rows in out.values():
        if isinstance(rows, list):
            assert all(r["result"] == report.MISSING for r in rows)
    assert out["8.4"] == "판정하지 않음(ADR 0025 범위 조정)"


def test_judgment_all_conditions_and_cost_time_thresholds():
    runs = {"R0": fake_run(80), "R1": fake_run(80), "R4": fake_run(90),
            "R4c-Jev": fake_run(87), "R4c-Qwen": fake_run(87)}
    checks = {"R0": fake_run(10, 30), "R4": fake_run(11, 30)}
    chunks = {report.B: {"calls": {"total_cost_usd": 1}, "seconds_wall": 5},
              "c-jev-W4000-C800-raw": {"calls": {"total_cost_usd": 0.2}, "seconds_wall": 5},
              "c-qwen-W4000-C800-raw": {"calls": {"total_cost_usd": 0}, "seconds_wall": 5}}
    answers = {k: {"by_type": {"single": {"unsupported_claim_rate": 0, "accuracy": 1}}} for k in ("R0", "R4")}
    rows = {k: [{"id": "q", "type": "single", "group": "법령",
                 "claims": [{"supported": True, "correctness": "match"}]}] for k in ("R0", "R4")}
    out = report.judge_conditions(runs, checks, answers, chunks, rows)
    assert all(r["result"] == "불만족" for r in out["8.1"])
    assert all(r["result"] == "만족" for r in out["8.2"])
    assert all(r["result"] == "만족" for r in out["8.3 R4c-Jev"])
    assert all(r["result"] == "만족" for r in out["8.3 R4c-Qwen"])
    chunks["c-jev-W4000-C800-raw"]["calls"]["total_cost_usd"] = 0.2001
    chunks["c-qwen-W4000-C800-raw"]["seconds_wall"] = 5.01
    out = report.judge_conditions(runs, checks, answers, chunks, rows)
    assert out["8.3 R4c-Jev"][1]["result"] == "불만족"
    assert out["8.3 R4c-Qwen"][2]["result"] == "불만족"


def test_paired_missing_questions_cannot_produce_a_passing_interval():
    a, b = fake_run(90), fake_run(80)
    del b["per_question@2000"]["single"]["q0"]
    assert report.paired(a, b) is None


def test_group_recall_weights_evidence_items_and_reviewed_ids():
    run = {"per_question@2000": {"single": {"a": [True]}, "multi": {"b": [False, False], "c": [True, True]}}}
    groups = {"a": "법령", "b": "법령", "c": "넘나듦"}
    grouped = report.group_recall(run, groups)
    assert grouped["법령"] == {"questions": 2, "evidence_items": 3, "items@2000": 0.3333, "complete@2000": 0.5}
    assert grouped["넘나듦"]["items@2000"] == 1
    assert grouped["고시"]["items@2000"] is None
    assert report.group_recall(run, groups, {"a"})["법령"]["items@2000"] == 1


def test_answer_bootstrap_uses_question_units_and_duplicate_samples():
    def rows(bad):
        return [{"id": f"q{i}", "type": "single", "group": "법령", "claims": [
            {"supported": not (bad and i == 0), "correctness": "match"}]} for i in range(2)]
    diff = report.answer_comparison(rows(False), rows(True), "unsupported_claim_rate")
    assert diff == {"diff": -0.5, "ci95": [-1.0, 0.0], "questions": 2}


def test_select_windows_uses_judgment_area_and_excludes_segment_first_line():
    # 창은 1~10, 9~18, 17~26, 25~30; 판단은 1~8, 9~16, 17~24, 25~30이다.
    lt = [100] * 30
    assert boundary_runs.selected_windows(lt, [{"start": 8, "end": 9}], 1000) == [(9, 16, 18, 1)]
    assert boundary_runs.selected_windows(lt, [{"start": 16, "end": 17}], 1000) == [(17, 24, 26, 2)]
    assert boundary_runs.selected_windows(lt, [{"start": 8, "end": 8}], 1000) == []
    assert boundary_runs.within_segments([{"id": "s", "doc": "d", "start": 3, "end": 6}],
                                         {"d": [1, 3, 4, 6, 7]}) == {"s": [4, 6]}


def test_repeat_inputs_are_fixed_and_hard_cuts_are_reported_separately(tmp_path, monkeypatch):
    from jikgam.ingest.corpus import Doc, Line
    from jikgam.ingest.window import CallLog

    segs = [{"id": "s", "doc": "d", "start": 9, "end": 20}]
    put(tmp_path, "data/v2/boundary/segments.json", {"segments": segs})
    doc = Doc("d", "가짜 문서", "법률", True, [Line(i, "본문", 1, "text", "가", i - 1) for i in range(1, 31)])
    seen = []

    class FakeJudge:
        def __init__(self, cfg):
            pass

        async def llm_window(self, doc, ctx, s, jend, wend, k):
            seen.append((ctx, s, jend, wend, k))
            return [11], "", CallLog("d", k, "judge")

        async def aclose(self):
            pass

    monkeypatch.setattr(boundary_runs, "ROOT", tmp_path)
    monkeypatch.setattr(boundary_runs, "load_docs", lambda only: {"d": doc})
    monkeypatch.setattr(boundary_runs.chunking, "line_tokens", lambda d: [400] * 30)
    monkeypatch.setattr(boundary_runs, "chunk_starts", lambda name: {"d": [1, 5, 13, 19]})
    monkeypatch.setattr(boundary_runs, "Judge", FakeJudge)
    out = asyncio.run(boundary_runs.run("b"))
    assert seen[:len(seen) // 2] == seen[len(seen) // 2:]
    assert any("5줄에서 시작" in ctx for ctx, *_ in seen)
    assert out["judgment_runs"] == [{"s": [11]}, {"s": [11]}]
    assert out["runs"] == [{"s": [11, 13, 15, 17, 19]}, {"s": [11, 13, 15, 17, 19]}]


def test_freeze_detects_changed_missing_and_added_file(tmp_path):
    for name in ("a", "b"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    manifest = freeze.write_manifest(tmp_path, paths=["a", "b"])
    assert len(manifest["files"]["a"]) == 64
    assert freeze.verify_manifest(tmp_path, paths=["a", "b"]) == []
    (tmp_path / "a").write_text("변경", encoding="utf-8")
    (tmp_path / "b").unlink()
    (tmp_path / "c").write_text("추가", encoding="utf-8")
    assert freeze.verify_manifest(tmp_path, paths=["a", "b", "c"]) == ["a", "b", "c"]


def test_freeze_discovers_corpus_outputs_and_embedding_caches(tmp_path):
    put(tmp_path, "data/v2/extracted/summary.json", {"docs": {"d": {"output": "data/v2/local/extracted/d.jsonl"}}})
    p = tmp_path / "data/v2/local/emb/nested/a.pt"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"cache")
    paths = freeze.input_paths(tmp_path)
    assert "data/v2/local/extracted/d.jsonl" in paths
    assert "data/v2/local/emb/nested/a.pt" in paths
    assert "data/v2/chunks/c-qwen-W4000-C800-raw.probs.json" in paths


def test_val_never_reads_reference_boundaries_or_check_questions(tmp_path, monkeypatch):
    original = Path.open

    def guarded(path, *args, **kwargs):
        assert "reference-" not in str(path)
        assert "/check/" not in path.as_posix()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    out = report.build_report(tmp_path, "val")
    assert out["boundary"] == {"status": "test에서만 계산"}
    assert "판정 아님" in out["status"]
    assert out["check"] == {}


def test_fake_report_has_only_metrics_and_drops_source_text(tmp_path):
    run = fake_run(1, 2)
    run.update({"question": "유출 금지 질문", "answer": "유출 금지 답변"})
    run["metrics"]["single"]["debug"] = "유출 금지 원문"
    put(tmp_path, "results/v2/val/R0-article256.json", run)
    out = report.build_report(tmp_path, "val")
    assert "유출 금지" not in json.dumps(out, ensure_ascii=False)
    assert "유출 금지" not in report.markdown(out)


def test_test_reference_comparison_uses_only_fake_files(tmp_path):
    seg = {"id": "s", "doc": "d", "start": 1, "end": 8}
    put(tmp_path, "data/v2/boundary/segments.json", {"segments": [seg]})
    for who in ("astra", "sol"):
        put(tmp_path, f"data/v2/boundary/reference-{who}.json", {"boundaries": {"s": [4]}})
    for name in ("fixed256", "article256", report.B, report.CJ, report.CQ):
        put(tmp_path, f"data/v2/chunks/{name}.jsonl", {"doc": "d", "start_line": 4})
    out = report.boundary_report(tmp_path, "test")
    assert out["astra_vs_sol"]["f1"] == 1
    assert out["astra"][report.B]["f1"] == 1
