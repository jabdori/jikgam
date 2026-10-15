"""숫자 출처 검사(ADR 0022 하네스 4의 동적 검사).

테스트 자료 두 벌(tests/web_fixture.py의 variant 0, 1)로 고객사·관리 콘솔의 모든 화면을 렌더링한다. 둘째 벌은 첫째 벌의
건수, 금액, 날짜, 시각, 확률, 지연, 비용을 모두 바꾼 것이다. 화면의 숫자는 fmt를 거쳐 data-num 요소로만 나가므로, 두 벌의
data-num 값이 하나라도 겹치면 자료와 무관하게 늘 같은 숫자(코드에 박힌 숫자)로 본다.

결과 자료가 아닌 종류는 뺀다: index(목록의 몇 번째), rank(순위), axis(재생 띠 눈금), criterion(평가 기준 문서의 사전 기준 값).
"""

import re
from html.parser import HTMLParser

import pytest
from starlette.testclient import TestClient
from web_fixture import build

from jikgam.web import auth
from jikgam.web.app import WebConfig, create_app

EXCLUDED_KINDS = {"index", "rank", "axis", "criterion"}
TENANT = auth.Account("t", "pw", "tenant", "org_t")
ADMIN = auth.Account("a", "pw", "admin")


class _Nums(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if "data-num" in a and a["data-num"] and a.get("data-kind") not in EXCLUDED_KINDS:
            self.values.append((a.get("data-kind") or "", a["data-num"]))


def numbers(html: str) -> list[tuple[str, str]]:
    p = _Nums()
    p.feed(html)
    return p.values


def clients(tmp_path):
    out = []
    for v in (0, 1):
        root = build(tmp_path, v)
        accounts = {"t": TENANT, "a": ADMIN}
        if v:  # 둘째 벌은 조직 유에도 계정이 있어 온보딩 남은 항목이 없다(첫째 벌은 1개)
            accounts["u"] = auth.Account("u", "pw", "tenant", "org_u")
        app = create_app(WebConfig(root=root, accounts=accounts, clock=lambda v=v: 0.0 + 7.3 * v))
        out.append(TestClient(app))
    return out


def first_call_id(html: str) -> str:
    return re.search(r"href=\"/app/calls\?[^\"]*call=([^\"&]+)", html).group(1)


SAAS = ["/app/live", "/app/calls", "/app/report?period=0", "/app/knowledge?doc=terms&seg=T-02", "/app/knowledge?doc=notices",
        "/app/integrations"]
ADMIN_PAGES = ["/admin/models", "/admin/tenants?org=org_u", "/admin/replay?s=rp-C/s20-r1-rp-C&at=2"]


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    return clients(tmp_path_factory.mktemp("numsrc"))


def _get(c: TestClient, path: str, acct: auth.Account) -> str:
    r = c.get(path, headers={"Authorization": auth.basic_header(acct.name, acct.password)})
    assert r.status_code == 200, (path, r.status_code)
    return r.text


@pytest.mark.parametrize("path", SAAS + ADMIN_PAGES)
def test_numbers_follow_the_data(pair, path):
    acct = ADMIN if path.startswith("/admin") else TENANT
    a, b = (numbers(_get(c, path, acct)) for c in pair)
    assert a and b, f"{path}: 숫자가 없다"
    same = {v for _, v in a} & {v for _, v in b}
    assert not same, f"{path}: 자료를 바꿔도 그대로인 숫자 {sorted(same)} — {[(k, v) for k, v in a if v in same][:8]}"


def test_call_detail_and_drawer_numbers_follow_the_data(pair):
    pages = []
    for c in pair:
        listing = _get(c, "/app/calls?period=0&changed=yes", TENANT)
        cid = first_call_id(listing)
        pages.append((_get(c, f"/app/calls?period=0&changed=yes&call={cid}", TENANT), _get(c, f"/app/calls/{cid}", TENANT)))
    for i in range(2):
        a, b = numbers(pages[0][i]), numbers(pages[1][i])
        same = {v for _, v in a} & {v for _, v in b}
        assert a and b and not same, sorted(same)


def test_checker_catches_a_hard_coded_number():
    """검사 자체의 확인: 두 벌이 같은 숫자를 내면 잡는다."""
    html = '<span class="num" data-num="30" data-kind="count">30건</span>'
    assert {v for _, v in numbers(html)} & {v for _, v in numbers(html)} == {"30"}
    assert numbers('<span class="num" data-num="3" data-kind="index">3</span>') == []
