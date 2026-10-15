"""문서 처리 1단계: 텍스트 추출(ADR 0023).

PDF에서 읽는 순서대로 글 덩이(Block)를 뽑는다. 문서 양식(조·항, 별표 번호 같은 것)을 전제하지 않는다.

- 머리말·꼬리말: 쪽의 맨 위·아래 두 줄 가운데, 같은 파일의 절반 이상 쪽에 되풀이되는 줄을 지운다.
  숫자는 같은 것으로 보고 센다(쪽 번호가 들어간 줄).
- 표: 선으로 그려진 표를 찾아 칸(열)을 나누고, 눈에 보이는 줄마다 "칸 | 칸 | 칸"으로 적는다. 한 칸 안에 여러 항목이
  들어 있어도 같은 높이의 글은 같은 줄에 놓이므로 옆 칸과의 대응이 남는다. 두 줄·두 칸이 안 되는 것은 표로 보지 않는다
  (글 배경을 칠한 상자를 표로 잘못 잡는 경우가 있다).
- 표 밖의 글은 PDF의 줄바꿈을 그대로 둔다. 단어 중간에서 끊긴 줄도 고치지 않는다.

입력은 data/v2/sources/manifest.json의 PDF다. 결과는 문서마다 JSONL 한 파일(글 덩이 한 줄)과 전체 요약(summary.json)이다.
다시 배포하지 않는 문서(redistribute = false)의 결과는 원본과 같은 data/v2/local/<문서>/에 둔다.

    uv run python -m jikgam.ingest.extract
    uv run python -m jikgam.ingest.extract --only dispute-std
"""

import argparse
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "data" / "v2" / "sources" / "manifest.json"
OUT = ROOT / "data" / "v2" / "extracted"
LOCAL = ROOT / "data" / "v2" / "local"

EDGE_LINES = 2  # 머리말·꼬리말 후보로 보는 쪽 위·아래 줄 수
REPEAT_SHARE = 0.5  # 이 비율 이상의 쪽에 되풀이되면 머리말·꼬리말로 본다
LINE_TOL = 3.0  # 같은 줄로 보는 글자 위치 차(pt)
X_TOL = 3.0  # 이보다 넓게 떨어진 글자 사이에 빈칸을 넣는다(pt, pdfplumber의 단어 나누기 기본값과 같다)
SEP = " | "


@dataclass
class Block:
    file: str  # "본문", "별표 1" 같은 파일 이름(manifest의 역할과 순번)
    page: int  # 파일 안의 쪽(1부터)
    kind: str  # "text" | "table"
    text: str


@dataclass
class FileStats:
    file: str
    pages: int
    chars: int = 0  # 뽑은 글자 수(공백 제외)
    tables: int = 0
    table_lines: int = 0
    tables_rejected: int = 0  # 표처럼 잡혔지만 두 줄·두 칸이 안 돼 버린 것
    removed: dict[str, int] = field(default_factory=dict)  # 지운 머리말·꼬리말(숫자는 #)과 지운 줄 수


def _norm(text: str) -> str:
    return re.sub(r"\d+", "#", " ".join(text.split()))


def _area(b: tuple) -> float:
    return (b[2] - b[0]) * (b[3] - b[1])


def _holds(b: tuple, x: float, y: float) -> bool:
    return b[0] - 1 <= x <= b[2] + 1 and b[1] - 1 <= y <= b[3] + 1


def _center(o: dict) -> tuple[float, float]:
    return (o["x0"] + o["x1"]) / 2, (o["top"] + o["bottom"]) / 2


def _tables(page) -> tuple[list[dict], int]:
    """쪽의 표. 다른 표 안에 들어간 표는 바깥 표에 합친다(칸은 모두 쓴다). 두 줄·두 칸이 안 되는 것은 버린다."""
    found = page.find_tables()
    kept = [t for t in found if len(t.rows) >= 2 and max(len(r.cells) for r in t.rows) >= 2]
    groups: list[dict] = []
    for t in sorted(kept, key=lambda t: _area(t.bbox), reverse=True):
        cells = [c for r in t.rows for c in r.cells if c is not None]
        outer = next((g for g in groups if all(_holds(g["bbox"], x, y) for x, y in
                                               ((t.bbox[0], t.bbox[1]), (t.bbox[2], t.bbox[3])))), None)
        if outer:
            outer["cells"] += cells
        else:
            groups.append({"bbox": t.bbox, "cells": cells})
    return sorted(groups, key=lambda g: g["bbox"][1]), len(found) - len(kept)


def _inside(line: dict, boxes: list[tuple]) -> bool:
    cx, cy = _center(line)
    return any(_holds(b, cx, cy) for b in boxes)


def table_lines(chars: list[dict], cells: list[tuple]) -> list[str]:
    """표 하나를 눈에 보이는 줄마다 적는다. 글자마다 그 글자를 담은 가장 작은 칸을 찾고, 같은 줄에서 칸이 바뀌는 곳에
    " | "를 넣는다. 칸 사이에 빈칸 없이 붙은 글자도 칸에 따라 나뉜다. 글자는 한 번만 쓴다."""
    lines: list[list[dict]] = []
    for ch in sorted(chars, key=lambda c: (c["top"], c["x0"])):
        if lines and abs(ch["top"] - lines[-1][0]["top"]) <= LINE_TOL:
            lines[-1].append(ch)
        else:
            lines.append([ch])
    out = []
    for line in lines:
        text, prev, end = "", None, None
        for ch in sorted(line, key=lambda c: c["x0"]):
            if not ch["text"].strip():
                continue
            cell = min((c for c in cells if _holds(c, *_center(ch))), key=_area, default=None)
            if prev is not None:
                if cell != prev:
                    text += SEP
                elif ch["x0"] - end > X_TOL:
                    text += " "
            text += ch["text"]
            prev, end = cell, ch["x1"]
        if text:
            out.append(text)
    return out


