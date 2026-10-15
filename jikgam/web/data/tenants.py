"""고객사(조직)와 그 통화 기록을 결과 폴더에서 읽는다(ADR 0021).

- 고객사 목록과 자료 위치는 <root>/config/web/tenants.toml, 조직 설정은 <root>/config/orgs/<org>.toml을 그대로 읽는다.
  엔진의 load_org는 저장소 루트에 묶여 있어, 테스트 자료 폴더에서도 같은 코드가 돌도록 여기서 직접 읽는다.
- 통화 기록: history 폴더의 엔진 이벤트 로그(*.jsonl, 시뮬레이터 기록 *.sim.jsonl 제외)마다 calls.build_call.
  보류용(미공개) 실행은 allow_holdout일 때만 더한다. live에 넣은 기록은 통화 기록에서 뺀다.
- 녹음: 로그의 voice.recording(녹음 시작 시각 t0_ms, 파일, 길이), 없으면 실행 폴더의 recordings.json. 파일이 없으면
  녹음 없음으로 둔다(WAV는 git에 넣지 않는다).
- 결과는 실행 폴더 파일들의 수정 시각과 크기로 캐시한다.
"""

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jikgam.engine import events as ev
from jikgam.engine.retrieval import Article, parse_terms

from .calls import Call, build_call

_cache: dict[str, tuple[tuple, Any]] = {}


@dataclass
class Tenant:
    org: str
    name: str
    desc: str
    service: str
    history: list[str]
    holdout_history: list[str]
    live: list[str]
    voice: bool
    handoff_article: str | None
    raw: dict[str, Any] = field(default_factory=dict)  # 조직 설정(config/orgs/<org>.toml) 원문
    adapter: str | None = None   # 업무 API 어댑터 코드 위치
    changes: str | None = None   # 붙이면서 바꾼 엔진 코드를 적은 문서


def load_tenants(root: Path) -> dict[str, Tenant]:
    path = root / "config/web/tenants.toml"
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    out = {}
    for org, t in raw.items():
        org_path = root / "config/orgs" / f"{org}.toml"
        org_raw = tomllib.loads(org_path.read_text(encoding="utf-8")) if org_path.is_file() else {}
        out[org] = Tenant(org, t["name"], t.get("desc", ""), t.get("service", ""), list(t.get("history", [])),
                          list(t.get("holdout_history", [])), list(t.get("live", [])), bool(t.get("voice", False)),
                          t.get("handoff_article"), org_raw, t.get("adapter"), t.get("changes"))
    return out


def voice_config(root: Path) -> dict[str, Any]:
    """음성 설정(config/voice.toml). 없으면 빈 dict."""
    p = root / "config/voice.toml"
    return tomllib.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def terms_of(root: Path, tenant: Tenant) -> dict[str, Article]:
    p = root / tenant.raw["terms"]
    key = f"terms:{p}"
    stamp = (p.stat().st_mtime_ns, p.stat().st_size)
    hit = _cache.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    arts = parse_terms(p)
    _cache[key] = (stamp, arts)
    return arts


def terms_header(root: Path, tenant: Tenant) -> dict[str, str]:
    """약관 문서 머리말의 "- 키: 값" 줄(조직, 약관 버전, 시행일)."""
    out = {}
    for line in (root / tenant.raw["terms"]).read_text(encoding="utf-8").splitlines():
        if line.strip() == "---":
            break
        m = re.match(r"^-\s*([^:]+):\s*(.+)$", line.strip())
        if m:
            out[m.group(1).strip()] = m.group(2).strip()
    return out


def article_label(a: Article | None, fallback: str) -> str:
    """"제7조 (월 구독의 해지)" → "제7조 월 구독의 해지"."""
    if a is None:
        return fallback
    return re.sub(r"\s*\((.*)\)\s*$", r" \1", a.title).strip()


def is_event_log(p: Path) -> bool:
    if p.suffix != ".jsonl" or p.name.endswith(".sim.jsonl"):
        return False
    try:
        with p.open(encoding="utf-8") as f:
            return json.loads(f.readline()).get("type") == ev.SESSION_START
    except (OSError, ValueError, AttributeError):
        return False


def _stamp(d: Path) -> tuple:
    return tuple(sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in d.iterdir() if p.is_file()))


def _recordings(run_dir: Path) -> dict[str, dict[str, Any]]:
    p = run_dir / "recordings.json"
    if not p.is_file():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return {r["session"]: r for r in data.get("recordings", [])}


