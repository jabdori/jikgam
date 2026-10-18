"""디자인 하네스의 브라우저 검사(ADR 0022 5·6과 3의 브라우저 쪽).

실제 결과 파일로 웹 서버를 띄우고 Chromium(Playwright)으로 화면마다 1440px와 390px 폭을 연다.

- 스크린샷: docs/design/screens/<화면>-<폭>.png. 찍기 전 파일과 화소를 비교해 바뀐 비율을 changes.md에 적는다.
- 접근성: axe-core로 심각(serious) 이상 위반 0건이어야 한다.
- 키보드: 화면마다 정한 주요 흐름을 키보드만으로 해 본다(1440px에서).
- 금지어: 고객사 화면은 스크립트가 돈 뒤의 글자와 보이는 속성을 docs/design/forbidden-terms.md로 검사한다.

실시간 통화는 재생 피드의 시계를 고정해 찍는다. 미공개 시나리오 실행은 열지 않는다(--allow-holdout을 쓰지 않는다).
하나라도 실패하면 종료 코드가 1이다.

    uv run python scripts/ui_check.py                 # 모든 화면
    uv run python scripts/ui_check.py --only saas-calls,admin-replay
    uv run python scripts/ui_check.py --no-shots      # 스크린샷 파일을 바꾸지 않고 검사만
"""

import argparse
import importlib.util
import io
import runpy
import socket
import sys
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import uvicorn
from PIL import Image, ImageChops
from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jikgam.web import auth  # noqa: E402
from jikgam.web.app import WebConfig, create_app  # noqa: E402
from jikgam.web.data.tenants import calls_of, load_tenants  # noqa: E402
from jikgam.web.ui import terms as tm  # noqa: E402

SCREENS_DIR = ROOT / "docs/design/screens"
WIDTHS = (1440, 390)
HEIGHT = {1440: 1000, 390: 844}
SERIOUS = ("serious", "critical")
# axe-core는 axe-playwright-python 꾸러미에 든 axe.min.js(4.12.1)를 쓴다. 그 꾸러미의 불러오기 코드는 이 PC의 기본
# 인코딩(cp949)으로 파일을 읽다가 실패해서, 파일만 UTF-8로 직접 읽어 넣는다.
AXE_JS = (Path(importlib.util.find_spec("axe_playwright_python").submodule_search_locations[0]) / "axe.min.js")     .read_text(encoding="utf-8")


def run_axe(page: Page) -> dict:
    page.add_script_tag(content=AXE_JS)
    return page.evaluate("() => axe.run(document, {resultTypes: ['violations']})")


@dataclass
class Screen:
    name: str
    console: str          # saas, admin
    title: str
    path: Callable[[], str]
    keys: Callable[[Page, str], None] | None = None
    prepare: Callable[[Page], None] | None = None
    fixture: Callable | None = None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pick_call(org: str = "org_a") -> str:
    """스크린샷에 쓸 통화: 개발용 실행의 음성 통화 중 고객이 요청을 바꾼 것(없으면 첫 통화)."""
    t = load_tenants(ROOT)[org]
    calls = calls_of(ROOT, t)
    voiced = [c for c in calls if c.channel == "voice" and c.changed and c.recording and c.recording.get("available")]
    best = [c for c in voiced if c.verified and c.outcome == "filed"]
    return (best or voiced or [c for c in calls if c.changed] or calls)[0].id


# --- 키보드 흐름 -------------------------------------------------------------------------------------


def tab_until(page: Page, selector: str, limit: int = 80) -> None:
    for _ in range(limit):
        page.keyboard.press("Tab")
        if page.evaluate("(s) => !!document.activeElement && document.activeElement.matches(s)", selector):
            return
    raise AssertionError(f"Tab {limit}번 안에 {selector}에 닿지 않는다")


def press_and_wait(page: Page, key: str, until: str = "networkidle") -> None:
    """링크·폼에서 키를 눌러 다음 문서가 뜰 때까지 기다린다."""
    with page.expect_navigation(wait_until=until):
        page.keyboard.press(key)


def ready_state(path: str) -> str:
    """문서가 뜬 것으로 볼 때. 실시간 통화는 짧은 간격으로 계속 갱신해 네트워크가 쉬지 않으므로 문서 로드까지만 기다린다."""
    return "load" if path.startswith("/app/live") else "networkidle"


def press_and_wait_swap(page: Page, key: str) -> None:
    """같은 화면 안의 이동(문서를 새로 열지 않고 영역만 바꾸고 주소만 바꾼다)에서 키를 눌러 주소가 바뀔 때까지 기다린다."""
    before = page.url
    page.keyboard.press(key)
    page.wait_for_function("u => window.location.href !== u", arg=before)
    page.wait_for_load_state("networkidle")


