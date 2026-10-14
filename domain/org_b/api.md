# 조직 B 업무 API와 어댑터

조직 B(책솔, 가상)의 업무 시스템은 조직 A와 계약이 다르다. 상담 엔진은 조직 B 업무 시스템을 직접 부르지 않는다. 어댑터(`bizapi/internal/orgbgw`, `go run ./cmd/orgbgw`)가 엔진의 공통 계약(조직 A 계약과 같은 도구 이름·필드·오류 코드, [`domain/org_a/api.md`](../org_a/api.md), [`bizapi/README.md`](../../bizapi/README.md))을 받아 아래 조직 B 계약으로 바꿔 부른다. 약관은 [`terms.md`](terms.md), 모의 데이터는 [`customers.json`](customers.json)(조직 B 자체 형식)이다.

```
상담 엔진 ──공통 계약(POST /v1/<도구>, X-Session-ID)──▶ 어댑터 127.0.0.1:8098 ──조직 B 계약──▶ 조직 B 업무 시스템 127.0.0.1:8097
```

## 1. 조직 B 업무 API (`bizapi/internal/orgbsrv`, `go run ./cmd/orgb`)

원칙은 조직 A와 같다(조회는 자유, 쓰기는 본인 확인과 고객 확인 뒤, 쓰기는 멱등, 금액·자격은 서버가 계산). 형식이 다르다.

- **시스템 인증:** 모든 `/api/v2` 호출에 `X-Api-Key`가 필요하다. 없거나 틀리면 `401 API_KEY_INVALID`. 키는 어댑터만 갖는다(모의 기본값 `orgb-mock-key`, 환경 변수 `ORGB_API_KEY`).
- **본인 확인:** `POST /api/v2/auth/verify`가 성공하면 **회원 토큰**을 준다. 쓰기는 `Authorization: Bearer <토큰>`이 그 회원의 것이어야 한다(조직 A는 서버가 `X-Session-ID`별로 본인 확인 상태를 둔다). 실패 횟수는 `X-Contact-Ref`(상담 건)마다 세고, **3회** 실패하면 그 상담 건을 잠근다(`423 AUTH_LOCKED`, 조직 A는 2회).
- **모의 인증번호:** 등록된 휴대폰 번호 숫자의 **마지막 6자리**(예: `010-5000-0101` → `000101`). 조직 A는 4자리다.
- **멱등 키:** 본문이 아니라 `Idempotency-Key` 헤더. 같은 키·같은 내용이면 처음 결과, 같은 키·다른 내용이면 `409 IDEMPOTENCY_MISMATCH`, 이미 접수된 대상에 다른 키면 `409 ALREADY_DONE`과 `existing`(기존 접수).
- **응답 봉투:** 성공은 `{"status": "ok", "data": {...}}`, 오류는 `{"status": "error", "error_code": "...", "detail": "...", "clauses": [...], "estimate": {...}, "existing": {...}}`.
- **시각:** 결제·장애 시각은 `+09:00`이 붙은 RFC 3339(`2026-10-03T21:15:00+09:00`). 장애 시간은 분(`minutes`) 단위다.

| 엔드포인트(장애 주입 이름) | 입력 | 출력(`data`) |
|---|---|---|
| `POST /api/v2/auth/verify` (`auth-verify`) | 헤더 `X-Contact-Ref`, 본문 `mobile`, `code` | `ok`, `member_no`, `token`, `failures`, `remaining_tries`, `locked`, `clauses` |
| `GET /api/v2/members/{member_no}` (`member`) | | `member_no`, `member_name`, `birth_date`, `state`(`NORMAL`·`SUSPENDED`) |
| `GET /api/v2/members/{member_no}/passes` (`passes`) | | `items[]`: `pass_no`, `product_code`(`MONTHLY`·`YEARLY`·`FAMILY`), `list_price`, `started_on`, `renews_on`, `state`(`ACTIVE`·`CANCELLED`), `ends_on`, `family_members` |
| `GET /api/v2/members/{member_no}/charges` (`charges`) | | `items[]`: `charge_no`, `pass_no`, `charged_at`, `amount_krw`, `pay_type`(`CARD`·`PHONE`·`POINT`), `chargeback`, `refund_no`, `refund_state` |
| `GET /api/v2/members/{member_no}/reading-log?from=` (`reading-log`) | | `items[]`: `on`, `kind`(`EBOOK_OPEN`·`AUDIO_PLAY`·`DOWNLOAD`), `by`(`OWNER`·`MEMBER`) |
| `GET /api/v2/outages?from=&to=` (`outages`) | | `items[]`: `outage_no`, `began_at`, `ended_at`, `minutes`, `fault`(`OURS`·`VENDOR`) |
| `POST /api/v2/refund-estimates` (`refund-estimates`) | `member_no`, `pass_no`, `refund_type`, `as_of` | `estimate_no`, `refundable`, `total_krw`, `items[]`(`charge_no`, `krw`, `route`, `needs_bank_account`, `eta_text`), `calc`, `clauses`, `needs_review`, `escalate`(`code`: `CHARGEBACK`·`REPEAT_REFUND`), `service_ends_on`, `note` |
| `POST /api/v2/refunds` (`refunds`) | 토큰, 멱등 키, `member_no`, `estimate_no`, `charge_no` | `refund_no`, `state`(`DONE`·`REVIEW`), `krw`, `route`, `eta_text`, `received_on`, `service_ends_on`, `clauses` |
| `POST /api/v2/passes/{pass_no}/cancel` (`pass-cancel`) | 토큰, 멱등 키, `member_no` | `cancel_no`, `service_ends_on`, `members_ended`, `clauses` |
| `PUT /api/v2/members/{member_no}/refund-account` (`refund-account`) | 토큰, 멱등 키, `bank_name`, `account_no`, `owner_name` | `accepted`, `reg_no`, `account_masked`, `reject_code`(`OWNER_MISMATCH`) |
| `POST /api/v2/agent-tickets` (`agent-tickets`) | `member_no`(선택), `topic`, `memo`, 멱등 키(선택) | `ticket_no`, `eta_minutes`(모의 값 7) |

