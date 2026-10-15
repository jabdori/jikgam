"""관리 콘솔: 고객사 관리. 고객사 목록, 온보딩 체크, 설정으로 표현한 차이, 붙이면서 바꾼 엔진 코드(ADR 0021).

값은 조직 설정(config/orgs), 고객사 설정(config/web/tenants.toml), 저장소 파일의 존재, 조직 B 기록(docs/org-b.md §5)에서
읽는다."""

import re
from typing import Any

from .. import labels as lb
from ..data.tenants import CHANGES_SECTION, Tenant, engine_changes, impact_checked, onboarding
from ..ui import components as ui
from ..ui import fmt
from .common import Ctx, shell

CAUSE = {"company": "회사", "vendor": "외부 서비스"}


def _reasons(codes: list[str] | None) -> str:
    return ", ".join(lb.label(lb.QUOTE_REASON, c) or c for c in codes) if codes else "없음"


def diff_rows(tenants: list[Tenant]) -> list[tuple[Any, list[Any]]]:
    """고객사마다 다른 설정 값과 그 설정 키(config/orgs/<org>.toml). 모든 고객사가 같은 항목은 넣지 않는다."""
    def pol(t: Tenant) -> dict[str, Any]:
        return t.raw.get("policy") or {}

    def comp(t: Tenant) -> Any:
        c = pol(t).get("compensation") or {}
        if not c:
            return "없음"
        return ui.joined(ui.joined(fmt.count(c.get("min_hours"), "시간"), " 이상 중단", sep=""),
                         ui.joined("중단 시간 × ", fmt.count(c.get("multiplier")), " 연장", sep=""),
                         ", ".join(CAUSE.get(x, x) for x in c.get("causes", [])) + " 귀책")

    rows: list[tuple[str, str, Any]] = [
        ("청약철회·장애 환불 기간", "policy.window_days",
         lambda t: ui.joined("결제일로부터 ", fmt.count(pol(t).get("window_days"), "일"), sep="")),
        ("중복 결제로 볼 간격", "policy.duplicate_window_days",
         lambda t: ui.joined(fmt.count(pol(t).get("duplicate_window_days") or 0, "일"), " 이내", sep="")),
        ("장애 보상", "policy.compensation", comp),
        ("환불로 접수하는 사유", "policy.refundable_reasons", lambda t: _reasons(pol(t).get("refundable_reasons"))),
        ("법정대리인 요청 사유", "policy.guardian_reasons", lambda t: _reasons(pol(t).get("guardian_reasons"))),
        ("부가 상품만 해지", "policy.addon_plans", lambda t: ui.code(", ".join(pol(t).get("addon_plans") or [])) if pol(t).get("addon_plans") else "없음"),
        ("필수 안내", "required_notices", lambda t: ui.ordered(t.raw.get("required_notices") or [])),
        ("구어 확장 규칙 수", "retrieval.expansions",
         lambda t: fmt.count(len((t.raw.get("retrieval") or {}).get("expansions") or []), "개")),
    ]
    out = []
    for name, key, get in rows:
        vals = [get(t) for t in tenants]
        if len({str(v) for v in vals}) > 1:
            out.append((ui.stack(name, ui.code(key), gap="xs"), vals))
    return out


def change_groups(changes: list[dict[str, str]]) -> list[tuple[str, list[dict[str, str]]]]:
    """같은 분류의 행을 한 변경으로 묶는다(기록 문서는 파일마다 한 행이다)."""
    groups: dict[str, list[dict[str, str]]] = {}
    for c in changes:
        groups.setdefault(re.sub(r"^\([a-z]\)\s*", "", c["kind"]), []).append(c)
    return list(groups.items())


def page(ctx: Ctx, account_orgs: set[str]) -> Any:
    tenants = list(ctx.tenants.values())
    if not tenants:
        return shell(ctx, "tenants", "고객사 관리", ui.card(ui.empty("고객사가 없습니다.")))
    cur = ctx.tenants.get(ctx.query.get("org", "")) or tenants[-1]
    checks = {t.org: onboarding(ctx.root, t, account_orgs) for t in tenants}
    items = []
    for t in tenants:
        left = sum(1 for c in checks[t.org] if not c[0])
        items.append(ui.ListItem(t.name, f"/admin/tenants?org={t.org}", sub=ui.joined(t.service, ui.code(t.org)),
                                 status=ui.badge(ui.joined("온보딩 · 남은 항목 ", fmt.count(left, "개"), sep=""), "caution")
                                 if left else None, active=t.org == cur.org))
    picker = ui.item_list(items, label="고객사 목록", cards=True)
    board = ui.card(ui.check_list([(d, n, ui.code(note)) for d, n, note in checks[cur.org]], label=f"{cur.name} 온보딩"),
                    title=f"{cur.name} 온보딩", label="온보딩")
    groups = change_groups(engine_changes(ctx.root, cur))
    if cur.changes:
        files = sum(len(rows) for _, rows in groups)
        blocks = [ui.version_block(ui.text(kind, strong=True), *[ui.stack(ui.code(c["file"]), ui.text(c["what"], tone="muted", size="sm"),
                                                                          gap="xs") for c in rows],
                                   tone="negative" if "버그" in kind else "neutral") for kind, rows in groups]
        impact = ui.text(ui.joined("기존 고객사 동작: 바꾸지 않음(근거 ", ui.code(f"{cur.changes} {CHANGES_SECTION}"), ")", sep=""), size="sm") \
            if impact_checked(ctx.root, cur) else ui.text("기존 고객사 동작에 미친 영향은 기록이 없습니다.", tone="negative", size="sm")
        side = ui.text(ui.joined(ui.joined("변경 ", fmt.count(len(groups), "건"), sep=""), ui.joined("파일 ", fmt.count(files, "개"), sep=""),
                                 "목표는 변경 없음"), tone="caution" if groups else "positive")
        change_card = ui.card(*(blocks or [ui.text("바꾼 엔진 코드가 없습니다.", tone="muted")]), impact,
                              title="붙이면서 바꾼 엔진 코드", side=side, label="엔진 변경")
    else:
        change_card = ui.card(ui.text("기준 고객사입니다. 엔진은 이 고객사로 만들었습니다.", tone="muted", size="md"),
                              title="붙이면서 바꾼 엔진 코드", label="엔진 변경")
    diffs = diff_rows(tenants)
    cols = [ui.Col("항목", key=True)] + [ui.Col(ui.joined(t.name, ui.code(t.org), sep=" ")) for t in tenants]
    table = ui.card(ui.table(cols, [ui.Row([name, *vals]) for name, vals in diffs], caption="설정으로 표현한 차이", stack=True,
                             fixed=True, empty_text="고객사 사이에 다른 설정이 없습니다."),
                    title="설정으로 표현한 차이", side=ui.code("config/orgs/<org>.toml"), label="설정 차이", flush=True)
    main = ui.stack(ui.page_header("고객사 관리", "고객사마다 설정과 어댑터만으로 붙였는지 확인합니다."), picker,
                    ui.grid(board, change_card, min="panel"), table, gap="lg")
    return shell(ctx, "tenants", "고객사 관리", main, crumbs=[ui.Crumb("고객사 관리", "/admin/tenants"), ui.Crumb(cur.name)])
