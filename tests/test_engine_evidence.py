"""응답의 근거 조항(cited_articles) = 그 응답을 만들 때 엔진이 실제로 쓴 근거 집합(docs/engine.md §12).

정책 규칙의 조항, 상담 상태로 붙인 절차 조항, 판별이 고른 조항, 업무 API가 돌려준 근거 조항을 생성 입력에 넣고, 제안의
cited_articles로 남긴다. 생성기가 문장의 근거로 든 조항은 generator_cited로 따로 남는다. 정책 A의 같은 원칙은
tests/test_rag.py에서 본다.
"""

import asyncio

from engine_fakes import EchoGenerator, NoBizApi, ScriptedDecider, utt

from jikgam.decision.types import DecisionResult
from jikgam.engine import events as ev
from jikgam.engine.bizapi import BizApiClient
from jikgam.engine.generate import GenerationResult
from jikgam.engine.org import load_org
from jikgam.engine.session import Session

ORG = load_org("org_a")
SOURCES = ("policy", "rule", "decision", "system")


class LowAppliesDecider(ScriptedDecider):
    """모든 후보 조항의 applies를 임계값 아래로 답한다(판별이 고른 조항이 없는 경우)."""

    async def decide(self, state, questions):
        res = await super().decide(state, questions)
        answers = {q: ({"type": "noul", "noul": 0.1} if q.startswith("applies:") else a) for q, a in res.answers.items()}
        return DecisionResult(res.backend, res.model, answers, latency_ms=res.latency_ms)


class SilentGenerator(EchoGenerator):
    """문장은 내지만 근거 조항을 하나도 들지 않는 생성기(입력에 없는 ID 하나만 든다: 검사에서 빠진다)."""

    async def generate(self, payload, feedback=None):
        r = await super().generate(payload, feedback)
        r.cited_articles = ["A-99"]
        return r


class FailingGenerator(EchoGenerator):
    async def generate(self, payload, feedback=None):
        self.payloads.append(payload)
        return GenerationResult("", [], None, False, "fake-gen", error="HTTP 503: 일시 장애")


def types(events, *wanted):
    return [e for e in events if e["type"] in wanted]


def last_retrieval_before(log, suggestion_id):
    """그 제안을 만든 마지막 패스의 검색 결과."""
    idx = next(i for i, e in enumerate(log) if e["type"] == ev.SUGGESTION_CREATED and e["suggestion_id"] == suggestion_id)
    return [e for e in log[:idx] if e["type"] == ev.RETRIEVAL_DONE][-1]