def focus_visible(page: Page) -> None:
    ok = page.evaluate("""() => { const el = document.activeElement; if (!el) return false;
        const s = getComputedStyle(el); return s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0; }""")
    if not ok:
        raise AssertionError("포커스가 보이지 않는다(outline 없음)")


def keys_skip(page: Page) -> None:
    page.keyboard.press("Tab")
    if not page.evaluate("() => document.activeElement.classList.contains('skip')"):
        raise AssertionError("첫 Tab이 본문 건너뛰기 링크가 아니다")
    focus_visible(page)
    page.keyboard.press("Enter")
    if page.evaluate("() => document.activeElement.id") != "main":
        raise AssertionError("본문 건너뛰기 뒤 포커스가 본문이 아니다")


def keys_calls(page: Page, base: str) -> None:
    page.goto(base + "/app/calls", wait_until="networkidle")
    keys_skip(page)
    tab_until(page, "a.row-link")
    focus_visible(page)
    press_and_wait(page, "Enter")
    if "call=" not in page.url:
        raise AssertionError("줄에서 Enter를 눌러도 상세가 열리지 않는다")
    if not page.evaluate("() => document.activeElement.hasAttribute('data-autofocus')"):
        raise AssertionError("상세가 열린 뒤 포커스가 상세 제목이 아니다")
    tab_until(page, ".drawer-footer a")
    press_and_wait(page, "Escape")
    if page.query_selector("[data-drawer]") is not None:
        raise AssertionError("Escape로 상세가 닫히지 않는다")


def keys_call(page: Page, base: str) -> None:
    page.goto(base + f"/app/calls/{pick_call()}", wait_until="networkidle")
    keys_skip(page)
    tab_until(page, "audio")
    page.keyboard.press("Space")
    page.wait_for_timeout(1500)
    if page.evaluate("() => document.querySelector('audio').paused"):
        raise AssertionError("녹음 재생기에서 Space로 재생되지 않는다")
    page.evaluate("() => document.querySelector('audio').pause()")


def keys_live(page: Page, base: str) -> None:
    page.goto(base + "/app/live", wait_until=ready_state("/app/live"))
    keys_skip(page)
    tab_until(page, ".item-list a.item:not([aria-current])")
    focus_visible(page)
    press_and_wait(page, "Enter", until=ready_state("/app/live"))
    if "call=" not in page.url:
        raise AssertionError("다른 통화를 Enter로 고를 수 없다")
    tab_until(page, "button[data-listen]")
    page.keyboard.press("Enter")
    page.wait_for_timeout(1500)
    if page.evaluate("() => document.getElementById('live-audio').paused"):
        raise AssertionError("통화 듣기를 Enter로 켤 수 없다")
    page.keyboard.press("Enter")
    tab_until(page, "form button[type=submit]")
    focus_visible(page)


def keys_report(page: Page, base: str) -> None:
    page.goto(base + "/app/report", wait_until="networkidle")
    keys_skip(page)
    tab_until(page, ".chips-segmented a.chip:not([aria-current])")
    focus_visible(page)
    press_and_wait(page, "Enter")
    if "period=" not in page.url:
        raise AssertionError("기간을 키보드로 바꿀 수 없다")


def keys_knowledge(page: Page, base: str) -> None:
    page.goto(base + "/app/knowledge?doc=ecom-act", wait_until="networkidle")
    keys_skip(page)
    tab_until(page, "a.btn[href*='seg=']:not([aria-disabled])")
    focus_visible(page)
    press_and_wait(page, "Enter")
    if "seg=" not in page.url:
        raise AssertionError("다음 구간으로 키보드로 갈 수 없다")
    page.focus("#q")
    page.keyboard.type("소비자")
    press_and_wait(page, "Enter")
    tab_until(page, "#segment-chooser summary")
    page.keyboard.press("Enter")
    tab_until(page, ".item-list a.item")
    press_and_wait(page, "Enter")
    if "seg=" not in page.url:
        raise AssertionError("검색 결과에서 구간을 키보드로 열 수 없다")


def keys_integrations(page: Page, base: str) -> None:
    page.goto(base + "/app/integrations", wait_until="networkidle")
    keys_skip(page)
    tab_until(page, "a.link[href*='seg=']")
    focus_visible(page)
    press_and_wait(page, "Enter")
    if "seg=" not in page.url:
        raise AssertionError("상담사 연결 조건의 약관 원문을 키보드로 열 수 없다")


