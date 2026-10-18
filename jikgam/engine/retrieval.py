"""조항 색인, 키워드 검색(BM25, 한국어 글자 바이그램), 구조 확장. docs/engine.md §4 3단계, docs/design.md §5.5.

1. 질의: 최근 고객 발화 + 현재 요청(직전 판단의 intent를 조직 설정의 낱말로 바꾼 것) + 구어→약관 낱말 확장(조직 설정).
2. BM25 상위 조항을 출발점으로 구조를 붙인다. 붙은 조항의 점수는 출발 조항 점수 × expansion_decay다.
   - 출발 조항이 참조하는 조항(refs 한 단계)
   - 출발 조항을 고치는 예외·부칙·별표(그 조항을 refs에 둔 exception/addendum/table)
   예외·부칙·별표에 더 높은 점수(0.9)를 줘 봤으나 retrieval_eval의 train·val 재현율이 모두 낮아져 참조와 같은 값을 쓴다.
3. 절차 조항은 검색 점수가 아니라 상담 상태로 붙인다(조직 설정의 retrieval.rules). 업무 API가 판단 근거로 돌려준
   조항(견적의 article_ids)도 함께 붙인다.
4. 상태로 붙인 조항을 먼저 두고, 나머지는 점수 순으로 채워 max_candidates개까지 낸다.

임베딩 검색은 아직 없다. 새 의존성 없이 시작하기 위해 키워드 검색과 낱말 확장만 쓴다.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .org import RetrievalConfig

_HEAD = re.compile(r"^## \[(\S+)\] (.+)$")
_META = re.compile(r"^<!-- type: (\w+); refs:(.*)-->$")
MODIFIER_TYPES = ("exception", "addendum", "table")


@dataclass(frozen=True)
class Article:
    id: str
    title: str
    type: str
    refs: tuple[str, ...]
    text: str


def parse_terms(path: Path) -> dict[str, Article]:
    """terms.md에서 조항 ID → Article. scripts/build_dataset.py의 parse_terms와 같은 규칙으로 읽는다
    (같은 결과인지는 tests/test_engine_retrieval.py가 확인한다)."""
    articles: dict[str, Article] = {}
    cur: dict | None = None
    body: list[str] = []

    def flush() -> None:
        if cur:
            articles[cur["id"]] = Article(cur["id"], cur["title"], cur.get("type", "general"),
                                          tuple(cur.get("refs", ())), "\n".join(body).strip())

    for line in path.read_text(encoding="utf-8").splitlines():
        head = _HEAD.match(line)
        if head:
            flush()
            cur, body = {"id": head.group(1), "title": head.group(2).strip()}, []
            continue
        if cur is None:
            continue
        meta = _META.match(line.strip())
        if meta:
            cur["type"] = meta.group(1)
            cur["refs"] = [r.strip() for r in meta.group(2).split(",") if r.strip()]
            continue
        if line.strip() == "---":
            flush()
            cur = None
            continue
        body.append(line)
    flush()
    return articles


def tokens(text: str) -> list[str]:
    """한국어 글자 바이그램. 띄어쓰기가 흔들려도("해지하고"/"해지 하고") 겹치는 토큰이 남도록 낱말 안에서 자른다.
    한 글자 낱말은 그대로 쓴다. 숫자와 영문은 낱말 단위로 둔다."""
    out: list[str] = []
    for word in re.findall(r"[가-힣]+|[a-z0-9]+", text.lower().replace(",", "")):
        if not re.match(r"[가-힣]", word) or len(word) == 1:
            out.append(word)
        else:
            out.extend(word[i:i + 2] for i in range(len(word) - 1))
    return out


class BM25:
    def __init__(self, docs: dict[str, list[str]], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = {d: Counter(toks) for d, toks in docs.items()}
        self.len = {d: len(toks) for d, toks in docs.items()}
        self.avg = sum(self.len.values()) / max(len(docs), 1)
        df = Counter(t for toks in docs.values() for t in set(toks))
        n = len(docs)
        self.idf = {t: math.log((n - c + 0.5) / (c + 0.5) + 1) for t, c in df.items()}

    def scores(self, query: list[str]) -> dict[str, float]:
        q = Counter(query)
        out = {}
        for d, tf in self.tf.items():
            norm = self.k1 * (1 - self.b + self.b * self.len[d] / self.avg)
            s = sum(self.idf.get(t, 0.0) * qn * tf[t] * (self.k1 + 1) / (tf[t] + norm) for t, qn in q.items() if t in tf)
            if s > 0:
                out[d] = s
        return out


@dataclass(frozen=True)
class RetrievalContext:
    customer_texts: tuple[str, ...]  # 최근 고객 발화(오래된 것부터)
    intent: str | None = None  # 현재 요청(직전 판단의 intent)
    request_text: str | None = None  # 요청이 intent 라벨이 아닌 글로만 있을 때(평가 데이터)
    identity_verified: bool = False
    identity_failed: bool = False
    approval_required: bool = False
    handoff_reason: bool = False
    system_article_ids: tuple[str, ...] = ()  # 업무 API가 근거로 돌려준 조항


@dataclass
class RetrievalResult:
    candidates: list[str]
    query: str
    sources: dict[str, str] = field(default_factory=dict)  # 조항 → 붙은 이유(rule, system, bm25, ref:A-05, modifies:A-08)
    scores: dict[str, float] = field(default_factory=dict)


class ArticleIndex:
    def __init__(self, articles: dict[str, Article], cfg: RetrievalConfig):
        self.articles = articles
        self.cfg = cfg
        self.bm25 = BM25({a.id: tokens(f"{a.title}\n{a.text}") for a in articles.values()}, cfg.k1, cfg.b)
        # 조항 → 그 조항을 고치는 예외·부칙·별표
        self.modifiers: dict[str, list[str]] = {a: [] for a in articles}
        for a in articles.values():
            if a.type in MODIFIER_TYPES:
                for r in a.refs:
                    if r in self.modifiers:
                        self.modifiers[r].append(a.id)

    def query_text(self, ctx: RetrievalContext, *, all_turns: bool = False) -> str:
        recent = list(ctx.customer_texts) if all_turns else list(ctx.customer_texts)[-self.cfg.query_turns:]
        parts = list(recent)
        if ctx.intent and ctx.intent in self.cfg.intent_terms:
            parts.append(self.cfg.intent_terms[ctx.intent])
        if ctx.request_text:
            parts.append(ctx.request_text)
        joined = " ".join(parts)
        parts += [terms for pat, terms in self.cfg.expansions if pat.search(joined)]
        return " ".join(parts)

    def rule_articles(self, ctx: RetrievalContext) -> list[str]:
        """상담 상태로 붙이는 절차 조항(조직 설정 retrieval.rules)."""
        out: list[str] = []
        for rule in self.cfg.rules:
            if rule.when == "identity_unverified":
                hit = not ctx.identity_verified and (ctx.intent is None or not rule.intents or ctx.intent in rule.intents)
            elif rule.when == "intent":
                hit = ctx.intent is not None and ctx.intent in rule.intents
            elif rule.when == "identity_failed":
                hit = ctx.identity_failed
            elif rule.when == "approval_required":
                hit = ctx.approval_required
            else:  # handoff_reason
                hit = ctx.handoff_reason
            if hit:
                out += [a for a in rule.add if a in self.articles and a not in out]
        return out

    def search(self, ctx: RetrievalContext) -> RetrievalResult:
        query = self.query_text(ctx)
        bm = self.bm25.scores(tokens(query))
        seeds = sorted(bm, key=lambda a: (-bm[a], a))[: self.cfg.seed_top_k]
        scored: dict[str, float] = {}
        sources: dict[str, str] = {}

        def put(aid: str, score: float, why: str) -> None:
            if aid in self.articles and score > scored.get(aid, 0.0):
                scored[aid] = score
                sources[aid] = why

        for a in sorted(bm, key=lambda x: -bm[x]):
            put(a, bm[a], "bm25")
        for s in seeds:
            for r in self.articles[s].refs:
                put(r, bm[s] * self.cfg.expansion_decay, f"ref:{s}")
            for m in self.modifiers[s]:
                put(m, bm[s] * self.cfg.expansion_decay, f"modifies:{s}")

        forced: list[str] = []
        for a in ctx.system_article_ids:
            if a in self.articles and a not in forced:
                forced.append(a)
                sources[a] = "system"
        for a in self.rule_articles(ctx):
            if a not in forced:
                forced.append(a)
                sources[a] = "rule"
        ranked = [a for a in sorted(scored, key=lambda x: (-scored[x], x)) if a not in forced]
        candidates = (forced + ranked)[: self.cfg.max_candidates]
        return RetrievalResult(candidates, query, {a: sources[a] for a in candidates},
                               {a: round(scored.get(a, 0.0), 3) for a in candidates})
