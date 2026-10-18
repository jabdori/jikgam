"""관리 콘솔의 검색 비교 세트와 실행 확인."""

from .. import settings
from ..data import processing as proc
from ..ui import components as ui
from ..ui import fmt


def cost_card(root, model):
    e = proc.estimate(root, model)
    label, _, configuration, _ = settings.RETRIEVAL_MODELS[model]
    labels = (("분할", "split"), ("구간 정보", "metadata"), ("리랭킹", "rerank"))
    rows = [ui.Row([label, fmt.usd(e[key], digits=settings.COST_DIGITS)]) for label, key in labels]
    summary = ui.keyvals([("선택 모델", label), ("분할 설정", configuration), ("검색 설정", settings.RETRIEVAL_PIPELINE),
                          ("실행 범위", "val · 전체 질문(근거 없음 포함)"),
                          ("대상 자료", ui.joined("기존 공개 문서 ", fmt.count(e["documents"], "개"), sep=""))], narrative=True)
    return ui.card(summary,
                   ui.table([ui.Col("단계"), ui.Col("예상 비용", num=True)], rows, caption="검색 실행 예상 비용"),
                   ui.keyvals([("예상 구간", fmt.count(e["chunks"])), ("예상 합계", fmt.usd(e["total"], digits=settings.COST_DIGITS))]),
                   ui.text("분할은 원래 실행의 총 호출비, 구간 정보는 같은 구성의 측정치 또는 기준 구성의 구간당 비용 × 예상 구간 수, "
                           "리랭킹은 같은 구성의 val 실측 합계를 씁니다. 색인은 로컬 GPU를 사용합니다. 실제 비용은 달라질 수 있습니다.",
                           size="sm", tone="muted"),
                   ui.disclosure("비용 계산 근거", ui.ordered([ui.code(p) for p in e["sources"]], label="비용 계산 근거")),
                   ui.form(ui.hidden("boundary_model", model), ui.hidden("split", "val"), ui.hidden("confirm", "yes"),
                           ui.row(ui.button("비용 확인 후 실행", type="submit", kind="primary", disabled=e["total"] is None),
                                  ui.button("취소", href="/admin/models")),
                           action="/admin/retrieval", label="검색 실행 확인"),
                   title="검색 실행 확인", label="검색 실행 확인")


def jobs_card(jobs):
    if not jobs:
        return None
    parts = []
    for j in jobs:
        rows = [ui.Row([s["name"], s["status"], fmt.seconds_fine(s.get("seconds")), fmt.usd(s.get("cost_usd"), digits=settings.COST_DIGITS)])
                for s in j["stages"]]
        counts = ui.joined(ui.joined("구간 ", fmt.count(j.get("chunks", 0)), sep=""),
                           ui.joined("구간 정보 ", fmt.count(j.get("metadata_done")), sep=""),
                           ui.joined("검색 질문 ", fmt.count(j.get("questions_done")), sep=""))
        parts.append(ui.disclosure(ui.joined(j["title"], j["status"], counts),
                                   ui.table([ui.Col("단계"), ui.Col("상태"), ui.Col("시간", num=True), ui.Col("실제 비용", num=True)],
                                            rows, caption="검색 실행 단계", stack=True),
                                   ui.notice(j["error"], tone="negative") if j.get("error") else None,
                                   open=j["status"] != "완료"))
    card = ui.card(*parts, ui.button("진행 상태 새로고침", href="/admin/models"), title="검색 실행 상태", label="검색 실행 상태")
    if all(j["status"] in ("완료", "실패") for j in jobs):
        return card
    # 실행 중에는 단계 상태를 스스로 다시 받아 온다. 끝나면 화면 전체를 한 번 맞추고 멈춘다(console.js)
    return ui.live("/admin/models", settings.PROGRESS_REFRESH_MS, ui.slot("jobs", card, polite=True))


def identity(result):
    if not result.get("run_id"):
        return ui.stack(ui.text(result["label"], strong=True), ui.text(result["description"], tone="muted", size="sm"), gap="xs")
    _, _, configuration, _ = settings.RETRIEVAL_MODELS[result["boundary_model"]]
    simulated = result.get("simulated") is True
    return ui.stack(ui.text("가상 실행" if simulated else "화면 실행", strong=True),
                    ui.text("화면 검증 자료(실측 아님)", tone="muted", size="sm") if simulated else None,
                    ui.text(ui.joined("모델", result["model_label"]), size="sm"),
                    ui.disclosure("설정·실행 ID", ui.keyvals([
                        ("분할 설정", configuration), ("검색 설정", settings.RETRIEVAL_PIPELINE),
                        ("실행 ID", ui.code(result["run_id"]))], narrative=True)), gap="xs")


