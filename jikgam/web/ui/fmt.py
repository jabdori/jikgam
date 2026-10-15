"""화면의 숫자 형식. 화면의 숫자는 모두 이 모듈을 거친다(ADR 0022 숫자 출처 검사).

- 각 함수는 <span class="num" data-num="원래 값" data-kind="종류">보이는 글자</span>를 돌려준다. 숫자 출처 검사가
  data-num 요소를 모아 테스트 자료를 바꿨을 때 값이 따라 바뀌는지 본다. 다음 종류는 결과 자료가 아니라서 그 검사에서
  뺀다: index(목록의 몇 번째), rank(순위), axis(재생 띠 눈금), criterion(평가 기준 문서에 고정한 사전 기준 값).
- 값이 없으면(None, NaN) 대시를 내고 data-num은 비운다.
- *_text 함수는 속성(aria-label 등)에 넣을 일반 글자를 돌려준다.
- 무한대(근거 갱신 미갱신 등)는 호출한 쪽이 뜻을 정해 글자로 보여 준다(inf_text).
"""

import math
from datetime import date, datetime
from typing import Any

from .html import Markup, attrs, esc

DASH = "—"


def _missing(x: Any) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def _span(raw: Any, text: str, cls: str | None = None, kind: str = "number") -> Markup:
    a = {"class": "num" + (f" {cls}" if cls else ""), "data-num": "" if _missing(raw) else str(raw), "data-kind": kind}
    return Markup(f"<span{attrs(a)}>{esc(text)}</span>")


def number_text(x: float | int | None, digits: int = 0) -> str:
    if _missing(x):
        return DASH
    if isinstance(x, float) and math.isinf(x):
        return "∞" if x > 0 else "-∞"
    return f"{x:,.{digits}f}"


def number(x: float | int | None, digits: int = 0) -> Markup:
    return _span(x, number_text(x, digits))


def index(n: int) -> Markup:
    """목록에서 몇 번째인지(쪽 표시 등). 화면 구조의 숫자다."""
    return _span(n, count_text(n), kind="index")


def rank(n: int) -> Markup:
    """순위(1, 2, 3…). 화면 구조의 숫자다."""
    return _span(n, count_text(n), kind="rank")


def count_text(n: int | None, unit: str = "") -> str:
    return DASH if _missing(n) else f"{n:,}{unit}"


def count(n: int | None, unit: str = "", kind: str = "count") -> Markup:
    return _span(n, count_text(n, unit), kind=kind)


def pct_text(x: float | None, digits: int = 1) -> str:
    """x는 0~1의 비율."""
    return DASH if _missing(x) else f"{x * 100:.{digits}f}%"


def pct(x: float | None, digits: int = 1, kind: str = "pct") -> Markup:
    return _span(None if _missing(x) else round(x, 6), pct_text(x, digits), kind=kind)


def won_text(x: int | float | None) -> str:
    return DASH if _missing(x) else f"{x:,.0f}원"


def won(x: int | float | None) -> Markup:
    return _span(x, won_text(x), kind="won")


def seconds_text(s: float | None) -> str:
    """상담 시간. 60초 미만은 "39초", 그 이상은 "2분 17초"."""
    if _missing(s):
        return DASH
    total = round(s)
    m, sec = divmod(total, 60)
    return f"{sec}초" if m == 0 else (f"{m}분" if sec == 0 else f"{m}분 {sec}초")


def seconds(s: float | None) -> Markup:
    return _span(None if _missing(s) else round(s, 3), seconds_text(s), kind="seconds")


def seconds_fine_text(s: float | None, digits: int = 2) -> str:
    """짧은 구간 길이. "1.66초"."""
    return DASH if _missing(s) else f"{s:.{digits}f}초"


def seconds_fine(s: float | None, digits: int = 2) -> Markup:
    return _span(None if _missing(s) else round(s, 3), seconds_fine_text(s, digits), kind="seconds")


def clock_text(s: float | None, tenths: bool = False) -> str:
    """재생 위치. "0:44", tenths면 "0:48.8"."""
    if _missing(s):
        return DASH
    s = max(s, 0.0)
    if tenths:
        whole = math.floor(s * 10) / 10
        m, rest = divmod(whole, 60)
        return f"{int(m)}:{rest:04.1f}"
    m, sec = divmod(int(s), 60)
    return f"{m}:{sec:02d}"


def clock(s: float | None, tenths: bool = False, kind: str = "clock") -> Markup:
    return _span(None if _missing(s) else round(s, 3), clock_text(s, tenths), kind=kind)


def ms_text(x: float | None, digits: int = 0) -> str:
    """관리 콘솔 전용(고객사 화면에서는 금지어 검사가 잡는다)."""
    if _missing(x):
        return DASH
    if math.isinf(x):
        return "∞"
    return f"{x:,.{digits}f}ms"


def ms(x: float | None, digits: int = 0, kind: str = "ms") -> Markup:
    return _span(None if _missing(x) else ("inf" if math.isinf(x) else round(x, 3)), ms_text(x, digits), kind=kind)


def usd_text(x: float | None, digits: int | None = None) -> str:
    """digits가 없으면 유효 숫자 세 자리, 있으면 소수 그 자리까지(표에서 자릿수를 맞출 때)."""
    if _missing(x):
        return DASH
    if digits is not None:
        return f"${x:,.{digits}f}"
    if x == 0:
        return "$0"
    digits = max(2, -math.floor(math.log10(abs(x))) + 2)
    return f"${x:,.{digits}f}"


def usd(x: float | None, digits: int | None = None) -> Markup:
    return _span(None if _missing(x) else x, usd_text(x, digits), kind="usd")


def prob_text(p: float | None) -> str:
    return DASH if _missing(p) else f"{p:.2f}"


def prob(p: float | None) -> Markup:
    return _span(p, prob_text(p), kind="prob")


def rank_delta_text(prev: int | None, now: int) -> str:
    """직전 판단 대비 순위 변화. 오르면 ▲, 내리면 ▼, 같으면 –, 직전에 없으면 "신규"."""
    if prev is None:
        return "신규"
    k = prev - now
    return f"▲{k}" if k > 0 else (f"▼{-k}" if k < 0 else "–")


def rank_delta(prev: int | None, now: int) -> Markup:
    tone = None if prev is None else ("up" if prev > now else ("down" if prev < now else None))
    return _span("" if prev is None else prev - now, rank_delta_text(prev, now), tone, kind="rank")


def date_text(d: date | datetime | str | None) -> str:
    if d is None:
        return DASH
    if isinstance(d, str):
        return d[:10]
    return d.strftime("%Y-%m-%d")


def date_(d: date | datetime | str | None) -> Markup:
    return _span(date_text(d) if d is not None else None, date_text(d), kind="date")


def time_text(d: datetime | str | None) -> str:
    """"14:03"처럼 시와 분."""
    if d is None:
        return DASH
    if isinstance(d, str):
        d = datetime.fromisoformat(d)
    return d.strftime("%H:%M")


def time_(d: datetime | str | None) -> Markup:
    return _span(None if d is None else (d if isinstance(d, str) else d.isoformat()), time_text(d), kind="time")


def ratio(a: int | None, b: int | None) -> Markup:
    """"0 / 64"."""
    return Markup(f"{count(a)} / {count(b)}")
