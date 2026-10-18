"""관리 콘솔: 음성 시험(/admin/voice-test). 브라우저 마이크로 직접 말하며 상담 엔진을 시험한다(jikgam/web/data/voicetest.py).

한 화면에 시작·종료, 대화(고객의 미확정 자막이 말하는 도중 바뀌고, AI 답은 재생에 맞춰 글자가 드러난다), 판단 로그(대화 버전,
판별 결과와 지연, 근거, 업무 API, 끼어들기, 폐기된 답과 사유), 시험자가 연기할 가상 고객 정보가 있다. 통화 중에는 세션 로그를
읽어 갱신한다. 이 PC와 같은 Tailscale 네트워크의 기기에서만 연다. 기록은 평가 수치에 쓰지 않는다.
"""

from typing import Any
from urllib.parse import quote, urlencode

from jikgam.engine import events as ev

from .. import labels as lb
from .. import settings
from ..data import voicetest as vt
from ..data.calls import build_call, mask_text
from ..data.live import speech_plan
from ..ui import components as ui
from ..ui import fmt
from .common import Ctx, shell

EXAMPLES = (
    "결제일 문의에서 해지로 바꾸기: \"다음 결제가 언제 되나요?\"라고 묻고, 답을 듣는 도중에 \"아니요, 그냥 해지할게요\"",
    "청약철회 환불: \"이번 달에 결제했는데 한 번도 안 썼어요. 환불받을 수 있나요?\"",
    "본인 확인: AI가 번호를 물으면 위의 휴대폰 번호를, 인증번호를 물으면 위의 인증번호를 말하기",
)
PLAN = {"monthly": "월 구독", "annual": "연 구독"}
METHOD = {"card": "카드", "phone": "휴대폰 결제", "bank": "계좌 이체"}
STATUS = {"연결 중": "accent", "통화 중": "positive", "끝내는 중": "caution", "끝남": "neutral"}
TRIGGER = {"vad": "말소리 감지", "transcript": "자막"}
KIND_TONE = {"판별": "accent", "끼어들기": "caution", "응답 보류": "caution", "폐기": "negative", "무효": "negative"}
HINT = {"연결 중": "음성 엔진을 준비하는 중입니다. '통화 중'으로 바뀌면 말씀하세요.",
        "통화 중": "말씀하세요. AI 음성은 이 브라우저 스피커로 나옵니다."}


def href(call: str | None, customer: str | None) -> str:
    q = {k: v for k, v in (("call", call), ("customer", customer)) if v}
    return "/admin/voice-test" + (f"?{urlencode(q)}" if q else "")


# --- 시험용 고객 -------------------------------------------------------------------------------------


def customer_card(customers: list[dict[str, Any]], cur: dict[str, Any] | None, call: str | None) -> Any:
    if cur is None:
        return ui.card(ui.empty("시험용 고객 자료를 읽지 못했습니다.", "domain/org_a/customers.json을 확인하세요."),
                       title="시험용 고객", label="시험용 고객")
    pick = ui.form(ui.hidden("call", call) if call else None,
                   ui.row(ui.field_select("customer", "연기할 고객", [(c["id"], f"{c['name']} · {c['id']}") for c in customers],
                                          cur["id"]), ui.button("고르기", type="submit", size="sm"), align="end", gap="sm"),
                   action="/admin/voice-test", method="get", label="시험용 고객 고르기")
    sub, pay = cur["subscription"], cur["payment"]
    rows = [("이름", cur["name"]), ("휴대폰 번호", ui.code(cur["phone"] or "-")),
            ("인증번호", ui.joined(ui.code(cur["otp"] or "-"), ui.text("모의 업무 API 규칙: 휴대폰 번호 끝 네 자리", tone="muted", size="sm"),
                                 sep=" ")) if cur["otp"] else ("인증번호", "-"),
            ("구독", ui.joined(PLAN.get(sub["plan"], sub["plan"]), fmt.won(sub.get("price")),
                             ui.joined("다음 결제일", fmt.date_(sub.get("next_billing")), sep=" ")) if sub else "없음"),
            ("최근 결제", ui.joined(fmt.date_(pay.get("date")), fmt.won(pay.get("amount")),
                                METHOD.get(pay.get("method"), pay.get("method"))) if pay else "없음")]
    return ui.card(pick, ui.keyvals(rows), ui.subheading("이렇게 말해 보세요"), ui.ordered(list(EXAMPLES), label="예시 질문"),
                   title="시험용 고객", label="시험용 고객",
                   sub="모의 업무 API 자료의 가상 고객입니다. 이 사람인 것처럼 말하면 본인 확인과 조회가 그대로 돕니다.")


