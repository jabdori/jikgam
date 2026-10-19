"""공용 컴포넌트. 화면 코드(jikgam/web/saas, jikgam/web/admin)는 이 모듈과 fmt만 써서 화면을 조합한다(ADR 0022).

기본 컴포넌트는 셸, 카드, 표, 배지, 지표 칸, 빈 상태, 드로어다. 그 밖에 더한 컴포넌트와 더한 이유는
docs/design/components.md에 적는다. 모양은 static/components.css가 정하고, 색·크기는 토큰만 쓴다.

글자 인자는 str(이스케이프한다)이나 Markup(fmt 결과, 다른 컴포넌트 결과)이다. 숫자는 fmt를 거쳐 넣는다.
동작(드로어 닫기, 재생과 자막 맞추기, 실시간 갱신)은 static/console.js가 data-* 속성을 보고 붙인다. JS는 글자를 만들지 않는다.
"""

import re
from dataclasses import dataclass, field
from html import unescape as html_unescape
from typing import Any, Sequence

from .. import settings
from .html import Markup, classes, h, join

Node = Any
TONES = ("neutral", "muted", "accent", "positive", "caution", "negative")


def _tone(tone: str) -> str:
    if tone not in TONES:
        raise ValueError(f"알 수 없는 tone: {tone}")
    return tone


def _style_vars(**pct: float | None) -> str | None:
    """자료에서 오는 비율·위치만 style 사용자 정의 속성으로 넘긴다(ADR 0022의 유일한 style 예외)."""
    parts = [f"--{k}: {max(0.0, min(100.0, v)):.2f}%" for k, v in pct.items() if v is not None]
    return "; ".join(parts) or None


def _alpha(a: float) -> str:
    return f"--alpha: {max(0.0, min(1.0, a)):.2f}"


# --- 배치 -------------------------------------------------------------------------------------------


def stack(*children: Node, gap: str = "md", label: str | None = None) -> Markup:
    """세로로 쌓는다. gap: sm, md, lg."""
    return h("div", {"class": f"stack gap-{gap}", "aria-label": label, "role": "group" if label else None}, *children)


def row(*children: Node, gap: str = "md", align: str | None = None, wrap: bool = True) -> Markup:
    """가로로 놓는다. 좁으면 줄을 바꾼다."""
    return h("div", {"class": classes("row", f"gap-{gap}", f"align-{align}" if align else None,
                                      None if wrap else "nowrap")}, *children)


def grid(*children: Node, min: str = "card") -> Markup:
    """같은 폭 칸을 채워 놓는다. min: card(지표 칸), wide(카드), panel(큰 카드), half(넓으면 두 칸)."""
    return h("div", {"class": f"grid grid-{min}"}, *children)


def columns(*parts: tuple[str, Node]) -> Markup:
    """화면 본문을 세로 영역 여러 개로 나눈다. parts는 (종류, 내용). 종류: list(왼쪽 목록), list-wide(넓은 왼쪽 목록, 좁은
    화면에서는 본문 뒤로 간다), main, aside(오른쪽 정보), drawer(오른쪽 상세). 좁은 화면에서는 위에서 아래로 쌓인다."""
    return h("div", {"class": "columns"}, *[h("div", {"class": f"col col-{kind}"}, body) for kind, body in parts])


def split(main: Node, aside: Node, aside_first: bool = True) -> Markup:
    """본문 영역 안을 넓은 왼쪽과 좁은 오른쪽으로 나눈다(통화 다시 듣기의 대화와 요약). 좁으면 쌓이고, aside_first면
    오른쪽(요약 등)이 먼저 온다. 오른쪽이 입력 폼처럼 나중에 볼 것이면 aside_first=False."""
    return h("div", {"class": classes("split", None if aside_first else "keep-order")},
             h("div", {"class": "split-main"}, main), h("div", {"class": "split-aside"}, aside))


def anchor(name: str, *children: Node) -> Markup:
    """주소의 #이름으로 바로 갈 수 있는 묶음."""
    return h("div", {"id": name, "class": "anchor"}, *children)


def spacer() -> Markup:
    """row 안에서 뒤 요소를 오른쪽 끝으로 민다."""
    return h("span", {"class": "spacer", "aria-hidden": "true"})


# --- 글자 -------------------------------------------------------------------------------------------


def text(t: Node, tone: str | None = None, size: str | None = None, mono: bool = False, strong: bool = False,
         strike: bool = False) -> Markup:
    """한 줄 글자. tone: muted, faint, accent, positive, caution, negative. size: sm, md, lg."""
    return h("span", {"class": classes("t", f"t-{tone}" if tone else None, f"t-{size}" if size else None,
                                       "t-mono" if mono else None, "t-strong" if strong else None,
                                       "t-strike" if strike else None)}, t)


def para(t: Node, tone: str | None = None) -> Markup:
    return h("p", {"class": classes("para", f"t-{tone}" if tone else None)}, t)


def code(t: str) -> Markup:
    """파일 경로, 도구 이름 같은 고정 폭 글자."""
    return h("code", {"class": "code"}, t)


def link(t: Node, href: str, current: bool = False) -> Markup:
    return h("a", {"class": "link", "href": href, "aria-current": "page" if current else None}, t)


HEADINGS = ("h1", "h2", "h3", "h4")


def heading(t: Node, level: int = 2, sub: Node = None, side: Node = None) -> Markup:
    """카드 안 제목. sub는 제목 옆의 짧은 설명, side는 오른쪽 끝 글자."""
    tag = HEADINGS[level - 1]
    return h("div", {"class": "heading"},
             h(tag, {"class": f"heading-title level-{level}"}, t),
             h("span", {"class": "heading-sub"}, sub) if sub is not None else None,
             h("span", {"class": "heading-side"}, side) if side is not None else None)


def subheading(t: Node, sub: Node = None, side: Node = None) -> Markup:
    """카드·드로어 안의 작은 제목(h3)."""
    return heading(t, HEADINGS.index("h3") + 1, sub, side)


