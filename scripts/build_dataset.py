"""판별 사례 원본을 검증하고, 조항 본문을 채워 분할별 내보내기 파일을 만든다.

사용법:
    python scripts/build_dataset.py            # 검증 + 빌드
    python scripts/build_dataset.py --check    # 검증만
    python scripts/build_dataset.py --aug      # 학습 전용 보강 사례(data/cases_aug) 검증 + train_aug.jsonl
    python scripts/build_dataset.py --aug --check

형식과 규칙은 docs/data-format.md를 따른다. 보강 사례는 분할(data/splits.json)과 train/val/test
산출물에 들어가지 않고 data/build/train_aug.jsonl에만 split "train_aug"로 쓴다.
"""

import argparse
import hashlib
import json
import random
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TERMS = {"org_a": ROOT / "domain/org_a/terms.md"}
QUESTIONS = ROOT / "data/questions.json"
CASES_DIR = ROOT / "data/cases"
AUG_DIR = ROOT / "data/cases_aug"
SPLITS = ROOT / "data/splits.json"
BUILD_DIR = ROOT / "data/build"
SEED = 20261012

TAGS = {
    "correction", "negation", "ellipsis", "colloquial", "number_date", "stt_error",
    "partial_stt", "similar_article", "exception", "multi_evidence", "missing_evidence",
    "insufficient_info", "stale_suggestion", "handoff",
}
SPEAKERS = {"customer", "agent"}


def parse_terms(path):
    """terms.md에서 조항 ID → {title, type, refs, text}를 읽는다."""
    articles = {}
    current = None
    body = []

    def flush():
        if current:
            current["text"] = "\n".join(body).strip()
            articles[current["id"]] = current

    for line in path.read_text(encoding="utf-8").splitlines():
        head = re.match(r"^## \[(\S+)\] (.+)$", line)
        if head:
            flush()
            current = {"id": head.group(1), "title": head.group(2).strip()}
            body = []
            continue
        if current is None:
            continue
        meta = re.match(r"^<!-- type: (\w+); refs:(.*)-->$", line.strip())
        if meta:
            current["type"] = meta.group(1)
            current["refs"] = [r.strip() for r in meta.group(2).split(",") if r.strip()]
            continue
        if line.strip() == "---":
            flush()
            current = None
            continue
        body.append(line)
    flush()
    return articles


