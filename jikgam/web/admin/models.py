"""관리 콘솔: 모델·벤치마크. 모델 등록(설정 파일 하나), 벤치마크 실행(확인 후), 결과 비교(ADR 0021,
jikgam/web/data/benchmarks.py). 판정 기준은 사전에 고정한 docs/evaluation.md §7, docs/evaluation-voice.md §5를 따른다."""

from typing import Any

from .. import settings
from ..data import benchmarks as bm
from ..ui import components as ui
from ..ui import fmt
from .common import Ctx, shell
from . import retrieval

VERDICT_TONE = {"통과": "positive", "기준선": "neutral", "기준선 없음": "neutral"}



def verdict_badges(verdict: str, failed: list[str]) -> Any:
    """판정 배지. 못 미친 조건은 모두 같은 모양의 빨간 배지로 나란히 둔다."""
    if failed:
        return ui.row(*[ui.badge(f, "negative") for f in failed], gap="xs")
    return ui.badge(verdict, VERDICT_TONE.get(verdict, "neutral"))


def decision_card(ctx: Ctx) -> Any:
    rows = bm.decision_rows(ctx.root)
    test = [r for r in rows if r.split == "test"]
    cols = [ui.Col("모델"), ui.Col("어댑터", hide_narrow=True), ui.Col("분할"), ui.Col("정확도", num=True),
            ui.Col("p95", num=True), ui.Col("1회 비용", num=True), ui.Col("사전 기준")]
    trs = []
    for r in rows:
        name = ui.stack(r.backend, ui.code(r.model) if r.model else None,
                        ui.text(ui.joined("실행 ", r.run, sep=""), tone="muted", size="sm", mono=True)
                        if not test or r.run != test[0].run else None, gap="xs")
        p95 = ui.text(fmt.ms(r.p95_ms), tone="negative" if "지연 초과" in r.failed else None)
        if r.mixed_p95 is not None:
            p95 = ui.stack(ui.joined(p95, ui.text("혼자 서빙", tone="muted", size="sm"), sep=" "),
                           ui.text(ui.joined("간헐(실행 전체) ", fmt.ms(r.mixed_p95), sep=""), tone="muted", size="sm"), gap="xs")
        acc = ui.text(fmt.pct(r.accuracy), tone="negative" if "정확도 미달" in r.failed else None)
        cost = ui.text("자체 GPU", tone="muted", size="sm") if r.local and not r.cost else \
            ui.text(fmt.usd(r.cost, digits=settings.COST_DIGITS), tone="negative" if "비용 초과" in r.failed else None)
        trs.append(ui.Row([name, r.adapter, r.split, acc, p95, cost, verdict_badges(r.verdict, r.failed)]))
    side = ui.joined(ui.joined("실행 ", test[0].run, sep=""), ui.joined("판단 ", fmt.count(test[0].decisions, "개"), sep="")) \
        if test else None
    c1, c3 = bm.CLAIM1, bm.CLAIM3
    note = ui.text(ui.joined(
        "사전 기준(", bm.CRITERIA_DOC, "): 판별 API는 같은 실행의 ", bm.GENERATIVE_BASE, " 대비 정확도 −",
        fmt.pct(c1["acc_drop"], digits=0, kind="criterion"), "p 이상, p95가 그 ", fmt.pct(c1["p95_ratio"], digits=0, kind="criterion"),
        " 이하이면서 ", fmt.ms(c1["p95_ms"], kind="criterion"), " 이하, 비용이 그 ", fmt.pct(c1["cost_ratio"], digits=0, kind="criterion"),
        " 이하. 로컬 서버는 ", bm.DECISION_BASE, " 대비 정확도 −", fmt.pct(c3["acc_drop"], digits=0, kind="criterion"),
        "p 이상, 혼자 서빙한 p95 ", fmt.ms(c3["p95_ms"], kind="criterion"), " 이하. 로컬 서버는 호출 과금이 없어 1회 비용을 적지 않는다. ",
        "화면에서 시작한 실행은 맨 위 표에 상태가 보이고, 끝나면 그 결과가 val 행으로 들어온다.",
        sep=""), tone="muted", size="sm")
    return ui.card(ui.table(cols, trs, caption="판별 모델 결과", stack=True, empty_text="판별 결과가 없습니다."),
                   ui.table_footer(note), title="판별 모델 결과", side=side, label="판별 모델 결과", flush=True)


