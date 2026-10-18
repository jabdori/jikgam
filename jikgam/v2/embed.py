"""v2 임베딩 검색(docs/evaluation-v2.md §3.4, §3.4 보완). 네 후보를 같은 형태로 다룬다.

- kure1: nlpai-lab/KURE-v1(단일 벡터 1,024차원, sentence-transformers)
- snow: dragonkue/snowflake-arctic-embed-l-v2.0-ko(단일 벡터 1,024차원, 질의에 "query" 프롬프트)
- kure2: nlpai-lab/KURE-v2(토큰마다 128차원, PyLate ColBERT, MaxSim). 압축 색인 없이 GPU에서 정확히 계산한다
- gemini: gemini-embedding-2(API, 3,072차원). 질의는 "task: search result | query: …", 문서는 "title: … | text: …"

로컬 모델은 고정한 Hugging Face 커밋을 쓰고 GPU에서 16비트로 돈다(uv sync --group embed).
문서 벡터는 data/v2/local/emb/에 저장해 다시 쓴다(원문에서 다시 만들 수 있고 커서 저장소에 넣지 않는다).
"""

import hashlib
import time

from jikgam.ingest.corpus import ROOT

REVISIONS = {
    "kure1": ("nlpai-lab/KURE-v1", "8b418a58414668e75532ed045c22d9ca018ae2b2"),
    "snow": ("dragonkue/snowflake-arctic-embed-l-v2.0-ko", "55ec6e9358a56d56af759bc8372e970caf8c305f"),
    "kure2": ("nlpai-lab/KURE-v2", "3431f86d399d666083890dbb882aced6708873bc"),
    "gemini": ("gemini-embedding-2", None),
}
CACHE = ROOT / "data" / "v2" / "local" / "emb"
GEMINI_DIM = 3072
GEMINI_PRICE_PER_MTOK = 0.20  # 공개 가격(2026-10-16 확인), §9
BATCH = 32


