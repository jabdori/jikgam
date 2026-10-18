"""화면 검사 전용 가상 val 실행. 임시 폴더만 쓰고 유료 API를 부르지 않는다."""

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from jikgam.web.data import processing


@contextmanager
def simulated_job():
    with TemporaryDirectory(prefix="jikgam-simulated-val-") as directory:
        local = Path(directory)
        job = local / "jobs/simulated-val-jev"
        processing.write(job / "state.json", {
            "id": job.name, "title": "가상 val 실행 · 화면 검증 자료", "model": "jev", "split": "val",
            "simulated": True, "status": "완료", "chunks": 400, "metadata_done": 400, "questions_done": 100,
            "stages": [{"name": name, "status": "완료", "seconds": None, "cost_usd": None}
                       for name in processing.STAGES]})
        processing.write(job / "result.json", {
            "name": job.name, "split": "val",
            "metrics": {"single": {"items@2000": .77}, "all": {"items@2000": .71, "ndcg@10": .68}},
            "latency_ms": {"retrieval_p95": 35, "rerank_p95": 240}, "rerank": {"errors": 0}})
        # 요청을 처리하는 서버 스레드에서도 임시 실행 폴더만 읽는다.
        with patch.object(processing, "local", return_value=local):
            yield
