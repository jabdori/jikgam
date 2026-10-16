"""문서 처리 2단계: 참조 방식 분할 "슬라이딩 윈도우 + 판단"(docs/evaluation-v2.md §3.2).

절차(문서마다):
1. 첫 줄은 언제나 경계다.
2. 창 k는 줄 s_k부터 W토큰 안에 드는 줄이다. 앞쪽 80%의 줄(판단 구역)만 경계인지 정하고, 뒤 20%는 맥락이다.
3. 다음 창은 판단 구역 바로 다음 줄에서 시작한다. 모든 줄은 한 번만 판단된다.
4. 창마다 s_k 앞에서 시작해 아직 끝나지 않은 구간(열린 구간)의 맥락을 넘긴다.
   - raw: 열린 구간의 첫 줄과 마지막 5줄
   - summary: 생성형 LLM이 쓴 2~3문장 요약
5. 구간이 Cmax토큰을 넘으면 코드가 넘기 직전 줄에서 자른다. 이 경계도 그 방식의 경계로 센다.

판단하는 쪽:
- (b) llm: 생성형 LLM(gemini-3.8-flash)이 판단 구역에서 새 구간이 시작되는 줄 번호를 답한다. 요약도 같은 호출에서 쓴다.
- (c) judge: 판별 모델이 판단 구역의 줄마다 "이 줄에서 새 구간이 시작되는가"를 예/아니오 확률로 답한다.
  확률이 θ 이상이면 경계다. 열린 구간의 맥락은 θ 0.5로 정한 경계로 만든다. 줄별 확률을 저장해 θ를 바꿔 다시 자를 때
  쓴다(맥락은 θ 0.5 실행의 것을 그대로 쓰는 근사다). 요약 방식이면 판별 뒤 창마다 생성형 LLM을 한 번 불러 요약을 쓴다.

문서 양식(조·항, 별표 번호)을 쓰는 규칙은 없다. 경계 정의는 §2.2의 문장 그대로다.

    uv run python -m jikgam.ingest.window llm --W 4000 --cmax 800 --context raw
    uv run python -m jikgam.ingest.window judge --backend jev --W 4000 --cmax 800 --context raw
    uv run python -m jikgam.ingest.window threshold c-jev-W4000-C800-raw --theta 0.45
"""

import argparse
import asyncio
import json
import time
from dataclasses import asdict, dataclass, field

from jikgam.ingest import chunk as chunking
from jikgam.ingest.chunk import Chunk
from jikgam.ingest.corpus import Doc, load_docs

JUDGE_SHARE = 0.8
RAW_TAIL = 5
CONCURRENCY = 4  # 동시 요청 수(§4.1 시간 측정 조건). 문서끼리 동시에 돈다
QWEN_BATCH = 40  # 튜닝 2B 요청 하나에 넣는 질문 수(서빙 한도 16,384토큰 안)
LLM = "gemini-flash"

DEFINITION = """- 경계는 "앞 내용을 찾는 사람에게 뒤 내용이 필요 없어지는 곳"이다.
- 번호, 제목, 문단 같은 겉모양이 바뀌어도 내용이 이어지면 경계가 아니다.
- 경계는 새 구간이 시작되는 줄로 표시한다. 줄 중간에서 내용이 바뀌면 다음 줄에 표시한다."""

DOC_NOTE = "글은 PDF에서 뽑은 것이다. 각 줄 앞의 숫자는 줄 번호이고 탭으로 나뉜다. PDF 줄바꿈이 단어 중간에 있을 수 있다. 표는 눈에 보이는 줄마다 칸을 \" | \"로 나눠 적었다."


