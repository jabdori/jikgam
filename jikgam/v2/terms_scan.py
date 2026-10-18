"""표준약관의 25자 줄 조각이 결과에 포함되었는지 검사한다. 원문은 출력하지 않는다."""

import argparse
import json
from pathlib import Path

from jikgam.ingest.corpus import load_docs


def fragments(docs):
    return {line.text[i:i + 25] for doc in docs.values() for line in doc.lines
            for i in range(max(0, len(line.text) - 24))}


def fragment_sets(docs, org_text=""):
    """공개 법령·고시나 조직 A 약관에도 있는 조각을 공백 없이 비교해 따로 센다."""
    terms = {k: d for k, d in docs.items() if k in ("game-terms", "ecom-terms")}
    public = ["".join(d.text.split()) for k, d in docs.items() if k not in terms]
    public.append("".join(org_text.split()))
    unique, shared = set(), set()
    for part in fragments(terms):
        normalized = "".join(part.split())
        (shared if any(normalized in text for text in public) else unique).add(part)
    return unique, shared


def load_fragment_sets():
    from jikgam.engine.org import load_org

    return fragment_sets(load_docs(), load_org("org_a").terms.read_text(encoding="utf-8"))


def scan(paths, needles):
    found = []
    for directory in paths:
        if not directory.exists():
            raise FileNotFoundError(str(directory))
        files = [directory] if directory.is_file() else sorted(p for p in directory.rglob("*") if p.is_file())
        for path in files:
            text = path.read_text(encoding="utf-8", errors="replace")
            # JSON에서 이스케이프된 줄바꿈·한글도 검사한다.
            try:
                text += "\n" + json.dumps(json.loads(text), ensure_ascii=False)
            except (ValueError, TypeError):
                for line in text.splitlines():
                    try:
                        text += "\n" + json.dumps(json.loads(line), ensure_ascii=False)
                    except (ValueError, TypeError):
                        pass
            count = sum(part in text for part in needles)
            if count:
                found.append({"file": str(path), "count": count})
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+", type=Path)
    args = ap.parse_args()
    unique, shared = load_fragment_sets()
    found, common = scan(args.paths, unique), scan(args.paths, shared)
    print(json.dumps({"files": found, "count": sum(r["count"] for r in found), "shared_files": common,
                      "shared_count": sum(r["count"] for r in common)}, ensure_ascii=False))
    raise SystemExit(bool(found))


if __name__ == "__main__":
    main()
