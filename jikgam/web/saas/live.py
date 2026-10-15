"""고객사 콘솔: 실시간 통화. 재생 피드(jikgam/web/data/live.py)가 지금까지 흘린 기록으로 그린다(ADR 0021).

화면은 settings.LIVE_REFRESH_MS마다 같은 주소를 다시 받아 data-slot 영역(통화 목록, 상태, 대화, 오른쪽 정보)만 바꾼다.
녹음과 버튼은 갱신 영역 밖에 두어 듣는 중에 끊기지 않는다.
"""

from typing import Any

from .. import settings
from ..data.calls import INTENT, Call, live_steps
from ..data.live import Feed
from ..ui import components as ui
from ..ui import fmt
from .calls import change_text, cut_note
from .common import Ctx, article_short, evidence_links, shell, who


def status(feed: Feed, call: Call) -> tuple[str, str]:
    """(글자, 색)."""
    if feed.taken_over:
        return "상담사 응대 중", "positive"
    if call.handoff_requested:
        return "상담사 연결 요청", "caution"
    return "AI 응대 중", "accent"


def call_list(ctx: Ctx, feeds: list[Feed], current: Feed) -> Any:
    items = []
    calls = [(f, f.call()) for f in feeds]
    for f, c in calls:
        text, tone = status(f, c)
        # 기본 상태(AI 응대 중)는 배지를 달지 않고, 운영팀이 움직여야 하는 상태만 배지로 보인다
        items.append(ui.ListItem(c.name or (c.phone or "확인 전 고객"), f"/app/live?call={f.id}", meta=fmt.clock(f.elapsed_s),
                                 sub=c.topic, status=ui.badge(text, tone) if tone != "accent" else None,
                                 active=f.id == current.id))
    ai = sum(1 for f, c in calls if status(f, c)[0] == "AI 응대 중")
    other = len(calls) - ai
    head = ui.pane_head("실시간 통화", ui.joined(ui.joined("AI 응대 ", fmt.count(ai), sep=""),
                                                 ui.joined("상담사 요청·응대 ", fmt.count(other), sep="") if other else None))
    return ui.stack(head, ui.item_list(items, label="진행 중인 통화"), gap="xs")


def conversation(ctx: Ctx, call: Call) -> Any:
    items: list[Any] = []
    changes = {ch.line_index: ch for ch in call.changes}
    for i, ln in enumerate(call.lines):
        who_ = "고객" if ln.kind == "customer" else ("AI · 말하는 중" if ln.playing else "AI")
        notes = [cut_note(ln) if ln.cut else None,
                 ("근거 " + ", ".join(article_short(a, ctx) for a in ln.articles)) if ln.articles else None]
        items.append(ui.Turn(who_, ln.text, fmt.clock(ln.elapsed_s), ln.kind, current=ln.playing,
                             note=ui.joined(*notes) if any(notes) else None))
        if i in changes:
            items.append(ui.Divider(change_text(changes[i])))
    if not items:
        return ui.empty("고객이 아직 말하지 않았습니다.")
    return ui.conversation(items)


def side(ctx: Ctx, feed: Feed, call: Call) -> Any:
    steps = [ui.Step(label, st, note=note) for label, st, note in live_steps(call, feed.taken_over)]
    facts = ui.keyvals(call.facts) if call.facts else ui.text("본인 확인과 조회가 끝나면 보입니다.", tone="muted", size="md")
    return ui.stack(
        ui.card(ui.steps(steps, label="진행 단계"), title="진행 단계", label="진행 단계"),
        ui.card(facts, title="고객 정보", label="고객 정보"),
        ui.card(evidence_links(ctx, ordered_articles(call)), title="안내 근거", label="안내 근거"), gap="lg")


def ordered_articles(call: Call) -> list[str]:
    """지금 나가는 답의 근거를 맨 앞에, 나머지는 처음 나온 순서로."""
    now = next((ln.articles for ln in reversed(call.lines) if ln.kind == "ai" and ln.articles), [])
    return list(now) + [a for a in call.articles if a not in now]


def page(ctx: Ctx) -> Any:
    feeds = ctx.feeds.of(ctx.tenant.org)
    if not feeds:
        main = ui.stack(ui.page_header("실시간 통화"), ui.card(ui.empty("진행 중인 통화가 없습니다.",
                                                                  "통화가 들어오면 여기에 보입니다.")), gap="lg")
        return shell(ctx, "live", "실시간 통화", main)
    feed = ctx.feeds.get(ctx.tenant.org, ctx.query.get("call", "")) or feeds[0]
    call = feed.call()
    text, tone = status(feed, call)
    rec = feed.recording if feed.recording and feed.recording.get("available") else None
    listen = ui.button("통화 듣기", pressed=False, listen="live-audio") if rec is not None \
        else ui.button("통화 듣기", disabled=True, label="통화 듣기(음성이 없는 통화)")
    takeover = ui.form(ui.button("넘겨받음" if feed.taken_over else "상담사가 넘겨받기", type="submit", kind="primary",
                                 disabled=feed.taken_over),
                       action=f"/app/live/{feed.id}/takeover", label="상담사가 넘겨받기")
    sub = ui.joined(call.phone, f"지금 요청: {INTENT.get(call.intents[-1], INTENT['other'])}" if call.intents else None)
    now_step = next(((label, note) for label, st, note in live_steps(call, feed.taken_over) if st == "now"), None)
    header = ui.page_header(ui.slot("title", who(call), inline=True), ui.slot("sub", sub, inline=True), demoted=True,
                            actions=ui.row(ui.slot("status", ui.badge(ui.joined(text, fmt.clock(feed.elapsed_s),
                                                                                 now_step[0] if now_step else None), tone, dot=True)),
                                           listen, takeover, gap="sm"))
    body = ui.columns(
        ("list", ui.slot("calls", call_list(ctx, feeds, feed))),
        ("main", ui.stack(header, ui.card(ui.slot("conv", conversation(ctx, call)), label="대화"), gap="lg")),
        ("aside", ui.slot("side", side(ctx, feed, call))))
    main = ui.live(f"/app/live?call={feed.id}", settings.LIVE_REFRESH_MS, body,
                   ui.listen_source("live-audio", f"/app/live/{feed.id}/audio", feed.audio_s) if rec is not None else None)
    return shell(ctx, "live", "실시간 통화", main, wide=True)
