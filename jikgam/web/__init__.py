"""웹 콘솔 두 개(ADR 0021): 고객사 콘솔(/app, 상담 운영팀)과 관리 콘솔(/admin, 직감 팀 내부).

화면은 서버가 공용 컴포넌트로 HTML을 만든다(jikgam/web/ui, saas, admin). 화면 수치는 결과 폴더의 원본 로그와 녹음에서
화면을 열 때 계산한다(jikgam/web/data, dashboard.py). 디자인 하네스(ADR 0022, docs/design/README.md)를 통과해야 화면을
완료로 본다.

    uv run python -m jikgam.web --port 8100
"""
