"""관리 콘솔: 통화 리플레이. 음성을 재생하면 재생 목록(고객 발화, 판단, AI 발화, 끼어들기)이 따라 움직이고, 고른 판단의
조항 순위와 직전 대비 변화, 무효가 된 답과 새 답, 통화 전체 조항 × 판단 순위 표와 정답 일치도를 보여 준다
(ADR 0021, jikgam/web/data/replay.py). 이전·다음 판단은 문서를 새로 열지 않고 바뀐 부분만 맞추며(셸의 data-swap), 순위가 바뀐
조항 행은 새 자리로 옮겨 가고 재생 위치 선은 다음 판단 위치로 미끄러진다(static/console.js)."""

from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from .. import labels as lb
from .. import settings
from ..data import replay as rp
from ..ui import components as ui
from ..ui import fmt
from .common import Ctx, article_namer, shell


LIVE_PREFIX = "live/"  # results/voice/live/: 관리 콘솔 음성 시험 기록


def key(s: rp.Session) -> str:
    return f"{s.run}/{s.name}"


def short(s: rp.Session) -> str:
    """"voice-barge-C · s20 · 1회차 · 정책 C"처럼 실행, 시나리오, 회차, 정책만."""
    if s.run.startswith(LIVE_PREFIX):  # 관리 콘솔 음성 시험(평가 아님)
        return " · ".join(x for x in ("음성 시험", s.run.split("/")[-1], f"정책 {s.policy}" if s.policy else None) if x)
    rep = next((part[1:] for part in s.name.split("-") if part.startswith("r") and part[1:].isdigit()), None)
    return " · ".join(x for x in (s.run.split("/")[-1], s.scenario, f"{rep}회차" if rep else None,
                                  f"정책 {s.policy}" if s.policy else None) if x)


def href(s: rp.Session, at: int | None = None) -> str:
    return f"/admin/replay?s={quote(key(s), safe='/')}" + (f"&at={at}" if at is not None else "")


def pct(r: rp.Replay, t: float) -> float:
    """전체 길이 대비 위치(%)."""
    return rp.percent(t, r.duration_s)


def intent(code: str | None) -> str:
    return lb.label(lb.INTENT, code) or "판단 없음"


def picker(ctx: Ctx, sessions: list[rp.Session], current: rp.Session) -> Any:
    opts = [(key(s), short(s)) for s in sessions if not s.holdout]
    locked = sum(1 for s in sessions if s.holdout)
    return ui.form(ui.row(ui.field_select("s", "통화", opts, key(current)), ui.button("열기", type="submit", size="sm"),
                          ui.text(ui.joined("미공개 시나리오 실행 ", fmt.count(locked, "개"), "는 잠김", sep=""),
                                  tone="muted", size="sm") if locked and not ctx.allow_holdout else None,
                          align="end", gap="sm"), action="/admin/replay", method="get", label="통화 고르기")


def marks(r: rp.Replay, cur: rp.Decision, s: rp.Session) -> list[ui.Mark]:
    groups: list[list[rp.Decision]] = []
    for d in r.decisions:
        if groups and pct(r, d.at_s) - pct(r, groups[-1][0].at_s) < settings.REPLAY_MARK_MERGE_PCT:
            groups[-1].append(d)
        else:
            groups.append([d])
    out = []
    for g in groups:
        label = f"v{g[0].version}" if len(g) == 1 else f"v{g[0].version}–{g[-1].version}"
        current = any(d.index == cur.index for d in g)
        out.append(ui.Mark(pct(r, g[0].at_s), label, current, href(s, cur.index if current else g[0].index)))
    return out


def band(r: rp.Replay, cur: rp.Decision, s: rp.Session, audio: str | None) -> Any:
    idx = cur.index
    prev = ui.button("◀ 이전 판단", href=href(s, idx - 1), size="sm", disabled=idx <= 1, key="prev")
    nxt = ui.button("다음 판단 ▶", href=href(s, idx + 1), size="sm", disabled=idx >= len(r.decisions), key="next")
    stop = ui.toggle("stop", "판단 시점마다 멈춤", True, stop_toggle="yes")
    controls = ui.row(prev, nxt, ui.play_time(fmt.clock(cur.at_s, tenths=True), fmt.clock(r.duration_s, tenths=True)), stop,
                      gap="sm")
    lanes = ui.lanes(
        [ui.Lane("고객", [ui.Seg(pct(r, a), pct(r, b - a), "customer") for a, b, _ in r.customer]),
         ui.Lane("AI", [ui.Seg(pct(r, a), pct(r, b - a), "series-b" if ans.invalidated_at is not None and ans.cut else "series-a")
                        for a, b, ans in r.ai])],
        marks(r, cur, s), pct(r, cur.at_s),
        [(pct(r, t), fmt.clock(t, kind="axis")) for t in range(0, int(r.duration_s), settings.REPLAY_TICK_S)]
        + [(pct(r, r.duration_s), fmt.clock(r.duration_s))],
        label="통화 전체 위의 발화와 판단", cuts=[pct(r, t) for t, _, _ in r.barges],
        scrub=(r.duration_s, cur.at_s, settings.REPLAY_SCRUB_STEP_S) if audio else None)
    player = ui.player(audio, label="통화 녹음", start=cur.at_s, stops=[d.at_s for d in r.decisions], target="playlist",
                       controls=controls, native=False, decisions=[d.at_s for d in r.decisions], decision=cur.index,
                       decision_href=href(s) + "&at=") if audio \
        else ui.stack(ui.notice("이 통화의 녹음 파일이 없어 재생할 수 없습니다.", tone="caution"), controls, gap="sm")
    return ui.card(player, lanes, label="재생")


