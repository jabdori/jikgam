"""E2의 생성 입력 기록·판정 정의·검색 범위·공개 사본 검증. 유료 호출은 없다."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from engine_fakes import EchoGenerator, NoBizApi, ScriptedDecider, utt
from jikgam.engine.events import read_log
from jikgam.engine.org import load_org
from jikgam.engine.retrieval import ArticleIndex, RetrievalContext, parse_terms
from jikgam.engine.session import Session
from jikgam.v2 import e2e_claims, e2e_logs, e2e_queries, e2e_report, terms_scan


@pytest.mark.parametrize("channel", ["chat", "voice"])
def test_every_generation_attempt_records_full_input_and_final_request_id(tmp_path, channel):
    class RetryGenerator(EchoGenerator):
        async def generate(self, payload, feedback=None):
            result = await super().generate(payload, feedback)
            if len(self.payloads) == 1:
                result.amount = 1234567
            return result

    gen = RetryGenerator()

    async def go():
        s = Session(load_org("org_a"), "입력검사", tmp_path, decision_client=ScriptedDecider(),
                    generator=gen, bizapi=NoBizApi(), channel=channel)
        await s.handle(utt("u1", "청약철회와 환불 기준이 궁금해요"))
        await s.aclose()

    asyncio.run(go())
    es = read_log(tmp_path / "입력검사.jsonl")
    requests = [e for e in es if e["type"] == "generation.request"]
    assert len(requests) == len(gen.payloads) == 2
    assert requests[0]["input"] == gen.payloads[0]
    assert requests[1]["input"] == gen.payloads[1]
    assert requests[0]["feedback"] is None and requests[1]["feedback"]
    assert {"plan", "articles", "quotes", "system_facts", "customer_claims", "conversation"} <= requests[0]["input"].keys()
    created = next(e for e in es if e["type"] == "suggestion.created")
    assert created["generation_request_id"] == requests[-1]["request_id"]


def test_all_turn_query_keeps_expansions_and_default_query_is_unchanged():
    org = load_org("org_a")
    index = ArticleIndex(parse_terms(org.terms), org.retrieval)
    texts = ("청약철회 기준을 알려주세요", "제 번호는 다음과 같아요", "인증했습니다", "네 감사합니다")
    ctx = RetrievalContext(texts, intent="cancel_refund")
    current = index.query_text(ctx)
    assert texts[0] not in current
    all_query = index.query_text(ctx, all_turns=True)
    assert texts[0] in all_query and "청약철회" in all_query
    dummy = SimpleNamespace(cfg=replace(org.retrieval, query_turns=len(texts)))
    assert all_query == ArticleIndex.query_text(dummy, ctx)
    assert current == index.query_text(ctx)


def test_old_generation_logs_are_not_sent_to_paid_judge(tmp_path, monkeypatch):
    (tmp_path / "summary.json").write_text(json.dumps({"run_id": "구정의", "sessions": [{"session": "s"}]}), encoding="utf-8")
    es = [{"type": "suggestion.created", "suggestion_id": "s1", "text": "안내", "cited_articles": []},
          {"type": "suggestion.exposed", "suggestion_id": "s1"}]
    monkeypatch.setattr(e2e_claims, "read_log", lambda _: es)
    monkeypatch.setattr(e2e_claims.GeminiJson, "from_config", lambda *_a, **_kw: pytest.fail("이전 정의에 유료 판정 호출"))
    with pytest.raises(ValueError, match="입력 없음"):
        asyncio.run(e2e_claims.run(tmp_path, 1))
    r = {"scenario": "합성", "valid": True, "pass": True, "claims": 0, "unsupported": 0, "missing": 1,
         "input_missing": 1, "legacy_claims": 10, "legacy_unsupported": 3,
         "items": {i: True for i in range(1, 11)}, "aux": {"precision": 1},
         "metrics": {"refresh": [], "stale": {"by_channel": {}, "events": []}}}
    report = e2e_report.summarize([r], {})
    assert report["unsupported_claim_rate"] is None and report["claim_status"] == "입력 없음(이전 정의)"
    assert report["legacy_cited_articles"]["rate"] == 0.3


def test_common_standard_terms_fragments_are_counted_separately_without_whitespace():
    common = "가나다라마바사아자차카타파하거너더러머버서어저처커터퍼허"
    unique = "고유한표준약관에서만찾을수있는설명문장을충분히길게기록합니다"
    terms = SimpleNamespace(lines=[SimpleNamespace(text=common), SimpleNamespace(text=unique)])
    public = SimpleNamespace(text=" ".join(common))
    a, b = terms_scan.fragment_sets({"game-terms": terms, "법률": public})
    assert common[:25] in b and common[:25] not in a and unique[:25] in a
    c, d = terms_scan.fragment_sets({"game-terms": terms}, org_text="\n".join(common))
    assert common[:25] in d and common[:25] not in c


def test_nested_generation_input_is_redacted_but_original_is_complete_and_public_is_lf(tmp_path):
    folder = tmp_path / "results/새실행"
    folder.mkdir(parents=True)
    (folder / "summary.json").write_bytes(b'{"run_id":"new","ended":"done"}\r\n')
    text = "표준약관에만존재하는충분히긴구절을만들어서입력전체기록을검사합니다"
    value = {"type": "generation.request", "input": {"articles": [{"text": text}], "quotes": {"amount": 100}}}
    path = folder / "세션.jsonl"
    before = (json.dumps(value, ensure_ascii=False) + "\r\n").encode()
    path.write_bytes(before)
    e2e_logs.publish(folder, {text[:25]}, tmp_path)
    public = json.loads(path.read_text(encoding="utf-8"))
    assert e2e_logs.MASK in public["input"]["articles"][0]["text"]
    assert public["input"]["quotes"]["amount"] == 100
    assert e2e_logs.original_path(path, tmp_path).read_bytes() == before
    assert all(b"\r" not in p.read_bytes() for p in folder.iterdir())


def test_query_rules_never_take_scenario_or_gold_input():
    assert e2e_queries.query_text("all-customer", "현재", ["처음", "둘", "셋"]) == "처음 둘 셋"
    assert e2e_queries.query_text("recent-two", "현재", ["처음", "둘", "셋"]) == "둘 셋"
    assert e2e_queries.confirmed_terms({"customer_id": "개인 식별값", "status": "active", "amount": 50}) == {"active"}


def test_offline_query_comparison_only_counts_evidence_events(tmp_path, monkeypatch):
    cfg = load_org("org_a").retrieval
    monkeypatch.setattr(e2e_queries, "ROOT", tmp_path)
    monkeypatch.setattr(e2e_queries, "load_org", lambda _: SimpleNamespace(retrieval=cfg))
    monkeypatch.setattr(e2e_queries, "load_law", lambda profile: profile)
    monkeypatch.setattr(e2e_queries, "bundle", lambda _: SimpleNamespace(cfg=SimpleNamespace(max_candidates=8), search=lambda q: (["근거"], 0)))
    monkeypatch.setattr(e2e_queries, "found", lambda gold, ids: [bool(ids)])
    monkeypatch.setattr(e2e_queries, "load_scenarios", lambda _: [{"id": "개발", "expected": {"law_evidence": [{}]}}])
    events = [{"type": "utterance.end", "text": "첫 질문"},
              {"type": "retrieval.done", "version": 1, "query": "취소됨"},
              {"type": "utterance.end", "text": "정정 질문"},
              {"type": "retrieval.done", "version": 2, "query": "실제 근거 갱신"},
              {"type": "evidence.updated", "version": 2, "intent": None, "candidates": [], "applies": {}}]
    monkeypatch.setattr(e2e_queries, "read_log", lambda p: [
        {**e, "law_profile": p.parent.name, "law_candidates": ["근거"]} if e["type"] == "retrieval.done" else e for e in events])
    dirs = []
    for profile in ("r0", "r4"):
        directory = tmp_path / profile
        directory.mkdir()
        (directory / "summary.json").write_text(json.dumps({"law": profile, "scenario_file": "data/scenarios/org_a_v2.jsonl", "sessions": [{"session": "합성", "scenario": "개발"}]}), encoding="utf-8")
        dirs.append(directory)
    result = e2e_queries.run(dirs, "검사")
    for profile in ("r0", "r4"):
        current = result["profiles"][profile]["current"]
        assert current["events"] == 1 and current["retrieval_events_total"] == 2
        assert current["canceled_before_evidence"] == 1
