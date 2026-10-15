"""v2 근거 검색 실험의 원본 문서를 공개 누리집에서 받는다(ADR 0023).

config/v2/sources.toml의 문서마다 PDF를 받아 두고, 출처·받은 날짜·버전·해시를 data/v2/sources/manifest.json에 적는다.

- 국가법령정보센터 법령·행정규칙: 본문 화면의 "저장"에서 PDF를 고르는 것과 같은 동작을 브라우저(Playwright)로 한다.
  별표는 화면에 붙은 PDF 파일을 받는다. 별지 서식(빈 양식)은 받지 않는다.
- 공정위 표준약관: 게시물에 붙은 한글 파일(HWP·HWPX)을 받아 한컴오피스 자동화(scripts/hwp_to_pdf.ps1)로 PDF로 저장한다.
  누리집에 PDF가 없기 때문이다. Windows와 한컴오피스가 필요하다.

redistribute = false인 문서는 data/v2/local/(git 제외)에 두고, 나머지는 data/v2/sources/<문서>/에 둔다.
이미 받은 문서는 건너뛴다. --force로 다시 받으면 머리말의 시행일·번호가 manifest와 같은지 확인한다(버전 고정).

    uv run python scripts/v2_fetch_sources.py
    uv run python scripts/v2_fetch_sources.py --only dispute-std --force
"""

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
from pathlib import Path

import httpx
from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "v2" / "sources.toml"
SHARED = ROOT / "data" / "v2" / "sources"
LOCAL = ROOT / "data" / "v2" / "local"
MANIFEST = SHARED / "manifest.json"

