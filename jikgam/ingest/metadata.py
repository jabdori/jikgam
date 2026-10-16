"""문서 처리 3단계: 구간 정보(메타데이터, docs/evaluation-v2.md §3.3).

생성형 LLM(gemini-3.8-flash)이 구간마다 검색에 도움이 되는 정보를 문서에 맞게 정해 JSON으로 쓴다.
입력은 그 구간, 문서 이름, 앞뒤 구간의 첫 줄이다. 평가 세트의 질문은 넣지 않는다.

- 항목은 미리 고정하지 않는다. 모델이 항목 이름과 값을 정한다(fields).
- 관련 내용 따라가기(§3.4)에 쓰려고, "이 구간과 함께 봐야 하는 다른 내용"만은 related로 따로 받는다.
- variant "noexpr"는 예시에서 "고객이 쓸 만한 표현"을 뺀 프롬프트다(R4 보고용, §3.3).

색인할 때는 fields와 related의 값을 모두 글로 이어 붙여 원문과 함께 넣는다(text_for_index).
표준약관 구간의 정보는 표준약관 문장을 풀어 쓴 것이라 저장소 밖(data/v2/local/meta/)에 둔다.

    uv run python -m jikgam.ingest.metadata fixed256
    uv run python -m jikgam.ingest.metadata b-W4000-C800-raw --variant noexpr
"""

import argparse
import asyncio
import json
import time
from pathlib import Path

from jikgam.ingest import chunk as chunking
from jikgam.ingest.corpus import ROOT, load_docs

MODEL = "gemini-flash"
CONCURRENCY = 8
OUT = ROOT / "data" / "v2" / "meta"
LOCAL_OUT = ROOT / "data" / "v2" / "local" / "meta"

EXAMPLES = {"full": "요약, 핵심어, 무엇에 관한 내용인지, 누구에게 적용되는지, 고객이 쓸 만한 표현",
            "noexpr": "요약, 핵심어, 무엇에 관한 내용인지, 누구에게 적용되는지"}


def system_prompt(variant: str) -> str:
    return f"""너는 문서 검색용 색인을 만든다. 주어진 구간에 대해, 이 구간을 검색으로 찾을 때 도움이 되는 정보를 쓴다.
글은 PDF에서 뽑은 것이다. PDF 줄바꿈이 단어 중간에 있을 수 있고, 표는 줄마다 칸을 " | "로 나눠 적었다.

- 어떤 항목을 쓸지는 문서와 구간에 맞게 정한다. 예: {EXAMPLES[variant]}.
- fields에 항목 이름(name)과 값(value)을 적는다. 값은 짧은 글이다. 여러 개면 쉼표로 잇는다.
- related에는 이 구간을 이해하거나 적용하려면 함께 봐야 하는 다른 내용을 짧은 글로 적는다(같은 문서의 다른 부분이 정한 기준, 이 구간이 가리키는 다른 규정 같은 것). 없으면 빈 목록이다.
- 구간에 없는 내용을 지어내지 않는다."""


SCHEMA = {"type": "object", "properties": {
    "fields": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"},
                                                                         "value": {"type": "string"}},
                                          "required": ["name", "value"]}},
    "related": {"type": "array", "items": {"type": "string"}}}, "required": ["fields", "related"]}


def text_for_index(meta: dict) -> str:
    parts = [f"{f.get('name', '')}: {f.get('value', '')}" for f in meta.get("fields") or []]
    parts += [f"관련: {r}" for r in meta.get("related") or []]
    return "\n".join(parts)


def paths(chunks_name: str, variant: str) -> tuple[Path, Path]:
    stem = chunks_name if variant == "full" else f"{chunks_name}--{variant}"
    return OUT / f"{stem}.jsonl", LOCAL_OUT / f"{stem}.jsonl"


def load(chunks_name: str, variant: str = "full") -> dict[str, dict]:
    out = {}
    for p in paths(chunks_name, variant):
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    r = json.loads(line)
                    out[r["id"]] = r
    return out


async def build(chunks_name: str, variant: str) -> dict:
    from jikgam.sim.gemini import GeminiJson

    docs = load_docs()
    chunks = chunking.load(chunks_name)
    done = load(chunks_name, variant)  # 이미 쓴 구간은 다시 부르지 않는다
    by_doc: dict[str, list] = {}
    for c in chunks:
        by_doc.setdefault(c.doc, []).append(c)
    client = GeminiJson.from_config(MODEL)
    sem = asyncio.Semaphore(CONCURRENCY)
    system = system_prompt(variant)

    async def one(c, prev, nxt) -> dict:
        d = docs[c.doc]
        first = lambda x: d.lines[x.start_line - 1].text if x else "(없음)"  # noqa: E731
        user = (f"문서: 「{d.title}」({d.kind})\n앞 구간의 첫 줄: {first(prev)}\n뒤 구간의 첫 줄: {first(nxt)}\n\n"
                f"## 구간({c.start_line}~{c.end_line}줄)\n{d.text[c.start:c.end]}")
        async with sem:
            r = await client.ask(system, user, SCHEMA)
        return {"id": c.id, "doc": c.doc, "fields": (r.data or {}).get("fields") or [],
                "related": (r.data or {}).get("related") or [], "usage": r.usage()}

    t0 = time.time()
    try:
        todo = []
        for cs in by_doc.values():
            for i, c in enumerate(cs):
                if c.id not in done or done[c.id]["usage"].get("error"):
                    todo.append(one(c, cs[i - 1] if i else None, cs[i + 1] if i + 1 < len(cs) else None))
        results = await asyncio.gather(*todo)
    finally:
        await client.aclose()
    for r in results:
        done[r["id"]] = r
    pub, loc = paths(chunks_name, variant)
    for p in (pub, loc):
        p.parent.mkdir(parents=True, exist_ok=True)
    rows = [done[c.id] for c in chunks if c.id in done]
    pub.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows if docs[r["doc"]].redistribute),
                   encoding="utf-8", newline="\n")
    loc.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows if not docs[r["doc"]].redistribute),
                   encoding="utf-8", newline="\n")
    summary = {"chunks": chunks_name, "variant": variant, "model": MODEL, "called": len(results),
               "seconds_wall": round(time.time() - t0, 1),
               "cost_usd": round(sum(r["usage"]["cost_usd"] for r in results), 4),
               "errors": sum(1 for r in rows if r["usage"].get("error")),
               "empty": sum(1 for r in rows if not r["fields"]),
               "with_related": sum(1 for r in rows if r["related"])}
    (OUT / f"{pub.stem}.meta.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8",
                                               newline="\n")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("chunks")
    ap.add_argument("--variant", choices=list(EXAMPLES), default="full")
    args = ap.parse_args()
    print(json.dumps(asyncio.run(build(args.chunks, args.variant)), ensure_ascii=False))


if __name__ == "__main__":
    main()
