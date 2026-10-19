"""엔드투엔드 사람 확인의 표본·비공개 경계·보고 보류 회귀 검사."""

import copy
import json
from pathlib import Path

import pytest

from jikgam.sim import metrics
from jikgam.v2 import e2e_check as check, e2e_claims, e2e_report


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    scenarios, runs = {}, {}
    for law in ("r0", "r4"):
        run_dir = tmp_path / "results/engine/scenarios" / law
        summary = {"law": law, "run_id": law, "ended": "완료", "scenario_file": "data/scenarios/개발.jsonl"}
        sessions = []
        for i in range(2):
            sid = f"세션{i}"
            scenario = {"id": sid, "expected": {"forbidden": ["본인 확인 전에 환불 접수", "금지 안내"],
                        "must_say": ["안내1", "안내2", "안내3", "안내4"], "action": "answer", "outcome": "기대 결과"}}
            scenarios[sid] = scenario
            events = [{"type": "utterance.end", "speaker": "customer", "text": "질문", "t_ms": 0},
                      {"type": "agent.say", "text": "앞선 안내", "t_ms": 1},
                      {"type": "generation.request", "request_id": "g", "version": 1,
                       "input": {"articles": [{"text": "미인용 조항"}], "quotes": {"금액": 100},
                                 "system_facts": {"상태": "활성"}, "plan": {}, "conversation": ["질문"]}},
                      {"type": "suggestion.created", "suggestion_id": "s", "text": "전체 제안 숨김",
                       "basis_version": 1, "generation_request_id": "g"},
                      {"type": "suggestion.exposed", "suggestion_id": "s"}]
            path = run_dir / f"{sid}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")
            ai = {"error": None, "verdict": {
                "forbidden": [{"index": 1, "violated": False, "reason": "비공개 이유 표식"}],
                "must_say": [{"index": n, "said": n % 2 == 0} for n in range(4)],
                "action_content": {"matches": True}}}
            put(run_dir / f"{sid}.judge.json", ai)
            source = next(e2e_claims.exposed_inputs(events))
            cache = e2e_claims.local_dir(law, tmp_path) / f"{e2e_claims.fingerprint(source, '합성')}.json"
            put(cache, {"error": None, "claims": [{"claim": f"주장{n}", "supported": n % 2 == 0,
                                                  "reason": "비공개 이유 표식"} for n in range(40)]})
            sessions.append({"session": sid, "scenario": sid, "ai": ai,
                             "result": {"compliance": {"valid": i == 0}}})
        summary["sessions"] = [{"session": s["session"]} for s in sessions]
        put(run_dir / "summary.json", summary)
        runs[run_dir] = {"summary": summary, "sessions": sessions}
    put(tmp_path / "data/scenarios/개발.jsonl", {"합성": True})
    monkeypatch.setattr(check, "load_run", lambda p: runs[p])
    monkeypatch.setattr(check, "load_scenarios", lambda _: list(scenarios.values()))
    monkeypatch.setattr(check, "load_config", lambda: {check.answer.JUDGE_MODEL: {"model": "합성"}})
    return tmp_path, list(runs), runs


def build_sample(inputs):
    root, dirs, _ = inputs
    out = check.build(*dirs, "개발", root)
    sample = json.loads((check.directory("개발", root) / "sample.json").read_text(encoding="utf-8"))
    return root, dirs, out, sample


def response(sample):
    return {"name": sample["name"], "sample_digest": sample["sample_digest"], "input_hash": sample["input_hash"],
            "items": {x["id"]: x["model"] for items in sample["items"].values() for x in items}}


