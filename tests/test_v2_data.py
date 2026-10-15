"""v2 평가 데이터 만들기: 말뭉치 줄과 원문 찾기, 참고 경계 덩어리, 질문 검증(docs/evaluation-v2.md §2)."""

import json

from jikgam.ingest.corpus import ROOT, Doc, Line, squash
from jikgam.v2 import segments
from jikgam.v2.qa_gen import Call, focus_regions, is_local, validate


def make_doc(did: str, kind: str, rows: list[tuple[str, str]], redistribute: bool = True) -> Doc:
    """rows: (파일 이름, 줄 글)."""
    lines, start = [], 0
    for no, (file, text) in enumerate(rows, 1):
        lines.append(Line(no, file, 1, "text", text, start))
        start += len(text) + 1
    return Doc(did, did, kind, redistribute, lines)


LAW = make_doc("law", "법률", [("본문", f"제{i}조(목적) 이 조는 {i}번째 내용을 정한다. 소비자는 청약을 철회할 수 있다{i}.")
                                for i in range(1, 21)])
TERMS = make_doc("terms", "표준약관", [("본문", "제1조(환불) 회사는 이용자가 환불을 요청하면 7일 안에 돌려준다."),
                                    ("본문", "제2조(해지) 이용자는 언제든지 계약을 해지할 수 있다.")], redistribute=False)
DOCS = {"law": LAW, "terms": TERMS}


def test_find_quote_ignores_spaces_line_breaks_and_table_bars():
    doc = make_doc("t", "고시", [("별표 1", "10) 침대품질불량 | o 제품교환"), ("별표 1", "또는 구입가 환급")])
    spans = doc.find_quote("10)침대품질불량 o 제품교환 또는\n구입가 | 환급")
    assert spans == [(0, len(doc.text))]
    assert squash("a | b\nc")[0] == "abc"


def test_find_quote_returns_every_occurrence_as_alternative():
    doc = make_doc("t", "법률", [("본문", "같은 기준을 적는다."), ("본문", "다른 줄"), ("본문", "같은 기준을 적는다.")])
    spans = doc.find_quote("같은 기준을 적는다")
    assert len(spans) == 2 and doc.line_at(spans[1][0]).no == 3


def test_focus_regions_cover_every_line_once_in_order():
    regions = focus_regions(DOCS, ["law", "terms"], 3)
    covered = [(d, n) for reg in regions for d, a, b in reg for n in range(a, b + 1)]
    assert covered == [("law", n) for n in range(1, 21)] + [("terms", 1), ("terms", 2)]
    assert all(regions)


def q(qtype: str, *quotes: tuple[str, str], elsewhere: list[dict] | None = None) -> dict:
    ev = [{"doc": d, "quote": t, "same_elsewhere": elsewhere or []} for d, t in quotes]
    return {"question": "질문", "type": qtype, "answer": "답", "evidence": ev}


def test_validate_keeps_single_and_maps_span():
    counts: dict[str, int] = {}
    call = Call("c", "answerable", "법령", ["law"], [("law", 1, 5)])
    item = validate(DOCS, call, q("single", ("law", "제3조(목적) 이 조는 3번째 내용을 정한다.")), counts)
    assert item and len(item["evidence"]) == 1
    alt = item["evidence"][0]["alternatives"][0]
    assert LAW.line_at(alt["start"]).no == 3 and counts == {}


def test_validate_drops_question_when_quote_is_not_in_source():
    counts: dict[str, int] = {}
    call = Call("c", "answerable", "법령", ["law"])
    assert validate(DOCS, call, q("single", ("law", "원문에 없는 문장을 지어냈다.")), counts) is None
    assert counts == {"drop_evidence_quote_not_found": 1}


def test_validate_keeps_question_when_only_same_elsewhere_fails():
    counts: dict[str, int] = {}
    call = Call("c", "answerable", "법령", ["law"])
    item = validate(DOCS, call, q("single", ("law", "제4조(목적) 이 조는 4번째 내용을 정한다."),
                                  elsewhere=[{"doc": "law", "quote": "없는 대체 근거 문장이다"}]), counts)
    assert item and counts == {"elsewhere_quote_not_found": 1}


