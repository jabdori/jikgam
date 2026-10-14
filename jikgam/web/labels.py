"""화면에 쓰는 한국어 이름. 라벨 값 자체(영어 코드)는 화면에도 괄호로 함께 보여 준다."""

INTENT = {"refund": "환불", "cancel": "해지", "cancel_refund": "해지·환불", "plan_change": "요금제 변경",
          "billing_inquiry": "결제 문의", "compensation": "장애 보상", "other": "기타", "unclear": "불분명"}
ACTION = {"answer": "답변", "ask": "확인 질문", "handoff": "상담사 인계", "hold": "보류"}
NEXT_INFO = {"plan_type": "요금제 종류", "payment_date": "결제일", "usage": "이용 여부", "payment_method": "결제 수단",
             "identity": "본인 확인", "account": "환불 계좌", "none": "없음"}
CALLER = {"owner": "본인", "third_party": "제3자", "guardian": "법정대리인"}
ASK = {"phone": "휴대폰 번호", "otp": "인증번호", "otp_retry": "인증번호 다시 확인", "account": "환불 계좌",
       "own_account": "본인 명의 계좌", "account_holder": "예금주", "plan_type": "요금제 종류", "identity": "본인 확인"}
RULE = {"third_party": "제3자 거절", "handoff": "상담사 인계", "api_failure": "업무 API 장애로 보류",
        "verify_identity": "본인 확인 호출", "identity_unavailable": "본인 확인 불가로 보류",
        "identity_required": "본인 확인 요청", "lookups": "기록 조회", "quote": "환불 견적", "compensation": "장애 보상 계산",
        "compensation_none": "보상 대상 없음", "target_ambiguous": "대상 구독 확인", "account_required": "환불 계좌 요청",
        "not_eligible": "환불 대상 아님 안내", "proposal": "접수 안내와 동의 요청", "writes": "접수 실행",
        "received": "접수 결과 안내", "declined": "접수하지 않음 확인", "decision": "판별 결과를 따름",
        "decision_unavailable": "판별 실패", "pass_limit": "반복 한도 도달", "engine_error": "엔진 오류", "rag": "RAG 답변"}
CLAIM = {"payment_date": "결제일", "incident_date": "장애 날짜", "mentioned_date": "언급한 날짜", "expected_refund": "기대 환불액",
         "payment_amount": "결제 금액", "mentioned_amount": "언급한 금액", "payment_count": "결제 횟수",
         "refund_account": "환불 계좌"}
STAGE = {"retrieval": "검색", "decision": "판별", "policy": "정책", "tools": "도구 호출", "tool": "도구 결과",
         "generation": "생성"}
TOOL = {"verify_identity": "본인 확인", "get_customer": "고객 조회", "list_subscriptions": "구독 조회",
        "list_payments": "결제 조회", "get_usage": "이용 기록 조회", "list_incidents": "장애 기록 조회",
        "quote_refund": "환불 견적", "create_cancellation": "해지 접수", "create_refund_request": "환불 접수",
        "register_refund_account": "환불 계좌 등록", "create_handoff": "상담사 인계 접수"}
QUOTE_REASON = {"minor": "미성년자 결제 취소", "duplicate": "중복 결제", "withdrawal": "청약철회", "incident": "장애 환불",
                "annual_cancel": "연 구독 중도 해지", "trial_cancel": "체험 해지", "plan_change": "요금제 변경 차감"}
QUOTE_STATUS = {"eligible": "환불 대상", "zero": "대상이지만 0원", "not_eligible": "대상 아님", "handoff": "인계 필요",
                "failed": "조회 실패"}
SUGGESTION_STATUS = {"active": "유효", "invalidated": "무효화됨", "superseded": "새 제안으로 바뀜"}
BUMP_CAUSE = {"utterance": "고객 발화", "tool": "도구 결과", "agent_handoff": "상담사 인계 요청"}


def label(table: dict[str, str], code: str | None) -> str | None:
    """코드의 한국어 이름. 표에 없으면 코드를 그대로 쓴다."""
    if code is None:
        return None
    return table.get(code, code)


def invalidation_reason(code: str) -> str:
    """suggestion.invalidated의 reasons 항목 하나를 읽을 수 있는 문장으로. 원래 코드는 괄호로 붙인다."""
    if code == "suggestion_valid=false":
        return "이전 제안이 지금 대화에 맞지 않음 (suggestion_valid=false)"
    if code == "condition_changed=true":
        return "고객 조건 변경 (condition_changed=true)"
    kind, _, change = code.partition(":")
    old, _, new = change.partition("->")
    if kind == "intent_changed":
        return f"요청 변경: {label(INTENT, old)} → {label(INTENT, new)} (intent_changed)"
    if kind == "caller_changed":
        return f"통화자 변경: {label(CALLER, old)} → {label(CALLER, new)} (caller_changed)"
    return code
