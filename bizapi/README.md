# bizapi: 조직 A 모의 업무 API

가상 조직 A(별담)의 업무 시스템을 흉내 내는 HTTP 서버다. 계약은 [`domain/org_a/api.md`](../domain/org_a/api.md), 환불·해지 규칙은 [`domain/org_a/terms.md`](../domain/org_a/terms.md)(약관 2026.07, 문서 개정 r2)를 따른다. 상담 엔진은 금액과 자격을 계산하지 않고 이 서버를 호출한다.

Go 표준 라이브러리만 쓴다. 상태(세션, 견적, 접수 내역, 멱등 키, 장애 주입)는 메모리에만 두며, 재시작하면 사라진다.

## 실행

```sh
cd bizapi
go run ./cmd/bizapi                       # 127.0.0.1:8081, ../domain/org_a/customers.json
go run ./cmd/bizapi -addr 127.0.0.1:9000 -data ../domain/org_a/customers.json
go test ./...
```

## 호출 형식

- 모든 도구는 `POST /v1/<도구 이름>`, 본문과 응답은 JSON이다. 모르는 필드가 있으면 `INVALID_REQUEST`로 거절한다.
- 모든 호출에 `X-Session-ID` 헤더(1~128자)가 필요하다. 본인 확인 상태는 세션별로 둔다.
- 오류 본문은 `{"error": {"code": "...", "message": "..."}}`이다. 경우에 따라 `article_ids`(근거 조항), `quote`(견적 판단 내역), `receipt`(기존 접수 내역)가 붙는다.

| 도구 | 본인 확인 | `idempotency_key` |
|---|---|---|
| `verify_identity` | - | - |
| `get_customer`, `list_subscriptions`, `list_payments`, `get_usage`, `list_incidents` | 불필요 | - |
| `quote_refund` | 불필요 | - |
| `create_cancellation`, `create_refund_request`, `register_refund_account` | 필요 | 필수 |
| `create_handoff` | 불필요(제3자 문의, 본인 확인 실패에도 쓴다) | 선택 |

`get_usage`의 `since`, `list_incidents`의 `from`·`to`, `create_handoff`의 `customer_id`·`summary`는 생략할 수 있다.

## 본인 확인

- **모의 인증번호 규칙: 등록된 휴대폰 번호 숫자의 마지막 4자리.** 예: `010-0000-0001` → `0001`. 실제 문자 발송은 없다.
- 번호의 하이픈은 무시한다. 등록되지 않은 번호도 실패로 센다. 실패 응답에는 고객 ID를 넣지 않는다.
- 응답: `verified`, `customer_id`, `attempts`(세션의 시도 수), `failed_attempts`, `handoff_required`.
- 같은 세션에서 2회 실패하면 그 응답에 `handoff_required: true`를 주고, 이후 본인 확인과 쓰기 호출은 `HANDOFF_REQUIRED`로 거절한다(제19조 제1항).
- 쓰기 호출은 그 세션이 같은 `customer_id`로 본인 확인을 마쳤을 때만 받는다. 아니면 `IDENTITY_REQUIRED`다.

## 멱등 키

- 같은 도구에 같은 키, 같은 내용 → 처음 결과를 그대로 돌려준다.
- 같은 키, 다른 내용 → `INVALID_REQUEST`.
- 이미 접수된 대상(환불은 결제, 해지는 구독, 계좌 등록은 고객)에 다른 키 → `DUPLICATE_REQUEST`와 `error.receipt`에 기존 접수 내역.
- 거절과 오류(주입한 장애 포함)는 저장하지 않는다. 같은 키로 다시 시도할 수 있다.

## quote_refund

입력: `customer_id`, `subscription_id`, `reason`, `request_date`(YYYY-MM-DD). 서버 시계는 쓰지 않는다.

