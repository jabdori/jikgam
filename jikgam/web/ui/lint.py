"""디자인 린트(ADR 0022 하네스 1·2·4의 정적 검사). scripts/lint_design.py와 tests/test_design_lint.py가 부른다.

검사:
- CSS(토큰 파일 밖): 색상값(#hex, rgb() 같은 색 함수, 색 이름), px 값, 정의되지 않은 토큰(var(--…)).
- 토큰 파일: 고객사·관리 테마가 같은 토큰 이름을 정의하는가.
- JS: 색상값, px 값, 숫자가 든 문자열, 글자를 만드는 코드(textContent, innerText, createTextNode), style 대입
  (사용자 정의 속성 --at, --pct를 setProperty로 바꾸는 것만 허용), html 조각이 아닌 값의 innerHTML·outerHTML 대입.
- 화면 코드(saas/, admin/)와 컴포넌트: 문자열 상수의 숫자·색상값·px, style 속성(컴포넌트의 비율 변수만 허용),
  0·1 밖의 숫자 상수(컴포넌트는 100도 허용), 화면 코드의 낮은 수준 HTML 함수(ui.html) 사용.
"""

import ast
import re
from dataclasses import dataclass
from pathlib import Path

WEB = Path(__file__).resolve().parents[1]
STATIC = WEB / "static"
TOKENS = STATIC / "tokens.css"
PAGE_DIRS = (WEB / "saas", WEB / "admin")
COMPONENTS = WEB / "ui" / "components.py"

HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
COLOR_FN = re.compile(r"\b(rgba?|hsla?|hwb|lab|lch|oklab|oklch)\s*\(", re.I)
PX = re.compile(r"(?<![\w-])-?\d*\.?\d+px\b")
VAR_USE = re.compile(r"var\(\s*(--[\w-]+)")
VAR_DEF = re.compile(r"(--[\w-]+)\s*:")
# CSS 색 이름(CSS Color 4의 이름 있는 색). transparent, currentColor는 색값이 아니라 허용한다.
NAMED = set("""aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue blueviolet brown burlywood
cadetblue chartreuse chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkcyan darkgoldenrod darkgray darkgreen
darkgrey darkkhaki darkmagenta darkolivegreen darkorange darkorchid darkred darksalmon darkseagreen darkslateblue darkslategray
darkslategrey darkturquoise darkviolet deeppink deepskyblue dimgray dimgrey dodgerblue firebrick floralwhite forestgreen fuchsia
gainsboro ghostwhite gold goldenrod gray green greenyellow grey honeydew hotpink indianred indigo ivory khaki lavender
lavenderblush lawngreen lemonchiffon lightblue lightcoral lightcyan lightgoldenrodyellow lightgray lightgreen lightgrey lightpink
lightsalmon lightseagreen lightskyblue lightslategray lightslategrey lightsteelblue lightyellow lime limegreen linen magenta maroon
mediumaquamarine mediumblue mediumorchid mediumpurple mediumseagreen mediumslateblue mediumspringgreen mediumturquoise
mediumvioletred midnightblue mintcream mistyrose moccasin navajowhite navy oldlace olive olivedrab orange orangered orchid
palegoldenrod palegreen paleturquoise palevioletred papayawhip peachpuff peru pink plum powderblue purple rebeccapurple red
rosybrown royalblue saddlebrown salmon sandybrown seagreen seashell sienna silver skyblue slateblue slategray slategrey snow
springgreen steelblue tan teal thistle tomato turquoise violet wheat white whitesmoke yellow yellowgreen""".split())
CSS_DECL = re.compile(r"(?<![\w-])([a-z-]+)\s*:\s*([^;{}]+)")
COLOR_PROPS = re.compile(r"(color|background|border|outline|shadow|fill|stroke|caret|accent|decoration)", re.I)
DIGIT = re.compile(r"\d")
STRING_ALLOW = (re.compile(r"^h[1-6]$"),)
# 숫자가 아니라 이름인 표기: 지표 이름(p50, p95, p99), "1회"(한 번당), "1M"(100만 토큰 단가 단위). 숫자 검사 전에 지운다.
LABEL_TOKENS = re.compile(r"(?<![A-Za-z0-9])p(50|95|99)(?![0-9])|1회|1M(?![A-Za-z0-9])")
# 컴포넌트에만 허용하는 구조 표기: tabindex 값, 문자 집합, 화면 폭 메타
COMPONENT_STRINGS = {"0", "-1", "utf-8", "width=device-width, initial-scale=1"}
DATA_VARS = ("--pct", "--at", "--len", "--alpha")
STYLE_VARS = re.compile(r"--(pct|at|len|alpha)")
JS_STRING = re.compile(r"""(?P<q>["'`])(?P<s>(?:\\.|(?!(?P=q)).)*)(?P=q)""", re.S)
JS_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
JS_TEXT = re.compile(r"\.(textContent|innerText)\s*=|createTextNode|insertAdjacentText")
# 재생기의 지금 재생 시각만 JS가 고쳐 쓴다(기록의 수치가 아니라 재생기 상태다, ADR 0022 예외). 그 줄에 이 표시를 둔다
JS_TEXT_EXCEPTION = "ADR 0022 예외: 재생 시각"
JS_HTML = re.compile(r"\.(innerHTML|outerHTML)\s*=(?!=)(?!\s*html\b)")
JS_STYLE = re.compile(r"\.style\b(?!\.setProperty\(\s*[\"']--(at|pct)[\"'])")


