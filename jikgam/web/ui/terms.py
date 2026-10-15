"""고객사 화면 금지어(docs/design/forbidden-terms.md의 표와 설정의 모델 이름). 테스트와 브라우저 검사가 함께 쓴다(ADR 0022)."""

import re
import tomllib
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from jikgam.engine.org import ROOT

DOC = ROOT / "docs/design/forbidden-terms.md"
VISIBLE_ATTRS = ("title", "aria-label", "placeholder", "alt", "value")
HIDDEN_TAGS = ("script", "style", "head", "title")


@dataclass(frozen=True)
class Term:
    word: str
    mode: str  # 부분, 낱말, 정규식
    pattern: re.Pattern


def _compile(word: str, mode: str) -> re.Pattern:
    if mode == "부분":
        return re.compile(re.escape(word))
    if mode == "낱말":
        return re.compile(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])", re.I)
    if mode == "정규식":
        return re.compile(word, re.I)
    raise ValueError(f"알 수 없는 방식: {mode}")


def load_terms(doc: Path = DOC, root: Path = ROOT) -> list[Term]:
    out = []
    in_table = False
    for line in doc.read_text(encoding="utf-8").splitlines():
        if line.startswith("| 낱말 |"):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            cells = [c.strip() for c in line.strip("|").split("|")]
            if set(cells[0]) <= {"-"}:
                continue
            word = cells[0].strip("`")
            out.append(Term(word, cells[1], _compile(word, cells[1])))
    models = set()
    for p in [root / "config/decision_models.toml", *sorted((root / "config/decision_models.d").glob("*.toml")),
              root / "config/voice.toml"]:
        if p.is_file():
            for v in tomllib.loads(p.read_text(encoding="utf-8")).values():
                if isinstance(v, dict) and isinstance(v.get("model"), str):
                    models.add(v["model"])
    out += [Term(m, "낱말", _compile(m, "낱말")) for m in sorted(models)]
    return out


class _Visible(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in HIDDEN_TAGS:
            self.hidden += 1
        for k, v in attrs:
            if k in VISIBLE_ATTRS and v and not (k == "value" and tag == "input" and dict(attrs).get("type") == "hidden"):
                self.parts.append(v)

    def handle_endtag(self, tag: str) -> None:
        if tag in HIDDEN_TAGS and self.hidden:
            self.hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def visible_text(html: str) -> str:
    p = _Visible()
    p.feed(html)
    return "\n".join(p.parts)


def find(text: str, terms: list[Term]) -> list[tuple[str, str]]:
    """(금지어, 걸린 글자 주변) 목록."""
    hits = []
    for t in terms:
        for m in t.pattern.finditer(text):
            a, b = max(0, m.start() - 20), m.end() + 20
            hits.append((t.word, text[a:b].replace("\n", " ")))
    return hits