def notice(t: Node, tone: str = "neutral") -> Markup:
    """눈에 띄게 둘 짧은 안내."""
    return h("div", {"class": f"notice tone-{_tone(tone)}"}, t)


def sr_only(t: Node) -> Markup:
    return h("span", {"class": "sr-only"}, t)


# --- 배지, 지표 칸, 빈 상태 ---------------------------------------------------------------------------


def badge(t: Node, tone: str = "neutral", dot: bool = False) -> Markup:
    return h("span", {"class": f"badge tone-{_tone(tone)}"},
             h("span", {"class": "badge-dot", "aria-hidden": "true"}) if dot else None, t)


def metric(label: Node, value: Node, sub: Node = None) -> Markup:
    return h("div", {"class": "metric"},
             h("div", {"class": "metric-label"}, label),
             h("div", {"class": "metric-value"}, value),
             h("div", {"class": "metric-sub"}, sub) if sub is not None else None)


def empty(t: Node, hint: Node = None) -> Markup:
    return h("div", {"class": "empty", "role": "status"},
             h("div", {"class": "empty-text"}, t),
             h("div", {"class": "empty-hint"}, hint) if hint is not None else None)


# --- 버튼, 칩 ----------------------------------------------------------------------------------------


def button(t: Node, href: str | None = None, kind: str = "secondary", type: str = "button", disabled: bool = False,
           size: str | None = None, label: str | None = None, pressed: bool | None = None, **data: str) -> Markup:
    """kind: primary(진한 배경), secondary(테두리), ghost(배경 없음). href가 있으면 링크로 만든다.
    pressed는 켜고 끄는 버튼의 상태(aria-pressed), data는 data-* 속성(JS 동작)으로 붙는다."""
    a = {"class": classes("btn", f"btn-{kind}", f"btn-{size}" if size else None), "aria-label": label,
         "aria-pressed": None if pressed is None else ("true" if pressed else "false"),
         **{f"data-{k.replace('_', '-')}": v for k, v in data.items()}}
    if href is not None and not disabled:
        return h("a", {**a, "href": href}, t)
    return h("button", {**a, "type": type, "disabled": disabled, "aria-disabled": "true" if disabled else None}, t)


@dataclass
class Chip:
    label: Node
    href: str
    count: Node = None
    active: bool = False


def chips(items: Sequence[Chip], label: str, kind: str = "pills") -> Markup:
    """필터 고르기. kind: pills(떨어진 알약), segmented(붙은 묶음). 고른 것은 aria-current로 표시한다."""
    return h("nav", {"class": f"chips chips-{kind}", "aria-label": label},
             *[h("a", {"class": "chip", "href": c.href, "aria-current": "true" if c.active else None},
                 c.label, h("span", {"class": "chip-count"}, c.count) if c.count is not None else None)
               for c in items])


# --- 카드 --------------------------------------------------------------------------------------------


def card(*body: Node, title: Node = None, label: str | None = None, sub: Node = None, side: Node = None,
         actions: Node = None, flush: bool = False, tone: str | None = None, level: int = 2) -> Markup:
    """테두리 있는 묶음. title이 있으면 제목 줄을 둔다. flush면 안쪽 여백을 빼고(표를 꽉 채울 때) 제목 줄만 여백을 둔다.
    label은 제목이 없을 때 화면 읽기 프로그램에 줄 이름이다."""
    head = None
    if title is not None or actions is not None:
        head = h("div", {"class": "card-head"}, heading(title, level, sub, side) if title is not None else None,
                 h("div", {"class": "card-actions"}, actions) if actions is not None else None)
    return h("section", {"class": classes("card", "card-flush" if flush else None, f"tone-{tone}" if tone else None),
                         "aria-label": label}, head, *body)


def pane_head(title: Node, sub: Node = None, demoted: bool = False) -> Markup:
    """세로 목록 영역(columns의 list) 맨 위 제목. 화면의 h1이 다른 곳에 있으면 demoted로 h2로 낮춘다."""
    return h("div", {"class": "pane-head"}, h("h2" if demoted else "h1", {"class": "pane-title"}, title),
             h("div", {"class": "pane-sub"}, sub) if sub is not None else None)


def page_header(title: Node, sub: Node = None, actions: Node = None, demoted: bool = False) -> Markup:
    """화면 맨 위 제목 줄. 화면의 h1이 다른 곳에 있으면 demoted로 h2로 낮춘다(모양은 같다)."""
    return h("header", {"class": "page-header"},
             h("div", {"class": "page-header-text"}, h("h2" if demoted else "h1", {"class": "page-title"}, title),
               h("div", {"class": "page-sub"}, sub) if sub is not None else None),
             h("div", {"class": "page-actions"}, actions) if actions is not None else None)


# --- 표 ----------------------------------------------------------------------------------------------


@dataclass
class Col:
    label: Node
    num: bool = False       # 숫자 칸(오른쪽 정렬, 고정 폭 숫자)
    mono: bool = False      # 코드 글자 칸
    hide_narrow: bool = False  # 좁은 화면에서 숨김
    key: bool = False       # 항목 이름 칸(fixed 표에서 고정 폭)


@dataclass
class Row:
    cells: list[Node]
    href: str | None = None   # 줄 전체를 그 주소로 가는 링크로(첫 칸에 링크를 둔다)
    selected: bool = False
    tone: str | None = None
    key: str | None = None    # 줄 이름. 없으면 href. 갱신 때 같은 줄을 그대로 두고 새 줄만 붙인다


def _plain(node: Node) -> str:
    """열 이름을 data-label 속성에 넣을 일반 글자로. 컴포넌트가 만든 HTML(Markup)이면 태그를 지운다."""
    if isinstance(node, Markup) or not isinstance(node, str):
        return html_unescape(re.sub(r"<[^>]+>", "", str(node)))
    return node


