"""v2 원본 문서 기록과 텍스트 추출(ADR 0023)."""

import hashlib
import json
import tomllib
from pathlib import Path

from jikgam.ingest.extract import SEP, X_TOL, file_names, table_lines

ROOT = Path(__file__).resolve().parent.parent
SOURCES = tomllib.loads((ROOT / "config" / "v2" / "sources.toml").read_text(encoding="utf-8"))["doc"]
MANIFEST = json.loads((ROOT / "data" / "v2" / "sources" / "manifest.json").read_text(encoding="utf-8"))["docs"]
SUMMARY = json.loads((ROOT / "data" / "v2" / "extracted" / "summary.json").read_text(encoding="utf-8"))["docs"]


def ch(text: str, x0: float, top: float, width: float = 8.0) -> dict:
    return {"text": text, "x0": x0, "x1": x0 + width, "top": top, "bottom": top + 10}


def test_table_lines_split_at_cell_border_even_without_space():
    # 왼쪽 칸 끝 글자와 오른쪽 칸 첫 글자가 붙어 있어도 칸에 따라 나뉜다
    cells = [(0, 0, 100, 40), (100, 0, 200, 40)]
    chars = [ch("보", 76, 5), ch("존", 84, 5), ch("하", 92, 5), ch("법", 100.5, 5), ch("제", 100.5 + 8 + X_TOL + 1, 5)]
    assert table_lines(chars, cells) == ["보존하" + SEP + "법 제"]


def test_table_lines_keep_row_alignment_inside_tall_cells():
    # 한 칸에 여러 항목이 있어도 같은 높이의 글은 같은 줄에 놓인다
    cells = [(0, 0, 100, 60), (100, 0, 200, 60)]
    chars = [ch("가", 2, 5), ch("나", 102, 5), ch("다", 2, 25), ch("라", 102, 26)]
    assert table_lines(chars, cells) == ["가" + SEP + "나", "다" + SEP + "라"]


def test_table_lines_use_smallest_cell_for_nested_tables():
    cells = [(0, 0, 200, 60), (0, 0, 100, 60), (100, 0, 200, 60)]
    chars = [ch("가", 2, 5), ch("나", 102, 5)]
    assert table_lines(chars, cells) == ["가" + SEP + "나"]


def test_file_names_skip_original_hwp_and_number_tables():
    files = [{"role": "원본", "path": "a/original.hwpx"}, {"role": "본문", "path": "a/body.pdf"},
             {"role": "별표", "path": "a/byl-01.pdf"}, {"role": "별표", "path": "a/byl-02.pdf"}]
    assert [n for n, _ in file_names(files)] == ["본문", "별표 1", "별표 2"]


def test_manifest_covers_every_source_with_version_and_hashes():
    assert [d["id"] for d in SOURCES] == list(MANIFEST)
    for d in SOURCES:
        m = MANIFEST[d["id"]]
        assert m["header"] and m["effective"] and m["fetched"], d["id"]
        assert m["redistribute"] == d.get("redistribute", True)
        for f in m["files"]:
            path = ROOT / f["path"]
            if m["redistribute"]:
                assert path.exists(), f["path"]
            if path.exists():  # 저장소 밖 문서는 받은 PC에만 있다
                assert hashlib.sha256(path.read_bytes()).hexdigest() == f["sha256"], f["path"]


def test_documents_not_redistributed_stay_out_of_the_repository():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "data/v2/local/" in ignored
    for did, m in MANIFEST.items():
        local = not m["redistribute"]
        for f in m["files"]:
            assert f["path"].startswith("data/v2/local/") == local, f["path"]
        assert SUMMARY[did]["output"].startswith("data/v2/local/") == local
        assert (ROOT / "data" / "v2" / "extracted" / f"{did}.jsonl").exists() != local


def test_summary_matches_manifest():
    assert list(SUMMARY) == list(MANIFEST)
    for did, s in SUMMARY.items():
        pdfs = [f for f in MANIFEST[did]["files"] if f["path"].endswith(".pdf")]
        assert [x["file"] for x in s["files"]] == [n for n, _ in file_names(pdfs)]
        assert s["pages"] == sum(f["pages"] for f in pdfs)
