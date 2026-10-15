"""고객사 콘솔: 운영 리포트. 통화 기록(jikgam/web/data/calls.py)을 기간으로 모아 센다. 정의는 docs/design/data.md."""

from collections import Counter
from typing import Any

from .. import settings
from ..data.calls import OUTCOME, Call
from ..data.tenants import calls_of
from ..ui import components as ui
from ..ui import fmt
from .calls import in_period, period_chips, period_days, period_text
from .common import CHANGED_LABEL, OUTCOME_TONE, Ctx, shell


def reasons(calls: list[Call], outcome: str) -> list[tuple[str, int]]:
    c = Counter(x.reason or OUTCOME[outcome] for x in calls if x.outcome == outcome)
    return sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))


def reason_card(title: str, rows: list[tuple[str, int]], total: int, empty_text: str, tone: str) -> Any:
    """사유별 건수. 막대는 모든 카드가 같은 기준(네 카드 중 가장 큰 건수)이라 카드끼리 길이를 비교할 수 있고, 색은 그 범주의
    처리 결과 색(배지·범례와 같은 색)이다."""
    body = ui.bar_list([(name, n, fmt.count(n, "건")) for name, n in rows], tone=tone, label=title, total=total) if rows \
        else ui.text(empty_text, tone="muted", size="md")
    return ui.card(body, title=title, label=title)


def page(ctx: Ctx) -> Any:
    days = period_days(ctx, default=settings.DEFAULT_PERIOD)
    calls, lo, hi = in_period(calls_of(ctx.root, ctx.tenant, ctx.allow_holdout), days)
    n = len(calls)
    by = Counter(c.outcome for c in calls)
    done = by["filed"] + by["answered"]
    cancels = sum(c.cancelled for c in calls)
    refunds = [c for c in calls if c.refunded]
    changed = [c for c in calls if c.changed]
    wrong = sum(c.wrong_total for c in calls)
    header = ui.page_header("운영 리포트", ui.joined(period_text(lo, hi), ui.joined("상담 ", fmt.count(n, "건"), sep="")),
                            actions=period_chips(ctx, "/app/report", days, {}))
    if not calls:
        return shell(ctx, "report", "운영 리포트", ui.stack(header, ui.card(ui.empty("이 기간에 상담이 없습니다.")), gap="lg"))

    kpis = ui.grid(
        ui.metric("AI가 끝까지 처리", fmt.pct(done / n),
                  ui.joined(fmt.count(n, "건"), " 중 ", fmt.count(done, "건"), sep="")),
        ui.metric("접수 건수(해지·환불 따로 셈)", fmt.count(cancels + len(refunds), "건"),
                  ui.joined(ui.joined("해지 ", fmt.count(cancels, "건"), sep=""), ui.joined("환불 ", fmt.count(len(refunds), "건"), sep=""),
                            ui.joined("환불 접수액 ", fmt.won(sum(c.refund_amount or 0 for c in refunds)), sep=""))),
        ui.metric(CHANGED_LABEL, fmt.count(len(changed), "건"),
                  ui.text(ui.joined("그중 잘못된 안내 ", fmt.count(wrong, "건"), sep=""), tone="negative", strong=True) if wrong
                  else ui.joined("그중 잘못된 안내 ", fmt.count(wrong, "건"), sep="")),
        ui.metric("평균 상담 시간", fmt.seconds(sum(c.duration_s for c in calls) / n),
                  ui.joined("고객 발화 평균 ", fmt.number(sum(c.customer_turns for c in calls) / n, digits=1), "회", sep="")),
    )
    order = [k for k in ("filed", "answered", "handoff", "hold", "dropped") if by[k]]
    parts = [ui.Part(OUTCOME[k], by[k], fmt.count(by[k], "건"), OUTCOME_TONE[k]) for k in order]
    label = ", ".join(f"{OUTCOME[k]} {fmt.count_text(by[k], '건')}" for k in order)
    outcome = ui.card(ui.stack_bar(parts, label=label), title="처리 결과", label="처리 결과")
    lists_by = {k: reasons(calls, k) for k in ("handoff", "hold", "answered", "dropped")}
    top = max((v for rows in lists_by.values() for _, v in rows), default=0)
    lists = ui.grid(
        reason_card("상담사 연결 사유", lists_by["handoff"], top, "상담사에게 연결한 상담이 없습니다.", OUTCOME_TONE["handoff"]),
        reason_card("보류 사유", lists_by["hold"], top, "보류한 상담이 없습니다.", OUTCOME_TONE["hold"]),
        reason_card(f"{OUTCOME['answered']}: 안내한 내용", lists_by["answered"], top, "안내만 하고 끝난 상담이 없습니다.",
                    OUTCOME_TONE["answered"]),
        reason_card(f"{OUTCOME['dropped']}: AI가 묻던 것", lists_by["dropped"], top, "중간에 끝난 상담이 없습니다.",
                    OUTCOME_TONE["dropped"])
        if by["dropped"] else None,
        min="half")
    return shell(ctx, "report", "운영 리포트", ui.stack(header, kpis, outcome, lists, gap="lg"))