def table(cols: Sequence[Col], rows: Sequence[Row], caption: str, wide: bool = False, empty_text: str | None = None,
          row_header: bool = True, stack: bool = False, fixed: bool = False) -> Markup:
    """첫 칸은 행 제목(th scope=row)이다. wide면 최소 폭을 두고 좁은 화면에서 가로로 밀어 본다. stack이면 좁은 화면에서
    줄마다 칸을 세로로 쌓은 카드로 보인다(칸 이름은 data-label로 붙는다)."""
    if not rows and empty_text is not None:
        return empty(empty_text)
    head = h("thead", None, h("tr", None, *[
        h("th", {"scope": "col", "class": classes("num" if c.num else None, "hide-narrow" if c.hide_narrow else None,
                                                  "col-key" if c.key else None)},
          c.label) for c in cols]))
    body = []
    for r in rows:
        cells = []
        for i, (c, v) in enumerate(zip(cols, r.cells)):
            cls = classes("num" if c.num else None, "mono" if c.mono else None, "hide-narrow" if c.hide_narrow else None)
            if i == 0 and r.href is not None:
                v = h("a", {"class": "row-link", "href": r.href, "aria-current": "true" if r.selected else None}, v)
            tag = "th" if i == 0 and row_header else "td"
            cells.append(h(tag, {"class": cls, "scope": "row" if tag == "th" else None,
                                 "data-label": _plain(c.label) if stack and i else None}, v))
        body.append(h("tr", {"class": classes("is-selected" if r.selected else None,
                                              "has-link" if r.href else None, f"tone-{r.tone}" if r.tone else None),
                             "data-key": r.key or r.href},
                      *cells))
    tbl = h("table", {"class": classes("table", "table-stack" if stack else None, "table-fixed" if fixed else None)},
            h("caption", {"class": "sr-only"}, caption),
            head, h("tbody", None, *body))
    return h("div", {"class": classes("table-wrap", "table-wide" if wide and not stack else None),
                     "tabindex": "0" if wide and not stack else None, "role": "region" if wide and not stack else None,
                     "aria-label": caption if wide and not stack else None}, tbl)


def table_footer(*children: Node) -> Markup:
    return h("div", {"class": "table-footer"}, *children)


# --- 키·값, 태그, 단계 --------------------------------------------------------------------------------


def keyvals(rows: Sequence[tuple[Node, Node]], divided: bool = False, wide_key: bool = False, narrative: bool = False) -> Markup:
    return h("dl", {"class": classes("keyvals", "divided" if divided else None, "wide-key" if wide_key else None,
                                     "narrative" if narrative else None)},
             *[h("div", {"class": "kv"}, h("dt", None, k), h("dd", None, v)) for k, v in rows])


def responsive(wide: Node, narrow: Node) -> Markup:
    """비교 자료는 넓은 표와 좁은 핵심 목록 중 화면에 맞는 것만 표시한다."""
    return h("div", {"class": "responsive"}, h("div", {"class": "viewport-wide"}, wide),
             h("div", {"class": "viewport-narrow"}, narrow))


def ordered(items: Sequence[Node], label: str | None = None) -> Markup:
    """번호 붙은 본문 항목(약관 조항의 항 등)."""
    return h("ol", {"class": "ordered", "aria-label": label}, *[h("li", None, t) for t in items])


def disclosure(summary: Node, *body: Node, open: bool = False) -> Markup:
    """한 줄 요약을 누르면 펼쳐지는 묶음(다 끝난 처리 단계처럼 평소에는 볼 일이 적은 내용)."""
    return h("details", {"class": "disclosure", "open": open}, h("summary", {"class": "disclosure-summary"}, summary),
             h("div", {"class": "disclosure-body"}, *body))


def link_list(items: Sequence[tuple[Node, str] | tuple[Node, str, bool]], label: str) -> Markup:
    """링크 목록(안내 근거 조항 등). items는 (글자, 주소) 또는 (글자, 주소, 강조). 강조한 항목(지금 나가는 답의 근거 등)은
    색으로 구분한다. 항목은 주소로 이름 붙어, 갱신 때 순서가 바뀌어도 같은 항목으로 이어진다."""
    out = []
    for it in items:
        t, href, *rest = it
        on = bool(rest and rest[0])
        out.append(h("li", {"data-key": href, "class": "is-current" if on else None}, h("a", {"href": href}, t)))
    return h("ul", {"class": "link-list", "aria-label": label}, *out)


def tags(items: Sequence[Node], label: str | None = None) -> Markup:
    return h("ul", {"class": "tags", "aria-label": label}, *[h("li", {"class": "tag"}, t) for t in items])


@dataclass
class Step:
    label: Node
    status: str  # done, now, todo, skip
    note: Node = None
    detail: Node = None


STEP_STATUS = {"done": "완료", "now": "진행 중", "todo": "대기", "skip": "적용 안 함"}


def steps(items: Sequence[Step], label: str, layout: str = "list") -> Markup:
    """진행 단계. layout: list(세로 목록, 점과 상태), tiles(가로 칸, 설명 포함)."""
    out = []
    for s in items:
        if s.status not in STEP_STATUS:
            raise ValueError(f"알 수 없는 단계 상태: {s.status}")
        state = h("span", {"class": "step-state"}, s.note if s.note is not None else STEP_STATUS[s.status])
        mark = h("span", {"class": "step-mark", "aria-hidden": "true"},
                 "✓" if s.status == "done" else ("–" if s.status == "skip" else None))
        out.append(h("li", {"class": f"step is-{s.status}", "aria-current": "step" if s.status == "now" else None},
                     mark, h("span", {"class": "step-label"}, s.label), state,
                     h("span", {"class": "step-detail"}, s.detail) if s.detail is not None else None))
    return h("ol", {"class": f"steps steps-{layout}", "aria-label": label}, *out)


# --- 막대 --------------------------------------------------------------------------------------------


def bar(value: float, total: float, tone: str = "accent") -> Markup:
    """가로 막대 하나(장식). 값 글자는 옆에 따로 둔다."""
    p = 0.0 if not total else value / total * 100
    return h("span", {"class": f"bar tone-{_tone(tone)}", "aria-hidden": "true"},
             h("span", {"class": "bar-fill", "style": _style_vars(pct=p)}))


