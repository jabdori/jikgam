"""질문·정답 사람 검토 화면을 만든다(docs/evaluation-v2.md §2.3 "사람 검토", §2.4).

검토 대상은 100개다. 검색 평가 세트에서 답이 있는 질문 무작위 40개와 근거 없음 질문 40개 전부,
점검용 질문 무작위 20개다(고정한 seed). 화면은 한 HTML 파일이다. 항목마다 질문, 정답, 정답 근거(원문과 앞뒤 줄)를
보여 주고, 기준별로 표시하고 메모를 남긴다. 표시는 브라우저에 저장되고, "결과 내려받기"로 JSON을 받는다.
대체 근거나 근거 없음을 확인할 수 있게 문서 전체 찾기를 함께 넣는다.

화면에는 test가 될 수 있는 질문과 표준약관 원문이 들어간다. 그래서 저장소 밖(data/v2/local/review/)에 쓰고,
이 스크립트는 내용을 출력하지 않는다. 검토 결과를 읽는 apply는 수만 출력한다.

    uv run python -m jikgam.v2.review build
    uv run python -m jikgam.v2.review apply <결과 JSON>
"""

import argparse
import html
import json
import random
import sys
from pathlib import Path

from jikgam.ingest.corpus import ROOT, Doc, load_docs

SEED = 20261016
ROUND = 2  # 2차 세트(2026-10-16). 1차·2차 화면과 결과는 data/v2/local/review/round1/, round2/에 있다
# 지금 검토: 2차 세트를 고친 뒤(jikgam/v2/repair.py) 다시 재는 확인 검토. 답이 있는 질문 40개만, 2차 표본과 겹치지 않게 뽑는다
STAGE = "2b"
TITLE = "2차 고친 뒤 확인"
OUT = ROOT / "data" / "v2" / "local" / "review"
PAGE = OUT / "qa_review.html"
SAMPLE = OUT / "sample.json"  # 검토 대상 id(내용 없음)
EXCLUDE = OUT / "round2" / "sample.json"  # 2차 검토 표본
QA_FILES = [ROOT / "data" / "v2" / "qa" / "items.jsonl", ROOT / "data" / "v2" / "local" / "qa" / "items.jsonl"]
CHECK_FILES = [ROOT / "data" / "v2" / "check" / "items.jsonl", ROOT / "data" / "v2" / "local" / "check" / "items.jsonl"]
N_ANSWERABLE = 40
N_CHECK = 0  # 점검용은 2차 검토에서 기준을 넘지 않아(10.0%) 다시 재지 않는다
INCLUDE_NONE = False  # 근거 없음은 2차 검토에서 35개 모두 문제없음
CONTEXT = 2  # 근거 앞뒤로 보여 주는 줄 수
LIMIT = 0.10  # 문제 비율이 이보다 크면 다시 만든다(§2.3)

CRITERIA = {
    "answerable": [("natural", "질문이 자연스럽다(고객이 실제로 쓸 말투)"),
                   ("correct", "정답 근거가 맞고, 답하기에 충분하다"),
                   ("complete", "더 나은 근거나 빠진 대체 근거가 없다")],
    "none": [("natural", "질문이 자연스럽다(고객이 실제로 쓸 말투)"),
             ("unanswerable", "정말 이 문서들로 답할 수 없다")],
}