def tuned_card(ctx: Ctx) -> Any:
    items = bm.tuned_latency(ctx.root)
    if not items:
        return None
    blocks = []
    for t in items:
        with_base = [(rep, p95) for rep, p95, others in t.reps if bm.GENERATIVE_BASE in others]
        rows = [ui.Row([fmt.rank(rep), fmt.ms(p95, digits=1), ", ".join(others),
                        "생성형 기준선과 번갈아" if bm.GENERATIVE_BASE in others else "판별 API와만 번갈아"])
                for rep, p95, others in t.reps]
        lo, hi = (min(p for _, p in with_base), max(p for _, p in with_base)) if with_base else (None, None)
        blocks.append(ui.stack(
            ui.subheading(t.backend),
            ui.grid(ui.metric("연속 호출 p95", fmt.ms(t.continuous_p95, digits=1), ui.joined("혼자 돈 실행 ", t.continuous_run or "-", sep="")),
                    ui.metric("간헐 호출 p95(생성형 기준선과 번갈아 돈 회차)",
                              ui.joined(fmt.ms(lo), fmt.ms(hi), sep="~") if with_base else fmt.DASH,
                              ui.joined(ui.joined("회차 ", ", ".join(fmt.count_text(r) for r, _ in with_base), sep=""),
                                        ui.joined("실행 ", t.intermittent_run or "-", sep=""),
                                        ui.joined("실행 전체 p95 ", fmt.ms(t.intermittent_p95, digits=1), sep=""))),
                    min="card"),
            ui.table([ui.Col("회차", num=True), ui.Col("회차 p95", num=True), ui.Col("함께 돈 백엔드"), ui.Col("조건")], rows,
                     caption=f"{t.backend} 간헐 호출 회차별 p95", row_header=False, stack=True) if rows else None, gap="sm"))
    return ui.card(*blocks, title="튜닝 모델 지연: 연속 호출과 간헐 호출", label="튜닝 모델 지연")


def policy_table(rows: list[bm.PolicyRow]) -> Any:
    judged = any(r.verdict for r in rows)
    cols = [ui.Col("방식"), ui.Col("상담 통과(유효 실행)", num=True), ui.Col("근거 갱신 p95(조건 변경)", num=True),
            ui.Col("이전 답 노출(노출/대상)", num=True), ui.Col("사전 기준")]
    trs = []
    for r in rows:
        refresh = ui.joined(fmt.ms(r.refresh_p95), ui.text(ui.joined("미갱신 ", fmt.count(r.refresh_misses), sep=""),
                                                          tone="muted", size="sm") if r.refresh_misses else None, sep=" ")
        if r.verdict:
            verdict = ui.stack(verdict_badges(r.verdict, [f"미달: {f}" for f in r.failed]),
                               *[ui.text(d, tone="muted", size="sm") for d in r.failed_detail], gap="xs")
        else:  # 판정한 묶음 안에서 판정이 없는 행은 비교 기준(정책 A)이다
            verdict = ui.text("비교 기준" if judged else fmt.DASH, tone="muted", size="sm")
        trs.append(ui.Row([ui.stack(r.label, ui.text(ui.joined(f"정책 {r.policy}", ", ".join(r.runs)), tone="muted", size="sm",
                                                     mono=True), gap="xs"),
                           ui.joined(fmt.pct(r.passed / r.valid if r.valid else None),
                                     ui.text(ui.joined(fmt.count(r.passed), fmt.count(r.valid), sep="/"), tone="muted", size="sm"),
                                     sep=" "),
                           refresh, ui.joined(fmt.count(r.stale), fmt.count(r.stale_den), sep=" / "), verdict]))
    return ui.table(cols, trs, caption="상담 방식 결과", stack=True)


def policy_card(ctx: Ctx) -> Any:
    rows = bm.policy_rows(ctx.root, ctx.allow_holdout)
    groups: dict[tuple[str, str], list[bm.PolicyRow]] = {}
    for r in rows:
        groups.setdefault((r.channel, r.scenarios), []).append(r)
    parts: list[Any] = []
    for (channel, scen), rs in groups.items():
        reps = rs[0].reps
        parts.append(ui.stack(ui.subheading(ui.joined(channel, scen), side=ui.joined(
            ui.joined("세션 ", fmt.count(sum(x.sessions for x in rs)), sep=""),
            ui.joined("시나리오마다 ", fmt.count(reps, "회"), sep="") if reps else None)), policy_table(rs), gap="xs"))
    if not parts:
        parts.append(ui.empty("상담 결과가 없습니다."))
    notes = [f"음성 미공개 시나리오의 사전 기준은 {bm.VOICE_CRITERIA_DOC}다. 채팅 개발용 시나리오는 판정 대상이 아니다.",
             "미갱신은 다음 고객 발화 전까지 근거를 다시 고르지 못한 조건 변경이다. 이전 답 노출의 대상은 조건 변경 뒤에 시작된 노출이다."]
    if not ctx.allow_holdout:
        notes.append("채팅 미공개 시나리오 결과는 서버를 --allow-holdout으로 띄울 때 계산한다.")
    return ui.card(*parts, ui.text(" ".join(notes), tone="muted", size="sm"), title="상담 방식 결과", label="상담 방식 결과")


