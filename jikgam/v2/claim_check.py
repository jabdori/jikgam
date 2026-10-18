"""답변 판정의 무작위 주장 30개를 모델 표시 없이 사람이 확인한다(§4.3)."""

import argparse
import hashlib
import html
import json
import random
import os
import tempfile
from pathlib import Path

from jikgam.ingest.corpus import ROOT
from jikgam.v2.report import CONFIGS

SEED = 20261016
SIZE = 30
RUNS = (CONFIGS["R0"], CONFIGS["R4"])
CORRECTNESS = ("match", "contradict", "unknown")


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def input_hashes(root, split):
    """파일의 유무까지 묶어, 나중에 구성이 추가된 경우도 이전 검토와 구별한다."""
    out = {}
    for name in RUNS:
        for kind in ("answers", "runs"):
            path = root / f"data/v2/local/{kind}" / split / f"{name}.jsonl"
            digest = None
            if path.is_file():
                with path.open("rb") as fh:
                    digest = hashlib.file_digest(fh, "sha256").hexdigest()
            out[path.relative_to(root).as_posix()] = digest
    return out


def review_path(root, split):
    return root / ("results/v2/test/claim-check.json" if split == "test" else "outputs/v2-smoke/claim-check-val.json")


def sample_path(root, split):
    return root / "data/v2/local/review" / f"claims_{split}_sample.json"


def atomic_json(path, value):
    """같은 디렉터리의 임시 파일을 완전히 닫은 뒤 원자적으로 바꾼다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as fh:
            temp = Path(fh.name)
            json.dump(value, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def read_review_json(path):
    """손상된 원본은 그대로 두고, 본문 없는 별도 표식으로 무효 상태를 남긴다."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None, False
    try:
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("객체가 아닌 검토 파일")
    except (ValueError, UnicodeError):
        atomic_json(path.with_suffix(".invalid.json"), {
            "valid": False, "invalidation_reason": "무효(손상)", "sha256": hashlib.sha256(raw).hexdigest()})
        return None, True
    return value, False


def sample_is_current(sample, root, split):
    sources = sample.get("source_hashes")
    if sample.get("split") != split or not isinstance(sources, dict) or not any(v for k, v in sources.items() if "/answers/" in k):
        return False
    if sources != input_hashes(root, split) or not sample.get("items"):
        return False
    if any(not isinstance(c.get("claim_sha256"), str) or len(c["claim_sha256"]) != 64 for c in sample["items"]):
        return False
    return sample.get("sample_digest") == sample_digest(sample["items"], sources)


def load_review(root, split):
    """입력·표본과 연결되지 않는 구형 또는 무효 검토는 현재 판정에 사용하지 않는다."""
    summary, broken_summary = read_review_json(review_path(root, split))
    sample, broken_sample = read_review_json(sample_path(root, split))
    if broken_summary or broken_sample:
        return {"valid": False, "invalidation_reason": "무효(손상)"}
    try:
        if summary.get("valid") is not True or summary.get("sample_digest") != sample.get("sample_digest"):
            return None
        if summary.get("source_hashes") != sample.get("source_hashes") or not sample_is_current(sample, root, split):
            return None
        return summary
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def validated_review(root, split):
    summary = load_review(root, split)
    return summary if summary and summary.get("valid") is True else None


def invalidate_review(root, split):
    """답변 재생성을 시작할 때 이전 수치는 보존하고 무효 표시만 남긴다."""
    path = review_path(root, split)
    summary, broken = read_review_json(path)
    if summary is None or broken:
        return
    summary.update({"valid": False, "invalidation_reason": "답변 단계 재실행"})
    atomic_json(path, summary)


def read_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def pick_claims(sources: dict[str, list[dict]], size: int = SIZE) -> tuple[list[dict], int]:
    """구성·행·주장 순서를 고정한 모집단에서 두 구성을 합쳐 뽑는다. 본문은 반환하지 않는다."""
    pool = []
    for name in sorted(sources):
        for row_no, row in enumerate(sources[name]):
            if row.get("judge_error"):
                continue
            for claim_no, claim in enumerate(row.get("claims") or []):
                if not isinstance(claim.get("supported"), bool) or claim.get("correctness") not in CORRECTNESS:
                    raise ValueError("주장 판정 형식이 맞지 않습니다")
                pool.append({"run": name, "id": row["id"], "rep": row.get("rep", 0), "row": row_no,
                             "claim_sha256": text_hash(claim["claim"]),
                             "claim_index": claim_no, "supported": claim["supported"], "correctness": claim["correctness"]})
    chosen = random.Random(SEED).sample(pool, min(size, len(pool)))
    return [{"sample_id": f"claim-{i:02d}", **c} for i, c in enumerate(chosen, 1)], len(pool)


def sample_digest(sample: list[dict], sources=None) -> str:
    # 브라우저 저장과 결과 적용을 같은 표본에 묶는다. 모델 표시는 화면 식별자에도 넣지 않는다.
    ids = [{k: c.get(k) for k in ("run", "id", "rep", "row", "claim_index", "claim_sha256")} for c in sample]
    return text_hash(json.dumps({"items": ids, "source_hashes": sources or {}}, sort_keys=True))