LAW = "https://www.law.go.kr/LSW"
FTC = "https://www.ftc.go.kr/www"
PAGES = {"law": LAW + "/lsInfoP.do?lsiSeq={seq}", "admrul": LAW + "/admRulInfoP.do?admRulSeq={seq}"}
# 본문 저장 대화상자를 여는 버튼과 PDF 선택 칸. 둘 다 같은 함수(beforeSavePrint)로 저장한다.
SAVE = {"law": ("#bdySaveBtn", "#FileSavePdf1"), "admrul": ("a[onclick*='f_LsInfoPrint(0']", "#FileSavePdf")}
HEADER = re.compile(r"\[시행\s*([0-9.\s]+?)\.?\s*\]\s*\[([^\]]+)\]")
TERMS_HEADER = re.compile(r"표준약관\s*제\s*(\d+)\s*호\s*\(([0-9.\s]+?)\.?\s*개정\)")
TIMEOUT_MS = 120_000


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def disposition_name(headers) -> str:
    cd = headers.get("content-disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
    if not m:
        return ""
    name = urllib.parse.unquote(m.group(1))
    try:  # 브라우저 요청에는 UTF-8 이름이 그대로 오고, 헤더는 latin-1로 읽힌다
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def header_of(text: str) -> dict:
    m = HEADER.search(text)
    if not m:
        return {}
    effective = dt.date(*map(int, re.findall(r"\d+", m.group(1))[:3])).isoformat()
    return {"effective": effective, "header": " ".join(m.group(0).split())}


def fetch_lawgo(page: Page, doc: dict, out: Path) -> dict:
    """법령·행정규칙 본문 PDF와 별표 PDF를 받는다."""
    url = PAGES[doc["site"]].format(seq=doc["seq"])
    page.goto(url, wait_until="networkidle", timeout=TIMEOUT_MS)
    info = header_of(page.inner_text("body"))
    if not info:
        raise RuntimeError(f"{doc['id']}: 머리말의 시행일을 찾지 못했다 ({url})")
    button, radio = SAVE[doc["site"]]
    page.locator(button).first.click()
    page.wait_for_selector(radio, timeout=TIMEOUT_MS)
    # 대화상자의 조문 목록(저장 범위)이 다 뜬 뒤에 저장해야 한다. 기본값(본문 전체)을 바꾸지 않는다.
    page.wait_for_function("document.getElementsByName('arSeq').length > 0", timeout=TIMEOUT_MS)
    page.locator(radio).check()
    with page.expect_download(timeout=TIMEOUT_MS) as d:
        page.evaluate("beforeSavePrint()")
    body = out / "body.pdf"
    d.value.save_as(body)
    files = [{"role": "본문", "path": body, "name": d.value.suggested_filename, "url": url}]
    links = law_tables(page, doc) if doc["site"] == "law" else admrul_tables(page)
    for i, href in enumerate(links, 1):
        resp = page.request.get(href, timeout=TIMEOUT_MS)
        fname = disposition_name(resp.headers)
        if not fname.lower().endswith(".pdf"):
            raise RuntimeError(f"{doc['id']}: 별표가 PDF가 아니다 ({fname})")
        path = out / f"byl-{i:02d}.pdf"
        path.write_bytes(resp.body())
        files.append({"role": "별표", "path": path, "name": fname, "url": href})
    return {**info, "source_url": url, "files": files}


def admrul_tables(page: Page) -> list[str]:
    """행정규칙 화면에는 별표마다 HWP와 PDF 링크가 같은 이름으로 붙어 있다. 이름이 [별표로 시작하는 것의 PDF만 고른다.
    링크만으로는 형식을 알 수 없고 HEAD 요청을 받지 않아, 받아 본 뒤 파일 이름으로 고른다."""
    links = page.eval_on_selector_all("a[href*='flDownload.do']", "els => els.map(e => e.href)")
    picked: dict[str, str] = {}
    for href in dict.fromkeys(links):
        name = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("flNm", [""])[0]
        if name.startswith("[별표") and name not in picked:
            if disposition_name(page.request.get(href, timeout=TIMEOUT_MS).headers).lower().endswith(".pdf"):
                picked[name] = href
    return list(picked.values())


def law_tables(page: Page, doc: dict) -> list[str]:
    """법령 화면의 "별표·서식" 목록에서 [별표]만 골라, 별표마다 팝업 화면이 가리키는 PDF 파일 주소를 얻는다.
    별지 서식(빈 양식)은 받지 않는다."""
    page.evaluate("fLsBylShow(document.body)")
    # 목록을 불러오는 동안 "Loading data..."가 보인다. 별표가 없는 법령은 목록이 빈 채로 끝난다.
    page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
    page.wait_for_function("!document.getElementById('lsBylLayer').innerText.includes('Loading')", timeout=TIMEOUT_MS)
    items = page.eval_on_selector_all(
        "#lsBylLayer a[onclick*='showDetailNwLsByl']",
        "els => els.map(e => [e.innerText.trim(), e.getAttribute('onclick')])")
    links = []
    for text, onclick in items:
        if not text.startswith("[별표"):
            continue
        byl, seq, ef = re.findall(r"'([^']*)'", onclick)[:3]
        # 목록은 이미 읽었으니 같은 탭에서 별표 팝업 화면으로 옮겨 간다.
        page.goto(f"{LAW}/lsBylInfoP.do?bylSeq={byl}&lsiSeq={seq}&efYd={ef}", wait_until="networkidle",
                  timeout=TIMEOUT_MS)
        fl = page.locator("#pdfFlSeq").input_value() if page.locator("#pdfFlSeq").count() else ""
        if not fl or fl == "0":
            raise RuntimeError(f"{doc['id']}: {text}의 PDF 파일이 없다")
        links.append(f"{LAW}/flDownload.do?flSeq={fl}")
    return links


def hwp_pids() -> set[int]:
    run = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command",
                          "Get-Process -Name Hwp -ErrorAction SilentlyContinue | ForEach-Object Id"],
                         capture_output=True, text=True)
    return {int(x) for x in run.stdout.split()}


def hwp_to_pdf(original: Path, pdf: Path) -> None:
    """한컴오피스로 PDF를 만든다. 저장소 폴더의 파일을 열면 한컴오피스가 확인 창을 띄우고 멈춰(2026-10-15 확인)
    임시 폴더에 복사해 변환한다. 실패하면 이 변환이 띄운 한컴오피스만 닫는다."""
    before = hwp_pids()
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / original.name, Path(tmp) / "out.pdf"
        src.write_bytes(original.read_bytes())
        try:
            run = subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(ROOT / "scripts" / "hwp_to_pdf.ps1"),
                 "-In", str(src), "-Out", str(dst), "-Format", original.suffix.lstrip(".").upper()],
                capture_output=True, text=True, timeout=180)
            ok = run.returncode == 0 and "ok" in run.stdout and dst.exists()
            detail = run.stdout + run.stderr
        except subprocess.TimeoutExpired:
            ok, detail = False, "시간 초과"
        if not ok:
            for pid in hwp_pids() - before:
                subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", f"Stop-Process -Id {pid}"])
            raise RuntimeError(f"PDF 변환 실패: {original}\n{detail}")
        pdf.write_bytes(dst.read_bytes())


