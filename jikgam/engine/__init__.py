"""상담 엔진(채팅). 설계는 docs/engine.md, 측정 정의는 docs/evaluation.md §3을 따른다.

음성 런타임, 웹 화면, 특정 판별 모델에 의존하지 않는다. 입력은 공통 대화 이벤트로 들어오고(events.py),
한 턴의 처리 순서는 session.py가 정한다.
"""