| 필드 | 뜻 |
|---|---|
| `quote_id` | 입력으로 정해지는 ID. 같은 입력이면 같은 ID다. `create_refund_request`에 넣는다. |
| `eligible` | 환불할 금액이 있는지(`refund_amount > 0`). 규정은 적용되지만 0원인 경우(제8조 제3항, 체험 해지)는 `false`다. |
| `refund_amount` | 환불 예정액 합계. `plan_change`에서는 환불이 아니라 연 구독 결제 금액에서 빼는 일할 차감액이다. |
| `refunds` | 결제별 환불 내역(`payment_id`, `amount`, `refund_method`, `account_required`, `expected_period`). 환불 접수는 이 단위로 한다. |
| `breakdown` | 판단에 쓴 사실과 계산 과정(결제, 7일 기한, 이용 이력, 장애, 개월 수, 차감액, 위약금 등) |
| `article_ids` | 판단에 쓴 조항 ID |
| `approval_required` | 환불 예정액이 50,000원을 넘는지(제19조 제2항) |
| `handoff_required`, `handoff_reason` | 제19조 제1항 사유(`minor`, `dispute`) |
| `last_usable_date` | 이대로 처리했을 때 이용 가능한 마지막 날. 구독이 유지되는 사유에는 없다. |
| `message` | 판단 요약 |

약관상 대상이 아니면 `NOT_ELIGIBLE`, 미성년자 결제 취소나 이의 제기 중인 결제면 `HANDOFF_REQUIRED`를 돌려주고, 두 경우 모두 판단 내역을 `error.quote`에 담는다.

## 쓰기 결과

- `create_refund_request`: 견적에 있는 결제만 받는다. 접수일은 견적 기준일이다. 승인 필요 견적이면 `status: pending_approval`, 아니면 `confirmed`. 휴대폰 결제 후 결제한 달이 지나 계좌로 환불하는 경우 계좌가 없으면 `ACCOUNT_REQUIRED`다. 0원 견적과 `plan_change` 견적은 `NOT_ELIGIBLE`이다.
- `create_cancellation`의 `last_usable_date`:
  - 같은 구독에 청약철회·장애·연 구독 중도 해지 환불 접수가 있으면 그 신청일
  - 무료 체험 중이면 체험 종료일 전날
  - 연 구독이면 해지 신청일. 요청에 날짜가 없으므로 `customers.json`의 `reference_date`(2026-10-12)를 업무 기준일로 쓴다.
  - 월 구독·부가 상품이면 다음 결제일 전날
  - 본 구독을 해지하면 부가 상품도 함께 해지한다(`cancelled_subscription_ids`).
- `register_refund_account`: 예금주가 회원 이름과 다르면(공백 무시) `registered: false`, `reject_reason: holder_not_customer`로 거절한다. 응답의 계좌번호는 끝 3자리만 보인다.
- `create_handoff`: `expected_wait_minutes`는 모의 값 5다.

## 오류 코드

| 코드 | HTTP | 비고 |
|---|---|---|
| `IDENTITY_REQUIRED` | 403 | |
| `NOT_ELIGIBLE` | 422 | |
| `HANDOFF_REQUIRED` | 422 | |
| `DUPLICATE_REQUEST` | 409 | |
| `TIMEOUT` | 504 | 장애 주입으로만 생긴다 |
| `UNAVAILABLE` | 503 | 장애 주입으로만 생긴다 |
| `INVALID_REQUEST` | 400 (메서드 오류는 405) | api.md에 없는 코드 |
| `NOT_FOUND` | 404 | api.md에 없는 코드 |
| `ACCOUNT_REQUIRED` | 422 | api.md에 없는 코드 |
| `INTERNAL` | 500 | api.md에 없는 코드 |

## 관리 엔드포인트

인증이 없으므로 로컬 주소에서만 띄운다.

```sh
# 다음 2번의 quote_refund 호출이 TIMEOUT을 돌려준다(count 생략 시 1). code는 TIMEOUT 또는 UNAVAILABLE.
curl -X POST localhost:8081/_admin/faults -d '{"tool":"quote_refund","code":"TIMEOUT","count":2}'
# 세션, 견적, 접수 내역, 멱등 키, 장애 주입, 접수 번호를 모두 비운다. 고객 데이터는 그대로다.
curl -X POST localhost:8081/_admin/reset
```

주입한 장애는 도구를 실행하기 전에 돌려주므로 상태가 바뀌지 않는다.

## 로그

요청마다 표준 출력에 JSON 한 줄을 남긴다: `time`, `session`, `tool`, `method`, `status`, `code`, `latency_ms`. 개인정보가 있으므로 요청·응답 본문은 남기지 않는다.

## 구성

- `cmd/bizapi`: 실행 파일
- `internal/orga`: 조직 A 데이터 읽기와 약관 규칙(견적, 해지 후 이용 마지막 날, 환불 방법). 상태가 없다.
- `internal/server`: HTTP, 세션, 멱등 키, 접수 내역, 장애 주입, 로그
