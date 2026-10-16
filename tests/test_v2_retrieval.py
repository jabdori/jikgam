"""v2 분할·검색·채점(docs/evaluation-v2.md §3.2~§3.6, §4.2)."""

from jikgam.ingest import chunk as chunking
from jikgam.ingest.corpus import Doc, Line
from jikgam.ingest.window import apply_cmax, windows_from
from jikgam.v2.evaluate import Coverage, Scorer
from jikgam.v2.hybrid import fuse


def make_doc(rows: list[tuple[str, str]], did: str = "d") -> Doc:
    lines, start = [], 0
    for no, (file, text) in enumerate(rows, 1):
        lines.append(Line(no, file, 1, "text", text, start))
        start += len(text) + 1
    return Doc(did, did, "법률", True, lines)


def test_fixed_ranges_cover_all_lines_with_overlap_and_progress():
    lt = [10] * 50  # 줄마다 10토큰
    ranges = chunking.fixed_ranges(lt, 1, 50, 100)
    assert ranges[0] == (1, 10) and ranges[-1][1] == 50
    for (a1, b1), (a2, b2) in zip(ranges, ranges[1:]):
        assert a2 > a1 and a2 <= b1 + 1  # 앞으로 나가고 끊기지 않는다
        assert b1 - a2 + 1 == 1  # 10% 겹침 = 한 줄(10토큰)
    covered = {n for a, b in ranges for n in range(a, b + 1)}
    assert covered == set(range(1, 51))


def test_fixed_ranges_move_cut_to_nearest_line_boundary():
    lt = [60, 60, 60]  # 100토큰에서 자르면 60(1줄)이 120(2줄)보다 가깝지 않다: 40 대 20
    assert chunking.fixed_ranges(lt, 1, 3, 100)[0] == (1, 2)
    lt = [90, 60, 60]  # 90이 150보다 100에 가깝다
    assert chunking.fixed_ranges(lt, 1, 3, 100)[0] == (1, 1)


def test_article_chunks_split_at_article_heads_and_fill_the_rest():
    rows = [("본문", "어떤 법률"), ("본문", "제1조(목적) 이 법은 목적을 정한다."), ("본문", "제2조(정의) 제1조에 따른 말의 뜻은 다음과 같다."),
            ("본문", "1. 소비자란 사람이다."), ("본문", "제2조의2(적용) 영 제3조와 「다른 법」 제4조를 따른다."),
            ("별표 1", "[별표 1] 과태료 기준(제2조 관련)"), ("별표 1", "1. 일반기준")]
    doc = make_doc(rows)
    lt = [5] * len(rows)
    cs = chunking.article(doc, 100, lt)
    arts = [(c.article, c.start_line, c.end_line) for c in cs if c.kind == "article"]
    assert arts == [("제1조", 2, 2), ("제2조", 3, 4), ("제2조의2", 5, 5)]
    assert next(c for c in cs if c.article == "제2조").refs == ["제1조"]
    assert next(c for c in cs if c.article == "제2조의2").refs == []  # 다른 법령의 조는 참조로 두지 않는다
    tables = [c for c in cs if c.table_of]
    assert tables and tables[0].table_of == "제2조" and tables[0].start_line == 6
    assert cs[0].kind == "fixed" and cs[0].start_line == 1  # 앞머리


def test_windows_judge_first_80_percent_and_advance():
    lt = [100] * 100
    jend, wend = windows_from(lt, 1, 1000)
    assert (jend, wend) == (8, 10)
    assert windows_from(lt, 95, 1000) == (100, 100)  # 끝에 닿으면 모두 판단한다


def test_apply_cmax_cuts_before_the_line_that_overflows():
    lt = [300] * 10
    bounds = {1, 6}
    open_start, cuts = apply_cmax(lt, bounds, 1, 10, 1, 800)
    # 두 줄(600)에 셋째 줄을 더하면 800을 넘어 셋째 줄에서 자른다. 6줄은 원래 경계다
    assert cuts == [3, 5, 8, 10] and bounds == {1, 3, 5, 6, 8, 10} and open_start == 10


def test_fuse_rrf_and_weighted():
    bm = [("a", 9.0), ("b", 3.0)]
    em = [("b", 0.9), ("c", 0.5), ("a", 0.1)]
    rrf = fuse(bm, em, "rrf")
    assert rrf["b"] > rrf["a"] > rrf["c"]
    w = fuse(bm, em, "w0.7")
    assert w["a"] == 0.7 and round(w["b"], 3) == 0.3


def test_scorer_needs_half_of_nonspace_chars_covered():
    doc = make_doc([("본문", "가나다라 마바사아"), ("본문", "자차카타")])
    sc = Scorer({"d": doc})
    alt = {"doc": "d", "start": 0, "end": 9}  # 공백 제외 8자
    cov = Coverage()
    cov.add("d", 0, 4)  # 4자
    assert sc.covered(alt, cov.merged("d"))
    cov2 = Coverage()
    cov2.add("d", 0, 3)  # 3자
    assert not sc.covered(alt, cov2.merged("d"))
    item = {"evidence": [{"alternatives": [{"doc": "d", "start": 10, "end": 14}, alt]}]}
    assert sc.found(item, cov) == [True]  # 대체 근거 하나만 덮어도 찾은 것이다
