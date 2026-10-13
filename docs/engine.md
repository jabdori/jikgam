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
| 채팅 실행 | `jikgam/engine/chat.py` | 터미널 채팅과 고정 대화 재생(`--policy A\|B\|C`) |
| 정책 A 기준선 | `jikgam/baselines/rag.py` | 개선형 RAG(`docs/evaluation.md` §5.1). 엔진과 같은 입출력 이벤트(§10) |

엔진은 음성 런타임, 웹 화면, 특정 판별 모델에 의존하지 않는다. 음성(Pipecat)과 AI 고객 시뮬레이터는 같은 입력 이벤트를 넣고 같은 출력 이벤트를 받는다. 음성 연결 방식은 `docs/voice.md`에 있다.

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

## 8. 구현 메모 (2026-10-14, 채팅판 첫 구현)

위 설계와 다르거나 더한 부분이다. 이유는 `PROGRESS.md` 2026-10-14에 있다.

- 멱등 키는 (세션, 도구, 대상)에서 만든다(§5 표의 "세션·대상·버전" 대신). 버전은 `tool.call` 로그에 남긴다.
- 동의 판단(§5 "고객이 동의함")은 엔진 전용 질문 `customer_agreed`(`data/question_sets/extra/engine-extra.json`)와 접수 내용 서명(쓰기 인자와 환불 금액)으로 한다. 노출한 제안이 무효화되었어도 `condition_changed`가 참이 아니고 서명이 같으면 동의로 본다.
- 이전 제안 무효화(§4 5단계)에 코드 규칙을 더했다: 판단한 intent가 제안을 만들 때의 intent와 다르면 무효화한다.
- 같은 발화로 3단계부터 다시 할 때는 applies, sufficient, next_info, action만 묻는다.
- 진행 중 작업이 취소되면 `task.cancelled`, 취소되지 않고 끝난 결과가 지난 버전이면 `result.discarded`를 남긴다. 단계별 지연을 보려고 `session.start`, `claims.extracted`, `retrieval.done`, `policy.decided`, `quote.selected`, `generation.request`, `generation.response`, `agent.say`, `engine.error`를 더했다.
- 검색은 키워드(한국어 글자 바이그램 BM25)와 구조 확장만 쓴다. 임베딩은 아직 없다.
- 조직 설정에 `extra_question_set`, `agreement_threshold`, `[retrieval]`(후보 수, 구어→약관 낱말, 절차 조항 규칙), `[policy]`(요청별 견적 사유, 견적 우선순위, 환불 접수 사유)를 더했다.

## 9. 구현 메모 (2026-10-14, 정책 보강)

§5 표와 §8에 더한 것이다. 정책 규칙의 전체 순서는 `jikgam/engine/policy.py` 머리말에 있다. 이유는 `PROGRESS.md` 2026-10-14 "상담 엔진: 정책 보강과 정책 A"에 있다.

