// 평가 대시보드. 서버(jikgam/web/dashboard.py)가 원본 로그에서 계산한 값만 표로 그린다.
"use strict";

const $ = (id) => document.getElementById(id);
const LOCAL_KINDS = new Set(["majority", "rules"]);

async function init() {
  let d;
  try {
    d = await getJSON("/api/dashboard");
  } catch (e) {
    $("dash-error").textContent = `대시보드 자료를 만들지 못했다: ${e.message}`;
    $("dash-error").hidden = false;
    return;
  }
  const note = clear($("note"));
  note.append(el("strong", { text: "합성 데이터, 운영 성과 아님. " }), d.note);
  $("root-line").textContent = `결과 폴더: ${d.results_root}` + (d.allow_holdout ? " · 보류용 실행 표시 켜짐" : " · 보류용 시나리오 실행은 잠김");
  renderDecisionRuns(d.decision_runs);
  renderGroups(d.scenario_groups);
  renderMatrices(d.scenario_groups);
  renderRuns(d.scenario_runs);
  renderGroupDetails(d.scenario_groups);
}

function th(text, cls) { return el("th", { scope: "col", class: cls, text }); }
function td(content, cls) { return el("td", { class: cls }, content); }
function sub(text) { return el("span", { class: "sub", text }); }

// --- (a) -------------------------------------------------------------------------------------

function accCell(acc, text) {
  if (!isNum(acc)) return text || "-";
  return el("div", { class: "acc" },
    el("span", { class: "bar", "aria-hidden": "true" }, el("span", { style: `width:${(acc * 100).toFixed(1)}%` })),
    el("span", { class: "num", text }));
}

function renderDecisionRuns(runs) {
  const box = clear($("decision-runs"));
  if (!runs.length) { box.append(el("p", { class: "muted", text: "실행 폴더가 없다." })); return; }
  const ordered = [...runs].reverse();
  ordered.forEach((r, i) => {
    const card = el("details", { class: "run-card", open: i === 0 });
    if (r.error) {
      card.append(el("summary", {}, el("strong", { text: r.run_id }), el("span", { class: "badge danger", text: "읽기 오류" })),
        el("div", { class: "inner error-box", text: r.error }));
      box.append(card);
      return;
    }
    card.append(el("summary", {},
      el("strong", { class: "mono", text: r.run_id }),
      el("span", { class: "badge info", text: `질문 세트 ${r.questions_version}` }),
      el("span", { class: "muted small", text: r.backends.map((b) => b.backend).join(", ") }),
      el("span", { class: "muted small", text: `${r.split} · 사례 ${r.cases}건 · 판단 ${r.decisions_per_rep}개/회차` }),
      r.reconstructed ? el("span", { class: "badge warn", text: "meta.json 복원본" }) : null));
    const inner = el("div", { class: "inner" });
    const capId = `cap-${r.run_id}`;
    const table = el("table", {},
      el("caption", { id: capId, text: `${r.run_id} 백엔드별 요약` }),
      el("thead", {}, el("tr", {}, th("백엔드"), th("응답 모델"), th("회차", "r"), th("정확도 (정답 수)"), th("ECE", "r"),
        th("p50 ms", "r"), th("p95 ms", "r"), th("평균 입력 토큰", "r"), th("요청당 USD", "r"), th("오류/호출", "r"),
        th("재시도", "r"), th("회차 간 라벨 일치", "r"))));
    const body = el("tbody");
    for (const b of r.backends) {
      const local = LOCAL_KINDS.has(b.kind);
      const localCell = () => td([el("span", { text: "-" }), sub("로컬 기준선")], "r");
      body.append(el("tr", {},
        el("th", { scope: "row" }, b.backend, b.served ? sub("이 PC 서빙") : null),
        td(b.models.join(", ")),
        td(String(b.reps ?? "-"), "r num"),
        td(accCell(b.accuracy, b.accuracy_text)),
        td(isNum(b.ece) ? b.ece.toFixed(3) : "-", "r num"),
        local ? localCell() : td(fmtMs(b.p50_ms), "r num"),
        local ? localCell() : td([fmtMs(b.p95_ms), sub(`성공 호출 ${b.latency_n}개`)], "r num"),
        local ? localCell() : td(isNum(b.input_tokens) ? nf0.format(b.input_tokens) : "-", "r num"),
        local ? localCell() : td(fmtUsd(b.cost_per_request), "r num"),
        td(`${b.errors}/${b.calls}`, "r num"),
        td(String(b.retries), "r num"),
        td(fmtPct(b.stability), "r num")));
    }
    table.append(body);
    inner.append(el("div", { class: "table-wrap", tabindex: "0", role: "region", "aria-labelledby": capId }, table));

    const qcap = `qcap-${r.run_id}`;
    const qt = el("table", {}, el("caption", { id: qcap, text: "질문 유형별 정확도 (정답 수)" }),
      el("thead", {}, el("tr", {}, th("질문 유형"), ...r.backends.map((b) => th(b.backend)))));
    const qb = el("tbody");
    for (const q of r.qtypes) {
      qb.append(el("tr", {}, el("th", { scope: "row" }, q.qtype, sub(`회차당 ${q.n}개`)),
        ...r.backends.map((b) => td(q.cells[b.backend].text, "num"))));
    }
    qt.append(qb);
    inner.append(el("div", { class: "table-wrap", tabindex: "0", role: "region", "aria-labelledby": qcap }, qt));

    const net = r.network ? `Cloudflare ${r.network.colo} (${r.network.loc}), TCP 연결 중앙값 ` +
      Object.entries(r.network.tcp_ms || {}).map(([h, ms]) => `${h} ${ms}ms`).join(", ") : "기록 없음";
    inner.append(el("ul", { class: "cond" },
      el("li", { text: `시각(KST): ${r.started} ~ ${r.finished || "?"}` }),
      el("li", { text: `코드 ${String(r.git_commit).slice(0, 12)}${r.git_dirty ? " (커밋 안 된 변경 있음)" : ""}` }),
      el("li", { text: `데이터 ${r.data_file} sha256 앞 16자리 ${r.data_sha256_16}` }),
      el("li", { text: `LLM 프롬프트 ${r.llm_prompt_version}, 동시 실행 ${r.concurrency}` }),
      el("li", { text: `네트워크(측정 전): ${net}` })));
    card.append(inner);
    box.append(card);
  });
}

