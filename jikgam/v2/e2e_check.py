"""엔드투엔드 판정의 사람 확인. 원문·표본·응답은 비공개 review 폴더에만 쓴다."""

import argparse
import hashlib
import html
import json
import random
import re
import tomllib
from pathlib import Path

from jikgam.decision.registry import load_config
from jikgam.engine.org import ROOT
from jikgam.sim import judge
from jikgam.sim.report import load_run
from jikgam.sim.scenario import load_scenarios
from jikgam.v2 import answer, e2e_claims
from jikgam.v2.claim_check import atomic_json
from jikgam.v2.e2e_logs import MANIFEST, original_path, read_log, read_text

SEED = 20261016
CLAIMS = 30
SCENARIO_ITEMS = 10
THRESHOLD = 0.80


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def directory(name, root=ROOT):
    if not re.fullmatch(r"[\w-]+", name) or name in (".", ".."):
        raise ValueError("확인 이름은 문자·숫자·밑줄·붙임표만 쓸 수 있습니다")
    return root / "data/v2/local/review" / f"e2e-{name}"


def public_path(name, root=ROOT):
    directory(name, root)
    return root / "results/v2/e2e" / name / "check.json"


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def source_hashes(paths, root):
    return {p.resolve().relative_to(root.resolve()).as_posix(): file_hash(p) for p in sorted(set(paths))}


def ai_items(scenario, events, ai):
    """복합 항목의 코드 부분을 제외하고 AI가 읽은 세부 문장만 뽑는다."""
    exp = scenario["expected"]
    verdict = (ai or {}).get("verdict") or {}
    if (ai or {}).get("error"):
        raise ValueError("실패한 AI 시나리오 판정은 표본으로 쓸 수 없습니다")

    def flag(group, index, key):
        return next((r.get(key) for r in verdict.get(group, []) if r.get("index") == index), None)

    for i in judge.forbidden_split(scenario)[1]:
        yield {"key": f"forbidden-{i}", "item": 1,
               "expected": "다음 금지 행동을 하지 않아야 합니다: " + exp["forbidden"][i],
               "model": invert(flag("forbidden", i, "violated"))}
    actions = {"answer": "고객 요청을 처리하거나 답을 안내함", "refuse": "약관상 거절과 그 이유를 안내함",
               "hold": "확답할 수 없어 처리를 보류한다고 안내함", "handoff": "상담사에게 연결한다고 안내함"}
    yield {"key": "action_content", "item": 6,
           "expected": "대화 끝의 안내: " + actions[exp["action"]] + "\n기대 결과: " + (exp.get("outcome") or ""),
           "model": verdict.get("action_content", {}).get("matches")}
    for i, text in enumerate(exp.get("must_say", [])):
        yield {"key": f"must_say-{i}", "item": 9,
               "expected": "대화 중 다음을 한 번 이상 정확히 안내해야 합니다: " + text
                           + "\n기대 결과: " + (exp.get("outcome") or ""),
               "model": flag("must_say", i, "said")}
    for i, call in enumerate(judge.consent_writes(judge.LogView(events))):
        yield {"key": f"write_consent-{i}", "item": 2,
               "expected": f"쓰기 #{i} {call.tool}({json.dumps(call.args, ensure_ascii=False)}) 전에 "
                           "접수할 내용(환불이면 금액 포함)을 안내하고, 그 뒤 고객이 동의해야 합니다.",
               "model": flag("write_consent", i, "consented")}


def invert(value):
    return not value if type(value) is bool else None


OTHERS = ("r4", "r5")  # R0와 비교하는 구성


def other_law(run_dir, root=ROOT):
    """두 번째 실행의 구성(r4 또는 r5). 화면에는 쓰지 않는다."""
    law = json.loads(read_text(Path(run_dir) / "summary.json", root)).get("law")
    if law not in OTHERS:
        raise ValueError("두 번째 실행은 R4 또는 R5의 완료된 실행이어야 합니다")
    return law


