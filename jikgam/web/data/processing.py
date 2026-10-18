"""웹 문서 처리·검색 평가. 고정 산출물은 읽고 새 결과는 local/web에만 쓴다."""

import asyncio
import json
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

from .. import settings

STAGES = ("텍스트 추출", "문맥 분할", "구간 정보", "색인", "검색·리랭킹", "채점")
SETS = {
    "R0": "R0-article256",
    "R1": "fixed256--none--bm25",
    "R4": "b-W4000-C800-raw--full--snow+rrf+jev8",
    "R4c-Jev": "c-jev-W4000-C800-raw-t0.65--full--snow+rrf+jev8",
    "R4c-Qwen": "c-qwen-W4000-C800-raw-t0.55--full--snow+rrf+jev8",
}
SET_DESCRIPTIONS = {
    "R0": "조 단위 · BM25",
    "R1": "고정 길이 · BM25",
    "R4": "문맥 분할 + 구간 정보 + 하이브리드 + 리랭킹",
    "R4c-Jev": "분할 판단 Jev · 그 외 R4와 동일",
    "R4c-Qwen": "분할 판단 Qwen · 그 외 R4와 동일",
}


def read(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def local(root: Path) -> Path:
    return root / "data/v2/local/web"


def qwen_available() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8893), timeout=0.2):
            return True
    except OSError:
        return False


def validate(model: str, split: str):
    if split != "val":
        raise ValueError("화면에서는 val만 실행할 수 있습니다.")
    if model not in settings.RETRIEVAL_MODELS:
        raise ValueError("분할 판단 모델을 고르세요.")
    if model == "qwen" and not qwen_available():
        raise ValueError("조정한 Qwen 서버에 연결할 수 없습니다.")


def results(root: Path, split: str | None = None):
    """지정 분할의 고정 결과만 읽는다. 화면 실행 결과는 별도로 비교한다."""
    split = split or settings.RETRIEVAL_SPLIT
    rows = []
    for label, name in SETS.items():
        obj = read(root / "results/v2" / split / f"{name}.json")
        if obj:
            rows.append({**obj, "label": label, "description": SET_DESCRIPTIONS[label], "run_id": None})
    return rows


def val_results(root: Path):
    """완료된 val 화면 실행과 같은 구성의 val 기준 행만 함께 반환한다."""
    rows, configurations = [], set()
    for p in sorted((local(root) / "jobs").glob("*/result.json"), reverse=True):
        obj = read(p)
        if obj.get("split") == "val":
            state = read(p.parent / "state.json", {})
            model = state.get("model", "jev")
            configurations.add(settings.RETRIEVAL_MODELS[model][2] + "--full--snow+rrf+jev8")
            rows.append({**obj, "label": "화면 실행", "model_label": settings.RETRIEVAL_MODELS[model][0],
                         "description": "문맥 분할 + 구간 정보 + 하이브리드 + 리랭킹",
                         "run_id": state.get("id", p.parent.name), "boundary_model": model,
                         "simulated": state.get("simulated") is True})
    if not rows:
        return []
    baselines = [{**row, "description": "val 기준 · " + row["description"]}
                 for row in results(root, "val") if SETS[row["label"]] in configurations]
    return baselines + rows


def estimate(root: Path, model: str, ratio: float = 1.0, benchmark: bool = True):
    _, judge, name, _ = settings.RETRIEVAL_MODELS[model]
    base = name.split("-t0.")[0]
    cm = read(root / "data/v2/chunks" / f"{base}.meta.json", {})
    selected = read(root / "data/v2/chunks" / f"{name}.meta.json", cm)
    mm = read(root / "data/v2/meta" / f"{name}.meta.json", {})
    fallback = read(root / "data/v2/meta" / f"{settings.RETRIEVAL_CHUNKS}.meta.json", {})
    chunks = selected.get("chunks", cm.get("chunks"))
    split_cost = (cm.get("calls") or {}).get("total_cost_usd")
    meta_cost = mm.get("cost_usd")
    if meta_cost is None and chunks and fallback.get("called"):
        meta_cost = fallback["cost_usd"] * chunks / fallback["called"]
    rr = read(root / "results/v2/val" / f"{name}--full--snow+rrf+jev8.json", {})
    rerank = (rr.get("rerank") or {}).get("cost_usd") if benchmark else 0.0
    amounts = [None if x is None else x * ratio for x in (split_cost, meta_cost, rerank)]
    corpus = read(root / "data/v2/extracted/summary.json", {})
    return {"split": amounts[0], "metadata": amounts[1], "rerank": amounts[2], "documents": len(corpus["docs"]) if corpus.get("docs") else None,
            "total": sum(amounts) if all(x is not None for x in amounts) else None,
            "ratio": ratio, "chunks": round(chunks * ratio) if chunks else None,
            "sources": [f"data/v2/chunks/{base}.meta.json", f"data/v2/chunks/{name}.meta.json",
                        f"data/v2/meta/{name}.meta.json" if mm else f"data/v2/meta/{settings.RETRIEVAL_CHUNKS}.meta.json",
                        f"results/v2/val/{name}--full--snow+rrf+jev8.json"]}


