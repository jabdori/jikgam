"""조직 설정(config/orgs/<org>.toml) 읽기. docs/engine.md §7.

엔진 코드는 조직별 조항 ID, 구어 낱말, 업무 규칙 값을 직접 갖지 않고 이 설정으로만 받는다.
환경 변수 JIKGAM_BIZAPI_URL이 있으면 설정 파일의 bizapi_url 대신 쓴다(여러 실행이 모의 업무 API를 따로 띄울 때).
"""

import os
import re
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ORGS_DIR = ROOT / "config/orgs"

# retrieval.rules의 when에 쓸 수 있는 조건 이름. 의미는 retrieval.ArticleIndex.rule_articles가 정한다.
RULE_CONDITIONS = ("identity_unverified", "intent", "identity_failed", "approval_required", "handoff_reason")


@dataclass(frozen=True)
class ArticleRule:
    when: str
    intents: tuple[str, ...]
    add: tuple[str, ...]


@dataclass(frozen=True)
class RetrievalConfig:
    max_candidates: int
    seed_top_k: int
    query_turns: int
    expansion_decay: float
    k1: float
    b: float
    intent_terms: dict[str, str]
    expansions: tuple[tuple[re.Pattern, str], ...]
    rules: tuple[ArticleRule, ...]


@dataclass(frozen=True)
class PolicyConfig:
    records_intents: frozenset[str]
    refund_intents: frozenset[str]
    cancel_intents: frozenset[str]
    refundable_reasons: frozenset[str]
    cancel_refund_reasons: frozenset[str]
    addon_plans: frozenset[str]
    addon_intents: frozenset[str]  # 부가 상품만 해지할 수 있는 요청
    addon_articles: tuple[str, ...]  # 부가 상품만 해지할 때의 근거 조항(환불 없음, 종료 시점)
    third_party_articles: tuple[str, ...]  # 제3자 요청을 거절할 때의 근거 조항
    guardian_reasons: tuple[str, ...]  # 법정대리인이 요청할 수 있는 견적 사유
    guardian_articles: tuple[str, ...]  # 법정대리인 요청의 근거 조항
    minor_age: int
    quote_priority: tuple[str, ...]
    quote_reasons: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class OrgConfig:
    org: str
    terms: Path
    today: date
    bizapi_url: str
    decision_backend: str
    question_set: Path
    extra_question_set: Path
    applies_threshold: float
    agreement_threshold: float
    decline_threshold: float
    caller_threshold: float
    owner_return_threshold: float
    addon_threshold: float
    identity_unavailable_threshold: float
    generator: str
    required_notices: tuple[str, ...]
    retrieval: RetrievalConfig
    policy: PolicyConfig


def _retrieval(raw: dict[str, Any]) -> RetrievalConfig:
    rules = []
    for r in raw.get("rules", []):
        if r["when"] not in RULE_CONDITIONS:
            raise ValueError(f"retrieval.rules.when은 {RULE_CONDITIONS} 중 하나여야 한다: {r['when']!r}")
        rules.append(ArticleRule(r["when"], tuple(r.get("intents", ())), tuple(r["add"])))
    return RetrievalConfig(
        max_candidates=int(raw["max_candidates"]), seed_top_k=int(raw["seed_top_k"]),
        query_turns=int(raw["query_turns"]), expansion_decay=float(raw["expansion_decay"]),
        k1=float(raw.get("k1", 1.2)), b=float(raw.get("b", 0.75)),
        intent_terms=dict(raw.get("intent_terms", {})),
        expansions=tuple((re.compile(e["pattern"]), e["terms"]) for e in raw.get("expansions", [])),
        rules=tuple(rules),
    )


def _policy(raw: dict[str, Any]) -> PolicyConfig:
    priority = tuple(raw["quote_priority"])
    reasons = {k: tuple(v) for k, v in raw.get("quote_reasons", {}).items()}
    guardian = tuple(raw.get("guardian_reasons", ()))
    unknown = ({r for rs in reasons.values() for r in rs} | set(guardian)) - set(priority)
    if unknown:
        raise ValueError(f"quote_priority에 없는 견적 사유: {sorted(unknown)}")
    return PolicyConfig(
        records_intents=frozenset(raw["records_intents"]), refund_intents=frozenset(raw["refund_intents"]),
        cancel_intents=frozenset(raw["cancel_intents"]), refundable_reasons=frozenset(raw["refundable_reasons"]),
        cancel_refund_reasons=frozenset(raw["cancel_refund_reasons"]),
        addon_plans=frozenset(raw.get("addon_plans", ())), addon_intents=frozenset(raw.get("addon_intents", ())),
        addon_articles=tuple(raw.get("addon_articles", ())),
        third_party_articles=tuple(raw.get("third_party_articles", ())), guardian_reasons=guardian,
        guardian_articles=tuple(raw.get("guardian_articles", ())), minor_age=int(raw["minor_age"]),
        quote_priority=priority, quote_reasons=reasons,
    )


def load_org(name_or_path: str | Path) -> OrgConfig:
    """조직 이름(org_a) 또는 설정 파일 경로로 설정을 읽는다. 상대 경로는 저장소 루트 기준이다."""
    path = Path(name_or_path)
    if path.suffix != ".toml":
        path = ORGS_DIR / f"{name_or_path}.toml"
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return OrgConfig(
        org=raw["org"], terms=ROOT / raw["terms"], today=date.fromisoformat(raw["today"]),
        bizapi_url=(os.environ.get("JIKGAM_BIZAPI_URL") or raw["bizapi_url"]).rstrip("/"),
        decision_backend=raw["decision_backend"],
        question_set=ROOT / raw["question_set"], extra_question_set=ROOT / raw["extra_question_set"],
        applies_threshold=float(raw["applies_threshold"]), agreement_threshold=float(raw["agreement_threshold"]),
        decline_threshold=float(raw["decline_threshold"]), caller_threshold=float(raw["caller_threshold"]),
        owner_return_threshold=float(raw["owner_return_threshold"]), addon_threshold=float(raw["addon_threshold"]),
        identity_unavailable_threshold=float(raw["identity_unavailable_threshold"]),
        generator=raw["generator"], required_notices=tuple(raw["required_notices"]),
        retrieval=_retrieval(raw["retrieval"]), policy=_policy(raw["policy"]),
    )