def load_cases(cases_dir=CASES_DIR):
    cases = []
    for path in sorted(cases_dir.glob("*.jsonl")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError as e:
                cases.append({"_error": f"{path.name}:{n} JSON 파싱 실패: {e}"})
                continue
            case["_where"] = f"{path.name}:{n}"
            cases.append(case)
    return cases


def question_def(qid, bank):
    """질문 ID를 질문 정의로 바꾼다. applies:A-05는 applies 정의에 조항 ID를 채운다."""
    base, _, arg = qid.partition(":")
    if base not in bank:
        return None
    q = json.loads(json.dumps(bank[base]))
    if base == "applies":
        if not arg:
            return None
        q["instructions"] = q["instructions"].replace("{article_id}", arg)
    elif arg:
        return None
    return q


def validate(cases, articles_by_org, bank):
    errors = []
    seen = set()
    for c in cases:
        if "_error" in c:
            errors.append(c["_error"])
            continue
        w = f"{c['_where']} ({c.get('id', '?')})"
        for f in ("id", "group", "tags", "state", "questions", "gold", "rationale"):
            if f not in c:
                errors.append(f"{w}: 필드 없음 '{f}'")
        if any(f not in c for f in ("id", "group", "state", "questions", "gold")):
            continue
        if c["id"] in seen:
            errors.append(f"{w}: id 중복")
        seen.add(c["id"])
        if not c["id"].startswith(c["group"] + "-"):
            errors.append(f"{w}: id는 group으로 시작해야 함 (예: {c['group']}-1)")

        for t in c.get("tags", []):
            if t not in TAGS:
                errors.append(f"{w}: 알 수 없는 태그 '{t}'")
        if not c.get("tags"):
            errors.append(f"{w}: 태그 없음")

        s = c["state"]
        org = s.get("org")
        if org not in articles_by_org:
            errors.append(f"{w}: 알 수 없는 org '{org}'")
            continue
        articles = articles_by_org[org]
        try:
            date.fromisoformat(s.get("today", ""))
        except ValueError:
            errors.append(f"{w}: state.today가 날짜 형식이 아님")
        if not isinstance(s.get("identity_verified"), bool):
            errors.append(f"{w}: state.identity_verified는 true/false")
        conv = s.get("conversation") or []
        if not conv:
            errors.append(f"{w}: 대화 없음")
        for i, u in enumerate(conv):
            if u.get("speaker") not in SPEAKERS:
                errors.append(f"{w}: conversation[{i}].speaker 오류")
            if not isinstance(u.get("final"), bool):
                errors.append(f"{w}: conversation[{i}].final는 true/false")
            if not str(u.get("text", "")).strip():
                errors.append(f"{w}: conversation[{i}].text 비어 있음")
        if conv and conv[-1].get("speaker") != "customer":
            errors.append(f"{w}: 마지막 발화는 고객이어야 함")
        if not isinstance(s.get("known_facts"), dict):
            errors.append(f"{w}: known_facts는 객체")
        aids = s.get("article_ids") or []
        for a in aids:
            if a not in articles:
                errors.append(f"{w}: 없는 조항 '{a}'")
        if len(set(aids)) != len(aids):
            errors.append(f"{w}: article_ids 중복")

        qs = c["questions"]
        if not 3 <= len(qs) <= 8:
            errors.append(f"{w}: 질문 수 {len(qs)} (3~8개)")
        if len(set(qs)) != len(qs):
            errors.append(f"{w}: 질문 중복")
        for qid in qs:
            q = question_def(qid, bank)
            if q is None:
                errors.append(f"{w}: 알 수 없는 질문 '{qid}'")
                continue
            if qid.startswith("applies:") and qid.split(":", 1)[1] not in aids:
                errors.append(f"{w}: '{qid}'의 조항이 article_ids에 없음")
            if qid == "suggestion_valid" and not s.get("previous_suggestion"):
                errors.append(f"{w}: suggestion_valid에는 previous_suggestion 필요")
            if qid == "condition_changed" and not s.get("known_facts"):
                errors.append(f"{w}: known_facts가 비어 있으면 condition_changed를 묻지 않음")
            if qid == "sufficient" and not aids:
                errors.append(f"{w}: sufficient에는 후보 조항 필요")
            label = c["gold"].get(qid)
            if label is None:
                errors.append(f"{w}: '{qid}' 정답 없음")
            elif q["type"] == "noul" and label not in ("true", "false"):
                errors.append(f"{w}: '{qid}' 정답은 \"true\"/\"false\" 문자열")
            elif q["type"] == "choice" and label not in q["criteria"]:
                errors.append(f"{w}: '{qid}' 정답 '{label}'이 선택지에 없음")
        for k in c["gold"]:
            if k not in qs:
                errors.append(f"{w}: 질문에 없는 정답 키 '{k}'")
    return errors


def assign_splits(group_tags, group_sizes):
    """group → split. 기존 배정은 유지하고, test가 이미 있으면 새 group은 train/val에만 넣는다.

    태그(실패 유형과 action 정답)가 분할마다 목표 비율에 가깝게 퍼지도록, 드문 태그를 가진
    group부터 그 태그가 목표보다 가장 부족한 분할에 넣는다.
    """
    existing = json.loads(SPLITS.read_text(encoding="utf-8")) if SPLITS.exists() else {}
    assignment = dict(existing.get("groups", {}))
    frozen = "test" in assignment.values()
    new = [g for g in group_tags if g not in assignment]
    if not new:
        return assignment, existing.get("frozen_at")

    target = {"train": 0.6, "val": 0.2, "test": 0.2} if not frozen else {"train": 0.75, "val": 0.25}
    tag_total = Counter()
    for g in group_tags:
        tag_total.update(group_tags[g])
    have = defaultdict(Counter)  # split → tag → 사례 수
    size = Counter()
    for g, sp in assignment.items():
        if g in group_tags:
            have[sp].update(group_tags[g])
            size[sp] += group_sizes[g]
    total = sum(group_sizes.values())

    rng = random.Random(SEED)
    order = sorted(new)
    rng.shuffle(order)
    order.sort(key=lambda g: min(tag_total[t] for t in group_tags[g]))
    for g in order:
        rare = min(group_tags[g], key=lambda t: (tag_total[t], t))

        def deficit(sp):
            tag_gap = target[sp] * tag_total[rare] - have[sp][rare]
            size_gap = target[sp] * total - size[sp]
            return (tag_gap, size_gap)

        sp = max(target, key=deficit)
        assignment[g] = sp
        have[sp].update(group_tags[g])
        size[sp] += group_sizes[g]
    frozen_at = existing.get("frozen_at") or date.today().isoformat()
    return assignment, frozen_at


def export_row(c, articles, bank, split):
    s = dict(c["state"])
    aids = s.pop("article_ids", [])
    s["articles"] = [{"id": a, "title": articles[a]["title"], "text": articles[a]["text"]} for a in aids]
    if not s.get("previous_suggestion"):
        s.pop("previous_suggestion", None)
    questions = {qid: question_def(qid, bank) for qid in c["questions"]}
    gold = {qid: {"type": questions[qid]["type"], "label": c["gold"][qid]} for qid in c["questions"]}
    return {
        "id": c["id"],
        "group": c["group"],
        "split": split,
        "tags": c["tags"],
        "state": json.dumps(s, ensure_ascii=False),
        "questions": json.dumps(questions, ensure_ascii=False),
        "gold": json.dumps(gold, ensure_ascii=False),
    }


def summary(rows, assignment):
    by_split = Counter(r["split"] for r in rows)
    groups_by_split = Counter(assignment.values())
    tag_split = defaultdict(Counter)
    label_dist = defaultdict(Counter)
    for r in rows:
        for t in r["tags"]:
            tag_split[t][r["split"]] += 1
        for qid, g in json.loads(r["gold"]).items():
            base = qid.split(":")[0]
            label_dist[base][g["label"]] += 1

    lines = ["# 판별 데이터 요약", "", "`scripts/build_dataset.py`가 생성한다. 손으로 고치지 않는다.", ""]
    lines += ["## 분할", "", "| 분할 | 사례 | 대화 그룹 |", "|---|---|---|"]
    for sp in ("train", "val", "test"):
        lines.append(f"| {sp} | {by_split[sp]} | {groups_by_split[sp]} |")
    lines.append(f"| 합계 | {len(rows)} | {len(assignment)} |")
    lines += ["", "## 실패 유형별 사례 수", "", "| 태그 | train | val | test | 합계 |", "|---|---|---|---|---|"]
    for t in sorted(tag_split):
        c = tag_split[t]
        lines.append(f"| {t} | {c['train']} | {c['val']} | {c['test']} | {sum(c.values())} |")
    lines += ["", "## 질문별 정답 분포", "", "| 질문 | 정답 분포 |", "|---|---|"]
    for q in sorted(label_dist):
        dist = ", ".join(f"{k} {v}" for k, v in label_dist[q].most_common())
        lines.append(f"| {q} | {dist} |")
    return "\n".join(lines) + "\n"


def build_aug(articles_by_org, bank, base_cases, check_only):
    """학습 전용 보강 사례를 같은 검증기로 검사하고 train_aug.jsonl을 쓴다.

    보강 사례는 분할에 넣지 않으므로 data/splits.json과 train/val/test/summary 산출물은 건드리지 않는다.
    """
    aug = load_cases(AUG_DIR)
    errors = validate(aug, articles_by_org, bank)
    base_ids = {c["id"] for c in base_cases if "id" in c}
    base_groups = {c["group"] for c in base_cases if "group" in c}
    for c in aug:
        if "_error" in c:
            continue
        w = f"{c['_where']} ({c.get('id', '?')})"
        if c.get("id") in base_ids:
            errors.append(f"{w}: id가 data/cases와 겹침")
        if c.get("group") in base_groups:
            errors.append(f"{w}: group이 data/cases와 겹침")

    print(f"보강 사례 {len(aug)}건")
    if errors:
        for e in errors:
            print("  -", e)
        print(f"보강 형식 오류 {len(errors)}건")
        sys.exit(1)
    print("보강 형식 오류 0건")
    if check_only:
        return
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    rows = [export_row(c, articles_by_org[c["state"]["org"]], bank, "train_aug") for c in aug]
    (BUILD_DIR / "train_aug.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8", newline="\n")
    print(f"train_aug {len(rows)} ({len({r['group'] for r in rows})}개 그룹)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="검증만 하고 파일을 쓰지 않는다")
    ap.add_argument("--aug", action="store_true",
                    help="data/cases_aug의 학습 전용 보강 사례만 빌드한다(분할·기존 산출물은 바꾸지 않음)")
    args = ap.parse_args()

    articles_by_org = {org: parse_terms(p) for org, p in TERMS.items()}
    bank = json.loads(QUESTIONS.read_text(encoding="utf-8"))["questions"]
    cases = load_cases()
    errors = validate(cases, articles_by_org, bank)

    print(f"조항 {sum(len(a) for a in articles_by_org.values())}개, 사례 {len(cases)}건")
    if errors:
        for e in errors:
            print("  -", e)
        print(f"형식 오류 {len(errors)}건")
        sys.exit(1)
    print("형식 오류 0건")
    if args.aug:
        build_aug(articles_by_org, bank, cases, args.check)
        return
    if args.check:
        return

    group_tags, group_sizes = defaultdict(Counter), Counter()
    for c in cases:
        group_tags[c["group"]].update(c["tags"])
        if "action" in c["gold"]:
            group_tags[c["group"]][f"action={c['gold']['action']}"] += 1
        group_sizes[c["group"]] += 1
    assignment, frozen_at = assign_splits(group_tags, group_sizes)
    SPLITS.write_text(json.dumps({"seed": SEED, "frozen_at": frozen_at, "groups": dict(sorted(assignment.items()))},
                                 ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for c in cases:
        rows.append(export_row(c, articles_by_org[c["state"]["org"]], bank, assignment[c["group"]]))
    for sp in ("train", "val", "test"):
        part = [r for r in rows if r["split"] == sp]
        (BUILD_DIR / f"{sp}.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in part), encoding="utf-8", newline="\n")
    # Windows에서도 줄바꿈을 LF로 써야 test 파일의 sha256이 플랫폼에 따라 달라지지 않는다.
    (BUILD_DIR / "summary.md").write_text(summary(rows, assignment), encoding="utf-8", newline="\n")
    digest = hashlib.sha256((BUILD_DIR / "test.jsonl").read_bytes()).hexdigest()[:16]
    print(f"train {sum(r['split']=='train' for r in rows)} / val {sum(r['split']=='val' for r in rows)} "
          f"/ test {sum(r['split']=='test' for r in rows)}, test sha256 {digest}")


if __name__ == "__main__":
    main()