- **통화자 관계.** 엔진 전용 질문 `caller`(choice: owner, third_party, guardian)를 첫 판단마다 묻는다. 제3자·법정대리인으로 바꿀 때는 `caller_threshold`, 제3자로 본 뒤 본인으로 돌아갈 때는 더 높은 `owner_return_threshold`가 필요하다. 관계가 바뀌면 이전 제안을 무효화한다(`caller_changed`). 친족 낱말 정규식이 아니라 판별 질문으로 정한 이유: 관계는 계정 소유자와의 관계라서 "제 카드로"(카드 주인), "아내 명의 계좌로"(환불 계좌), "엄마 몰래"(미성년 본인), "딸이 회사에 있어서"(성인 자녀)처럼 같은 낱말이 다른 뜻이 되고, 전화를 바꾸는 경우(s29)는 대화 흐름으로만 알 수 있다. 코드는 비대칭 임계값, 법정대리인 권한의 결정적 확인(결제 당시 나이), 제3자에게 본인 확인 규칙을 막는 일을 맡는다.
- **제3자**(정책 규칙 0): 계정 처리 요청이거나 번호·인증번호를 말하면 거절 안내(`third_party`). 본인 확인을 묻지도 하지도 않는다. 생성 입력에서 시스템 사실, 견적, 고객 진술을 빼고 근거 조항은 `third_party_articles`.
- **법정대리인**(규칙 7): 미성년 회원 계정의 휴대폰으로 본인 확인(제15조 제1항 단서)과 조회를 한 뒤, `guardian_reasons`의 사유(minor)가 결제 당시 나이로 맞으면 그 사유만 견적한다. 업무 API가 `HANDOFF_REQUIRED`를 주면 규칙 1이 `create_handoff(customer_id, reason=minor)`를 부른다. 사유가 맞지 않으면 제3자와 같이 거절한다.
- **부가 상품만 해지**(규칙 8): `addon_only`는 유효한 부가 상품 구독이 조회됐을 때만 묻는다. 참이면 견적 없이 부가 상품 해지만 안내하고(`addon_articles`, 환불 없음), 동의하면 그 구독만 해지한다. 이용 마지막 날은 해지 접수 결과(업무 API)의 값이다.
- **접수 거절**(규칙 12): `customer_declined`가 임계값 이상이면 같은 접수 내용을 쓰지도 다시 묻지도 않는다(그 발화에서만). 동의와 거절이 함께 높으면 쓰지 않는다.
- **본인 확인 불가**(규칙 4): `identity_unavailable`은 본인 확인을 물었거나 한 번 실패한 뒤에만 묻고, 참이면 보류한다. 그 발화에 번호나 인증번호가 있으면 판별과 관계없이 본인 확인을 계속한다.
- **생성 제약.** 정책의 근거 조항(`Plan.articles`)을 생성 입력에 반드시 넣는다. 인계·보류에서는 견적을 빼고, 거절에서는 계정 정보를 뺀다. 고객 계정 일을 다루는 응답이면(본인 확인됨, 계정 처리 요청, 제3자·법정대리인) 대상 견적이나 환불 접수 없이 "환불이 된다·받을 수 있다"고 말하지 못하게 코드로 검사한다(`generate.refund_claims`, 서술어 꼴만, 뒤따르는 부정·여부·기한 경과 표현은 제외). 위반이면 다시 만들고, 그래도 위반이면 고정 문구다.
- **업무 API 장애**는 세션 끝까지 기억한다. 같은 세션에서 그 도구를 다시 부르지 않는다(§8 이후의 "새 발화마다 다시 시도"를 바꿈).
- **세션 시작 예열**: `Session.start()`가 판별 백엔드에 고정 요청을 한 번 보내고 `warmup` 이벤트로 지연과 비용을 남긴다. `decision.*` 이벤트로는 세지 않는다. 예열이 끝나기 전에 고객 발화가 오면 기다린다.
- `agent.say`에도 `cited_articles`, `amount`, `amount_final`을 남긴다(`suggestion.created`와 같은 값).
- 환경 변수 `JIKGAM_BIZAPI_URL`이 조직 설정의 `bizapi_url`보다 우선한다.
- 재생 파일의 줄에 선택 필드 `faults`(모의 업무 API `/_admin/faults` 본문 목록)를 두면 그 줄을 보내기 직전에 장애를 넣고 `fault.injected` 이벤트를 남긴다(로컬 주소만). `--policy A|B|C`로 실행 정책을 고른다.
- 조직 설정에 더한 키: `decline_threshold`, `caller_threshold`, `owner_return_threshold`, `addon_threshold`, `identity_unavailable_threshold`, `[policy]`의 `addon_intents`, `addon_articles`, `third_party_articles`, `guardian_reasons`, `guardian_articles`.

## 9.1 구현 메모 (2026-10-14, 시뮬레이터 실행 뒤 보강)

시뮬레이터 실행 `results/engine/scenarios/20261014T040727-C`(보강 전 엔진)에서 나온 실패를 고친 것이다. 정책 규칙의 순서는 `jikgam/engine/policy.py` 머리말에 있다.

