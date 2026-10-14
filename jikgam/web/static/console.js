// 상담 콘솔. 서버가 SSE로 보내는 화면 상태(view)를 그대로 그린다. 상태 계산은 서버(jikgam/web/view.py)가 한다.
"use strict";

const ui = {
  config: null,
  sid: null,
  source: null,
  session: null,
  view: null,
  rendered: { transcript: 0, activity: 0 },
  announced: new Set(),
};

const $ = (id) => document.getElementById(id);

async function init() {
  try {
    ui.config = await getJSON("/api/config");
  } catch (e) {
    $("env-line").textContent = `설정을 읽지 못했다: ${e.message}`;
    return;
  }
  const c = ui.config;
  $("env-line").textContent = `조직 ${c.org} · 기준일 ${c.today} · 모의 업무 API ${c.bizapi_url} · ` +
    `판별 ${c.decision_backend} · 생성 ${c.generator}`;
  for (const p of c.policies) $("policy").append(el("option", { value: p.id, text: `${p.id}. ${p.description}` }));
  for (const r of c.replays) $("replay").append(el("option", { value: r, text: r }));
  for (const s of c.speeds) {
    $("speed").append(el("option", { value: s, text: s === 0 ? "대기 없음(응답 직후)" : `${s}배`, selected: s === 1 }));
  }
  document.querySelectorAll("input[name=mode]").forEach((r) => r.addEventListener("change", syncMode));
  syncMode();
  $("start-form").addEventListener("submit", startSession);
  $("say-form").addEventListener("submit", sendMessage);
  $("handoff-btn").addEventListener("click", requestHandoff);
  $("close-btn").addEventListener("click", closeSession);
  const m = location.hash.match(/s=([\w.-]+)/);
  if (m && c.sessions.some((s) => s.session_id === m[1])) connect(m[1]);
}

function syncMode() {
  const replay = document.querySelector("input[name=mode]:checked").value === "replay";
  document.body.classList.toggle("mode-replay", replay);
}

async function startSession(ev) {
  ev.preventDefault();
  const err = $("start-error");
  err.hidden = true;
  const replay = document.querySelector("input[name=mode]:checked").value === "replay";
  const body = { policy: $("policy").value };
  if (replay) { body.replay = $("replay").value; body.speed = Number($("speed").value); }
  $("start-btn").disabled = true;
  try {
    if (ui.sid && ui.session && !ui.session.closed) await postJSON(`/api/sessions/${ui.sid}/close`);
    const res = await postJSON("/api/sessions", body);
    connect(res.session_id);
  } catch (e) {
    err.textContent = `세션을 시작하지 못했다: ${e.message}`;
    err.hidden = false;
  } finally {
    $("start-btn").disabled = false;
  }
}

function resetPanels() {
  ui.rendered = { transcript: 0, activity: 0 };
  ui.announced = new Set();
  clear($("transcript"));
  clear($("activity"));
  $("transcript-empty").hidden = false;
}

function connect(sid) {
  if (ui.source) ui.source.close();
  ui.sid = sid;
  history.replaceState(null, "", `#s=${sid}`);
  resetPanels();
  $("session-line").hidden = false;
  $("session-id").textContent = sid;
  const src = new EventSource(`/api/sessions/${encodeURIComponent(sid)}/stream`);
  ui.source = src;
  src.addEventListener("update", (e) => {
    const data = JSON.parse(e.data);
    ui.session = data.session;
    ui.view = data.view;
    appendActivity(data.messages);
    render();
  });
  src.addEventListener("closed", () => {
    src.close();
    if (ui.session) ui.session.closed = true;
    render();
  });
  src.onerror = () => {
    if (ui.session && ui.session.closed) src.close();
  };
}

async function sendMessage(ev) {
  ev.preventDefault();
  const input = $("say-input");
  const text = input.value.trim();
  if (!text || !ui.sid) return;
  input.value = "";
  input.focus();
  try {
    await postJSON(`/api/sessions/${ui.sid}/messages`, { text });
  } catch (e) {
    announce(`보내지 못했다: ${e.message}`);
    input.value = text;
  }
}

