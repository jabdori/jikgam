"""고객사 콘솔: 통화 기록(목록, 상세 드로어, 대화 다시 듣기). 화면 수치의 정의는 docs/design/data.md."""

from datetime import date, timedelta
from typing import Any

from .. import settings
from ..data.calls import INTENT, OUTCOME, Call
from ..data.tenants import calls_of
from ..ui import components as ui
from ..ui import fmt
from ..ui.words import eul
from .common import CHANGED_LABEL, Ctx, article_names, article_short, evidence_links, outcome_badge, shell, who

FILTERS = ("filed", "answered", "handoff", "hold", "dropped")


def period_label(days: int) -> str:
    return "오늘" if days == 1 else ("전체" if days == 0 else f"{days}일")


def intent_name(code: str | None) -> str | None:
    return None if code is None else INTENT.get(code, INTENT["other"])


def period_days(ctx: Ctx, default: int) -> int:
    raw = ctx.query.get("period")
    try:
        days = int(raw) if raw is not None else default
    except ValueError:
        days = default
    return days if days in settings.REPORT_PERIODS else default


def in_period(calls: list[Call], days: int) -> tuple[list[Call], date | None, date | None]:
    """마지막 통화 날짜를 기준으로 최근 days일(0은 전체). (통화, 기간 첫날, 기간 끝날). 기간은 고른 범위 그대로다
    (전체는 첫 통화 날짜부터)."""
    dated = [c for c in calls if c.started is not None]
    if not dated:
        return calls, None, None
    last = max(c.started.date() for c in dated)
    first = min(c.started.date() for c in dated) if days == 0 else last - timedelta(days=days - 1)
    picked = [c for c in calls if c.started is not None and first <= c.started.date() <= last]
    return picked, first, last


def matches(c: Call, q: str) -> bool:
    q = q.strip()
    if not q:
        return True
    digits = "".join(ch for ch in q if ch.isdigit())
    if c.name and q in c.name:
        return True
    if digits and c.phone and digits in c.phone.replace("-", ""):
        return True
    return any(q in ln.text for ln in c.lines)


def period_chips(ctx: Ctx, base: str, days: int, keep: dict[str, str]) -> Any:
    def href(d: int) -> str:
        params = {**keep, "period": str(d)}
        return base + "?" + "&".join(f"{k}={v}" for k, v in params.items() if v)
    return ui.chips([ui.Chip(period_label(d), href(d), active=d == days) for d in settings.REPORT_PERIODS],
                    label="기간", kind="segmented")


def period_text(lo: date | None, hi: date | None) -> Any:
    if lo is None:
        return None
    return fmt.date_(lo) if lo == hi else ui.joined(fmt.date_(lo), fmt.date_(hi), sep=" ~ ")


# --- 요약 문장(상세) -----------------------------------------------------------------------------------


def summary(ctx: Ctx, c: Call) -> list[Any]:
    """처리 사실로 만든 요약 문장. 생성 모델을 쓰지 않는다. 배지·문의·처리 내역에 이미 있는 것(처리 결과, 요청 흐름)은
    되풀이하지 않고 환불 금액과 근거, 이용 마지막 날, 상담사 연결·보류·중간 종료의 사유만 적는다(docs/design/data.md).
    적을 것이 없으면 빈 목록."""
    out: list[Any] = []
    if c.refunded:
        out += ["환불 ", fmt.won(c.refund_amount), "을 접수했습니다. "]
        if c.refund_pending_approval:
            out.append("환불은 상담사 승인 뒤 확정됩니다. ")
    elif c.refund_denied_articles:
        out.append(f"환불은 {', '.join(article_names(ctx, c.refund_denied_articles))}에 따라 대상이 아닙니다(업무 시스템 견적). ")
    elif c.cancelled:
        out.append("환불은 접수하지 않았습니다" + (f"(해지 근거 {', '.join(article_names(ctx, c.cancel_articles))}). " if c.cancel_articles
                                               else ". "))
    if c.outcome == "handoff":
        out.append(f"{c.reason} 사유로 상담사에게 연결했습니다. ")
    elif c.outcome == "hold":
        out.append(f"{c.reason} 때문에 처리를 보류했습니다. ")
    elif c.outcome == "dropped":
        out.append(f"AI가 {c.asked}{eul(c.asked)} 묻는 중에 통화가 끝났습니다. " if c.asked else "AI가 확인을 묻는 중에 통화가 끝났습니다. ")
    if c.last_usable_date:
        out += ["이용 가능한 마지막 날은 ", fmt.date_(c.last_usable_date), "입니다."]
    return out


def change_text(ch: Any) -> str:
    """대화 사이의 한 줄. 통화 기록과 실시간 통화가 같은 문구를 쓴다."""
    before, after = intent_name(ch.before), intent_name(ch.after)
    if before and after and before != after:
        return f"요청 변경: {before} → {after}"
    return "요청 변경"


