"""고객사 콘솔의 PDF 처리, 처리 단계와 원문·구간 정보 열람. 기존 약관 화면도 유지한다."""

from typing import Any
from urllib.parse import urlencode

from .. import settings
from ..data import knowledge as kn
from ..data import processing as proc
from ..data.tenants import article_label, terms_header, terms_of
from ..ui import components as ui
from ..ui import fmt
from .common import Ctx, shell

SOON = "PDF를 올리고 예상 처리 비용을 확인한 뒤 시작하세요."


def upload_form() -> Any:
    return ui.card(ui.form(ui.field_file("pdf", "PDF 문서"),
                           ui.button("예상 비용 보기", type="submit", kind="primary"),
                           action="/app/knowledge/upload", label="문서 업로드", multipart=True),
                   title="문서 업로드", sub=SOON, label="문서 업로드")


def processing_card(state: dict) -> Any:
    rows = [ui.Row([s["name"], "처리 완료" if s["status"] == "완료" else s["status"]]) for s in state["stages"]]
    body = [ui.table([ui.Col("단계"), ui.Col("상태")],
                     rows, caption="문서 처리 상태", stack=True),
            ui.text(ui.joined("구간 ", fmt.count(state.get("chunks", 0)), sep=""))]
    if state["status"] == "대기":
        e = state["estimate"]
        body += [ui.notice(ui.joined("예상 처리 비용 · USD ", fmt.number(e["total"], digits=settings.COST_DIGITS), sep="")),
                 ui.text("분할·구간 정보의 이전 처리 비용을 문서 분량에 맞춰 계산합니다. 실제 비용은 달라질 수 있습니다.", size="sm", tone="muted"),
                 ui.form(ui.hidden("upload", state["id"]), ui.hidden("confirm", "yes"),
                         ui.button("비용 확인 후 처리 시작", type="submit", kind="primary", disabled=e["total"] is None),
                         action="/app/knowledge/process", label="문서 처리 확인")]
    elif state["status"] == "진행":
        body.append(ui.button("진행 상태 새로고침", href="/app/knowledge?" + urlencode({"doc": state["id"]})))
    elif state["status"] == "실패":
        body.append(ui.notice("문서 처리를 마치지 못했습니다. 완료된 단계와 실패한 단계를 확인하세요.", tone="negative"))
    status = "처리 완료" if state["status"] == "완료" else state["status"]
    title = ui.joined(state["title"], "처리 단계", status)
    if state["status"] == "완료":
        return ui.disclosure(title, *body)
    card = ui.card(*body, title=title, label="문서 처리")
    if state["status"] != "진행":
        return card
    # 진행 중에는 단계 상태를 스스로 다시 받아 온다. 끝나면 화면 전체를 한 번 맞추고 멈춘다(console.js)
    url = "/app/knowledge?" + urlencode({"doc": state["id"]})
    return ui.live(url, settings.PROGRESS_REFRESH_MS, ui.slot("progress", card, polite=True))


def original_pipeline(item: dict) -> Any:
    steps = [ui.Step(name, "done", note="처리 완료") for name in proc.STAGES[:settings.KNOWLEDGE_STAGE_COUNT]]
    return ui.disclosure(ui.joined(item["title"], "처리 단계", "처리 완료"),
                         ui.steps(steps, label="문서 처리 단계", layout="tiles"))


def corpus_card(ctx: Ctx, items: list[dict]) -> Any:
    if not items:
        return None
    rows = [ui.Row([d["title"], d["kind"], fmt.count(d["count"]), "처리 완료" if d["status"] == "완료" else d["status"]],
                   href="/app/knowledge?" + urlencode({"doc": d["id"]}), selected=ctx.query.get("doc") == d["id"]) for d in items]
    return ui.card(ui.table([ui.Col("문서"), ui.Col("종류"), ui.Col("구간", num=True), ui.Col("처리 상태")], rows,
                            caption="처리 문서 목록", stack=True), title="처리 문서", label="처리 문서", flush=True)