async function requestHandoff() {
  if (!ui.sid) return;
  $("handoff-btn").disabled = true;
  try {
    await postJSON(`/api/sessions/${ui.sid}/handoff`);
    announce("상담사 인계를 요청했다. 엔진 정책이 인계를 진행한다.");
  } catch (e) {
    $("handoff-note").textContent = `인계 요청 실패: ${e.message}`;
  }
}

async function closeSession() {
  if (!ui.sid) return;
  $("close-btn").disabled = true;
  try { await postJSON(`/api/sessions/${ui.sid}/close`); } catch (e) { announce(e.message); }
}

// --- 그리기 -----------------------------------------------------------------------------------

function render() {
  const v = ui.view;
  const s = ui.session;
  if (!v || !s) return;
  const closed = s.closed;
  const status = $("session-status");
  status.className = "badge " + (closed ? "" : v.pending || s.replaying ? "warn" : "ok");
  status.textContent = closed ? "종료됨" : v.pending ? "판단 중" : s.replaying ? "재생 중" : "응답 대기";
  $("session-policy").textContent = `${s.policy}${s.replay ? ` · 재생 ${s.replay}` : ""}`;
  $("session-version").textContent = `v${v.version}`;
  const c = v.costs;
  $("session-cost").textContent = `${fmtUsd(c.total_usd, 5)} (판별 ${c.decision_calls}회, 생성 ${c.generation_calls}회, 설정 단가 × 토큰)`;
  $("timeline-link").href = `/timeline/console/${s.log_path.split(/[\\/]/).pop()}`;
  $("say-input").disabled = closed;
  $("say-btn").disabled = closed;
  $("close-btn").disabled = closed;
  renderTranscript(v);
  renderCurrent(v);
  renderNext(v);
  renderHandoff(v, s);
  renderHistory(v);
  renderState(v);
  renderEvidence(v);
  renderFacts(v.facts);
  renderClaims(v.claims);
  renderTools(v.tools);
  renderDiscards(v);
  announceChanges(v);
}

function renderTranscript(v) {
  const list = $("transcript");
  const items = v.transcript;
  $("transcript-empty").hidden = items.length > 0;
  // 미확정 자막은 같은 줄을 고쳐 쓰고, 새 줄만 덧붙인다(화면 읽기 프로그램이 새 줄만 읽도록)
  for (let i = 0; i < items.length; i++) {
    const t = items[i];
    const existing = list.children[i];
    if (existing && i < ui.rendered.transcript && existing.dataset.final === "true") {
      updateTranscriptBadges(existing, t, v);
      continue;
    }
    const node = transcriptItem(t, v);
    if (existing) existing.replaceWith(node); else list.append(node);
  }
  ui.rendered.transcript = items.length;
  const box = $("transcript-box");
  if (items.length) box.scrollTop = box.scrollHeight;
}

function transcriptItem(t, v) {
  const who = t.speaker === "customer" ? `고객 ${t.utt_id}` : t.speaker === "engine" ? `상담(제안 ${t.utt_id})` : `${t.speaker} ${t.utt_id}`;
  const li = el("li", { class: `msg ${t.speaker === "customer" ? "customer" : "engine"}${t.final ? "" : " partial"}`, "data-final": String(t.final) },
    el("span", { class: "who" }, who, t.final ? "" : " · 미확정 자막"),
    el("span", { class: "body", text: t.text }));
  const m = t.marker || {};
  if (m.condition_change) {
    li.querySelector(".who").append(el("span", { class: "badge warn", text: m.invalidates_prior ? "조건 변경 표시 · 이전 제안 무효" : "조건 변경 표시" }));
  }
  updateTranscriptBadges(li, t, v);
  return li;
}

