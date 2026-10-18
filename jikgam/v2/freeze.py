"""고정 입력의 전체 SHA-256을 기록하고 다시 대조한다. 글은 출력하지 않는다."""

import argparse
import hashlib
import json

from jikgam.ingest.corpus import ROOT
from jikgam.v2.report import B, CHUNKS

INPUTS = (
    "docs/evaluation-v2.md", "config/decision_models.toml", "config/v2/law.toml",
    "data/v2/qa/items.jsonl", "data/v2/qa/splits.json", "data/v2/qa/reviewed_passed.json",
    "data/v2/qa/repair.json", "data/v2/qa/generation.json",
    "data/v2/check/items.jsonl", "data/v2/check/generation.json", "data/v2/check/repair.json",
    "data/v2/boundary/segments.json", "data/v2/boundary/reference-astra.json", "data/v2/boundary/reference-sol.json",
    "data/v2/local/qa/items.jsonl", "data/v2/local/check/items.jsonl", "data/v2/extracted/summary.json",
)


def input_paths(root=ROOT) -> list[str]:
    """load_docs가 쓰는 추출 파일과, 판정·보고 구성에 필요한 파일을 모두 포함한다."""
    paths = set(INPUTS)
    for name in (*CHUNKS, "c-jev-W4000-C800-raw", "c-qwen-W4000-C800-raw"):
        paths.update(f"data/v2/chunks/{name}{ext}" for ext in (".jsonl", ".meta.json"))
    for name in ("c-jev-W4000-C800-raw", "c-qwen-W4000-C800-raw"):
        paths.add(f"data/v2/chunks/{name}.probs.json")
    for name in ("fixed256", B, "c-jev-W4000-C800-raw-t0.65", "c-qwen-W4000-C800-raw-t0.55", f"{B}--noexpr"):
        paths.update(f"data/v2/meta/{name}{ext}" for ext in (".jsonl", ".meta.json"))
        paths.add(f"data/v2/local/meta/{name}.jsonl")
    summary = root / "data/v2/extracted/summary.json"
    if summary.exists():
        for doc in json.loads(summary.read_text(encoding="utf-8"))["docs"].values():
            paths.add(doc["output"])
    # 저장소 밖 파일도 내용 대신 경로·해시만 남긴다. 새 캐시 파일의 추가도 verify에서 감지한다.
    for pattern in ("data/v2/local/meta/*.jsonl", "data/v2/local/meta/*.meta.json", "data/v2/local/emb/**/*"):
        paths.update(p.relative_to(root).as_posix() for p in root.glob(pattern) if p.is_file())
    return sorted(paths)


def sha256(path) -> str:
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def prepare():
    """질문을 읽지 않고 모든 구성의 말뭉치 캐시를 준비한다. Gemini는 절대 새로 부르지 않는다."""
    import gc
    from jikgam.v2.embed import EmbeddingIndex, cache_path, corpus_inputs
    from jikgam.v2.report import CONFIGS

    specs = set()
    for name in CONFIGS.values():
        if "--" not in name:
            continue
        chunks, variant, search = name.split("--")
        key = search.split("+")[0]
        if key in ("snow", "kure1", "kure2", "gemini"):
            specs.add((key, chunks, variant))
    missing, ready = [], []
    for key, chunks, variant in sorted(specs):
        ids, texts, titles = corpus_inputs(chunks, variant)
        tag = f"{chunks}--{variant}"
        path = cache_path(key, texts, tag)
        if not path.is_file():
            if key == "gemini":
                missing.append(str(path))
                continue
            index = EmbeddingIndex(key, ids, texts, titles, tag)
            del index
            gc.collect()
            import torch
            torch.cuda.empty_cache()
        ready.append(str(path))
    return {"ready": ready, "missing_gemini": missing}


def write_manifest(root=ROOT, adr="0025", paths=None):
    paths = input_paths(root) if paths is None else sorted(paths)
    missing = [p for p in paths if not (root / p).is_file()]
    if missing:
        raise FileNotFoundError("\n".join(missing))
    manifest = {"adr": adr, "algorithm": "sha256", "files": {p: sha256(root / p) for p in paths}}
    path = root / "config/v2" / f"freeze-{adr}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    return manifest