def corpus_detail(ctx: Ctx, item: dict) -> Any:
    d, chunks, meta = proc.detail(item)
    if not chunks:
        return ui.empty("처리한 구간이 아직 없습니다.")
    q = ctx.query.get("q", "").strip()
    candidates = [c for c in chunks if not q or q in d.text[c.start:c.end] or q in proc_meta(meta.get(c.id, {}))]
    sid = ctx.query.get("seg")
    c = next((c for c in candidates if c.id == sid), candidates[0] if candidates else None)
    if c is None:
        return ui.empty("맞는 구간이 없습니다.")
    i = chunks.index(c)
    links = [ui.ListItem(ui.joined("구간 ", fmt.index(chunks.index(x) + 1), sep=""),
                         "/app/knowledge?" + urlencode({"doc": item["id"], "seg": x.id}),
                         sub=d.text[x.start:x.end].splitlines()[0]) for x in candidates[:settings.KNOWLEDGE_PAGE_SIZE]]
    m = meta.get(c.id, {})
    fields = [(str(f.get("name", "")), str(f.get("value", ""))) for f in m.get("fields", [])]
    related = m.get("related") or []
    nav = ui.row(ui.button("이전 구간", href="/app/knowledge?" + urlencode({"doc": item["id"], "seg": chunks[i - 1].id}), disabled=i == 0),
                 ui.button("다음 구간", href="/app/knowledge?" + urlencode({"doc": item["id"], "seg": chunks[i + 1].id})
                           if i + 1 < len(chunks) else None, disabled=i + 1 >= len(chunks)))
    chooser = ui.anchor("segment-chooser", ui.disclosure("구간 찾기", ui.item_list(links, label="구간 목록")))
    title = ui.joined("원문 · 구간 ", fmt.index(i + 1), " / ", fmt.count(len(chunks)), sep="")
    return ui.stack(chooser,
                    ui.split(ui.card(nav, ui.preformatted(d.text[c.start:c.end]), title=title, label="구간 원문"),
                             ui.card(ui.keyvals(fields, narrative=True) if fields else ui.empty("구간 정보가 없습니다."),
                                     ui.subheading("함께 볼 내용"), ui.ordered(related) if related else ui.text("없음"),
                                     title="구간 정보", label="구간 정보"), aside_first=False), gap="lg")


def proc_meta(m: dict) -> str:
    return " ".join(str(f.get("value", "")) for f in m.get("fields", []))


def steps_of(doc: kn.Doc) -> list[ui.Step]:
    """v1에서 실제로 한 단계만 완료다. 하지 않은 단계는 상태 글자로 무엇을 안 했는지 적는다."""
    if doc.key == "terms":
        return [
            ui.Step("텍스트 추출", "done", detail="원문이 텍스트 문서"),
            ui.Step("문맥 단위 분할", "done", detail="조항 제목으로 나눔"),
            ui.Step("구간 정보", "skip", note="요약 없음", detail="조항 성격과 참조 조항만 문서 주석에서 읽음"),
            ui.Step("색인", "done"),
            ui.Step("검토 후 게시", "done"),
        ]
    return [
        ui.Step("텍스트 추출", "skip", note="해당 없음", detail="설정에 직접 적은 항목"),
        ui.Step("문맥 단위 분할", "skip", note="해당 없음", detail="항목마다 하나"),
        ui.Step("구간 정보", "skip", note="해당 없음"),
        ui.Step("색인", "skip", note="해당 없음", detail="찾지 않고 접수 안내에 늘 넣음"),
        ui.Step("검토 후 게시", "done"),
    ]


def pipeline(doc: kn.Doc) -> Any:
    """처리 단계. 진행 중인 단계가 없으면(다 끝난 문서) 한 줄 요약으로 접어 둔다."""
    steps = steps_of(doc)
    for step in steps:
        if step.status == "done":
            step.note = "처리 완료"
    tiles = ui.steps(steps, label=f"{doc.name} 처리 단계", layout="tiles")
    if any(s.status == "now" for s in steps):
        return ui.card(tiles, title=f"{doc.name} 처리 단계", label="처리 단계")
    skipped = [s.label for s in steps if s.status == "skip"]
    line = ui.joined("처리 단계", ui.joined("하지 않은 단계: ", ", ".join(skipped), sep="") if skipped else "처리 완료")
    return ui.disclosure(line, tiles)


def doc_table(docs: list[kn.Doc], current: kn.Doc, standalone: bool = True) -> Any:
    cols = [ui.Col("문서"), ui.Col("종류"), ui.Col("시행일", num=True), ui.Col("구간", num=True),
            ui.Col("상태")]
    rows = [ui.Row([ui.stack(d.name, ui.text(d.file, tone="muted", size="sm"), gap="xs"), d.kind,
                    fmt.date_(d.effective) if d.effective else fmt.DASH, fmt.count(d.segments),
                    ui.badge(d.status, "positive")], href=f"/app/knowledge?doc={d.key}", selected=d.key == current.key)
            for d in docs]
    table = ui.table(cols, rows, caption="게시 문서 목록", stack=True)
    return ui.card(table, title="상담 안내 문서 · 게시 설정", label="게시 문서", flush=True) if standalone else table