def recording_of(root: Path, events: list[dict[str, Any]], run_dir: Path) -> dict[str, Any] | None:
    rec = next((e for e in events if e["type"] == "voice.recording"), None)
    if rec is None:
        session = next((e.get("session") for e in events if e["type"] == ev.SESSION_START), None)
        rec = _recordings(run_dir).get(session)
    if rec is None:
        return None
    path = root / rec["file"]
    return {"file": rec["file"], "path": path, "t0_ms": float(rec["t0_ms"]), "duration_ms": float(rec["duration_ms"]),
            "available": path.is_file()}


def call_id(run_dir: Path, log: Path) -> str:
    return f"{run_dir.name}~{log.stem}"


def _calls_in(root: Path, run_dir: Path, skip: set[Path]) -> list[Call]:
    key = f"calls:{run_dir.resolve()}:{sorted(str(s) for s in skip)}"
    stamp = _stamp(run_dir)
    hit = _cache.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    out = []
    for p in sorted(run_dir.glob("*.jsonl")):
        if p.resolve() in skip or not is_event_log(p):
            continue
        events = ev.read_log(p)
        out.append(build_call(events, call_id(run_dir, p), recording_of(root, events, run_dir)))
    _cache[key] = (stamp, out)
    return out


def calls_of(root: Path, tenant: Tenant, allow_holdout: bool = False) -> list[Call]:
    """고객사의 끝난 통화(최근 것부터)."""
    skip = {(root / p).resolve() for p in tenant.live}
    dirs = tenant.history + (tenant.holdout_history if allow_holdout else [])
    out: list[Call] = []
    for d in dirs:
        run = root / d
        if run.is_dir():
            out += _calls_in(root, run, skip)
    return sorted(out, key=lambda c: (c.started.isoformat() if c.started else "", c.id), reverse=True)


def find_call(root: Path, tenant: Tenant, cid: str, allow_holdout: bool = False) -> Call | None:
    return next((c for c in calls_of(root, tenant, allow_holdout) if c.id == cid), None)


def load_events(path: Path) -> list[dict[str, Any]]:
    return ev.read_log(path)


def onboarding(root: Path, tenant: Tenant, account_orgs: set[str]) -> list[tuple[bool, str, str]]:
    """온보딩 체크 목록(끝남, 이름, 근거). 파일이 있는지와 계정 설정으로만 판단한다."""
    raw = tenant.raw
    has_runs = any((root / d).is_dir() and any((root / d).glob("*.jsonl")) for d in tenant.history)
    return [
        (bool(raw), "설정 파일", f"config/orgs/{tenant.org}.toml"),
        (bool(raw.get("terms")) and (root / raw["terms"]).is_file(), "약관·규칙 문서", raw.get("terms", "-")),
        (bool(tenant.adapter) and (root / tenant.adapter).is_dir(), "업무 API 어댑터", tenant.adapter or "-"),
        (bool(raw.get("question_set")) and (root / raw["question_set"]).is_file(), "평가 질문 세트",
         raw.get("question_set", "-")),
        (has_runs, "벤치마크 실행(상담 재생·시나리오)", ", ".join(tenant.history) or "-"),
        (tenant.org in account_orgs, "고객사 콘솔 계정", "JIKGAM_WEB_ACCOUNTS"),
    ]


CHANGES_SECTION = "§5"  # 기록 문서에서 엔진 변경을 적은 절


def impact_checked(root: Path, tenant: Tenant) -> bool:
    """기록 문서에 기존 고객사(조직 A) 동작을 바꾸지 않았다는 근거가 적혀 있는가."""
    if not tenant.changes or not (root / tenant.changes).is_file():
        return False
    return "동작을 바꾸지 않았다는 근거" in (root / tenant.changes).read_text(encoding="utf-8")


def engine_changes(root: Path, tenant: Tenant) -> list[dict[str, str]]:
    """붙이면서 바꾼 엔진 코드: 기록 문서 §5 표의 행(번호, 파일, 바꾼 것, 이유, 분류). 문서가 없으면 빈 목록."""
    if not tenant.changes or not (root / tenant.changes).is_file():
        return []
    text = (root / tenant.changes).read_text(encoding="utf-8")
    m = re.search(r"^## 5\..*?$(.*?)^## ", text, re.S | re.M)
    if not m:
        return []
    out = []
    for line in m.group(1).splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 5 and cells[0].isdigit():
            clean = [re.sub(r"[`*]", "", c) for c in cells]
            out.append({"no": clean[0], "file": clean[1], "what": clean[2], "why": clean[3], "kind": clean[4]})
    return out
