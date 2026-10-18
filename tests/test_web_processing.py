"""웹 문서 처리의 권한·비용 확인·격리와 실제 채점 연결을 검증한다. 유료 호출은 하지 않는다."""

import asyncio
from dataclasses import asdict

import pytest
from starlette.testclient import TestClient
from web_fixture import build

from jikgam.web import auth, settings
from jikgam.web.app import WebConfig, create_app
from jikgam.web.data import processing as p
from jikgam.decision.types import DecisionResult


def cost_sources(root):
    for name in (settings.RETRIEVAL_CHUNKS, "c-jev-W4000-C800-raw", "c-jev-W4000-C800-raw-t0.65"):
        p.write(root / f"data/v2/chunks/{name}.meta.json", {
            "chunks": 2, "tokens": {"mean": 20}, "calls": {"total_cost_usd": .1}})
        p.write(root / f"data/v2/meta/{name}.meta.json", {"called": 2, "cost_usd": .2})
    for label, name in p.SETS.items():
        p.write(root / f"results/v2/val/{name}.json", {
            "name": name, "split": "val", "metrics": {"single": {"items@2000": .8},
                "all": {"items@2000": .6, "ndcg@10": .7}},
            "latency_ms": {"retrieval_p95": 31, "rerank_p95": 210}, "rerank": {"cost_usd": .03}})


@pytest.fixture
def env(tmp_path):
    root = build(tmp_path, 0)
    cost_sources(root)
    accounts = {"t": auth.Account("t", "pw", "tenant", "org_t"),
                "u": auth.Account("u", "pw", "tenant", "org_u"),
                "a": auth.Account("a", "pw", "admin")}
    return root, TestClient(create_app(WebConfig(root=root, accounts=accounts)))


def hdr(name):
    return {"Authorization": auth.basic_header(name, "pw")}


@pytest.mark.parametrize("split", ["test", "check", "train", "../val", ""])
def test_retrieval_rejects_every_split_except_val(env, split, monkeypatch):
    root, c = env
    monkeypatch.setattr(p.Processing, "submit", lambda *_: pytest.fail("금지 분할을 실행함"))
    r = c.post("/admin/retrieval", data={"boundary_model": "jev", "split": split, "confirm": "yes"}, headers=hdr("a"))
    assert r.status_code == 400
    assert not p.local(root).exists()


def test_retrieval_cost_confirmation_and_role_boundary(env, monkeypatch):
    root, c = env
    submitted = []
    monkeypatch.setattr(p.Processing, "submit", lambda self, model, split: submitted.append((model, split)))
    data = {"boundary_model": "jev", "split": "val"}
    assert c.post("/admin/retrieval", data=data, headers=hdr("t")).status_code == 403
    r = c.post("/admin/retrieval", data=data, headers=hdr("a"))
    assert r.status_code == 200 and not submitted
    assert "검색 실행 확인" in r.text and "계산 근거" in r.text
    assert "0.330" in r.text
    data["confirm"] = "yes"
    assert c.post("/admin/retrieval", data=data, headers=hdr("a")).status_code == 200
    assert submitted == [("jev", "val")]


def test_qwen_disabled_and_backend_guard(env, monkeypatch):
    _, c = env
    monkeypatch.setattr(p, "qwen_available", lambda: False)
    r = c.get("/admin/models", headers=hdr("a"))
    assert 'value="qwen" disabled' in r.text
    assert c.post("/admin/retrieval", data={"boundary_model": "qwen", "confirm": "yes"}, headers=hdr("a")).status_code == 400


def test_pdf_validation_and_no_unconfirmed_paid_work(env, monkeypatch):
    root, c = env
    monkeypatch.setattr(p.Processing, "_run", lambda *_: pytest.fail("확인 없이 실행함"))
    assert c.post("/app/knowledge/upload", files={"pdf": ("x.pdf", b"invalid")}, headers=hdr("t")).status_code == 400
    assert c.post("/app/knowledge/process", data={"upload": "../anything"}, headers=hdr("t")).status_code == 400
    assert c.post("/app/knowledge/upload", files={"pdf": ("x.pdf", b"%PDF-")}, headers=hdr("a")).status_code == 403
    assert not list(p.local(root).glob("**/state.json"))