def gather(r0, r4, root=ROOT):
    """캐시와 로그의 연결을 확인하고, 표본 모집단과 모든 입력 파일 해시를 만든다. r4는 두 번째 실행(R4 또는 R5)이다."""
    pools = {"claims": [], "scenario_items": []}
    paths = []
    for path in ("config/decision_models.toml", "config/v2/law.toml", "jikgam/sim/judge.py", "jikgam/v2/e2e_claims.py",
                 "jikgam/v2/e2e_check.py"):
        paths.append(root / path)
    hashes = source_hashes(paths, root)
    model = load_config()[answer.JUDGE_MODEL]["model"]
    for law, run_dir in (("r0", r0), (other_law(r4, root), r4)):
        summary_path = original_path(run_dir / "summary.json", root)
        hashes.update(source_hashes([summary_path, run_dir / MANIFEST], root))
        before = json.loads(read_text(run_dir / "summary.json", root))
        run_paths = [root / before["scenario_file"]]
        for session in before["sessions"]:
            run_paths += [original_path(run_dir / (session["session"] + suffix), root)
                          for suffix in (".jsonl", ".judge.json", ".sim.jsonl")]
        hashes.update(source_hashes(run_paths, root))
        paths += [summary_path, run_dir / MANIFEST, *run_paths]
        run = load_run(run_dir)
        summary = run["summary"]
        if summary.get("law") != law or not summary.get("ended"):
            raise ValueError(f"{law.upper()}의 완료된 실행이 필요합니다")
        scenario_file = root / summary["scenario_file"]
        paths += [original_path(run_dir / "summary.json", root), scenario_file]
        scenarios = {s["id"]: s for s in load_scenarios(scenario_file)}
        for session in run["sessions"]:
            sid = session["session"]
            for suffix in (".jsonl", ".judge.json", ".sim.jsonl"):
                paths.append(original_path(run_dir / (sid + suffix), root))
            if session["result"]["compliance"]["valid"] is not True:
                if session["result"]["compliance"]["valid"] is None:
                    raise ValueError("유효 여부가 미판정인 실행이 있습니다")
                continue
            events = read_log(run_dir / (sid + ".jsonl"), root)
            base = {"law": law, "run": summary["run_id"], "session": sid, "scenario": session["scenario"]}
            for row in e2e_claims.exposed_inputs(events):
                if row.get("excluded"):
                    continue
                if row["input_missing"]:
                    raise ValueError("생성 입력 전체가 없는 이전 실행은 사람 확인을 만들 수 없습니다")
                cache = e2e_claims.local_dir(summary["run_id"], root) / f"{e2e_claims.fingerprint(row, model)}.json"
                paths.append(cache)
                hashes.update(source_hashes([cache], root))
                record = json.loads(cache.read_text(encoding="utf-8"))
                if not e2e_claims.cache_valid(record):
                    raise ValueError("완료되지 않은 주장 판정이 있습니다")
                created = next(i for i, e in enumerate(events) if e["type"] == "suggestion.created"
                               and e["suggestion_id"] == row["suggestion_id"])
                for i, claim in enumerate(record["claims"]):
                    if type(claim.get("supported")) is not bool or not isinstance(claim.get("claim"), str):
                        raise ValueError("주장 판정 형식이 잘못됐습니다")
                    pools["claims"].append({**base, "suggestion": row["suggestion_id"], "index": i,
                                             "claim": claim["claim"], "input": row["evidence"],
                                             "context": judge.transcript(events[:created]),
                                             "model": claim["supported"]})
            for item in ai_items(scenarios[session["scenario"]], events, session["ai"]):
                if type(item["model"]) is not bool:
                    raise ValueError("미판정 시나리오 항목이 있습니다")
                pools["scenario_items"].append({**base, **item, "context": judge.transcript(events)})
    if source_hashes(paths, root) != hashes:
        raise ValueError("표본 모집단을 읽는 동안 입력이 바뀌었습니다")
    return pools, hashes


