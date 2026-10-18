"""사람 확인 파일의 손상·저장 중단은 합성 자료로만 재현한다."""

import hashlib
import json

import pytest

from jikgam.v2 import claim_check, report, stage
from tests.v2_claim_fixtures import bind_review_inputs


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def review_fixture(root):
    sample = {"split": "val", "items": [{"sample_id": "claim-01", "supported": True, "correctness": "match"}]}
    result = {"split": "val", "items": {"claim-01": {"supported": True, "correctness": "match", "done": True}}}
    bind_review_inputs(root, sample, result)
    put(claim_check.sample_path(root, "val"), sample)
    put(root / "user.json", result)
    claim_check.apply(root / "user.json", "val", root)


@pytest.mark.parametrize("kind", ["summary", "sample"])
@pytest.mark.parametrize("raw", [b"", b"{", b"[]", b"null", b"42", b"\xff"])
def test_corrupt_review_is_preserved_marked_and_reported_pending(tmp_path, kind, raw):
    review_fixture(tmp_path)
    path = claim_check.review_path(tmp_path, "val") if kind == "summary" else claim_check.sample_path(tmp_path, "val")
    path.write_bytes(raw)
    out = report.build_report(tmp_path, "val")
    assert claim_check.validated_review(tmp_path, "val") is None
    assert path.read_bytes() == raw
    marker = json.loads(path.with_suffix(".invalid.json").read_text(encoding="utf-8"))
    assert marker == {"valid": False, "invalidation_reason": "무효(손상)", "sha256": hashlib.sha256(raw).hexdigest()}
    assert out["claim_check"]["status"] == "사람 확인 전"
    assert out["claim_check"]["invalidation_reason"] == "무효(손상)"
    assert out["claim_check"]["supported_agreement"] is None
    assert "사람 확인 전 · 무효(손상)" in report.markdown(out)


@pytest.mark.parametrize("raw", [b"", b"{", b"[]", b"null", b"42", b"\xff"])
def test_corrupt_summary_does_not_block_answer_stage_resume(tmp_path, monkeypatch, raw):
    review_fixture(tmp_path)
    path = claim_check.review_path(tmp_path, "val")
    path.write_bytes(raw)
    name = claim_check.RUNS[0]
    put(tmp_path / f"results/v2/val/{name}.json", {})
    put(tmp_path / "config/v2/freeze-0025.json", {})
    result = f"results/v2/val/answers-{name}.json"
    command = ["uv", "run", "python", "-m", "jikgam.v2.answer", "--run", name, "--split", "val"]
    started = []

    def child(args, **kwargs):
        started.append(args)
        put(tmp_path / result, {"questions": 1, "reps": 1})

    monkeypatch.setattr(stage.subprocess, "run", child)
    assert stage.run_stage("answers", result, command, tmp_path, head="synthetic", expected={"q0": "single"})
    assert started == [command]
    assert path.read_bytes() == raw
    assert path.with_suffix(".invalid.json").is_file()
    assert (tmp_path / "outputs/logs/v2-test/done/answers.json").is_file()


def test_apply_with_corrupt_sample_preserves_previous_summary_and_refreshes_pending_report(tmp_path):
    review_fixture(tmp_path)
    summary = claim_check.review_path(tmp_path, "val")
    previous = summary.read_bytes()
    sample = claim_check.sample_path(tmp_path, "val")
    sample.write_bytes(b"{")
    out = claim_check.apply(tmp_path / "user.json", "val", tmp_path)
    assert out == {"valid": False, "invalidation_reason": "무효(손상)", "status": "사람 확인 전"}
    assert sample.read_bytes() == b"{" and summary.read_bytes() == previous
    saved = json.loads((tmp_path / "outputs/v2-smoke/report-val.json").read_text(encoding="utf-8"))
    assert saved["claim_check"]["status"] == "사람 확인 전"
    assert saved["claim_check"]["invalidation_reason"] == "무효(손상)"


def test_interrupted_summary_replacement_leaves_previous_complete_json(tmp_path, monkeypatch):
    review_fixture(tmp_path)
    path = claim_check.review_path(tmp_path, "val")
    previous = path.read_bytes()
    replacements = []

    def interrupt(source, target):
        assert target == path and source.parent == path.parent and source != path
        assert path.read_bytes() == previous
        assert json.loads(source.read_text(encoding="utf-8"))["valid"] is True
        replacements.append(source)
        raise OSError("합성 교체 중단")

    monkeypatch.setattr(claim_check.os, "replace", interrupt)
    with pytest.raises(OSError, match="합성 교체 중단"):
        claim_check.apply(tmp_path / "user.json", "val", tmp_path)
    assert len(replacements) == 1 and not replacements[0].exists()
    assert path.read_bytes() == previous
    assert claim_check.validated_review(tmp_path, "val") is not None


def test_new_valid_review_is_not_invalidated_by_preserved_corruption_marker(tmp_path):
    review_fixture(tmp_path)
    path = claim_check.review_path(tmp_path, "val")
    path.write_bytes(b"{")
    claim_check.invalidate_review(tmp_path, "val")
    marker = path.with_suffix(".invalid.json")
    assert marker.is_file()
    claim_check.apply(tmp_path / "user.json", "val", tmp_path)
    assert marker.is_file()
    out = report.build_report(tmp_path, "val")
    assert out["claim_check"]["status"] == "사람 확인 완료"
    assert "invalidation_reason" not in out["claim_check"]


def test_missing_review_files_are_pending_without_corruption_marker(tmp_path):
    assert report.build_report(tmp_path, "val")["claim_check"]["status"] == "사람 확인 전"
    assert not list(tmp_path.rglob("*.invalid.json"))