def read(paths: list[Path]) -> list[dict]:
    rows = []
    for p in paths:
        if p.exists():
            rows += [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows


def pick() -> dict[str, list[str]]:
    qa = sorted(read(QA_FILES), key=lambda i: i["id"])
    check = sorted(read(CHECK_FILES), key=lambda i: i["id"])
    seen: set[str] = set()
    if EXCLUDE is not None and EXCLUDE.exists():  # 앞 검토에서 본 질문은 뽑지 않는다
        prev = json.loads(EXCLUDE.read_text(encoding="utf-8"))
        seen = set(prev["answerable"] + prev["none"] + prev["check"])
    answerable = [i["id"] for i in qa if i["type"] != "none" and i["id"] not in seen]
    none = [i["id"] for i in qa if i["type"] == "none"] if INCLUDE_NONE else []
    checks = [i["id"] for i in check if i["id"] not in seen]
    return {"answerable": random.Random(f"{SEED}:{STAGE}:review:answerable").sample(answerable, min(N_ANSWERABLE, len(answerable))),
            "none": none,
            "check": random.Random(f"{SEED}:{STAGE}:review:check").sample(checks, min(N_CHECK, len(checks)))}


def evidence_html(docs: dict[str, Doc], alt: dict) -> str:
    d = docs[alt["doc"]]
    first, last = d.line_at(alt["start"]), d.line_at(alt["end"] - 1)
    rows = []
    for line in d.lines[max(0, first.no - 1 - CONTEXT):min(len(d.lines), last.no + CONTEXT)]:
        inside = first.no <= line.no <= last.no
        if inside:
            a = max(alt["start"], line.start) - line.start
            b = min(alt["end"], line.start + len(line.text)) - line.start
            text = (html.escape(line.text[:a]) + "<mark>" + html.escape(line.text[a:b]) + "</mark>"
                    + html.escape(line.text[b:]))
        else:
            text = html.escape(line.text)
        rows.append(f'<div class="ln{" in" if inside else ""}"><span class="no">{line.no}</span>{text or "&nbsp;"}</div>')
    where = f"{html.escape(d.title)} · {html.escape(first.file)} · {first.no}~{last.no}줄"
    return f'<div class="ev"><div class="where">{where}</div>{"".join(rows)}</div>'


def item_html(docs: dict[str, Doc], item: dict, section: str) -> str:
    kind = "none" if item["type"] == "none" else "answerable"
    type_name = {"single": "단일 근거", "multi": "여러 근거", "none": "근거 없음"}[item["type"]]
    parts = [f'<section class="item" data-id="{item["id"]}" data-kind="{kind}">',
             f'<div class="tags"><span>{html.escape(section)}</span><span>{html.escape(item["group"])}</span>'
             f'<span>{type_name}</span><span class="mono">{item["id"]}</span></div>',
             f'<p class="question">{html.escape(item["question"])}</p>']
    if kind == "answerable":
        parts.append(f'<p class="answer"><b>정답</b> {html.escape(item.get("answer") or "")}</p>')
        for k, ev in enumerate(item["evidence"], 1):
            alts = ev["alternatives"]
            head = f"근거 {k}" + (f" (대체 근거 {len(alts) - 1}곳 포함)" if len(alts) > 1 else "")
            parts.append(f'<div class="evhead">{head}</div>' + "".join(
                (f'<div class="alt">대체 {j}</div>' if j else "") + evidence_html(docs, a) for j, a in enumerate(alts)))
    elif item.get("note"):
        parts.append(f'<p class="answer"><b>만든 모델이 적은 이유</b> {html.escape(item["note"])}</p>')
    checks = "".join(f'<label><input type="checkbox" name="{key}" checked> {html.escape(text)}</label>'
                     for key, text in CRITERIA[kind])
    parts.append(f'<div class="review"><div class="checks">{checks}</div>'
                 '<textarea name="note" rows="2" placeholder="문제가 있으면 무엇이 문제인지(선택)"></textarea>'
                 '<label class="done"><input type="checkbox" name="done"> 검토 완료</label></div></section>')
    return "".join(parts)


def corpus_json(docs: dict[str, Doc]) -> str:
    data = [{"id": d.id, "title": d.title, "lines": [[ln.no, ln.file, ln.text] for ln in d.lines]} for d in docs.values()]
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


STYLE = """
:root { --bg:#ffffff; --fg:#1d1d1f; --muted:#6e6e73; --line:#d2d2d7; --card:#f5f5f7; --accent:#0a66c2; --mark:#fff3b0; --warn:#b3261e; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#161617; --fg:#f5f5f7; --muted:#a1a1a6; --line:#3a3a3c; --card:#1f1f21; --accent:#5ea8ff; --mark:#5c4b00; --warn:#ff8a80; } }
:root[data-theme="dark"] { --bg:#161617; --fg:#f5f5f7; --muted:#a1a1a6; --line:#3a3a3c; --card:#1f1f21; --accent:#5ea8ff; --mark:#5c4b00; --warn:#ff8a80; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.6 system-ui, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif; }
main { max-width: 980px; margin: 0 auto; padding: 16px 16px 160px; }
h1 { font-size: 22px; margin: 8px 0; } h2 { font-size: 18px; margin: 32px 0 8px; }
.note { color: var(--muted); }
.item { border:1px solid var(--line); border-radius:10px; padding:16px; margin:16px 0; background:var(--card); }
.tags span { display:inline-block; border:1px solid var(--line); border-radius:999px; padding:0 8px; margin:0 4px 4px 0; font-size:12px; color:var(--muted); }
.mono, .no { font-family: ui-monospace, Consolas, monospace; }
.question { font-size: 16px; font-weight: 600; margin: 8px 0; }
.answer { margin: 4px 0 8px; }
.evhead { margin-top: 10px; font-weight: 600; } .alt { margin-top: 6px; font-size: 12px; color: var(--muted); }
.ev { border:1px solid var(--line); border-radius:6px; background:var(--bg); padding:6px 8px; margin:4px 0; overflow-x:auto; }
.where { font-size: 12px; color: var(--muted); margin-bottom: 4px; }
.ln { white-space: pre-wrap; word-break: break-word; font-size: 13px; color: var(--muted); }
.ln.in { color: var(--fg); }
.no { display:inline-block; min-width: 44px; color: var(--muted); font-size: 12px; }
mark { background: var(--mark); color: inherit; }
.review { margin-top: 12px; padding-top: 12px; border-top: 1px dashed var(--line); }
.checks label, .done { display:block; margin: 2px 0; }
textarea { width: 100%; margin: 6px 0; padding: 6px; border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--fg); font: inherit; }
.problem { border-color: var(--warn); }
.bar { position: sticky; top: 0; z-index: 2; background: var(--bg); border-bottom: 1px solid var(--line); padding: 8px 0; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
button { font: inherit; padding: 6px 12px; border:1px solid var(--line); border-radius:6px; background:var(--card); color:var(--fg); cursor:pointer; }
button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
#search { position: fixed; right: 16px; bottom: 16px; width: min(520px, calc(100vw - 32px)); max-height: 60vh; display:flex; flex-direction:column; background: var(--card); border:1px solid var(--line); border-radius:10px; padding:8px; z-index:3; }
#search.closed #results, #search.closed #q { display:none; }
#q { width:100%; padding:6px; border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--fg); font:inherit; margin: 6px 0; }
#results { overflow:auto; font-size: 13px; }
#results .hit { border-bottom: 1px solid var(--line); padding: 4px 0; white-space: pre-wrap; word-break: break-word; }
"""

SCRIPT = """
const KEY = 'jikgam-v2-qa-review-r__STAGE__';
const CORPUS = JSON.parse(document.getElementById('corpus').textContent);
function load() { try { return JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) { return {}; } }
function save(state) { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {} }
function collect() {
  const out = {};
  document.querySelectorAll('.item').forEach(el => {
    const r = {kind: el.dataset.kind, failed: [], note: el.querySelector('textarea').value, done: el.querySelector('[name=done]').checked};
    el.querySelectorAll('.checks input').forEach(c => { if (!c.checked) r.failed.push(c.name); });
    out[el.dataset.id] = r;
  });
  return out;
}
function refresh() {
  const state = collect();
  let done = 0, problem = 0;
  document.querySelectorAll('.item').forEach(el => {
    const r = state[el.dataset.id];
    el.classList.toggle('problem', r.failed.length > 0);
    if (r.done) done++; if (r.failed.length) problem++;
  });
  document.getElementById('progress').textContent = `검토 완료 ${done} / ${Object.keys(state).length} · 문제 표시 ${problem}`;
  save(state);
}
function restore() {
  const state = load();
  document.querySelectorAll('.item').forEach(el => {
    const r = state[el.dataset.id]; if (!r) return;
    el.querySelectorAll('.checks input').forEach(c => { c.checked = !r.failed.includes(c.name); });
    el.querySelector('textarea').value = r.note || '';
    el.querySelector('[name=done]').checked = !!r.done;
  });
}
function download() {
  const blob = new Blob([JSON.stringify({page: 'qa_review', round: __ROUND__, stage: '__STAGE__', saved: new Date().toISOString(), items: collect()}, null, 1)], {type: 'application/json'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'qa_review_result_r__STAGE__.json'; a.click();
}
function squash(s) { return s.replace(/[\\s|\\u00a0\\u3000]/g, ''); }
function search(term) {
  const box = document.getElementById('results'); box.innerHTML = '';
  const t = squash(term); if (t.length < 2) return;
  let n = 0;
  for (const d of CORPUS) {
    for (let i = 0; i < d.lines.length && n < 200; i++) {
      // 줄을 넘는 말도 찾도록 이어진 세 줄을 함께 본다
      const joined = squash(d.lines.slice(i, i + 3).map(l => l[2]).join(''));
      if (joined.includes(t) && !squash(d.lines.slice(i + 1, i + 3).map(l => l[2]).join('')).includes(t)) {
        const div = document.createElement('div'); div.className = 'hit';
        div.textContent = `${d.title} · ${d.lines[i][1]} · ${d.lines[i][0]}줄\\n` + d.lines.slice(i, i + 3).map(l => l[2]).join('\\n');
        box.appendChild(div); n++;
      }
    }
  }
  if (!n) box.textContent = '찾은 곳이 없습니다.';
}
document.addEventListener('input', refresh);
document.addEventListener('change', refresh);
document.getElementById('dl').addEventListener('click', download);
document.getElementById('toggle').addEventListener('click', () => document.getElementById('search').classList.toggle('closed'));
document.getElementById('q').addEventListener('keydown', e => { if (e.key === 'Enter') search(e.target.value); });
restore(); refresh();
"""


def build() -> None:
    docs = load_docs()
    items = {i["id"]: i for i in read(QA_FILES) + read(CHECK_FILES)}
    chosen = pick()
    sections = [("answerable", "A. 답이 있는 질문(무작위 40개)", "검색 평가 세트"),
                ("none", "B. 근거 없음 질문(전부)", "검색 평가 세트"),
                ("check", "C. 점검용 질문(무작위 20개)", "점검용")]
    body = []
    for key, title, label in sections:
        if not chosen[key]:
            continue
        body.append(f"<h2>{html.escape(title)} · {len(chosen[key])}개</h2>")
        body += [item_html(docs, items[i], label) for i in chosen[key]]
    intro = """<h1>v2 질문·정답 검토(__TITLE__)</h1>
<p class="note">기준은 docs/evaluation-v2.md §2.3입니다. 항목마다 맞지 않는 기준의 표시를 끄고, 필요하면 메모를 남긴 뒤 "검토 완료"를 켜 주세요.
기준 표시가 하나라도 꺼진 항목을 "문제"로 셉니다. 노란 부분이 정답 근거이고, 앞뒤 줄은 맥락입니다.
표시는 이 브라우저에 저장됩니다. 끝나면 "결과 내려받기"로 받은 <b>qa_review_result_r__STAGE__.json</b>의 위치만 알려 주세요(메모에 질문 내용이 들어가므로 채팅에 붙여 넣지 말아 주세요).
2차 검토에서 본 질문과 겹치지 않는 새 표본(답이 있는 질문 40개)입니다. 2차 검토 뒤 대체 근거 찾기로 붙였던 근거는 뺐습니다. 근거 범위에 표의 상위 줄이 들어가 노란 부분이 여러 줄일 수 있습니다.
오른쪽 아래 "문서 찾기"로 대체 근거나 답이 있는지 찾아볼 수 있습니다(공백과 표 구분자는 무시합니다).</p>"""
    page = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>v2 질문 검토 __STAGE__</title><style>{STYLE}</style></head><body><main>{intro}
<div class="bar"><button class="primary" id="dl">결과 내려받기</button><span id="progress" class="note"></span></div>
{"".join(body)}</main>
<div id="search" class="closed"><button id="toggle">문서 찾기</button><input id="q" placeholder="찾을 말(Enter)"><div id="results"></div></div>
<script type="application/json" id="corpus">{corpus_json(docs)}</script>
<script>{SCRIPT}</script></body></html>"""
    page = page.replace("__ROUND__", str(ROUND)).replace("__STAGE__", STAGE).replace("__TITLE__", TITLE)
    OUT.mkdir(parents=True, exist_ok=True)
    PAGE.write_text(page, encoding="utf-8", newline="\n")
    SAMPLE.write_text(json.dumps({"seed": SEED, "round": ROUND, "stage": STAGE, **chosen}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"검토 화면: {PAGE} ({', '.join(f'{k} {len(v)}개' for k, v in chosen.items())}, {PAGE.stat().st_size:,}바이트)")


def apply(result_path: Path) -> None:
    """검토 결과의 수만 출력한다(메모와 질문은 출력하지 않는다)."""
    result = json.loads(result_path.read_text(encoding="utf-8"))["items"]
    chosen = json.loads(SAMPLE.read_text(encoding="utf-8"))
    for key in ("answerable", "none", "check"):
        ids = chosen[key]
        done = sum(result.get(i, {}).get("done", False) for i in ids)
        problems = [i for i in ids if result.get(i, {}).get("failed")]
        by = {}
        for i in problems:
            for f in result[i]["failed"]:
                by[f] = by.get(f, 0) + 1
        print(f"{key}: 검토 {done}/{len(ids)}, 문제 {len(problems)}개 {by}")
    qa_ids = chosen["answerable"] + chosen["none"]
    qa_rate = sum(bool(result.get(i, {}).get("failed")) for i in qa_ids) / max(1, len(qa_ids))
    ck_rate = sum(bool(result.get(i, {}).get("failed")) for i in chosen["check"]) / max(1, len(chosen["check"]))
    line = f"검색 평가 세트 문제 비율 {qa_rate:.1%}"
    if chosen["check"]:
        line += f", 점검용 문제 비율 {ck_rate:.1%}"
    print(line + f" (기준 {LIMIT:.0%})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["build", "apply"])
    ap.add_argument("result", nargs="?")
    args = ap.parse_args()
    if args.step == "build":
        build()
    else:
        if not args.result:
            sys.exit("결과 JSON 경로가 필요하다")
        apply(Path(args.result))


if __name__ == "__main__":
    main()