function updateTranscriptBadges(li, t, v) {
  if (t.speaker !== "engine" || !t.suggestion_id) return;
  const s = v.suggestions.find((x) => x.id === t.suggestion_id);
  if (s && s.status === "invalidated" && !li.querySelector(".inv-badge")) {
    li.querySelector(".who").append(el("span", { class: "badge danger inv-badge", text: "이후 무효화됨" }));
  }
}

function suggestionMeta(s) {
  const dl = el("dl", { class: "kv small" });
  const add = (k, val) => { if (val !== null && val !== undefined && val !== "") dl.append(el("dt", { text: k }), el("dd", {}, val)); };
  add("기준 버전", `v${s.basis_version}`);
  add("행동", `${s.action_label} (${s.action})`);
  add("규칙", `${s.rule_label} (${s.rule})`);
  if (s.intent) add("요청", `${s.intent_label} (${s.intent})`);
  add("인용 조항", s.cited_articles.length ? el("span", { class: "chips" }, s.cited_articles.map((a) => el("span", { class: "chip", text: a }))) : "없음");
  if (s.amount !== null && s.amount !== undefined) add("안내 금액", `${fmtWon(s.amount)} (${s.amount_final ? "확정 금액으로 안내" : "확정 아님"})`);
  if (s.proposal && s.proposal.length) add("동의하면 접수", s.proposal.join(", "));
  if (s.fallback) add("생성", "검사에 걸려 고정 문구로 대신함");
  return dl;
}

function statusBadge(s) {
  const cls = s.status === "active" ? "ok" : s.status === "invalidated" ? "danger" : "";
  const label = { active: "유효", invalidated: "무효화됨", superseded: "새 제안으로 바뀜" }[s.status] || s.status;
  return el("span", { class: `badge ${cls}`, text: label });
}

function renderCurrent(v) {
  const box = $("current-suggestion");
  clear(box);
  const s = v.current;
  if (!s) {
    box.className = "suggestion-card empty";
    box.append(el("p", { class: "muted", text: v.pending ? "첫 제안을 만드는 중…" : "아직 제안이 없다." }));
    return;
  }
  box.className = `suggestion-card ${s.status}`;
  box.append(el("div", { class: "head" }, el("strong", { text: `현재 제안 ${s.short_id}` }), statusBadge(s),
    el("span", { class: "muted small", text: `기준 대화 버전 v${s.basis_version}` })));
  if (s.status === "invalidated") {
    box.append(el("p", { class: "text" }, el("del", { text: s.text })));
    box.append(el("p", { class: "small", text: `무효화됨(v${s.invalidated_version}): 이유` }));
    box.append(el("ul", { class: "reasons" }, s.reasons.map((r) => el("li", { text: r.label }))));
  } else {
    box.append(el("p", { class: "text", text: s.text }));
  }
  if (v.pending) box.append(el("p", { class: "pending-line", text: `대화 버전 v${v.version}로 다시 판단하는 중… 이 제안은 v${s.basis_version} 기준이다.` }));
  box.append(suggestionMeta(s));
}

function renderNext(v) {
  const box = clear($("next-action"));
  const p = v.policy;
  const e = v.evidence;
  if (!p && !e) { box.append(el("p", { class: "muted", text: "-" })); return; }
  const dl = el("dl", { class: "kv small" });
  const add = (k, val) => { if (val) dl.append(el("dt", { text: k }), el("dd", { text: val })); };
  if (p) {
    add("정책 규칙", `${p.rule_label} (${p.rule}) · v${p.version}`);
    add("행동", `${p.action_label} (${p.action})`);
    if (p.ask) add("물을 것", `${p.ask_label} (${p.ask})`);
    const work = [];
    if (p.calls.length) work.push(`호출 ${p.calls.join(", ")}`);
    if (p.lookups) work.push("기록 조회 묶음");
    if (p.quote_reasons.length) work.push(`견적 ${p.quote_reasons.join(", ")}`);
    if (p.writes.length) work.push(`쓰기 ${p.writes.join(", ")}`);
    add("도구", work.join(" · "));
    if (p.agreed) add("고객 동의", "동의로 판단(접수 실행)");
    if (p.declined) add("고객 의사", "접수를 원하지 않음");
  } else if (e) {
    add("행동(답의 표시)", `${e.action_label || "-"}`);
  }
  if (e && e.next_info) add("판별 next_info", `${e.next_info_label} (${e.next_info})`);
  box.append(dl);
}