def load_card(ctx: Ctx) -> Any:
    rows = bm.load_rows(ctx.root)
    if not rows:
        return ui.card(ui.text("부하 측정 결과가 없습니다. 벤치마크 실행에서 부하를 고르면 여기에 보입니다.", tone="muted", size="md"),
                       title="부하", label="부하")
    cols = [ui.Col("모델"), ui.Col("동시 요청", num=True), ui.Col("호출", num=True), ui.Col("p50", num=True),
            ui.Col("p95", num=True), ui.Col("오류율", num=True), ui.Col("초당 호출", num=True)]
    trs = [ui.Row([ui.stack(r.backend, ui.text(r.run, tone="muted", size="sm", mono=True), gap="xs"), fmt.count(r.concurrency),
                   fmt.count(r.calls), fmt.ms(r.p50), fmt.ms(r.p95), fmt.pct(r.errors / r.calls if r.calls else None),
                   fmt.number(r.per_s, digits=1)]) for r in rows]
    return ui.card(ui.table(cols, trs, caption="부하 측정", stack=True), title="부하", label="부하", flush=True)


def jobs_card(ctx: Ctx) -> Any:
    jobs = ctx.jobs.recent() if ctx.jobs else []
    if not jobs:
        return None
    tone = {"대기": "neutral", "실행 중": "accent", "끝남": "positive", "실패": "negative"}
    trs = [ui.Row([ui.stack(j.model, ui.text(j.id, tone="muted", size="sm", mono=True), gap="xs"), bm.SUITES[j.suite][0],
                   ui.badge(j.status, tone[j.status]), j.note or fmt.DASH, ui.code(j.log.relative_to(ctx.root).as_posix()) if j.log else fmt.DASH])
           for j in jobs]
    cols = [ui.Col("모델"), ui.Col("벤치마크"), ui.Col("상태"), ui.Col("메모"), ui.Col("로그")]
    return ui.card(ui.table(cols, trs, caption="벤치마크 실행", stack=True), title="이 서버에서 시작한 벤치마크",
                   label="벤치마크 실행", flush=True)


def suite_checks() -> Any:
    return ui.field_checks("suite", "실행할 벤치마크",
                           [(k, ui.joined(ui.text(name, strong=True), ui.text(desc, tone="muted", size="sm"), sep=" "), True)
                            for k, (name, desc, _) in bm.SUITES.items()],
                           help="test 분할과 미공개 시나리오는 화면에서 실행하지 않습니다.")


def add_form(ctx: Ctx) -> Any:
    q = ctx.query
    paid = "decision_api openai"
    return ui.form(
        ui.field_text("name", "이름", q.get("name"), mono=True, required=True, placeholder="영어 소문자·숫자"),
        ui.field_select("adapter", "어댑터", list(bm.ADAPTERS.items()), q.get("adapter") or "local"),
        ui.field_text("url", "엔드포인트", q.get("url"), mono=True, required=True),
        ui.field_text("model", "모델 ID", q.get("model"), mono=True, required=True, help="외부 모델은 버전 ID를 고정합니다."),
        ui.field_text("key_env", "키 환경 변수 이름", q.get("key_env"), mono=True, show_for=paid,
                      help="키 값은 넣지 않습니다. 저장소 밖 .env에 둔 변수 이름만 적습니다."),
        ui.field_text("price_in", "입력 단가(USD/1M 토큰)", q.get("price_in"), mono=True, show_for=paid),
        ui.field_text("price_out", "출력 단가(USD/1M 토큰)", q.get("price_out"), mono=True, show_for=paid),
        ui.button("등록", type="submit", kind="primary", size="lg"),
        action="/admin/models", label="모델 추가")


def run_form(ctx: Ctx, names: list[tuple[str, str]]) -> Any:
    pick = ctx.query.get("model") or (names[0][0] if names else "")
    return ui.form(
        ui.field_select("model", "모델", names, pick),
        suite_checks(),
        ui.row(ui.field_text("concurrency", "부하: 동시 요청", str(settings.LOAD_CONCURRENCY), mono=True),
               ui.field_text("duration", "부하: 시간(초)", str(settings.LOAD_DURATION_S), mono=True), gap="sm"),
        ui.button("예상 비용 보기", type="submit", kind="primary", size="lg"),
        action="/admin/benchmarks", label="벤치마크 실행")