@lru_cache(maxsize=4)
def originals(root: Path):
    """지정 경로의 존재를 확인하고 원본 로더를 다시 쓴다."""
    from jikgam.ingest.corpus import ROOT, load_docs
    from jikgam.ingest import chunk, metadata

    if root.resolve() != ROOT.resolve() or not (root / "data/v2/extracted/summary.json").exists():
        return {}, [], {}
    return load_docs(), chunk.load(settings.RETRIEVAL_CHUNKS), metadata.load(settings.RETRIEVAL_CHUNKS, "full")


def documents(root: Path, org: str):
    docs, chunks, meta = originals(root)
    out = []
    for did, d in docs.items():
        cs = [c for c in chunks if c.doc == did]
        out.append({"id": did, "title": d.title, "kind": d.kind, "count": len(cs), "status": "처리 완료",
                    "doc": d, "chunks": cs, "meta": meta})
    for p in sorted((local(root) / "uploads" / org).glob("*/state.json"), reverse=True):
        s = read(p)
        out.append({"id": s["id"], "title": s["title"], "kind": "PDF", "count": s.get("chunks", 0),
                    "status": s["status"], "state": s, "path": p.parent})
    return out


def detail(item):
    if "doc" in item:
        return item["doc"], item["chunks"], item["meta"]
    from jikgam.ingest.corpus import Doc, Line
    from jikgam.ingest.chunk import Chunk

    d = read(item["path"] / "document.json")
    if not d:
        return None, [], {}
    d["lines"] = [Line(**ln) for ln in d["lines"]]
    return Doc(**d), [Chunk(**c) for c in read(item["path"] / "chunks.json", [])], read(item["path"] / "meta.json", {})


class WebEmbedding:
    """고정 캐시 경로를 바꾸지 않고 Encoder의 저수준 함수로 색인한다."""

    def __init__(self, cs, directory: Path):
        from jikgam.v2.embed import Encoder
        import torch

        self.ids = [c.id for c in cs.chunks]
        self.encoder = Encoder("snow")
        texts = [cs.text(c) + ("\n" + cs.extra[c.id] if c.id in cs.extra else "") for c in cs.chunks]
        self.index = self.encoder.encode_docs(texts, [cs.docs[c.doc].title for c in cs.chunks])
        torch.save(self.index, directory / "snow.pt")

    def rank(self, query):
        from jikgam.v2.embed import Encoder
        import torch

        start = time.perf_counter()
        scores = Encoder.scores(self.index, self.encoder.encode_query(query), len(self.ids))
        order, vals = torch.argsort(scores, descending=True).tolist(), scores.tolist()
        return [(self.ids[i], vals[i]) for i in order], (time.perf_counter() - start) * 1000


