"""고객사 콘솔: 연동. 업무 시스템 연결과 허용 동작, 통화, 상담사 연결 조건, 데이터 보관(ADR 0021, docs/design/data.md).

값은 조직 설정(config/orgs), 고객사 설정(config/web/tenants.toml), 음성 설정(config/voice.toml), 약관에서 읽는다.
정하지 않은 값(전화번호, 데이터 보관 기간)은 "정하지 않음"으로 보인다.
"""


from typing import Any

from jikgam.engine.bizapi import READ_TOOLS, WRITE_TOOLS

from ..data import knowledge as kn
from ..data.tenants import article_label, terms_of, voice_config
from ..ui import components as ui
from .common import Ctx, shell

TOOLS = ("verify_identity", "get_customer", "list_subscriptions", "list_payments", "get_usage", "list_incidents",
         "quote_refund", "create_cancellation", "create_refund_request", "register_refund_account", "create_handoff")
TOOL_NAME = {"verify_identity": "본인 확인", "get_customer": "고객 조회", "list_subscriptions": "구독 목록 조회",
             "list_payments": "결제 내역 조회", "get_usage": "이용 기록 조회", "list_incidents": "장애 이력 조회",
             "quote_refund": "환불 금액 계산", "create_cancellation": "해지 접수", "create_refund_request": "환불 접수",
             "register_refund_account": "환불 계좌 등록", "create_handoff": "상담사 연결"}
LANG = {"ko-KR": "한국어"}
NOT_SET = "정하지 않음"


def condition(tool: str) -> tuple[str, bool]:
    """(실행 조건, 강조할지). 업무 API 클라이언트의 읽기·쓰기 구분과 엔진 정책을 따른다. 고객 동의가 필요한 동작만 색을 쓴다."""
    if tool == "verify_identity":
        return "항상", False
    if tool == "create_handoff":
        return "연결 조건에 해당할 때", False
    if tool in WRITE_TOOLS:
        return "고객 동의 후 실행", True
    return "본인 확인 후", False


def page(ctx: Ctx) -> Any:
    raw = ctx.tenant.raw
    tools = [t for t in TOOLS if t in READ_TOOLS | WRITE_TOOLS]
    tts = voice_config(ctx.root).get("tts") or {}
    arts = terms_of(ctx.root, ctx.tenant)
    handoff = arts.get(ctx.tenant.handoff_article or "")

    api = ui.card(
        ui.keyvals([("주소", ui.code(raw.get("bizapi_url", NOT_SET))),
                    ("응답이 없을 때", "한 번 다시 시도하고, 그래도 없으면 고객에게 알린 뒤 보류")], divided=True, wide_key=True),
        title="업무 시스템", sub="회원·결제·해지 시스템", label="업무 시스템")
    phone = ui.card(
        ui.keyvals([("음성 응대", "켜짐 · 전화 회선은 아직 없음" if ctx.tenant.voice else "꺼짐"),
                    ("전화번호", NOT_SET),
                    ("AI 목소리", ui.joined(tts.get("voice", NOT_SET), LANG.get(tts.get("language", ""), None))
                     if ctx.tenant.voice else "해당 없음"),
                    ("첫 인사", f"{NOT_SET} · 지금은 고객이 먼저 말합니다" if ctx.tenant.voice else "해당 없음")],
                   divided=True, wide_key=True),
        title="통화", label="통화")
    conds = kn.conditions(handoff.text) if handoff else []
    agent = ui.card(
        ui.ordered(conds, label="상담사 연결 조건") if conds else ui.text(NOT_SET, tone="muted"),
        ui.link(ui.joined("약관 ", article_label(handoff, ctx.tenant.handoff_article or ""), " 원문", sep=""),
                f"/app/knowledge?doc=terms&seg={handoff.id}") if handoff else None,
        title="상담사 연결 조건", label="상담사 연결 조건")
    keep = ui.card(ui.keyvals([("녹음·대화 보관 기간", NOT_SET)], wide_key=True), title="데이터 보관", label="데이터 보관")
    cols = [ui.Col("동작"), ui.Col("실행 조건")]
    rows = []
    for t in tools:
        text, strong = condition(t)
        rows.append(ui.Row([ui.stack(TOOL_NAME.get(t, t), ui.code(t), gap="xs"),
                            ui.badge(text, "caution") if strong else text]))
    table = ui.card(ui.table(cols, rows, caption="업무 시스템 동작", stack=True), title="AI가 할 수 있는 동작",
                    label="AI가 할 수 있는 동작", flush=True)
    header = ui.page_header("연동", "AI가 쓰는 업무 시스템과 통화 설정입니다.")
    return shell(ctx, "integrations", "연동",
                 ui.stack(header, ui.grid(api, phone, keep, min="wide"), agent, table, gap="lg"))