def fetch_ftc(doc: dict, out: Path) -> dict:
    """표준약관 게시물의 한글 파일을 받아 PDF로 바꾼다. 버전은 PDF 첫 쪽의 "표준약관 제N호 (날짜 개정)"이다."""
    import pdfplumber

    post = f"{FTC}/selectBbsNttView.do?key=202&bordCd=201&nttSn={doc['post']}"
    href = f"{FTC}/downloadBbsFile.do?atchmnflNo={doc['file']}"
    with httpx.Client(timeout=TIMEOUT_MS / 1000, follow_redirects=True) as c:
        resp = c.get(href)
        resp.raise_for_status()
    fname = disposition_name(resp.headers)
    ext = Path(fname).suffix.lower().lstrip(".")
    if ext not in ("hwp", "hwpx"):
        raise RuntimeError(f"{doc['id']}: 한글 파일이 아니다 ({fname})")
    original = out / f"original.{ext}"
    original.write_bytes(resp.content)
    pdf = out / "body.pdf"
    hwp_to_pdf(original, pdf)
    with pdfplumber.open(pdf) as f:
        m = TERMS_HEADER.search(f.pages[0].extract_text() or "")
    if not m:
        raise RuntimeError(f"{doc['id']}: 첫 쪽에서 표준약관 번호와 개정일을 찾지 못했다")
    return {
        "effective": dt.date(*map(int, re.findall(r"\d+", m.group(2))[:3])).isoformat(),
        "header": " ".join(m.group(0).split()),
        "source_url": post,
        "files": [{"role": "원본", "path": original, "name": fname, "url": href},
                  {"role": "본문", "path": pdf, "name": fname.rsplit(".", 1)[0] + ".pdf", "url": href,
                   "converted": "한컴오피스 PDF 저장(scripts/hwp_to_pdf.ps1)"}],
    }


def pdf_meta(path: Path) -> dict:
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        return {"pages": len(pdf.pages), "producer": str(pdf.metadata.get("Producer", "")),
                "creator": str(pdf.metadata.get("Creator", ""))}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", default="", help="쉼표로 나눈 문서 id")
    ap.add_argument("--force", action="store_true", help="이미 받은 문서도 다시 받는다")
    args = ap.parse_args()

    docs = tomllib.loads(CONFIG.read_text(encoding="utf-8"))["doc"]
    only = {x for x in args.only.split(",") if x}
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {"docs": {}}
    today = dt.date.today().isoformat()
    todo = [d for d in docs if (not only or d["id"] in only) and (args.force or d["id"] not in manifest["docs"])]
    if not todo:
        print("받을 문서가 없다")
        return 0

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(accept_downloads=True)
        # 저장할 때 뜨는 안내(시행 예정 조문 등)는 화면에서 "확인"을 누른 것처럼 받는다. 문구는 기록에 남긴다.
        page.on("dialog", lambda d: (print(f"  안내: {d.message}"), d.accept()))
        for doc in todo:
            shared = doc.get("redistribute", True)
            out = (SHARED if shared else LOCAL) / doc["id"]
            out.mkdir(parents=True, exist_ok=True)
            for old in out.glob("*"):
                old.unlink()
            got = fetch_lawgo(page, doc, out) if doc["site"] in PAGES else fetch_ftc(doc, out)
            prev = manifest["docs"].get(doc["id"])
            if prev and prev.get("header") != got.get("header"):
                raise RuntimeError(f"{doc['id']}: 버전이 바뀌었다\n  전: {prev.get('header')}\n  후: {got.get('header')}")
            files = []
            for f in got.pop("files"):
                entry = {"role": f["role"], "path": f["path"].relative_to(ROOT).as_posix(), "name": f["name"],
                         "url": f["url"], "sha256": sha256(f["path"]), "bytes": f["path"].stat().st_size}
                if f["path"].suffix == ".pdf":
                    entry.update(pdf_meta(f["path"]))
                if "converted" in f:
                    entry["converted"] = f["converted"]
                files.append(entry)
            manifest["docs"][doc["id"]] = {
                "title": doc["title"], "kind": doc["kind"], "site": doc["site"], "redistribute": shared,
                **{k: doc[k] for k in ("seq", "post", "file") if k in doc},
                **got, "fetched": today, "files": files,
            }
            write_manifest(manifest, docs)
            n_byl = sum(f["role"] == "별표" for f in files)
            print(f"{doc['id']}: {got.get('header', '')} | 파일 {len(files)}개(별표 {n_byl})")
        browser.close()
    return 0


def write_manifest(manifest: dict, docs: list[dict]) -> None:
    """문서 하나를 받을 때마다 쓴다(중간에 실패해도 받은 문서의 기록은 남는다). 순서는 설정 파일 순서다."""
    manifest["docs"] = {d["id"]: manifest["docs"][d["id"]] for d in docs if d["id"] in manifest["docs"]}
    SHARED.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    sys.exit(main())