def playlist(r: rp.Replay, cur: rp.Decision, s: rp.Session) -> Any:
    items: list[tuple[float, ui.PlayItem]] = []
    for a, b, text in r.customer:
        items.append((a, ui.PlayItem(fmt.clock(a, tenths=True), "고객", "caution", text, seek=a)))
    for a, b, ans in r.ai:
        items.append((a, ui.PlayItem(fmt.clock(a, tenths=True), "AI", "accent", ans.text, seek=a)))
    for t, played, _ in r.barges:
        items.append((t, ui.PlayItem(fmt.clock(t, tenths=True), "끼어듦", "negative",
                                     ui.joined("AI 음성 ", fmt.seconds_fine(played), " 만에 중단", sep=""), seek=t)))
    for d in r.decisions:
        text = ui.joined("이전 답 무효" if d.invalidated else None, intent(d.intent), lb.label(lb.ACTION, d.action))
        items.append((d.at_s, ui.PlayItem(fmt.clock(d.at_s, tenths=True), f"v{d.version}",
                                          "positive" if d.index == cur.index else "neutral", text, href=href(s, d.index),
                                          current=d.index == cur.index)))
    items.sort(key=lambda x: x[0])
    out = []
    for i, (t, it) in enumerate(items):
        end = items[i + 1][0] if i + 1 < len(items) else r.duration_s
        it.cue = (t, max(end, t))
        it.past = t <= cur.at_s
        out.append(it)
    return ui.stack(ui.pane_head("재생 목록", demoted=True), ui.anchor("playlist", ui.playlist(out, label="재생 목록")), gap="xs")


def rank_card(r: rp.Replay, cur: rp.Decision, prev: rp.Decision | None, name: Callable[[str], Any]) -> Any:
    rows = [ui.RankRow(fmt.rank(x.rank), name(x.article), fmt.rank_delta(x.prev_rank, x.rank), x.p, fmt.prob(x.p), prev=x.prev_p,
                       prev_shown=fmt.prob(x.prev_p) if x.prev_p is not None else None, selected=x.selected, gold=x.gold,
                       key=x.article)
            for x in cur.ranks[:settings.REPLAY_RANK_ROWS]]
    side = ui.joined(f"v{prev.version} 대비" if prev else "첫 판단", "← 직전 확률", "세로선 = 선택 기준")
    return ui.card(ui.rank_bars(rows, label="조항 순위", threshold=r.threshold), title="조항 순위", side=side, label="조항 순위")


def answer_line(a: rp.Answer) -> Any:
    if a.start_s is None:
        played = "재생 전에 무효"
    elif a.cut:
        played = ui.joined(fmt.seconds_fine(a.played_s), " 재생 후 끼어들기로 중단", sep="")
    else:
        played = ui.joined(fmt.seconds_fine(a.played_s), " 끝까지 재생", sep="")
    return ui.joined(a.suggestion.rsplit("-", 1)[-1], f"v{a.basis} 기준", played)


def answers_card(cur: rp.Decision) -> Any:
    blocks = []
    for a in cur.invalidated:
        blocks.append(ui.version_block(ui.text(answer_line(a), tone="negative", size="sm", mono=True),
                                       ui.text(a.text, tone="muted", strike=True),
                                       ui.text(ui.joined("무효 사유", ui.joined(*[lb.invalidation_reason(x) for x in (
                                           [x for x in a.reasons if x != "suggestion_valid=false"] or a.reasons)]), sep=" "),
                                               tone="muted", size="sm"), tone="negative"))
    for a in cur.new:
        when = ui.joined("판단 ", fmt.seconds_fine(a.start_s - cur.at_s), " 뒤 재생 시작", sep="") if a.start_s is not None \
            else "재생되지 않음"
        blocks.append(ui.version_block(ui.text(ui.joined(a.suggestion.rsplit("-", 1)[-1], f"v{a.basis} 기준", when),
                                               tone="accent", size="sm", mono=True), a.text, tone="accent"))
    if not blocks:
        blocks.append(ui.text("이 판단에서 무효가 되거나 새로 만든 답이 없습니다.", tone="muted", size="md"))
    return ui.card(*blocks, title="답변 버전", label="답변 버전")


