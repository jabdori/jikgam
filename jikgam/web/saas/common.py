"""고객사 콘솔 화면들이 함께 쓰는 셸과 이름표."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..data.calls import Call
from ..data.live import Feeds
from ..data.tenants import Tenant, article_label, terms_of
from ..ui import components as ui
from ..ui import fmt

NOTE = "데모 자료: AI 고객과 나눈 합성 상담을 기록한 것입니다."
# 처리 결과의 색. 통화 기록의 배지와 운영 리포트의 막대가 같은 색을 쓴다.
OUTCOME_TONE = {"filed": "positive", "answered": "neutral", "handoff": "accent", "hold": "caution", "dropped": "muted"}
CHANGED_LABEL = "요청이 바뀐 상담"


@dataclass
class Ctx:
    """화면 하나를 그릴 때 필요한 것."""
    root: Path
    tenant: Tenant
    feeds: Feeds
    allow_holdout: bool
    query: dict[str, str]
    user: str
    knowledge_documents: Any = None


def shell(ctx: Ctx, active: str, title: str, main: Any, search: str | None = None, wide: bool = False) -> Any:
    live_n = len(ctx.feeds.of(ctx.tenant.org))
    nav = [
        ui.NavGroup("운영", [
            ui.NavItem("실시간 통화", "/app/live", active == "live", fmt.count(live_n) if live_n else None),
            ui.NavItem("통화 기록", "/app/calls", active == "calls"),
            ui.NavItem("운영 리포트", "/app/report", active == "report"),
        ]),
        ui.NavGroup("설정", [
            ui.NavItem("지식·정책", "/app/knowledge", active == "knowledge"),
            ui.NavItem("연동", "/app/integrations", active == "integrations"),
        ]),
    ]
    placeholder = "문서, 조항, 내용 검색" if active == "knowledge" else "고객 이름, 전화번호, 대화 내용 검색"
    action = "/app/knowledge" if active == "knowledge" else "/app/calls"
    return ui.shell(theme="saas", title=title, product="직감", nav=nav, main=main, note=NOTE,
                    user_initial="운", user_label="사용자 상담 운영팀", tenant=(ctx.tenant.name, ctx.tenant.desc),
                    search=(action, placeholder, search or ""), wide=wide,
                    search_fields=[("doc", ctx.query["doc"])] if active == "knowledge" and ctx.query.get("doc") else [])


def who(call: Call) -> str:
    if call.name:
        return f"{call.name} 고객"
    return "확인 전 고객"


def outcome_badge(call: Call) -> Any:
    return ui.badge(call.outcome_label, OUTCOME_TONE.get(call.outcome, "neutral"))


def article_names(ctx: Ctx, ids: list[str]) -> list[str]:
    arts = terms_of(ctx.root, ctx.tenant)
    return [article_label(arts.get(a), a) for a in ids]


def article_href(article_id: str) -> str:
    return f"/app/knowledge?doc=terms&seg={article_id}"


def article_short(article_id: str, ctx: Ctx) -> str:
    """"제7조"처럼 조 번호만(대화 줄 아래 근거 표시)."""
    return article_names(ctx, [article_id])[0].split(" ")[0]


def evidence_links(ctx: Ctx, ids: list[str]) -> Any:
    if not ids:
        return ui.text("근거 조항을 든 안내가 없습니다.", tone="muted", size="md")
    return ui.link_list([(f"이용약관 {n.replace(' ', ' · ', 1)}", article_href(a)) for a, n in zip(ids, article_names(ctx, ids))],
                        label="안내 근거 조항")