def confirm_card(ctx: Ctx, model: str, suites: list[str], concurrency: int, duration_s: float) -> Any:
    ests = bm.estimates(ctx.root, model, suites, concurrency, duration_s)
    rows = []
    for e in ests:
        if e.suite == "decisions":
            how = ui.joined("val 사례 ", fmt.count(e.cases, "개"), " × ", fmt.count(e.reps, "회"), sep="")
        elif e.suite == "load":
            how = ui.joined("동시 ", fmt.count(e.concurrency, "개"), " × ", fmt.seconds(e.duration_s),
                            " 동안 응답마다 다음 요청 · ", fmt.count(settings.COST_PER_N_CALLS, "회당 ", kind="criterion"),
                            fmt.usd(e.per_call * settings.COST_PER_N_CALLS), sep="")
        else:
            how = ui.joined(fmt.count(e.sessions, "세션"), ui.joined("기준 ", e.source or fmt.DASH, sep=""))
        rows.append(ui.Row([bm.SUITES[e.suite][0], how, fmt.count(e.calls, "회") if e.calls is not None else "응답 시간에 따름",
                            fmt.usd(e.model_cost) if e.model_cost is not None else fmt.DASH,
                            fmt.usd(e.other_cost) if e.other_cost is not None else fmt.DASH]))
    total = sum(x for e in ests for x in (e.model_cost, e.other_cost) if x is not None)
    hidden = [ui.hidden("model", model), ui.hidden("confirm", "yes"), ui.hidden("concurrency", str(concurrency)),
              ui.hidden("duration", str(duration_s))] + [ui.hidden("suite", s) for s in suites]
    cols = [ui.Col("벤치마크"), ui.Col("계산 근거"), ui.Col("이 모델 호출", num=True), ui.Col("이 모델 비용", num=True),
            ui.Col("다른 비용", num=True)]
    return ui.card(
        ui.table(cols, rows, caption="예상 호출과 비용", stack=True),
        ui.notice(ui.joined("예상 합계 ", fmt.usd(total), " · 다른 비용은 고객 AI, 생성, 판정, 음성 인식·합성의 이전 실행 값입니다. "
                            "부하는 호출 수가 응답 속도에 따라 달라 합계에 넣지 않았습니다.", sep=""), tone="caution"),
        ui.form(*hidden, ui.row(ui.button("실행", type="submit", kind="primary", size="lg"),
                                ui.button("취소", href="/admin/models", size="lg"), gap="sm"),
                action="/admin/benchmarks", label="실행 확인"),
        title=ui.joined(model, "실행 확인", sep=" "), label="실행 확인")


def page(ctx: Ctx, confirm: tuple[str, list[str], int, float] | None = None, retrieval_confirm: str | None = None,
         retrieval_jobs: list | None = None) -> Any:
    cfgs = bm.models(ctx.root)
    names = sorted((n, f"{n} · {c.get('model', n)}") for n, c in cfgs.items() if c.get("kind") in ("systemone", "openai", "gemini"))
    head = ui.page_header("모델·벤치마크", "모델은 어댑터로 등록하고 같은 벤치마크 세트, 같은 채점기로 비교합니다.")
    parts = [head]
    if ctx.message:
        parts.append(ui.notice(ctx.message, tone="negative" if ctx.query.get("error") else "positive"))
    if confirm is not None:
        parts.append(confirm_card(ctx, *confirm))
    parts += [ui.card(retrieval.cost_card(ctx.root, retrieval_confirm) if retrieval_confirm else None,
                      retrieval.card(ctx), retrieval.jobs_card(retrieval_jobs), title="검색 비교·실행", label="검색 비교·실행")]
    other = [jobs_card(ctx), decision_card(ctx), tuned_card(ctx), policy_card(ctx), load_card(ctx)]
    side = ui.stack(ui.card(add_form(ctx), title="모델 추가", label="모델 추가"),
                    ui.card(run_form(ctx, names), title="판별·상담·음성·부하 실행", label="일반 벤치마크 실행"), gap="lg")
    parts.append(ui.disclosure("판별·상담·음성·부하 및 모델 등록",
                               ui.split(ui.stack(*other, gap="lg"), side, aside_first=False),
                               open=confirm is not None or ctx.query.get("view") == "other"))
    return shell(ctx, "models", "모델·벤치마크", ui.stack(*parts, gap="lg"))
