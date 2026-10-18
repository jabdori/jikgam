"""브라우저 검사 전용 가상 업로드 상태. 실제 저장소 파일과 유료 API는 변경하지 않는다."""

from jikgam.ingest.chunk import Chunk
from jikgam.ingest.corpus import Doc, Line
from jikgam.web.data import processing


def uploads():
    rows = []
    for uid, title, status in (("support-refund", "환불 접수 안내.pdf", "진행"),
                                ("support-retention", "상담 자료 보관 안내.pdf", "완료")):
        text = "상담 운영팀의 가상 업무 안내입니다.\n접수 결과는 담당자가 확인한 뒤 고객에게 안내합니다."
        parts = text.splitlines()
        lines = [Line(1, "본문", 1, "text", parts[0], 0), Line(2, "본문", 1, "text", parts[1], len(parts[0]) + 1)]
        doc = Doc(uid, title, "업로드 문서", False, lines)
        chunk = Chunk(f"{uid}:segment", uid, 1, 2, 0, len(text), 40)
        state = {"id": uid, "title": title, "status": status, "chunks": 1,
                 "stages": [{"name": name, "status": "완료", "seconds": seconds, "cost_usd": cost}
                            for name, seconds, cost in zip(processing.STAGES[:4], (.8, 2.3, 4.6, 1.2), (0, .0012, .0007, 0))]}
        if status == "진행":
            state["stages"][2]["status"] = "진행"
            state["stages"][3].update(status="대기", seconds=None, cost_usd=None)
        rows.append({"id": uid, "title": title, "kind": "PDF", "status": status, "count": 1,
                     "state": state, "doc": doc, "chunks": [chunk], "meta": {
                         chunk.id: {"fields": [{"name": "안내 목적", "value": "상담 담당자의 접수 결과 확인"}],
                                    "related": ["고객 안내 절차"]}}})
    return rows


def documents(root, org):
    # 고정 원본은 실제로 읽고, 개발 확인용 업로드는 촬영 자료에 섞지 않는다.
    return [item for item in processing.documents(root, org) if "doc" in item] + uploads()