@dataclass
class Problem:
    path: str
    line: int
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.rule}] {self.detail}"


def _rel(p: Path) -> str:
    try:
        return p.relative_to(WEB.parents[1]).as_posix()
    except ValueError:
        return p.as_posix()


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _strip_css_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)


def token_names(text: str | None = None) -> dict[str, set[str]]:
    """토큰 파일의 블록별 정의 이름. 키는 "root", "saas", "admin". 같은 블록이 여러 번 나오면(움직임 줄이기 설정의
    @media 안 :root 등) 이름을 합친다."""
    text = _strip_css_comments(TOKENS.read_text(encoding="utf-8") if text is None else text)
    out: dict[str, set[str]] = {}
    for m in re.finditer(r"(:root(?:\[data-theme=\"(\w+)\"\])?)\s*\{(.*?)\}", text, re.S):
        out.setdefault(m.group(2) or "root", set()).update(VAR_DEF.findall(m.group(3)))
    return out


def lint_tokens(text: str | None = None, path: str = "jikgam/web/static/tokens.css") -> list[Problem]:
    names = token_names(text)
    probs = []
    saas, admin = names.get("saas", set()), names.get("admin", set())
    for n in sorted(saas ^ admin):
        probs.append(Problem(path, 1, "theme-mismatch", f"{n}가 한 테마에만 있다"))
    for n in sorted((saas | admin) & names.get("root", set())):
        probs.append(Problem(path, 1, "theme-duplicate", f"{n}가 공통과 테마 양쪽에 있다"))
    return probs


def lint_css(text: str, path: str, defined: set[str]) -> list[Problem]:
    probs = []
    clean = _strip_css_comments(text)
    for m in HEX.finditer(clean):
        probs.append(Problem(path, _line(clean, m.start()), "color", f"색상값 {m.group(0)}"))
    for m in COLOR_FN.finditer(clean):
        probs.append(Problem(path, _line(clean, m.start()), "color", f"색 함수 {m.group(1)}()"))
    for m in PX.finditer(clean):
        probs.append(Problem(path, _line(clean, m.start()), "px", f"px 값 {m.group(0)}"))
    for m in CSS_DECL.finditer(clean):
        prop, value = m.group(1), m.group(2)
        if prop.startswith("--") or not COLOR_PROPS.search(prop):
            continue
        for word in re.findall(r"(?<![\w-])[a-zA-Z]+(?![\w-])", value):
            if word.lower() in NAMED:
                probs.append(Problem(path, _line(clean, m.start()), "color", f"색 이름 {word}"))
    for m in VAR_USE.finditer(clean):
        if m.group(1) not in defined and m.group(1) not in DATA_VARS:
            probs.append(Problem(path, _line(clean, m.start()), "token", f"정의되지 않은 토큰 {m.group(1)}"))
    for m in re.finditer(r"(?<![\w-])(--[\w-]+)\s*:", clean):  # 토큰 파일 밖에서 토큰을 새로 정의하지 않는다
        if m.group(1) not in DATA_VARS:
            probs.append(Problem(path, _line(clean, m.start()), "token", f"토큰 파일 밖의 정의 {m.group(1)}"))
    return probs