def test_pdf_extract_estimate_then_tenant_scoped_confirmation(env, monkeypatch):
    from jikgam.ingest import extract, chunk

    root, c = env
    monkeypatch.setattr(extract, "extract_file", lambda *_: ([extract.Block("본문", 1, "text", "가상 정책 첫 문장\n문의 접수 안내")], None))
    monkeypatch.setattr(chunk, "line_tokens", lambda _: [10, 10])
    submitted = []
    monkeypatch.setattr(p.Processing, "_run", lambda self, directory, state, bench: submitted.append((directory, state, bench)))
    r = c.post("/app/knowledge/upload", files={"pdf": ("가상 정책.pdf", b"%PDF-example", "application/pdf")}, headers=hdr("t"))
    assert r.status_code == 200 and "비용 확인 후 처리 시작" in r.text
    assert not submitted
    state_path = next(p.local(root).glob("uploads/org_t/*/state.json"))
    s = p.read(state_path)
    assert s["stages"][0]["status"] == "완료" and s["estimate"]["total"] == pytest.approx(.15)
    data = {"upload": s["id"], "confirm": "yes"}
    assert c.post("/app/knowledge/process", data=data, headers=hdr("u")).status_code == 400
    assert s["title"] not in c.get("/app/knowledge", headers=hdr("u")).text
    assert c.post("/app/knowledge/process", data=data, headers=hdr("t")).status_code == 200
    assert c.post("/app/knowledge/process", data=data, headers=hdr("t")).status_code == 400
    assert len(submitted) == 1 and submitted[0][2] is False


def test_live_pipeline_uses_web_paths_and_native_scoring(tmp_path, monkeypatch):
    from jikgam.ingest import chunk, corpus, window
    from jikgam.sim.gemini import GeminiJson, JsonReply
    from jikgam.decision import registry
    from jikgam.v2 import evaluate

    root = tmp_path
    lines = [corpus.Line(1, "본문", 1, "text", "가상 안내", 0),
             corpus.Line(2, "본문", 1, "text", "가상 처리", 6),
             corpus.Line(3, "본문", 1, "text", "가상 문의", 12)]
    doc = corpus.Doc("sample", "가상 문서", "법률", True, lines)
    monkeypatch.setattr(corpus, "load_docs", lambda: {doc.id: doc})
    monkeypatch.setattr(chunk, "line_tokens", lambda _: [2, 2, 2])
    observed = []

    class Judge:
        def __init__(self, cfg):
            self.cfg = cfg
        async def aclose(self):
            pass

    async def split(judge, d, lt):
        observed.append(asdict(judge.cfg))
        return window.DocResult([1, 2], probs={1: .9, 2: .7, 3: .6},
                                calls=[window.CallLog(doc.id, 0, "judge", cost_usd=.01)])

    class Client:
        async def ask(self, *args):
            return JsonReply({"fields": [{"name": "요약", "value": "가상 구간"}], "related": ["관련 가상 안내"]}, "fake", cost_usd=.02)
        async def decide(self, state, questions):
            return DecisionResult(backend="fake", model="fake", answers={q: {"type": "noul", "noul": .9} for q in questions},
                                   latency_ms=12, input_tokens=20, cost_usd=.003, error=None)
        async def aclose(self):
            pass

    class Emb:
        def __init__(self, cs, directory):
            self.ids = [c.id for c in cs.chunks]
            (directory / "snow.pt").write_bytes(b"fake vectors")
        def rank(self, q):
            return [(c, 1 / (i + 1)) for i, c in enumerate(self.ids)], 1

    question = {"id": "val-only", "type": "single", "question": "가상 안내", "evidence": [
        {"alternatives": [{"doc": doc.id, "start": 0, "end": 5}]}]}

    def questions(split, include_none):
        assert split == "val" and include_none
        return [question]

    monkeypatch.setattr(window, "Judge", Judge)
    monkeypatch.setattr(window, "split_doc", split)
    monkeypatch.setattr(GeminiJson, "from_config", lambda *_: Client())
    monkeypatch.setattr(registry, "make_client", lambda *_: Client())
    monkeypatch.setattr(p, "WebEmbedding", Emb)
    monkeypatch.setattr(evaluate, "load_questions", questions)
    cost_sources(root)
    protected = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    svc = p.Processing(root)
    state = {"id": "run", "title": "가상 실행", "model": "jev", "split": "val", "status": "진행", "chunks": 0,
             "stages": [{"name": n, "status": "대기", "cost_usd": None, "seconds": None} for n in p.STAGES]}
    directory = p.local(root) / "jobs/run"
    svc.save(directory, state)
    asyncio.run(svc._process(directory, state, True))
    result = p.read(directory / "result.json")
    assert state["status"] == "완료"
    assert state["chunks"] == 2  # 선택한 임계값에서는 마지막 줄이 경계가 아니다.
    assert observed == [{"judge": "jev", "W": 4000, "cmax": 800, "context": "raw"}]
    assert result["metrics"]["single"]["items@2000"] == 1
    assert result["config"] == {"embed": "snow", "fusion": "rrf", "related": False, "rerank": 8}
    assert result["latency_ms"]["rerank_p95"] is not None
    assert result["latency_ms"]["rerank_p95"] >= 0
    assert sum(x.get("cost_usd") or 0 for x in state["stages"]) == pytest.approx(.056)
    assert p.read(directory / "meta.json")
    assert (directory / "snow.pt").exists()
    assert all(path.read_bytes() == data for path, data in protected.items())
    assert all(path in protected or path.is_relative_to(p.local(root)) for path in root.rglob("*") if path.is_file())
    assert any(row["name"] == result["name"] for row in p.results(root))