def heat_card(r: rp.Replay, cur: rp.Decision, name: Callable[[str], Any]) -> Any:
    seen: list[str] = list(r.gold)
    for d in r.decisions:
        for a in d.selected:
            if a not in seen:
                seen.append(a)
    best = sorted({x.article for d in r.decisions for x in d.ranks} - set(seen),
                  key=lambda a: -max(d.applies.get(a, 0.0) for d in r.decisions))
    rows_ids = (seen + best)[:settings.REPLAY_HEAT_ROWS]
    rows = []
    for a in rows_ids:
        cells = []
        for d in r.decisions:
            rk = next((x for x in d.ranks if x.article == a), None)
            cells.append(ui.HeatCell(None) if rk is None else ui.HeatCell(fmt.rank(rk.rank), rk.p, rk.selected))
        rows.append((ui.joined(name(a), ui.text("★", tone="caution") if a in r.gold else None, sep=" "), cells))
    cols = [f"v{d.version}" for d in r.decisions]
    foot = [("요청", [lb.label(lb.INTENT, d.intent) or "-" for d in r.decisions])]
    if r.gold:
        foot.append(("정답 일치", [ui.joined(fmt.count(d.agree), fmt.count(len(r.gold)), sep="/") for d in r.decisions]))
    note = ui.text(ui.joined("칸 숫자 = 그 판단의 순위, 색 = 적용 확률, 테두리 = 선택, · = 후보 아님",
                             "행 = 시나리오 정답 조항(★), 한 번이라도 선택된 조항, 확률이 높았던 조항 순",
                             ui.joined("정답 일치 = 정답 조항 중 상위 ", fmt.rank(settings.REPLAY_TOP_K), "위 안에 든 개수", sep="")
                             if r.gold else None), tone="muted", size="sm")
    return ui.card(ui.heatmap(cols, rows, foot, caption="조항 × 판단 순위 표", current_col=cur.index - 1), note,
                   title="통화 전체 순위 변화", label="통화 전체 순위 변화")


def page(ctx: Ctx) -> Any:
    sessions = rp.sessions(ctx.root, ctx.allow_holdout)
    want = ctx.query.get("s") or settings.REPLAY_DEFAULT
    run, _, name = want.rpartition("/")
    s = rp.find(ctx.root, run, name, ctx.allow_holdout)
    if s is None:
        main = ui.stack(ui.page_header("통화 리플레이"), ui.card(ui.empty("이 통화를 열 수 없습니다.",
                                                                     "없는 통화이거나 미공개 시나리오 실행입니다.")), gap="lg")
        return shell(ctx, "replay", "통화 리플레이", main)
    r = rp.build(ctx.root, s, ctx.allow_holdout)
    if not r.decisions:
        main = ui.stack(ui.page_header("통화 리플레이"), picker(ctx, sessions, s), ui.card(ui.empty("판단 기록이 없는 통화입니다.")), gap="lg")
        return shell(ctx, "replay", "통화 리플레이", main)
    try:
        at = int(ctx.query.get("at") or 0)
    except ValueError:
        at = 0
    default = next((d for d in r.decisions if any(a.cut for a in d.invalidated)), r.decisions[0])
    cur = next((d for d in r.decisions if d.index == at), default)
    prev = next((d for d in r.decisions if d.index == cur.index - 1), None)
    audio = f"/admin/replay/audio?s={quote(key(s), safe='/')}" if s.recording and s.recording.get("available") else None

    badges = []
    if prev is not None and prev.intent != cur.intent:
        badges.append(ui.badge(f"요청 변경 {intent(prev.intent)} → {intent(cur.intent)}", "caution"))
    meta = ui.text(ui.joined(ui.joined(cur.model or "판별", fmt.ms(cur.latency_ms), sep=" ") if cur.latency_ms is not None else None,
                             lb.label(lb.ACTION, cur.action),
                             {True: "정보 충분", False: "정보 부족", None: None}[cur.sufficient]), tone="muted", size="sm")
    head = ui.row(ui.heading(ui.joined(f"v{cur.version} 판단", fmt.clock(cur.at_s, tenths=True)), level=1), *badges, meta,
                  align="baseline", gap="sm")
    name = article_namer(ctx, r.org)
    main = ui.stack(head, ui.grid(rank_card(r, cur, prev, name), answers_card(cur), min="panel"), heat_card(r, cur, name),
                    gap="lg")
    body = ui.stack(
        ui.stack(picker(ctx, sessions, s), band(r, cur, s, audio), gap="sm"),
        ui.columns(("list-wide", playlist(r, cur, s)), ("main", main)), gap="lg")
    crumbs = [ui.Crumb("통화 리플레이", "/admin/replay"), ui.Crumb(r.title or "시나리오 정보 없음")]
    return shell(ctx, "replay", "통화 리플레이", ui.anchor("top", body), crumbs=crumbs)
