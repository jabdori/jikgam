"""고정 실행의 성공 산출물을 검증하고 원자적 완료 표식으로 재개한다."""

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

from jikgam.ingest.corpus import ROOT
from jikgam.v2.freeze import sha256


def option(command, flag, default=None):
    return command[command.index(flag) + 1] if flag in command else default


def expected_scope(command):
    module = option(command, "-m", "")
    if module not in ("jikgam.v2.run", "jikgam.v2.answer", "jikgam.v2.claim_check"):
        return {}
    from jikgam.v2.evaluate import load_questions

    rows = load_questions(option(command, "--split", "test"), include_none=True)
    types = set(option(command, "--types", "single,none").split(",")) if module != "jikgam.v2.run" else None
    return {q["id"]: q["type"] for q in rows if types is None or q["type"] in types}


def artifacts(root, result, command):
    paths = [root / result]
    module = option(command, "-m", "")
    split = option(command, "--split", "test")
    if module == "jikgam.v2.run":
        paths.append(root / "data/v2/local/runs" / split / f"{Path(result).stem}.jsonl")
    elif module == "jikgam.v2.answer":
        paths.append(root / "data/v2/local/answers" / split / f"{option(command, '--run')}.jsonl")
    elif module == "jikgam.v2.claim_check":
        paths.append(root / "data/v2/local/review" / f"claims_{split}_sample.json")
    return paths


def source_paths(root, command):
    """앞 단계가 다시 만들어졌으면 그 결과를 쓰는 단계도 갱신한다."""
    module, split = option(command, "-m", ""), option(command, "--split", "test")
    if module == "jikgam.v2.answer":
        name = option(command, "--run")
        return [root / "results/v2" / split / f"{name}.json", root / "data/v2/local/runs" / split / f"{name}.jsonl"]
    if module == "jikgam.v2.claim_check":
        from jikgam.v2.claim_check import RUNS
        return [root / f"data/v2/local/{kind}" / split / f"{name}.jsonl" for name in RUNS for kind in ("answers", "runs")]
    return []


def validate(paths, command, expected):
    if not all(p.is_file() for p in paths):
        raise ValueError("필요한 산출물이 없습니다")
    module = option(command, "-m", "")
    if module not in ("jikgam.v2.run", "jikgam.v2.answer"):
        return
    result = json.loads(paths[0].read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in paths[1].read_text(encoding="utf-8").splitlines() if line.strip()]
    if module == "jikgam.v2.run":
        ids = [r["id"] for r in rows]
        found = result.get("per_question@2000", {})
        wanted = {q for q, t in expected.items() if t != "none"}
        if set(ids) != set(expected) or len(ids) != len(expected) or {q for d in found.values() for q in d} != wanted:
            raise ValueError("검색 산출물의 질문 집합이 완전하지 않습니다")
    else:
        reps = int(option(command, "--reps", "1"))
        pairs = [(r["id"], r["rep"]) for r in rows]
        wanted = {(q, n) for q in expected for n in range(reps)}
        if set(pairs) != wanted or len(pairs) != len(wanted) or result.get("questions") != len(expected) or result.get("reps") != reps:
            raise ValueError("답변 산출물의 질문·반복 집합이 완전하지 않습니다")


def run_stage(name, result, command, root=ROOT, head=None, expected=None, always=False):
    head = head or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    frozen = root / "config/v2/freeze-0025.json"
    scope = expected_scope(command) if expected is None else expected
    identity = {"command": command, "HEAD": head, "freeze_sha256": sha256(frozen),
                "expected_questions": len(scope),
                "inputs": [{"path": p.relative_to(root).as_posix(), "sha256": sha256(p)} for p in source_paths(root, command)],
                "expected_ids_sha256": hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()}
    done = root / "outputs/logs/v2-test/done" / f"{name}.json"
    paths = artifacts(root, result, command)
    if done.is_file() and not always:
        try:
            saved = json.loads(done.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                saved = {}
        except (OSError, ValueError):
            saved = {}
        if all(saved.get(k) == v for k, v in identity.items()):
            current = [{"path": p.relative_to(root).as_posix(), "sha256": sha256(p)} for p in paths if p.is_file()]
            if len(current) == len(paths) and current == saved.get("artifacts"):
                validate(paths, command, scope)
                return False
    # 결과 파일만 남은 중단 실행은 처음부터 다시 돌린다. 성공 전에는 새 완료 표식이 없다.
    if done.exists():
        done.unlink()
    if option(command, "-m") == "jikgam.v2.answer":
        from jikgam.v2.claim_check import invalidate_review
        invalidate_review(root, option(command, "--split", "test"))
    log = root / "outputs/logs/v2-test" / f"{name}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as output:
        subprocess.run(command, cwd=root, stdout=output, stderr=subprocess.STDOUT, check=True)
    validate(paths, command, scope)
    payload = {**identity, "artifacts": [{"path": p.relative_to(root).as_posix(), "sha256": sha256(p)} for p in paths]}
    done.parent.mkdir(parents=True, exist_ok=True)
    temp = done.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    os.replace(temp, done)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--result", required=True)
    ap.add_argument("--always", action="store_true")
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    run_stage(args.name, args.result, command, always=args.always)


if __name__ == "__main__":
    main()