def system_prompt(cmax: int, summary: bool) -> str:
    s = f"""너는 문서를 검색용 구간으로 나눈다. 검색은 구간 단위로 하므로, 한 구간에는 함께 찾아야 하는 내용이 들어가야 한다.
{DOC_NOTE}

{DEFINITION}
- 구간 하나는 {cmax}토큰을 넘지 않는다. 넘으면 코드가 자른다. 그러니 그보다 짧은 범위에서 내용이 바뀌는 곳을 고른다.
- 판단 구역의 줄만 고른다. 앞뒤 맥락의 줄은 고르지 않는다."""
    if summary:
        s += "\n- summary에는 판단 구역이 끝날 때 아직 끝나지 않은 구간(판단 구역의 마지막 경계부터 끝까지)의 내용을 2~3문장으로 쓴다. 다음 창을 판단할 때 앞 맥락으로 쓴다."
    return s


SCHEMA_LLM = {"type": "object", "properties": {"boundaries": {"type": "array", "items": {"type": "integer"}},
                                               "summary": {"type": "string"}}, "required": ["boundaries", "summary"]}
SCHEMA_SUMMARY = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}
SUMMARY_SYSTEM = f"""너는 문서 구간의 내용을 요약한다. {DOC_NOTE}
주어진 줄들은 아직 끝나지 않은 한 구간이다. 이 구간이 무엇에 관한 내용인지 2~3문장으로 쓴다. 다음 창에서 경계를 판단할 때 앞 맥락으로 쓴다."""


@dataclass
class Config:
    judge: str  # llm | jev | qwen
    W: int
    cmax: int
    context: str  # raw | summary

    @property
    def name(self) -> str:
        who = "b" if self.judge == "llm" else f"c-{self.judge}"
        return f"{who}-W{self.W}-C{self.cmax}-{self.context}"


@dataclass
class CallLog:
    doc: str
    window: int
    kind: str  # judge | summary
    requests: int = 1
    questions: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    attempts: int = 1
    error: str | None = None


@dataclass
class DocResult:
    boundaries: list[int]  # 경계 줄 번호(오름차순, 첫 줄 포함)
    hard_cuts: list[int] = field(default_factory=list)  # Cmax로 자른 줄
    probs: dict[int, float] = field(default_factory=dict)  # (c) 줄 → 경계 확률
    calls: list[CallLog] = field(default_factory=list)


def numbered(doc: Doc, a: int, b: int) -> str:
    return "\n".join(f"{line.no}\t{line.text}" for line in doc.lines[a - 1:b])


def windows_from(lt: list[int], s: int, W: int) -> tuple[int, int]:
    """s에서 시작하는 창의 (판단 구역 끝 줄, 창 끝 줄)."""
    n = len(lt)
    acc, end = 0, s
    while end <= n and acc + lt[end - 1] <= W:
        acc += lt[end - 1]
        end += 1
    end = max(end - 1, s)
    if end == n:
        return n, n
    acc, jend = 0, s
    while jend <= end and acc + lt[jend - 1] <= JUDGE_SHARE * W:
        acc += lt[jend - 1]
        jend += 1
    return max(jend - 1, s), end


def context_text(doc: Doc, open_start: int, s: int, summary: str | None, mode: str, open_tokens: int) -> str:
    if open_start >= s:
        return "(앞에서 이어지는 구간이 없다. 판단 구역의 첫 줄은 새 구간의 시작이다.)"
    head = f"앞에서 이어지는 구간은 {open_start}줄에서 시작했고 지금까지 {open_tokens}토큰이다."
    if mode == "summary":
        return f"{head}\n요약: {summary or '(요약 없음)'}"
    tail_from = max(open_start + 1, s - RAW_TAIL)
    parts = [head, f"첫 줄:\n{open_start}\t{doc.lines[open_start - 1].text}"]
    if tail_from < s:
        parts.append(f"마지막 {s - tail_from}줄:\n{numbered(doc, tail_from, s - 1)}")
    return "\n".join(parts)


