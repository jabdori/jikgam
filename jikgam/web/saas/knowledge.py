"""고객사 콘솔: 지식·정책. AI가 근거로 쓰는 문서, 처리 단계, 구간 상세(ADR 0021, jikgam/web/data/knowledge.py).

v1 문서는 업로드·처리 백엔드가 없다(v2에서 붙인다). 그래서 업로드와 구간 경계 수정은 누를 수 없는 버튼으로 두고,
처리 단계는 v1에서 실제로 한 것만 완료로 보인다.
"""

from typing import Any

from ..data import knowledge as kn
from ..data.tenants import article_label, terms_header, terms_of
from ..ui import components as ui
from ..ui import fmt
from .common import Ctx, shell

SOON = "업로드와 구간 경계 수정은 문서 처리 기능을 연결한 뒤에 쓸 수 있습니다."


def steps_of(doc: kn.Doc) -> list[ui.Step]:
    """v1에서 실제로 한 단계만 완료다. 하지 않은 단계는 상태 글자로 무엇을 안 했는지 적는다."""
    if doc.key == "terms":
        return [
            ui.Step("텍스트 추출", "done", detail="원문이 텍스트 문서"),
            ui.Step("문맥 단위 분할", "done", detail="조항 제목으로 나눔"),
            ui.Step("요약·메타데이터", "skip", note="요약 없음", detail="조항 성격과 참조 조항만 문서 주석에서 읽음"),
            ui.Step("색인", "done"),
            ui.Step("검토 후 게시", "done"),
        ]
    return [
        ui.Step("텍스트 추출", "skip", note="해당 없음", detail="설정에 직접 적은 항목"),
        ui.Step("문맥 단위 분할", "skip", note="해당 없음", detail="항목마다 하나"),
        ui.Step("요약·메타데이터", "skip", note="해당 없음"),
        ui.Step("색인", "skip", note="해당 없음", detail="찾지 않고 접수 안내에 늘 넣음"),
        ui.Step("검토 후 게시", "done"),
    ]


def pipeline(doc: kn.Doc) -> Any:
    """처리 단계. 진행 중인 단계가 없으면(다 끝난 문서) 한 줄 요약으로 접어 둔다."""
    steps = steps_of(doc)
    tiles = ui.steps(steps, label=f"{doc.name} 처리 단계", layout="tiles")
    if any(s.status == "now" for s in steps):
        return ui.card(tiles, title=f"{doc.name} 처리 단계", label="처리 단계")
    skipped = [s.label for s in steps if s.status == "skip"]
    line = ui.joined("처리 단계", ui.joined("하지 않은 단계: ", ", ".join(skipped), sep="") if skipped else "모두 완료")
    return ui.disclosure(line, tiles)


def doc_table(docs: list[kn.Doc], current: kn.Doc) -> Any:
    cols = [ui.Col("문서"), ui.Col("종류"), ui.Col("시행일", num=True), ui.Col("구간", num=True),
            ui.Col("상태")]
    rows = [ui.Row([ui.stack(d.name, ui.text(d.file, tone="muted", size="sm"), gap="xs"), d.kind,
                    fmt.date_(d.effective) if d.effective else fmt.DASH, fmt.count(d.segments),
                    ui.badge(d.status, "positive")], href=f"/app/knowledge?doc={d.key}", selected=d.key == current.key)
            for d in docs]
    return ui.card(ui.table(cols, rows, caption="문서 목록"), label="문서", flush=True)


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
                    ("문서 개정", ui.joined(fmt.date_(revised), revision) if revised else fmt.DASH)], wide_key=True),
        ui.subheading("고객 표현"),
        ui.text("정리된 고객 표현이 없습니다. 문서를 요약·메타데이터 단계까지 처리하면 생깁니다.", tone="muted", size="md"),
        title="메타데이터", label="메타데이터")
    return ui.split(detail, meta, aside_first=False)


def page(ctx: Ctx) -> Any:
    docs = kn.docs(ctx.root, ctx.tenant)
    current = next((d for d in docs if d.key == ctx.query.get("doc")), docs[0])
    q = ctx.query.get("q", "")
    header = ui.page_header("지식·정책", "AI는 게시된 문서만 근거로 안내합니다.",
                            actions=ui.row(ui.text(SOON, tone="muted", size="sm"),
                                           ui.button("문서 업로드", kind="primary", disabled=True), gap="sm"))
    parts: list[Any] = [header, doc_table(docs, current), pipeline(current)]
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
