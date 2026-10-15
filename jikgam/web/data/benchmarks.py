"""모델·벤치마크 화면의 자료와 벤치마크 실행(ADR 0021, docs/design/data.md).

모델
- 목록은 jikgam.decision.registry.load_config(config/decision_models.toml + config/decision_models.d/*.toml)다.
- 등록은 config/decision_models.d/<이름>.toml 파일 하나를 쓰는 것이다. 어댑터는 판별 API·로컬 서버(kind systemone,
  Jev 형식)와 OpenAI 호환 생성형(kind openai)이다. 키 값은 받지 않고 키를 담은 환경 변수 이름만 받는다.

결과(모두 결과 폴더의 원본 로그에서 화면을 열 때 계산한다)
- 판별 정확도: results/decisions/{test,val}/<실행>(jikgam.web.dashboard.decision_runs). 사전 기준은 docs/evaluation.md
  §7.1(주장 1: 같은 실행의 gemini-flash 대비 정확도 −3%p 이상, p95 1/5 이하이면서 500ms 이하, 비용 1/10 이하)과
  §7.2(주장 3: 로컬 서빙 모델은 같은 실행의 Jev 대비 정확도 −5%p 이상, p95 200ms 이하)를 그대로 적용한다.
- 튜닝 모델의 연속·간헐 호출 지연: 같은 로컬 모델이 혼자 돈 test 실행(연속)과 원격 백엔드와 번갈아 돈 test 실행(간헐)의
  p95, 그리고 회차별 p95와 그 회차에 함께 돈 백엔드.
- 부하: results/load/<실행>(jikgam.eval.load).
- 상담 방식: 채팅 개발용 시나리오(jikgam.web.dashboard.scenario_groups), 음성 미공개 시나리오(results/voice/
  voice-holdout-report.json의 집계와 사전 기준 판정). 채팅 미공개 시나리오는 allow_holdout일 때만 계산한다.

실행
- 판별 정확도(val), 부하, 상담 시나리오(개발용, 정책 C), 음성 재생(개발용 s20·s21, 정책 C). test 분할과 미공개
  시나리오는 화면에서 실행하지 않는다(설정 고정 뒤 한 번만 평가한다는 고정 규칙).
- 실행 전에 예상 호출 수와 비용을 결과 파일의 이전 실행으로 계산해 보여 준다. 확인한 뒤에만 실행한다.
- 실행은 기존 실행기를 하위 프로세스로 돌린다. 상담 시나리오와 음성 재생은 모의 업무 API(Go)를 임시로 띄운다.
  출력은 outputs/benchmarks/<작업>.log(git에 넣지 않는다), 결과는 각 실행기의 결과 폴더에 남는다.
"""

import json
import math
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jikgam.decision.registry import load_config
from jikgam.eval.metrics import percentile

from .. import dashboard as db

ADAPTERS = {"decision_api": "판별 API", "local": "로컬 서버", "openai": "OpenAI 호환 생성형"}
KIND_LABEL = {"gemini": "생성형 기준선(Gemini API)", "rules": "규칙 기준선", "majority": "다수 라벨 기준선"}
CRITERIA_DOC = "docs/evaluation.md §7.1·§7.2"          # 판별 결과의 사전 기준
VOICE_CRITERIA_DOC = "docs/evaluation-voice.md §5(ADR 0017)"  # 음성 상담 방식의 사전 기준
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,40}$")
ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
LOCAL_HOSTS = ("127.0.0.1", "localhost")
GENERATIVE_BASE = "gemini-flash"   # 주장 1의 비교 대상(docs/evaluation.md §7.1)
DECISION_BASE = "jev"              # 주장 3의 비교 대상(§7.2)
# docs/evaluation.md §7.1·§7.2의 사전 기준. 그 문서를 바꾸지 않으며 여기 값도 바꾸지 않는다.
CLAIM1 = {"acc_drop": 0.03, "p95_ratio": 1 / 5, "p95_ms": 500.0, "cost_ratio": 1 / 10}
CLAIM3 = {"acc_drop": 0.05, "p95_ms": 200.0}
SUITES = {
    "decisions": ("판별 정확도", "판별 val 분할 · 정답 라벨", "정확도 · 지연 · 비용"),
    "scenarios": ("상담 시나리오", "개발용 시나리오 · AI 고객 · 1회", "통과율 · 이전 답 노출"),
    "voice": ("음성 재생", "개발용 재생 s20·s21 · 합성 고객 음성", "끼어들기 반응 · 첫 음성"),
    "load": ("부하", "판별 val 사례 반복 · 동시 요청", "p50 · p95 · 오류율"),
}