def prepare_published_documents(page: Page) -> None:
    page.locator("#published-documents summary").click()
    clipped = page.evaluate("""() => {
        const card = document.querySelector('#published-documents .disclosure-body');
        const bounds = card.getBoundingClientRect();
        return Array.from(card.querySelectorAll('.row-link, .badge')).some(el => {
            const rect = el.getBoundingClientRect();
            return rect.left < bounds.left || rect.right > bounds.right || el.scrollWidth > el.clientWidth + 1;
        });
    }""")
    if clipped:
        raise AssertionError("게시 문서명 또는 상태 배지가 카드에서 잘린다")


def keys_replay(page: Page, base: str) -> None:
    page.goto(base + "/admin/replay", wait_until="networkidle")
    keys_skip(page)
    tab_until(page, "a.btn[href*='at=']")
    focus_visible(page)
    before = page.url
    press_and_wait_swap(page, "Enter")
    if page.url == before or "at=" not in page.url:
        raise AssertionError("이전/다음 판단으로 키보드로 갈 수 없다")
    if page.evaluate("() => !document.activeElement || document.activeElement === document.body"):
        raise AssertionError("판단을 옮긴 뒤 포커스를 잃는다")
    tab_until(page, "button[data-play]")
    page.keyboard.press("Enter")
    page.wait_for_timeout(1500)
    if page.evaluate("() => document.querySelector('[data-player] audio').paused"):
        raise AssertionError("재생 버튼을 Enter로 누를 수 없다")
    page.keyboard.press("Enter")
    tab_until(page, "input[name=stop]")
    page.keyboard.press("Space")
    if page.evaluate("() => document.querySelector('input[name=stop]').checked"):
        raise AssertionError("판단 시점마다 멈춤을 Space로 끌 수 없다")
    tab_until(page, ".playlist a.play-item")
    focus_visible(page)
    press_and_wait(page, "Enter")
    if "at=" not in page.url:
        raise AssertionError("재생 목록의 판단을 키보드로 열 수 없다")


def keys_models(page: Page, base: str) -> None:
    page.goto(base + "/admin/models?view=other", wait_until="networkidle")
    keys_skip(page)
    tab_until(page, "#f-name")
    focus_visible(page)
    tab_until(page, "select[name=model]")
    tab_until(page, "form[action='/admin/benchmarks'] button[type=submit]")
    press_and_wait(page, "Enter")
    if page.query_selector("form[aria-label='실행 확인']") is None:
        raise AssertionError("예상 비용 확인을 키보드로 열 수 없다")
    tab_until(page, "form[aria-label='실행 확인'] a.btn")
    press_and_wait(page, "Enter")


def keys_tenants(page: Page, base: str) -> None:
    page.goto(base + "/admin/tenants", wait_until="networkidle")
    keys_skip(page)
    tab_until(page, ".item-list a.item:not([aria-current])")
    focus_visible(page)
    press_and_wait(page, "Enter")
    if "org=" not in page.url:
        raise AssertionError("다른 고객사를 키보드로 고를 수 없다")


def simulated_val_results():
    """가상 실행은 해당 화면 촬영 중에만 임시 폴더에서 읽는다."""
    fixture = runpy.run_path(str(ROOT / "tests/web_retrieval_fixture.py"))
    return fixture["simulated_job"]()


def check_simulated_val_label(page: Page) -> None:
    """두 폭에서 가상 수치와 실측 아님 안내가 같은 행에 펼쳐져 있어야 한다."""
    row = page.get_by_role("table").filter(has=page.get_by_role("rowheader", name="가상 실행", exact=False)) \
        .get_by_role("row").filter(has=page.get_by_role("rowheader", name="가상 실행", exact=False))
    if row.count() != 1 or "77.0%" not in row.inner_text():
        raise AssertionError("보이는 val 표에서 가상 실행 수치 행을 찾을 수 없다")
    label = row.get_by_text("화면 검증 자료(실측 아님)", exact=True)
    if not label.is_visible() or label.locator("xpath=ancestor::details").count():
        raise AssertionError("가상 실행 수치 행의 실측 아님 안내가 펼쳐져 보이지 않는다")


