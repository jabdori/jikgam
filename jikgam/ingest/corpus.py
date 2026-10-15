"""v2 말뭉치: 텍스트 추출 결과를 문서별 줄과 글로 읽는다(ADR 0023, docs/evaluation-v2.md §2).

문서의 줄은 추출 결과(JSONL)의 글 덩이를 차례로 줄바꿈에서 나눈 것이다. 줄 번호는 문서마다 1부터 센다.
문서의 글은 그 줄들을 줄바꿈 하나로 이은 것이고, 원문 위치(글자 범위)는 이 글의 글자 위치다.
분할·검색·평가 세트가 모두 이 정의를 쓴다.

원문 찾기(find_quote)는 공백과 표 구분자 "|"를 무시하고 비교한다(docs/evaluation-v2.md §2.3).
"""

import json
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUMMARY = ROOT / "data" / "v2" / "extracted" / "summary.json"

# 문서 종류 → 보고 묶음(docs/evaluation-v2.md §2.1)
GROUP_OF_KIND = {"법률": "법령", "시행령": "법령", "시행규칙": "법령", "고시": "고시", "표준약관": "표준약관"}
GROUPS = ("법령", "고시", "표준약관")
IGNORED = frozenset(" \t\n\r\f\v 　|")


@dataclass(frozen=True)
class Line:
    no: int  # 문서 안의 줄 번호(1부터)
    file: str  # "본문", "별표 1" 같은 파일 이름
    page: int
    kind: str  # "text" | "table"
    text: str
    start: int  # 문서 글에서 이 줄이 시작하는 글자 위치


@dataclass
class Doc:
    id: str
    title: str
    kind: str
    redistribute: bool
    lines: list[Line]

    @property
    def group(self) -> str:
        return GROUP_OF_KIND[self.kind]

    @cached_property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @cached_property
    def _squashed(self) -> tuple[str, list[int]]:
        return squash(self.text)

    def line_at(self, pos: int) -> Line:
        """글자 위치 pos가 든 줄(줄바꿈 글자는 앞 줄로 센다)."""
        lo, hi = 0, len(self.lines) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.lines[mid].start <= pos:
                lo = mid
            else:
                hi = mid - 1
        return self.lines[lo]

    def find_quote(self, quote: str) -> list[tuple[int, int]]:
        """quote가 나오는 모든 원문 위치 [시작, 끝). 공백과 "|"는 무시하고, 겹치지 않게 앞에서부터 찾는다."""
        needle, _ = squash(quote)
        if not needle:
            return []
        hay, pos = self._squashed
        spans = []
        i = hay.find(needle)
        while i >= 0:
            spans.append((pos[i], pos[i + len(needle) - 1] + 1))
            i = hay.find(needle, i + len(needle))
        return spans


def squash(text: str) -> tuple[str, list[int]]:
    """무시하는 글자를 뺀 글과, 남은 글자마다 원래 위치."""
    kept, pos = [], []
    for i, c in enumerate(text):
        if c not in IGNORED:
            kept.append(c)
            pos.append(i)
    return "".join(kept), pos


def nonspace_len(text: str) -> int:
    return sum(c not in IGNORED for c in text)


def _read(path: Path) -> list[Line]:
    lines, start = [], 0
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            block = json.loads(raw)
            for text in block["text"].split("\n"):
                lines.append(Line(len(lines) + 1, block["file"], block["page"], block["kind"], text, start))
                start += len(text) + 1
    return lines


def load_docs(only: set[str] | None = None) -> dict[str, Doc]:
    """summary.json의 순서대로 문서를 읽는다. 저장소 밖 문서(표준약관)는 받은 PC에만 있다."""
    summary = json.loads(SUMMARY.read_text(encoding="utf-8"))["docs"]
    docs = {}
    for did, s in summary.items():
        if only is not None and did not in only:
            continue
        path = ROOT / s["output"]
        if not path.exists():
            raise FileNotFoundError(f"{did}: {s['output']}이 없다(저장소 밖 문서는 받은 PC에서만 읽는다)")
        docs[did] = Doc(did, s["title"], s["kind"], s["redistribute"], _read(path))
    return docs
