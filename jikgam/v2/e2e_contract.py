"""법령 프로필만 바꿀 수 있는 엔드투엔드 실행 계약. 본문은 저장하지 않는다."""

import math
import tomllib
from pathlib import Path

from jikgam.engine.org import ROOT

HOLDOUT_SCENARIO = "data/scenarios/org_a_v2_holdout.jsonl"
HOLDOUT_BIZAPI_DATA = ["domain/org_a/customers.json", "domain/org_a/customers_holdout.json",
                      "domain/org_a/customers_v2_holdout.json"]
# 두 번째 v2 미공개 묶음(data/scenarios/HOLDOUT_V2B.md). 업무 API는 앞의 세 파일에 이 묶음의 고객 파일을 더한다
HOLDOUT_B_SCENARIO = "data/scenarios/org_a_v2b_holdout.jsonl"
HOLDOUT_B_BIZAPI_DATA = [*HOLDOUT_BIZAPI_DATA, "domain/org_a/customers_v2b_holdout.json"]
# 미공개 시나리오 파일 → 정해진 업무 API 데이터 순서. 실행기 고객 파일은 그 마지막 파일이다
HOLDOUTS = {HOLDOUT_SCENARIO: HOLDOUT_BIZAPI_DATA, HOLDOUT_B_SCENARIO: HOLDOUT_B_BIZAPI_DATA}
LAW_PROFILES = ("r0", "r4", "r5")


def profile_files(p, root):
    """법령 프로필이 실제로 읽는 파일: 구간, 구간 정보, r5의 구간 메타데이터와 주제 질문."""
    chosen = {f"data/v2/chunks/{p['chunks']}.jsonl"}
    if p.get("meta", "none") != "none":
        chosen.update(relative(x, root) for x in (
            root / f"data/v2/meta/{p['chunks']}.jsonl", root / f"data/v2/local/meta/{p['chunks']}.jsonl") if x.exists())
    for key in ("labels", "topic_question_set"):
        if p.get(key):
            chosen.add(relative(p[key], root))
    return chosen


def relative(path, root):
    path = Path(path)
    resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("실행 입력은 저장소 안의 경로여야 합니다")
    return resolved.relative_to(root.resolve()).as_posix()