SCREENS = [
    Screen("saas-live", "saas", "실시간 통화", lambda: "/app/live", keys_live),
    Screen("saas-calls", "saas", "통화 기록", lambda: f"/app/calls?call={pick_call()}", keys_calls),
    Screen("saas-call", "saas", "통화 다시 듣기", lambda: f"/app/calls/{pick_call()}", keys_call),
    Screen("saas-report", "saas", "운영 리포트", lambda: "/app/report", keys_report),
    Screen("saas-knowledge", "saas", "지식·정책", lambda: "/app/knowledge", keys_knowledge),
    Screen("saas-knowledge-law", "saas", "지식·정책(법령 원문과 구간 정보)",
           lambda: "/app/knowledge?doc=ecom-act", lambda page, base: (
               page.goto(base + "/app/knowledge?doc=ecom-act", wait_until="networkidle"), keys_skip(page),
               tab_until(page, "a.btn[href*='seg=']:not([aria-disabled])"), focus_visible(page),
               press_and_wait(page, "Enter"))),
    Screen("saas-knowledge-progress", "saas", "지식·정책(고정 자료 · 진행 중)",
           lambda: "/app/knowledge?doc=support-refund", lambda page, base: (
               page.goto(base + "/app/knowledge?doc=support-refund", wait_until="networkidle"), keys_skip(page),
               tab_until(page, "a.btn[href*='upload=']"), focus_visible(page), press_and_wait(page, "Enter"),
               tab_until(page, "input[type=file]"), focus_visible(page))),
    Screen("saas-knowledge-complete", "saas", "지식·정책(고정 자료 · 완료)",
           lambda: "/app/knowledge?doc=support-retention", lambda page, base: (
               page.goto(base + "/app/knowledge?doc=support-retention", wait_until="networkidle"), keys_skip(page),
               tab_until(page, "#document-stages summary"), focus_visible(page), page.keyboard.press("Enter")),
           lambda page: page.locator("#document-stages summary").click()),
    Screen("saas-knowledge-published", "saas", "지식·정책(게시 설정 펼침)",
           lambda: "/app/knowledge?doc=ecom-act", lambda page, base: (
               page.goto(base + "/app/knowledge?doc=ecom-act", wait_until="networkidle"), keys_skip(page),
               tab_until(page, "#published-documents summary"), focus_visible(page), page.keyboard.press("Enter")),
           prepare_published_documents),
    Screen("saas-integrations", "saas", "연동", lambda: "/app/integrations", keys_integrations),
    Screen("admin-models", "admin", "모델·벤치마크", lambda: "/admin/models", keys_models),
    Screen("admin-models-simulated-val", "admin", "모델·벤치마크(가상 val 실행 결과)",
           lambda: "/admin/models", prepare=check_simulated_val_label, fixture=simulated_val_results),
    Screen("admin-retrieval-confirm", "admin", "검색 벤치마크 비용 확인",
           lambda: "/admin/models?retrieval=jev", lambda page, base: (
               page.goto(base + "/admin/models?retrieval=jev", wait_until="networkidle"), keys_skip(page),
               tab_until(page, "form[aria-label='검색 실행 확인'] a.btn"), focus_visible(page), press_and_wait(page, "Enter"))),
    Screen("admin-tenants", "admin", "고객사 관리", lambda: "/admin/tenants?org=org_b", keys_tenants),
    Screen("admin-replay", "admin", "통화 리플레이(s20 음성 2회차)", lambda: "/admin/replay", keys_replay),
    Screen("admin-replay-barge", "admin", "통화 리플레이(1단계 끼어들기 재생)",
           lambda: "/admin/replay?s=voice-barge-C/s20-C-r1-voice-voice-barge-C", None),
]


# --- 검사 -------------------------------------------------------------------------------------------


def visible_dump(page: Page) -> str:
    return page.evaluate("""() => {
        const parts = [document.body.innerText];
        for (const el of document.querySelectorAll('[title],[aria-label],[placeholder],[alt],input[value]')) {
            for (const a of ['title', 'aria-label', 'placeholder', 'alt']) if (el.getAttribute(a)) parts.push(el.getAttribute(a));
            if (el.tagName === 'INPUT' && el.type !== 'hidden' && el.value) parts.push(el.value);
        }
        return parts.join('\\n'); }""")


