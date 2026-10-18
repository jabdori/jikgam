"""사람 확인 도구는 가짜 답변·표본·원문만으로 검증한다."""

import json

import pytest

from jikgam.v2 import claim_check, report
from tests.v2_claim_fixtures import bind_review_inputs


def fake_sources():
    return {name: [{"id": f"q{i}", "rep": 0, "judge_error": None,
                    "claims": [{"claim": f"합성 주장 {i}", "supported": i % 2 == 0,
                                "correctness": "match", "reason": "비공개 모델 이유"}]} for i in range(50)]
            for name in claim_check.RUNS}


def fake_sample_result():
    items = [{"sample_id": f"claim-{i}", "id": f"q{i}", "row": i, "run": claim_check.RUNS[0], "rep": 0,
              "claim_index": 0, "supported": True, "correctness": "match"} for i in range(5)]
    sample = {"split": "val", "items": items, "sample_digest": claim_check.sample_digest(items)}
    result = {"split": "val", "sample_digest": sample["sample_digest"],
              "items": {c["sample_id"]: {"supported": True, "correctness": "match", "done": True} for c in items}}
    result["items"]["claim-2"]["correctness"] = "contradict"
    result["items"]["claim-3"]["correctness"] = "unknown"
    result["items"]["claim-4"]["supported"] = False
    return sample, result


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_sample_is_deterministic_pools_runs_and_excludes_judge_errors():
    sources = fake_sources()
    sources[claim_check.RUNS[0]].append({"id": "bad", "judge_error": "오류", "claims": [{"claim": "제외 주장"}]})
    picked, total = claim_check.pick_claims(sources)
    reordered = dict(reversed(list(sources.items())))
    assert claim_check.pick_claims(reordered) == (picked, total)
    assert total == 100 and len(picked) == 30
    assert {c["run"] for c in picked} == set(claim_check.RUNS)
    assert len({(c["run"], c["row"], c["claim_index"]) for c in picked}) == 30
    assert all(c["id"] != "bad" for c in picked)
    assert "합성 주장" not in json.dumps(picked, ensure_ascii=False)
    assert "비공개 모델 이유" not in json.dumps(picked, ensure_ascii=False)


def test_fewer_than_thirty_val_claims_use_all_available_claims():
    sources = {claim_check.RUNS[0]: fake_sources()[claim_check.RUNS[0]][:2]}
    chosen, total = claim_check.pick_claims(sources)
    assert total == len(chosen) == 2


def test_page_has_no_model_verdict_or_reason_and_escapes_source_text():
    item = {"sample_id": "claim-01", "question": "합성 질문 <script>금지</script>",
            "evidence": "합성 근거", "gold": "합성 정답", "claim": "합성 주장",
            "supported": False, "correctness": "contradict", "reason": "비공개 모델 이유"}
    page = claim_check.render_page([item], "val", "digest")
    changed = {**item, "supported": True, "correctness": "match", "reason": "다른 비공개 이유"}
    assert page == claim_check.render_page([changed], "val", "digest")
    assert "비공개 모델 이유" not in page
    assert '<script>금지</script>' not in page
    assert '&lt;script&gt;금지&lt;/script&gt;' in page
    assert " selected" not in page
    assert "localStorage" in page and "결과 내려받기" in page
    assert "claims_${SPLIT}_result.json" in page
    assert "정답과 맞음" in page and "근거 없음" in page


def test_agreement_counts_both_judgments_separately():
    sample, result = fake_sample_result()
    assert claim_check.agreement(sample, result) == {
        "sampled": 5, "reviewed": 5, "supported_matches": 4, "correctness_matches": 3,
        "both_matches": 2, "supported_agreement": 0.8, "correctness_agreement": 0.6, "both_agreement": 0.4}


@pytest.mark.parametrize("problem", ["partial", "missing", "extra", "wrong_sample", "wrong_split", "invalid_label"])
def test_apply_rejects_incomplete_or_wrong_sample(problem):
    sample, result = fake_sample_result()
    if problem == "partial":
        result["items"]["claim-0"]["done"] = False
    elif problem == "missing":
        del result["items"]["claim-0"]
    elif problem == "extra":
        result["items"]["unknown"] = result["items"]["claim-0"]
    elif problem == "wrong_sample":
        result["sample_digest"] = "다른 표본"
    elif problem == "wrong_split":
        result["split"] = "test"
    else:
        result["items"]["claim-0"]["supported"] = "true"
    with pytest.raises(ValueError):
        claim_check.agreement(sample, result)


def test_apply_writes_counts_and_fingerprint_without_source_text(tmp_path):
    sample, result = fake_sample_result()
    bind_review_inputs(tmp_path, sample, result)
    put(tmp_path / "data/v2/local/review/claims_val_sample.json", sample)
    put(tmp_path / "user.json", result)
    summary = claim_check.apply(tmp_path / "user.json", "val", tmp_path)
    path = tmp_path / "outputs/v2-smoke/claim-check-val.json"
    assert json.loads(path.read_text(encoding="utf-8")) == summary
    assert all(isinstance(v, (int, float)) for k, v in summary.items() if k not in ("sample_digest", "source_hashes"))
    assert summary["sample_digest"] == sample["sample_digest"]
    assert not (tmp_path / "results").exists()
    assert "claim-0" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("rate,expected", [(0.7999, "판정 보류"), (0.8, "만족"), (0.8001, "만족"), (None, "만족")])