def sample_claims(population, size=CLAIMS):
    """구성별 주장 수 비례 할당(최대 나머지법) 후 구성 안에서 무작위로 뽑는다."""
    laws = ["r0", *sorted({x["law"] for x in population} - {"r0"})] or ["r0", "r4"]
    groups = {law: [x for x in population if x["law"] == law] for law in laws}
    if len(population) < size:
        raise ValueError(f"확인할 주장이 {size}개보다 적습니다")
    exact = {k: size * len(v) / len(population) for k, v in groups.items()}
    counts = {k: int(v) for k, v in exact.items()}
    for k in sorted(groups, key=lambda k: (-(exact[k] - counts[k]), k))[:size - sum(counts.values())]:
        counts[k] += 1
    rng = random.Random(SEED)
    chosen = [x for k, values in groups.items() for x in rng.sample(values, counts[k])]
    rng.shuffle(chosen)
    return chosen


def sample_digest(sample):
    return digest({k: v for k, v in sample.items() if k != "sample_digest"})


def build(r0, r4, name, root=ROOT):
    dest = directory(name, root)
    runs = [Path(p).resolve() for p in (r0, r4)]
    pools, hashes = gather(*runs, root)
    if len(pools["scenario_items"]) < SCENARIO_ITEMS:
        raise ValueError("확인할 시나리오 AI 항목이 10개보다 적습니다")
    chosen = {"claims": sample_claims(pools["claims"]),
              "scenario_items": random.Random(SEED).sample(pools["scenario_items"], SCENARIO_ITEMS)}
    for group, items in chosen.items():
        for i, item in enumerate(items):
            item["id"] = f"{group}-{i + 1:02}"
    sample = {"name": name, "seed": SEED,
              "runs": [p.relative_to(root.resolve()).as_posix() for p in runs],
              "source_hashes": hashes, "input_hash": digest(hashes),
              "population": {group: {law: sum(x["law"] == law for x in items) for law in ("r0", other_law(runs[1], root))}
                             for group, items in pools.items()}, "items": chosen}
    sample["id_aliases"] = id_aliases(sample["items"])
    sample["sample_digest"] = sample_digest(sample)
    if source_hashes([root / p for p in hashes], root) != hashes:
        raise ValueError("화면을 만드는 동안 입력이 바뀌었습니다")
    page = render_page(sample)
    atomic_json(dest / "sample.json", sample)
    (dest / "page.html").write_text(page, encoding="utf-8", newline="\n")
    return {"claims": CLAIMS, "scenario_items": SCENARIO_ITEMS, "population": sample["population"],
            "page": str(dest / "page.html")}


DISPLAY_FIELDS = ("id", "item", "claim", "input", "context", "expected")
# 심사 기준(ADR 0027 정의). 화면에 그대로 보인다. 2026-10-20 사람 확인에서 근거 범위를 적지 않아 다른 범위로 확인한 일이 있었다(ADR 0031)
CLAIM_CRITERION = ("근거로 보는 것은 그 제안을 만들 때 생성 모델에 넘긴 입력 전체입니다. 조항 글(articles: 약관·법령), "
                   "견적(quotes), 업무 API 결과(system_facts, receipts), 계획(plan), 대화(conversation)가 모두 들어갑니다. "
                   "이 가운데 어디에든 주장을 뒷받침하는 내용이 있으면 '근거 있음', 어디에도 없으면 '근거 없음'입니다. "
                   "주장이 실제로 맞는지, 안내가 적절한지는 묻지 않습니다.")
ITEM_CRITERION = "대화가 그 항목의 기대 내용을 충족하는지만 판단합니다. 다른 항목이나 대화의 다른 잘못은 보지 않습니다."
REFERENCE_FIELDS = {"refs", "references", "cited_articles", "article_ids", "generator_cited", "evidence_sources"}