def test_cited_articles_are_the_evidence_given_to_the_generator(bizapi, tmp_path):
    """판별이 아무 조항도 고르지 않고 생성기도 조항을 들지 않아도, 상태로 붙인 절차 조항과 업무 API의 근거 조항이
    근거 집합이 된다(s01 흐름). 검색 점수로만 들어온 후보는 판별이 고르지 않으면 근거가 아니다."""
    dec = LowAppliesDecider(intents=[("취소하고 환불", "cancel_refund")])
    gen = SilentGenerator()

    async def go():
        s = Session(ORG, "t-evid", tmp_path, decision_client=dec, generator=gen, bizapi=BizApiClient(bizapi, "t-evid"))
        for i, text in enumerate(["지난주에 결제됐는데 안 쓸 거라서 취소하고 환불받고 싶어요.", "010-0000-0001이에요.",
                                  "인증번호는 공공공일이요.", "네, 진행해 주세요."], 1):
            await s.handle(utt(f"u{i}", text))
        await s.aclose()

    asyncio.run(go())
    log = ev.read_log(tmp_path / "t-evid.jsonl")
    created = types(log, ev.SUGGESTION_CREATED)
    says = types(log, ev.AGENT_SAY)
    requests = types(log, ev.GENERATION_REQUEST)
    assert [c["rule"] for c in created] == ["identity_required", "identity_required", "proposal", "received"]
    for c, say, req in zip(created, says, requests):
        assert c["generator_cited"] == [] and say["generator_cited"] == []  # 입력에 없는 A-99는 검사에서 빠졌다
        assert c["cited_articles"] == say["cited_articles"] == req["articles"]  # 생성기가 본 조항 = 근거 집합
        assert set(c["evidence_sources"]) <= {"rule", "system"}  # 정책 조항 없는 규칙, 판별이 고른 조항 없음
        retrieval = last_retrieval_before(log, c["suggestion_id"])
        rule = [a for a, src in retrieval["sources"].items() if src == "rule"]
        assert c["evidence_sources"]["rule"] == rule
        assert not {a for a, src in retrieval["sources"].items() if src not in ("rule", "system")} & set(c["cited_articles"])
    # 첫 답: 본인 확인 전 절차 조항(retrieval.rules identity_unverified)
    assert created[0]["cited_articles"] == ["A-15"]
    # 요청이 정해진 뒤: 환불 방법·안내 의무 조항(intent 규칙)
    assert {"A-15", "A-14", "A-20"} <= set(created[1]["cited_articles"])
    # 견적 뒤: 업무 API가 판단 근거로 돌려준 조항
    quote_ids = next(e["data"]["article_ids"] for e in log if e["type"] == ev.TOOL_RESULT and e["tool"] == "quote_refund")
    assert quote_ids and set(quote_ids) <= set(created[2]["cited_articles"])
    assert set(created[2]["evidence_sources"]["system"]) | set(created[2]["evidence_sources"]["rule"]) >= set(quote_ids)


def test_decision_selected_and_policy_articles_are_evidence_even_if_not_cited(tmp_path):
    """판별이 고른 조항(decision)과 정책 규칙의 조항(policy, 제3자 거절의 제15조)은 생성기가 들지 않아도 근거 집합이다.
    출처가 겹치면 앞 출처(policy → rule → decision → system)에만 남는다."""
    dec = ScriptedDecider(intents=[("해지", "cancel")], callers=[("아내", "third_party")])

    async def go():
        s = Session(ORG, "t-evid-tp", tmp_path, decision_client=dec, generator=SilentGenerator(), bizapi=NoBizApi())
        await s.handle(utt("u1", "아내 대신 전화했는데요, 아내 구독 해지해 주세요."))
        await s.aclose()

    asyncio.run(go())
    log = ev.read_log(tmp_path / "t-evid-tp.jsonl")
    c = types(log, ev.SUGGESTION_CREATED)[0]
    assert c["rule"] == "third_party"
    assert c["evidence_sources"]["policy"] == ["A-15"]
    selected = types(log, ev.EVIDENCE_UPDATED)[-1]["articles"]
    assert selected and set(selected) <= set(c["cited_articles"])  # applies 0.8로 고른 후보 모두
    flat = [a for k in SOURCES for a in c["evidence_sources"].get(k, [])]
    assert flat == c["cited_articles"] and len(flat) == len(set(flat))
    assert c["generator_cited"] == []


def test_fallback_answer_has_no_evidence(tmp_path):
    """생성이 실패해 고정 문구로 대신한 답은 근거 조항으로 만든 문장이 아니므로 근거 집합을 비운다(정책 A와 같다)."""
    gen = FailingGenerator()

    async def go():
        s = Session(ORG, "t-evid-fb", tmp_path, decision_client=ScriptedDecider(intents=[("궁금", "other")]),
                    generator=gen, bizapi=NoBizApi())
        await s.handle(utt("u1", "가입 전에 환불 규정이 궁금해요"))
        await s.aclose()

    asyncio.run(go())
    log = ev.read_log(tmp_path / "t-evid-fb.jsonl")
    c = types(log, ev.SUGGESTION_CREATED)[0]
    assert c["fallback"] and c["cited_articles"] == [] and c["evidence_sources"] == {}
    assert types(log, ev.AGENT_SAY)[0]["cited_articles"] == []
    assert gen.payloads and gen.payloads[0]["articles"]  # 생성 입력에는 근거 집합이 들어갔다