def change_result(ch: Any) -> Any:
    """요청이 바뀐 뒤 AI가 한 일. 바뀌기 전 내용으로 만든 안내가 나갔을 때만 경고로 보인다."""
    before, after = intent_name(ch.before), intent_name(ch.after)
    head = f"AI가 {before} 안내를 멈추고 {after} 안내로 바꿨습니다." if before and after and before != after \
        else "AI가 바뀐 요청으로 다시 안내했습니다."
    if ch.wrong:
        return ui.stack(ui.text(head, tone="neutral", size="md"),
                        ui.notice(ui.joined("바뀌기 전 내용의 잘못된 안내가 ", fmt.count(ch.wrong, "번"), " 나갔습니다.", sep=""),
                                  tone="negative"), gap="sm")
    return ui.text(head, tone="neutral", size="md")


def step_items(c: Call) -> list[ui.Step]:
    return [ui.Step(s.label, "done" if s.done else "skip", note=s.result) for s in c.steps]


def evidence(ctx: Ctx, c: Call) -> Any:
    return evidence_links(ctx, c.articles)


def cut_note(ln: Any) -> Any:
    if ln.played_s:
        return ui.joined("고객이 ", fmt.seconds_fine(ln.played_s, digits=1), " 듣고 말을 끊음", sep="")
    return "고객이 말을 끊음"


def conversation(ctx: Ctx, c: Call, with_cues: bool) -> Any:
    items: list[Any] = []
    changes = {ch.line_index: ch for ch in c.changes}
    for i, ln in enumerate(c.lines):
        cue = (ln.start_s, ln.end_s) if with_cues and ln.start_s is not None and ln.end_s is not None else None
        notes = [cut_note(ln) if ln.cut else None,
                 ("근거 " + ", ".join(article_short(a, ctx) for a in ln.articles)) if ln.articles else None]
        note = ui.joined(*notes) if any(notes) else None
        items.append(ui.Turn("고객" if ln.kind == "customer" else "AI", ln.text, fmt.clock(ln.elapsed_s), ln.kind, cue=cue,
                             note=note))
        if i in changes:
            items.append(ui.Divider(change_text(changes[i])))
    return ui.conversation(items, id="conv")


def meta_line(c: Call) -> Any:
    when = ui.joined(fmt.date_(c.started), fmt.time_(c.started), sep=" ") if c.started else None
    return ui.joined(when, "음성" if c.channel == "voice" else "채팅", ui.joined("상담 ", fmt.seconds(c.duration_s), sep=""))


# --- 목록 ----------------------------------------------------------------------------------------------


def _href(base: dict[str, str], **kw: str | None) -> str:
    params = {**base, **{k: v for k, v in kw.items()}}
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v)
    return "/app/calls" + (f"?{qs}" if qs else "")


def drawer(ctx: Ctx, c: Call, close: str) -> Any:
    text = summary(ctx, c)
    body = [
        ui.row(outcome_badge(c), ui.text(c.topic, tone="muted", size="md"), gap="sm"),
        ui.stack(ui.subheading("요약"), ui.para(text), gap="sm") if text else None,
        ui.stack(ui.subheading("처리 내역"), ui.steps(step_items(c), label="처리 내역"), gap="sm"),
        ui.stack(ui.subheading("안내 근거"), evidence(ctx, c), gap="sm"),
    ]
    for ch in c.changes:
        body.append(ui.stack(ui.subheading("고객이 요청을 바꾼 순간"),
                             ui.card(ui.text(ui.joined(ui.text("고객", tone="caution", strong=True), ch.text, sep=" "), size="md"),
                                     change_result(ch)), gap="sm"))
    listen = c.recording is not None and c.recording.get("available")
    footer = ui.button("대화 전체와 녹음 보기" if listen else "대화 전체 보기", href=f"/app/calls/{c.id}", kind="primary", size="lg")
    return ui.drawer(*body, title=who(c), sub=meta_line(c), close_href=close, label="통화 상세", footer=footer)