def render_page(items: list[dict], split: str, digest: str) -> str:
    """화면에는 원문과 빈 선택란만 쓴다. 모델 표시는 HTML·스크립트에 넣지 않는다."""
    from jikgam.v2.review import STYLE

    body = []
    for i, item in enumerate(items, 1):
        parts = [f'<section class="item" data-id="{html.escape(item["sample_id"], quote=True)}"><h2>주장 {i}</h2>']
        for label, key in (("질문", "question"), ("생성에 넘긴 근거 글", "evidence"), ("정답 근거", "gold"), ("판정할 주장", "claim")):
            parts.append(f'<h3>{label}</h3><pre>{html.escape(item[key])}</pre>')
        parts.append('''<div class="review"><label>생성에 넘긴 글에 이 주장을 뒷받침하는 근거가 있나요?
<select name="support"><option value="">선택해 주세요</option><option value="yes">근거 있음</option><option value="no">근거 없음</option></select></label>
<label>이 주장은 정답 근거와 맞나요?
<select name="correctness"><option value="">선택해 주세요</option><option value="match">정답과 맞음</option>
<option value="contradict">정답과 어긋남</option><option value="unknown">판단 불가</option></select></label></div></section>''')
        body.append("".join(parts))
    script = SCRIPT.replace("__SPLIT__", split).replace("__DIGEST__", digest)
    return f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>v2 답변 판정 사람 확인</title><style>{STYLE}