def digest(texts: list[str]) -> str:
    h = hashlib.sha256()
    for t in texts:
        h.update(t.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def cache_path(key, texts, tag):
    return CACHE / key / f"{tag}-{digest(texts)}.pt"


def corpus_inputs(chunks_name, variant):
    """검색 실행과 사전 준비가 같은 말뭉치 캐시 키를 만들도록 입력을 공유한다."""
    from jikgam.ingest import chunk as chunking, metadata
    from jikgam.ingest.corpus import load_docs
    from jikgam.v2.search import ChunkSet

    docs, chunks = load_docs(), chunking.load(chunks_name)
    meta = metadata.load(chunks_name, variant) if variant != "none" else {}
    if variant != "none" and any(c.id not in meta for c in chunks):
        raise ValueError("구간 정보가 빠졌습니다")
    extra = {cid: metadata.text_for_index(m) for cid, m in meta.items()}
    cs = ChunkSet(chunks_name, chunks, docs, extra)
    return ([c.id for c in chunks], [cs.text(c) + ("\n" + extra[c.id] if c.id in extra else "") for c in chunks],
            [docs[c.doc].title for c in chunks])


class Encoder:
    """문서 벡터를 만들고, 질의 하나에 대한 문서 점수를 낸다."""

    def __init__(self, key: str):
        self.key = key
        self.model = None
        self.http = None
        self.cost_usd = 0.0
        self.requests = self.retries = 0

    # ---------- 불러오기 ----------
    def _load(self):
        if self.model is not None or self.http is not None:
            return
        import torch

        name, rev = REVISIONS[self.key]
        if self.key in ("kure1", "snow"):
            from sentence_transformers import SentenceTransformer

            self.model = SentenceTransformer(name, revision=rev, device="cuda", model_kwargs={"torch_dtype": torch.float16})
        elif self.key == "kure2":
            from pylate import models

            self.model = models.ColBERT(model_name_or_path=name, revision=rev, device="cuda")
            self.model.half()
        else:
            import httpx

            from jikgam.decision.registry import secrets

            self.http = httpx.Client(timeout=120.0, headers={"x-goog-api-key": secrets()["GCP_API_KEY"]})

    # ---------- 문서 ----------
    def encode_docs(self, texts: list[str], titles: list[str]):
        """문서 벡터. 단일 벡터는 (문서 수, 차원) 텐서, kure2는 (토큰 벡터 행렬, 토큰별 문서 번호)."""
        import torch

        self._load()
        if self.key in ("kure1", "snow"):
            return self.model.encode(texts, batch_size=BATCH, convert_to_tensor=True, normalize_embeddings=True).half()
        if self.key == "kure2":
            embs = self.model.encode(texts, is_query=False, batch_size=BATCH, convert_to_tensor=False)
            lens = [len(e) for e in embs]
            mat = torch.cat([torch.as_tensor(e) for e in embs]).to("cuda", torch.float16)
            owner = torch.repeat_interleave(torch.arange(len(embs), device="cuda"), torch.tensor(lens, device="cuda"))
            return mat, owner
        vecs = []
        for i in range(0, len(texts), 100):
            reqs = [{"model": "models/gemini-embedding-2", "output_dimensionality": GEMINI_DIM,
                     "content": {"parts": [{"text": f"title: {t or 'none'} | text: {x}"}]}}
                    for x, t in zip(texts[i:i + 100], titles[i:i + 100])]
            vecs += self._gemini_batch(reqs)
        return torch.tensor(vecs, device="cuda", dtype=torch.float16)

    def _gemini_batch(self, reqs: list[dict]) -> list[list[float]]:
        import httpx

        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:batchEmbedContents"
        for attempt, delay in enumerate((0, 2, 4, 8)):
            if delay:
                self.retries += 1
                time.sleep(delay)
            self.requests += 1
            try:
                r = self.http.post(url, json={"requests": reqs})
            except httpx.HTTPError:
                if attempt == 3:
                    raise RuntimeError("gemini 임베딩 연결 실패(4회)") from None
                continue
            if r.status_code == 200:
                break
            if (r.status_code != 429 and r.status_code < 500) or attempt == 3:
                raise RuntimeError(f"gemini-embedding-2 HTTP {r.status_code}")
        # 공개 가격으로 비용을 낸다. 토큰 수는 응답에 없어 countTokens 대신 글자 수 × 0.6으로 어림한다(§9 가정)
        self.cost_usd += sum(len(q["content"]["parts"][0]["text"]) for q in reqs) * 0.6 * GEMINI_PRICE_PER_MTOK / 1e6
        return [e["values"] for e in r.json()["embeddings"]]

    # ---------- 질의 ----------
    def encode_query(self, query: str):
        import torch

        self._load()
        if self.key == "kure1":
            return self.model.encode([query], convert_to_tensor=True, normalize_embeddings=True).half()[0]
        if self.key == "snow":
            return self.model.encode([query], prompt_name="query", convert_to_tensor=True,
                                     normalize_embeddings=True).half()[0]
        if self.key == "kure2":
            q = self.model.encode([query], is_query=True, convert_to_tensor=False)[0]
            return torch.as_tensor(q).to("cuda", torch.float16)
        v = self._gemini_batch([{"model": "models/gemini-embedding-2", "output_dimensionality": GEMINI_DIM,
                                 "content": {"parts": [{"text": f"task: search result | query: {query}"}]}}])[0]
        return torch.tensor(v, device="cuda", dtype=torch.float16)

    @staticmethod
    def scores(index, qv, n_docs: int):
        """질의 벡터와 문서 색인의 점수(문서 수 길이 텐서)."""
        import torch

        if isinstance(index, tuple):
            mat, owner = index
            sim = qv @ mat.T
            best = torch.full((qv.shape[0], n_docs), -1e4, device=sim.device, dtype=sim.dtype)
            best = best.scatter_reduce(1, owner.expand(qv.shape[0], -1), sim, reduce="amax")
            return best.float().sum(0)
        return (index @ qv).float()


class EmbeddingIndex:
    """구간 집합 하나에 대한 임베딩 색인. 같은 글이면 저장한 벡터를 다시 쓴다."""

    def __init__(self, key: str, ids: list[str], texts: list[str], titles: list[str], tag: str, allow_build=True):
        import torch

        self.key, self.ids = key, ids
        self.encoder = Encoder(key)
        path = cache_path(key, texts, tag)
        t0 = time.time()
        if path.exists():
            self.index = torch.load(path, map_location="cuda")
            self.built = False
        else:
            if not allow_build:
                raise FileNotFoundError(f"고정한 말뭉치 캐시가 없습니다. freeze prepare가 필요합니다: {path}")
            self.index = self.encoder.encode_docs(texts, titles)
            path.parent.mkdir(parents=True, exist_ok=True)
            torch.save(self.index, path)
            self.built = True
        self.build_seconds = round(time.time() - t0, 1)
        self.path = path

    def rank(self, query: str) -> tuple[list[tuple[str, float]], float]:
        """(점수 순 구간, 걸린 시간 ms). 시간은 질의 인코딩과 점수 계산을 넣는다."""
        import torch

        t0 = time.perf_counter()
        qv = self.encoder.encode_query(query)
        s = Encoder.scores(self.index, qv, len(self.ids))
        order = torch.argsort(s, descending=True).tolist()
        vals = s.tolist()
        ms = (time.perf_counter() - t0) * 1000
        return [(self.ids[i], vals[i]) for i in order], ms

    def info(self) -> dict:
        return {"model": self.key, "revision": REVISIONS[self.key][1], "cache": str(self.path.relative_to(ROOT)),
                "built": self.built, "build_seconds": self.build_seconds, "cost_usd": round(self.encoder.cost_usd, 4),
                "requests": self.encoder.requests, "retries": self.encoder.retries}