def extract_file(path: Path, name: str) -> tuple[list[Block], FileStats]:
    """PDF 파일 하나를 글 덩이로 뽑는다."""
    with pdfplumber.open(path) as pdf:
        stats = FileStats(file=name, pages=len(pdf.pages))
        pages = []  # 쪽마다 (표 밖의 줄, 표)
        for page in pdf.pages:
            tables, rejected = _tables(page)
            stats.tables_rejected += rejected
            boxes = [t["bbox"] for t in tables]
            lines = [ln for ln in page.extract_text_lines(layout=False, strip=True, return_chars=False)
                     if ln["text"].strip() and not _inside(ln, boxes)]
            lines.sort(key=lambda ln: ln["top"])
            owned: list[list[dict]] = [[] for _ in tables]  # 글자는 처음 담는 표 하나에만 들어간다
            for ch in page.chars if tables else []:
                i = next((i for i, b in enumerate(boxes) if _holds(b, *_center(ch))), None)
                if i is not None:
                    owned[i].append(ch)
            items = [(t["bbox"][1], "table", table_lines(owned[i], t["cells"])) for i, t in enumerate(tables)]
            pages.append((lines, items))

        # 머리말·꼬리말: 쪽마다 위·아래 줄의 모양을 한 번씩만 센다
        edge = Counter()
        for lines, _ in pages:
            edge.update({_norm(ln["text"]) for ln in lines[:EDGE_LINES] + lines[-EDGE_LINES:]})
        need = max(2, math.ceil(REPEAT_SHARE * len(pages)))
        repeated = {k for k, n in edge.items() if n >= need} if len(pages) >= 2 else set()

        blocks: list[Block] = []
        for pno, (lines, items) in enumerate(pages, 1):
            ends = {id(ln) for ln in lines[:EDGE_LINES] + lines[-EDGE_LINES:]}
            kept = []
            for ln in lines:
                key = _norm(ln["text"])
                if id(ln) in ends and key in repeated:
                    stats.removed[key] = stats.removed.get(key, 0) + 1
                    continue
                kept.append((ln["top"], "text", ln["text"]))
            # 읽는 순서: 위에서 아래로. 이어지는 글 줄은 한 덩이로 묶는다.
            for _, kind, body in sorted(kept + items, key=lambda x: x[0]):
                if kind == "table":
                    if body:
                        blocks.append(Block(name, pno, "table", "\n".join(body)))
                        stats.tables += 1
                        stats.table_lines += len(body)
                elif blocks and blocks[-1].kind == "text" and blocks[-1].page == pno and blocks[-1].file == name:
                    blocks[-1].text += "\n" + body
                else:
                    blocks.append(Block(name, pno, "text", body))
        stats.chars = sum(len(re.sub(r"\s", "", b.text.replace("|", ""))) for b in blocks)
        return blocks, stats


def file_names(files: list[dict]) -> list[tuple[str, dict]]:
    """manifest의 PDF 파일에 이름을 붙인다: 본문, 별표 1, 별표 2 …(원본 한글 파일은 뺀다)."""
    out, n = [], 0
    for f in files:
        if not f["path"].endswith(".pdf"):
            continue
        if f["role"] == "별표":
            n += 1
            out.append((f"별표 {n}", f))
        else:
            out.append((f["role"], f))
    return out


def run(only: set[str] | None = None) -> dict:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    summary_path = OUT / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {"docs": {}}
    for did, doc in manifest["docs"].items():
        if only and did not in only:
            continue
        blocks, stats = [], []
        for name, f in file_names(doc["files"]):
            b, s = extract_file(ROOT / f["path"], name)
            blocks += b
            stats.append(s)
        target = (OUT / f"{did}.jsonl") if doc["redistribute"] else (LOCAL / did / "extracted.jsonl")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="\n") as fh:
            for b in blocks:
                fh.write(json.dumps(asdict(b), ensure_ascii=False) + "\n")
        summary["docs"][did] = {
            "title": doc["title"], "kind": doc["kind"], "redistribute": doc["redistribute"],
            "output": target.relative_to(ROOT).as_posix(),
            "pages": sum(s.pages for s in stats), "chars": sum(s.chars for s in stats),
            "blocks": len(blocks), "tables": sum(s.tables for s in stats),
            "files": [asdict(s) for s in stats],
        }
        print(f"{did}: 쪽 {summary['docs'][did]['pages']}, 글자 {summary['docs'][did]['chars']:,}, "
              f"표 {summary['docs'][did]['tables']}, 덩이 {len(blocks)}")
    summary["docs"] = {d: summary["docs"][d] for d in manifest["docs"] if d in summary["docs"]}
    OUT.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="v2 문서 처리 1단계: 텍스트 추출")
    ap.add_argument("--only", default="", help="쉼표로 나눈 문서 id")
    args = ap.parse_args()
    run({x for x in args.only.split(",") if x} or None)


if __name__ == "__main__":
    main()