pre {{ white-space:pre-wrap; overflow-wrap:anywhere; font:inherit; }} h3 {{ margin-bottom:4px; font-size:15px; }}
.review label {{ display:block; margin:10px 0; }} select {{ font:inherit; padding:5px; margin-left:8px; }}
button:disabled {{ opacity:0.5; cursor:default; }}</style></head><body><main>
<h1>v2 답변 판정 사람 확인 · {split}</h1><p class="note">각 주장을 읽고 두 항목을 모두 선택해 주세요. 표시는 이 브라우저에 저장됩니다.
전부 확인하면 “결과 내려받기”로 JSON 파일을 받으세요. 받은 파일의 위치만 알려 주시면 됩니다.</p>
<div class="bar"><button class="primary" id="dl" disabled>결과 내려받기</button><span id="progress" class="note"></span></div>
{"".join(body)}</main><script>{script}</script></body></html>'''


SCRIPT = """
const SPLIT = '__SPLIT__';
const SAMPLE = '__DIGEST__';
const KEY = `jikgam-v2-claims-${SPLIT}-${SAMPLE}`;
function load() { try { return JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) { return {}; } }
function collect() {
  const out = {};
  document.querySelectorAll('.item').forEach(el => {
    const support = el.querySelector('[name=support]').value;
    const correctness = el.querySelector('[name=correctness]').value;
    out[el.dataset.id] = {supported: support === '' ? null : support === 'yes', correctness,
                         done: support !== '' && correctness !== ''};
  });
  return out;
}
function refresh() {
  const state = collect();
  const count = Object.values(state).filter(r => r.done).length;
  document.getElementById('progress').textContent = `확인 완료 ${count} / ${Object.keys(state).length}`;
  document.getElementById('dl').disabled = count === 0 || count !== Object.keys(state).length;
  try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {
    document.getElementById('progress').textContent += ' · 브라우저 저장을 사용할 수 없습니다';
  }
}
function restore() {
  const state = load();
  document.querySelectorAll('.item').forEach(el => {
    const r = state[el.dataset.id]; if (!r) return;
    el.querySelector('[name=support]').value = r.supported === true ? 'yes' : r.supported === false ? 'no' : '';
    el.querySelector('[name=correctness]').value = r.correctness || '';
  });
}
function download() {
  const state = collect();
  if (!Object.values(state).every(r => r.done)) return;
  const blob = new Blob([JSON.stringify({page: 'claims_review', split: SPLIT, sample_digest: SAMPLE,
    saved: new Date().toISOString(), items: state}, null, 1)], {type: 'application/json'});
  const url = URL.createObjectURL(blob), a = document.createElement('a');
  a.href = url; a.download = `claims_${SPLIT}_result.json`; a.click(); URL.revokeObjectURL(url);
}
document.addEventListener('change', refresh);
document.getElementById('dl').addEventListener('click', download);
restore(); refresh();
"""


def build(split: str, root=ROOT) -> dict:
    from jikgam.ingest import chunk as chunking
    from jikgam.ingest.corpus import load_docs
    from jikgam.v2.answer import evidence_block, gold_block
    from jikgam.v2.evaluate import load_questions
    from jikgam.v2.search import ChunkSet

    fingerprints = input_hashes(root, split)
    sources, missing = {}, []
    for name in RUNS:
        path = root / "data/v2/local/answers" / split / f"{name}.jsonl"
        if path.exists():
            sources[name] = read_rows(path)
        else:
            missing.append(name)
    if not sources or (split == "test" and missing):
        raise ValueError("필요한 답변 파일이 없습니다")
    sample, population = pick_claims(sources)
    if not sample:
        raise ValueError("오류 없는 판정에서 고를 주장이 없습니다")
    if split == "test" and population < SIZE:
        raise ValueError("test에서 확인할 유효 주장이 30개보다 적습니다")
    docs = load_docs()
    questions = {q["id"]: q for q in load_questions(split, include_none=True)}
    chunk_names = {RUNS[0]: "article256", RUNS[1]: "b-W4000-C800-raw"}
    sets, traces = {}, {}
    for name in {c["run"] for c in sample}:
        sets[name] = ChunkSet(chunk_names[name], chunking.load(chunk_names[name]), docs)
        traces[name] = {row["id"]: row for row in read_rows(root / "data/v2/local/runs" / split / f"{name}.jsonl")}
    items = []
    for chosen in sample:
        name, qid = chosen["run"], chosen["id"]
        row = sources[name][chosen["row"]]
        cs, q = sets[name], questions[qid]
        items.append({"sample_id": chosen["sample_id"], "question": q["question"],
                      "evidence": evidence_block(cs, traces[name][qid]["budget4000"]), "gold": gold_block(cs, q),
                      "claim": row["claims"][chosen["claim_index"]]["claim"]})
    if input_hashes(root, split) != fingerprints:
        raise ValueError("화면을 만드는 동안 입력이 바뀌었습니다")
    digest = sample_digest(sample, fingerprints)
    out = root / "data/v2/local/review"
    out.mkdir(parents=True, exist_ok=True)
    page = out / f"claims_{split}.html"
    metadata = {"split": split, "seed": SEED, "population": population, "sample_digest": digest,
                "source_hashes": fingerprints, "items": sample}
    atomic_json(sample_path(root, split), metadata)
    page.write_text(render_page(items, split, digest), encoding="utf-8", newline="\n")
    return {"claims": len(sample), "population": population, "runs_used": sorted(sources), "runs_missing": missing,
            "page": str(page)}


def agreement(sample: dict, result: dict) -> dict:
    """같은 표본의 모든 항목을 확인한 결과만 센다. 본문과 개별 판정은 반환하지 않는다."""
    if not isinstance(result, dict) or result.get("split") != sample["split"] or result.get("sample_digest") != sample["sample_digest"]:
        raise ValueError("검토 결과의 표본이 다릅니다")
    chosen, labels = sample["items"], result.get("items", {})
    if not chosen or not isinstance(labels, dict) or set(labels) != {c["sample_id"] for c in chosen}:
        raise ValueError("검토 결과의 항목이 빠졌거나 일치하지 않습니다")
    support = correct = both = 0
    for c in chosen:
        r = labels[c["sample_id"]]
        if not isinstance(r, dict) or r.get("done") is not True or not isinstance(r.get("supported"), bool) or r.get("correctness") not in CORRECTNESS:
            raise ValueError("아직 확인하지 않은 항목이 있습니다")
        s, a = r["supported"] == c["supported"], r["correctness"] == c["correctness"]
        support += s
        correct += a
        both += s and a
    n = len(chosen)
    return {"sampled": n, "reviewed": n, "supported_matches": support, "correctness_matches": correct,
            "both_matches": both, "supported_agreement": support / n,
            "correctness_agreement": correct / n, "both_agreement": both / n}


def apply(path: Path, split: str, root=ROOT) -> dict:
    from jikgam.v2.report import write_report

    sample, broken = read_review_json(sample_path(root, split))
    if broken:
        write_report(root, split)
        return {"valid": False, "invalidation_reason": "무효(손상)", "status": "사람 확인 전"}
    if sample is None:
        raise ValueError("표본 파일이 없습니다")
    result = json.loads(path.read_text(encoding="utf-8"))
    if sample["split"] != split:
        raise ValueError("표본의 분할이 다릅니다")
    if not sample_is_current(sample, root, split):
        raise ValueError("표본 입력이 현재 답변·단계 기록과 다릅니다")
    summary = {**agreement(sample, result), "valid": True, "sample_digest": sample["sample_digest"],
               "source_hashes": sample["source_hashes"]}
    target = review_path(root, split)
    atomic_json(target, summary)
    write_report(root, split)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "apply"])
    ap.add_argument("result", nargs="?", type=Path)
    ap.add_argument("--split", choices=["test", "val"], required=True)
    args = ap.parse_args()
    if args.cmd == "apply" and args.result is None:
        ap.error("결과 JSON 경로가 필요합니다")
    try:
        out = build(args.split) if args.cmd == "build" else apply(args.result, args.split)
    except (OSError, ValueError, KeyError):
        raise SystemExit("입력 파일·표본·검토 완료 상태를 확인해 주세요. 내용은 출력하지 않습니다.") from None
    if args.cmd == "apply":
        if out.get("valid") is False:
            out = {k: out[k] for k in ("status", "invalidation_reason")}
        else:
            out = {k: v for k, v in out.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
