"""원본 로그의 비공개 보존과 공개 사본. 판정은 해시를 확인한 원본을 읽는다."""

import argparse
import hashlib
import json
from pathlib import Path

from jikgam.engine.org import ROOT
from jikgam.v2 import terms_scan

MANIFEST = "private-logs.json"
MASK = "[표준약관 원문 생략]"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def hide(text, needles):
    """일치한 조각들이 겹치거나 붙으면 하나의 구간으로 가린다."""
    spans = []
    for part in needles:
        start = text.find(part)
        while start >= 0:
            spans.append((start, start + len(part)))
            start = text.find(part, start + 1)
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    for start, end in reversed(merged):
        text = text[:start] + MASK + text[end:]
    return text, len(merged), len(spans)


def hide_value(value, needles):
    if isinstance(value, str):
        return hide(value, needles)
    if isinstance(value, (list, dict)):
        result = [] if isinstance(value, list) else {}
        regions = pieces = 0
        for key, v in enumerate(value) if isinstance(value, list) else value.items():
            changed, n, p = hide_value(v, needles)
            if isinstance(result, list):
                result.append(changed)
            else:
                result[key] = changed
            regions += n
            pieces += p
        return result, regions, pieces
    return value, 0, 0


def publish(run_dir, needles, root=ROOT):
    """실행을 마친 폴더만 처리한다. 재실행은 보존된 원본을 다시 써서 멱등적이다."""
    run_dir = run_dir.resolve()
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    if summary.get("ended") is None:
        raise ValueError("진행 중인 실행의 로그는 공개 사본으로 바꾸지 않습니다")
    run_id = summary["run_id"]
    if Path(run_id).name != run_id or run_id in (".", ".."):
        raise ValueError("실행 이름에 경로가 있습니다")
    dest = root / "data/v2/local/e2e" / run_id / "logs"
    dest.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {
        "version": 1, "run_id": run_id, "files": {}}
    files = [p for p in run_dir.iterdir() if p.is_file() and p.suffix in (".json", ".jsonl") and p.name != MANIFEST]
    for path in sorted(files):
        private = dest / path.name
        old = manifest["files"].get(path.name)
        raw = private.read_bytes() if private.exists() else path.read_bytes()
        if old and sha(raw) != old["sha256"]:
            raise ValueError(f"보존 원본 해시가 다릅니다: {path.name}")
        text = raw.decode("utf-8")
        if path.suffix == ".jsonl":
            values = [json.loads(line) for line in text.splitlines() if line.strip()]
            changed, regions, pieces = hide_value(values, needles)
            public = "".join(json.dumps(v, ensure_ascii=False) + "\n" for v in changed)
        else:
            changed, regions, pieces = hide_value(json.loads(text), needles)
            public = json.dumps(changed, ensure_ascii=False, indent=1) + "\n"
        if not regions:
            public = text
        if not private.exists():
            private.write_bytes(raw)
        manifest["files"][path.name] = {"sha256": sha(raw), "redacted_regions": regions,
                                         "redacted_fragments": pieces}
        # 먼저 출처를 기록해, 중단되더라도 사본을 원본으로 오인하지 않게 한다.
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        path.write_bytes(public.encode("utf-8"))
    found = terms_scan.scan([run_dir], needles)
    if found:
        raise ValueError("공개 사본에 표준약관 원문이 남았습니다: " + json.dumps(found, ensure_ascii=False))
    return {"files": len(manifest["files"]),
            "redacted_regions": sum(v["redacted_regions"] for v in manifest["files"].values()),
            "redacted_fragments": sum(v["redacted_fragments"] for v in manifest["files"].values())}


def original_path(path, root=ROOT):
    path = Path(path)
    mp = path.parent / MANIFEST
    if not mp.exists():
        return path
    manifest = json.loads(mp.read_text(encoding="utf-8"))
    entry = manifest["files"].get(path.name)
    if not entry:
        return path
    run_id = manifest["run_id"]
    if Path(run_id).name != run_id or run_id in (".", ".."):
        raise ValueError("원본 로그의 실행 이름이 올바르지 않습니다")
    private = root / "data/v2/local/e2e" / run_id / "logs" / path.name
    if private.is_file():
        if sha(private.read_bytes()) != entry["sha256"]:
            raise ValueError(f"원본 로그 해시가 다릅니다: {path.name}")
        return private
    if entry["redacted_regions"]:
        raise FileNotFoundError(f"가려진 사본으로는 판정할 수 없습니다. 비공개 원본이 필요합니다: {path.name}")
    if sha(path.read_bytes()) != entry["sha256"]:
        # 무가림 사본도 직렬화가 달라질 수 있으므로 원본 파일을 복원해야 한다.
        raise FileNotFoundError(f"원본과 같은 사본이 아닙니다: {path.name}")
    return path


def read_text(path, root=ROOT):
    return original_path(path, root).read_text(encoding="utf-8")


def read_log(path, root=ROOT):
    return [json.loads(line) for line in read_text(path, root).splitlines() if line.strip()]


def store_json(path, value, root=ROOT):
    """재판정 전 판본을 비공개로 보존하고 원본·공개 사본·해시를 함께 갱신한다."""
    path = Path(path)
    mp = path.parent / MANIFEST
    manifest = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else None
    run_id = manifest["run_id"] if manifest else path.parent.name
    if Path(run_id).name != run_id or run_id in (".", ".."):
        raise ValueError("판정 기록의 실행 이름이 올바르지 않습니다")
    dest = root / "data/v2/local/e2e" / run_id / "logs"
    old = original_path(path, root) if path.exists() else None
    if old:
        raw = old.read_bytes()
        history = dest / "history" / f"{sha(raw)}-{path.name}"
        history.parent.mkdir(parents=True, exist_ok=True)
        if not history.exists():
            history.write_bytes(raw)
    text = json.dumps(value, ensure_ascii=False, indent=1) + "\n"
    if not manifest:
        path.write_bytes(text.encode("utf-8"))
        return
    from jikgam.ingest.corpus import load_docs

    needles = terms_scan.fragments(load_docs({"game-terms", "ecom-terms"}))
    safe, regions, pieces = hide_value(value, needles)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / path.name).write_bytes(text.encode("utf-8"))
    manifest["files"][path.name] = {"sha256": sha(text.encode("utf-8")), "redacted_regions": regions,
                                     "redacted_fragments": pieces}
    mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    public = json.dumps(safe, ensure_ascii=False, indent=1) + "\n" if regions else text
    path.write_bytes(public.encode("utf-8"))


def main():
    from jikgam.ingest.corpus import load_docs

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_dir", type=Path)
    args = ap.parse_args()
    print(json.dumps(publish(args.run_dir, terms_scan.fragments(load_docs({"game-terms", "ecom-terms"}))),
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
