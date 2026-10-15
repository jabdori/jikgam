"""참고 경계용 덩어리 15개를 뽑는다(docs/evaluation-v2.md §2.2).

연속한 60줄 덩어리다. 시작 위치는 고정한 seed로 무작위로 정하고, 같은 문서의 덩어리가 겹치면 다음 seed 값으로
다시 뽑는다. 내용을 보지 않고 위치만 정한다. 결과는 위치(문서, 줄 번호)만 담으므로 저장소에 둔다.

    uv run python -m jikgam.v2.segments
"""

import json
import random
from dataclasses import asdict, dataclass

from jikgam.ingest.corpus import ROOT, Doc, load_docs

SEED = 20261016
SIZE = 60
CONTEXT = 20
OUT = ROOT / "data" / "v2" / "boundary" / "segments.json"

# (문서, 구역 이름, 구역에 드는 파일, 덩어리 수). 구역 안에서만 덩어리를 뽑는다.
PLAN = [
    ("ecom-act", "전체", None, 1),
    ("ecom-decree", "전체", None, 1),
    ("ecom-rule", "전체", None, 1),
    ("terms-act", "전체", None, 1),
    ("consumer-act", "전체", None, 1),
    ("content-act", "전체", None, 1),
    ("dispute-std", "본문·별표 1·3·4", {"본문", "별표 1", "별표 3", "별표 4"}, 1),
    ("dispute-std", "별표 2", {"별표 2"}, 2),
    ("content-guide", "전체", None, 2),
    ("game-terms", "전체", None, 2),
    ("ecom-terms", "전체", None, 2),
]


@dataclass
class Segment:
    id: str
    doc: str
    group: str
    region: str
    start: int  # 첫 줄 번호(판단 대상이 아니다)
    end: int  # 마지막 줄 번호(포함)
    context_start: int  # 맥락으로 보여 주는 첫 줄(덩어리 앞 최대 20줄)
    seed: str


def valid_starts(doc: Doc, files: set[str] | None) -> list[int]:
    """덩어리 60줄이 모두 구역 안에 드는 시작 줄."""
    inside = [files is None or line.file in files for line in doc.lines]
    starts = []
    for s in range(1, len(doc.lines) - SIZE + 2):
        if all(inside[s - 1:s - 1 + SIZE]):
            starts.append(s)
    return starts


def draw(docs: dict[str, Doc]) -> list[Segment]:
    segments: list[Segment] = []
    for did, region, files, count in PLAN:
        doc = docs[did]
        starts = valid_starts(doc, files)
        attempt = 0
        while True:
            seed = f"{SEED}:{did}:{region}:{attempt}"
            picked = sorted(random.Random(seed).sample(starts, count))
            taken = [(g.start, g.end) for g in segments if g.doc == did] + [(s, s + SIZE - 1) for s in picked]
            taken.sort()
            if all(a[1] < b[0] for a, b in zip(taken, taken[1:])):
                break
            attempt += 1
        for s in picked:
            segments.append(Segment(f"seg-{len(segments) + 1:02d}", did, doc.group, region, s, s + SIZE - 1,
                                    max(1, s - CONTEXT), seed))
    return segments


def main() -> None:
    docs = load_docs()
    segments = draw(docs)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {"seed": SEED, "size": SIZE, "context": CONTEXT, "segments": [asdict(s) for s in segments]}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    for s in segments:
        print(f"{s.id} {s.doc} {s.region} {s.start}-{s.end}")


if __name__ == "__main__":
    main()
