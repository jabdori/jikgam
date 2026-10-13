# 상담 엔진 구현 설계

`docs/design.md`의 설계를 구현 단위로 옮긴 문서다. 모듈 경계, 이벤트와 상태의 형식, 한 턴의 처리 순서를 정한다. 측정 정의는 `docs/evaluation.md`를 따른다.

## 1. 구성

| 모듈 | 위치 | 하는 일 |
|---|---|---|
| 이벤트 | `jikgam/engine/events.py` | 입력 이벤트 형식, 단조 시계, 이벤트 로그(JSONL) |
| 세션 상태 | `jikgam/engine/state.py` | 대화, 시스템 사실, 고객 진술, 본인 확인, 근거, 제안, 대화 버전 |
| 진술 추출 | `jikgam/engine/claims.py` | 발화에서 날짜·금액·횟수·전화번호·인증번호·계좌를 코드로 뽑는다 |
| 검색 | `jikgam/engine/retrieval.py` | 조항 색인, 하이브리드 검색, 구조 확장 |
| 판별 | `jikgam/decision/` | 공통 인터페이스(ADR 0007). 엔진은 백엔드 이름만 안다 |
| 업무 API | `jikgam/engine/bizapi.py` | 업무 API 클라이언트. 세션 헤더, 멱등 키, 재시도 예산 |
| 정책 | `jikgam/engine/policy.py` | 판별 결과와 시스템 사실로 다음 행동과 도구 호출을 정하는 코드 규칙 |
| 생성 | `jikgam/engine/generate.py` | 선택한 근거와 확인된 사실만으로 응답 문구 생성 |
| 세션 진행 | `jikgam/engine/session.py` | 한 턴의 처리 순서, 작업 취소와 결과 폐기 |
| 조직 설정 | `config/orgs/<org>.toml` | 약관, 업무 API 주소, 판별 백엔드·질문 세트·임계값, 생성 모델, 필수 안내 |
| 채팅 실행 | `jikgam/engine/chat.py` | 터미널 채팅과 고정 대화 재생 |

엔진은 음성 런타임, 웹 화면, 특정 판별 모델에 의존하지 않는다. 음성(Pipecat)과 AI 고객 시뮬레이터는 같은 입력 이벤트를 넣고 같은 출력 이벤트를 받는다.

## 2. 이벤트

모든 이벤트는 세션별 JSONL 로그에 한 줄씩 남는다. 공통 필드는 `t_ms`(엔진 프로세스의 단조 시계), `wall`(KST ISO 시각), `session`, `type`, `version`(그 시점의 대화 버전)이다. 유형은 `docs/evaluation.md` §3.1을 따른다.

입력 이벤트:

```json
{"type": "utterance", "utt_id": "u7", "speaker": "customer", "text": "아 아니다, 칠월 십오일이었어요", "final": true, "t_end_ms": 182345}
```

- 미확정 자막은 같은 `utt_id`로 `final: false`가 여러 번 오고, 마지막에 `final: true`가 온다.
- 채팅은 항상 `final: true`이고 `t_end_ms`는 엔진이 메시지를 받은 시각이다.
- 시뮬레이터는 조건 변경 이벤트를 실행한 발화에 `"marker": {"condition_change": true, "invalidates_prior": true, "event": "s20#0"}`을 붙인다. 엔진은 이 표시를 읽지 않고 로그에만 남긴다.

## 3. 상태와 대화 버전

### 3.1 대화 버전

- 버전은 판단 결과를 바꿀 수 있는 입력이 들어올 때 1씩 오른다. 고객의 확정 발화, 시스템 사실을 바꾸는 도구 결과가 여기에 해당한다.
- 엔진 자신의 발화는 그 버전의 결과이므로 버전을 올리지 않는다.
- 미확정 자막은 버전을 올리지 않는다. 선판단(정책 D)은 `(버전, utt_id)`를 붙인 투기적 작업으로 돌리고, 확정 발화가 오면 결과를 쓰거나 버린다.
- 검색·판별·생성·조회 작업은 시작할 때의 버전을 갖는다. 끝났을 때 세션 버전이 달라졌으면 결과를 쓰지 않고 `result.discarded`를 남긴다. 버전이 오르면 진행 중인 이전 버전 작업은 취소한다. 쓰기 API 호출은 취소하지 않는다(고객 확인 뒤에만 시작하므로).

### 3.2 판별 상태

판별 모델에 보내는 상태는 판별 데이터의 `state`와 같은 형식이다. val에서 잰 성능이 엔진에서도 의미를 갖게 하기 위해서다.

