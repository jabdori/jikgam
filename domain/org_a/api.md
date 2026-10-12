# 조직 A 모의 업무 API

상담 엔진이 호출하는 업무 API의 계약이다. 구현은 `customers.json`을 읽는 모의 서버로 시작하고, 조직 B에서는 같은 계약을 다른 어댑터로 구현한다.

원칙은 다음과 같다.

- 조회 API는 자유롭게 호출한다.
- 쓰기 API(해지, 환불, 계좌 등록)는 본인 확인을 마친 세션에서만, 고객이 내용을 확인한 뒤에만 호출한다.
- 모든 쓰기 API는 `idempotency_key`를 받는다. 같은 키로 다시 호출하면 새 요청을 만들지 않고 처음 결과를 돌려준다.
- 금액 계산과 자격 판단은 서버(`quote_refund`)가 약관 규칙으로 한다. 모델은 금액을 계산하지 않고, 서버가 준 금액과 근거 조항을 안내한다.

## 조회

| 이름 | 입력 | 출력 |
|---|---|---|
| `verify_identity` | `phone`, `otp` | `verified`, `customer_id`, `attempts` |
| `get_customer` | `customer_id` | 이름, 생년월일, 계정 상태(`active`·`suspended`) |
| `list_subscriptions` | `customer_id` | 구독 목록(요금제, 가격, 프로모션 여부, 시작일, 다음 결제일, 체험 여부) |
| `list_payments` | `customer_id` | 결제 목록(결제일, 금액, 수단, 이의 제기 여부) |
| `get_usage` | `customer_id`, `since` | 이용 이력(날짜, 유형, 체험 기간 여부) |
| `list_incidents` | `from`, `to` | 장애 기록(시작, 종료, 시간, 귀책) |
| `quote_refund` | `customer_id`, `subscription_id`, `reason`, `request_date` | 환불 가능 여부, 환불액, 계산 내역, 근거 조항 ID, 승인 필요 여부, 인계 필요 여부 |

`reason`은 `withdrawal`(청약철회), `annual_cancel`(연 구독 중도 해지), `duplicate`(중복 결제), `incident`(장애), `minor`(미성년자), `trial_cancel`(체험 해지), `plan_change`(월 구독에서 연 구독으로 변경 시 일할 차감액) 중 하나다.

## 쓰기

| 이름 | 입력 | 출력 |
|---|---|---|
| `create_cancellation` | `customer_id`, `subscription_id`, `idempotency_key` | 해지 접수 ID, 이용 가능 마지막 날 |
| `create_refund_request` | `customer_id`, `payment_id`, `quote_id`, `idempotency_key` | 환불 접수 ID, 상태(`confirmed`·`pending_approval`), 예상 소요 기간 |
| `register_refund_account` | `customer_id`, `bank`, `account`, `holder`, `idempotency_key` | 등록 결과(예금주가 회원 본인이 아니면 거절) |
| `create_handoff` | `customer_id`, `reason`, `summary` | 인계 접수 ID, 예상 대기 시간 |

## 오류

| 코드 | 의미 | 엔진의 기본 동작 |
|---|---|---|
| `IDENTITY_REQUIRED` | 본인 확인 전 쓰기 호출 | 본인 확인 단계로 돌아감 |
| `NOT_ELIGIBLE` | 약관상 대상 아님 | 근거 조항과 함께 안내 |
| `HANDOFF_REQUIRED` | 제19조 제1항 사유 | `create_handoff` 호출 |
| `TIMEOUT`, `UNAVAILABLE` | 일시 장애 | 재시도 예산 내 1회 재시도 후 접수 보류 안내 |
| `DUPLICATE_REQUEST` | 같은 대상에 다른 키로 중복 접수 | 기존 접수 내역 안내 |
