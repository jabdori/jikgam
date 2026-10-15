"""고객사 화면 문구 검사(ADR 0022 하네스 3). 금지어 목록은 docs/design/forbidden-terms.md의 표와 설정의 모델 이름이다.

고객사 콘솔의 모든 경로를 테스트 자료(tests/web_fixture.py)로 렌더링해 보이는 글자와 사용자에게 보이는 속성을 검사한다.
"""

import re

import pytest
from starlette.testclient import TestClient
from web_fixture import build

from jikgam.web import auth
from jikgam.web.app import WebConfig, create_app
from jikgam.web.ui import terms as tm

TENANT = auth.Account("t", "pw", "tenant", "org_t")
H = {"Authorization": auth.basic_header("t", "pw")}


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    root = build(tmp_path_factory.mktemp("terms"), 1)
    client = TestClient(create_app(WebConfig(root=root, accounts={"t": TENANT})))
    return client, tm.load_terms(root=root)


def saas_paths(client: TestClient) -> list[str]:
    listing = client.get("/app/calls?period=0", headers=H).text
    ids = sorted(set(re.findall(r"call=([^\"&]+)", listing)))
    paths = ["/app", "/app/live", "/app/calls?period=0", "/app/calls?period=0&changed=yes", "/app/calls?q=고객",
             "/app/report", "/app/report?period=0", "/app/report?period=1", "/app/knowledge", "/app/knowledge?doc=notices",
             "/app/knowledge?q=조항", "/app/integrations"]
    paths += [f"/app/calls?period=0&call={i}" for i in ids] + [f"/app/calls/{i}" for i in ids]
    for outcome in ("filed", "answered", "handoff", "hold", "dropped"):
        paths.append(f"/app/calls?period=0&outcome={outcome}")
    live = client.get("/app/live", headers=H).text
    paths += [f"/app/live?call={f}" for f in sorted(set(re.findall(r"/app/live\?call=([\w-]+)", live)))]
    seg = client.get("/app/knowledge", headers=H).text
    paths += [f"/app/knowledge?doc=terms&seg={s}" for s in sorted(set(re.findall(r"seg=(T-\d+)", seg)))]
    return paths


def test_saas_pages_have_no_engine_terms(setup):
    client, terms = setup
    checked = 0
    for path in saas_paths(client):
        r = client.get(path, headers=H, follow_redirects=True)
        assert r.status_code == 200, (path, r.status_code)
        hits = tm.find(tm.visible_text(r.text), terms)
        assert not hits, f"{path}: 금지어 {hits[:5]}"
        checked += 1
    assert checked > 20


def test_term_list_is_read_from_the_doc_and_config(setup):
    _, terms = setup
    words = {t.word for t in terms}
    assert {"ms", "판별", "확률", "무효화", "임베딩", "STT", "TTS", "Jev", "Gemini", "Qwen"} <= words
    assert {"jev-x", "gem-x", "local-x", "tts-x"} <= words  # 테스트 자료 설정의 모델 이름


@pytest.mark.parametrize("text, hit", [
    ("응답 233ms", "ms"), ("p95 지연", r"p\d{2}"), ("판별 결과", "판별"), ("v6판단", r"(?<![A-Za-z0-9])v\d+(?![A-Za-z0-9])"), ("Jev가 답함", "Jev"),
    ("정책 C로 상담", r"정책\s*[A-D](?![A-Za-z])"), ("비용 $0.0001", r"\$\s?\d"), ("u3 발화", r"(?<![A-Za-z0-9])u\d+(?![A-Za-z0-9])"),
])
def test_checker_finds_terms(setup, text, hit):
    _, terms = setup
    assert hit in [w for w, _ in tm.find(text, terms)]


def test_checker_ignores_hidden_parts_and_word_boundaries(setup):
    _, terms = setup
    html = '<head><title>판별</title></head><body><p>items 목록</p><script>var ms = 1;</script>' \
           '<input type="hidden" value="판별"><span aria-label="확률">x</span></body>'
    hits = [w for w, _ in tm.find(tm.visible_text(html), terms)]
    assert hits == ["확률"]  # 보이는 속성(aria-label)은 검사하고, 제목·스크립트·숨은 값·낱말 안의 ms는 넘어간다