- **계좌 진술**(s08): 목록에 없는 은행은 "…은행"으로 말한 이름 그대로 받는다(계좌번호나 '계좌'와 함께 말할 때만). 은행이나 '계좌'가 같은 발화에 있으면 9자리 계좌번호를 받는다(8자리는 날짜와 겹쳐 받지 않는다). 예금주 이름 뒤의 "로/으로"를 읽는다. 예금주 이름이 회원 이름(조회 결과)과 다르면 등록을 부르지 않고 본인 명의 계좌를 다시 묻는다.
- **환불에 따른 해지**(s07, s13): 환불만 요청해도 그 환불이 이용 기간을 끝내는 사유(`refund_ends_period_reasons`: 청약철회 제5조 제3항, 장애 환불 제9조 제2항, 연 구독 중도 해지 제8조 제4항)면 해지를 함께 안내하고 접수한다. 이용 기간이 끝났는데 구독을 남겨 두면 다음 자동 결제가 이루어지기 때문이다. 고객 동의 전에 안내하므로 고객은 해지가 함께 된다는 것을 보고 동의한다. 중복 결제는 구독을 유지한다.
- **차감액**(s16): `deduction_reasons`(plan_change)의 견적은 생성 입력에서 `kind: deduction`으로 표시한다. 그 금액은 환불 금액(`amount`)이 될 수 없고, 환불 주장의 근거도 아니다.
- **본인 확인 반복 실패**(s25): 업무 API는 가입되지 않은 번호와 틀린 인증번호를 구분해 주지 않는다(실패 응답에 고객 ID도 없다). 그래서 실패 안내는 "인증번호 불일치"가 아니라 "그 번호와 인증번호로 확인되지 않음"으로 한다. 이전 발화에서 실패한 번호·인증번호를 다시 말하면 다시 보내지 않고 보류하며(계정을 찾을 수 없음, 다른 번호·가족 가입·카드 명세서 확인 안내), 새 번호나 인증번호가 오기 전까지 보류를 유지한다(통화자가 제3자로 바뀌어도).
- **승인 대상 환불**(s03): 견적의 `approval_required`나 접수 결과 `pending_approval`이면 `approval_notice`, `approval_articles`(A-19), `approval_must_mention`("승인")을 붙인다.
- **장애 보상 시간**(s27): `[policy.compensation]`(귀책, 최소 시간, 배수, 근거 조항)으로 장애 기록에서 코드가 연장 시간을 계산하고 `must_mention`에 넣는다(5시간 × 3 = "15시간"). 연장을 적용하는 업무 API가 없어 대상과 시간만 안내한다(`applied: false`).
- **must_mention 검사**: 생성 문장에 빠지면 한 번 다시 만들고, 그래도 빠지면 위반을 기록한 채 두 번째 결과를 쓴다. 금액·환불 주장 위반과 달리 고정 문구로 바꾸지 않는다.
- **판별 결과의 본인 확인 질문**(s14, s16): 본인 확인을 마쳤거나 통화자가 제3자면 판별의 next_info=identity를 쓰지 않는다(ask면 answer로 바꾸고 `detail.overridden`에 남긴다).
- **빠른 정정과 예열**(`docs/evaluation.md` §3.3 보완): 버전은 발화가 도착할 때 올리고 진행 중인 작업을 취소한다. 예열을 기다리던 발화는 예열 뒤에 가장 최근 발화만 진행한다. 앞 턴이 시작한 쓰기는 취소하지 않고, 새 발화의 파이프라인은 정책 단계 전에 쓰기가 끝나기를 기다린다(`writes.wait`). 쓰기 결과로 버전이 오르면 새 파이프라인은 취소되고 다시 진행한다. `tests/test_engine_concurrency.py`가 느린 가짜로 이 경우들과 "새 발화 뒤에 지난 버전 제안이 노출되지 않는다"는 조건을 확인한다.
- 조직 설정에 더한 키: `[policy]`의 `refund_ends_period_reasons`, `deduction_reasons`, `approval_articles`, `approval_notice`, `approval_must_mention`, `[policy.compensation]`(`intents`, `causes`, `min_hours`, `multiplier`, `articles`).
- **주장한 사유의 견적**(코디네이터 결정, s27): `window_days` 필터는 고객이 주장하지 않은 사유만 거른다. 고객 발화 하나가 `[[policy.claim_patterns]]`의 `all` 정규식에 모두 맞고 `none`에 하나도 맞지 않으면 그 사유를 주장한 것이다. 주장한 청약철회·장애 환불은 기간이 지나도 견적을 물어, 업무 API의 판단(NOT_ELIGIBLE과 근거 조항)으로 거절을 설명한다(ADR 0004). 판별 확률은 쓰지 않는다(흔들리면 견적 수가 늘어난다). 패턴은 train 분할과 개발용 시나리오·재생의 고객 발화로 맞췄다.

## 10. 정책 A(개선형 RAG) 구현

`jikgam/baselines/rag.py`. `docs/evaluation.md` §5.1을 구현한 방식과 다른 점은 모듈 머리말에 있다. 요약:

- 엔진 Session과 같은 인터페이스(`start`, `handle`, `aclose`)와 같은 이벤트 로그 형식. 같은 재생 파일과 지표 코드를 쓴다.
- 검색은 엔진과 같은 함수와 조직 설정이다. 판별 계층이 없어 검색 상태의 요청(intent)은 비우고, 업무 API 결과에서 바로 나오는 상태만 넣는다. §5.1의 임베딩 검색은 엔진과 마찬가지로 아직 없다.
- 고객 확정 발화마다 Gemini 함수 호출 도구 루프를 한 번 돈다(엔진의 응답 생성 모델과 같은 설정). 업무 API 11개와 `final_answer`(say, cited_articles, amount, amount_final, action)를 함수로 주고 호출 방식은 ANY다. 대화 기록(이전 답, 도구 호출과 결과)을 이어 보낸다. 운영 규칙(본인 확인 먼저, 금액은 견적만, 동의 뒤 접수, 제3자 거절, 장애 시 보류, 인계, 안내 의무)은 시스템 프롬프트에 적는다. 엔진의 생성 검사는 하지 않는다.
- 판별, 대화 버전, 무효화, 취소가 없다. 발화는 받은 순서대로 하나씩 답하고, 앞 답을 만드는 동안 새 발화가 와도 앞 답을 취소하지 않고 노출한다. 음성에서는 고객이 말하는 동안 이전 답이 계속 만들어져 재생되므로 지난 조건의 답이 나갈 수 있다.
- `evidence.updated`는 답이 나온 시점에 남긴다(그 답이 근거로 든 조항, 버전은 그 답을 만들 때 본 고객 확정 발화 수). RAG에서 근거와 다음 행동이 정해지는 시점이 그때이기 때문이다.
- 예열은 없다(판별 백엔드가 없고, 엔진도 생성 모델은 예열하지 않는다).