@dataclass
class Part:
    label: Node
    value: int
    shown: Node   # fmt.count 등으로 만든 값 글자
    tone: str = "accent"  # 배지와 같은 색 이름(TONES)


def stack_bar(parts: Sequence[Part], label: str) -> Markup:
    """전체를 나눈 한 줄 막대와 범례. label은 막대 전체의 글자 설명(aria-label)이다."""
    total = sum(p.value for p in parts)
    segs = [h("span", {"class": f"stack-seg tone-{_tone(p.tone)}", "style": _style_vars(pct=p.value / total * 100 if total else 0)})
            for p in parts if p.value]
    legend = h("ul", {"class": "legend"}, *[
        h("li", None, h("span", {"class": f"legend-swatch tone-{_tone(p.tone)}", "aria-hidden": "true"}), p.label, " ",
          h("span", {"class": "legend-value"}, p.shown)) for p in parts])
    return h("div", {"class": "stack-bar-wrap"}, h("div", {"class": "stack-bar", "role": "img", "aria-label": label}, *segs),
             legend)


def bar_list(rows: Sequence[tuple[Node, float, Node]], tone: str = "accent", label: str | None = None,
             total: float | None = None) -> Markup:
    """이름, 막대, 값이 한 줄씩. rows는 (이름, 막대 길이 값, 보이는 값). total을 주면 그 값이 꽉 찬 막대이고(여러 카드를 같은
    기준으로 비교할 때), 없으면 가장 큰 값이 꽉 찬 막대다."""
    top = total if total is not None else max((v for _, v, _ in rows), default=0)
    return h("ul", {"class": "bar-list", "aria-label": label},
             *[h("li", {"class": "bar-row"}, h("span", {"class": "bar-name"}, n), bar(v, top, tone),
                 h("span", {"class": "bar-value"}, shown)) for n, v, shown in rows])


# --- 셸 ----------------------------------------------------------------------------------------------


@dataclass
class NavItem:
    label: str
    href: str
    active: bool = False
    count: Node = None


@dataclass
class NavGroup:
    label: str
    items: list[NavItem] = field(default_factory=list)


@dataclass
class Crumb:
    label: Node
    href: str | None = None
    mono: bool = False


def _nav(groups: Sequence[NavGroup], label: str) -> Markup:
    out = []
    for g in groups:
        out.append(h("div", {"class": "nav-group"}, h("div", {"class": "nav-group-label", "aria-hidden": "true"}, g.label),
                     h("ul", {"class": "nav-list", "aria-label": g.label}, *[
                         h("li", None, h("a", {"class": "nav-link", "href": i.href,
                                               "aria-current": "page" if i.active else None},
                                         h("span", {"class": "nav-link-text"}, i.label),
                                         h("span", {"class": "nav-count"}, i.count) if i.count is not None else None))
                         for i in g.items])))
    return h("nav", {"class": "nav-groups", "aria-label": label}, *out)


def shell(*, theme: str, title: str, product: str, nav: Sequence[NavGroup], main: Node, note: Node,
          user_initial: str, user_label: str, tenant: tuple[str, str] | None = None, search: tuple[str, str, str] | None = None,
          crumbs: Sequence[Crumb] = (), admin_badge: bool = False, wide: bool = False, live: str | None = None,
          search_fields: Sequence[tuple[str, str]] = ()) -> Markup:
    """화면 전체 문서. theme: saas, admin. search는 (폼 주소, 자리 표시 글자, 현재 검색어), crumbs는 상단바 경로.
    tenant는 (조직 이름, 설명)으로 메뉴 위에 보인다. live가 있으면 그 주소에서 실시간 갱신 조각을 받는다.
    같은 화면의 다른 상태로 가는 링크(필터, 기간, 문서·구간, 상세 서랍, 이전·다음)와 같은 주소로 보내는 검색 폼은 문서를 새로
    열지 않고 셸(data-swap)에서 바뀐 부분만 맞춘다. 다른 화면으로 가는 링크는 화면 전환 효과와 함께 문서를 연다(console.js)."""
    if theme not in ("saas", "admin"):
        raise ValueError(theme)
    logo = h("div", {"class": "brand"}, h("span", {"class": "brand-mark", "aria-hidden": "true"}, "직"),
             h("span", {"class": "brand-name"}, product),
             h("span", {"class": "admin-badge"}, "ADMIN") if admin_badge else None)
    tenant_box = None
    if tenant is not None:
        tenant_box = h("div", {"class": "tenant"}, h("span", {"class": "tenant-mark", "aria-hidden": "true"}, tenant[0][:1]),
                       h("span", {"class": "tenant-text"}, h("span", {"class": "tenant-name"}, tenant[0]),
                         h("span", {"class": "tenant-desc"}, tenant[1])))
    side = h("div", {"class": "shell-nav"}, logo, tenant_box, _nav(nav, "주 메뉴" if theme == "saas" else "관리 메뉴"),
             h("div", {"class": "nav-note"}, note))
    top = []
    if crumbs:
        top.append(h("nav", {"class": "crumbs", "aria-label": "위치"}, h("ol", None, *[
            h("li", None, h("a", {"href": c.href}, c.label) if c.href else h("span", {"class": "t-mono" if c.mono else None}, c.label))
            for c in crumbs])))
    if search is not None:
        action, placeholder, q = search
        top.append(h("form", {"class": "search", "action": action, "method": "get", "role": "search"},
                     *[hidden(name, value) for name, value in search_fields],
                     h("label", {"class": "sr-only", "for": "q"}, "검색"),
                     h("input", {"id": "q", "name": "q", "type": "search", "placeholder": placeholder, "value": q or None})))
    top.append(h("span", {"class": "avatar", "role": "img", "aria-label": user_label}, user_initial))
    body = h("div", {"class": "shell-body"}, h("div", {"class": "topbar"}, *top),
             h("main", {"id": "main", "class": classes("main", "main-wide" if wide else None), "tabindex": "-1",
                        "data-live": live}, main))
    doc = h("html", {"lang": "ko", "data-theme": theme},
            h("head", None, h("meta", {"charset": "utf-8"}),
              h("meta", {"name": "viewport", "content": "width=device-width, initial-scale=1"}),
              h("title", None, f"직감 · {title}"),
              *[h("link", {"rel": "stylesheet", "href": f"/static/{n}.css"}) for n in ("tokens", "base", "components")],
              h("script", {"src": "/static/console.js", "defer": True})),
            h("body", None, h("a", {"class": "skip", "href": "#main"}, "본문으로 건너뛰기"),
              h("div", {"class": "shell", "data-swap": True}, side, body)))
    return Markup("<!doctype html>") + doc


