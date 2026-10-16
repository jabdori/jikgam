"""v2 검색 구성(docs/evaluation-v2.md §3.4~§3.6). 구간 목록과 질문으로 순위를 낸다.

- BM25는 v1과 같다(jikgam/engine/retrieval.py의 tokens, BM25, k1 1.2, b 0.75).
- R0: v1 검색을 v2 문서에 옮긴 것. BM25 상위 4개에서 참조 조와 관련 별표를 한 단계 따라가 점수 × 0.5로 더하고 후보 8개를
  세운다. 예산을 채우려면 8개 뒤로 BM25 순서를 이어 간다(§3.6).
- R1: (a) 고정 길이 구간 + BM25(구간 정보 없음).
"""

from dataclasses import dataclass, field

from jikgam.engine.retrieval import BM25, tokens
from jikgam.ingest.chunk import Chunk
from jikgam.ingest.corpus import Doc

R0_SEEDS = 4
R0_DECAY = 0.5
R0_CANDIDATES = 8


@dataclass
class ChunkSet:
    name: str
    chunks: list[Chunk]
    docs: dict[str, Doc]
    extra: dict[str, str] = field(default_factory=dict)  # 구간 id → 색인에 더하는 글(구간 정보)

    def __post_init__(self) -> None:
        self.by_id = {c.id: c for c in self.chunks}
        self.bm25 = BM25({c.id: tokens(self.text(c) + ("\n" + self.extra[c.id] if c.id in self.extra else ""))
                          for c in self.chunks})

    def text(self, c: Chunk) -> str:
        return self.docs[c.doc].text[c.start:c.end]


def rank_bm25(cs: ChunkSet, query: str) -> list[tuple[str, float]]:
    s = cs.bm25.scores(tokens(query))
    return sorted(s.items(), key=lambda kv: (-kv[1], kv[0]))


def rank_r0(cs: ChunkSet, query: str) -> list[tuple[str, float]]:
    """v1 검색(ArticleIndex.search)과 같은 규칙. 조 이름은 같은 문서 안에서만 찾는다."""
    bm = dict(rank_bm25(cs, query))
    order = sorted(bm, key=lambda c: (-bm[c], c))
    by_article = {(c.doc, c.article): c.id for c in cs.chunks if c.article}
    tables: dict[tuple[str, str], list[str]] = {}
    for c in cs.chunks:
        if c.table_of:
            tables.setdefault((c.doc, c.table_of), []).append(c.id)
    scored = dict(bm)
    for s in order[:R0_SEEDS]:
        seed = cs.by_id[s]
        if not seed.article:
            continue
        name = seed.article.split("#")[0]
        linked = [by_article[(seed.doc, r)] for r in seed.refs if (seed.doc, r) in by_article]
        linked += tables.get((seed.doc, name), [])
        for cid in linked:
            v = bm[s] * R0_DECAY
            if v > scored.get(cid, 0.0):
                scored[cid] = v
    candidates = sorted(scored, key=lambda c: (-scored[c], c))[:R0_CANDIDATES]
    rest = [c for c in order if c not in candidates]
    return [(c, scored[c]) for c in candidates] + [(c, bm[c]) for c in rest]


RANKERS = {"bm25": rank_bm25, "r0": rank_r0}
