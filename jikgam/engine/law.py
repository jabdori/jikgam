"""엔진의 법령·고시 묶음(docs/evaluation-v2.md §4.4, config/v2/law.toml).

조직 A 약관(v1 조항 색인)과 따로, 법령·고시 구간을 찾아 근거 후보에 더한다. 구간은 엔진의 조항 사전에 종류 "law"인
조항으로 들어가므로, 판별(applies)·근거 선택·생성 입력은 v1과 같은 길을 쓴다. 이 묶음은 설정에서 켠 세션에서만 쓴다.

- r0: 조 단위 구간 + v1 검색 규칙(jikgam/v2/search.py rank_r0), 후보 max_candidates개
- r4: (b) 구간 + 구간 정보, 키워드·임베딩 결합(jikgam/v2/hybrid.py). 리랭킹은 엔진의 applies 판별이 한다
- r5: r4 검색 순위에서 조직의 적용 업종(조직 설정 law_industries)과 직전 판단의 상담 주제(law_topic, 확률 topic_threshold
  이상일 때)에 해당하는 구간만 남긴다. 상위 max_candidates개 중 reserved 자리는 거르지 않은 순위로 채운다(docs/v2-law-final.md).
  구간 메타데이터는 labels 파일, 주제 질문은 topic_question_set이다

색인과 임베딩 모델은 프로세스 안에서 한 번만 만든다(세션마다 다시 쓴다).
"""

import json
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jikgam.engine.retrieval import Article

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "v2" / "law.toml"
LAW_TYPE = "law"


@dataclass(frozen=True)
class Guard:
    id: str
    pattern: re.Pattern
    min: int
    message: str


@dataclass(frozen=True)
class LawConfig:
    profile: str
    mode: str  # r0 | r4
    chunks: str
    meta: str = "none"
    embed: str | None = None
    fusion: str = "rrf"
    max_candidates: int = 8
    guards: tuple[Guard, ...] = field(default_factory=tuple)
    question_set: Path | None = None  # 법령 구간의 판별 질문(law_applies)
    query_scope: str = "recent"
    labels: Path | None = None  # r5: 구간 메타데이터(적용 업종, 주제)
    topic_question_set: Path | None = None  # r5: 상담 주제 질문(law_topic)
    reserved: int = 0  # r5: 상위 후보 중 거르지 않은 순위로 채우는 자리 수
    topic_threshold: float = 0.5  # r5: 주제로 거르는 최소 확률


def load_law(profile: str, path: Path = CONFIG) -> LawConfig:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    p = raw[profile]
    guards = tuple(Guard(g["id"], re.compile(g["pattern"]), int(g["min"]), g["message"]) for g in raw.get("guards", []))
    qs = raw.get("question_set")
    return LawConfig(profile, p["mode"], p["chunks"], p.get("meta", "none"), p.get("embed"), p.get("fusion", "rrf"),
                     int(p.get("max_candidates", 8)), guards, ROOT / qs if qs else None, raw.get("query_scope", "recent"),
                     ROOT / p["labels"] if p.get("labels") else None,
                     ROOT / p["topic_question_set"] if p.get("topic_question_set") else None,
                     int(p.get("reserved", 0)), float(p.get("topic_threshold", 0.5)))


def filter_candidates(ranked: list[str], labels: dict[str, dict[str, Any]], industries: tuple[str, ...] | list[str],
                      topic: str | None, reserved: int, k: int) -> tuple[list[str], dict[str, Any]]:
    """r5 거르기. ranked는 지금 검색의 전체 순위다. 업종(있으면)과 주제(있으면)에 해당하는 구간만 남긴 순위의 앞 k−reserved개,
    거르지 않은 순위에서 아직 없는 앞 reserved개, 나머지 거른 순위, 나머지 거르지 않은 순위 차례로 k개를 고른다."""
    if not industries and topic is None:
        return ranked[:k], {"filtered": False, "topic": None, "kept": len(ranked)}
    keep = [c for c in ranked if not industries or set(industries) & set((labels.get(c) or {}).get("적용 업종", ()))]
    if topic is not None:
        keep = [c for c in keep if topic in (labels.get(c) or {}).get("주제", ())]
    head = keep[:k - reserved]
    extra = [c for c in ranked if c not in head][:reserved]
    taken = set(head) | set(extra)
    order = head + extra + [c for c in keep if c not in taken] + [c for c in ranked if c not in taken and c not in set(keep)]
    return order[:k], {"filtered": True, "topic": topic, "kept": len(keep)}


