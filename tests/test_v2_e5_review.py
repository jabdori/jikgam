"""R1b의 다섯 합성 실패와 서버 병합 데이터 계약을 검증한다. 유료 호출은 없다."""

import copy
import json
import socket
import shutil
from pathlib import Path

import pytest

from jikgam.sim import runner
from jikgam.v2 import e2e_check, e2e_contract, e2e_logs, e2e_run, freeze, terms_scan
from test_v2_e4_review import frozen, put


def test_r1b01_named_copy_and_changed_runs_are_rejected_before_organization_loading(frozen, monkeypatch):
    root, args, original = frozen
    put(root / "data/scenarios/copy.jsonl", {"합성": "복사"})
    args.scenario_file = "data/scenarios/copy.jsonl"
    args.runs = 4
    args.run_id = "잠금검사"
    with pytest.raises(SystemExit, match="정확한 저장소 경로"):
        freeze.require_e2e_frozen(root / args.scenario_file, "0027", root, args)
    assert args.frozen_verification["contract_match"] is False
    assert args.frozen_verification["manifest_match"] is True
    saved = json.loads((root / "data/v2/local/e2e/잠금검사/frozen-verification.json").read_text(encoding="utf-8"))
    assert saved == args.frozen_verification
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(runner, "load_org", lambda *_: pytest.fail("잠금 거절 전에 조직 로드"))
    import asyncio
    with pytest.raises(SystemExit, match="정확한 저장소 경로"):
        asyncio.run(runner.run(args))


def test_r1b01_explicit_frozen_applies_to_dev_names_and_records_checks_separately(frozen):
    root, args, _ = frozen
    args.scenario_file = "data/scenarios/dev.jsonl"
    put(root / args.scenario_file, {"합성": True})
    c = e2e_contract.build_contract(args, root)
    freeze.write_manifest(root, "0027", [], c)
    assert freeze.require_e2e_frozen(root / args.scenario_file, "0027", root, args) == c
    assert args.frozen_verification["contract_match"] is True
    assert args.frozen_verification["manifest_match"] is True
    manifest_path = root / "config/v2/freeze-0027.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["questions/main.json"] = "0" * 64
    put(manifest_path, manifest)
    with pytest.raises(SystemExit, match="고정 해시"):
        freeze.require_e2e_frozen(root / args.scenario_file, "0027", root, args)
    assert args.frozen_verification["contract_match"] is True
    assert args.frozen_verification["manifest_match"] is False


@pytest.mark.parametrize("adr", ["", "잘못된번호"])
def test_r1b01_even_invalid_explicit_frozen_is_not_ignored_for_dev_paths(tmp_path, adr):
    with pytest.raises(SystemExit, match="ADR 번호"):
        freeze.require_e2e_frozen(tmp_path / "data/scenarios/copy.jsonl", adr, tmp_path)


def test_r1b02_selection_order_is_shared_and_duplicates_rejected(frozen):
    root, args, _ = frozen
    args.scenarios = " 가, 나 "
    c = e2e_contract.build_contract(args, root)
    assert c["arguments"]["scenarios"] == ["가", "나"]
    rows = [{"id": "나"}, {"id": "가"}]
    assert [r["id"] for r in runner.select(rows, args.scenarios)] == ["가", "나"]
    assert runner.select(rows, c["arguments"]["scenarios"]) == runner.select(rows, args.scenarios)
    freeze.write_manifest(root, "0027", [], c)
    args.scenarios = "나,가"
    with pytest.raises(SystemExit, match="실행 계약"):
        freeze.require_e2e_frozen(root / args.scenario_file, "0027", root, args)
    args.scenarios = "가,나,가"
    with pytest.raises(ValueError, match="중복"):
        e2e_contract.build_contract(args, root)
    with pytest.raises(SystemExit, match="중복"):
        runner.select(rows, args.scenarios)