def test_report_holds_answer_quality_only_below_point_eight(rate, expected):
    conditions = {"8.2": [{"condition": label, "result": "만족"} for label in
                          ("R4 검색 p95(ms)", "R4 − R0 근거 없는 주장 비율", "R4 − R0 정답률")]}
    summary = {"supported_agreement": rate, "correctness_agreement": rate} if rate is not None else None
    status = report.apply_claim_check(conditions, summary)
    assert conditions["8.2"][0]["result"] == "만족"
    assert all(r["result"] == expected for r in conditions["8.2"][1:])
    if rate is None:
        assert status["status"] == "사람 확인 전"
    elif rate < 0.8:
        assert all(r["result_before_human_check"] == "만족" for r in conditions["8.2"][1:])
    else:
        assert status["status"] == "사람 확인 완료"


def test_report_loads_claim_check_and_displays_pending_status(tmp_path):
    pending = report.build_report(tmp_path, "val")
    assert pending["claim_check"]["status"] == "사람 확인 전"
    assert "사람 확인 전" in report.markdown(pending)
    path = tmp_path / "outputs/v2-smoke/claim-check-val.json"
    sample, _ = fake_sample_result()
    bind_review_inputs(tmp_path, sample)
    put(tmp_path / "data/v2/local/review/claims_val_sample.json", sample)
    put(path, {"supported_agreement": 0.7, "correctness_agreement": 0.9, "both_agreement": 0.6, "reviewed": 5, "sampled": 5,
               "valid": True, "sample_digest": sample["sample_digest"], "source_hashes": sample["source_hashes"]})
    out = report.build_report(tmp_path, "val")
    held = [r for r in out["conditions"]["8.2"] if r["condition"] in ("R4 − R0 근거 없는 주장 비율", "R4 − R0 정답률")]
    assert len(held) == 2 and held[0]["result"] == "판정 보류" and held[1]["result"] == report.MISSING
    assert "근거 판정 일치율 0.7" in report.markdown(out)


def test_build_val_with_only_r0_reconstructs_same_evidence_and_keeps_verdict_separate(tmp_path, monkeypatch):
    from jikgam.ingest import chunk as chunking
    from jikgam.ingest import corpus
    from jikgam.ingest.chunk import Chunk
    from jikgam.ingest.corpus import Doc, Line
    from jikgam.v2 import evaluate

    text = "합성 근거입니다."
    doc = Doc("d", "합성 문서", "법률", True, [Line(1, "본문", 1, "text", text, 0)])
    chunk = Chunk("chunk", "d", 1, 1, 0, len(text), 12)
    question = {"id": "q0", "type": "single", "question": "합성 질문입니다.", "answer": "합성 정답",
                "evidence": [{"alternatives": [{"doc": "d", "start": 0, "end": len(text)}]}]}
    sources = {claim_check.RUNS[0]: fake_sources()[claim_check.RUNS[0]][:1]}
    answer = tmp_path / f"data/v2/local/answers/val/{claim_check.RUNS[0]}.jsonl"
    answer.parent.mkdir(parents=True)
    answer.write_text(json.dumps(sources[claim_check.RUNS[0]][0], ensure_ascii=False) + "\n", encoding="utf-8")
    put(tmp_path / f"data/v2/local/runs/val/{claim_check.RUNS[0]}.jsonl", {"id": "q0", "budget4000": ["chunk"]})
    monkeypatch.setattr(corpus, "load_docs", lambda: {"d": doc})
    monkeypatch.setattr(chunking, "load", lambda name: [chunk])
    monkeypatch.setattr(evaluate, "load_questions", lambda split, include_none: [question])
    sample_path = claim_check.sample_path(tmp_path, "val")
    sample_path.parent.mkdir(parents=True)
    sample_path.write_bytes(b"{")
    replace = claim_check.os.replace
    replacements = []

    def atomic_sample(source, target):
        assert target == sample_path and source.parent == target.parent and source != target
        assert sample_path.read_bytes() == b"{"
        assert json.loads(source.read_text(encoding="utf-8"))["population"] == 1
        replacements.append(target)
        replace(source, target)

    monkeypatch.setattr(claim_check.os, "replace", atomic_sample)
    summary = claim_check.build("val", tmp_path)
    assert replacements == [sample_path]
    assert summary["claims"] == 1
    assert summary["runs_used"] == [claim_check.RUNS[0]]
    assert summary["runs_missing"] == [claim_check.RUNS[1]]
    page = (tmp_path / "data/v2/local/review/claims_val.html").read_text(encoding="utf-8")
    assert "합성 질문입니다." in page and "[근거 1] 「합성 문서」" in page
    assert "정답 근거 1 「합성 문서」" in page and "합성 주장 0" in page
    sample_text = (tmp_path / "data/v2/local/review/claims_val_sample.json").read_text(encoding="utf-8")
    assert "합성 질문" not in sample_text and "합성 근거" not in sample_text and "합성 주장" not in sample_text
    assert "비공개 모델 이유" not in page and "비공개 모델 이유" not in sample_text
    assert '"supported": true' in sample_text


def test_build_test_requires_both_answer_runs(tmp_path):
    path = tmp_path / f"data/v2/local/answers/test/{claim_check.RUNS[0]}.jsonl"
    put(path, fake_sources()[claim_check.RUNS[0]][0])
    with pytest.raises(ValueError, match="답변 파일"):
        claim_check.build("test", tmp_path)


def test_test_sample_requires_thirty_valid_claims(tmp_path):
    for name, rows in fake_sources().items():
        put(tmp_path / f"data/v2/local/answers/test/{name}.jsonl", rows[0])
    with pytest.raises(ValueError, match="30개"):
        claim_check.build("test", tmp_path)