# --- 드로어 ------------------------------------------------------------------------------------------


def drawer(*body: Node, title: Node, close_href: str, label: str, sub: Node = None, footer: Node = None) -> Markup:
    """선택한 항목의 상세. 목록 옆(좁으면 아래)에 붙는다. 열리면 제목으로 포커스가 가고, Escape나 닫기로 close_href로 간다."""
    return h("aside", {"class": "drawer", "aria-label": label, "data-drawer": close_href},
             h("div", {"class": "drawer-head"},
               h("div", {"class": "drawer-title-wrap"},
                 h("div", {"class": "drawer-sub"}, sub) if sub is not None else None,
                 h("h2", {"class": "drawer-title", "tabindex": "-1", "data-autofocus": True}, title)),
               h("a", {"class": "btn btn-secondary btn-icon", "href": close_href, "aria-label": "닫기"},
                 h("span", {"aria-hidden": "true"}, "✕"))),
             h("div", {"class": "drawer-body"}, *body),
             h("div", {"class": "drawer-footer"}, footer) if footer is not None else None)


# --- 대화 --------------------------------------------------------------------------------------------


@dataclass
class Reveal:
    """말하는 중인 답. 재생 시작(start_ms)부터 끝(end_ms)까지 text의 글자를 차례로 드러낸다. 시각은 data-now와 같은 시계다.
    shown은 그린 순간까지 드러난 글자 수(스크립트 없이도 그만큼만 보인다)."""
    text: str
    start_ms: float
    end_ms: float
    shown: int


@dataclass
class Turn:
    who: str          # 화자 이름(고객, AI, 상담사)
    text: Node
    at: Node = None   # 시각(fmt.clock)
    kind: str = "customer"  # customer, ai, agent
    current: bool = False   # 지금 말하는 중
    cue: tuple[float, float] | None = None  # 녹음과 맞출 (시작, 끝) 초
    note: Node = None       # 말 아래 작은 글자(그 말의 근거 조항 등)
    key: str | None = None  # 줄 이름. 실시간 갱신이 이미 있는 줄을 그대로 두고 새 줄만 붙인다
    reveal: Reveal | None = None  # 있으면 text 대신 글자를 차례로 드러낸다


@dataclass
class Divider:
    text: Node
    key: str | None = None


def _reveal(r: Reveal) -> Markup:
    chars = [h("span", {"class": classes("rv", "on" if i < r.shown else None)}, ch) for i, ch in enumerate(r.text)]
    return h("span", {"class": "reveal", "data-reveal-from": f"{r.start_ms:.1f}", "data-reveal-to": f"{r.end_ms:.1f}",
                      "data-reveal-sig": f"{len(r.text)}"}, *chars)


def conversation(items: Sequence[Turn | Divider], label: str = "대화", id: str | None = None, now_ms: float | None = None,
                 running: bool = True, follow: bool = False) -> Markup:
    """대화 줄과 그 사이의 한 줄 표시(고객이 요청을 바꾼 순간). cue가 있으면 재생 위치에 맞춰 강조된다.
    now_ms는 말하는 중인 답을 드러낼 시계(실시간 통화), follow면 대화를 따로 스크롤되는 영역에 두고 새 줄을 따라 내린다."""
    out = []
    for it in items:
        if isinstance(it, Divider):
            out.append(h("li", {"class": "conv-divider", "data-key": it.key}, h("span", {"class": "conv-divider-text"}, it.text)))
            continue
        cue = {"data-cue-start": f"{it.cue[0]:.3f}", "data-cue-end": f"{it.cue[1]:.3f}"} if it.cue else {}
        out.append(h("li", {"class": classes("conv-turn", f"is-{it.kind}", "is-current" if it.current else None),
                            "data-key": it.key, **cue},
                     h("span", {"class": "conv-who"}, it.who),
                     h("span", {"class": "conv-text"}, _reveal(it.reveal) if it.reveal is not None else it.text,
                       h("span", {"class": "conv-note"}, it.note) if it.note is not None else None),
                     h("span", {"class": "conv-at"}, it.at) if it.at is not None else h("span", {"class": "conv-at"})))
    clock = {"data-now": f"{now_ms:.1f}", "data-running": "true" if running else "false"} if now_ms is not None else {}
    ol = h("ol", {"class": "conversation", "aria-label": None if follow else label, "id": id, **clock}, *out)
    if not follow:
        return ol
    return h("div", {"class": "conv-scroll", "data-follow": True, "tabindex": "0", "role": "region", "aria-label": label}, ol)


@dataclass
class ListItem:
    title: Node
    href: str
    meta: Node = None    # 오른쪽 위(시각 등)
    sub: Node = None     # 둘째 줄
    status: Node = None  # 배지
    active: bool = False


def item_list(items: Sequence[ListItem], label: str, cards: bool = False) -> Markup:
    """고를 수 있는 목록(진행 중인 통화 등). 고른 것은 aria-current. cards면 가로로 늘어선 카드(고객사 고르기)."""
    return h("ul", {"class": classes("item-list", "item-cards" if cards else None), "aria-label": label}, *[
        h("li", {"data-key": i.href}, h("a", {"class": "item", "href": i.href, "aria-current": "true" if i.active else None},
                        h("span", {"class": "item-top"}, h("span", {"class": "item-title"}, i.title),
                          h("span", {"class": "item-meta"}, i.meta) if i.meta is not None else None),
                        h("span", {"class": "item-sub"}, i.sub) if i.sub is not None else None,
                        h("span", {"class": "item-status"}, i.status) if i.status is not None else None))
        for i in items])