def diff_ratio(old: bytes | None, new: bytes) -> float | None:
    if old is None:
        return None
    a, b = Image.open(io.BytesIO(old)).convert("RGB"), Image.open(io.BytesIO(new)).convert("RGB")
    if a.size != b.size:
        return 1.0
    bbox = ImageChops.difference(a, b).getbbox()
    if bbox is None:
        return 0.0
    hist = ImageChops.difference(a, b).convert("L").histogram()
    return (a.size[0] * a.size[1] - hist[0]) / (a.size[0] * a.size[1])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", help="쉼표로 구분한 화면 이름")
    ap.add_argument("--no-shots", action="store_true", help="스크린샷 파일을 바꾸지 않는다")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    screens = [s for s in SCREENS if not args.only or s.name in args.only.split(",")]

    accounts = auth.generate_accounts(list(load_tenants(ROOT)))
    tenant = next(a for a in accounts.values() if a.org == "org_a")
    admin = next(a for a in accounts.values() if a.role == "admin")
    fixture = runpy.run_path(str(ROOT / "tests/web_knowledge_fixture.py"))
    cfg = WebConfig(root=ROOT, accounts=accounts, clock=lambda: 0.0, knowledge_documents=fixture["documents"])
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(cfg), host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    while not server.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    terms = tm.load_terms()
    SCREENS_DIR.mkdir(parents=True, exist_ok=True)
    rows, failures = [], []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for s in screens:
                with s.fixture() if s.fixture else nullcontext():
                    acct = tenant if s.console == "saas" else admin
                    for w in WIDTHS:
                        ctx = browser.new_context(viewport={"width": w, "height": HEIGHT[w]}, device_scale_factor=1,
                                                  http_credentials={"username": acct.name, "password": acct.password},
                                                  reduced_motion="reduce", locale="ko-KR")
                        page = ctx.new_page()
                        resp = page.goto(base + s.path(), wait_until=ready_state(s.path()))
                        if resp is None or resp.status != 200:
                            failures.append(f"{s.name} {w}px: 응답 {resp.status if resp else '없음'}")
                            ctx.close()
                            continue
                        page.wait_for_timeout(300)
                        if s.prepare:
                            s.prepare(page)
                        shot = page.screenshot(full_page=True, animations="disabled")
                        out = SCREENS_DIR / f"{s.name}-{w}.png"
                        ratio = diff_ratio(out.read_bytes() if out.exists() else None, shot)
                        if not args.no_shots:
                            out.write_bytes(shot)
                        axe = run_axe(page)
                        bad = [v for v in axe["violations"] if v.get("impact") in SERIOUS]
                        for v in bad:
                            nodes = "; ".join(n["target"][0] if isinstance(n["target"][0], str) else str(n["target"][0])
                                              for n in v["nodes"][:3])
                            failures.append(f"{s.name} {w}px axe {v['impact']} {v['id']}: {v['help']} ({nodes})")
                        hits = tm.find(visible_dump(page), terms) if s.console == "saas" else []
                        for word, near in hits[:5]:
                            failures.append(f"{s.name} {w}px 금지어 {word}: …{near}…")
                        page_width = page.evaluate("() => document.documentElement.scrollWidth")
                        if page_width > w:
                            failures.append(f"{s.name} {w}px: 가로로 넘친다(문서 폭 {page_width}px)")
                        kb = "-"
                        if s.keys is not None and w == WIDTHS[0]:
                            try:
                                s.keys(page, base)
                                kb = "통과"
                            except AssertionError as e:
                                kb = "실패"
                                failures.append(f"{s.name} 키보드: {e}")
                        rows.append((s.name, s.title, w, ratio, len(bad), len(hits), kb))
                        print(f"{s.name:18} {w:5}px  바뀐 화소 {('-' if ratio is None else f'{ratio:.2%}'):>7}  "
                              f"axe 심각 {len(bad)}  금지어 {len(hits)}  키보드 {kb}")
                        ctx.close()
            browser.close()
    finally:
        server.should_exit = True
        th.join(timeout=5)

    if not args.no_shots:
        lines = ["# 스크린샷 변경 기록", "", "`scripts/ui_check.py`가 마지막으로 찍은 결과다. 바뀐 화소는 직전 스크린샷과 비교한 비율이다"
                 "(처음 찍은 화면은 -, 크기가 바뀌면 100%).", "",
                 f"찍은 시각: {time.strftime('%Y-%m-%d %H:%M')}", "",
                 "| 화면 | 이름 | 폭 | 바뀐 화소 | axe 심각 이상 | 금지어 | 키보드 흐름 |", "|---|---|---|---|---|---|---|"]
        for name, title, w, ratio, bad, hits, kb in rows:
            lines.append(f"| {name} | {title} | {w}px | {'-' if ratio is None else f'{ratio:.2%}'} | {bad} | "
                         f"{hits if name.startswith('saas') else '해당 없음'} | {kb} |")
        (SCREENS_DIR / "changes.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    if failures:
        print("\n실패:")
        for f in failures:
            print(" -", f)
        raise SystemExit(1)
    print("\n모두 통과")


if __name__ == "__main__":
    main()
