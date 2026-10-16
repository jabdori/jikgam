"""v2 새 방식의 검색 흐름(docs/evaluation-v2.md §3.4~§3.6): R2, R3, R4, R4c.

1. 키워드: BM25(v1과 같다). 원문과 구간 정보를 함께 색인한다.
2. 임베딩: 고른 임베딩으로 원문과 구간 정보를 함께 색인한다.
3. 결합: 순위 결합(RRF, k = 60) 또는 정규화한 점수의 가중합(키워드 가중 w).
4. 관련 내용 따라가기: 결합 상위 4개 구간의 구간 정보 "관련" 값마다 같은 문서에서 BM25로 가장 가까운 구간 하나를
   후보에 더한다. 점수는 v1 구조 확장과 같이 × 0.5다. 한 단계만 따라간다.
5. 리랭킹: 상위 N개를 Jev가 "이 구간이 질문에 답하는 근거를 담고 있는가"로 판단해 확률 순으로 다시 세운다.
   확률이 같으면 결합 순서를 따른다. N 뒤로는 결합 순서를 이어 간다. 실패하면 결합 순서를 그대로 쓴다(§4.5).

검색 단계 시간(1~4)과 리랭킹 호출 시간(5)을 따로 잰다(§4.2). 생성형 LLM은 부르지 않는다(§3.4 질문 분석).
"""

import time
from dataclasses import dataclass, field

from jikgam.engine.retrieval import BM25, tokens
from jikgam.v2.search import ChunkSet, rank_bm25

RRF_K = 60
RELATED_SEEDS = 4
RELATED_DECAY = 0.5


@dataclass
class HybridConfig:
    embed: str | None = None  # kure1 | snow | kure2 | gemini | None(BM25만)
    fusion: str = "rrf"  # rrf | w0.3 | w0.5 | w0.7
    related: bool = False
    rerank: int = 0  # 0이면 리랭킹하지 않는다

    @property
    def name(self) -> str:
        parts = [self.embed or "bm25"]
        if self.embed:
            parts.append(self.fusion)
        if self.related:
            parts.append("rel")
        if self.rerank:
            parts.append(f"jev{self.rerank}")
        return "+".join(parts)


@dataclass
class Trace:
    retrieval_ms: float = 0.0
    rerank_ms: float | None = None
    rerank_tokens: int = 0
    rerank_cost_usd: float = 0.0
    rerank_error: str | None = None
    related_added: list[str] = field(default_factory=list)


def fuse(bm: list[tuple[str, float]], em: list[tuple[str, float]], fusion: str) -> dict[str, float]:
    if fusion == "rrf":
        out: dict[str, float] = {}
        for ranked in (bm, em):
            for r, (cid, _) in enumerate(ranked):
                out[cid] = out.get(cid, 0.0) + 1.0 / (RRF_K + r + 1)
        return out
    w = float(fusion[1:])

    def norm(ranked):
        if not ranked:
            return {}
        vals = [s for _, s in ranked]
        lo, hi = min(vals), max(vals)
        return {c: (s - lo) / (hi - lo) if hi > lo else 0.0 for c, s in ranked}

    nb, ne = norm(bm), norm(em)
    return {c: w * nb.get(c, 0.0) + (1 - w) * ne.get(c, 0.0) for c in set(nb) | set(ne)}


class Hybrid:
    def __init__(self, cs: ChunkSet, cfg: HybridConfig, meta: dict[str, dict] | None = None, emb=None, jev=None):
        self.cs, self.cfg, self.meta, self.emb, self.jev = cs, cfg, meta or {}, emb, jev
        # 관련 내용 따라가기: 문서마다 따로 BM25(원문만)
        self.doc_bm25: dict[str, BM25] = {}
        if cfg.related:
            by_doc: dict[str, dict[str, list[str]]] = {}
            for c in cs.chunks:
                by_doc.setdefault(c.doc, {})[c.id] = tokens(cs.text(c))
            self.doc_bm25 = {d: BM25(toks) for d, toks in by_doc.items()}

    def retrieve(self, query: str) -> tuple[list[tuple[str, float]], Trace]:
        tr = Trace()
        t0 = time.perf_counter()
        bm = rank_bm25(self.cs, query)
        if self.emb is not None:
            em, _ = self.emb.rank(query)
            scored = fuse(bm, em, self.cfg.fusion)
        else:
            scored = dict(bm)
        order = sorted(scored, key=lambda c: (-scored[c], c))
        if self.cfg.related:
            for s in order[:RELATED_SEEDS]:
                for rel in (self.meta.get(s) or {}).get("related") or []:
                    hits = self.doc_bm25[self.cs.by_id[s].doc].scores(tokens(rel))
                    best = max((c for c in hits if c != s), key=lambda c: (hits[c], c), default=None)
                    if best is None:
                        continue
                    v = scored[s] * RELATED_DECAY
                    if v > scored.get(best, 0.0):
                        scored[best] = v
                        tr.related_added.append(best)
            order = sorted(scored, key=lambda c: (-scored[c], c))
        tr.retrieval_ms = (time.perf_counter() - t0) * 1000
        return [(c, scored[c]) for c in order], tr

    async def rerank(self, query: str, ranked: list[tuple[str, float]], tr: Trace) -> list[tuple[str, float]]:
        n = self.cfg.rerank
        head = [c for c, _ in ranked[:n]]
        state = {"task": "질문에 답할 근거를 찾는다. 후보 구간마다 그 구간이 질문에 답하는 근거를 담고 있는지 판단한다.",
                 "question": query,
                 "candidates": [{"id": f"c{i + 1}", "document": self.cs.docs[self.cs.by_id[c].doc].title,
                                 "text": self.cs.text(self.cs.by_id[c])} for i, c in enumerate(head)]}
        questions = {f"c{i + 1}": {"type": "noul", "instructions": f"후보 c{i + 1}은 질문에 답하는 근거를 담고 있는가?"}
                     for i in range(len(head))}
        r = await self.jev.decide(state, questions)
        tr.rerank_ms, tr.rerank_tokens, tr.rerank_cost_usd, tr.rerank_error = r.latency_ms, r.input_tokens, r.cost_usd, r.error
        if r.error and not r.answers:
            return ranked
        probs = {c: float((r.answers.get(f"c{i + 1}") or {}).get("noul", 0.0)) for i, c in enumerate(head)}
        pos = {c: i for i, c in enumerate(head)}
        new_head = sorted(head, key=lambda c: (-probs[c], pos[c]))
        return [(c, probs[c]) for c in new_head] + ranked[n:]

    async def rank(self, query: str) -> tuple[list[tuple[str, float]], Trace]:
        ranked, tr = self.retrieve(query)
        if self.cfg.rerank and self.jev is not None:
            ranked = await self.rerank(query, ranked, tr)
        return ranked, tr