function renderHandoff(v, s) {
  const btn = $("handoff-btn");
  const note = $("handoff-note");
  const hasCustomer = v.transcript.some((t) => t.speaker === "customer");
  const receipt = v.handoff.receipt;
  if (!s.can_handoff) {
    btn.disabled = true;
    note.textContent = "정책 A(개선형 RAG 기준선)에는 상담사 인계 진입점이 없다. 인계는 모델이 create_handoff를 부를 때만 일어난다.";
  } else if (s.closed) {
    btn.disabled = true;
    note.textContent = "세션이 끝났다.";
  } else if (receipt) {
    btn.disabled = true;
    note.textContent = `인계 접수됨: ${receipt.summary}`;
  } else if (v.handoff.requested) {
    btn.disabled = true;
    note.textContent = "인계를 요청했다. 엔진 정책(규칙 1)이 create_handoff를 부른다. 통화자가 제3자면 거절 규칙이 앞선다.";
  } else if (!hasCustomer) {
    btn.disabled = true;
    note.textContent = "고객 발화가 있어야 인계할 수 있다.";
  } else {
    btn.disabled = false;
    note.textContent = "누르면 엔진이 인계 사유(agent_request)를 두고 정책 경로로 create_handoff와 인계 안내를 만든다." +
      (v.evidence && v.evidence.caller === "third_party" ? " 지금 통화자가 제3자로 판단돼 있어 정책 규칙 0(거절 안내)이 앞선다." : "");
  }
}

function renderHistory(v) {
  const list = clear($("suggestions"));
  for (const s of [...v.suggestions].reverse()) {
    const li = el("li", { class: s.status },
      el("div", { class: "line" }, el("strong", { text: s.short_id }), statusBadge(s),
        el("span", { class: "muted small", text: `v${s.basis_version} · ${s.action_label} · ${s.rule_label}` })),
      s.status === "invalidated" ? el("del", { class: "small", text: s.text }) : el("span", { class: "small", text: s.text }));
    if (s.status === "invalidated") li.append(el("div", { class: "small reasons" }, `무효화됨: ${s.reasons.map((r) => r.label).join("; ")}`));
    list.append(li);
  }
  if (!v.suggestions.length) list.append(el("li", { class: "muted", text: "없음" }));
}

function renderState(v) {
  const dl = clear($("state-summary"));
  const e = v.evidence;
  const id = v.facts.identity;
  const add = (k, val) => dl.append(el("dt", { text: k }), el("dd", {}, val));
  add("대화 버전", `v${v.version}` + (e ? ` · 근거는 v${e.version} 기준` : ""));
  add("현재 요청", e && e.intent ? `${e.intent_label} (${e.intent})` : v.meta.policy === "A" ? "정책 A는 요청 라벨이 없다" : "-");
  if (e && e.caller) add("통화자", `${e.caller_label} (${e.caller})`);
  add("본인 확인", id.verified ? el("span", { class: "badge ok", text: `확인됨 ${id.customer_id}` })
    : id.failed_attempts ? el("span", { class: "badge danger", text: `실패 ${id.failed_attempts}회` }) : el("span", { class: "badge", text: "미확인" }));
  if (e) add("근거 충분성", e.sufficient === true ? "충분" : e.sufficient === false ? "부족(확인 질문이나 보류)" : "판단 없음");
  if (e && e.decision_error) add("판별 오류", e.decision_error);
  if (v.warmup) add("예열", `${v.warmup.backend} ${fmtMs(v.warmup.latency_ms)} ms${v.warmup.error ? `, 오류 ${v.warmup.error}` : ""}`);
  if (v.errors.length) add("엔진 오류", v.errors.map((x) => x.error).join("; "));
  if (v.faults.length) add("장애 주입", v.faults.map((f) => `${f.before_utt} 전: ${JSON.stringify(f.faults)}`).join("; "));
}