def guard_violations(text: str, guards: list[dict[str, Any]]) -> list[str]:
    """생성 문장에 대한 결정적 검사. guards는 생성 입력에 넣은 형태({pattern, min, message})다."""
    out = []
    for g in guards:
        for m in re.finditer(g["pattern"], text):
            if int(m.group(1)) < int(g["min"]):
                out.append(f"{g['message']}: '{m.group(0)}'")
    return out


class LawBundle:
    def __init__(self, cfg: LawConfig):
        from jikgam.ingest import chunk as chunking
        from jikgam.ingest import metadata
        from jikgam.ingest.corpus import load_docs
        from jikgam.v2.search import ChunkSet

        self.cfg = cfg
        docs = load_docs()
        chunks = chunking.load(cfg.chunks)
        meta = metadata.load(cfg.chunks, cfg.meta) if cfg.meta != "none" else {}
        extra = {cid: metadata.text_for_index(m) for cid, m in meta.items()}
        self.cs = ChunkSet(cfg.chunks, chunks, docs, extra)
        self.articles: dict[str, Article] = {
            c.id: Article(c.id, f"「{docs[c.doc].title}」 {c.start_line}~{c.end_line}줄", LAW_TYPE, (), self.cs.text(c))
            for c in chunks}
        self.labels: dict[str, dict[str, Any]] = {}
        if cfg.labels is not None:
            self.labels = json.loads(cfg.labels.read_text(encoding="utf-8"))["segments"]
        self.topics: dict[str, str] = {}  # 주제 질문의 선택지 키 → 주제 값
        if cfg.topic_question_set is not None:
            self.topics = json.loads(cfg.topic_question_set.read_text(encoding="utf-8"))["topics"]
        self.hybrid = None
        if cfg.mode in ("r4", "r5"):
            from jikgam.v2.embed import EmbeddingIndex
            from jikgam.v2.hybrid import Hybrid, HybridConfig

            emb = None
            if cfg.embed:
                texts = [self.cs.text(c) + ("\n" + extra[c.id] if c.id in extra else "") for c in chunks]
                emb = EmbeddingIndex(cfg.embed, [c.id for c in chunks], texts, [docs[c.doc].title for c in chunks],
                                     f"{cfg.chunks}--{cfg.meta}")
                emb.rank("예열 질의입니다.")
            self.hybrid = Hybrid(self.cs, HybridConfig(cfg.embed, cfg.fusion, False, 0), meta, emb)

    def search(self, query: str) -> tuple[list[str], float]:
        """(법령 후보 id, 걸린 시간 ms)."""
        from jikgam.v2.search import rank_r0

        t0 = time.perf_counter()
        if self.cfg.mode == "r0":
            ranked = [c for c, _ in rank_r0(self.cs, query)]
        else:
            ranked = [c for c, _ in self.hybrid.retrieve(query)[0]]
        return ranked[:self.cfg.max_candidates], (time.perf_counter() - t0) * 1000

    def search_filtered(self, query: str, industries: tuple[str, ...], topic: str | None) -> tuple[list[str], float, dict[str, Any]]:
        """r5: (법령 후보 id, 걸린 시간 ms, 거르기 정보). 검색은 r4와 같다."""
        t0 = time.perf_counter()
        ranked = [c for c, _ in self.hybrid.retrieve(query)[0]]
        ids, info = filter_candidates(ranked, self.labels, industries, topic, self.cfg.reserved, self.cfg.max_candidates)
        return ids, (time.perf_counter() - t0) * 1000, info


_BUNDLES: dict[str, LawBundle] = {}


def bundle(cfg: LawConfig) -> LawBundle:
    if cfg.profile not in _BUNDLES:
        _BUNDLES[cfg.profile] = LawBundle(cfg)
    return _BUNDLES[cfg.profile]