def selected(args, root=ROOT):
    """조직 설정이 가리키는 실제 파일을 따라간다. 시나리오 내용은 해석하지 않는다."""
    from jikgam.sim import customer, judge

    org_arg = getattr(args, "org", "org_a")
    org = relative(org_arg if str(org_arg).endswith(".toml") else f"config/orgs/{org_arg}.toml", root)
    raw = tomllib.loads((root / org).read_text(encoding="utf-8"))
    scenario = relative(getattr(args, "scenario_file", None) or f"data/scenarios/{raw['org']}.jsonl", root)
    holdout = HOLDOUTS.get(scenario)
    defaults = [holdout[-1]] if holdout else [str(Path(raw["terms"]).parent / "customers.json")]
    customers = sorted({relative(p, root) for p in (getattr(args, "customers", None) or defaults)})
    bizapi_data = [relative(p, root) for p in (getattr(args, "bizapi_data", None) or (holdout or customers))]
    if len(set(bizapi_data)) != len(bizapi_data):
        raise ValueError("업무 API 데이터 파일 중복은 허용하지 않습니다")
    if holdout and bizapi_data != holdout:
        raise ValueError(f"미공개 실행 계약의 업무 API 데이터는 정해진 {({3: '세', 4: '네'}).get(len(holdout), len(holdout))} 파일 순서여야 합니다")
    for path in [scenario, *customers, *bizapi_data]:
        if "holdout" in Path(path).name.casefold():
            allowed = {*HOLDOUTS, *(p for files in HOLDOUTS.values() for p in files)}
            if path not in allowed:
                raise ValueError("미공개 파일은 정확한 저장소 경로만 허용합니다")
    from jikgam.sim.scenario import normalize_selection
    scenarios = normalize_selection(getattr(args, "scenarios", "all"))
    arguments = {"policy": getattr(args, "policy", "C"), "org": org, "scenario_file": scenario,
                 "customers": customers, "decision_backend": getattr(args, "decision_backend", None) or raw["decision_backend"],
                 "bizapi_data": bizapi_data,
                 "customer_model": getattr(args, "customer_model", customer.DEFAULT_MODEL),
                 "judge_model": getattr(args, "judge_model", judge.DEFAULT_JUDGE_MODEL),
                 "runs": int(getattr(args, "runs", 1)), "run_start": int(getattr(args, "run_start", 1)),
                 "max_turns": int(getattr(args, "max_turns", 16)),
                 "fast_delay_ms": float(getattr(args, "fast_delay_ms", 1000.0)), "scenarios": scenarios}
    if any(arguments[k] < 1 for k in ("runs", "run_start", "max_turns")) or not (
            math.isfinite(arguments["fast_delay_ms"]) and arguments["fast_delay_ms"] >= 0):
        raise ValueError("반복·턴 수·빠른 정정 인자가 올바르지 않습니다")
    files = {org, scenario, *customers, *bizapi_data, *(relative(raw[k], root) for k in ("terms", "question_set", "extra_question_set"))}
    for pattern in ("jikgam/engine/*.py", "jikgam/decision/*.py", "jikgam/sim/*.py", "jikgam/v2/*.py",
                    "jikgam/ingest/*.py", "jikgam/eval/*.py", "config/decision_models.toml",
                    "config/decision_models.d/*.toml", "config/v2/law.toml", "uv.lock", "pyproject.toml",
                    "bizapi/**/*.go", "bizapi/go.mod", "bizapi/go.sum", "data/v2/local/emb/**/*"):
        files.update(p.relative_to(root).as_posix() for p in root.glob(pattern)
                     if p.is_file() and not p.name.endswith("_test.go"))
    profiles = {}
    cfg = root / "config/v2/law.toml"
    if cfg.exists():
        law = tomllib.loads(cfg.read_text(encoding="utf-8"))
        if law.get("question_set"):
            files.add(relative(law["question_set"], root))
        for name in LAW_PROFILES:
            if name in law:
                profiles[name] = profile_files(law[name], root)
    summary = root / "data/v2/extracted/summary.json"
    if summary.exists():
        import json
        files.add(relative(summary, root))
        files.update(relative(d["output"], root) for d in json.loads(summary.read_text(encoding="utf-8"))["docs"].values())
    return arguments, files, profiles


def build_contract(args, root=ROOT):
    from jikgam.v2.freeze import sha256

    arguments, files, profiles = selected(args, root)
    hashed = lambda paths: {p: sha256(root / p) for p in sorted(paths)}
    files_hashed = hashed(files)
    return {"version": 2, "arguments": arguments, "files": files_hashed,
            "bizapi_data": [{"path": p, "sha256": files_hashed[p]} for p in arguments["bizapi_data"]],
            "law_profiles": {name: hashed(paths) for name, paths in sorted(profiles.items())}}


def contract_files(contract):
    return set(contract["files"]) | {p for rows in contract["law_profiles"].values() for p in rows}


def legacy_contract(summary):
    """계약 도입 전 개발용 기록끼리는 기록된 조건만 대조하고 그 한계를 표시한다."""
    keys = ("policy", "org", "scenario_file", "scenario_sha256", "customer_files", "decision_backend",
            "decision_model", "generator_model", "customer_model", "judge_model", "runs", "run_start",
            "max_turns", "fast_delay_ms", "customer_prompt", "judge_prompt")
    return {"legacy": True, "arguments": {k: summary.get(k) for k in keys},
            "scenarios": summary.get("scenarios", "all")}


def verify_comparison(r0, r4):
    contracts = []
    for rows in (r0, r4):
        values = [r.get("execution_contract") for r in rows]
        if any(v != values[0] for v in values):
            raise ValueError("실행 안에서 계약이 다릅니다")
        contracts.append(values[0])
    if contracts[0] != contracts[1]:
        raise ValueError("두 실행의 계약은 법령 프로필 외에 달라질 수 없습니다")
    return "이전 실행(기록된 인자만 대조)" if contracts[0] is None or contracts[0].get("legacy") else "계약 일치"