def lint_js(text: str, path: str) -> list[Problem]:
    probs = []
    clean = JS_COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    for m in JS_STRING.finditer(clean):
        s = m.group("s")
        ln = _line(clean, m.start())
        if HEX.search(s) or COLOR_FN.search(s):
            probs.append(Problem(path, ln, "color", f"색상값이 든 문자열 {s[:40]!r}"))
        elif PX.search(s):
            probs.append(Problem(path, ln, "px", f"px 값이 든 문자열 {s[:40]!r}"))
        elif DIGIT.search(s):
            probs.append(Problem(path, ln, "digit", f"숫자가 든 문자열 {s[:40]!r}"))
    lines = text.splitlines()
    for rx, rule, msg in ((JS_TEXT, "js-text", "JS가 글자를 만든다"), (JS_HTML, "js-html", "html 조각이 아닌 값을 HTML로 넣는다"),
                          (JS_STYLE, "style", "style 대입(--at, --pct의 setProperty만 허용)")):
        for m in rx.finditer(clean):
            ln = _line(clean, m.start())
            if rule == "js-text" and JS_TEXT_EXCEPTION in lines[ln - 1]:
                continue
            probs.append(Problem(path, ln, rule, msg))
    return probs


def _docstrings(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                ids.add(id(first.value))
    return ids


def lint_python(text: str, path: str, page: bool) -> list[Problem]:
    """page=True는 화면 코드(saas, admin), False는 컴포넌트 모듈."""
    probs = []
    tree = ast.parse(text)
    skip = _docstrings(tree)
    for node in ast.walk(tree):  # f-string의 형식 지정(:.2f 등)은 글자가 아니다
        if isinstance(node, ast.FormattedValue) and node.format_spec is not None:
            for sub in ast.walk(node.format_spec):
                skip.add(id(sub))
    allowed_numbers = {0, 1, -1} if page else {0, 1, -1, 2, 100}  # 컴포넌트: 제목 수준 기본값 2, 백분율 100
    for node in ast.walk(tree):
        ln = getattr(node, "lineno", 1)
        if isinstance(node, ast.Constant) and id(node) not in skip:
            v = node.value
            if isinstance(v, str):
                if HEX.search(v) or COLOR_FN.search(v):
                    probs.append(Problem(path, ln, "color", f"색상값이 든 문자열 {v[:40]!r}"))
                elif PX.search(v):
                    probs.append(Problem(path, ln, "px", f"px 값이 든 문자열 {v[:40]!r}"))
                elif (DIGIT.search(LABEL_TOKENS.sub("", v)) and not any(a.match(v) for a in STRING_ALLOW)
                      and not (not page and v in COMPONENT_STRINGS)):
                    probs.append(Problem(path, ln, "digit", f"숫자가 든 문자열 {v[:40]!r}"))
                if v == "style" and page:
                    probs.append(Problem(path, ln, "style", "화면 코드의 style 속성"))
                if page and STYLE_VARS.search(v):
                    probs.append(Problem(path, ln, "style", "화면 코드의 비율 변수"))
            elif isinstance(v, (int, float)) and not isinstance(v, bool) and v not in allowed_numbers:
                probs.append(Problem(path, ln, "number", f"숫자 상수 {v!r}(settings.py에 이름을 붙여 둔다)"))
        if page and isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.endswith("ui.html") or mod == "html" and node.level:
                probs.append(Problem(path, ln, "low-level-html", "화면 코드가 ui.html을 직접 쓴다(컴포넌트만 쓴다)"))
            if mod.endswith("ui") and any(a.name == "html" for a in node.names):
                probs.append(Problem(path, ln, "low-level-html", "화면 코드가 ui.html을 직접 쓴다(컴포넌트만 쓴다)"))
        if page and isinstance(node, ast.Import) and any(a.name.endswith("ui.html") for a in node.names):
            probs.append(Problem(path, ln, "low-level-html", "화면 코드가 ui.html을 직접 쓴다(컴포넌트만 쓴다)"))
    if not page:
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == "style":
                        ok = isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id in ("_style_vars", "_alpha")
                        if not ok:
                            probs.append(Problem(path, k.lineno, "style", "style은 _style_vars·_alpha로만 만든다"))
    return probs


def lint() -> list[Problem]:
    probs = lint_tokens()
    defined = set().union(*token_names().values())
    for p in sorted(STATIC.glob("*.css")):
        if p != TOKENS:
            probs += lint_css(p.read_text(encoding="utf-8"), _rel(p), defined)
    for p in sorted(STATIC.glob("*.js")):
        probs += lint_js(p.read_text(encoding="utf-8"), _rel(p))
    for p in sorted(STATIC.glob("*.html")):
        probs.append(Problem(_rel(p), 1, "static-html", "화면은 서버가 컴포넌트로 만든다(정적 HTML을 두지 않는다)"))
    for d in PAGE_DIRS:
        for p in sorted(d.glob("*.py")):
            probs += lint_python(p.read_text(encoding="utf-8"), _rel(p), page=True)
    probs += lint_python(COMPONENTS.read_text(encoding="utf-8"), _rel(COMPONENTS), page=False)
    return probs
