// 세 화면이 함께 쓰는 도우미. 화면에 넣는 글은 모두 textContent로 넣는다(로그의 글을 HTML로 해석하지 않는다).
"use strict";

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "style") node.setAttribute("style", v);
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }

const nf0 = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat("ko-KR", { minimumFractionDigits: 1, maximumFractionDigits: 1 });

function isNum(x) { return typeof x === "number" && Number.isFinite(x); }

// ms: 100 이상은 정수, 그 아래는 소수 한 자리. "inf"는 미갱신을 포함한 무한대
function fmtMs(x) {
  if (x === "inf") return "∞(미갱신 포함)";
  if (!isNum(x)) return "-";
  return Math.abs(x) >= 100 ? nf0.format(x) : nf1.format(x);
}
function fmtPct(x) { return isNum(x) ? `${nf1.format(x * 100)}%` : "-"; }
function fmtUsd(x, digits = 6) { return isNum(x) ? `$${x.toFixed(digits)}` : "-"; }
function fmtWon(x) { return isNum(x) ? `${nf0.format(x)}원` : "-"; }
function fmtProb(x) { return isNum(x) ? x.toFixed(2) : "-"; }
function fmtRatio(n, d) { return d ? `${n}/${d} (${fmtPct(n / d)})` : `${n}/${d}`; }

async function getJSON(url, options) {
  const res = await fetch(url, options);
  let body = null;
  try { body = await res.json(); } catch (_) { /* 본문 없음 */ }
  if (!res.ok) throw new Error((body && body.error) || `${res.status} ${res.statusText}`);
  return body;
}

function postJSON(url, body) {
  return getJSON(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
}

function announce(text) {
  const region = document.getElementById("announce");
  if (!region) return;
  region.textContent = "";
  window.setTimeout(() => { region.textContent = text; }, 50);
}