function renderEvidence(v) {
  const tbody = clear($("evidence-table").tBodies[0]);
  const e = v.evidence;
  const th = ui.config ? ui.config.applies_threshold : 0.5;
  $("evidence-note").textContent = !e ? "아직 근거 판단이 없다."
    : e.has_probabilities ? `v${e.version} 판단. 선택 기준: applies 확률 ${th} 이상(조직 설정). 확률은 판별 모델이 낸 값이며 보정 전이다.`
    : `v${e.version} 답. 정책 A(RAG)에는 판별 확률이 없어 답이 인용한 조항을 선택으로 표시한다.`;
  if (!e) return;
  const rows = [...e.candidates].sort((a, b) => (b.p ?? -1) - (a.p ?? -1));
  for (const c of rows) {
    const prob = c.p === null || c.p === undefined ? el("span", { class: "muted", text: "-" })
      : el("div", { class: "prob" },
          el("div", { class: "bar", "aria-hidden": "true" }, el("span", { class: c.p >= th ? "" : "below", style: `width:${Math.round(c.p * 100)}%` })),
          el("span", { class: "num", text: fmtProb(c.p) }));
    tbody.append(el("tr", { class: c.selected ? "selected" : "" },
      el("th", { scope: "row" }, el("code", { text: c.id })),
      el("td", { text: c.title || "-" }),
      el("td", {}, prob),
      el("td", { text: c.selected ? "선택" : "-" }),
      el("td", { class: "muted small", text: c.source || "-" })));
  }
}

function renderFacts(f) {
  const dl = clear($("facts"));
  const add = (k, val) => dl.append(el("dt", { text: k }), el("dd", {}, val));
  const none = (x) => x === null || x === undefined;
  const list = (items, fmt) => items.length ? el("ul", { class: "plain-list" }, items.map((x) => el("li", { text: fmt(x) }))) : "없음";
  add("본인 확인", f.identity.verified ? `확인됨 (${f.identity.customer_id}), 시도 ${f.identity.attempts}회`
    : f.identity.attempts ? `확인 안 됨, 실패 ${f.identity.failed_attempts}회${f.identity.handoff_required ? ", 인계 필요" : ""}` : "아직 없음");
  add("고객", none(f.customer) ? "조회 전" : `${f.customer.name} · ${f.customer.status} · 생년월일 ${f.customer.birth}`);
  add("구독", none(f.subscriptions) ? "조회 전" : list(f.subscriptions, (s) =>
    `${s.subscription_id} ${s.plan} ${fmtWon(s.price)} · ${s.status} · 시작 ${s.start} · 다음 결제 ${s.next_billing || "-"}${s.promo ? " · 프로모션" : ""}${s.trial ? " · 체험" : ""}`));
  add("결제", none(f.payments) ? "조회 전" : list(f.payments, (p) =>
    `${p.payment_id} ${p.date} ${fmtWon(p.amount)} ${p.method}${p.dispute ? " · 이의 제기" : ""}`));
  add("이용 기록", none(f.usage) ? "조회 전" : list(f.usage, (u) => `${u.date} ${u.type}${u.during_trial ? " (체험 기간)" : ""}`));
  add("장애 기록", none(f.incidents) ? "조회 전" : list(f.incidents, (i) => `${i.incident_id} ${i.start}~${i.end} ${i.hours}시간 · 귀책 ${i.cause}`));
  if (f.quotes.length) add("환불 견적", list(f.quotes, (q) =>
    `${q.reason_label}: ${q.status_label}${isNum(q.refund_amount) && q.status !== "failed" ? ` ${fmtWon(q.refund_amount)}` : ""}` +
    `${q.approval_required ? " · 상담사 승인 필요" : ""}${q.chosen ? " · 선택됨" : ""}${q.message ? ` · ${q.message}` : ""}`));
  if (f.receipts.length) add("접수", list(f.receipts, (r) => `${r.summary}${r.existing ? " (기존 접수)" : ""}`));
  if (f.account_rejections.length) add("계좌 등록 거절", list(f.account_rejections, (x) =>
    `${x.reason || "거절"}${x.holder ? ` · 예금주 ${x.holder}` : ""}${x.message ? ` — ${x.message}` : ""}`));
  if (f.api_errors.length) add("업무 API 오류", list(f.api_errors, (x) => `${x.tool}: ${x.code}${x.message ? ` — ${x.message}` : ""}`));
}