def test_validate_multi_needs_items_more_than_three_lines_apart():
    call = Call("c", "answerable", "법령", ["law"])
    near = q("multi", ("law", "제5조(목적) 이 조는 5번째 내용을 정한다."), ("law", "제7조(목적) 이 조는 7번째 내용을 정한다."))
    far = q("multi", ("law", "제5조(목적) 이 조는 5번째 내용을 정한다."), ("law", "제9조(목적) 이 조는 9번째 내용을 정한다."))
    counts: dict[str, int] = {}
    assert validate(DOCS, call, near, counts) is None and counts == {"drop_multi_too_close": 1}
    assert validate(DOCS, call, far, {}) is not None


def test_cross_questions_need_two_groups_and_terms_items_stay_local():
    call = Call("x", "cross", "넘나듦", ["law", "terms"])
    one_group = q("multi", ("law", "제1조(목적) 이 조는 1번째 내용을 정한다."), ("law", "제9조(목적) 이 조는 9번째 내용을 정한다."))
    counts: dict[str, int] = {}
    assert validate(DOCS, call, one_group, counts) is None and counts == {"drop_cross_single_group": 1}
    both = validate(DOCS, call, q("multi", ("law", "제1조(목적) 이 조는 1번째 내용을 정한다."),
                                  ("terms", "회사는 이용자가 환불을 요청하면 7일 안에 돌려준다.")), {})
    assert both and is_local(DOCS, both)
    law_only = validate(DOCS, Call("c", "answerable", "법령", ["law"]),
                        q("single", ("law", "제2조(목적) 이 조는 2번째 내용을 정한다.")), {})
    assert law_only and not is_local(DOCS, law_only)


def test_reference_segments_follow_the_plan_without_overlap():
    data = json.loads((ROOT / "data" / "v2" / "boundary" / "segments.json").read_text(encoding="utf-8"))
    segs = data["segments"]
    assert data["seed"] == segments.SEED and len(segs) == 15
    assert sum(n for *_, n in segments.PLAN) == 15
    assert [s["group"] for s in segs].count("법령") == 6
    assert [s["group"] for s in segs].count("고시") == 5
    assert [s["group"] for s in segs].count("표준약관") == 4
    for s in segs:
        assert s["end"] - s["start"] + 1 == segments.SIZE and s["context_start"] == max(1, s["start"] - segments.CONTEXT)
    by_doc: dict[str, list] = {}
    for s in segs:
        by_doc.setdefault(s["doc"], []).append((s["start"], s["end"]))
    for spans in by_doc.values():
        spans.sort()
        assert all(a[1] < b[0] for a, b in zip(spans, spans[1:]))
    # 분쟁해결기준 별표 2의 덩어리는 별표 2 안에, 나머지 하나는 그 밖에 있다(별표 2는 284~4648줄)
    regions = sorted((s["region"], s["start"], s["end"]) for s in segs if s["doc"] == "dispute-std")
    assert [r for r, *_ in regions] == ["별표 2", "별표 2", "본문·별표 1·3·4"]
    assert all(284 <= a and b <= 4648 for r, a, b in regions if r == "별표 2")
    assert all(b < 284 or a > 4648 for r, a, b in regions if r != "별표 2")


def test_split_clusters_keep_overlapping_evidence_together_and_follow_ratio():
    from jikgam.v2.splits import assign, clusters

    def item(qid: str, start: int, end: int, doc: str = "law") -> dict:
        return {"id": qid, "group": "법령", "type": "single",
                "evidence": [{"alternatives": [{"doc": doc, "start": start, "end": end}]}]}

    items = [item(f"q{k:03d}", k * 100, k * 100 + 50) for k in range(100)]
    items += [item("q900", 10, 20), item("q901", 140, 160)]  # q000·q001과 겹친다
    cs = clusters(items)
    assert ["q000", "q900"] in [[i["id"] for i in c] for c in cs]
    split_of = assign(items)
    assert split_of["q900"] == split_of["q000"] and split_of["q901"] == split_of["q001"]
    totals = {s: sum(v == s for v in split_of.values()) for s in ("train", "val", "test")}
    assert abs(totals["train"] - 30.6) <= 2 and abs(totals["val"] - 20.4) <= 2 and abs(totals["test"] - 51) <= 2