// --- (b) -------------------------------------------------------------------------------------

function refreshText(r) {
  if (!r || !r.events) return ["이벤트 없음"];
  return [`${fmtMs(r.p50)} / ${fmtMs(r.p95)}`, sub(`이벤트 ${r.events}개, 미갱신 ${r.misses}개`)];
}

function staleText(st) {
  const chans = Object.entries(st.by_channel);
  if (!chans.length) return ["노출 없음"];
  return [chans.map(([ch, c]) => `${ch}: ${c.invalid}/${c.denominator}${c.denominator ? ` (${fmtPct(c.invalid / c.denominator)})` : ""}`).join(", "),
    sub(chans.some(([, c]) => c.unconfirmed) ? `미재확인 노출 ${chans.map(([, c]) => c.unconfirmed).reduce((a, b) => a + b, 0)}` : "")];
}

function renderGroups(groups) {
  const table = clear($("group-table"));
  table.append(el("caption", { text: "묶음별 판정·지표·비용 (유효 실행 기준)" }),
    el("thead", {}, el("tr", {}, th("묶음"), th("세트"), th("실행", "r"), th("세션", "r"), th("통과 / 유효", "r"),
      th("실패", "r"), th("판정 불가", "r"), th("무효", "r"), th("근거 갱신 p50 / p95 ms"), th("무효 노출 (무효/분모)"),
      th("무효 노출 있었던 이벤트", "r"), th("엔진 비용 USD", "r"), th("평가 비용 USD", "r"))));
  const body = el("tbody");
  for (const g of groups) {
    const s = g.stats;
    const name = el("th", { scope: "row" }, g.label,
      g.in_progress ? el("span", { class: "badge warn", text: "진행 중" }) : null,
      g.locked.length ? sub(`잠긴 보류용 실행 ${g.locked.length}개`) : null,
      g.errors.length ? sub(`읽기 오류 ${g.errors.length}개`) : null);
    const setBadge = el("span", { class: `badge ${g.holdout ? "warn" : ""}`, text: g.set_label || "-" });
    if (!s) {
      body.append(el("tr", {}, name, td(setBadge), td(String(g.runs.length), "r num"),
        el("td", { colspan: "10", class: "muted", text: g.locked.length ? "보류용 시나리오: 엔진 설정 고정 전 열람 금지(data/scenarios/HOLDOUT.md)" : "계산할 세션 없음" })));
      continue;
    }
    body.append(el("tr", {}, name, td(setBadge), td(String(g.runs.length), "r num"), td(String(s.sessions), "r num"),
      td(fmtRatio(s.passed, s.valid), "r num"), td(String(s.failed), "r num"), td(String(s.undetermined), "r num"),
      td(String(s.invalid), "r num"), td(refreshText(s.refresh_valid), "num"), td(staleText(s.stale), "num"),
      td(`${s.stale.events_with_invalid}/${s.stale.events_total}`, "r num"),
      td([fmtUsd(s.cost.engine, 4), sub(`판별 ${fmtUsd(s.cost.decision, 4)} · 생성 ${fmtUsd(s.cost.generation, 4)}`)], "r num"),
      td([fmtUsd(s.cost.customer + s.cost.judge, 4), sub(`고객 AI ${fmtUsd(s.cost.customer, 4)} · 판정 ${fmtUsd(s.cost.judge, 4)}`)], "r num")));
  }
  if (!groups.length) body.append(el("tr", {}, el("td", { colspan: "13", class: "muted", text: "실행 폴더가 없다." })));
  table.append(body);
}