def verify_manifest(root=ROOT, adr="0025", paths=None) -> list[str]:
    path = root / "config/v2" / f"freeze-{adr}.json"
    if not path.is_file():
        return [path.relative_to(root).as_posix()]
    manifest = json.loads(path.read_text(encoding="utf-8"))
    frozen = manifest["files"]
    current = set(input_paths(root) if paths is None else paths)
    changed = set(frozen) ^ current
    for name in set(frozen) & current:
        file = root / name
        if not file.is_file() or sha256(file) != frozen[name]:
            changed.add(name)
    return sorted(changed)


def e2e_input_paths(root=ROOT):
    """검색 test 입력을 열지 않고 엔드투엔드 실행에 쓰는 고정 입력만 수집한다."""
    paths = {"docs/evaluation.md", "docs/evaluation-v2.md", "uv.lock", "pyproject.toml",
             "data/scenarios/org_a_v2.jsonl", "data/scenarios/org_a_v2_holdout.jsonl",
             "domain/org_a/customers.json", "domain/org_a/customers_v2_holdout.json"}
    for pattern in ("jikgam/engine/*.py", "jikgam/decision/*.py", "jikgam/sim/*.py", "jikgam/v2/*.py",
                    "jikgam/ingest/*.py", "jikgam/eval/*.py", "config/*.toml", "config/orgs/*.toml",
                    "config/v2/law.toml", "config/decision_models.d/*.toml", "domain/org_a/*.md",
                    "data/question_sets/**/*.json", "data/v2/local/emb/**/*", "bizapi/**/*.go",
                    "bizapi/go.mod", "bizapi/go.sum"):
        paths.update(p.relative_to(root).as_posix() for p in root.glob(pattern)
                     if p.is_file() and (not p.name.endswith("_test.go")))
    # 실행 구간·구간 정보·본문은 QA·점검용·참고 경계와 분리한다.
    import tomllib
    cfg_path = root / "config/v2/law.toml"
    if cfg_path.exists():
        cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
        for profile in ("r0", "r4"):
            name = cfg[profile]["chunks"]
            paths.add(f"data/v2/chunks/{name}.jsonl")
            if cfg[profile].get("meta", "none") != "none":
                for base in ("data/v2/meta", "data/v2/local/meta"):
                    paths.add(f"{base}/{name}.jsonl")
    summary = root / "data/v2/extracted/summary.json"
    paths.add("data/v2/extracted/summary.json")
    if summary.exists():
        paths.update(d["output"] for d in json.loads(summary.read_text(encoding="utf-8"))["docs"].values())
    return sorted(paths)


def require_e2e_frozen(scenario_file, adr, root=ROOT):
    """미공개 v2는 파일을 열기 전에 ADR·전체 고정 해시를 확인한다. 목록은 고정 시 따로 만든다."""
    from pathlib import Path
    import re

    target = Path(scenario_file).resolve()
    expected = (root / "data/scenarios/org_a_v2_holdout.jsonl").resolve()
    if target != expected and target.name.casefold() != expected.name.casefold():
        return
    if not adr or not re.fullmatch(r"\d{4}", adr):
        raise SystemExit("미공개 v2 실행에는 설정 고정 ADR 번호(--frozen)가 필요합니다")
    if not any((root / "docs/adr").glob(f"{adr}-*.md")):
        raise SystemExit(f"설정 고정 ADR {adr}를 찾을 수 없습니다")
    path = root / f"config/v2/freeze-{adr}.json"
    if not path.is_file():
        raise SystemExit(f"설정 고정 해시 목록이 없습니다: {path.relative_to(root)}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("adr") != adr or manifest.get("algorithm") != "sha256":
            raise ValueError("ADR 번호나 해시 알고리즘이 다릅니다")
        changed = verify_manifest(root, adr, paths=e2e_input_paths(root))
    except (ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"설정 고정 목록이 올바르지 않습니다: {exc}") from None
    if changed:
        raise SystemExit("고정 해시가 다르거나 입력이 빠졌습니다: " + ", ".join(changed))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prepare", "write", "verify"])
    ap.add_argument("--adr", required=True, choices=["0025"])
    args = ap.parse_args()
    if args.cmd == "prepare":
        out = prepare()
        print(json.dumps(out, ensure_ascii=False))
        raise SystemExit(bool(out["missing_gemini"]))
    if args.cmd == "write":
        try:
            write_manifest(ROOT, args.adr)
        except FileNotFoundError as exc:
            print(str(exc))
            raise SystemExit(1) from None
    else:
        changed = verify_manifest(ROOT, args.adr)
        for path in changed:
            print(path)
        raise SystemExit(bool(changed))


if __name__ == "__main__":
    main()