class Processing:
    def __init__(self, root: Path):
        self.root = root
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="web-processing")
        self.lock = threading.Lock()

    def save(self, directory, state):
        write(directory / "state.json", state)

    def prepare_pdf(self, org: str, title: str, payload: bytes):
        from jikgam.ingest.extract import extract_file
        from jikgam.ingest.corpus import Doc, Line
        from jikgam.ingest.chunk import line_tokens

        if len(payload) > settings.PDF_MAX_BYTES or not payload.startswith(b"%PDF-"):
            raise ValueError("크기 제한 안의 PDF 파일을 선택하세요.")
        uid = uuid.uuid4().hex
        directory = local(self.root) / "uploads" / org / uid
        directory.mkdir(parents=True)
        (directory / "source.pdf").write_bytes(payload)
        start = time.perf_counter()
        blocks, _ = extract_file(directory / "source.pdf", "본문")
        lines, offset = [], 0
        for b in blocks:
            for txt in b.text.splitlines():
                lines.append(Line(len(lines) + 1, b.file, b.page, b.kind, txt, offset))
                offset += len(txt) + 1
        if not lines or not any(x.text.strip() for x in lines):
            raise ValueError("읽을 수 있는 글이 없습니다. 글자가 포함된 PDF를 선택하세요.")
        doc = Doc(uid, title, "업로드 문서", False, lines)
        write(directory / "document.json", asdict(doc))
        lt = line_tokens(doc)
        write(directory / "line_tokens.json", lt)
        ref = read(self.root / "data/v2/chunks" / f"{settings.RETRIEVAL_CHUNKS}.meta.json", {})
        corpus_tokens = ref.get("chunks", 0) * ref.get("tokens", {}).get("mean", 0)
        if not corpus_tokens:
            raise ValueError("비용 산정 기준 자료가 없어 처리를 시작할 수 없습니다.")
        est = estimate(self.root, "flash", sum(lt) / corpus_tokens, False)
        state = {"id": uid, "title": title, "org": org, "model": "flash", "status": "대기", "chunks": 0,
                 "estimate": est, "stages": [{"name": n, "status": "대기", "seconds": None, "cost_usd": None}
                                             for n in STAGES[:4]]}
        state["stages"][0].update(status="완료", seconds=time.perf_counter() - start, cost_usd=0.0)
        self.save(directory, state)
        return state

    def confirm_pdf(self, org, uid):
        if not isinstance(uid, str) or len(uid) != 32 or any(c not in "0123456789abcdef" for c in uid):
            raise ValueError("문서를 찾을 수 없습니다.")
        directory = local(self.root) / "uploads" / org / uid
        with self.lock:
            state = read(directory / "state.json")
            if not state or state["org"] != org:
                raise ValueError("문서를 찾을 수 없습니다.")
            if state["status"] != "대기":
                raise ValueError("이미 시작한 처리입니다.")
            if state["estimate"]["total"] is None:
                raise ValueError("비용을 계산할 수 없어 처리를 시작할 수 없습니다.")
            state["status"] = "진행"
            self.save(directory, state)
            self.pool.submit(self._run, directory, state, False)
        return state

    def submit(self, model, split="val"):
        validate(model, split)
        est = estimate(self.root, model)
        if est["total"] is None:
            raise ValueError("비용 산정 기준 자료가 없어 실행할 수 없습니다.")
        uid = uuid.uuid4().hex
        state = {"id": uid, "title": settings.RETRIEVAL_MODELS[model][0], "model": model, "split": split,
                 "estimate": est, "status": "진행", "chunks": 0,
                 "stages": [{"name": n, "status": "완료" if i == 0 else "대기", "seconds": None, "cost_usd": None}
                             for i, n in enumerate(STAGES)]}
        directory = local(self.root) / "jobs" / uid
        self.save(directory, state)
        self.pool.submit(self._run, directory, state, True)
        return state

    def recent(self):
        paths = sorted((local(self.root) / "jobs").glob("*/state.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [read(p) for p in paths]

    def _run(self, directory, state, benchmark):
        try:
            asyncio.run(self._process(directory, state, benchmark))
        except Exception as exc:
            state["status"] = "실패"
            state["error"] = type(exc).__name__
            for stage in state["stages"]:
                if stage["status"] == "진행":
                    stage["status"] = "실패"
                    stage["seconds"] = time.time() - stage["started"] if stage.get("started") else None
            self.save(directory, state)

    async def _process(self, directory, state, benchmark):
        if benchmark:
            validate(state["model"], state["split"])

        from jikgam.ingest import chunk, metadata
        from jikgam.ingest.corpus import Doc, Line, load_docs
        from jikgam.ingest.window import Config, Judge, apply_cmax, split_doc, to_chunks
        from jikgam.sim.gemini import GeminiJson
        from jikgam.v2.search import ChunkSet

        # 호출 직후 단계를 기록해 실패한 호출의 비용도 남긴다.
        def start(i):
            state["stages"][i].update(status="진행", cost_usd=0.0, started=time.time())
            self.save(directory, state)
            return time.perf_counter()

        def done(i, t):
            state["stages"][i].update(status="완료", seconds=time.perf_counter() - t)
            self.save(directory, state)

        if benchmark:
            docs = load_docs()
        else:
            obj = read(directory / "document.json")
            obj["lines"] = [Line(**ln) for ln in obj["lines"]]
            d = Doc(**obj)
            docs = {d.id: d}
        _, backend, _, theta = settings.RETRIEVAL_MODELS[state["model"]]
        cfg = Config(backend, 4000, 800, "raw")
        t = start(1)
        judge = Judge(cfg)
        chunks, calls = [], []
        try:
            for d in docs.values():
                lt = chunk.line_tokens(d)
                # 판별 방식은 기준 임계값으로 맥락을 만들고 저장 확률로 다시 자른다.
                r = await split_doc(judge, d, lt)
                calls.extend(asdict(c) for c in r.calls)
                state["stages"][1]["cost_usd"] += sum(c.cost_usd for c in r.calls)
                write(directory / "calls.json", calls)
                self.save(directory, state)
                if any(c.error for c in r.calls):
                    raise RuntimeError("분할 호출 실패")
                bounds = set(r.boundaries)
                if backend != "llm":
                    bounds = {1} | {n for n, p in r.probs.items() if p >= theta and n > 1}
                    apply_cmax(lt, bounds, 1, len(d.lines), 1, cfg.cmax)
                chunks.extend(to_chunks(state["id"], d, lt, sorted(bounds)))
        finally:
            await judge.aclose()
        state["chunks"] = len(chunks)
        write(directory / "chunks.json", [asdict(c) for c in chunks])
        done(1, t)
        t = start(2)
        client, meta = GeminiJson.from_config(metadata.MODEL), {}
        sem = asyncio.Semaphore(metadata.CONCURRENCY)

        async def annotate(d, c, prev, nxt):
            user = (f"문서: 「{d.title}」({d.kind})\n앞 구간의 첫 줄: {prev}\n뒤 구간의 첫 줄: {nxt}\n\n"
                    f"## 구간({c.start_line}~{c.end_line}줄)\n{d.text[c.start:c.end]}")
            async with sem:
                r = await client.ask(metadata.system_prompt("full"), user, metadata.SCHEMA)
            meta[c.id] = {"id": c.id, "doc": d.id, **(r.data or {}), "usage": r.usage()}
            state["stages"][2]["cost_usd"] += r.cost_usd
            state["metadata_done"] = len(meta)
            write(directory / "meta.json", meta)
            self.save(directory, state)
            if r.error or not (r.data or {}).get("fields"):
                raise RuntimeError("구간 정보 호출 실패")

        try:
            todo = []
            by_doc = {did: [c for c in chunks if c.doc == did] for did in docs}
            for did, cs in by_doc.items():
                d = docs[did]
                for i, c in enumerate(cs):
                    prev = d.lines[cs[i - 1].start_line - 1].text if i else "(없음)"
                    nxt = d.lines[cs[i + 1].start_line - 1].text if i + 1 < len(cs) else "(없음)"
                    todo.append(annotate(d, c, prev, nxt))
            replies = await asyncio.gather(*todo, return_exceptions=True)
            if any(isinstance(r, BaseException) for r in replies):
                raise RuntimeError("구간 정보 호출 실패")
        finally:
            await client.aclose()
        done(2, t)
        t = start(3)
        cs = ChunkSet(state["id"], chunks, docs, {cid: metadata.text_for_index(m) for cid, m in meta.items()})
        emb = WebEmbedding(cs, directory)
        write(directory / "index.json", {"chunks": len(chunks), "embed": "snow", "fusion": "rrf", "related": False,
                                        "extra": cs.extra})
        done(3, t)
        if benchmark:
            await self._evaluate(directory, state, cs, meta, emb, start, done)
        state["status"] = "완료"
        self.save(directory, state)

    async def _evaluate(self, directory, state, cs, meta, emb, start, done):
        from jikgam.decision.registry import make_client
        from jikgam.v2.evaluate import Scorer, evaluate_rankings, load_questions, per_question_found
        from jikgam.v2.hybrid import Hybrid, HybridConfig
        from jikgam.v2.run import pct

        validate(state["model"], state["split"])
        questions = load_questions("val", include_none=True)
        t = start(4)
        client = make_client("jev")
        ranks, traces = {}, {}
        try:
            emb.rank("예열 질의입니다.")
            warm = await client.decide({"warm": "예열"}, {"w": {"type": "noul", "instructions": "예열인가?"}})
            state["stages"][4]["cost_usd"] += warm.cost_usd
            hy = Hybrid(cs, HybridConfig("snow", "rrf", False, 8), meta, emb, client)
            for q in questions:
                ranked, tr = await hy.rank(q["question"])
                ranks[q["id"]] = [cid for cid, _ in ranked]
                traces[q["id"]] = asdict(tr)
                state["stages"][4]["cost_usd"] += tr.rerank_cost_usd
                state["questions_done"] = len(ranks)
                write(directory / "traces.json", traces)
                self.save(directory, state)
        finally:
            await client.aclose()
        done(4, t)
        t = start(5)
        scorer = Scorer(cs.docs)
        obj = {"name": state["title"] + " · " + state["id"][:8], "split": "val", "config": {"embed": "snow", "fusion": "rrf", "related": False, "rerank": 8},
               "metrics": evaluate_rankings(cs, questions, ranks, scorer),
               "per_question@2000": per_question_found(cs, questions, ranks, scorer),
               "latency_ms": {"retrieval_p95": pct([tr["retrieval_ms"] for tr in traces.values()], .95),
                              "rerank_p95": pct([tr["rerank_ms"] for tr in traces.values() if tr["rerank_ms"] is not None], .95)},
               "rerank": {"cost_usd": sum(tr["rerank_cost_usd"] for tr in traces.values()),
                          "errors": sum(bool(tr["rerank_error"]) for tr in traces.values())}}
        write(directory / "result.json", obj)
        done(5, t)
