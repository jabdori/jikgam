// 턴 상세 타임라인. 서버(jikgam/web/timeline.py)가 계산한 막대·점을 턴마다 그린다.
"use strict";

const $ = (id) => document.getElementById(id);
const STATUS_WORD = { cancelled: "취소됨", discarded: "폐기", invalidated: "무효화", error: "오류", open: "끝 기록 없음" };
const POINT_CLASS = {
  "evidence.updated": "evidence", "suggestion.exposed": "exposed", "suggestion.invalidated": "invalidated",
  "task.cancelled": "cancelled", "result.discarded": "discarded", "engine.error": "error",
};

function currentPath() {
  const m = location.pathname.match(/^\/timeline\/(.+)$/);
  return m ? decodeURIComponent(m[1]) : "";
}

async function init() {
  const path = currentPath();
  $("pick-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const p = $("log-select").value;
    if (p) location.href = `/timeline/${p.split("/").map(encodeURIComponent).join("/")}`;
  });
  loadPicker(path);
  if (path) await loadTimeline(path);
}

async function loadPicker(path) {
  const sel = $("log-select");
  try {
    const groups = await getJSON("/api/logs");
    clear(sel);
    sel.append(el("option", { value: "", text: "로그를 고른다" }));
    let locked = 0;
    for (const g of groups) {
      const label = `${g.root === "console" ? "콘솔" : "결과"} · ${g.dir || "(최상위)"}${g.policy ? ` · 정책 ${g.policy}` : ""}`;
      const og = el("optgroup", { label });
      if (g.locked) {
        locked += 1;
        og.append(el("option", { value: "", disabled: true, text: "보류용 시나리오 실행: 엔진 설정 고정 전 열람 금지" }));
      }
      for (const f of g.files) og.append(el("option", { value: f.path, text: f.name, selected: f.path === path }));
      sel.append(og);
    }
    $("pick-note").textContent = `로그 ${groups.reduce((n, g) => n + g.files.length, 0)}개` +
      (locked ? `, 잠긴 보류용 실행 폴더 ${locked}개(서버를 --allow-holdout으로 띄울 때만 열린다)` : "") +
      ". 시뮬레이터 기록(*.sim.jsonl)은 엔진 로그가 아니라서 빠진다.";
  } catch (e) {
    $("pick-note").textContent = `로그 목록을 읽지 못했다: ${e.message}`;
  }
}

async function loadTimeline(path) {
  const err = $("tl-error");
  err.hidden = true;
  try {
    const data = await getJSON(`/api/timeline?path=${encodeURIComponent(path)}`);
    render(data);
  } catch (e) {
    err.textContent = `타임라인을 만들지 못했다: ${e.message}`;
    err.hidden = false;
  }
}

function render(d) {
  const meta = $("tl-meta");
  clear(meta);
  meta.hidden = false;
  $("legend").hidden = false;
  const m = d.meta || {};
  const policy = m.policy || (m.decision_backend === "gemini-flash" ? "B" : m.decision_backend ? "C" : "-");
  meta.append(el("h2", { id: "h-session", text: `세션 ${m.session || "-"}` }));
  const dl = el("dl", { class: "kv" });
  const add = (k, v) => dl.append(el("dt", { text: k }), el("dd", { text: v }));
  add("로그", d.path);
  add("정책", `${policy}${m.rag_prompt ? ` (RAG 프롬프트 ${m.rag_prompt})` : ""}`);
  add("판별", m.decision_backend || "없음(RAG)");
  add("생성", m.generator || "-");
  add("채널", d.channels.length ? d.channels.join(", ") : m.channel || "-");
  if (m.question_sets) add("질문 세트", m.question_sets.join(", "));
  add("이벤트", `${nf0.format(d.n_events)}줄, 고객 발화 ${d.turns.length}개`);
  add("조건 변경 표시 발화", d.refresh_events ? `${d.refresh_events}개, 미갱신 ${d.refresh_misses}개` : "없음");
  meta.append(dl);
  if (policy === "A") meta.append(el("p", { class: "muted small", text: "정책 A(RAG)는 판별·대화 버전·무효화가 없다. 근거 갱신은 답이 나온 시점이다(jikgam/baselines/rag.py)." }));

  const box = clear($("turns"));
  if (d.pre.length) {
    const sec = el("section", { class: "panel turn", "aria-label": "첫 발화 전" },
      el("div", { class: "turn-head" }, el("h2", { text: "세션 시작" }), el("span", { class: "muted small", text: "0 ms = session.start" })));
    const hi = Math.max(1, ...d.pre.map((r) => r.end ?? r.start));
    sec.append(waterfall(d.pre, [0, hi], null, null));
    box.append(sec);
  }
  for (const t of d.turns) box.append(turnSection(t));
  // 주소에 #h-u3처럼 턴이 있으면 그 턴으로 간다(자료를 받은 뒤에 그리므로 브라우저의 기본 이동이 닿지 않는다)
  const target = location.hash ? document.getElementById(location.hash.slice(1)) : null;
  if (target) target.scrollIntoView();
}

