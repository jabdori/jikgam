"""실패 유형 태그별로 튜닝 모델이 비교 대상보다 나아졌는지 나빠졌는지 원본 로그로 판정한다(docs/evaluation.md §7.2).

    uv run python -m jikgam.train.tag_compare results/decisions/val/<run_id> --models q35-0.8b-ko,q35-2b-ko --against clef-flash,jev

판정 규칙(결과를 보기 전에 정함): 회차별 정답 수의 최소~최대 범위가 겹치면 "같음", 튜닝 모델의 최소가 비교 대상의
최대보다 크면 "나아짐", 최대가 비교 대상의 최소보다 작으면 "나빠짐". 판단 수가 적은 태그가 많아 1~2개 차이도
"나아짐/나빠짐"으로 나오므로 건수를 함께 적는다.
"""

import argparse
from collections import defaultdict
from pathlib import Path

from jikgam.eval.report import decisions, load


def counts(ds: list[dict], backend: str, tag: str) -> tuple[int, int, int]:
    by_rep = defaultdict(lambda: [0, 0])
    for d in ds:
        if d["backend"] == backend and tag in d["tags"]:
            by_rep[d["rep"]][0] += d["correct"]
            by_rep[d["rep"]][1] += 1
    cs = [c for c, _ in by_rep.values()]
    return min(cs), max(cs), next(iter(by_rep.values()))[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--models", required=True)
    ap.add_argument("--against", default="clef-flash,jev")
    args = ap.parse_args()
    _, cases, raw = load(Path(args.run_dir))
    ds = decisions(raw, cases)
    models, against = args.models.split(","), args.against.split(",")
    tags = sorted({t for c in cases.values() for t in c["tags"]})

    def fmt(lo, hi, n):
        return f"{lo}/{n}" if lo == hi else f"{lo}~{hi}/{n}"

    L = ["| 태그 | " + " | ".join(models + against) + " | " +
         " | ".join(f"{m} 대 {a}" for m in models for a in against) + " |",
         "|---|" + "---|" * (len(models) + len(against) + len(models) * len(against))]
    for t in tags:
        c = {b: counts(ds, b, t) for b in models + against}
        verdicts = []
        for m in models:
            for a in against:
                (mlo, mhi, _), (alo, ahi, _) = c[m], c[a]
                verdicts.append("나아짐" if mlo > ahi else "나빠짐" if mhi < alo else "같음")
        L.append(f"| {t} ({sum(t in x['tags'] for x in cases.values())}건) | " +
                 " | ".join(fmt(*c[b]) for b in models + against) + " | " + " | ".join(verdicts) + " |")
    print("\n".join(L))


if __name__ == "__main__":
    main()
