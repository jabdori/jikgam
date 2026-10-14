"""턴 상세 타임라인: 이벤트 로그 하나를 고객 발화(턴)별 단계 폭포 그래프 자료로 바꾼다.

모든 시각은 엔진 단조 시계(t_ms)이며, 턴마다 그 발화의 utterance.end를 0으로 둔 상대 시각(ms)으로 낸다.

- 턴 배정: 고객 확정 발화가 만든 대화 버전(version.bumped의 to, 버전 증가가 없는 RAG 로그는 발화 시점 버전 + 1)과,
  그 뒤 도구 결과·상담사 인계 요청으로 오른 버전은 같은 턴이다. 각 이벤트는 그 작업을 시작한 버전(started_version,
  request_version, basis_version, 없으면 version)으로 턴에 배정한다(무효화는 무효화한 버전). 그래서 다음 발화가 들어온 뒤에 끝나거나 취소된 앞 턴의
  작업도 앞 턴에 남고, 막대가 "다음 발화" 선을 넘어간다(빠른 정정).
- 막대: 검색(retrieval.done의 duration_ms), 판별(decision.request → decision.response, 같은 버전 안에서 순서대로 짝짓는다.
  응답 전에 취소되면 task.cancelled까지), 도구(tool.call → tool.result, call_id), 생성(generation.request →
  generation.response, RAG는 단계별).
- 점: 버전 증가, 진술 추출, 근거 갱신, 정책 결정, 견적 선택, 제안 생성, 노출, 무효화, 작업 취소, 결과 폐기, 쓰기 대기,
  엔진 오류, 장애 주입, 미확정 자막.
- 턴 요약 수치는 jikgam.engine.events.turn_latencies(재생 보고서와 같은 함수)로 계산한다. 조건 변경 표시 발화의 근거
  갱신 시간(docs/evaluation.md §3.2)은 그 함수의 evidence_ms다(t0 = utterance.end, t1 = 그 발화의 버전 이상인 첫
  evidence.updated, 다음 고객 발화 전까지 없으면 미갱신).
"""

from collections import defaultdict, deque
from typing import Any

from jikgam.engine import events as ev
from jikgam.engine.bizapi import WRITE_TOOLS

from . import labels as lb
from .view import _short, tool_summary

SKIP_TYPES = {ev.AGENT_SAY, ev.SESSION_START}  # agent.say는 suggestion.exposed와 같은 시각·내용이다


def _is_customer_utt(e: dict[str, Any]) -> bool:
    return e["type"] == ev.UTTERANCE_END and e.get("speaker", "customer") == "customer"


def stage_version(e: dict[str, Any]) -> int | None:
    """이벤트가 속한 작업을 시작한 대화 버전."""
    for k in ("started_version", "request_version", "basis_version"):
        if e.get(k) is not None:
            return int(e[k])
    return None if e.get("version") is None else int(e["version"])


def version_owners(events: list[dict[str, Any]]) -> dict[int, str]:
    """대화 버전 → 그 버전을 연 고객 발화(utt_id)."""
    owner: dict[int, str] = {}
    cur: str | None = None
    for e in events:
        if _is_customer_utt(e):
            cur = e["utt_id"]
            owner.setdefault(int(e["version"]) + 1, cur)
        elif e["type"] == ev.VERSION_BUMPED:
            if e.get("utt_id"):
                owner[int(e["to"])] = e["utt_id"]
            else:
                src = owner.get(int(e["from"]), cur)
                if src is not None:
                    owner[int(e["to"])] = src
    return owner