```json
{"org": "org_a", "today": "2026-10-12", "identity_verified": true,
 "conversation": [{"speaker": "customer", "text": "...", "final": true}],
 "known_facts": {"customer_id": "C003", "subscription": {...}, "payments": [...], "request": "cancel_refund", "payment_date": "2026-06-말(고객 진술)"},
 "articles": [{"id": "A-08", "title": "...", "text": "..."}],
 "previous_suggestion": "..."}
```

- `known_facts`는 마지막 고객 발화 **이전**까지의 사실이다. 마지막 발화에서 뽑은 진술은 판단이 끝난 뒤 반영한다(`condition_changed`를 판단할 수 있게).
- 시스템 조회 값은 그대로, 고객 진술은 값 뒤에 `(고객 진술)`을 붙인다(ADR 0003).
- `request`에는 직전 판단의 intent 라벨을 둔다.

## 4. 한 턴의 처리 순서

고객 확정 발화가 들어오면:

1. 버전을 올리고(`version.bumped`) 이전 버전 작업을 취소한다.
2. 발화에서 진술을 뽑는다(코드). 판별 상태에는 아직 넣지 않는다.
3. 검색: 최근 고객 발화와 현재 요청으로 후보 조항을 찾고 구조로 확장한다.
4. 판별 1회: intent, condition_changed(기록된 사실이 있을 때), 후보 조항별 applies, sufficient, next_info, action, suggestion_valid(이전 제안이 있을 때)를 한 요청으로 묻는다.
5. 근거 선택(코드): applies 확률이 임계값 이상인 조항. 이전 제안은 `suggestion_valid`가 거짓이거나 `condition_changed`가 참이면 무효화한다(`suggestion.invalidated`).
6. `evidence.updated`를 남긴다. 조건 변경부터 근거 갱신까지의 시간은 여기서 끝난다.
7. 정책(코드)이 다음 행동과 도구 호출을 정한다(§5). 도구 결과가 시스템 사실을 바꾸면 버전을 올리고 3번부터 다시 한다.
8. 생성: 행동이 정해지면 응답 문구를 만든다. 끝났을 때 버전이 바뀌었으면 버린다.
9. 제안을 만들고(`suggestion.created`) 채널에 낸다(`suggestion.exposed`).

## 5. 정책: 코드가 정하는 것

val 기준선(2026-10-14)에서 판별 모델이 가장 약했던 질문은 next_info와 action이었다. 두 질문의 정답은 대부분 정해진 우선순위 규칙이므로, 엔진은 아래 규칙을 코드로 먼저 적용하고 규칙이 정하지 못하는 경우에만 판별 결과를 쓴다.

| 조건(코드로 확인) | 행동 |
|---|---|
| 업무 API가 인계 사유를 돌려줌(`HANDOFF_REQUIRED`, `handoff_required`, 본인 확인 2회 실패) | handoff, `create_handoff` |
| 업무 API 장애(재시도 예산 소진) | hold |
| 본인 확인 전이고 요청 처리에 고객 기록이 필요함 | ask, 본인 확인 |
| 발화에 전화번호와 인증번호가 있음 | `verify_identity` |
| 본인 확인 직후 | 조회 API 병렬 호출 → 시스템 사실 |
| 환불 요청이고 계산 입력이 확인됨 | `quote_refund`(사유 후보를 병렬로 묻고 대상인 견적을 고른다) |
| 계좌 환불이 필요한데 계좌가 없음 | ask, 계좌 |
| 접수 내용을 안내했고 고객이 동의함 | 쓰기 API(멱등 키: 세션·대상·버전) |
| 그 밖 | 판별 결과의 action과 next_info |

금액과 자격은 `quote_refund` 결과만 쓴다(ADR 0004).

## 6. 생성

- 입력: 선택한 조항 본문, 시스템 사실, 견적 결과, 정책이 정한 행동과 확인할 정보, 조직 설정의 필수 안내.
- 출력(JSON): `say`(고객에게 할 말), `cited_articles`, `amount`(안내한 금액), `amount_final`(확정 금액으로 안내했는지). 시나리오 판정의 금액 항목은 이 필드로 본다.
- 확인되지 않은 사실은 확정하지 않고, 금액 확답과 접수는 고객 확인을 거친다.

## 7. 조직 설정

```toml
org = "org_a"
terms = "domain/org_a/terms.md"
today = "2026-10-12"            # 가상 데이터 기준일
bizapi_url = "http://127.0.0.1:8081"
decision_backend = "jev"
question_set = "data/questions.json"
applies_threshold = 0.5
generator = "gemini-flash"
required_notices = ["환불 예정액과 산정 근거", "환불 방법과 예상 소요 기간", "이용 가능한 마지막 날"]
```

조직 B는 이 파일과 업무 API 어댑터만 추가한다. 엔진 코드를 고쳐야 했다면 무엇을 왜 고쳤는지 기록한다.