def segment(ctx: Ctx, seg_id: str | None) -> Any:
    arts = terms_of(ctx.root, ctx.tenant)
    ids = list(arts)
    sid = seg_id if seg_id in arts else ids[0]
    a = arts[sid]
    i = ids.index(sid)
    head = terms_header(ctx.root, ctx.tenant)
    version, revised, revision = kn.version(head)
    nav = ui.row(
        ui.button("이전 구간", href=f"/app/knowledge?doc=terms&seg={ids[i - 1]}", size="sm", disabled=i == 0),
        ui.button("다음 구간", href=f"/app/knowledge?doc=terms&seg={ids[i + 1]}" if i + 1 < len(ids) else None, size="sm",
                  disabled=i + 1 >= len(ids)),
        ui.button("구간 경계 수정", size="sm", disabled=True), gap="sm")
    title = ui.stack(ui.text(ui.joined(f"이용약관 {version or ''}".strip(), ui.joined("구간 ", fmt.index(i + 1), " / ",
                                                                            fmt.count(len(ids)), sep="")), tone="muted", size="sm"),
                     ui.heading(article_label(a, sid)), gap="xs")
    text_items = kn.items(a.text)
    detail = ui.card(
        ui.row(title, ui.spacer(), nav, align="start"),
        ui.ordered(text_items, label="조항 본문") if text_items
        else ui.text("본문이 없습니다.", tone="muted"),
        label="구간 상세")
    refs = [article_label(arts.get(r), r) for r in a.refs]
    meta = ui.card(
        ui.keyvals([("조항 성격", kn.TYPE.get(a.type, "일반 규정")),
                    ("참조 조항", ", ".join(refs) if refs else "없음"),
                    ("요약", "없음"),
                    ("문서 개정", ui.joined(fmt.date_(revised), revision) if revised else fmt.DASH)], narrative=True),
        ui.subheading("고객 표현"),
        ui.text("정리된 고객 표현이 없습니다. 문서를 구간 정보 단계까지 처리하면 생깁니다.", tone="muted", size="md"),
        title="구간 정보", label="구간 정보")
    return ui.split(detail, meta, aside_first=False)


def page(ctx: Ctx, pending: dict | None = None, message: str | None = None) -> Any:
    docs = kn.docs(ctx.root, ctx.tenant)
    current = next((d for d in docs if d.key == ctx.query.get("doc")), docs[0])
    q = ctx.query.get("q", "")
    explicit_selection = bool(ctx.query.get("doc"))
    header = ui.page_header("지식·정책", "안내에 사용할 문서를 처리하고 원문과 구간 정보를 확인합니다.",
                            actions=ui.button("문서 업로드", href="/app/knowledge?" + urlencode({
                                "doc": ctx.query.get("doc", ""), "upload": "yes"}) + "#document-upload", kind="primary"))
    items = (ctx.knowledge_documents or proc.documents)(ctx.root, ctx.tenant.org)
    selected = next((d for d in items if d["id"] == ctx.query.get("doc")), None)
    if not ctx.query.get("doc") and items:
        selected = items[0]
        ctx.query["doc"] = selected["id"]
    parts: list[Any] = [header]
    if message:
        parts.append(ui.notice(message, tone="negative"))
    if ctx.query.get("upload") == "yes":
        parts.append(ui.anchor("document-upload", upload_form()))
    listing = corpus_card(ctx, items)
    parts.append(ui.disclosure(ui.joined("문서 목록", selected["title"]), listing)
                 if selected and explicit_selection else listing)
    if pending:
        parts.append(ui.anchor("document-stages", processing_card(pending)))
    elif selected and selected.get("state"):
        parts.append(ui.anchor("document-stages", processing_card(selected["state"])))
    if selected:
        if not selected.get("state"):
            parts.append(original_pipeline(selected))
        parts += [corpus_detail(ctx, selected),
                  ui.anchor("published-documents", ui.disclosure("상담 안내 문서 · 게시 설정", doc_table(docs, current, standalone=False)))]
        return shell(ctx, "knowledge", "지식·정책", ui.stack(*parts, gap="lg"), search=q)
    parts += [doc_table(docs, current), pipeline(current)]
    if q:
        arts = terms_of(ctx.root, ctx.tenant)
        found = kn.search(arts, q)
        rows = [ui.ListItem(article_label(a, a.id), f"/app/knowledge?doc=terms&seg={a.id}", sub=kn.TYPE.get(a.type))
                for a in found]
        parts.append(ui.card(ui.item_list(rows, label="검색 결과") if rows else ui.empty("맞는 구간이 없습니다."),
                             title="검색 결과", side=fmt.count(len(found), "건"), label="검색 결과", flush=True))
    if current.key == "terms":
        parts.append(segment(ctx, ctx.query.get("seg")))
    else:
        notices = ctx.tenant.raw.get("required_notices") or []
        parts.append(ui.card(ui.ordered(notices, label="필수 안내 항목"),
                             title="필수 안내 항목", sub="해지·환불을 접수할 때 AI가 늘 안내하는 내용", label="필수 안내 항목"))
    return shell(ctx, "knowledge", "지식·정책", ui.stack(*parts, gap="lg"), search=q)