def page(ctx: Ctx) -> Any:
    q = ctx.query.get("q", "")
    days = period_days(ctx, default=settings.DEFAULT_PERIOD)
    outcome = ctx.query.get("outcome") or ""
    changed = ctx.query.get("changed") == "yes"
    all_calls, lo, hi = in_period(calls_of(ctx.root, ctx.tenant, ctx.allow_holdout), days)
    found = [c for c in all_calls if matches(c, q)]
    keep = {"q": q, "period": str(days)}
    counts = {k: sum(c.outcome == k for c in found) for k in FILTERS}
    chips = [ui.Chip("전체", _href(keep), fmt.count(len(found)), active=not outcome and not changed)]
    chips += [ui.Chip(OUTCOME[k], _href(keep, outcome=k), fmt.count(counts[k]), active=outcome == k)
              for k in FILTERS if counts[k]]
    toggle = [ui.Chip(CHANGED_LABEL, _href(keep, changed="" if changed else "yes"), fmt.count(sum(c.changed for c in found)),
                      active=changed)]
    rows = [c for c in found if (not outcome or c.outcome == outcome) and (not changed or c.changed)]

    size = settings.CALLS_PAGE_SIZE
    try:
        pg = max(int(ctx.query.get("page") or 1), 1)
    except ValueError:
        pg = 1
    pages = max((len(rows) - 1) // size + 1, 1)
    pg = min(pg, pages)
    shown = rows[(pg - 1) * size: pg * size]
    state = {**keep, "outcome": outcome, "changed": "yes" if changed else "", "page": str(pg) if pg > 1 else ""}
    sel_id = ctx.query.get("call")
    sel = next((c for c in rows if c.id == sel_id), None)

    cols = [ui.Col("고객"), ui.Col("문의"), ui.Col("처리 결과"), ui.Col("환불 접수액", num=True),
            ui.Col("상담 시간", num=True, hide_narrow=True), ui.Col("일시", num=True, hide_narrow=True)]
    trs = [ui.Row([ui.stack(c.name or "확인 전 고객", ui.text(c.phone, tone="muted", size="sm", mono=True) if c.phone else None,
                            gap="xs"),
                   c.topic, outcome_badge(c), fmt.won(c.refund_amount) if c.refunded else fmt.DASH,
                   fmt.seconds(c.duration_s),
                   ui.joined(fmt.date_(c.started), fmt.time_(c.started), sep=" ") if c.started else fmt.DASH],
                  href=_href(state, call=c.id), selected=sel is not None and c.id == sel.id) for c in shown]
    first = (pg - 1) * size + 1 if shown else 0
    prev = ui.button("이전", href=_href(state, page=str(pg - 1)), size="sm", disabled=pg <= 1)
    nxt = ui.button("다음", href=_href(state, page=str(pg + 1)), size="sm", disabled=pg >= pages)
    footer = ui.table_footer(ui.joined(fmt.count(len(rows), "건"), " 중 ", fmt.index(first), "–", fmt.index(first + len(shown) - 1),
                                       sep=""), ui.spacer(), prev, nxt) if rows else None
    listing = ui.card(ui.table(cols, trs, caption="통화 목록", wide=True, stack=True, empty_text="조건에 맞는 통화가 없습니다."),
                      footer, label="통화 목록", flush=True)
    main = ui.stack(
        ui.page_header("통화 기록", period_text(lo, hi), actions=period_chips(ctx, "/app/calls", days, {"q": q})),
        ui.row(ui.chips(chips, label="처리 결과 필터"), ui.chips(toggle, label="요청 변경 필터"), gap="lg"),
        listing, gap="lg")
    body = ui.columns(("main", main), ("drawer", drawer(ctx, sel, _href(state)))) if sel else \
        ui.columns(("main", main))
    return shell(ctx, "calls", "통화 기록", body, search=q, wide=True)


# --- 대화 전체(다시 듣기) --------------------------------------------------------------------------------


def detail(ctx: Ctx, cid: str) -> Any | None:
    c = next((x for x in calls_of(ctx.root, ctx.tenant, ctx.allow_holdout) if x.id == cid), None)
    if c is None:
        return None
    rec = c.recording if c.recording and c.recording.get("available") else None
    if rec is not None:
        listen = ui.card(ui.player(f"/app/calls/{c.id}/audio", label="통화 녹음", target="conv"), title="녹음", label="녹음")
    elif c.channel == "voice":
        listen = ui.card(ui.empty("녹음 파일이 없습니다.", "이 통화의 녹음은 보관 위치에 없습니다."), title="녹음", label="녹음")
    else:
        listen = None
    conv = ui.card(conversation(ctx, c, with_cues=rec is not None), title="대화", label="대화")
    text = summary(ctx, c)
    main = ui.stack(
        ui.row(ui.link("← 통화 기록", "/app/calls"), gap="sm"),
        ui.page_header(ui.joined(who(c), outcome_badge(c), sep=" "), ui.joined(meta_line(c), c.topic)),
        ui.split(ui.stack(ui.anchor("player", listen) if listen is not None else None, conv, gap="lg"),
                 ui.stack(ui.card(ui.para(text), title="요약", label="요약") if text else None,
                          ui.card(ui.steps(step_items(c), label="처리 내역"), title="처리 내역", label="처리 내역"),
                          ui.card(evidence(ctx, c), title="안내 근거", label="안내 근거"), gap="lg")),
        gap="lg")
    return shell(ctx, "calls", f"{who(c)} 통화", main)