function renderClaims(claims) {
  const ul = clear($("claims"));
  if (!claims.length) { ul.append(el("li", { class: "muted", text: "없음" })); return; }
  for (const c of claims) {
    ul.append(el("li", { class: c.superseded ? "superseded" : "" },
      `${c.label}: `, el("strong", { text: c.value }), ` (고객 진술, ${c.utt_id})`, c.superseded ? " · 이후 다시 말함" : ""));
  }
}

function renderTools(tools) {
  const list = clear($("tools"));
  if (!tools.length) { list.append(el("li", { class: "muted", text: "없음" })); return; }
  for (const t of [...tools].reverse()) {
    const args = Object.entries(t.args).map(([k, val]) => `${k}=${val}`).join(", ");
    const cls = [t.ok === false ? "error" : "", t.discarded ? "discarded" : ""].join(" ");
    const li = el("li", { class: cls },
      el("div", { class: "line" }, el("code", { text: t.call_id }), el("strong", { text: t.label }),
        el("code", { class: "small", text: `${t.tool}(${args})` }),
        el("span", { class: `badge ${t.ok === null ? "warn" : t.ok ? "ok" : "danger"}`, text: t.status }),
        t.discarded ? el("span", { class: "badge", text: "폐기됨(지난 버전, 사실에 반영 안 함)" }) : null),
      el("div", { class: "small" }, t.summary || "응답 기다리는 중",
        t.latency_ms !== null ? el("span", { class: "muted num", text: ` · ${fmtMs(t.latency_ms)} ms · 시도 ${t.attempts} · v${t.request_version}` }) : null));
    if (t.data) li.append(el("details", {}, el("summary", { class: "small", text: "응답 본문" }), el("pre", { tabindex: "0", text: JSON.stringify(t.data, null, 1) })));
    list.append(li);
  }
}

function renderDiscards(v) {
  const ul = clear($("discards"));
  if (!v.discards.length) { ul.append(el("li", { class: "muted", text: "없음" })); return; }
  for (const d of v.discards) {
    const what = d.kind === "cancelled" ? `${d.stage_label} 단계에서 작업 취소` : `${d.stage_label} 결과 폐기`;
    ul.append(el("li", {}, el("span", { class: `badge ${d.kind === "cancelled" ? "warn" : ""}`, text: d.kind === "cancelled" ? "취소" : "폐기" }),
      ` ${what}: 시작 v${d.started_version}, 그때 v${d.version}${d.detail ? ` (${d.detail})` : ""}`));
  }
}

function appendActivity(messages) {
  const list = $("activity");
  for (const m of messages) {
    list.append(el("li", { class: `k-${m.kind}` },
      el("span", { class: "t num", text: isNum(m.t_ms) ? `${fmtMs(m.t_ms)} ms` : "" }),
      el("code", { class: "small", text: m.type }),
      el("span", { text: m.text })));
  }
  $("activity-count").textContent = `(${list.children.length})`;
}

function announceChanges(v) {
  for (const s of v.suggestions) {
    const key = `${s.id}:${s.status}`;
    if (s.status === "invalidated" && !ui.announced.has(key)) {
      ui.announced.add(key);
      announce(`제안 ${s.short_id} 무효화됨: ${s.reasons.map((r) => r.label).join(", ")}`);
    }
  }
  if (v.handoff.receipt && !ui.announced.has("handoff")) {
    ui.announced.add("handoff");
    announce(`인계 접수됨: ${v.handoff.receipt.summary}`);
  }
}

init();
