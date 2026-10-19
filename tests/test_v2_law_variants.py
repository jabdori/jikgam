"""법령 검색 변형의 오프라인 비교(jikgam/v2/law_variants.py): 범위 제한과 판별 결과 검색어."""

from types import SimpleNamespace

from jikgam.v2.law_variants import decision_query, fact_terms, in_scope


def test_scope_keeps_order_and_reads_the_document_from_chunk_metadata():
    chunks = {"x1": "law-a", "x2": "terms-g", "x3": "law-b", "x4": "law-a"}
    law = SimpleNamespace(cs=SimpleNamespace(by_id={k: SimpleNamespace(doc=v) for k, v in chunks.items()}))
    assert in_scope(law, ["x2", "x4", "x1", "x3"], frozenset({"law-a", "law-b"})) == ["x4", "x1", "x3"]


def test_decision_query_uses_request_words_and_confirmed_categories_without_names():
    data = {"customer": {"name": "가상 고객", "status": "active"},
            "subscriptions": [{"plan_type": "monthly", "id": "SUB-1", "price": 9900}],
            "payments": [{"method": "card", "amount": 9900}]}
    facts = fact_terms(data)
    assert facts == {"active", "monthly", "card"}  # 이름, id, 금액은 쓰지 않는다
    assert decision_query("해지 환불 결제", facts) == "해지 환불 결제 active card monthly"
    assert decision_query("", set()) == ""  # 판별 결과가 아직 없으면 빈 검색어(실행에서는 a로 대신한다)