# --- 녹음 재생 ---------------------------------------------------------------------------------------


def player(src: str, label: str, start: float | None = None, stops: Sequence[float] = (), target: str | None = None,
           controls: Node = None, native: bool = True, decisions: Sequence[float] = (), decision: int | None = None,
           decision_href: str | None = None) -> Markup:
    """녹음 재생기. 같은 문서의 data-cue-start/end 요소(대화 줄, 재생 목록)가 재생 위치에 맞춰 강조된다.
    stops는 '판단 시점마다 멈춤'에 쓰는 시각(초), target은 맞출 영역의 id(없으면 문서 전체).
    native=False면 브라우저 기본 컨트롤 대신 재생 버튼 하나를 두고, 위치는 화면의 재생 띠(lanes)로 보인다.
    decisions(판단 번호 순서의 시각)와 decision_href(뒤에 번호를 붙이면 그 판단 화면)를 주면, 재생하거나 위치를 옮길 때
    화면이 그 시각 직전의 판단(decision은 지금 화면의 번호)으로 제자리에서 바뀐다."""
    a = {"class": "player", "data-player": True, "data-target": target,
         "data-stops": " ".join(f"{s:.3f}" for s in stops) or None,
         "data-start": None if start is None else f"{start:.3f}",
         "data-decisions": " ".join(f"{t:.3f}" for t in decisions) or None,
         "data-decision": None if decision is None else str(decision), "data-decision-href": decision_href}
    if native:
        return h("div", a, h("audio", {"controls": True, "preload": "metadata", "src": src, "aria-label": label}), controls)
    return h("div", a, h("audio", {"preload": "metadata", "src": src, "aria-label": label}),
             h("button", {"type": "button", "class": "btn btn-primary btn-play", "data-play": True, "aria-pressed": "false"},
               h("span", {"class": "play-on"}, "▶ 재생"), h("span", {"class": "play-off"}, "❚❚ 멈춤")), controls)


def toggle(name: str, label: str, checked: bool = False, **data: str) -> Markup:
    """켜고 끄는 상자 하나(판단 시점마다 멈춤 등)."""
    return h("label", {"class": "check-option"},
             h("input", {"type": "checkbox", "name": name, "checked": checked, **{f"data-{k.replace('_', '-')}": v for k, v in data.items()}}),
             label)


def live(url: str, every_ms: int, *children: Node) -> Markup:
    """every_ms마다 url을 다시 받아 안의 data-slot 영역만 새 내용으로 바꾼다(실시간 통화)."""
    return h("div", {"class": "live", "data-live": url, "data-interval": str(every_ms)}, *children)


def slot(name: str, *children: Node, polite: bool = False, inline: bool = False) -> Markup:
    """live 안에서 갱신되는 영역. polite면 화면 읽기 프로그램에 바뀐 내용을 조용히 알린다. inline이면 글자 안에 둔다."""
    return h("span" if inline else "div", {"class": "slot", "data-slot": name, "aria-live": "polite" if polite else None},
             *children)


def voice_controls(start_label: Node, stop_label: Node, offer_url: str, stop_url: str, customer: str | None,
                   call: str | None, running: bool, can_start: bool, messages: Sequence[tuple[str, Node, str]],
                   note: Node = None) -> Markup:
    """브라우저 마이크로 하는 음성 시험의 시작·종료 버튼과 상태 알림(console.js의 data-voice-test). 마이크는 에코 제거·잡음
    억제·자동 음량을 켜서 연다. messages는 (상태 이름, 글, 색)이고, 스크립트가 지금 상태의 알림만 보인다(글은 만들지 않는다).
    AI 음성은 숨은 audio 요소로 재생한다."""
    start = h("button", {"class": "btn btn-primary", "type": "button", "data-voice-start": True,
                         "disabled": running or not can_start}, start_label)
    stop = h("button", {"class": "btn btn-secondary", "type": "button", "data-voice-stop": True, "disabled": not running},
             stop_label)
    notes = [h("p", {"class": f"notice tone-{_tone(tone)}", "data-voice-msg": name, "role": "status", "hidden": True}, text)
             for name, text, tone in messages]
    return h("div", {"class": "voice-controls", "data-voice-test": True, "data-offer": offer_url, "data-stop": stop_url,
                     "data-customer": customer, "data-call": call},
             h("div", {"class": "voice-buttons"}, start, stop, note), *notes,
             h("audio", {"data-voice-audio": True, "autoplay": True, "hidden": True}))


def listen_source(id: str, src: str, at_s: float | None) -> Markup:
    """'통화 듣기' 버튼(data-listen)이 쓸 숨은 녹음과 지금 위치(초)."""
    return h("span", {"class": "listen-source"},
             h("audio", {"id": id, "src": src, "preload": "none"}),
             h("span", {"data-audio-at": "" if at_s is None else f"{at_s:.3f}", "hidden": True}))


# --- 관리 콘솔: 재생 띠, 재생 목록, 순위 막대, 순위 표 ------------------------------------------------------


@dataclass
class Seg:
    at: float     # 시작 위치(전체 대비 %)
    len: float    # 길이(%)
    tone: str = "series-a"


@dataclass
class Lane:
    label: str
    segs: list[Seg]


@dataclass
class Mark:
    at: float
    label: Node
    current: bool = False
    href: str | None = None