def test_build_is_blind_and_only_samples_valid_exposed_full_input(inputs):
    root, dirs, out, sample = build_sample(inputs)
    assert out["claims"] == 30 and out["scenario_items"] == 10
    assert sample["population"] == {"claims": {"r0": 40, "r4": 40}, "scenario_items": {"r0": 6, "r4": 6}}
    assert all(x["session"] == "세션0" for items in sample["items"].values() for x in items)
    page = Path(out["page"]).read_text(encoding="utf-8")
    for hidden in ('"model"', '"supported"', '"violated"', '"matches"', '"said"', "비공개 이유 표식", "전체 제안 숨김"):
        assert hidden not in page
    assert "R0" not in page and "R4" not in page and '"law"' not in page
    assert "미인용 조항" in page and "금액" in page and "활성" in page and "앞선 안내" in page
    assert page.count('class="item"') == 40 and 'id="download" disabled' in page
    assert b"\r\n" not in Path(out["page"]).read_bytes()
    check.build(*dirs, "개발", root)
    assert sample == json.loads((check.directory("개발", root) / "sample.json").read_text(encoding="utf-8"))
    assert not check.public_path("개발", root).exists()


def test_claim_sampling_is_proportional_and_seeded():
    pool = [{"law": law, "id": i} for law, n in (("r0", 75), ("r4", 25)) for i in range(n)]
    chosen = check.sample_claims(pool)
    assert sum(x["law"] == "r0" for x in chosen) == 23
    assert chosen == check.sample_claims(pool)
    assert len({(x["law"], x["id"]) for x in chosen}) == 30
    with pytest.raises(ValueError, match="적습니다"):
        check.sample_claims(pool[:29])


def test_digest_and_input_hash_mismatches_are_rejected(inputs):
    root, _, _, sample = build_sample(inputs)
    check.validate_sample(sample, "개발", root)
    edited = copy.deepcopy(sample)
    edited["items"]["claims"][0]["model"] = not edited["items"]["claims"][0]["model"]
    with pytest.raises(ValueError, match="digest"):
        check.validate_sample(edited, "개발", root)
    result = response(sample)
    result["sample_digest"] = "다름"
    with pytest.raises(ValueError, match="digest"):
        check.agreement(sample, result)
    result = response(sample)
    result["input_hash"] = "다름"
    with pytest.raises(ValueError, match="입력 해시"):
        check.agreement(sample, result)
    (root / "data/scenarios/개발.jsonl").write_text("변경", encoding="utf-8")
    with pytest.raises(ValueError, match="입력 해시"):
        check.validate_sample(sample, "개발", root)


@pytest.mark.parametrize("bad", [None, 1, "yes"])
def test_incomplete_or_non_boolean_answers_are_rejected(inputs, bad):
    _, _, _, sample = build_sample(inputs)
    result = response(sample)
    result["items"][next(iter(result["items"]))] = bad
    with pytest.raises(ValueError, match="모든 표본"):
        check.agreement(sample, result)


def test_apply_persists_only_counts_publicly_and_rechecks_sources(inputs, monkeypatch):
    root, dirs, _, sample = build_sample(inputs)
    result = response(sample)
    for item in sample["items"]["claims"][:7]:
        result["items"][item["id"]] = not item["model"]
    for item in sample["items"]["scenario_items"][:2]:
        result["items"][item["id"]] = not item["model"]
    path = check.directory("개발", root) / "download.json"
    put(path, result)
    row = {"scenario": "합성", "run": 1, "valid": True, "pass": True, "claims": 10,
           "unsupported": 1, "missing": 0, "items": {i: True for i in range(1, 11)},
           "aux": {"precision": 1, "law": {"law_precision": 1, "law_tokens": 10}},
           "metrics": metrics.session_metrics([])}
    monkeypatch.setattr(e2e_report, "collect", lambda *_: ([row], {"total": 0}))
    counts = check.apply(path, "개발", root)
    assert counts["claims"] == {"sampled": 30, "matched": 23, "agreement": 23 / 30}
    assert counts["scenario_items"] == {"sampled": 10, "matched": 8, "agreement": .8}
    assert json.loads(check.public_path("개발", root).read_text(encoding="utf-8")) == counts
    published = json.loads((check.public_path("개발", root).parent / "report.json").read_text(encoding="utf-8"))
    assert published["conditions"]["unsupported_not_higher"] is None
    assert published["conditions"]["pass_noninferior"] is True
    assert published["human_check"]["claims"]["agreement"] == 23 / 30
    table = (check.public_path("개발", root).parent / "report.md").read_text(encoding="utf-8")
    assert "판정 보류" in table and "76.67%" in table
    assert check.load_check("개발", *dirs, root)["status"] == "확인 완료"
    assert check.load_check("개발", *reversed(dirs), root)["status"] != "확인 완료"
    put(check.public_path("개발", root), {"claims": {"agreement": 1}})
    assert check.load_check("개발", *dirs, root)["status"] != "확인 완료"