# --- 대화 --------------------------------------------------------------------------------------------


def interim(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """아직 확정되지 않은 고객 말의 마지막 미확정 자막."""
    finals = {e.get("utt_id") for e in events if e["type"] == "stt.final"}
    part = next((e for e in reversed(events) if e["type"] == "stt.partial"), None)
    return part if part is not None and part.get("utt_id") not in finals else None


def conversation(call: vt.TestCall, events: list[dict[str, Any]], now: float | None) -> Any:
    c = build_call(events, call.id, None, now_ms=now)
    plan = speech_plan(events)
    changes = {ch.line_index: ch for ch in c.changes}
    items: list[Any] = []
    for i, ln in enumerate(c.lines):
        sp = plan.get(ln.sid) if ln.sid else None
        text: Any = ln.text
        reveal = None
        if sp is not None and ln.playing:
            reveal = ui.Reveal(ln.text[:sp.chars], sp.start_ms, sp.end_ms, sp.shown(now))
        elif sp is not None and sp.cut:
            text = ln.text[:sp.chars] + "…"  # 고객이 들은 데까지만
        who = "고객" if ln.kind == "customer" else ("AI · 말하는 중" if ln.playing else "AI")
        notes = [ui.joined("고객이 끼어들어", fmt.seconds_fine(ln.played_s, digits=1), "만에 멈춤", sep=" ") if ln.cut else None,
                 ui.joined("근거", ", ".join(ln.articles), sep=" ") if ln.articles else None]
        items.append(ui.Turn(who, text, fmt.clock(ln.elapsed_s), ln.kind, current=ln.playing,
                             note=ui.joined(*notes) if any(notes) else None, key=ln.key, reveal=reveal))
        if i in changes:
            ch = changes[i]
            items.append(ui.Divider(ui.joined("요청 변경", ui.joined(lb.label(lb.INTENT, ch.before) or "-",
                                                                   lb.label(lb.INTENT, ch.after) or "-", sep=" → "), sep=": "),
                                    key=ch.key))
    part = interim(events)
    if part is not None and now is not None:
        items.append(ui.Turn("고객 · 말하는 중", mask_text(part.get("text", "")), None, "customer", current=True,
                             key=f"partial:{part.get('utt_id')}"))
    if not items:
        return ui.empty("말을 시작하면 여기에 대화가 보입니다.")
    return ui.conversation(items, now_ms=now, running=now is not None, follow=True)


# --- 판단 로그 --------------------------------------------------------------------------------------


def _p(answer: Any) -> float | None:
    if not isinstance(answer, dict):
        return None
    return answer.get("p_true", answer.get("p"))


def log_rows(events: list[dict[str, Any]], t0: float) -> list[ui.Row]:
    rows = []
    for e in events:
        t, kind, body = e["type"], None, None
        if t == ev.VERSION_BUMPED:
            kind, body = "대화 버전", ui.joined(ui.joined(f"v{e.get('from')}", f"v{e.get('to')}", sep=" → "),
                                             lb.label(lb.BUMP_CAUSE, e.get("cause")))
        elif t == "decision.response":
            a = e.get("answers") or {}
            intent = (a.get("intent") or {}).get("label") if isinstance(a.get("intent"), dict) else None
            changed = _p(a.get("condition_changed"))
            kind = "판별"
            body = ui.joined(ui.joined(e.get("model") or e.get("backend"), fmt.ms(e.get("latency_ms")), sep=" "),
                             ui.joined("요청", lb.label(lb.INTENT, intent), sep=" ") if intent else None,
                             ui.joined("조건 변경", fmt.prob(changed), sep=" ") if changed is not None else None,
                             ui.text(e["error"], tone="negative") if e.get("error") else None)
        elif t == ev.EVIDENCE_UPDATED:
            applies = sorted((e.get("applies") or {}).items(), key=lambda kv: -kv[1])[:settings.VOICE_TEST_TOP_ARTICLES]
            kind = "근거"
            body = ui.joined(ui.joined("적용 조항 상위", ", ".join(a for a, _ in applies) or "-", sep=" "),
                             ui.joined(*[fmt.prob(p) for _, p in applies], sep=" / ") if applies else None,
                             ui.joined("고름", ", ".join(e.get("articles") or []) or "없음", sep=" "),
                             {True: "정보 충분", False: "정보 부족"}.get(e.get("sufficient")),
                             lb.label(lb.ACTION, e.get("action")))
        elif t == ev.TOOL_RESULT:
            kind = "업무 API"
            body = ui.joined(ui.joined(lb.label(lb.TOOL, e.get("tool")), ui.code(e.get("tool") or "-"), sep=" "),
                             "성공" if e.get("ok") else ui.text("실패", tone="negative"),
                             fmt.ms(e.get("latency_ms")))
        elif t == "voice.barge_in":
            kind = "끼어들기"
            body = ui.joined(TRIGGER.get(e.get("trigger"), e.get("trigger")),
                             ui.joined("AI 음성", fmt.seconds_fine(vt.seconds(e.get("played_ms"))), "만에 멈춤", sep=" "))
        elif t == ev.RESULT_DISCARDED:
            kind = "폐기"
            stage = "음성 재생" if e.get("stage") == "voice" else lb.label(lb.STAGE, e.get("stage"))
            body = ui.joined(ui.joined(stage, "결과", sep=" "),
                             f"v{e.get('started_version')}에서 시작했으나 대화 버전이 바뀜",
                             e.get("suggestion_id", "").rsplit("-", 1)[-1] or None)
        elif t == "suggestion.invalidated":
            kind = "무효"
            body = ui.joined(e.get("suggestion_id", "").rsplit("-", 1)[-1],
                             *[lb.invalidation_reason(r) for r in e.get("reasons") or []])
        elif t == "voice.reply_held":
            kind = "응답 보류"
            body = e.get("reason") or "고객이 말하는 중"
        if kind is None:
            continue
        rows.append(ui.Row([fmt.clock(vt.seconds(float(e["t_ms"]) - t0), tenths=True), f"v{e.get('version')}",
                            ui.badge(kind, KIND_TONE.get(kind, "neutral")), body],
                           key=f"{t}:{e['t_ms']}"))
    return rows


def decision_log(events: list[dict[str, Any]], t0: float) -> Any:
    rows = log_rows(events, t0)
    return ui.table([ui.Col("시각", num=True), ui.Col("버전"), ui.Col("종류"), ui.Col("내용")], rows, caption="판단 로그",
                    empty_text="판단이 생기면 시각순으로 쌓입니다.", row_header=False, stack=True)


# --- 화면 --------------------------------------------------------------------------------------------


def status_line(call: vt.TestCall, events: list[dict[str, Any]], now: float | None) -> Any:
    tone = STATUS.get(call.status, "neutral")
    start = call.started_ms
    elapsed = vt.seconds(now - start) if now is not None and start is not None else None
    left = settings.VOICE_TEST_MAX_S - elapsed if elapsed is not None else None
    replay = f"/admin/replay?s={quote(f'live/{call.id}/{call.session_id}', safe='/')}"
    return ui.row(ui.badge(call.status, tone, dot=call.running),
                  ui.text(HINT[call.status]) if call.status in HINT else None,
                  ui.text(ui.joined(ui.joined("경과", fmt.clock(elapsed), sep=" ") if elapsed is not None else None,
                                    ui.joined("남은 시간", fmt.clock(max(left, 0)), sep=" ") if left is not None else None,
                                    call.reason), tone="muted", size="sm"),
                  ui.code(call.session_id),
                  ui.button("리플레이에서 열기", href=replay, size="sm") if not call.running else None, gap="sm")


def controls(ctx: Ctx, call: vt.TestCall | None, customer: str | None) -> Any:
    url = vt.bizapi_url()
    api_up, webrtc = vt.bizapi_up(url), vt.webrtc_available()
    cost = vt.cost_per_min(ctx.root)
    running = bool(call and call.running)
    notes = []
    if not api_up:
        notes.append(ui.notice(ui.joined(ui.joined("모의 업무 API", ui.code(url), "가 꺼져 있어 시작할 수 없습니다.", sep=" "),
                                         ui.joined("새 터미널에서", ui.code("cd bizapi && go run ./cmd/bizapi"),
                                                   "를 실행한 뒤 이 화면을 새로 고치세요.", sep=" "), sep=" "), tone="caution"))
    if not webrtc:
        notes.append(ui.notice(ui.joined("브라우저 음성 연결 모듈(aiortc)이 없습니다.", ui.code("uv sync"), "로 설치하세요.", sep=" "),
                               tone="caution"))
    cost_note = ui.text(ui.joined(ui.joined("분당 예상 비용 약", fmt.usd(cost["usd_per_min"], digits=settings.VOICE_TEST_COST_DIGITS), sep=" "),
                                  ui.joined("음성 기록", ui.code(cost["source"]), "의 엔진·STT·TTS 추정 합 기준, 청구 금액 아님", sep=" ")),
                        tone="muted", size="sm") if cost else ui.text("분당 예상 비용: 비교할 음성 기록이 없어 계산하지 못했습니다.",
                                                                     tone="muted", size="sm")
    limit = ui.text(ui.joined("최대", fmt.clock(settings.VOICE_TEST_MAX_S, kind="setting"), sep=" "), tone="muted", size="sm")
    messages = [("connecting", "마이크를 열고 연결하는 중입니다.", "accent"),
                ("denied", "마이크를 쓸 수 없습니다. 브라우저의 마이크 권한을 확인하세요.", "negative"),
                ("failed", "연결하지 못했습니다. 모의 업무 API와 서버 로그를 확인하세요.", "negative"),
                ("ended", "시험 통화를 끝냈습니다.", "neutral")]
    box = ui.voice_controls("시작", "종료", "/admin/voice-test/offer", "/admin/voice-test/stop", customer,
                            call.id if call else None, running, api_up and webrtc, messages, limit)
    return ui.card(box, cost_note, *notes, title="시험 통화", label="시험 통화",
                   sub="이어폰 없이 써도 됩니다. 브라우저가 에코 제거를 켜서 AI 목소리를 말로 받지 않게 합니다.")


def page(ctx: Ctx, tests: vt.VoiceTests) -> Any:
    customers = vt.customers(ctx.root)
    want = ctx.query.get("customer") or settings.VOICE_TEST_CUSTOMER
    cur = next((c for c in customers if c["id"] == want), customers[0] if customers else None)
    call = tests.find(ctx.query.get("call"))
    cid = cur["id"] if cur else None
    side = customer_card(customers, cur, call.id if call else None)
    parts: list[Any] = [controls(ctx, call, cid)]
    if call is not None:
        events = vt.read_events(call.log_path)
        now = ev.now_ms() if call.running else None
        t0 = float(events[0]["t_ms"]) if events else 0.0
        view = ui.stack(ui.slot("state", status_line(call, events, now)),
                        ui.card(ui.slot("conv", conversation(call, events, now)), title="대화", label="대화"),
                        ui.card(ui.slot("log", decision_log(events, t0)), title="판단 로그", label="판단 로그"), gap="lg")
        parts.append(ui.live(href(call.id, cid), settings.LIVE_REFRESH_MS, view) if call.running else view)
    else:
        parts.append(ui.card(ui.empty("시작을 누르면 대화와 판단 로그가 여기에 쌓입니다.",
                                      "처음에는 브라우저가 마이크 권한을 묻습니다."), title="대화", label="대화"))
    main = ui.stack(ui.page_header("음성 시험", "브라우저 마이크로 직접 말하며 상담 엔진을 시험합니다. 이 PC와 같은 Tailscale 네트워크에서만 열리고, "
                                               "기록(results/voice/live/)은 평가 수치에 쓰지 않습니다."),
                    ui.split(ui.stack(*parts, gap="lg"), side), gap="lg")
    return shell(ctx, "voice-test", "음성 시험", main)