def results_table(results, caption, compact_caption):
    budget = fmt.count(settings.RETRIEVAL_BUDGET, kind="criterion")
    cols = [ui.Col("구성"), ui.Col(ui.joined("단일 근거 재현율@", budget, " · 판정 기준", sep=""), num=True),
            ui.Col(ui.joined("전체 재현율@", budget, sep=""), num=True),
            ui.Col(ui.joined("nDCG@", fmt.count(settings.RETRIEVAL_NDCG_K, kind="criterion"), sep=""), num=True),
            ui.Col("검색 p95", num=True), ui.Col("리랭킹 p95", num=True), ui.Col("리랭킹 오류", num=True)]
    rows, compact = [], []
    # 지표 이름의 기준값은 설정에서 읽는다.
    for r in results:
        m, lat = r.get("metrics", {}), r.get("latency_ms", {})
        key = "items@" + str(settings.RETRIEVAL_BUDGET)
        ndcg = "ndcg@" + str(settings.RETRIEVAL_NDCG_K)
        major = fmt.pct(m.get("single", {}).get(key))
        rest = [("전체 근거 재현율", fmt.pct(m.get("all", {}).get(key))),
                ("nDCG", fmt.number(m.get("all", {}).get(ndcg), digits=settings.RETRIEVAL_METRIC_DIGITS)),
                ("검색 p95", fmt.ms(lat.get("retrieval_p95"))), ("리랭킹 p95", fmt.ms(lat.get("rerank_p95"))),
                ("리랭킹 오류", fmt.count((r.get("rerank") or {}).get("errors")))]
        rows.append(ui.Row([identity(r), major, fmt.pct(m.get("all", {}).get(key)),
                            fmt.number(m.get("all", {}).get(ndcg), digits=settings.RETRIEVAL_METRIC_DIGITS),
                            fmt.ms(lat.get("retrieval_p95")), fmt.ms(lat.get("rerank_p95")),
                            fmt.count((r.get("rerank") or {}).get("errors"))]))
        compact.append(ui.Row([identity(r), major, ui.disclosure("상세", ui.keyvals(rest, narrative=True))]))
    compact_cols = [ui.Col("구성"), ui.Col("단일 근거 재현율", num=True), ui.Col("추가 지표")]
    return ui.responsive(ui.table(cols, rows, caption=caption, empty_text="검색 결과가 없습니다."),
                         ui.table(compact_cols, compact, caption=compact_caption, empty_text="검색 결과가 없습니다."))


def card(ctx):
    results = proc.results(ctx.root)
    val_results = proc.val_results(ctx.root)
    qwen = proc.qwen_available()
    options = [(key, value[0] + " · 서버 없음 — 선택 불가" if key == "qwen" and not qwen else value[0],
                key != "qwen" or qwen) for key, value in settings.RETRIEVAL_MODELS.items()]
    form = ui.form(ui.field_choices("boundary_model", "분할 판단 모델", options, "jev"), ui.hidden("split", "val"),
                   ui.button("검색 예상 비용 보기", type="submit", kind="primary"),
                   action="/admin/retrieval", label="검색 벤치마크 실행")
    split_label = ui.joined(settings.RETRIEVAL_SPLIT, "결과(최종 평가 분할) · 설정 고정 커밋",
                            settings.RETRIEVAL_FIXED_COMMIT, "에서 한 번 실행", sep=" ") \
        if settings.RETRIEVAL_SPLIT == "test" else settings.RETRIEVAL_SPLIT + " 결과(튜닝용 분할)"
    return ui.stack(ui.text(split_label, strong=True),
                   results_table(results, "검색 비교 세트", "검색 비교 핵심 지표"),
                   ui.text("전체 재현율은 모든 근거 항목을 합친 값입니다. 단일 근거 재현율이 판정 기준입니다. "
                           "분할 → 구간 정보 → 색인 → 검색·리랭킹 → 채점 순서로 실행합니다. test·check는 실행할 수 없습니다.", size="sm", tone="muted"),
                   ui.stack(ui.subheading("val 실행 결과"),
                            results_table(val_results, "val 실행 결과 비교", "val 실행 결과 핵심 지표") if val_results else
                            ui.empty("아직 완료된 val 실행 결과가 없습니다."), gap="sm"),
                   ui.disclosure("검색 실행 · 분할 판단 모델 선택", form, open=True), gap="lg")
