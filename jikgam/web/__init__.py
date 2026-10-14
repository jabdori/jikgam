"""웹 화면 세 개(ADR 0005): 상담 콘솔, 턴 상세 타임라인, 평가 대시보드.

엔진 동작은 바꾸지 않는다. 콘솔은 엔진 세션(정책 B·C)이나 RAG 기준선 세션(정책 A)을 그대로 돌리고, 세션의 이벤트 로그
줄을 화면 메시지로 옮긴다. 타임라인과 대시보드는 결과 폴더의 원본 로그만 읽고, 수치는 jikgam.engine.events,
jikgam.sim.report·metrics, jikgam.eval.report·metrics의 함수로 계산한다.

    uv run python -m jikgam.web --port 8100
"""