def adapter(cfg: dict[str, Any]) -> str:
    kind = cfg.get("kind")
    if kind == "systemone":
        host = urlsplit(str(cfg.get("url", "")).replace("{", "").replace("}", "")).hostname or ""
        return ADAPTERS["local"] if host in LOCAL_HOSTS else ADAPTERS["decision_api"]
    if kind == "openai":
        return ADAPTERS["openai"]
    return KIND_LABEL.get(kind, kind or "알 수 없음")


def is_local(cfg: dict[str, Any]) -> bool:
    return adapter(cfg) == ADAPTERS["local"]


def models(root: Path) -> dict[str, dict[str, Any]]:
    return load_config(root / "config/decision_models.toml", root / "config/decision_models.d")


@dataclass
class Registration:
    name: str
    adapter: str
    url: str
    model: str
    key_env: str = ""
    price_in: str = ""
    price_out: str = ""


def register(root: Path, r: Registration) -> tuple[bool, str]:
    """모델 하나를 설정 파일로 등록한다. (성공, 안내 글)."""
    if not NAME_RE.match(r.name):
        return False, "이름은 영어 소문자·숫자로 시작하고 영어 소문자, 숫자, 점, 밑줄, 하이픈만 쓴다(41자 이하)."
    if r.name in models(root):
        return False, f"{r.name}은 이미 있는 이름이다."
    if r.adapter not in ADAPTERS:
        return False, "어댑터는 판별 API, 로컬 서버, OpenAI 호환 생성형 중 하나다."
    u = urlsplit(r.url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False, "엔드포인트는 http(s) 주소다."
    if r.adapter == "local" and u.hostname not in LOCAL_HOSTS:
        return False, "로컬 서버는 127.0.0.1이나 localhost 주소다."
    if not r.model.strip():
        return False, "모델 ID를 넣는다(외부 모델은 버전 ID를 고정한다)."
    if r.key_env and not ENV_RE.match(r.key_env):
        return False, "키 환경 변수 이름은 영어 대문자로 시작하는 대문자·숫자·밑줄이다. 키 값은 넣지 않는다."
    try:
        prices = [float(p) if p.strip() else 0.0 for p in (r.price_in, r.price_out)]
    except ValueError:
        return False, "단가는 숫자(USD / 1M 토큰)다."
    if any(p < 0 or math.isinf(p) or math.isnan(p) for p in prices):
        return False, "단가는 0 이상이다."
    kind = "openai" if r.adapter == "openai" else "systemone"
    lines = [f"# 관리 콘솔에서 등록({datetime.now():%Y-%m-%d %H:%M}). 어댑터: {ADAPTERS[r.adapter]}",
             f"[{json.dumps(r.name)}]", f'kind = "{kind}"', f"url = {json.dumps(r.url)}", f"model = {json.dumps(r.model.strip())}"]
    if kind == "systemone":
        lines.append('envelope = "plain"')
    if r.key_env:
        lines.append(f"api_key_env = {json.dumps(r.key_env)}")
    lines += [f"price_input_per_mtok = {prices[0]}", f"price_output_per_mtok = {prices[1]}",
              'price_source = "관리 콘솔 등록 때 입력한 값"']
    d = root / "config/decision_models.d"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{r.name}.toml").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return True, f"{r.name}을 등록했다(config/decision_models.d/{r.name}.toml)."


# --- 판별 결과 -------------------------------------------------------------------------------------


@dataclass
class DecisionRow:
    run: str              # 정확도·비용을 읽은 실행
    split: str
    backend: str
    adapter: str
    accuracy: float
    p95_ms: float
    cost: float
    decisions: int
    verdict: str          # 통과, 정확도 미달, 지연 초과, 비용 초과, 기준선 없음, 기준선
    failed: list[str] = field(default_factory=list)
    latency_run: str | None = None  # p95를 읽은 실행(로컬 모델은 혼자 돈 실행, 없으면 run)
    model: str | None = None        # 설정의 모델 ID(버전)
    local: bool = False             # 로컬 서빙(호출 과금 없음)
    mixed_p95: float | None = None  # 로컬 모델의 원격 백엔드와 번갈아 돈 실행 p95(간헐 호출)


def _verdict(backend: str, acc: float, p95: float, cost: float, by: dict[str, dict[str, Any]],
             cfgs: dict[str, dict[str, Any]]) -> tuple[str, list[str]]:
    cfg = cfgs.get(backend) or {}
    if cfg.get("kind") in ("gemini", "openai"):
        return "기준선", []
    failed = []
    if is_local(cfg):  # 주장 3(§7.2): 지연은 이 PC에서 동시 실행 1로 서빙한 값
        base = by.get(DECISION_BASE)
        if base is None:
            return "기준선 없음", []
        if acc < base["accuracy"] - CLAIM3["acc_drop"]:
            failed.append("정확도 미달")
        if p95 > CLAIM3["p95_ms"]:
            failed.append("지연 초과")
        return ("통과" if not failed else failed[0]), failed
    base = by.get(GENERATIVE_BASE)  # 주장 1(§7.1)
    if base is None:
        return "기준선 없음", []
    if acc < base["accuracy"] - CLAIM1["acc_drop"]:
        failed.append("정확도 미달")
    if p95 > min(CLAIM1["p95_ms"], base["p95_ms"] * CLAIM1["p95_ratio"]):
        failed.append("지연 초과")
    if cost > base["cost_per_request"] * CLAIM1["cost_ratio"]:
        failed.append("비용 초과")
    return ("통과" if not failed else failed[0]), failed


def decision_rows(root: Path) -> list[DecisionRow]:
    """test: 기준선(gemini-flash)과 함께 돈 실행 하나에서 백엔드마다 한 줄. 로컬 서빙 모델의 p95는 그 모델만 돈 실행
    (주장 3의 측정 조건)에서 읽는다. val: 등록한 모델(config/decision_models.d)이 든 실행에서 그 모델과 기준선."""
    cfgs = models(root)
    registered = {n for n, c in cfgs.items() if c.get("registered_file")}
    out = []
    test = [r for r in db.decision_runs(root / "results", "test") if not r.get("error")]
    main = next((r for r in test if len(r["backends"]) > 1 and any(x["backend"] == GENERATIVE_BASE for x in r["backends"])), None)
    if main is not None:
        by = {x["backend"]: x for x in main["backends"]}
        for x in main["backends"]:
            p95, lat_run, mixed = x["p95_ms"], main["run_id"], None
            local = is_local(cfgs.get(x["backend"], {}))
            if local:
                alone = next((r for r in test if [y["backend"] for y in r["backends"]] == [x["backend"]]), None)
                if alone is not None:
                    p95, lat_run, mixed = alone["backends"][0]["p95_ms"], alone["run_id"], x["p95_ms"]
            verdict, failed = _verdict(x["backend"], x["accuracy"], p95, x["cost_per_request"], by, cfgs)
            out.append(DecisionRow(main["run_id"], "test", x["backend"], adapter(cfgs.get(x["backend"]) or {"kind": x["kind"]}),
                                   x["accuracy"], p95, x["cost_per_request"], main["decisions_per_rep"], verdict, failed, lat_run,
                                   (cfgs.get(x["backend"]) or {}).get("model"), local, mixed))
    for run in db.decision_runs(root / "results", "val"):
        if run.get("error"):
            continue
        names = {x["backend"] for x in run["backends"]}
        if not registered & names:
            continue
        by = {x["backend"]: x for x in run["backends"]}
        for x in run["backends"]:
            if x["backend"] not in registered and x["backend"] not in (GENERATIVE_BASE, DECISION_BASE):
                continue
            verdict, failed = _verdict(x["backend"], x["accuracy"], x["p95_ms"], x["cost_per_request"], by, cfgs)
            out.append(DecisionRow(run["run_id"], "val", x["backend"], adapter(cfgs.get(x["backend"]) or {"kind": x["kind"]}),
                                   x["accuracy"], x["p95_ms"], x["cost_per_request"], run["decisions_per_rep"], verdict, failed,
                                   run["run_id"], (cfgs.get(x["backend"]) or {}).get("model"),
                                   is_local(cfgs.get(x["backend"], {}))))
    return out


@dataclass
class TunedLatency:
    backend: str
    continuous_run: str | None
    continuous_p95: float | None
    intermittent_run: str | None
    intermittent_p95: float | None
    reps: list[tuple[int, float, list[str]]]   # (회차, 그 회차 p95, 그 회차에 함께 돈 다른 백엔드)


def tuned_latency(root: Path) -> list[TunedLatency]:
    cfgs = models(root)
    base = root / "results/decisions/test"
    out = []
    if not base.is_dir():
        return out
    runs = {d.name: [json.loads(line) for line in (d / "raw.jsonl").read_text(encoding="utf-8").splitlines() if line]
            for d in sorted(base.iterdir()) if (d / "raw.jsonl").is_file()}
    local = sorted({r["backend"] for raw in runs.values() for r in raw if is_local(cfgs.get(r["backend"], {}))})
    for b in local:
        cont = inter = None
        for run_id, raw in runs.items():
            names = {r["backend"] for r in raw}
            if b not in names:
                continue
            if names == {b}:
                cont = (run_id, raw)
            elif any(not is_local(cfgs.get(n, {})) for n in names):
                inter = (run_id, raw)

        def p95(raw: list[dict[str, Any]], rep: int | None = None) -> float:
            return percentile([r["latency_ms"] for r in raw if r["backend"] == b and not r["error"]
                               and (rep is None or r["rep"] == rep)], 95)
        reps = []
        if inter:
            for rep in sorted({r["rep"] for r in inter[1] if r["backend"] == b}):
                others = sorted({r["backend"] for r in inter[1] if r["rep"] == rep and r["backend"] != b})
                reps.append((rep + 1, p95(inter[1], rep), others))
        out.append(TunedLatency(b, cont[0] if cont else None, p95(cont[1]) if cont else None,
                                inter[0] if inter else None, p95(inter[1]) if inter else None, reps))
    return out


# --- 부하 ------------------------------------------------------------------------------------------


@dataclass
class LoadRow:
    run: str
    backend: str
    concurrency: int
    duration_s: float
    calls: int
    errors: int
    p50: float
    p95: float
    per_s: float
    cost: float


def load_rows(root: Path) -> list[LoadRow]:
    base = root / "results/load"
    out = []
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        if not (d / "meta.json").is_file():
            continue
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        raw = [json.loads(line) for line in (d / "raw.jsonl").read_text(encoding="utf-8").splitlines() if line]
        ok = [r for r in raw if not r["error"]]
        lat = [r["latency_ms"] for r in ok]
        out.append(LoadRow(meta["run_id"], meta["backend"], int(meta["concurrency"]), float(meta["duration_s"]), len(raw),
                           len(raw) - len(ok), percentile(lat, 50) if lat else math.nan, percentile(lat, 95) if lat else math.nan,
                           len(raw) / float(meta["duration_s"]), sum(r.get("cost_usd") or 0 for r in raw)))
    return out


# --- 상담 방식 ------------------------------------------------------------------------------------


@dataclass
class PolicyRow:
    label: str          # 방식 이름
    channel: str        # 채팅, 음성
    scenarios: str      # 개발용 시나리오, 미공개 시나리오
    policy: str         # 정책 문자(A~D)
    runs: list[str]     # 계산에 쓴 실행 폴더
    sessions: int       # 세션 수(무효 포함)
    passed: int
    valid: int
    refresh_p95: float | None
    refresh_misses: int
    stale: int
    stale_den: int
    verdict: str | None  # 사전 기준 판정(미공개 음성만): 통과, 일부 미달
    failed: list[str] = field(default_factory=list)  # 못 미친 조건 이름
    failed_detail: list[str] = field(default_factory=list)  # 못 미친 조건의 기준 문구와 값(보고서의 판정 그대로)
    reps: int | None = None  # 시나리오마다 돌린 횟수


POLICY_LABEL = {"A": "개선형 RAG", "B": "엔진 + 생성형 판단", "C": "직감(엔진 + 판별)", "D": "직감 + 실시간 자막 선판단"}


def policy_rows(root: Path, allow_holdout: bool) -> list[PolicyRow]:
    out = []
    groups = db.scenario_groups(db.scenario_runs(root / "results", allow_holdout))
    for g in groups:
        st = g.get("stats")
        if st is None or g["policy"] not in POLICY_LABEL or not (g["prefix"] == "dev3" or g["holdout"]):
            continue
        rf = st["refresh_valid"]
        ch = st["stale"]["by_channel"].get("chat", {"invalid": 0, "denominator": 0})
        out.append(PolicyRow(POLICY_LABEL[g["policy"]], "채팅", "미공개 시나리오" if g["holdout"] else "개발용 시나리오",
                             g["policy"], [i["run_id"] for i in g["runs"]], st["sessions"],
                             st["passed"], st["valid"], rf.get("p95"), int(rf.get("misses") or 0), ch["invalid"], ch["denominator"],
                             None, reps=len(g["runs"])))
    rep = root / "results/voice/voice-holdout-report.json"
    if rep.is_file():
        data = json.loads(rep.read_text(encoding="utf-8"))
        verdicts: dict[str, list[tuple[bool, str, str]]] = {}
        for v in data.get("verdict", []):
            cond = str(v.get("condition", ""))
            verdicts.setdefault(v["policy"], []).append((bool(v["ok"]), cond.split(":")[0],
                                                         f"{cond} — {v['value']}" if v.get("value") else cond))
        runs = [Path(r).name for r in data.get("runs", [])]
        for p, s in data["policies"].items():
            vr = s["valid_runs"]
            ok = verdicts.get(p)
            out.append(PolicyRow(POLICY_LABEL.get(p, p), "음성", "미공개 시나리오", p, [r for r in runs if r.endswith(f"-{p}")],
                                 int(s.get("sessions") or s["valid"]), s["passed"], s["valid"], vr["refresh"]["p95"],
                                 int(vr["refresh"]["misses"]), vr["stale"]["invalid"], vr["stale"]["denominator"],
                                 None if ok is None else ("통과" if all(o for o, _, _ in ok) else "일부 미달"),
                                 [name for o, name, _ in (ok or []) if not o], [d for o, _, d in (ok or []) if not o],
                                 reps=len(s.get("per_run") or {}) or None))
    return out


# --- 예상 비용 ------------------------------------------------------------------------------------


@dataclass
class Estimate:
    """실행 하나의 예상 비용. 이전 실행의 결과 파일로 계산한다(값이 없으면 None)."""
    suite: str
    calls: int | None            # 이 모델에 보낼 판별 호출 수(부하는 응답 시간에 따라 달라 None)
    per_call: float              # 호출 한 번의 예상 비용(최근 val 실행의 평균 토큰 × 등록 단가)
    model_cost: float | None
    other_cost: float | None     # 고객 AI, 생성, 판정, STT·TTS 등 이 모델과 무관한 비용(이전 실행 기준)
    cases: int | None = None     # 판별 정확도: val 사례 수
    reps: int | None = None      # 판별 정확도: 반복 횟수
    sessions: int | None = None  # 상담 시나리오·음성 재생: 세션 수
    concurrency: int | None = None
    duration_s: float | None = None
    source: str | None = None    # 기준으로 쓴 이전 실행


def _avg_tokens(root: Path) -> tuple[float, float]:
    """최근 val 실행 세 개에서 성공한 호출의 평균 입력 토큰과(출력이 있는 호출의) 평균 출력 토큰."""
    base = root / "results/decisions/val"
    runs = sorted(base.glob("*/raw.jsonl")) if base.is_dir() else []
    tin, tout = [], []
    for p in runs[-3:]:
        for line in p.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if not r["error"]:
                tin.append(r.get("input_tokens") or 0)
                if r.get("output_tokens"):
                    tout.append(r["output_tokens"])
    return (sum(tin) / len(tin) if tin else 0.0), (sum(tout) / len(tout) if tout else 0.0)


def per_call_cost(root: Path, cfg: dict[str, Any]) -> float:
    tin, tout = _avg_tokens(root)
    out_tokens = tout if cfg.get("kind") in ("openai", "gemini") else 0.0
    return (tin * float(cfg.get("price_input_per_mtok") or 0) + out_tokens * float(cfg.get("price_output_per_mtok") or 0)) / 1e6


def _count_requests(paths: list[Path]) -> int:
    n = 0
    for p in paths:
        if p.name.endswith(".sim.jsonl"):
            continue
        with p.open(encoding="utf-8") as f:
            n += sum(1 for line in f if '"type": "decision.request"' in line)
    return n


def estimates(root: Path, name: str, suites: list[str], concurrency: int, duration_s: float) -> list[Estimate]:
    cfg = models(root)[name]
    one = per_call_cost(root, cfg)
    val = root / "data/build/val.jsonl"
    cases = sum(1 for line in val.open(encoding="utf-8") if line.strip()) if val.is_file() else 0
    reps = 3 if cfg.get("kind") in ("openai", "gemini") else 5
    out = []
    for suite in suites:
        if suite == "decisions":
            out.append(Estimate(suite, cases * reps, one, cases * reps * one, 0.0, cases=cases, reps=reps))
        elif suite == "load":
            out.append(Estimate(suite, None, one, None, 0.0, concurrency=concurrency, duration_s=duration_s))
        elif suite == "scenarios":
            group = next((g for g in db.scenario_groups(db.scenario_runs(root / "results", False))
                          if g.get("prefix") == "dev3" and g.get("policy") == "C" and g.get("stats")), None)
            if group is None:
                out.append(Estimate(suite, None, one, None, None))
                continue
            st, n_runs = group["stats"], max(len(group["runs"]), 1)
            logs = [p for info in group["runs"] for p in (root / "results/engine/scenarios" / info["dir"]).glob("*.jsonl")]
            calls = _count_requests(logs) // n_runs
            out.append(Estimate(suite, calls, one, calls * one, (st["cost"]["total"] - st["cost"]["decision"]) / n_runs,
                                sessions=st["sessions"] // n_runs, source=group["label"]))
        elif suite == "voice":
            d = root / "results/voice/voice-barge-C"
            if not (d / "summary.json").is_file():
                out.append(Estimate(suite, None, one, None, None))
                continue
            sm = json.loads((d / "summary.json").read_text(encoding="utf-8"))
            n = max(len(sm.get("sessions") or []), 1)
            calls = _count_requests(sorted(d.glob("*.jsonl"))) * 2 // n
            c = sm.get("cost_usd") or {}
            out.append(Estimate(suite, calls, one, calls * one, (c.get("stt_estimate", 0) + c.get("tts_estimate", 0)) * 2 / n,
                                sessions=2, source=sm.get("run_id")))
    return out


# --- 실행 ------------------------------------------------------------------------------------------


@dataclass
class Job:
    id: str
    model: str
    suite: str
    status: str = "대기"      # 대기, 실행 중, 끝남, 실패
    started: str | None = None
    ended: str | None = None
    log: Path | None = None
    note: str = ""


class Jobs:
    """벤치마크 실행 목록. 한 번에 하나씩 차례로 돈다. 서버 프로세스가 끝나면 목록도 사라진다(결과는 결과 폴더에 남는다)."""

    def __init__(self, root: Path, runner: Any = None):
        self.root = root
        self.items: list[Job] = []
        self.lock = threading.Lock()
        self.runner = runner or self._run_subprocess
        self.thread: threading.Thread | None = None

    def submit(self, model: str, suites: list[str], concurrency: int, duration_s: float) -> list[Job]:
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        jobs = [Job(f"{stamp}-{model}-{s}", model, s) for s in suites]
        for j in jobs:
            j.log = self.root / "outputs/benchmarks" / f"{j.id}.log"
            j.note = f"동시 {concurrency}개 · {duration_s:.0f}초" if j.suite == "load" else ""
        with self.lock:
            self.items += jobs
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, args=(concurrency, duration_s), daemon=True)
                self.thread.start()
        return jobs

    def _loop(self, concurrency: int, duration_s: float) -> None:
        while True:
            with self.lock:
                job = next((j for j in self.items if j.status == "대기"), None)
            if job is None:
                return
            job.status, job.started = "실행 중", datetime.now().isoformat(timespec="seconds")
            try:
                ok = self.runner(job, concurrency, duration_s)
                job.status = "끝남" if ok else "실패"
            except Exception as e:  # 실행기 오류는 그 작업만 실패로 둔다
                job.status, job.note = "실패", f"{type(e).__name__}: {e}"
            job.ended = datetime.now().isoformat(timespec="seconds")

    def commands(self, job: Job, concurrency: int, duration_s: float, bizapi_url: str | None = None) -> list[list[str]]:
        py = [sys.executable, "-m"]
        if job.suite == "decisions":
            return [py + ["jikgam.eval.run_decisions", "--split", "val", "--backends", job.model]]
        if job.suite == "load":
            return [py + ["jikgam.eval.load", "--backend", job.model, "--concurrency", str(concurrency),
                          "--duration-s", str(duration_s)]]
        if job.suite == "scenarios":
            return [py + ["jikgam.sim.runner", "--policy", "C", "--decision-backend", job.model, "--runs", "1",
                          "--run-id", f"bench-{job.id}", "--bizapi-url", bizapi_url or ""]]
        if job.suite == "voice":
            return [py + ["jikgam.voice.replay", "--replays", "s20", "s21", "--policy", "C", "--decision-backend", job.model,
                          "--run-id", f"bench-{job.id}", "--bizapi-url", bizapi_url or ""]]
        raise ValueError(job.suite)

    def _run_subprocess(self, job: Job, concurrency: int, duration_s: float) -> bool:
        assert job.log is not None
        job.log.parent.mkdir(parents=True, exist_ok=True)
        proc_api = tmp = None
        url = None
        if job.suite in ("scenarios", "voice"):
            from jikgam.web.__main__ import start_bizapi
            url = f"http://127.0.0.1:{_free_port()}"
            proc_api, tmp = start_bizapi(url)
        try:
            with job.log.open("w", encoding="utf-8") as f:
                for cmd in self.commands(job, concurrency, duration_s, url):
                    f.write("$ " + " ".join(cmd) + "\n")
                    f.flush()
                    r = subprocess.run(cmd, cwd=self.root, stdout=f, stderr=subprocess.STDOUT,
                                       env={**_env(), "PYTHONUTF8": "1"})
                    if r.returncode != 0:
                        job.note = f"종료 코드 {r.returncode}"
                        return False
            return True
        finally:
            if proc_api is not None:
                proc_api.terminate()
                proc_api.wait(timeout=10)
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)

    def recent(self) -> list[Job]:
        with self.lock:
            return list(reversed(self.items))


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _env() -> dict[str, str]:
    import os
    return dict(os.environ)


def wait_all(jobs: Jobs, timeout_s: float) -> None:
    """테스트용: 실행이 모두 끝날 때까지 기다린다."""
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if all(j.status in ("끝남", "실패") for j in jobs.items):
            return
        time.sleep(0.05)