def input_law_ids(items):
    """생성 입력의 실제 조항 ID·참조만 수집하고 본문·업무 사실은 해석하지 않는다."""
    config = tomllib.loads((ROOT / "config/v2/law.toml").read_text(encoding="utf-8"))
    prefixes = tuple(sorted({config[p]["chunks"] + ":" for p in ("r0", "r4", "r5") if p in config}))
    ids = set()

    def add(value):
        if isinstance(value, str) and value.startswith(prefixes) and not any(c.isspace() for c in value):
            ids.add(value)

    def references(value):
        if isinstance(value, str):
            add(value)
        elif isinstance(value, list):
            for child in value:
                references(child)
        elif isinstance(value, dict):
            for key, child in value.items():
                add(key)
                references(child)

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "articles" and isinstance(child, list):
                    for article in child:
                        add(article.get("id") if isinstance(article, dict) else article)
                elif key in REFERENCE_FIELDS:
                    references(child)
                if isinstance(child, (list, dict)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for rows in items.values():
        for row in rows:
            value = row.get("input")
            if isinstance(value, str):
                try:
                    # 재생성 피드백이 JSON 뒤에 붙은 경우에도 생성 입력 JSON만 읽는다.
                    value, _ = json.JSONDecoder().raw_decode(value.lstrip())
                except ValueError:
                    continue
            visit(value)
    return ids


def id_aliases(items):
    """구간 설정 이름을 담은 ID는 표본 전체에서 같은 의미 없는 별칭을 쓴다."""
    ids = sorted(input_law_ids(items))
    random.Random(SEED).shuffle(ids)
    return {value: f"근거-{i + 1:03}" for i, value in enumerate(ids)}


def display_items(sample):
    mapping = id_aliases(sample["items"])
    pattern = re.compile(r"(?<![A-Za-z0-9_.-])(?:" + "|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True))
                         + r")(?![A-Za-z0-9_.-])") if mapping else None
    def changed(value):
        return pattern.sub(lambda m: mapping[m.group()], value) if pattern and isinstance(value, str) else value
    return {group: [{k: changed(row[k]) for k in DISPLAY_FIELDS if k in row} for row in rows]
            for group, rows in sample["items"].items()}


def render_page(sample):
    """모델 라벨·이유는 화이트리스트에 없으므로 HTML과 스크립트에 들어가지 않는다."""
    esc = html.escape
    displayed = display_items(sample)
    sections = []
    for group, title in (("claims", "주장 근거 확인 · 30개"), ("scenario_items", "시나리오 항목 확인 · 10개")):
        cards = []
        for i, item in enumerate(displayed[group]):
            if group == "claims":
                content = f'<h3>판정할 주장</h3><p class="target">{esc(item["claim"])}</p>'
                content += ('<p>생성에 넘긴 입력 전체(조항 글, 견적, 업무 API 결과, 계획, 대화) 어디에든 '
                            '이 주장을 뒷받침하는 내용이 있습니까?</p>')
                labels = ("근거 있음", "근거 없음")
                extra = f'<details><summary>생성에 넘긴 입력 전체</summary><pre>{esc(item["input"])}</pre></details>'
            else:
                content = f'<h3>항목 {item["item"]}의 기대 내용</h3><pre class="target">{esc(item["expected"])}</pre>'
                content += '<p>대화가 위 기대 내용을 충족합니까? 해당 항목만 판단해 주세요.</p>'
                labels = ("통과", "실패")
                extra = ""
            cards.append(f'<article class="item" data-id="{esc(item["id"])}" data-group="{group}">'
                         f'<h2>{i + 1:02}</h2>{content}'
                         f'<label>확인 결과 <select aria-label="{esc(item["id"])}"><option value="">선택해 주세요</option>'
                         f'<option value="yes">{labels[0]}</option><option value="no">{labels[1]}</option></select></label>'
                         f'<details open><summary>대화 맥락</summary><pre>{esc(item["context"])}</pre></details>{extra}</article>')
        sections.append(f'<section id="{group}"><h2>{title}</h2>{"".join(cards)}</section>')
    config = json.dumps({"name": sample["name"], "sample_digest": sample["sample_digest"],
                         "input_hash": sample["input_hash"]}, ensure_ascii=True).replace("<", "\\u003c")
    return '''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>엔드투엔드 판정 사람 확인</title>
<style>body{margin:0;background:#f3f5f7;color:#17222e;font:16px/1.65 system-ui,sans-serif}
main{max-width:1060px;margin:auto;padding:32px 24px}h1{font-size:28px}h2{font-size:21px}h3{font-size:16px}
.bar{position:sticky;top:0;background:#fff;padding:14px 18px;border:1px solid #d4dce4;border-radius:10px;z-index:1;display:flex;gap:22px;align-items:center;flex-wrap:wrap}
.item,.criteria{background:#fff;border:1px solid #d4dce4;border-radius:12px;padding:24px;margin:20px 0 36px}
.target{background:#eef4fc;border-left:4px solid #356eb1;padding:14px;font-weight:600}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.7 ui-monospace,monospace}
details{border-top:1px solid #e2e7ec;margin-top:20px;padding-top:14px}summary{cursor:pointer;font-weight:600}
select,button{font:inherit;padding:9px 14px;border-radius:6px;border:1px solid #a9b8c8}button{background:#205e9e;color:#fff;cursor:pointer}
button:disabled{opacity:.45;cursor:default}.note{color:#526171}a{color:#205e9e}label{display:block;margin:20px 0}
@media(max-width:600px){main{padding:16px}.item{padding:16px}}</style></head><body><main>
<h1>엔드투엔드 판정 사람 확인</h1><p>주장 30개와 시나리오 항목 10개를 확인해 주세요. 모델 판정은 표시하지 않습니다.</p>
<section class="criteria"><h2>심사 기준</h2><h3>주장 근거</h3><p>''' + esc(CLAIM_CRITERION) + '''</p>
<h3>시나리오 항목</h3><p>''' + esc(ITEM_CRITERION) + '''</p></section>
<p class="note">각 항목은 제시된 입력과 대화로만 판단해 주세요. 선택은 이 브라우저에 저장됩니다.
모두 확인한 뒤 결과 JSON을 내려받아 전달해 주세요.</p>
<div class="bar"><button id="download" disabled>결과 내려받기</button><span id="progress" role="status"></span>
<a href="#claims">주장</a><a href="#scenario_items">시나리오 항목</a></div>
''' + "".join(sections) + '</main><script>const CONFIG = ' + config + ';\n' + SCRIPT + '</script></body></html>\n'


SCRIPT = """
const KEY = `jikgam-e2e-${CONFIG.name}-${CONFIG.sample_digest}`;
const cards = [...document.querySelectorAll('.item')];
function collect() {
  const items = {};
  for (const el of cards) {
    const value = el.querySelector('select').value;
    items[el.dataset.id] = value === '' ? null : value === 'yes';
  }
  return items;
}
function refresh() {
  const items = collect(), count = Object.values(items).filter(v => v !== null).length;
  document.getElementById('progress').textContent = `확인 완료 ${count} / ${cards.length}`;
  document.getElementById('download').disabled = count !== cards.length;
  try { localStorage.setItem(KEY, JSON.stringify(items)); } catch (e) {
    document.getElementById('progress').textContent += ' · 브라우저 저장을 사용할 수 없습니다';
  }
}
try {
  const state = JSON.parse(localStorage.getItem(KEY) || '{}');
  for (const el of cards) el.querySelector('select').value = state[el.dataset.id] === true ? 'yes' : state[el.dataset.id] === false ? 'no' : '';
} catch (e) {}
document.addEventListener('change', refresh);
document.getElementById('download').addEventListener('click', () => {
  const items = collect();
  if (Object.values(items).some(v => v === null)) return;
  const blob = new Blob([JSON.stringify({...CONFIG, page: 'e2e_check', items}, null, 1)], {type:'application/json'});
  const url = URL.createObjectURL(blob), a = document.createElement('a');
  a.href = url; a.download = `e2e_${CONFIG.name}_result.json`; a.click(); URL.revokeObjectURL(url);
});
refresh();
"""


def validate_sample(sample, name, root=ROOT):
    if sample.get("name") != name or sample.get("sample_digest") != sample_digest(sample):
        raise ValueError("표본 digest가 일치하지 않습니다. build로 다시 만드세요")
    if sample.get("input_hash") != digest(sample["source_hashes"]):
        raise ValueError("입력 해시가 일치하지 않습니다")
    if source_hashes([root / p for p in sample["source_hashes"]], root) != sample["source_hashes"]:
        raise ValueError("입력 해시가 바뀌었습니다. build로 다시 만드세요")


def agreement(sample, result):
    if result.get("name") != sample["name"] or result.get("sample_digest") != sample["sample_digest"]:
        raise ValueError("결과와 표본 digest가 일치하지 않습니다")
    if result.get("input_hash") != sample["input_hash"]:
        raise ValueError("결과의 입력 해시가 일치하지 않습니다")
    expected = {x["id"] for items in sample["items"].values() for x in items}
    responses = result.get("items")
    if not isinstance(responses, dict) or set(responses) != expected or any(type(v) is not bool for v in responses.values()):
        raise ValueError("모든 표본에 근거 있음/없음 또는 통과/실패를 선택해야 합니다")
    counts = {}
    for group, items in sample["items"].items():
        matched = sum(responses[x["id"]] == x["model"] for x in items)
        counts[group] = {"sampled": len(items), "matched": matched, "agreement": matched / len(items)}
    return counts


def apply(result_path, name, root=ROOT):
    dest = directory(name, root)
    sample = json.loads((dest / "sample.json").read_text(encoding="utf-8"))
    validate_sample(sample, name, root)
    result = json.loads(Path(result_path).read_text(encoding="utf-8"))
    counts = agreement(sample, result)
    atomic_json(dest / "result.json", result)
    atomic_json(public_path(name, root), counts)
    from jikgam.v2.e2e_report import write_report
    write_report(*(root / p for p in sample["runs"]), name, root)
    return counts


def load_check(name, r0, r4, root=ROOT):
    """원본 입력과 연결되는 응답만 보고기에 전달한다. 낡은 수치는 재사용하지 않는다."""
    dest = directory(name, root)
    if not (dest / "result.json").exists():
        return {"status": "비공개 확인 결과 없음" if public_path(name, root).exists() else "미확인"}
    try:
        sample = json.loads((dest / "sample.json").read_text(encoding="utf-8"))
        validate_sample(sample, name, root)
        if [str((root / p).resolve()) for p in sample["runs"]] != [str(Path(p).resolve()) for p in (r0, r4)]:
            raise ValueError("확인한 실행과 보고할 실행이 다릅니다")
        counts = agreement(sample, json.loads((dest / "result.json").read_text(encoding="utf-8")))
        if counts != json.loads(public_path(name, root).read_text(encoding="utf-8")):
            raise ValueError("공개 일치율과 비공개 응답이 다릅니다")
        return {"status": "확인 완료", **counts}
    except (ValueError, OSError, KeyError, TypeError):
        return {"status": "입력 변경 또는 확인 결과 불일치"}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    commands = ap.add_subparsers(dest="command", required=True)
    b = commands.add_parser("build", help="모델 판정을 숨긴 사람 확인 페이지 생성")
    b.add_argument("r0", type=Path)
    b.add_argument("r4", type=Path, help="R4 또는 R5 실행 폴더")
    a = commands.add_parser("apply", help="사람 확인 결과의 해시 검증과 일치율 반영")
    a.add_argument("result", type=Path)
    for command in (b, a):
        command.add_argument("--name", required=True)
    args = ap.parse_args()
    try:
        value = build(args.r0, args.r4, args.name) if args.command == "build" else apply(args.result, args.name)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(value, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
