"""HTML을 만드는 가장 낮은 수준의 함수. 컴포넌트(components.py)와 숫자 형식(fmt.py)만 이 모듈을 쓴다(ADR 0022).

- 문자열은 언제나 이스케이프한다. 이미 만든 HTML은 Markup으로 감싸 그대로 넣는다.
- 숫자(int, float)는 받지 않는다. 화면의 숫자는 fmt의 형식 함수를 거쳐 data-num 표시가 붙은 요소로만 나간다.
- 속성 값이 True면 이름만, False·None이면 빼고, 그 밖의 값은 문자열이어야 한다.
"""

from html import escape
from typing import Any, Iterable

VOID = {"area", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class Markup(str):
    """이스케이프를 마친 HTML."""

    __slots__ = ()

    def __add__(self, other: Any) -> "Markup":
        return Markup(str(self) + str(render(other)))


def esc(x: Any) -> Markup:
    if isinstance(x, Markup):
        return x
    if isinstance(x, bool) or not isinstance(x, str):
        raise TypeError(f"HTML 글자에는 문자열만 넣는다(숫자는 fmt를 거친다): {type(x).__name__}")
    return Markup(escape(x, quote=False))


def render(node: Any) -> Markup:
    """노드 하나를 HTML로. None은 빈 글자, 목록은 이어 붙인다."""
    if node is None:
        return Markup("")
    if isinstance(node, (list, tuple)):
        return Markup("".join(render(n) for n in node))
    return esc(node)


def _attr_value(name: str, value: Any) -> str:
    if isinstance(value, Markup):
        return escape(str(value), quote=True)
    if isinstance(value, bool) or not isinstance(value, str):
        raise TypeError(f"속성 {name}의 값은 문자열이다(숫자는 fmt를 거친다): {type(value).__name__}")
    return escape(value, quote=True)


def attrs(a: dict[str, Any] | None) -> str:
    out = []
    for k, v in (a or {}).items():
        if v is None or v is False:
            continue
        name = k.rstrip("_").replace("_", "-")
        out.append(f" {name}" if v is True else f' {name}="{_attr_value(name, v)}"')
    return "".join(out)


def h(tag: str, a: dict[str, Any] | None = None, *children: Any) -> Markup:
    if tag in VOID:
        if children:
            raise ValueError(f"<{tag}>에는 자식이 없다")
        return Markup(f"<{tag}{attrs(a)}>")
    return Markup(f"<{tag}{attrs(a)}>{render(list(children))}</{tag}>")


def join(parts: Iterable[Any], sep: str = "") -> Markup:
    return Markup(esc(sep).join(render(p) for p in parts if p is not None))


def classes(*names: str | None) -> str | None:
    """None과 빈 이름을 뺀 class 값. 하나도 없으면 None(속성을 쓰지 않는다)."""
    s = " ".join(n for n in names if n)
    return s or None