def _point_label(e: dict[str, Any]) -> tuple[str, str]:
    """점 이벤트의 (이름, 설명)."""
    t = e["type"]
    if t == ev.VERSION_BUMPED:
        return f"버전 v{e.get('from')}→v{e.get('to')}", lb.label(lb.BUMP_CAUSE, e.get("cause")) or ""
    if t == ev.CLAIMS_EXTRACTED:
        keys = e.get("keys") or {}
        parts = [f"{lb.label(lb.CLAIM, k)} {v}" for k, v in keys.items()] + [f"번호 {p}" for p in e.get("phones") or []]
        return "진술 추출", ", ".join(parts) or "없음"
    if t == ev.EVIDENCE_UPDATED:
        suff = {True: "충분", False: "부족", None: "판단 없음"}[e.get("sufficient")]
        arts = ", ".join(e.get("articles") or []) or "선택 조항 없음"
        return f"근거 갱신 v{e.get('version')}", f"{arts} · 충분성 {suff} · 행동 {lb.label(lb.ACTION, e.get('action')) or '-'}"
    if t == ev.POLICY_DECIDED:
        return "정책 결정", f"{lb.label(lb.RULE, e.get('rule'))} ({e.get('rule')}) → {lb.label(lb.ACTION, e.get('action'))}"
    if t == ev.QUOTE_SELECTED:
        return "견적 선택", lb.label(lb.QUOTE_REASON, e.get("chosen")) or "없음"
    if t == ev.SUGGESTION_CREATED:
        return f"제안 생성 {_short(e.get('suggestion_id'))}", f"기준 v{e.get('basis_version')} · {e.get('text')}"
    if t == ev.SUGGESTION_EXPOSED:
        return f"노출 {_short(e.get('suggestion_id'))}", f"채널 {e.get('channel')}"
    if t == ev.SUGGESTION_INVALIDATED:
        reasons = "; ".join(lb.invalidation_reason(r) for r in e.get("reasons") or [])
        return f"무효화 {_short(e.get('suggestion_id'))}", f"기준 v{e.get('basis_version')} → v{e.get('invalidated_version')}: {reasons}"
    if t == ev.TASK_CANCELLED:
        return "작업 취소", f"{lb.label(lb.STAGE, e.get('stage'))} 단계, 시작 v{e.get('started_version')}, 그때 v{e.get('version')}"
    if t == ev.RESULT_DISCARDED:
        return "결과 폐기", f"{lb.label(lb.STAGE, e.get('stage'))}, 시작 v{e.get('started_version')}, 그때 v{e.get('version')}"
    if t == ev.WRITES_WAIT:
        return "쓰기 대기", f"앞 턴의 쓰기 {e.get('writes')}건"
    if t == ev.ENGINE_ERROR:
        return "엔진 오류", str(e.get("error"))
    if t == ev.FAULT_INJECTED:
        return "장애 주입", str(e.get("faults"))
    if t in (ev.STT_PARTIAL, ev.STT_FINAL):
        return ("미확정 자막" if t == ev.STT_PARTIAL else "확정 자막"), str(e.get("text"))
    return t, ""


