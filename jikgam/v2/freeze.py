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


def write_manifest(root=ROOT, adr="0025", paths=None, contract=None):
    if contract is not None:
        from jikgam.v2.e2e_contract import contract_files
        paths = sorted(set(e2e_input_paths(root) if paths is None else paths) | contract_files(contract))
    paths = input_paths(root) if paths is None else sorted(paths)
    missing = [p for p in paths if not (root / p).is_file()]
    if missing:
        raise FileNotFoundError("\n".join(missing))
    manifest = {"adr": adr, "algorithm": "sha256", "files": {p: sha256(root / p) for p in paths}}
    if contract is not None:
        manifest["contract"] = contract
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
    paths.add("domain/org_a/customers_holdout.json")
    # 두 번째 v2 미공개 묶음과 그 기준 문서(있을 때). 나중에 생기면 목록이 달라져 verify가 감지한다
    paths.update(p for p in ("docs/evaluation-v2-r5.md", "data/scenarios/org_a_v2b_holdout.jsonl",
                             "domain/org_a/customers_v2b_holdout.json") if (root / p).is_file())
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
        for profile in ("r0", "r4", "r5"):
            if profile not in cfg:
                continue
            name = cfg[profile]["chunks"]
            paths.add(f"data/v2/chunks/{name}.jsonl")
            if cfg[profile].get("meta", "none") != "none":
                for base in ("data/v2/meta", "data/v2/local/meta"):
                    paths.add(f"{base}/{name}.jsonl")
            for key in ("labels", "topic_question_set"):
                if cfg[profile].get(key):
                    paths.add(cfg[profile][key])
    summary = root / "data/v2/extracted/summary.json"
    paths.add("data/v2/extracted/summary.json")
    if summary.exists():
        paths.update(d["output"] for d in json.loads(summary.read_text(encoding="utf-8"))["docs"].values())
    return sorted(paths)


def require_e2e_frozen(scenario_file, adr, root=ROOT, args=None):
    """미공개 v2는 파일을 열기 전에 ADR·전체 고정 해시를 확인한다. 목록은 고정 시 따로 만든다."""
    from pathlib import Path
    import re

    from jikgam.v2.e2e_contract import HOLDOUTS

    target = Path(scenario_file).resolve()
    holdouts = {(root / p).resolve(): p for p in HOLDOUTS}
    names = {p.name.casefold() for p in holdouts}
    requested = adr is not None
    if not requested and target not in holdouts and target.name.casefold() not in names:
        if "holdout" in target.name.casefold() and "v2" in target.name.casefold():
            raise SystemExit("미공개 v2 파일은 정확한 저장소 경로만 허용합니다")
        return
    if not requested and target not in holdouts:
        raise SystemExit("미공개 v2 파일은 정확한 저장소 경로만 허용합니다")
    if not adr or not re.fullmatch(r"\d{4}", adr):
        raise SystemExit("미공개 v2 실행에는 설정 고정 ADR 번호(--frozen)가 필요합니다")
    if not any((root / "docs/adr").glob(f"{adr}-*.md")):
        raise SystemExit(f"설정 고정 ADR {adr}를 찾을 수 없습니다")
    path = root / f"config/v2/freeze-{adr}.json"
    if not path.is_file():
        raise SystemExit(f"설정 고정 해시 목록이 없습니다: {path.relative_to(root)}")
    verification = {"adr": adr, "contract_match": None, "manifest_match": None, "scenario_path_match": None}
    if args is not None:
        args.frozen_verification = verification
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("adr") != adr or manifest.get("algorithm") != "sha256":
            raise ValueError("ADR 번호나 해시 알고리즘이 다릅니다")
        from jikgam.v2.e2e_contract import build_contract, contract_files
        if args is None or not isinstance(manifest.get("contract"), dict):
            raise ValueError("정규화한 실행 계약이 필요합니다")
        if getattr(args, "law", None) not in ("r0", "r4", "r5") or getattr(args, "no_judge", False):
            raise ValueError("미공개 v2는 R0·R4·R5 가운데 하나와 시나리오 판정이 필요합니다")
        frozen_scenario = manifest["contract"].get("arguments", {}).get("scenario_file")
        holdout_contract = frozen_scenario in HOLDOUTS
        verification["scenario_path_match"] = not holdout_contract or target == (root / frozen_scenario).resolve()
        frozen_paths = set(e2e_input_paths(root)) | contract_files(manifest["contract"])
        changed = verify_manifest(root, adr, paths=frozen_paths)
        verification["manifest_match"] = not changed
        verification["manifest_sha256"] = sha256(path)
        if not verification["scenario_path_match"]:
            verification["contract_match"] = False
            raise ValueError("미공개 고정 계약은 정확한 저장소 경로만 허용합니다")
        contract = build_contract(args, root)
        verification["contract_match"] = contract == manifest["contract"]
        if not verification["contract_match"]:
            raise ValueError("실행 계약의 인자 또는 선택 파일 해시가 다릅니다")
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise SystemExit(f"설정 고정 목록이 올바르지 않습니다: {exc}") from None
    finally:
        # 거절된 사전 검사도 비공개 기록으로 남겨 계약 비교와 목록 대조를 구분한다.
        run_id = getattr(args, "run_id", None)
        if run_id and Path(run_id).name == run_id and run_id not in (".", ".."):
            from jikgam.v2.claim_check import atomic_json
            atomic_json(root / "data/v2/local/e2e" / run_id / "frozen-verification.json", verification)
    if changed:
        raise SystemExit("고정 해시가 다르거나 입력이 빠졌습니다: " + ", ".join(changed))
    return contract


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prepare", "write", "verify"])
    ap.add_argument("--adr", required=True)
    ap.add_argument("--e2e", action="store_true", help="엔드투엔드 입력과 실행 계약을 고정")
    ap.add_argument("--contract-args", help="실행 인자를 담은 비공개 JSON 파일")
    args = ap.parse_args()
    import re
    if not re.fullmatch(r"\d{4}", args.adr):
        ap.error("ADR 번호는 네 자리 숫자여야 합니다")
    contract = None
    paths = None
    if args.e2e:
        if not args.contract_args:
            ap.error("--e2e에는 --contract-args가 필요합니다")
        from types import SimpleNamespace
        from pathlib import Path
        from jikgam.v2.e2e_contract import build_contract, contract_files
        contract = build_contract(SimpleNamespace(**json.loads(Path(args.contract_args).read_text(encoding="utf-8"))))
        paths = sorted(set(e2e_input_paths()) | contract_files(contract))
    if args.cmd == "prepare":
        out = prepare()
        print(json.dumps(out, ensure_ascii=False))
        raise SystemExit(bool(out["missing_gemini"]))
    if args.cmd == "write":
        try:
            write_manifest(ROOT, args.adr, paths=paths, contract=contract)
        except FileNotFoundError as exc:
            print(str(exc))
            raise SystemExit(1) from None
    else:
        changed = verify_manifest(ROOT, args.adr, paths=paths)
        if contract is not None:
            manifest = json.loads((ROOT / f"config/v2/freeze-{args.adr}.json").read_text(encoding="utf-8"))
            if manifest.get("contract") != contract:
                changed.append("contract")
        for path in changed:
            print(path)
        raise SystemExit(bool(changed))


if __name__ == "__main__":
    main()