function turnSection(t) {
  const mk = t.marker || {};
  const sec = el("section", { class: "panel turn", "aria-labelledby": `h-${t.utt_id}` });
  const head = el("div", { class: "turn-head" },
    el("h2", { id: `h-${t.utt_id}` }, `${t.utt_id} `, el("span", { class: "quote", text: `고객: ${t.text}` })));
  if (mk.condition_change) head.append(el("span", { class: "badge warn", text: mk.invalidates_prior ? "조건 변경 표시 · 이전 제안 무효" : "조건 변경 표시" }));
  if (mk.event) head.append(el("code", { class: "small", text: mk.event }));
  head.append(el("span", { class: "muted small", text: `대화 버전 v${t.version ?? "-"}` }));
  sec.append(head);

  const stats = el("dl");
  const stat = (k, v) => stats.append(el("div", {}, el("dt", { text: k }), el("dd", { text: v })));
  stat("근거 갱신", t.evidence_ms === null ? "다음 발화 전 없음(미갱신)" : `${fmtMs(t.evidence_ms)} ms`);
  stat("첫 응답", t.say_ms === null ? "다음 발화 전 없음" : `${fmtMs(t.say_ms)} ms`);
  stat("검색 합", `${fmtMs(t.retrieval_ms)} ms`);
  stat("판별", t.decision_ms.length ? `${t.decision_ms.length}회 (${t.decision_ms.map(fmtMs).join(", ")} ms)` : "없음");
  stat("도구", t.tool_ms.length ? `${t.tool_ms.length}개 (합 ${fmtMs(t.tool_ms.reduce((a, x) => a + x[1], 0))} ms)` : "없음");
  stat("생성", t.generation_ms.length ? `${t.generation_ms.length}회 (${t.generation_ms.map(fmtMs).join(", ")} ms)` : "없음");
  stat("폐기·취소", `${t.discarded} / ${t.cancelled}`);
  if (t.next_offset !== null) stat("다음 발화", `+${fmtMs(t.next_offset)} ms`);
  sec.append(el("div", { class: "turn-stats" }, stats));
  sec.append(el("p", { class: "muted small", text: "턴 판정 수치는 jikgam.engine.events.turn_latencies(재생 보고서와 같은 함수)로 계산했다." }));

  const rows = [...t.rows];
  if (t.refresh) {
    const lat = t.refresh.latency_ms;
    rows.unshift({ kind: lat === null ? "point" : "bar", stage: "refresh", label: "근거 갱신 시간(§3.2)",
      start: 0, end: lat, status: lat === null ? "error" : "ok",
      detail: lat === null ? "다음 고객 발화 전에 이 발화 버전의 evidence.updated가 없다(미갱신)" :
        `utterance.end → 이 발화 버전 이상의 첫 evidence.updated${t.refresh.invalidates_prior ? " · 이전 제안을 무효로 만드는 변경" : ""}` });
  }
  sec.append(waterfall(rows, t.span, t.next_offset, t.utt_id));
  return sec;
}

function waterfall(rows, span, nextOffset, uttId) {
  const [lo, hi] = span;
  const width = Math.max(hi - lo, 1);
  const pct = (x) => Math.min(100, Math.max(0, ((x - lo) / width) * 100));
  const vars = [];
  if (nextOffset !== null && nextOffset <= hi) vars.push(`--next:${pct(nextOffset)}%`);
  if (lo < 0) vars.push(`--zero:${pct(0)}%`);
  const list = el("ol", { class: "wf", "aria-label": uttId ? `${uttId} 단계 목록` : "세션 시작 단계 목록" });
  const ticks = el("div", { class: "ticks", "aria-hidden": "true" });
  for (const f of [0, 0.25, 0.5, 0.75, 1]) {
    const x = lo + width * f;
    ticks.append(el("span", { style: `left:${f * 100}%`, text: `${fmtMs(x)} ms` }));
  }
  const nextNote = nextOffset === null ? "" : nextOffset <= hi ? "세로선 = 다음 발화" : `다음 발화 +${fmtMs(nextOffset)} ms(범위 밖)`;
  list.append(el("li", { class: "wf-row axis", "aria-hidden": "true" }, el("span", { class: "label" }), ticks, el("span", { class: "nums", text: nextNote })));
  for (const r of rows) {
    const track = el("div", { class: "track", style: vars.join(";"), "aria-hidden": "true" });
    let nums;
    if (r.kind === "bar") {
      const end = r.end ?? hi;
      const cls = `bar ${r.status === "cancelled" ? "cancelled" : r.status === "open" ? "open" : r.status === "error" ? "error" : ""} ${r.stage === "refresh" ? "refresh" : `stage-${r.stage}`}`;
      track.append(el("span", { class: cls, style: `left:${pct(r.start)}%;width:${Math.max(pct(end) - pct(r.start), 0.3)}%` }));
      nums = r.end === null ? `+${fmtMs(r.start)} → (끝 기록 없음)` : `+${fmtMs(r.start)} → ${fmtMs(r.end)} ms (${fmtMs(r.end - r.start)})`;
    } else {
      const cls = POINT_CLASS[r.stage] || (r.status !== "ok" ? r.status : "");
      track.append(el("span", { class: `pt ${cls}`, style: `left:${pct(r.start)}%` }));
      nums = `+${fmtMs(r.start)} ms`;
    }
    const word = STATUS_WORD[r.status];
    const label = el("span", { class: "label" }, el("span", { text: r.label }),
      word && r.status !== "ok" ? el("span", { class: `status-text ${r.status}`, text: ` · ${word}` }) : null,
      r.detail ? el("span", { class: "detail", text: r.detail }) : null);
    list.append(el("li", { class: `wf-row${r.stage === "refresh" ? " refresh-row" : ""}` }, label, track, el("span", { class: "nums", text: nums })));
  }
  return list;
}

init();
