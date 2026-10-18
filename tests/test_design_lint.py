"""디자인 린트(ADR 0022 하네스 1·2·4의 정적 검사). 저장소의 화면 코드가 통과하는지와, 검사가 위반을 잡는지 본다."""

from jikgam.web.ui import lint


def test_repository_passes_design_lint():
    problems = lint.lint()
    assert not problems, "\n".join(str(p) for p in problems)


def rules(problems):
    return sorted({p.rule for p in problems})


def test_css_rules():
    defined = {"--color-text", "--space-4"}
    css = """
    .a { color: #fff; background: rgb(1, 2, 3); }
    .b { margin: 12px; border-color: white; }
    .c { color: var(--color-text); padding: var(--space-4); width: var(--pct); }
    .d { color: var(--nope); }
    .e { --my-token: 3px; }
    /* #abc 12px 주석은 넘어간다 */
    """
    got = lint.lint_css(css, "x.css", defined)
    assert rules(got) == ["color", "px", "token"]
    assert sum(p.rule == "color" for p in got) == 3
    assert any("--nope" in p.detail for p in got) and any("--my-token" in p.detail for p in got)
    assert not any("--pct" in p.detail for p in got)  # 자료에서 오는 비율 변수는 허용


def test_tokens_must_match_between_themes():
    css = ':root { --a: 1; } :root[data-theme="saas"] { --c: #fff; --d: #000; } :root[data-theme="admin"] { --c: #111; }'
    got = lint.lint_tokens(css)
    assert [p.rule for p in got] == ["theme-mismatch"] and "--d" in got[0].detail


def test_js_rules():
    js = """
    el.textContent = "x";
    el.innerHTML = other;
    el.innerHTML = html;
    el.style.color = "red";
    cursor.style.setProperty("--at", v);
    var s = "30건";
    // var t = "12px"; 주석은 넘어간다
    """
    got = lint.lint_js(js, "x.js")
    assert rules(got) == ["digit", "js-html", "js-text", "style"]
    assert sum(p.rule == "js-html" for p in got) == 1


def test_page_code_rules():
    code = '''
from ..ui.html import h
from ..ui import components as ui

def page():
    x = ui.badge("30건")
    y = {"style": "--pct: 3%"}
    z = 12
    w = ui.text("p95와 1회 비용")
    v = f"{q:.2f}"
    return h("div", None, "#ffffff", "8px")
'''
    got = lint.lint_python(code, "saas/x.py", page=True)
    assert rules(got) == ["color", "digit", "low-level-html", "number", "px", "style"]
    assert not any("p95" in p.detail for p in got)  # 지표 이름은 숫자가 아니다
    assert not any(".2f" in p.detail for p in got)  # f-string 형식 지정은 글자가 아니다


def test_components_may_use_style_only_through_helpers():
    code = '''
def bar(p):
    return h("span", {"style": _style_vars(pct=p)})

def bad():
    return h("span", {"style": "width: 3%"})
'''
    got = lint.lint_python(code, "ui/components.py", page=False)
    assert [p.rule for p in got if p.rule == "style"] == ["style"]


def test_repeated_token_blocks_are_merged():
    """움직임 줄이기 설정처럼 @media 안에서 :root를 다시 정의해도 앞 블록의 토큰이 사라지지 않는다."""
    css = """
    :root { --space-4: 8px; --motion-base: 200ms; }
    @media (prefers-reduced-motion: reduce) { :root { --motion-base: 0ms; } }
    :root[data-theme="saas"] { --color-text: #000000; }
    :root[data-theme="admin"] { --color-text: #ffffff; }
    """
    names = lint.token_names(css)
    assert {"--space-4", "--motion-base"} <= names["root"]
    assert not lint.lint_tokens(css)