@pytest.mark.parametrize("key", ["input", "claim", "reason"])
@pytest.mark.parametrize("separator", ["\n", "\t", " "])
def test_r1b04_each_decoded_string_without_source_is_detected_and_redacted(tmp_path, key, separator):
    phrase = "이것은검사전용고유문장으로줄바꿈이나탭을끼워도반드시검출해야합니다"
    text = separator.join(phrase)
    path = tmp_path / "합성.json"
    value = {"nested": [{key: text}]}
    put(path, value)
    needles = terms_scan.FragmentSet([phrase[:25]])
    assert terms_scan.scan([path], needles)[0]["count"] == 1
    hidden, regions, _ = e2e_logs.hide_value(value, needles)
    assert regions == 1 and e2e_logs.MASK in hidden["nested"][0][key]
    put(path, hidden)
    assert terms_scan.scan([path], needles) == []


def test_r1b05_real_profile_id_formats_are_consistently_aliased_only_for_display():
    a, b = "article256:ecom-act:0000", "b-W4000-C800-raw:ecom-act:0000"
    original = {"claims": [{"id": "claims-01", "law": "r0", "model": True, "claim": f"{a}의 내용",
                           "context": f"고객이 {a}와 {b}를 질문", "input": json.dumps({"articles": [
                               {"id": a, "text": "본문 보존"}, {"id": b, "text": "다른 본문"}], "refs": [a, b]}, ensure_ascii=False)}],
                "scenario_items": [{"id": "scenario_items-01", "item": 9, "expected": f"{b}의 안내", "context": a}]}
    sample = {"name": "합성", "items": copy.deepcopy(original), "sample_digest": "digest", "input_hash": "hash"}
    sample["id_aliases"] = e2e_check.id_aliases(sample["items"])
    page = e2e_check.render_page(sample)
    for clue in ("R0", "R4", "article256", "b-W4000", a, b):
        assert clue not in page
    display = e2e_check.display_items(sample)
    alias = sample["id_aliases"][a]
    payload = json.loads(display["claims"][0]["input"])
    assert payload["articles"][0]["id"] == alias == payload["refs"][0]
    assert alias in display["claims"][0]["context"] and alias in display["claims"][0]["claim"]
    assert alias == display["scenario_items"][0]["context"]
    assert "본문 보존" in page and sample["items"] == original
    assert '"id_aliases"' not in page


@pytest.mark.parametrize("feedback", ["", "\n\n이전 출력의 문제: 같은 근거로 다시 작성"])
def test_r1c01_aliases_only_actual_input_law_ids_and_preserves_business_codes(feedback):
    a, b = "article256:ecom-act:0000", "b-W4000-C800-raw:ecom-act:0000"
    business = "case:refund:123"
    outside = "article256:ecom-act:9999"
    payload = {"articles": [{"id": a, "text": f"확인 코드는 {business}입니다. 인용 예시 {outside}",
                             "refs": [a, b]}], "system_facts": {"code": business}}
    original = {"claims": [{"id": "claims-01", "claim": "확인 코드는 123으로 끝납니다.",
                            "input": json.dumps(payload, ensure_ascii=False) + feedback,
                            "context": f"참조 {a}, {b}, 업무 코드 {business}"}], "scenario_items": []}
    sample = {"items": copy.deepcopy(original)}
    sample["id_aliases"] = e2e_check.id_aliases(original)
    assert set(sample["id_aliases"]) == {a, b}
    # 이전 방식의 과도한 대응표가 있어도 표시 대상을 넓히지 않는다.
    sample["id_aliases"][business] = "잘못된별칭"
    shown = e2e_check.display_items(sample)["claims"][0]
    shown_payload, _ = json.JSONDecoder().raw_decode(shown["input"])
    assert shown_payload["articles"][0]["text"] == payload["articles"][0]["text"]
    assert shown_payload["system_facts"]["code"] == business
    assert shown["claim"] == original["claims"][0]["claim"]
    assert business in shown["context"] and "잘못된별칭" not in shown["context"]
    assert a not in shown["input"] and b not in shown["input"]
    assert outside in shown["input"] and sample["items"] == original