def lanes(rows: Sequence[Lane], marks: Sequence[Mark], cursor: float | None, ticks: Sequence[tuple[float, Node]],
          label: str, cuts: Sequence[float] = (), scrub: tuple[float, float, float] | None = None) -> Markup:
    """통화 전체 위의 화자별 구간 띠, 판단 표시, 지금 위치 선. 위치는 모두 전체 길이 대비 %다. cuts는 끼어들기 위치.
    scrub=(전체 초, 지금 초, 키보드 한 칸 초)이면 띠를 누르거나 끌어 재생 위치를 옮기고, 위치 선은 키보드 슬라이더다
    (←/→ 한 칸, PageUp/PageDown 판단 하나, Home/End 처음·끝)."""
    out = []
    for ln in rows:
        out.append(h("span", {"class": "lane-label"}, ln.label))
        out.append(h("div", {"class": "lane-track"},
                     *[h("span", {"class": f"lane-seg {s.tone}", "style": _style_vars(at=s.at, len=s.len)}) for s in ln.segs],
                     *([h("span", {"class": "lane-cut", "style": _style_vars(at=c)}) for c in cuts]
                       if ln is rows[-1] else [])))
    out.append(h("span", {"class": "lane-label"}, "판단"))
    out.append(h("div", {"class": "lane-marks"}, *[
        (h("a", {"class": classes("lane-mark", "is-current" if m.current else None), "href": m.href,
                 "style": _style_vars(at=m.at), "aria-current": "true" if m.current else None}, m.label)
         if m.href else h("span", {"class": classes("lane-mark", "is-current" if m.current else None),
                                   "style": _style_vars(at=m.at)}, m.label)) for m in marks]))
    out.append(h("span", {"class": "lane-label"}))
    out.append(h("div", {"class": "lane-ticks"},
                 h("span", {"class": "lane-cursor", "style": _style_vars(at=cursor), "data-cursor": True, "data-key": "cursor",
                            **({"role": "slider", "tabindex": "0", "aria-label": "재생 위치(초)", "aria-valuemax": f"{scrub[0]:.1f}",
                                "aria-valuenow": f"{scrub[1]:.1f}"} if scrub else {})})
                 if cursor is not None else None,
                 *[h("span", {"class": "lane-tick", "style": _style_vars(at=at)}, t) for at, t in ticks]))
    return h("div", {"class": "lanes", "role": "group", "aria-label": label,
                     **({"data-scrub": True, "data-duration": f"{scrub[0]:.3f}", "data-step": f"{scrub[2]:.3f}"} if scrub else {})},
             *out)


@dataclass
class PlayItem:
    at: Node        # 보이는 시각
    kind: str       # 고객, AI, 끼어듦, 판단 이름
    kind_tone: str  # caution(고객), accent(AI), negative(끼어듦), neutral(판단), positive(지금 판단)
    text: Node
    href: str | None = None
    current: bool = False
    past: bool = False
    cue: tuple[float, float] | None = None
    seek: float | None = None   # 누르면 녹음의 이 위치(초)부터 재생한다(href가 없는 발화 줄)


def playlist(items: Sequence[PlayItem], label: str) -> Markup:
    out = []
    for it in items:
        cue = {"data-cue-start": f"{it.cue[0]:.3f}", "data-cue-end": f"{it.cue[1]:.3f}"} if it.cue else {}
        inner = [h("span", {"class": "play-at"}, it.at), h("span", {"class": f"play-kind t-{it.kind_tone}"}, it.kind),
                 h("span", {"class": "play-text"}, it.text)]
        cls = classes("play-item", "is-current" if it.current else None, "is-past" if it.past else None)
        if it.href:
            node = h("a", {"class": cls, "href": it.href, "aria-current": "true" if it.current else None, **cue}, *inner)
        elif it.seek is not None:
            node = h("button", {"class": cls, "type": "button", "data-seek": f"{it.seek:.3f}", **cue}, *inner)
        else:
            node = h("div", {"class": cls, **cue}, *inner)
        out.append(h("li", None, node))
    return h("ol", {"class": "playlist", "aria-label": label}, *out)


@dataclass
class RankRow:
    rank: Node
    name: Node
    delta: Node      # fmt.rank_delta
    p: float         # 이번 확률
    shown: Node      # 이번 확률 글자(fmt.prob)
    prev: float | None = None  # 직전 확률(흐린 막대)
    prev_shown: Node = None    # 직전 확률 글자
    selected: bool = False
    gold: bool = False
    key: str | None = None     # 행 이름(조항). 다른 판단으로 넘어갈 때 같은 조항의 행이 새 순위 자리로 옮겨 간다


def rank_bars(rows: Sequence[RankRow], label: str, threshold: float | None = None) -> Markup:
    """조항 순위와 직전 대비 변화. 막대는 이번 확률, 흐린 막대는 직전 확률, 세로선은 선택 기준."""
    out = []
    for r in rows:
        out.append(h("li", {"class": classes("rank-row", "is-selected" if r.selected else None), "data-key": r.key},
                     h("span", {"class": "rank-no"}, r.rank),
                     h("span", {"class": "rank-name"}, r.name, h("span", {"class": "rank-gold", "aria-label": "정답 조항"}, "★")
                       if r.gold else None),
                     h("span", {"class": "rank-delta"}, r.delta),
                     h("span", {"class": "rank-track", "aria-hidden": "true"},
                       h("span", {"class": "rank-prev", "style": _style_vars(pct=(r.prev or 0) * 100)}) if r.prev is not None else None,
                       h("span", {"class": "rank-fill", "style": _style_vars(pct=r.p * 100)}),
                       h("span", {"class": "rank-threshold", "style": _style_vars(at=threshold * 100)})
                       if threshold is not None else None),
                     h("span", {"class": "rank-p"}, r.shown,
                       h("span", {"class": "rank-prev-p"}, " ← ", r.prev_shown) if r.prev_shown is not None else None)))
    return h("ol", {"class": "rank-bars", "aria-label": label}, *out)


@dataclass
class HeatCell:
    shown: Node            # 순위 글자(없으면 None → 후보 아님)
    p: float | None = None  # 색 진하기
    selected: bool = False