def build_timeline(events: list[dict[str, Any]]) -> dict[str, Any]:
    starts = [i for i, e in enumerate(events) if _is_customer_utt(e)]
    owner = version_owners(events)
    by_utt = {events[i]["utt_id"]: events[i] for i in starts}
    rows: dict[str | None, list[dict[str, Any]]] = defaultdict(list)  # None = 첫 발화 전(세션 시작, 예열)
    open_bars: dict[tuple[str, Any], deque] = defaultdict(deque)
    tool_rows: dict[str, dict[str, Any]] = {}

    def turn_of(e: dict[str, Any]) -> str | None:
        if e["type"] in (ev.STT_PARTIAL, ev.STT_FINAL) and e.get("utt_id") in by_utt:
            return e["utt_id"]
        if e["type"] == ev.FAULT_INJECTED and e.get("before_utt") in by_utt:
            return e["before_utt"]
        if e["type"] == ev.VERSION_BUMPED:
            return owner.get(int(e["to"]))
        if e["type"] == ev.SUGGESTION_INVALIDATED:  # 기준 버전이 아니라 무효화한 파이프라인의 턴
            return owner.get(int(e.get("invalidated_version") or e["version"]))
        v = stage_version(e)
        return owner.get(v) if v is not None else None

    def add(turn: str | None, row: dict[str, Any]) -> dict[str, Any]:
        rows[turn].append(row)
        return row

    meta: dict[str, Any] = {}
    for e in events:
        t = e["type"]
        tm = float(e["t_ms"])
        if t == ev.SESSION_START:
            meta = {k: e.get(k) for k in ("session", "org", "today", "policy", "decision_backend", "generator", "channel",
                                          "question_sets", "generation_prompt", "rag_prompt")}
            meta["t_ms"] = tm
            continue
        if t in SKIP_TYPES or _is_customer_utt(e):
            continue
        turn = turn_of(e)
        if t == ev.WARMUP:
            add(None, {"kind": "bar", "stage": "warmup", "label": f"예열 {e.get('backend')}",
                       "start": tm - float(e.get("wall_ms") or e.get("latency_ms") or 0), "end": tm, "status": "error" if e.get("error") else "ok",
                       "detail": f"{e.get('latency_ms')} ms" + (f", 오류 {e['error']}" if e.get("error") else "")})
        elif t == ev.RETRIEVAL_DONE:
            dur = float(e.get("duration_ms") or 0)
            srcs = e.get("sources") or {}
            add(turn, {"kind": "bar", "stage": "retrieval", "label": "검색", "start": tm - dur, "end": tm, "status": "ok",
                       "detail": f"후보 {len(e.get('candidates') or [])}개: " + ", ".join(
                           f"{a}({srcs.get(a, '-')})" for a in e.get("candidates") or [])})
        elif t == ev.DECISION_REQUEST:
            row = add(turn, {"kind": "bar", "stage": "decision", "label": f"판별 {e.get('backend')}", "start": tm,
                             "end": None, "status": "open",
                             "detail": f"질문 {len(e.get('questions') or [])}개, 상태 {e.get('state_chars')}자"})
            open_bars[("decision", stage_version(e))].append(row)
        elif t == ev.DECISION_RESPONSE:
            q = open_bars[("decision", stage_version(e))]
            row = q.popleft() if q else add(turn, {"kind": "bar", "stage": "decision", "label": f"판별 {e.get('backend')}",
                                                    "start": tm - float(e.get("latency_ms") or 0), "detail": ""})
            row.update(end=tm, status="error" if e.get("error") else "ok")
            row["detail"] += f" · 응답 {e.get('latency_ms')} ms, 시도 {e.get('attempts')}" + \
                (f", 비용 ${e['cost_usd']:.6f}" if e.get("cost_usd") else "") + (f", 오류 {e['error']}" if e.get("error") else "")
        elif t == ev.GENERATION_REQUEST:
            step = e.get("step")
            row = add(turn, {"kind": "bar", "stage": "generation",
                             "label": f"생성 {e.get('model')}" + (f" 단계 {step}" if step else ""), "start": tm, "end": None,
                             "status": "open", "detail": f"규칙 {e['rule']}" if e.get("rule") else ""})
            open_bars[("generation", stage_version(e), step)].append(row)
        elif t == ev.GENERATION_RESPONSE:
            q = open_bars[("generation", stage_version(e), e.get("step"))]
            row = q.popleft() if q else add(turn, {"kind": "bar", "stage": "generation", "label": f"생성 {e.get('model')}",
                                                    "start": tm - float(e.get("latency_ms") or 0), "detail": ""})
            bad = e.get("error") or e.get("fallback")
            row.update(end=tm, status="error" if bad else "ok")
            extra = [f"{e.get('latency_ms')} ms", f"토큰 {e.get('input_tokens')}/{e.get('output_tokens')}"]
            if e.get("calls"):
                extra.append("함수 " + ", ".join(e["calls"]))
            if e.get("attempts") and e["attempts"] > 1:
                extra.append(f"시도 {e['attempts']}")
            if e.get("fallback"):
                extra.append("고정 문구")
            if e.get("violations"):
                extra.append(f"검사 위반 {e['violations']}")
            if e.get("error"):
                extra.append(f"오류 {e['error']}")
            row["detail"] = " · ".join(([row["detail"]] if row.get("detail") else []) + extra)
        elif t == ev.TOOL_CALL:
            tool_rows[e["call_id"]] = add(turn, {"kind": "bar", "stage": "tool",
                                                 "label": f"도구 {e.get('tool')}", "start": tm, "end": None,
                                                 "status": "open", "detail": "", "args": e.get("args") or {},
                                                 "tool": e.get("tool"), "version": stage_version(e)})
        elif t == ev.TOOL_RESULT:
            row = tool_rows.get(e["call_id"]) or add(turn, {"kind": "bar", "stage": "tool", "label": f"도구 {e.get('tool')}",
                                                            "start": tm - float(e.get("latency_ms") or 0), "args": {}})
            row.update(end=tm, status="ok" if e.get("ok") else "error",
                       detail=f"{tool_summary(e.get('tool'), bool(e.get('ok')), e.get('error_code'), e.get('data'), row.get('args'))}"
                              f" · {e.get('latency_ms')} ms, 시도 {e.get('attempts')}")
        else:  # POINT_TYPES와 모르는 유형은 점으로 둔다
            name, detail = _point_label(e)
            row = add(turn, {"kind": "point", "stage": t, "label": name, "start": tm, "end": None, "status": "ok",
                             "detail": detail})
            if t == ev.TASK_CANCELLED:  # 응답 없이 취소된 판별·생성·조회 막대를 취소 시각에서 닫는다
                for key in [k for k in open_bars if k[0] in ("decision", "generation") and k[1] == e.get("started_version")]:
                    while open_bars[key]:
                        bar = open_bars[key].popleft()
                        bar.update(end=tm, status="cancelled")
                        bar["detail"] += " · 응답 전에 취소됨"
                for bar in tool_rows.values():  # 쓰기는 shield라 취소되지 않고 나중에 결과가 온다
                    if (bar["end"] is None and bar.get("tool") not in WRITE_TOOLS
                            and bar.get("version") == e.get("started_version")):
                        bar.update(end=tm, status="cancelled", detail="결과 전에 취소됨")
                row["status"] = "cancelled"
            elif t == ev.RESULT_DISCARDED:
                row["status"] = "discarded"
            elif t == ev.SUGGESTION_INVALIDATED:
                row["status"] = "invalidated"
            elif t == ev.ENGINE_ERROR:
                row["status"] = "error"

    latencies = {x["utt_id"]: x for x in ev.turn_latencies(events)}
    turns = []
    for n, i in enumerate(starts):
        u = events[i]
        t0 = float(u["t_ms"])
        nxt = float(events[starts[n + 1]]["t_ms"]) - t0 if n + 1 < len(starts) else None
        lat = latencies.get(u["utt_id"], {})
        marker = u.get("marker") or {}
        refresh = None
        if marker.get("condition_change"):
            refresh = {"latency_ms": lat.get("evidence_ms"), "invalidates_prior": bool(marker.get("invalidates_prior")),
                       "event": marker.get("event")}
        trows = []
        for r in sorted(rows.get(u["utt_id"], []), key=lambda r: r["start"]):
            r = {k: v for k, v in r.items() if k not in ("args", "tool", "version")}
            r["start"] = round(r["start"] - t0, 1)
            r["end"] = None if r["end"] is None else round(r["end"] - t0, 1)
            trows.append(r)
        ends = [r["end"] if r["end"] is not None else r["start"] for r in trows] + [0.0]
        if refresh and refresh["latency_ms"] is not None:
            ends.append(refresh["latency_ms"])
        lo = min([r["start"] for r in trows] + [0.0])
        hi = max(ends + ([nxt] if nxt is not None and nxt <= max(ends) else []))
        turns.append({"utt_id": u["utt_id"], "text": u.get("text"), "marker": u.get("marker"), "t0": t0,
                      "wall": u.get("wall"), "version": lat.get("version"), "next_offset": None if nxt is None else round(nxt, 1),
                      "evidence_ms": lat.get("evidence_ms"), "say_ms": lat.get("say_ms"),
                      "retrieval_ms": lat.get("retrieval_ms"), "decision_ms": lat.get("decision_ms", []),
                      "tool_ms": lat.get("tool_ms", []), "generation_ms": lat.get("generation_ms", []),
                      "discarded": lat.get("discarded", 0), "cancelled": lat.get("cancelled", 0),
                      "refresh": refresh, "rows": trows, "span": [round(lo, 1), round(max(hi, lo + 1.0), 1)]})
    origin = meta.get("t_ms", float(events[0]["t_ms"]) if events else 0.0)
    pre = []
    for r in sorted(rows.get(None, []), key=lambda r: r["start"]):
        r = {k: v for k, v in r.items() if k not in ("args", "tool", "version")}
        r["start"] = round(r["start"] - origin, 1)
        r["end"] = None if r["end"] is None else round(r["end"] - origin, 1)
        pre.append(r)
    channels = sorted({e.get("channel") for e in events if e["type"] == ev.SUGGESTION_EXPOSED and e.get("channel")})
    return {"meta": meta, "channels": channels, "n_events": len(events), "pre": pre, "turns": turns,
            "refresh_events": sum(1 for x in turns if x["refresh"]),
            "refresh_misses": sum(1 for x in turns if x["refresh"] and x["refresh"]["latency_ms"] is None)}
