"""관리 콘솔 화면들이 함께 쓰는 셸."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..data.tenants import Tenant
from ..ui import components as ui

NOTE = "직감 팀 내부용 · 고객사에는 보이지 않습니다."


@dataclass
class Ctx:
    root: Path
    tenants: dict[str, Tenant]
    allow_holdout: bool
    query: dict[str, str]
    user: str
    jobs: Any = None   # 벤치마크 실행 관리(jikgam/web/data/benchmarks.py의 Jobs)
    message: str | None = None


def shell(ctx: Ctx, active: str, title: str, main: Any, crumbs: list[ui.Crumb] | None = None, wide: bool = False) -> Any:
    nav = [
        ui.NavGroup("엔진", [ui.NavItem("모델·벤치마크", "/admin/models", active == "models"),
                             ui.NavItem("통화 리플레이", "/admin/replay", active == "replay"),
                             ui.NavItem("음성 시험", "/admin/voice-test", active == "voice-test")]),
        ui.NavGroup("고객사", [ui.NavItem("고객사 관리", "/admin/tenants", active == "tenants")]),
    ]
    return ui.shell(theme="admin", title=title, product="직감", nav=nav, main=main, note=NOTE,
                    user_initial=(ctx.user[:1] or "관").upper(), user_label=f"사용자 {ctx.user}", admin_badge=True,
                    crumbs=crumbs or [ui.Crumb(title)], wide=wide)