function renderMatrices(groups) {
  const box = clear($("matrices"));
  const families = new Map();
  for (const g of groups) {
    if (!g.stats) continue;
    const key = `${g.prefix}|${g.set_label}`;
    if (!families.has(key)) families.set(key, []);
    families.get(key).push(g);
  }
  for (const [key, gs] of families) {
    if (gs.length < 2 && !gs.some((g) => g.runs.length > 1)) continue;  // 비교할 정책이나 반복이 있을 때만
    const [prefix, setLabel] = key.split("|");
    const scenarios = [...new Set(gs.flatMap((g) => Object.keys(g.stats.by_scenario)))].sort();
    const capId = `mx-${prefix}`;
    const table = el("table", { class: "matrix" },
      el("caption", { id: capId, text: `${prefix} · ${setLabel}: 시나리오별 통과(유효 실행 중)` }),
      el("thead", {}, el("tr", {}, th("시나리오"), ...gs.map((g) => th(`정책 ${g.policy}`)))));
    const body = el("tbody");
    for (const sc of scenarios) {
      const cells = gs.map((g) => {
        const marks = g.stats.by_scenario[sc] || [];
        if (!marks.length) return td("-", "muted");
        const inval = marks.filter((m) => m === "무효").length;
        const und = marks.filter((m) => m === "판정 불가").length;
        const valid = marks.length - inval;
        const pass = marks.filter((m) => m === "통과").length;
        const extra = [inval ? `무효 ${inval}` : "", und ? `판정 불가 ${und}` : ""].filter(Boolean).join(", ");
        const cls = valid && pass === valid ? "pass" : valid && pass === 0 && !und ? "fail" : "";
        return td([`통과 ${pass}/${valid}`, extra ? sub(extra) : null], `num ${cls}`);
      });
      body.append(el("tr", {}, el("th", { scope: "row", text: sc }), ...cells));
    }
    table.append(body);
    box.append(el("h3", { text: `${prefix} 시나리오별 비교` }),
      el("div", { class: "table-wrap", tabindex: "0", role: "region", "aria-labelledby": capId }, table));
  }
}