def test_failure_retains_stage_and_incurred_cost(tmp_path, monkeypatch):
    svc = p.Processing(tmp_path)
    state = {"status": "진행", "stages": [{"status": "진행", "cost_usd": .023}]}

    async def failing(*_):
        raise RuntimeError("가상 실패")

    monkeypatch.setattr(svc, "_process", failing)
    svc._run(p.local(tmp_path) / "jobs/failure", state, True)
    saved = p.read(p.local(tmp_path) / "jobs/failure/state.json")
    assert saved["status"] == "실패" and saved["stages"][0]["status"] == "실패"
    assert saved["stages"][0]["cost_usd"] == .023


def test_original_segment_fields_related_and_search_keep_document(env, monkeypatch):
    from jikgam.ingest.corpus import Doc, Line
    from jikgam.ingest.chunk import Chunk
    from jikgam.web.ui import terms

    _, c = env
    d = Doc("statute", "가상 법령", "법률", True, [Line(1, "본문", 1, "text", "가상 청구 안내", 0)])
    ch = Chunk("original:statute:one", d.id, 1, 1, 0, len(d.text), 8)
    meta = {ch.id: {"fields": [{"name": "내용 요약", "value": "가상 접수 안내"}], "related": ["가상 확인 절차"]}}
    monkeypatch.setattr(p, "originals", lambda _: ({d.id: d}, [ch], meta))
    r = c.get("/app/knowledge?doc=statute&seg=" + ch.id, headers=hdr("t"))
    assert r.status_code == 200
    assert "가상 청구 안내" in r.text and "내용 요약" in r.text and "가상 확인 절차" in r.text
    assert 'name="doc" value="statute"' in r.text
    assert not terms.find(terms.visible_text(r.text), terms.load_terms())
    found = c.get("/app/knowledge?doc=statute&q=접수", headers=hdr("t"))
    assert "가상 청구 안내" in found.text
    absent = c.get("/app/knowledge?doc=statute&q=없는단어", headers=hdr("t"))
    assert "맞는 구간이 없습니다." in absent.text


def test_display_split_does_not_change_execution_guard(env, monkeypatch):
    root, c = env
    monkeypatch.setattr(settings, "RETRIEVAL_SPLIT", "test")
    p.write(root / "results/v2/test/R0-article256.json", {"name": "기존 집계", "split": "test", "metrics": {}})
    assert p.results(root)[0]["split"] == "test"
    assert c.post("/admin/retrieval", data={"boundary_model": "jev", "split": "test", "confirm": "yes"}, headers=hdr("a")).status_code == 400