@pytest.mark.parametrize("claims,items,deferred", [(.8, .8, []), (.79, 1, ["unsupported_not_higher"]),
    (1, .7, ["pass_noninferior", "pass_improved"]),
    (.7, .7, ["unsupported_not_higher", "pass_noninferior", "pass_improved"])])
def test_agreement_threshold_defers_corresponding_report_condition(claims, items, deferred):
    conditions = {k: True for k in ("pass_noninferior", "pass_improved", "unsupported_not_higher", "refresh_p95_le_500")}
    report = e2e_report.reflect_check({"conditions": conditions.copy()}, {"status": "확인 완료",
                                    "claims": {"agreement": claims}, "scenario_items": {"agreement": items}})
    assert report["deferred_conditions"] == deferred
    assert report["automated_conditions"] == conditions
    assert all(report["conditions"][k] is None for k in deferred)
    assert report["conditions"]["refresh_p95_le_500"] is True


def test_stale_check_defers_and_unreviewed_build_does_not_invent_answers(inputs):
    root, dirs, _, _ = build_sample(inputs)
    assert check.load_check("개발", *dirs, root) == {"status": "미확인"}
    put(check.public_path("개발", root), {"claims": {"agreement": 1}})
    stale = check.load_check("개발", *dirs, root)
    assert stale["status"] == "비공개 확인 결과 없음"
    value = e2e_report.reflect_check({"conditions": {}}, stale)
    assert len(value["deferred_conditions"]) == 3


def test_names_cannot_escape_review_or_result_directory(tmp_path):
    for name in ("../외부", "a/b", "a\\b", "", ".", "..", "<script>"):
        with pytest.raises(ValueError):
            check.directory(name, tmp_path)


def test_build_accepts_r5_as_the_second_run_and_still_hides_the_configuration(inputs):
    root, dirs, runs = inputs
    runs[dirs[1]]["summary"]["law"] = "r5"
    put(dirs[1] / "summary.json", {**runs[dirs[1]]["summary"], "sessions": [{"session": "세션0"}, {"session": "세션1"}]})
    out = check.build(*dirs, "개발2", root)
    sample = json.loads((check.directory("개발2", root) / "sample.json").read_text(encoding="utf-8"))
    assert sample["population"] == {"claims": {"r0": 40, "r5": 40}, "scenario_items": {"r0": 6, "r5": 6}}
    page = Path(out["page"]).read_text(encoding="utf-8")
    assert "R5" not in page and "r5" not in page and '"law"' not in page
    runs[dirs[1]]["summary"]["law"] = "r1"
    put(dirs[1] / "summary.json", {**runs[dirs[1]]["summary"], "sessions": []})
    with pytest.raises(ValueError, match="R4 또는 R5"):
        check.build(*dirs, "개발3", root)


def test_page_states_the_claim_criterion_and_counts_only_review_cards(inputs):
    root, dirs, out, _ = build_sample(inputs)
    page = Path(out["page"]).read_text(encoding="utf-8")
    assert "심사 기준" in page and page.count('class="item"') == 40
    for part in ("articles", "quotes", "system_facts", "receipts", "plan", "conversation"):
        assert part in page
    assert "주장이 실제로 맞는지" in page
