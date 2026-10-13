"""test 분할 사례를 사람이 검토할 수 있게 한 화면(HTML)으로 만든다(docs/evaluation.md §8).

    uv run python scripts/make_test_review.py            # outputs/test_review.html

사례별로 대화, 기록된 사실, 후보 조항, 질문과 정답, 정답의 이유를 보여 주고, 사례마다 동의/수정 필요와 메모를
남긴 뒤 결과를 JSON으로 복사할 수 있게 한다. 출력은 outputs/(git 무시)에 둔다. 판별 설정을 고르는 쪽이 test 내용을
보지 않도록, 이 스크립트는 화면을 만들기만 하고 내용을 출력하지 않는다.
"""

import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_dataset import CASES_DIR, QUESTIONS, ROOT, SPLITS, TERMS, load_cases, parse_terms  # noqa: E402

OUT = ROOT / "outputs/test_review.html"
QUESTION_NAMES = {
    "intent": "고객이 원하는 처리", "condition_changed": "마지막 발화가 기록을 바꾸는가",
    "applies": "이 조항이 필요한 근거인가", "sufficient": "지금 처리 결과를 확정할 수 있는가",
    "next_info": "다음에 확인할 정보", "suggestion_valid": "이전 제안이 아직 유효한가", "action": "다음 행동",
}

STYLE = """
:root { --bg:#fff; --fg:#1d1d1f; --muted:#6e6e73; --line:#d2d2d7; --card:#f5f5f7; --accent:#0a66c2; --warn:#b3261e; }
@media (prefers-color-scheme: dark) { :root { --bg:#161617; --fg:#f5f5f7; --muted:#a1a1a6; --line:#3a3a3c; --card:#1f1f21; --accent:#5ea8ff; --warn:#ff8a80; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.6 system-ui, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif; }
main { max-width: 960px; margin: 0 auto; padding: 24px 16px 120px; }
h1 { font-size: 22px; } h2 { font-size: 18px; margin: 40px 0 8px; } h3 { font-size: 16px; margin: 0 0 8px; }
.note { color: var(--muted); }
.case { border:1px solid var(--line); border-radius:10px; padding:16px; margin:16px 0; background:var(--card); }
.tags span { display:inline-block; border:1px solid var(--line); border-radius:999px; padding:0 8px; margin:0 4px 4px 0; font-size:12px; color:var(--muted); }
.turn { margin:4px 0; } .turn b { display:inline-block; min-width:56px; }
.partial { color: var(--muted); font-style: italic; }
pre { white-space: pre-wrap; word-break: break-word; background: var(--bg); border:1px solid var(--line); border-radius:6px; padding:8px; font-size:13px; }
table { border-collapse: collapse; width:100%; margin:8px 0; } td, th { border-bottom:1px solid var(--line); padding:6px; text-align:left; vertical-align: top; }
details { margin: 4px 0; } summary { cursor: pointer; }
.review { margin-top:12px; padding-top:12px; border-top:1px dashed var(--line); }
.review label { margin-right: 16px; }
textarea { width:100%; min-height:56px; background:var(--bg); color:var(--fg); border:1px solid var(--line); border-radius:6px; padding:6px; font: inherit; }
.bar { position:fixed; left:0; right:0; bottom:0; background:var(--card); border-top:1px solid var(--line); padding:10px 16px; }
.bar div { max-width:960px; margin:0 auto; display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
button { background:var(--accent); color:#fff; border:0; border-radius:6px; padding:8px 14px; font: inherit; cursor:pointer; }
#out { display:none; margin-top:8px; }
"""

SCRIPT = """
const KEY = 'jikgam-test-review';
function load() { try { return JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) { return {}; } }
function save(state) { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {} }
function collect() {
  const state = {};
  document.querySelectorAll('.case').forEach(c => {
    const id = c.dataset.id;
    const r = c.querySelector('input[type=radio]:checked');
    const note = c.querySelector('textarea').value.trim();
    if (r || note) state[id] = { status: r ? r.value : null, note };
  });
  return state;
}
function refresh() {
  const state = collect(); save(state);
  const done = Object.values(state).filter(v => v.status).length;
  document.getElementById('count').textContent = done + ' / ' + document.querySelectorAll('.case').length + '건 판정';
}
window.addEventListener('DOMContentLoaded', () => {
  const state = load();
  document.querySelectorAll('.case').forEach(c => {
    const s = state[c.dataset.id]; if (!s) return;
    if (s.status) { const r = c.querySelector('input[value="' + s.status + '"]'); if (r) r.checked = true; }
    c.querySelector('textarea').value = s.note || '';
  });
  document.querySelectorAll('input, textarea').forEach(e => e.addEventListener('input', refresh));
  refresh();
  document.getElementById('copy').addEventListener('click', () => {
    const text = JSON.stringify({ reviewed_at: new Date().toISOString(), cases: collect() }, null, 1);
    const out = document.getElementById('out'); out.style.display = 'block'; out.value = text; out.select();
    try { navigator.clipboard.writeText(text); } catch (e) {}
  });
});
"""