function renderRuns(runs) {
  const table = clear($("run-table"));
  table.append(el("caption", { text: "실행 폴더별" }),
    el("thead", {}, el("tr", {}, th("실행"), th("정책"), th("세트"), th("판별 / 생성"), th("세션", "r"), th("통과 / 유효", "r"),
      th("무효", "r"), th("근거 갱신 p50 / p95 ms"), th("무효 노출"), th("엔진 비용 USD", "r"), th("시각"), th("코드"))));
  const body = el("tbody");
  for (const r of [...runs].reverse()) {
    const i = r.info;
    const s = r.stats;
    const name = el("th", { scope: "row" }, el("span", { class: "mono", text: i.run_id }),
      i.in_progress ? el("span", { class: "badge warn", text: "진행 중" }) : null);
    const common = [name, td(i.policy || "-"), td(el("span", { class: `badge ${i.holdout ? "warn" : ""}`, text: i.set_label || "-" })),
      td([i.decision_model || i.decision_backend || "없음(RAG)", sub(`생성 ${i.generator_model || "-"}`)])];
    const when = td([i.started || "-", sub(i.ended ? `끝 ${i.ended}` : "끝 기록 없음")], "small");
    const code = td(el("code", { text: String(i.git_commit || "-").slice(0, 10) + (i.git_dirty ? " *" : "") }));
    if (r.locked) {
      body.append(el("tr", {}, ...common, el("td", { colspan: "6", class: "muted", text: "보류용 시나리오: 엔진 설정 고정 전 열람 금지" }), when, code));
    } else if (r.error || !s) {
      body.append(el("tr", {}, ...common, el("td", { colspan: "6", class: "muted", text: `읽기 오류: ${r.error}` }), when, code));
    } else {
      body.append(el("tr", {}, ...common, td(String(s.sessions), "r num"), td(fmtRatio(s.passed, s.valid), "r num"),
        td(String(s.invalid), "r num"), td(refreshText(s.refresh_valid), "num"), td(staleText(s.stale), "num"),
        td(fmtUsd(s.cost.engine, 4), "r num"), when, code));
    }
  }
  table.append(body);
}

function renderGroupDetails(groups) {
  const box = clear($("group-details"));
  box.append(el("h3", { text: "묶음별 세부" }));
  for (const g of groups) {
    const s = g.stats;
    if (!s) continue;
    const card = el("details", { class: "run-card group-detail" }, el("summary", {}, el("strong", { text: g.label }),
      el("span", { class: "muted small", text: `${g.set_label} · 실행 ${g.runs.map((r) => r.run_id).join(", ")}` })));
    const inner = el("div", { class: "inner" });
    inner.append(el("p", { class: "small", text: `항목별 실패 수(유효 실행 ${s.valid}개, docs/evaluation.md §4.1 항목 번호):` }),
      el("ul", { class: "items" }, Object.keys(s.item_fail).map((n) => el("li", { text: `${n}. ${s.item_names[n]} ${s.item_fail[n]}` }))));
    inner.append(el("ul", { class: "cond" },
      el("li", { text: `10번을 마지막 제안 하나로 읽으면(판정에 쓰지 않음) 실패 ${s.item10_last_fail}/${s.valid}` }),
      el("li", { text: `무효 실행 ${s.invalid}개${s.invalid_scenarios.length ? `: ${s.invalid_scenarios.join(", ")}` : ""}` }),
      el("li", { text: `근거 갱신(전체 실행 포함): ${fmtMs(s.refresh_all.p50)} / ${fmtMs(s.refresh_all.p95)} ms, 이벤트 ${s.refresh_all.events}개, 미갱신 ${s.refresh_all.misses}개` }),
      el("li", { text: `빠른 정정 ${s.fast_corrections}건, 작업 취소 ${s.cancelled}건, 결과 폐기 ${s.discarded}건` }),
      el("li", { text: `비용 합계 ${fmtUsd(s.cost.total, 4)} (엔진 ${fmtUsd(s.cost.engine, 4)}, 고객 AI ${fmtUsd(s.cost.customer, 4)}, 판정 ${fmtUsd(s.cost.judge, 4)})` })));
    card.append(inner);
    box.append(card);
  }
}

init();
