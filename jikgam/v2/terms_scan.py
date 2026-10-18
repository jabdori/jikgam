"""표준약관의 출처 본문·공백 제거 조각·짧은 고유 줄을 검사한다. 원문은 출력하지 않는다."""

import argparse
import json
import re
from functools import lru_cache
from pathlib import Path

from jikgam.ingest.corpus import load_docs

TERMS = ("game-terms", "ecom-terms")
MASK = "[표준약관 원문 생략]"
BODY_FIELDS = {"text", "body", "content", "page_content"}


def normalize(text):
    return "".join(text.split())


class FragmentSet(set):
    def __init__(self, values=()):
        super().__init__(normalize(v) for v in values if normalize(v))
        self.lengths = sorted({len(v) for v in self})
        self.prefix_lengths = {}
        self.prefix_size = min(4, min(self.lengths, default=4))
        for value in self:
            self.prefix_lengths.setdefault(value[:self.prefix_size], set()).add(len(value))


def normalized_matches(plain, needles):
    for i in range(len(plain)):
        for size in needles.prefix_lengths.get(plain[i:i + needles.prefix_size], ()):
            part = plain[i:i + size]
            if part in needles:
                yield part, i, i + size


def matches(text, needles):
    """정규화 위치를 원래 문자열 위치로 돌린다. 가리기와 검사에 같은 정의를 쓴다."""
    needles = needles if isinstance(needles, FragmentSet) else FragmentSet(needles)
    positions = [i for i, c in enumerate(text) if not c.isspace()]
    plain = "".join(text[i] for i in positions)
    for part, i, end in normalized_matches(plain, needles):
        yield part, positions[i], positions[end - 1] + 1


@lru_cache(maxsize=1)
def source_documents():
    """기존 로그처럼 doc 필드 없이 구간 ID만 있어도 출처를 복원한다."""
    from jikgam.engine.org import ROOT

    out = {}
    for path in (ROOT / "data/v2/chunks").glob("*.jsonl"):
        if path.name.endswith(".calls.jsonl"):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("doc") in TERMS and isinstance(row.get("id"), str):
                out[row["id"]] = row["doc"]
    return out


def terms_object(value):
    if not isinstance(value, dict):
        return False
    for key in ("doc", "document", "source_doc", "doc_id"):
        if isinstance(value.get(key), str) and value[key] in TERMS:
            return True
    for key in ("id", "article_id", "chunk_id"):
        name = value.get(key)
        if isinstance(name, str) and (name in source_documents() or any(
                re.search(r"(?:^|[:/])" + re.escape(doc) + r"(?:$|[:/])", name) for doc in TERMS)):
            return True
    return False


def fragments(docs):
    out = set()
    for doc in docs.values():
        for line in doc.lines:
            text = normalize(line.text)
            if len(text) >= 25:
                out.update(text[i:i + 25] for i in range(len(text) - 24))
            elif len(text) >= 12:
                out.add(text)
    return out


def fragment_sets(docs, org_text=""):
    """공개 법령·고시나 조직 A 약관에도 있는 조각을 공백 없이 비교해 따로 센다."""
    terms = {k: d for k, d in docs.items() if k in ("game-terms", "ecom-terms")}
    public = ["".join(d.text.split()) for k, d in docs.items() if k not in terms]
    public.append("".join(org_text.split()))
    unique, shared = set(), set()
    for part in fragments(terms):
        normalized = "".join(part.split())
        (shared if any(normalized in text for text in public) else unique).add(part)
    return FragmentSet(unique), FragmentSet(shared)


def load_fragment_sets():
    from jikgam.engine.org import load_org

    return fragment_sets(load_docs(), load_org("org_a").terms.read_text(encoding="utf-8"))


def values(text):
    try:
        return [json.loads(text)]
    except (ValueError, TypeError):
        out = []
        for line in text.splitlines():
            try:
                out.append(json.loads(line))
            except (ValueError, TypeError):
                pass
        return out


def source_body_count(value):
    if isinstance(value, dict):
        return sum(1 for k, v in value.items() if k in BODY_FIELDS and terms_object(value)
                   and isinstance(v, str) and v.strip() and v != MASK) + sum(source_body_count(v) for v in value.values())
    return sum(source_body_count(v) for v in value) if isinstance(value, list) else 0


def string_values(value):
    """다시 직렬화하지 않고 JSON의 이스케이프를 푼 문자열 값을 순회한다."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from string_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from string_values(child)


def scan(paths, needles, check_sources=True):
    needles = needles if isinstance(needles, FragmentSet) else FragmentSet(needles)
    found = []
    for directory in paths:
        if not directory.exists():
            raise FileNotFoundError(str(directory))
        files = [directory] if directory.is_file() else sorted(p for p in directory.rglob("*") if p.is_file())
        for path in files:
            # 음성·그림·텐서의 이진 바이트를 문장으로 해석하지 않는다. 알 수 없는 확장자는 검사한다.
            if path.suffix.lower() in {".wav", ".mp3", ".mp4", ".png", ".jpg", ".jpeg", ".webp", ".pt", ".npy", ".npz"}:
                continue
            prefix = path.open("rb")
            with prefix:
                bom = prefix.read(4)
            text = path.read_text(encoding="utf-16" if bom[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig")
            parsed = values(text)
            # 원시 파일과 JSON 문자열 값을 각각 검사하여 실제 줄바꿈·탭도 잡는다.
            strings = [text, *(s for value in parsed for s in string_values(value))]
            count = len({part for s in strings for part, _, _ in normalized_matches(normalize(s), needles)})
            if check_sources:
                count += sum(source_body_count(v) for v in parsed)
            if count:
                found.append({"file": str(path), "count": count})
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+", type=Path)
    args = ap.parse_args()
    unique, shared = load_fragment_sets()
    found, common = scan(args.paths, unique), scan(args.paths, shared, check_sources=False)
    print(json.dumps({"files": found, "count": sum(r["count"] for r in found), "shared_files": common,
                      "shared_count": sum(r["count"] for r in common)}, ensure_ascii=False))
    raise SystemExit(bool(found))


if __name__ == "__main__":
    main()
