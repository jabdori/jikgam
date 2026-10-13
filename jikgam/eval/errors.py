"""판단 단위 오답 목록. 백엔드마다 무엇을 틀렸는지 나란히 본다(실패 분석용).

    uv run python -m jikgam.eval.errors <run_dir> [<run_dir> ...] [--rep 0] [--only-disagree]

칸의 값은 해당 회차의 예측 라벨(예측 확률)이며, 틀린 칸에 *를 붙인다.
"""

import argparse
from collections import defaultdict
from pathlib import Path

from jikgam.eval.report import decisions, load


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dirs", nargs="+")
    ap.add_argument("--rep", type=int, default=0)
    ap.add_argument("--only-disagree", action="store_true", help="백엔드 사이에 맞고 틀림이 갈린 판단만")
    args = ap.parse_args()
    ds, backends, cases = [], [], {}
    for d in args.run_dirs:
        meta, cases, raw = load(Path(d))
        backends += list(meta["backends"])
        ds += [x for x in decisions(raw, cases) if x["rep"] == args.rep]
    cell = defaultdict(dict)
    for x in ds:
        cell[(x["case"], x["qid"])][x["backend"]] = x
    print("| 사례 | 질문 | 정답 | " + " | ".join(backends) + " |")
    print("|---|---|---|" + "---|" * len(backends))
    for (case, qid), by_b in sorted(cell.items()):
        oks = {b: x["correct"] for b, x in by_b.items()}
        if all(oks.values()) or (args.only_disagree and not any(oks.values())):
            continue
        gold = next(iter(by_b.values()))["gold"]
        cols = []
        for b in backends:
            x = by_b.get(b)
            if x is None:
                cols.append("")
            elif x["pred"] is None:
                cols.append("*(없음)")
            else:
                cols.append(f"{'' if x['correct'] else '*'}{x['pred']} ({x['prob']:.2f})")
        print(f"| {case} | {qid} | {gold} | " + " | ".join(cols) + " |")


if __name__ == "__main__":
    main()