def test_separate_ordered_bizapi_data_contract(frozen):
    root, args, _ = frozen
    args.customers = ["domain/org_a/customers_v2_holdout.json"]
    c = e2e_contract.build_contract(args, root)
    assert c["arguments"]["customers"] == ["domain/org_a/customers_v2_holdout.json"]
    assert c["arguments"]["bizapi_data"] == e2e_contract.HOLDOUT_BIZAPI_DATA
    assert c["bizapi_data"] == [{"path": p, "sha256": c["files"][p]} for p in e2e_contract.HOLDOUT_BIZAPI_DATA]
    args.bizapi_data = list(reversed(e2e_contract.HOLDOUT_BIZAPI_DATA))
    with pytest.raises(ValueError, match="세 파일 순서"):
        e2e_contract.build_contract(args, root)


def test_dev_bizapi_data_uses_same_contract_path_with_one_file(frozen):
    root, args, _ = frozen
    args.scenario_file = "data/scenarios/dev.jsonl"
    put(root / args.scenario_file, {"합성": True})
    args.customers = None
    c = e2e_contract.build_contract(args, root)
    assert c["arguments"]["customers"] == c["arguments"]["bizapi_data"] == ["domain/org_a/customers.json"]
    assert c["bizapi_data"] == [{"path": "domain/org_a/customers.json", "sha256": c["files"]["domain/org_a/customers.json"]}]


@pytest.mark.parametrize("data_count", [1, 3])
def test_r1b03_real_managed_server_is_bound_to_current_nonce_and_ordered_loaded_data(tmp_path, data_count):
    source_root = Path(__file__).resolve().parents[1]
    # 미공개 테스트·입력은 읽지 않고 런타임 Go 소스만 임시 작업 공간에 복사한다.
    for path in (source_root / "bizapi").rglob("*"):
        if path.is_file() and (path.suffix == ".go" and not path.name.endswith("_test.go") or path.name in ("go.mod", "go.sum")):
            target = tmp_path / path.relative_to(source_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    data_paths = ["data/first.json", "data/second.json", "data/third.json"][:data_count]
    for p in data_paths:
        put(tmp_path / p, {"reference_date": "2026-10-12", "customers": [], "incidents": []})
    from jikgam.v2.freeze import sha256
    files = {p.relative_to(tmp_path).as_posix(): sha256(p) for p in (tmp_path / "bizapi").rglob("*") if p.is_file()}
    files.update({p: sha256(tmp_path / p) for p in data_paths})
    c = {"arguments": {"customers": [data_paths[-1]], "bizapi_data": data_paths}, "files": files}
    args = type("Args", (), {"run_id": "합성현재기동"})()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with e2e_run.managed_bizapi(args, c, tmp_path, port) as (url, path):
        args.bizapi_url = url
        record = e2e_run.verify_bizapi_record(path, args, c, tmp_path)
        assert record["data_paths"] == data_paths
        assert record["command"][record["command"].index("-data") + 1::2] == [str((tmp_path / p).resolve()) for p in data_paths]
        live = e2e_run.live_instance(url)
        assert live["nonce"] == record["nonce"] and live["binary_sha256"] == record["binary_sha256"]
        assert live["data_paths"] == [str((tmp_path / p).resolve()) for p in data_paths]
        changed = {**record, "nonce": "다른기동"}
        with pytest.raises(ValueError, match="기동 식별자"):
            e2e_run.verify_live_instance(changed, tmp_path)
        with pytest.raises(ValueError, match="바이너리"):
            e2e_run.verify_live_instance({**record, "binary_sha256": "0" * 64}, tmp_path)
        with pytest.raises(ValueError, match="데이터 해시"):
            e2e_run.verify_live_instance({**record, "data_files": {p: "0" * 64 for p in data_paths}}, tmp_path)
        changed = {**record, "data_paths": list(reversed(data_paths))}
        put(path, changed)
        if data_count > 1:
            with pytest.raises(ValueError, match="순서"):
                e2e_run.verify_bizapi_record(path, args, c, tmp_path)
        put(path, record)
    with pytest.raises(ValueError, match="현재 업무 API 기동"):
        e2e_run.verify_bizapi_record(path, args, c, tmp_path)
    with e2e_run.managed_bizapi(args, c, tmp_path, port) as (url, new_path):
        second = json.loads(new_path.read_text(encoding="utf-8"))
        assert second["nonce"] != record["nonce"]
        with pytest.raises(ValueError, match="기동 식별자"):
            e2e_run.verify_live_instance(record, tmp_path)