class Judge:
    """창 하나를 판단한다. (b)는 줄 번호 목록, (c)는 줄별 확률을 돌려준다."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        from jikgam.sim.gemini import GeminiJson

        self.llm = GeminiJson.from_config(LLM) if (cfg.judge == "llm" or cfg.context == "summary") else None
        self.decider = None
        if cfg.judge != "llm":
            from jikgam.decision.registry import make_client

            self.decider = make_client({"jev": "jev", "qwen": "q35-2b-ko-direct"}[cfg.judge])
        # 로컬 튜닝 2B는 요청을 하나씩 처리하므로 동시 1로 잰다(§4.1)
        self.sem = asyncio.Semaphore(1 if cfg.judge == "qwen" else CONCURRENCY)

    async def llm_window(self, doc: Doc, ctx: str, s: int, jend: int, wend: int, k: int) -> tuple[list[int], str, CallLog]:
        user = (f"문서: 「{doc.title}」({doc.kind})\n\n## 앞 맥락\n{ctx}\n\n## 창\n{numbered(doc, s, wend)}\n\n## 할 일\n"
                f"- 판단 구역은 {s}~{jend}줄이다. 이 가운데 새 구간이 시작되는 줄 번호를 모두 boundaries에 적는다."
                f" 없으면 빈 목록이다.")
        if wend > jend:
            user += f"\n- {jend + 1}~{wend}줄은 뒤 맥락이다. 고르지 않는다."
        if self.cfg.context != "summary":
            user += "\n- summary는 빈 글로 둔다."
        async with self.sem:
            r = await self.llm.ask(system_prompt(self.cfg.cmax, self.cfg.context == "summary"), user, SCHEMA_LLM)
        log = CallLog(doc.id, k, "judge", input_tokens=r.input_tokens, output_tokens=r.output_tokens,
                      latency_ms=r.latency_ms, cost_usd=r.cost_usd, attempts=r.attempts, error=r.error)
        data = r.data or {}
        picked = sorted({int(x) for x in data.get("boundaries") or [] if isinstance(x, (int, float)) and s <= int(x) <= jend})
        return picked, str(data.get("summary") or ""), log

    async def judge_window(self, doc: Doc, ctx: str, s: int, jend: int, wend: int, k: int) -> tuple[dict[int, float], CallLog]:
        state = {"task": "문서를 검색용 구간으로 나눌 때, 줄마다 그 줄에서 새 구간이 시작되는지 판단한다.",
                 "rules": DOC_NOTE + "\n" + DEFINITION + f"\n- 구간 하나는 {self.cfg.cmax}토큰을 넘지 않는다.",
                 "document": f"「{doc.title}」({doc.kind})", "previous_context": ctx,
                 "window": numbered(doc, s, wend), "judge_lines": f"{s}~{jend}"}
        questions = {f"line_{n}": {"type": "noul", "instructions": f"{n}줄에서 새 구간이 시작되는가?"} for n in range(s, jend + 1)}
        # Jev는 한 요청에 질문 수 제한이 없다. 튜닝 2B는 질문을 입력 앞에 붙여(질문 하나에 약 155토큰) 16,384토큰 한도를
        # 넘으므로, 질문을 나눠 같은 창으로 여러 번 보낸다(나눈 요청 수는 requests에 센다)
        keys = list(questions)
        size = QWEN_BATCH if self.cfg.judge == "qwen" else len(keys)
        answers, log = {}, CallLog(doc.id, k, "judge", requests=0, questions=len(questions))
        for i in range(0, len(keys), size):
            part = {q: questions[q] for q in keys[i:i + size]}
            async with self.sem:
                r = await self.decider.decide(state, part)
            answers.update(r.answers)
            log.requests += 1
            log.input_tokens += r.input_tokens
            log.output_tokens += r.output_tokens
            log.latency_ms += r.latency_ms
            log.cost_usd += r.cost_usd
            if r.error:
                log.error = r.error if log.error is None else log.error
        probs = {}
        for n in range(s, jend + 1):
            a = answers.get(f"line_{n}")
            probs[n] = float(a["noul"]) if a and a.get("type") == "noul" else 0.0  # 실패한 줄은 경계 아님(§4.5)
        return probs, log

    async def summarize(self, doc: Doc, open_start: int, end: int, k: int) -> tuple[str, CallLog]:
        user = f"문서: 「{doc.title}」({doc.kind})\n\n## 구간({open_start}~{end}줄)\n{numbered(doc, open_start, end)}"
        async with self.sem:
            r = await self.llm.ask(SUMMARY_SYSTEM, user, SCHEMA_SUMMARY)
        log = CallLog(doc.id, k, "summary", input_tokens=r.input_tokens, output_tokens=r.output_tokens,
                      latency_ms=r.latency_ms, cost_usd=r.cost_usd, attempts=r.attempts, error=r.error)
        return str((r.data or {}).get("summary") or ""), log

    async def aclose(self) -> None:
        if self.llm:
            await self.llm.aclose()
        if self.decider:
            await self.decider.aclose()


def apply_cmax(lt: list[int], bounds: set[int], a: int, b: int, open_start: int, cmax: int) -> tuple[int, list[int]]:
    """줄 a~b에서 경계(bounds)를 따라가며 Cmax를 넘는 구간을 자른다. (끝난 뒤의 열린 구간 시작, 자른 줄)."""
    cuts = []
    seg_tokens = sum(lt[open_start - 1:a - 1])
    for n in range(a, b + 1):
        if n in bounds:
            open_start, seg_tokens = n, 0
        elif seg_tokens + lt[n - 1] > cmax and n > open_start:
            bounds.add(n)
            cuts.append(n)
            open_start, seg_tokens = n, 0
        seg_tokens += lt[n - 1]
    return open_start, cuts


async def split_doc(judge: Judge, doc: Doc, lt: list[int], theta: float = 0.5) -> DocResult:
    cfg = judge.cfg
    n = len(doc.lines)
    bounds: set[int] = {1}
    res = DocResult([])
    s, open_start, summary, k = 1, 1, "", 0
    while s <= n:
        jend, wend = windows_from(lt, s, cfg.W)
        open_tokens = sum(lt[open_start - 1:s - 1])
        ctx = context_text(doc, open_start, s, summary, cfg.context, open_tokens)
        if cfg.judge == "llm":
            picked, new_summary, log = await judge.llm_window(doc, ctx, s, jend, wend, k)
            res.calls.append(log)
            bounds.update(picked)
        else:
            probs, log = await judge.judge_window(doc, ctx, s, jend, wend, k)
            res.calls.append(log)
            res.probs.update(probs)
            bounds.update(x for x, p in probs.items() if p >= theta and x > 1)
        open_start, cuts = apply_cmax(lt, bounds, s, jend, open_start, cfg.cmax)
        res.hard_cuts += cuts
        if cfg.context == "summary" and jend < n:
            if cfg.judge == "llm":
                summary = new_summary
            else:
                summary, slog = await judge.summarize(doc, open_start, jend, k)
                res.calls.append(slog)
        s, k = jend + 1, k + 1
    res.boundaries = sorted(bounds)
    return res


def to_chunks(name: str, doc: Doc, lt: list[int], boundaries: list[int]) -> list[Chunk]:
    starts = sorted(set(boundaries) | {1})
    out = []
    for i, a in enumerate(starts):
        b = starts[i + 1] - 1 if i + 1 < len(starts) else len(doc.lines)
        out.append(chunking.make(doc, name, a, b, lt, i, kind="window"))
    return out


def summarize_calls(calls: list[CallLog]) -> dict:
    by = {}
    for kind in ("judge", "summary"):
        cs = [c for c in calls if c.kind == kind]
        if cs:
            by[kind] = {"windows": len(cs), "requests": sum(c.requests for c in cs), "questions": sum(c.questions for c in cs),
                        "input_tokens": sum(c.input_tokens for c in cs), "output_tokens": sum(c.output_tokens for c in cs),
                        "cost_usd": round(sum(c.cost_usd for c in cs), 5), "retries": sum(c.attempts - 1 for c in cs),
                        "errors": sum(1 for c in cs if c.error)}
    by["total_cost_usd"] = round(sum(c.cost_usd for c in calls), 5)
    return by


def chunk_meta(name: str, cfg: Config, chunks: list[Chunk], extra: dict) -> dict:
    toks = sorted(c.tokens for c in chunks)
    return {"method": name, "config": asdict(cfg), "chunks": len(chunks),
            "tokens": {"mean": round(sum(toks) / len(toks), 1), "median": toks[len(toks) // 2], "max": toks[-1]}, **extra}


async def run(cfg: Config, only: set[str] | None = None) -> dict:
    docs = load_docs(only)
    lts = {d: chunking.line_tokens(doc) for d, doc in docs.items()}
    judge = Judge(cfg)
    t0 = time.time()
    try:
        results = await asyncio.gather(*(split_doc(judge, docs[d], lts[d]) for d in docs))
    finally:
        await judge.aclose()
    seconds = round(time.time() - t0, 1)
    name = cfg.name
    chunks = [c for d, r in zip(docs, results) for c in to_chunks(name, docs[d], lts[d], r.boundaries)]
    calls = [c for r in results for c in r.calls]
    extra = {"seconds_wall": seconds, "concurrency": 1 if cfg.judge == "qwen" else CONCURRENCY, "calls": summarize_calls(calls),
             "hard_cuts": sum(len(r.hard_cuts) for r in results), "boundaries": sum(len(r.boundaries) for r in results)}
    chunking.write(name, chunks, chunk_meta(name, cfg, chunks, extra))
    (chunking.OUT / f"{name}.calls.jsonl").write_text("".join(json.dumps(asdict(c), ensure_ascii=False) + "\n" for c in calls),
                                                     encoding="utf-8", newline="\n")
    if cfg.judge != "llm":
        probs = {d: {str(k): round(v, 4) for k, v in sorted(r.probs.items())} for d, r in zip(docs, results)}
        (chunking.OUT / f"{name}.probs.json").write_text(json.dumps(probs) + "\n", encoding="utf-8", newline="\n")
    return chunk_meta(name, cfg, chunks, extra)


def rethreshold(name: str, theta: float) -> dict:
    """(c)의 줄별 확률로 θ를 바꿔 다시 자른다(Cmax는 같은 규칙으로 다시 적용). 호출은 하지 않는다."""
    meta = json.loads((chunking.OUT / f"{name}.meta.json").read_text(encoding="utf-8"))
    cfg = Config(**meta["config"])
    probs = json.loads((chunking.OUT / f"{name}.probs.json").read_text(encoding="utf-8"))
    docs = load_docs(set(probs))
    out_name = f"{name}-t{theta:.2f}"
    chunks = []
    for d, doc in docs.items():
        lt = chunking.line_tokens(doc)
        bounds = {1} | {int(k) for k, p in probs[d].items() if p >= theta and int(k) > 1}
        apply_cmax(lt, bounds, 1, len(doc.lines), 1, cfg.cmax)
        chunks += to_chunks(out_name, doc, lt, sorted(bounds))
    m = chunk_meta(out_name, cfg, chunks, {"theta": theta, "from": name})
    chunking.write(out_name, chunks, m)
    return m


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for cmd in ("llm", "judge"):
        p = sub.add_parser(cmd)
        if cmd == "judge":
            p.add_argument("--backend", choices=["jev", "qwen"], required=True)
        p.add_argument("--W", type=int, required=True)
        p.add_argument("--cmax", type=int, required=True)
        p.add_argument("--context", choices=["raw", "summary"], required=True)
        p.add_argument("--only", nargs="*")
    t = sub.add_parser("threshold")
    t.add_argument("name")
    t.add_argument("--theta", type=float, required=True)
    args = ap.parse_args()
    if args.cmd == "threshold":
        print(json.dumps(rethreshold(args.name, args.theta), ensure_ascii=False))
        return
    cfg = Config("llm" if args.cmd == "llm" else args.backend, args.W, args.cmax, args.context)
    print(json.dumps(asyncio.run(run(cfg, set(args.only) if args.only else None)), ensure_ascii=False))


if __name__ == "__main__":
    main()