`refund_type`은 `COOLING_OFF`(청약철회, 제5조), `YEARLY_PRORATE`(연 이용권 일할 환불, 제8조), `DOUBLE_CHARGE`(중복 결제, 제10조) 셋뿐이다. 장애 환불, 미성년자 취소, 무료 체험, 요금제 변경 차감은 약관에 없다.

오류 코드:

| 코드 | HTTP | 뜻 |
|---|---|---|
| `BAD_INPUT` | 400 | 입력 형식 오류, 멱등 키 헤더 없음 |
| `API_KEY_INVALID` | 401 | 시스템 키 없음·틀림 |
| `AUTH_REQUIRED` | 401 | 쓰기에 그 회원의 토큰이 없음 |
| `AUTH_LOCKED` | 423 | 본인 확인 3회 실패로 잠긴 상담 건 |
| `NO_SUCH_RESOURCE` | 404 | 없는 회원·이용권·견적·경로 |
| `ALREADY_DONE` | 409 | 이미 접수된 대상(다른 멱등 키) |
| `IDEMPOTENCY_MISMATCH` | 409 | 같은 멱등 키에 다른 내용 |
| `NOT_REFUNDABLE` | 422 | 약관상 환불 대상 아님(`estimate`에 판단 내역) |
| `NEEDS_AGENT` | 422 | 제15조 제1항 사유(`estimate.escalate`) |
| `BANK_ACCOUNT_MISSING` | 422 | 계좌 환불인데 환불 계좌 없음 |
| `SERVER_ERROR` | 500 | 내부 오류 |
| `MAINTENANCE` | 503 | 점검·일시 장애(장애 주입) |
| `UPSTREAM_TIMEOUT` | 504 | 하위 시스템 시간 초과(장애 주입) |

관리 엔드포인트(인증 없음, 로컬 전용): `POST /_mock/reset`, `POST /_mock/faults {"endpoint": "refund-estimates", "mode": "timeout"|"maintenance", "times": 2}`.

## 2. 어댑터: 공통 계약 → 조직 B

어댑터는 엔진 쪽에서 보면 조직 A 모의 서버와 같은 계약이다(`POST /v1/<도구>`, `X-Session-ID` 1~128자, 모르는 필드는 `INVALID_REQUEST`, 오류 본문 `{"error": {"code", "message", "article_ids", "quote", "receipt"}}`, 같은 HTTP 상태). 금액과 자격은 계산하지 않는다(ADR 0004).

