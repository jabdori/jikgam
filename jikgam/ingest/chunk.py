"""문서 처리 2단계: 문맥 분할(docs/evaluation-v2.md §3.2, §3.6).

구간은 문서 안의 이어진 줄 범위다. 구간 파일은 위치(문서, 줄, 글자 범위, 토큰 수)만 담고 원문은 담지 않는다.
원문은 말뭉치(jikgam/ingest/corpus.py)에서 위치로 꺼낸다. 그래서 표준약관의 구간 파일도 저장소에 둘 수 있다.

- fixed: (a) 고정 길이. 정한 토큰 수마다 자르되 가장 가까운 줄 경계로 옮기고, 이웃 구간과 10% 겹친다.
- article: 기준선 R0. 줄 머리의 "제N조(" 또는 "제N조의M(" 제목 줄에서 나눈다. 조 제목이 없는 부분(별표, 앞머리)은
  (a)와 같은 길이로 자른다. 이 규칙은 기준선에만 쓴다.
- (b)(c) 참조 방식은 jikgam/ingest/window.py에 있다.
"""

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from jikgam.ingest import tokens
from jikgam.ingest.corpus import ROOT, Doc

OUT = ROOT / "data" / "v2" / "chunks"
OVERLAP = 0.10
ARTICLE_HEAD = re.compile(r"^제(\d+)조(?:의(\d+))?\s*\(")
# 본문의 "제N조" 언급. 앞에 다른 법령을 가리키는 말("법 ", "영 ", 「」로 묶은 이름)이 있으면 같은 문서의 참조가 아니다
ARTICLE_REF = re.compile(r"제(\d+)조(?:의(\d+))?")
OTHER_LAW_BEFORE = re.compile(r"(?:(?:^|[\s(])(?:법|영|규칙)\s?|」\s?)$")
TABLE_OF = re.compile(r"\(제(\d+)조(?:의(\d+))?(?:제\d+항)?(?:제\d+호)?\s*관련\)")


@dataclass
class Chunk:
    id: str
    doc: str
    start_line: int  # 첫 줄 번호(1부터)
    end_line: int  # 끝 줄 번호(포함)
    start: int  # 문서 글의 글자 위치 [start, end)
    end: int
    tokens: int
    kind: str = "fixed"  # fixed | article | window
    article: str | None = None  # R0: "제5조", 같은 번호가 다시 나오면 "제1조#2"
    refs: list[str] = field(default_factory=list)  # R0: 같은 문서에서 참조하는 조
    table_of: str | None = None  # R0: 별표가 관련된 조("(제N조 관련)")


def line_tokens(doc: Doc) -> list[int]:
    return tokens.counts([line.text + "\n" for line in doc.lines])


def span(doc: Doc, a: int, b: int) -> tuple[int, int]:
    """줄 번호 a~b(포함)의 글자 범위."""
    first, last = doc.lines[a - 1], doc.lines[b - 1]
    return first.start, last.start + len(last.text)


def fixed_ranges(lt: list[int], lo: int, hi: int, size: int) -> list[tuple[int, int]]:
    """줄 lo~hi(1부터, 포함)를 size토큰 안팎으로 자른다. 끝은 size에 가장 가까운 줄 경계, 다음 시작은 10% 겹치게."""
    out = []
    s = lo
    while s <= hi:
        acc, e = 0, s
        while e <= hi:
            acc += lt[e - 1]
            if acc >= size:
                # 이 줄까지 넣은 것과 빼고 자른 것 중 size에 가까운 쪽
                if e > s and (acc - size) > (size - (acc - lt[e - 1])):
                    acc -= lt[e - 1]
                    e -= 1
                break
            e += 1
        e = min(e, hi)
        out.append((s, e))
        if e >= hi:
            break
        # 겹침: 끝에서 거꾸로 OVERLAP × size에 가장 가까운 줄까지
        back, ns = 0, e + 1
        while ns - 1 > s:
            nxt = back + lt[ns - 2]
            if abs(nxt - OVERLAP * size) > abs(back - OVERLAP * size) and back > 0:
                break
            back, ns = nxt, ns - 1
            if back >= OVERLAP * size:
                break
        s = max(ns, s + 1)
    return out


def make(doc: Doc, method: str, a: int, b: int, lt: list[int], k: int, **kw) -> Chunk:
    st, en = span(doc, a, b)
    return Chunk(f"{method}:{doc.id}:{k:04d}", doc.id, a, b, st, en, sum(lt[a - 1:b]), **kw)


