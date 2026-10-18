"""독립 화면 리뷰에서 지적한 문서 연결과 실행 범위 표시를 검증한다."""

import pytest
from starlette.testclient import TestClient
from web_fixture import build
from web_knowledge_fixture import uploads

from jikgam.web import auth
from jikgam.web.app import WebConfig, create_app
from jikgam.web.data import processing as proc
from jikgam.web.ui import terms


@pytest.fixture
def client(tmp_path):
    root = build(tmp_path, 0)
    accounts = {"t": auth.Account("t", "pw", "tenant", "org_t"), "a": auth.Account("a", "pw", "admin")}
    app = create_app(WebConfig(root=root, accounts=accounts, knowledge_documents=lambda *_: uploads()))
    return root, TestClient(app)


def get(client, path, user="t"):
    return client.get(path, headers={"Authorization": auth.basic_header(user, "pw")}).text


def test_selected_document_connects_stage_and_detail_and_keeps_upload_near_title(client):
    _, c = client
    page = get(c, "/app/knowledge?doc=support-refund")
    assert "환불 접수 안내.pdf · 처리 단계 · 진행" in page
    assert "텍스트 추출" in page and "문맥 분할" in page and "구간 정보" in page and "색인" in page
    assert page.index("문서 업로드") < page.index("처리 문서 목록")
    assert 'aria-current="true">환불 접수 안내.pdf' in page
    assert page.index("처리 문서 목록") < page.index("처리 단계") < page.index('aria-label="구간 원문"')
    assert "메타데이터" not in terms.visible_text(page)
    assert not terms.find(terms.visible_text(page), terms.load_terms())


def test_completed_document_collapses_completed_stages_and_normalizes_status(client):
    _, c = client
    page = get(c, "/app/knowledge?doc=support-retention")
    assert '<details class="disclosure"><summary class="disclosure-summary">상담 자료 보관 안내.pdf · 처리 단계 · 처리 완료' in page
    assert "처리 완료" in page
    assert 'class="keyvals narrative"' in page
    assert "상담 안내 문서 · 게시 설정" in page


@pytest.mark.parametrize("document", ["support-refund", "support-retention"])
def test_customer_progress_hides_internal_cost_and_precise_time(client, document):
    _, c = client
    page = get(c, "/app/knowledge?doc=" + document)
    visible = terms.visible_text(page)
    assert "실제 비용" not in visible and "걸린 시간" not in visible and "USD" not in visible
    assert "0.001200" not in visible and "4.60초" not in visible
    assert "구간" in visible and "처리 완료" in visible


def test_completed_step_title_is_only_in_disclosure_heading(client):
    _, c = client
    page = get(c, "/app/knowledge?doc=support-retention")
    assert page.count("상담 자료 보관 안내.pdf · 처리 단계 · 처리 완료") == 1
    assert 'aria-label="문서 처리"' not in page


def test_published_documents_start_with_table_without_duplicate_card_title(client):
    _, c = client
    page = get(c, "/app/knowledge?doc=support-retention")
    assert page.count("상담 안내 문서 · 게시 설정") == 1
    published = page.split('id="published-documents"', 1)[1]
    assert 'class="disclosure-body"><div class="table-wrap"' in published
    assert 'aria-label="게시 문서"' not in published


def test_upload_entry_opens_form_before_document_list(client):
    _, c = client
    page = get(c, "/app/knowledge?doc=support-refund&upload=yes")
    assert page.index('name="pdf"') < page.index("처리 문서 목록")
    assert 'enctype="multipart/form-data"' in page


def test_search_default_and_general_benchmarks_are_separate(client):
    _, c = client
    page = get(c, "/admin/models", "a")
    assert "검색 비교·실행" in page
    assert '<details class="disclosure"><summary class="disclosure-summary">판별·상담·음성·부하 및 모델 등록' in page
    assert "판별·상담·음성·부하 실행" in page
    opened = get(c, "/admin/models?view=other", "a")
    assert '<details class="disclosure" open><summary class="disclosure-summary">판별·상담·음성·부하 및 모델 등록' in opened


def test_cost_confirmation_names_model_configuration_scope_and_fixed_precision(client):
    root, c = client
    for name in ("c-jev-W4000-C800-raw", "c-jev-W4000-C800-raw-t0.65"):
        proc.write(root / f"data/v2/chunks/{name}.meta.json", {"chunks": 389, "calls": {"total_cost_usd": .02655}})
        proc.write(root / f"data/v2/meta/{name}.meta.json", {"cost_usd": .6971})
    proc.write(root / "data/v2/extracted/summary.json", {"docs": {"fake-law": {}}})
    proc.write(root / "results/v2/val/c-jev-W4000-C800-raw-t0.65--full--snow+rrf+jev8.json", {"rerank": {"cost_usd": .0292}})
    page = get(c, "/admin/models?retrieval=jev", "a")
    assert all(label in page for label in ("선택 모델", "분할 설정", "검색 설정", "실행 범위", "대상 자료"))
    assert "val · 전체 질문(근거 없음 포함)" in page
    assert all(cost in page for cost in ("$0.026550", "$0.697100", "$0.029200", "$0.752850"))


def test_saved_run_identity_keeps_model_settings_and_run_separate(client):
    root, c = client
    proc.write(proc.local(root) / "jobs/fake-run/state.json", {
        "id": "fake-run", "model": "jev", "title": "가상 검색 실행", "status": "완료", "stages": []})
    proc.write(proc.local(root) / "jobs/fake-run/result.json", {"name": "지금 실행", "split": "val", "metrics": {}})
    result = proc.results(root)[0]
    assert result["run_id"] == "fake-run" and result["boundary_model"] == "jev"
    page = get(c, "/admin/models", "a")
    assert all(value in page for value in ("화면 실행", "설정·실행 ID", "분할 설정", "검색 설정", "fake-run"))
    assert 'class="viewport-wide"' in page and 'class="viewport-narrow"' in page
    assert "검색 비교 핵심 지표" in page and "추가 지표" in page