| 공통 도구 | 조직 B 호출 | 변환 |
|---|---|---|
| `verify_identity(phone, otp)` | `POST /auth/verify`, `X-Contact-Ref = X-Session-ID` | 성공하면 회원 토큰을 세션에 보관. `attempts`는 어댑터가 센다(조직 B는 주지 않음). `locked` → `handoff_required` |
| `get_customer` | `GET /members/{id}` | `member_name→name`, `birth_date→birth`, `NORMAL→active` |
| `list_subscriptions` | `GET /members/{id}/passes` | `pass_no→subscription_id`, `MONTHLY→monthly`, `YEARLY→annual`, `FAMILY→family`, `renews_on→next_billing`, `ends_on→last_usable_date`, `promo`·`trial`은 `false` |
| `list_payments` | `GET /members/{id}/charges` | `charge_no→payment_id`, `charged_at`(+09:00) → `date`(KST 날짜)와 `paid_at`, `CARD→card`, `PHONE→mobile`, `POINT→point`, `chargeback→dispute`, `DONE→confirmed`, `REVIEW→pending_approval` |
| `get_usage(since)` | `GET /members/{id}/reading-log?from=` | `on→date`, `kind`를 소문자로 `type`, `by=MEMBER → reader=family_member` |
| `list_incidents(from, to)` | `GET /outages` | `outage_no→incident_id`, `minutes/60→hours`, `OURS→company`, `VENDOR→vendor` |
| `quote_refund(reason)` | `POST /refund-estimates` | `withdrawal↔COOLING_OFF`, `annual_cancel↔YEARLY_PRORATE`, `duplicate↔DOUBLE_CHARGE`. `estimate_no→quote_id`, `total_krw→refund_amount`, `items→refunds`(`route`: `CARD_CANCEL→card_cancel`, `PHONE_CANCEL→mobile_cancel`, `BANK_TRANSFER→bank_transfer`, `POINT_RESTORE→point_restore`), `calc→breakdown`, `clauses→article_ids`, `needs_review→approval_required`, `escalate.code`(`CHARGEBACK→dispute`, `REPEAT_REFUND→repeat_refund`) → `handoff_required`·`handoff_reason`, `service_ends_on→last_usable_date`, `note→message`. 조직 B에 없는 사유(`incident`, `minor`, `trial_cancel`, `plan_change`)는 업무 시스템을 부르지 않고 `NOT_ELIGIBLE`(근거 조항 B-09, B-03, 없음, B-11) |
| `create_refund_request` | `POST /refunds`, Bearer, `Idempotency-Key` | `quote_id→estimate_no`, `payment_id→charge_no`. 응답 `state` → `status`, `refund_type` → `reason` |
| `create_cancellation` | `POST /passes/{id}/cancel`, Bearer, `Idempotency-Key` | `service_ends_on→last_usable_date`, `cancelled_subscription_ids=[pass_no]`, `members_ended→family_members_ended` |
| `register_refund_account` | `PUT /members/{id}/refund-account`, Bearer, `Idempotency-Key` | `bank→bank_name`, `account→account_no`, `holder→owner_name`. `accepted→registered`, `OWNER_MISMATCH→holder_not_customer` |
| `create_handoff` | `POST /agent-tickets` | `reason→topic`, `summary→memo`, `ticket_no→handoff_id`, `eta_minutes→expected_wait_minutes` |

어댑터는 쓰기 호출에 그 세션이 같은 고객으로 본인 확인했을 때만 토큰을 붙인다. 인증 판단 자체는 조직 B가 하고(`AUTH_REQUIRED`), 어댑터는 결과를 `IDENTITY_REQUIRED`로 바꾼다.

오류 코드 변환:

| 조직 B | 공통 |
|---|---|
| `BAD_INPUT`, `IDEMPOTENCY_MISMATCH` | `INVALID_REQUEST` |
| `AUTH_REQUIRED` | `IDENTITY_REQUIRED` |
| `AUTH_LOCKED`, `NEEDS_AGENT` | `HANDOFF_REQUIRED`(견적이면 `error.quote`) |
| `NO_SUCH_RESOURCE` | `NOT_FOUND` |
| `ALREADY_DONE` | `DUPLICATE_REQUEST`(`existing` → 공통 `error.receipt`) |
| `NOT_REFUNDABLE` | `NOT_ELIGIBLE`(`estimate` → 공통 `error.quote`) |
| `BANK_ACCOUNT_MISSING` | `ACCOUNT_REQUIRED` |
| `MAINTENANCE` | `UNAVAILABLE` |
| `UPSTREAM_TIMEOUT` | `TIMEOUT` |
| `SERVER_ERROR`, `API_KEY_INVALID`, 모르는 코드, 봉투가 아닌 응답 | `INTERNAL` |
| 연결 실패 / 제한 시간(`-timeout`, 기본 3초) 초과 | `UNAVAILABLE` / `TIMEOUT` |

어댑터는 재시도하지 않는다. 재시도 예산은 엔진 클라이언트가 공통 코드로 쓴다(`TIMEOUT`·`UNAVAILABLE`·`INTERNAL` 1회).

관리 엔드포인트도 공통 형식이다. `POST /_admin/reset`은 어댑터의 세션 토큰을 비우고 조직 B의 `/_mock/reset`을 부른다. `POST /_admin/faults {"tool", "code", "count"}`는 조직 B의 `/_mock/faults`로 넘긴다(`TIMEOUT→timeout`, `UNAVAILABLE→maintenance`). 그래서 재생 파일의 `faults`는 조직 B 오류가 어댑터 변환을 거쳐 공통 코드가 되는 경로를 탄다.

## 3. 실행

```sh
cd bizapi
go run ./cmd/orgb   -addr 127.0.0.1:8097 -data ../domain/org_b/customers.json
go run ./cmd/orgbgw -addr 127.0.0.1:8098 -backend http://127.0.0.1:8097
# 엔진(저장소 루트)
JIKGAM_BIZAPI_URL=http://127.0.0.1:8098 uv run python -m jikgam.engine.chat --org org_b --replay data/replays/org_b/b01.jsonl --reset-bizapi
```