def heatmap(cols: Sequence[Node], rows: Sequence[tuple[Node, Sequence[HeatCell]]], foot: Sequence[tuple[Node, Sequence[Node]]],
            caption: str, current_col: int | None = None) -> Markup:
    """행(조항) × 열(판단) 표. 칸 글자는 순위, 색은 확률, 굵은 테두리는 선택. foot은 아래에 붙는 요약 행."""
    head = h("tr", None, h("td", None), *[
        h("th", {"scope": "col", "class": "is-current" if i == current_col else None,
                 "data-current-col": True if i == current_col else None}, c) for i, c in enumerate(cols)])
    body = []
    for name, cells in rows:
        tds = []
        for i, c in enumerate(cells):
            cur = "is-current" if i == current_col else None
            if c.shown is None:
                tds.append(h("td", {"class": classes("heat-none", cur)}, h("span", {"aria-hidden": "true"}, "·"),
                             sr_only("후보 아님")))
            else:
                tds.append(h("td", {"class": classes("heat", "is-selected" if c.selected else None, cur,
                                                     "is-dark" if (c.p or 0) > settings.HEAT_DARK_ABOVE else None),
                                    "style": _alpha(c.p or 0)}, c.shown))
        body.append(h("tr", None, h("th", {"scope": "row"}, name), *tds))
    for name, vals in foot:
        body.append(h("tr", {"class": "heat-foot"}, h("th", {"scope": "row"}, name),
                      *[h("td", {"class": "is-current" if i == current_col else None}, v) for i, v in enumerate(vals)]))
    return h("div", {"class": "table-wrap table-wide", "tabindex": "0", "role": "region", "aria-label": caption,
                     "data-scroll-current": True},
             h("table", {"class": "heatmap"}, h("caption", {"class": "sr-only"}, caption), h("thead", None, head),
               h("tbody", None, *body)))


def version_block(*lines: Node, tone: str = "accent") -> Markup:
    """답변 하나(무효가 된 답 또는 새 답). 왼쪽 굵은 선의 색으로 구분한다."""
    return h("div", {"class": f"version-block tone-{_tone(tone)}"}, *[h("div", {"class": "version-line"}, ln) for ln in lines])


def check_list(items: Sequence[tuple[bool, Node, Node]], label: str) -> Markup:
    """체크 목록(온보딩). items는 (끝남, 이름, 근거 글자)."""
    return h("ul", {"class": "check-list", "aria-label": label}, *[
        h("li", {"class": classes("check", "is-done" if done else None)},
          h("span", {"class": "check-mark", "aria-hidden": "true"}, "✓" if done else "○"),
          h("span", {"class": "check-text"}, h("span", None, name), sr_only("완료" if done else "남음"),
            h("span", {"class": "check-note"}, note) if note is not None else None))
        for done, name, note in items])


# --- 폼 ----------------------------------------------------------------------------------------------


def form(*children: Node, action: str, method: str = "post", label: str | None = None, multipart: bool = False) -> Markup:
    return h("form", {"class": "form", "action": action, "method": method, "aria-label": label,
                      "enctype": "multipart/form-data" if multipart else None}, *children)


def field_file(name: str, label: str, accept: str = ".pdf") -> Markup:
    """키보드로 고를 수 있는 파일 입력."""
    fid = f"f-{name}"
    return h("div", {"class": "field"}, h("label", {"for": fid}, label),
             h("input", {"id": fid, "name": name, "type": "file", "accept": accept, "required": True}))


def preformatted(text: str) -> Markup:
    """원문의 줄바꿈을 보존한다."""
    return h("pre", {"class": "document-text"}, text)


def field_choices(name: str, label: str, options: Sequence[tuple[str, str, bool]], selected: str) -> Markup:
    """연결되지 않은 실행 대상을 고를 수 없게 표시한다."""
    fid = f"f-{name}"
    return h("div", {"class": "field"}, h("label", {"for": fid}, label),
             h("select", {"id": fid, "name": name}, *[
                 h("option", {"value": v, "selected": v == selected, "disabled": not enabled}, text)
                 for v, text, enabled in options]))


def field_text(name: str, label: str, value: str | None = None, mono: bool = False, required: bool = False,
               placeholder: str | None = None, help: Node = None, show_for: str | None = None) -> Markup:
    """한 줄 입력. show_for가 있으면 같은 폼의 어댑터 고르기(name=adapter) 값이 그 목록(공백으로 구분)에 있을 때만 보인다."""
    fid = f"f-{name}"
    return h("div", {"class": "field", "data-show-for": show_for}, h("label", {"for": fid}, label),
             h("input", {"id": fid, "name": name, "type": "text", "value": value, "required": required,
                         "placeholder": placeholder, "class": "t-mono" if mono else None,
                         "aria-describedby": f"{fid}-help" if help is not None else None}),
             h("div", {"class": "field-help", "id": f"{fid}-help"}, help) if help is not None else None)


def field_select(name: str, label: str, options: Sequence[tuple[str, str]], selected: str | None = None) -> Markup:
    fid = f"f-{name}"
    return h("div", {"class": "field"}, h("label", {"for": fid}, label),
             h("select", {"id": fid, "name": name}, *[
                 h("option", {"value": v, "selected": v == selected}, t) for v, t in options]))


def field_checks(name: str, legend: str, options: Sequence[tuple[str, Node, bool]], help: Node = None) -> Markup:
    """여러 개 고르기. options는 (값, 이름, 처음에 고름)."""
    return h("fieldset", {"class": "field checks"}, h("legend", None, legend),
             *[h("label", {"class": "check-option"}, h("input", {"type": "checkbox", "name": name, "value": v, "checked": on}), t)
               for v, t, on in options],
             h("div", {"class": "field-help"}, help) if help is not None else None)


def hidden(name: str, value: str) -> Markup:
    return h("input", {"type": "hidden", "name": name, "value": value})


def joined(*parts: Node, sep: str = " · ") -> Markup:
    """짧은 정보 여러 개를 가운뎃점으로 잇는다. None은 뺀다."""
    return join([p for p in parts if p is not None], sep)