def fixed(doc: Doc, size: int, lt: list[int] | None = None) -> list[Chunk]:
    lt = lt or line_tokens(doc)
    method = f"fixed{size}"
    return [make(doc, method, a, b, lt, k) for k, (a, b) in enumerate(fixed_ranges(lt, 1, len(doc.lines), size))]


def refs_in(text: str) -> list[str]:
    out = []
    for m in ARTICLE_REF.finditer(text):
        if OTHER_LAW_BEFORE.search(text[max(0, m.start() - 12):m.start()]):
            continue
        ref = f"제{m.group(1)}조" + (f"의{m.group(2)}" if m.group(2) else "")
        if ref not in out:
            out.append(ref)
    return out


def article(doc: Doc, size: int, lt: list[int] | None = None) -> list[Chunk]:
    """기준선 R0: 조 단위 + 조가 없는 부분은 size토큰 고정 길이(§3.6). size 0은 R0-원형(조가 없는 부분을 파일마다 한 구간)."""
    lt = lt or line_tokens(doc)
    method = f"article{size}"
    size = size if size > 0 else 10 ** 9
    heads = []  # (줄 번호, 조 이름)
    seen: dict[str, int] = {}
    for line in doc.lines:
        m = ARTICLE_HEAD.match(line.text)
        if m:
            name = f"제{m.group(1)}조" + (f"의{m.group(2)}" if m.group(2) else "")
            seen[name] = seen.get(name, 0) + 1
            heads.append((line.no, name if seen[name] == 1 else f"{name}#{seen[name]}"))
    chunks: list[Chunk] = []
    n = len(doc.lines)

    def filler(a: int, b: int) -> None:
        # 조가 없는 줄은 파일(본문, 별표 n)마다 따로 고정 길이로 자른다
        cur = a
        while cur <= b:
            f = doc.lines[cur - 1].file
            end = cur
            while end + 1 <= b and doc.lines[end].file == f:
                end += 1
            table = None
            head = " ".join(x.text for x in doc.lines[cur - 1:min(end, cur + 2)])
            if f.startswith("별표") and (m := TABLE_OF.search(head)):
                table = f"제{m.group(1)}조" + (f"의{m.group(2)}" if m.group(2) else "")
            for x, y in fixed_ranges(lt, cur, end, size):
                chunks.append(make(doc, method, x, y, lt, len(chunks), kind="fixed", table_of=table))
            cur = end + 1

    prev_end = 0
    for i, (no, name) in enumerate(heads):
        if no > prev_end + 1:
            filler(prev_end + 1, no - 1)
        # 조는 다음 조 제목 앞까지다. 같은 파일이 끝나면(본문 → 별표) 거기서 멈춘다
        end = heads[i + 1][0] - 1 if i + 1 < len(heads) else n
        f = doc.lines[no - 1].file
        stop = no
        while stop + 1 <= end and doc.lines[stop].file == f:
            stop += 1
        st, en = span(doc, no, stop)
        refs = [r for r in refs_in(doc.text[st:en]) if r != name.split("#")[0]]
        chunks.append(make(doc, method, no, stop, lt, len(chunks), kind="article", article=name, refs=refs))
        if stop < end:
            filler(stop + 1, end)
        prev_end = end
    if prev_end < n:
        filler(prev_end + 1, n)
    return chunks


def write(method: str, chunks: list[Chunk], meta: dict) -> Path:
    path = OUT / f"{method}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(asdict(c), ensure_ascii=False) + "\n" for c in chunks), encoding="utf-8", newline="\n")
    (OUT / f"{method}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8",
                                             newline="\n")
    return path


def load(method: str) -> list[Chunk]:
    path = OUT / f"{method}.jsonl"
    return [Chunk(**json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    import argparse

    from jikgam.ingest.corpus import load_docs

    ap = argparse.ArgumentParser()
    ap.add_argument("method", choices=["fixed", "article"])
    ap.add_argument("--size", type=int, required=True)
    args = ap.parse_args()
    docs = load_docs()
    out: list[Chunk] = []
    for d in docs.values():
        lt = line_tokens(d)
        out += fixed(d, args.size, lt) if args.method == "fixed" else article(d, args.size, lt)
    name = f"fixed{args.size}" if args.method == "fixed" else f"article{args.size}"
    toks = sorted(c.tokens for c in out)
    meta = {"method": args.method, "size": args.size, "overlap": OVERLAP, "chunks": len(out),
            "tokens": {"mean": round(sum(toks) / len(toks), 1), "median": toks[len(toks) // 2], "max": toks[-1]},
            "tokenizer": list(tokens.TOKENIZER)}
    if args.method == "article":
        meta["articles"] = sum(c.kind == "article" for c in out)
    write(name, out, meta)
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