def esc(x) -> str:
    return html.escape(str(x))


def render_case(c: dict, articles: dict, bank: dict) -> str:
    s = c["state"]
    turns = "".join(
        f'<div class="turn{"" if u["final"] else " partial"}"><b>{"고객" if u["speaker"] == "customer" else "상담"}</b>'
        f'{esc(u["text"])}{"" if u["final"] else " (미확정 자막)"}</div>' for u in s["conversation"])
    arts = "".join(
        f'<details><summary>{esc(a)} {esc(articles[a]["title"])}</summary><pre>{esc(articles[a]["text"])}</pre></details>'
        for a in s.get("article_ids", []))
    rows = []
    for qid in c["questions"]:
        base, _, arg = qid.partition(":")
        name = QUESTION_NAMES.get(base, base) + (f" ({arg})" if arg else "")
        label = c["gold"][qid]
        meaning = bank[base].get("criteria", {}).get(label, "") if bank[base]["type"] == "choice" else ""
        rows.append(f"<tr><td><code>{esc(qid)}</code><br><span class='note'>{esc(name)}</span></td>"
                    f"<td><b>{esc(label)}</b>{'<br><span class=note>' + esc(meaning) + '</span>' if meaning else ''}</td></tr>")
    prev = s.get("previous_suggestion")
    return f"""
<section class="case" data-id="{esc(c['id'])}">
  <h3>{esc(c['id'])} <span class="note">· 출처 {esc(c.get('source', ''))} · 본인 확인 {'완료' if s['identity_verified'] else '전'} · 기준일 {esc(s['today'])}</span></h3>
  <div class="tags">{''.join(f'<span>{esc(t)}</span>' for t in c['tags'])}</div>
  <h4>대화</h4>{turns}
  <h4>기록된 사실(마지막 발화 이전)</h4><pre>{esc(json.dumps(s['known_facts'], ensure_ascii=False, indent=1))}</pre>
  {f'<h4>이전 응답 제안</h4><pre>{esc(prev)}</pre>' if prev else ''}
  <h4>후보 조항</h4>{arts}
  <h4>질문과 정답</h4><table>{''.join(rows)}</table>
  <h4>정답의 이유</h4><p>{esc(c['rationale'])}</p>
  <div class="review">
    <label><input type="radio" name="r-{esc(c['id'])}" value="agree"> 정답에 동의</label>
    <label><input type="radio" name="r-{esc(c['id'])}" value="disagree"> 수정이 필요함</label>
    <textarea placeholder="수정이 필요한 질문과 이유, 또는 메모"></textarea>
  </div>
</section>"""


def main() -> None:
    splits = json.loads(SPLITS.read_text(encoding="utf-8"))["groups"]
    test_groups = {g for g, sp in splits.items() if sp == "test"}
    cases = [c for c in load_cases() if c.get("group") in test_groups]
    articles = parse_terms(TERMS["org_a"])
    bank = json.loads(QUESTIONS.read_text(encoding="utf-8"))["questions"]
    by_group: dict[str, list] = {}
    for c in cases:
        by_group.setdefault(c["group"], []).append(c)
    body = []
    for g in sorted(by_group):
        body.append(f"<h2>대화 {esc(g)} ({len(by_group[g])}건)</h2>")
        body += [render_case(c, articles, bank) for c in sorted(by_group[g], key=lambda c: c["id"])]
    doc = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>test 사례 검토</title><style>{STYLE}</style><script>{SCRIPT}</script></head><body><main>
<h1>test 사례 검토 ({len(cases)}건, 대화 {len(by_group)}개)</h1>
<p class="note">정답은 AI가 작성하고 AI가 검토한 것이다. 사례마다 정답에 동의하는지 판정하고, 수정이 필요하면 질문 ID와 이유를 적는다.
test는 고정돼 있어 정답을 고치지 않는다. 수정 의견은 진행 기록에 남기고, 동의한 사례는 출처를 reviewed로 바꾼다.
판정은 이 브라우저에 저장되며, 끝나면 아래 버튼으로 결과를 복사해 전달한다. 라벨 기준은 docs/data-format.md에 있다.</p>
{''.join(body)}
</main><div class="bar"><div><button id="copy">검토 결과 복사</button><span id="count" class="note"></span></div>
<div><textarea id="out" readonly></textarea></div></div></body></html>"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(doc, encoding="utf-8", newline="\n")
    print(f"{OUT.relative_to(ROOT)}: 사례 {len(cases)}건, 대화 {len(by_group)}개")


if __name__ == "__main__":
    main()
