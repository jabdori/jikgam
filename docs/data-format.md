# 판별 데이터 형식과 라벨 기준

판별 데이터 한 건은 "상담 중 어느 한 순간"이다. 그 순간의 대화와 확인된 사실(state)을 주고, 상담 엔진이 실제로 던지는 질문들(questions)에 대한 정답(gold)을 붙인다.

같은 파일을 세 곳에 쓴다.

- Jev API 평가: state와 questions를 그대로 요청에 넣는다.
- 로컬 판별 모델 평가와 학습: Unsloth decision 형식(`state`, `questions`, `gold` 열)으로 내보낸다.
- 생성형 LLM 판단 기준선: 같은 state와 질문을 프롬프트로 바꿔 넣는다.

## 원본 형식 (`data/cases/*.jsonl`)

사람과 에이전트가 작성하는 형식이다. 조항 본문 대신 조항 ID만 적고, 빌드 스크립트가 `domain/org_a/terms.md`에서 본문을 채운다. 조항 문구를 고치면 모든 사례에 자동 반영된다.

```json
{
  "id": "g012-2",
  "group": "g012",
  "tags": ["correction", "multi_evidence"],
  "state": {
    "org": "org_a",
    "today": "2026-10-12",
    "identity_verified": false,
    "conversation": [
      {"speaker": "customer", "text": "연간 결제한 거 해지하고 남은 돈 받고 싶어요", "final": true},
      {"speaker": "agent", "text": "언제 결제하셨는지 기억나세요?", "final": true},
      {"speaker": "customer", "text": "유월 말쯤이었던 것 같아요", "final": true},
      {"speaker": "agent", "text": "6월 말이시군요.", "final": true},
      {"speaker": "customer", "text": "아 아니다, 칠월 십오일이었어요. 휴가 가기 전에 했거든요", "final": true}
    ],
    "known_facts": {"plan": "annual", "payment_date": "2026-06-말(고객 진술)"},
    "article_ids": ["A-05", "A-08", "A-11", "A-21"],
    "previous_suggestion": null
  },
  "questions": ["intent", "condition_changed", "applies:A-05", "applies:A-08", "applies:A-11", "applies:A-21", "sufficient", "next_info"],
  "gold": {
    "intent": "cancel_refund",
    "condition_changed": "true",
    "applies:A-05": "false",
    "applies:A-08": "true",
    "applies:A-11": "true",
    "applies:A-21": "false",
    "sufficient": "false",
    "next_info": "identity"
  },
  "rationale": "결제일을 6월 말에서 7월 15일로 정정했다. 7월 1일 이후 결제라 부칙 경과 조치(A-21)는 빠지고, 결제 후 7일이 지나 청약철회(A-05)도 아니다. 프로모션 가입 여부를 모르므로 A-11은 결과를 바꿀 수 있어 필요하고, 결제 금액이 미확인이라 충분하지 않다. 금액 확인에는 계정 조회가 필요하므로 본인 확인이 먼저다.",
  "source": "synthetic"
}
```

필드 규칙은 다음과 같다.

- `group`: 같은 대화에서 나온 사례들의 묶음. 분할은 group 단위로 하므로, 한 대화의 앞뒤 시점이나 문구만 바꾼 변형은 반드시 같은 group에 둔다.
- `known_facts`: 마지막 발화 **이전**까지 엔진이 기록해 둔 사실. 마지막 발화의 내용은 여기 반영하지 않는다. 그래야 `condition_changed`를 판단할 수 있다.
- `identity_verified`: 본인 확인을 마쳤는지. 마쳤다면 시스템 조회 결과가 `known_facts`에 들어 있을 수 있다.
- 고객 진술로만 알게 된 사실은 값 뒤에 `(고객 진술)`을 붙인다. 시스템 조회로 확인된 값은 그대로 쓴다. 금액 계산과 접수에 쓰는 사실은 시스템으로 확인돼야 확정된 것으로 본다.
- `conversation[].final`: STT 확정 여부. `false`는 미확정 자막이다.
- `article_ids`: 검색 단계가 후보로 넘긴 조항. 정답 조항만 넣지 말고, 헷갈리는 유사 조항과 무관한 조항을 섞는다.
- `previous_suggestion`: `suggestion_valid`를 묻는 사례에서만 쓴다.
- `rationale`: 정답의 이유. 검토자가 읽는 용도이며 모델 입력에는 들어가지 않는다.
- `source`: `synthetic`(합성), `reviewed`(사람 검토 완료) 등.

## 질문 세트

질문 문구는 `data/questions.json`에 고정되어 있다. 사례는 질문 ID만 고른다. 한 사례에 3~8개 질문을 둔다.

| ID | 유형 | 묻는 것 |
|---|---|---|
| `intent` | choice | 고객이 원하는 처리 |
| `condition_changed` | noul | 마지막 발화가 기록된 사실을 바꾸거나 정정하는가 |
| `applies:<조항ID>` | noul | 그 조항이 지금 처리에 필요한 근거인가 |
| `sufficient` | noul | 후보 조항과 확인된 사실만으로 처리 결과를 확정할 수 있는가 |
| `next_info` | choice | 다음에 확보할 정보 |
| `suggestion_valid` | noul | 이전 응답 제안이 지금도 유효한가 |
| `action` | choice | 다음 행동(답변, 질문, 인계, 보류) |

## 라벨 기준

### 공통
- 미확정 자막(`final: false`) 뒤에 확정 발화가 이어지면 확정 발화 기준으로 판단한다. 미확정 자막은 그 뒤에 확정 발화가 없을 때만 판단 근거가 된다.
- 고객 쪽 화자가 바뀌어도(예: 부모가 전화를 넘겨받음) `speaker`는 `customer`로 두고, 누가 말하는지는 발화 내용과 `known_facts.caller`로 드러낸다.
- 고객이 원하는 처리는 `known_facts.request`에 기록한다(`refund`, `cancel` 등 intent 라벨 값). 요청 철회나 변경을 `condition_changed`로 판단할 수 있게 하기 위해서다.
- 장애 기록처럼 고객 계정 밖의 조회 결과도 `known_facts`에 들어 있어야 확인된 것으로 본다.
- 시스템 조회 결과가 `known_facts`에 있으면 그것이 기준이다. 고객이 기록과 다른 주장(없는 결제, 다른 결제일)을 해도, 이미 조회된 기록으로 판단할 수 있으면 다시 묻지 않고 기록을 근거로 안내한다.

### intent
- 고객이 부정한 요청은 제외한다. "해지하려는 건 아니고요, 이번 달 거만 돌려받고 싶어요"는 `refund`.
- 해지 의사와 환불 요구가 모두 있으면 `cancel_refund`.
- 청약철회 요청은 결과적으로 이용이 끝나더라도, 고객이 해지를 따로 말하지 않았으면 `refund`.
- 가입 전 문의처럼 기존 결제나 구독과 무관한 질문은 `other`.
- 장애 보상으로 이용 기간 연장을 원하면 `compensation`.
- 첫 발화가 "결제가 이상해요"처럼 모호하면 `unclear` 또는 `billing_inquiry` 중 대화 내용에 맞는 것.

### condition_changed
- `true`: 마지막 발화가 `known_facts`에 기록된 값과 다른 내용을 주장한다(정정, 번복, 요청 철회·변경). 시스템 기록과 다른 주장이어도 주장 자체가 달라졌으면 `true`다. 그 주장을 믿을지는 다른 질문에서 다룬다.
- `false`: 같은 내용을 반복하거나, 기록되지 않은 새 사실을 처음 말한 경우. 시스템이 정한 처리 방침(예: `refund_route`가 본인 명의 계좌)에 맞지 않는 구체적인 값을 처음 제시하는 것(예: 배우자 명의 계좌)도 새 사실이다. 고객이 기록되지 않았던 자신의 이전 말과 다른 시스템 값을 받아들이는 경우도 `false`다.
- 대화 첫 발화처럼 `known_facts`가 비어 있는 시점에는 이 질문을 넣지 않는다.

### applies
- "이 조항 없이 처리하면 틀릴 수 있는가"를 기준으로 한다.
- 적용 요건이 이미 거짓으로 확인된 조항은 `false`. 예: 이용 정지가 아닌 계정에 제16조, 환불 예정액이 50,000원 이하이고 인계 사유도 없을 때 제19조.
- 요건이 아직 확인되지 않았더라도 그 결과가 처리에 영향을 줄 수 없으면 `false`. 예: 중복 결제 환불에서 계정 정지 여부를 모를 때 제16조(중복 결제는 정지와 무관하게 환불).
- 요건이 아직 확인되지 않아 결과를 바꿀 수 있는 조항은 `true`. 예: 이용 이력을 아직 모를 때 제6조.
- 다른 조항을 가리키기만 하고 지금 결과를 정하지 않는 조항은 `false`. 예: 순수한 청약철회 사례에서 제7조 제3항.
- 두 조항이 같은 결과를 주면 그 처리를 직접 규정하는 조항만 `true`. 예: 월 구독 해지에서 제7조 제2항은 `true`, 같은 내용인 제4조 제3항은 `false`.
- 절차 조항:
  - 제14조(환불 방법), 제20조(안내 의무)는 환불이나 해지를 안내·접수하는 시점이면 `true`. 고객이 환불 시기나 방법을 묻는 시점도 포함한다. 제20조는 환불 없는 해지에도 적용된다.
  - 제15조(본인 확인)는 본인 확인 전에 해지·환불을 요청받았거나 제3자가 요청하는 시점이면 `true`. 계정 소유자 본인 확인을 마친 뒤에는 `false`.
  - 제19조는 인계 사유나 50,000원 초과 환불이 확인됐거나 아직 확인되지 않아 가능성이 있으면 `true`.

### sufficient
- "처리 결과"는 자격(가능 여부), 금액 계산 입력, 처리 방법이다. 계좌번호처럼 처리를 실행할 때 받을 값이 아직 없다는 이유로는 `false`가 되지 않는다.
- 처리 결과를 바꿀 수 있는 사실이 하나라도 미확인이면 `false`.
- 결과를 바꿀 수 있는 조항이 후보에 없으면 `false`. 리랭킹으로는 해결되지 않는 검색 누락 사례다. 빠진 조항이 이 상황에서 결과를 바꾸지 않으면 영향이 없다.
- 금액 자체는 시스템이 계산하므로, 계산 입력(결제 금액, 결제일, 프로모션 여부 등)이 확인됐으면 `true`.
- 인계 대상 사례도 같은 기준으로 판단한다. 인계 여부는 `action`이 다룬다.

### next_info
- 본인 확인 전이고 고객 기록 조회가 필요하면 `identity`가 우선이다.
- 본인 확인 후에는 시스템 조회로 알 수 없거나 아직 조회되지 않은 것만 고른다. 예: 대상 구독이 모호하면 `plan_type`, 조회가 실패해 이용 이력이 없으면 `usage`, 결제 내역이 아직 조회되지 않았는데 고객이 특정 결제를 말하면 `payment_date`. 결제 내역이 이미 조회돼 있으면 기록이 기준이므로 그 결제를 묻지 않는다.
- 휴대폰 결제를 결제한 달이 지나 환불하는데 계좌가 없으면 `account`.
- 더 확인할 정보가 없으면 `none`. 필요한 조항이 후보에 없어서 막힌 경우도 정보 문제가 아니므로 `none`.

### suggestion_valid
- 마지막 발화로 정책 판단이 바뀌면 `false`. 문구만 어색한 것은 `true`.

### action
- 우선순위는 `handoff` > `hold` > `ask` > `answer`다. 여러 조건이 겹치면 앞의 것을 고른다.
- `handoff`: 제19조 제1항 사유(미성년자 취소, 이의 제기 진행 중, 본인 확인 2회 실패)가 확인됐을 때. 필요한 조항이 후보에 없어도 인계 사유가 확인됐으면 `handoff`.
- `hold`: 후보 조항끼리 충돌하거나, 결과를 바꿀 조항이 후보에 없는데 고객이 확답을 요구하는 경우. 업무 API 장애로 확인이 불가능한 경우도 포함한다.
- `ask`: 처리 결과를 바꿀 정보가 미확인이거나, 처리 실행에 필요한 값(계좌 등)을 받아야 하는 경우.
- `answer`: 안내하거나 처리를 진행한다. 약관상 거절(제3자 요청, 대상 아님)을 안내하는 것도 `answer`다. 50,000원 초과 환불의 상담사 승인(제19조 제2항)은 접수 후 승인이므로 `answer`다.

## 실패 유형 태그

| 태그 | 의미 |
|---|---|
| `correction` | 고객이 앞서 말한 사실을 정정 |
| `negation` | 부정 표현("~하려는 건 아니고요") |
| `ellipsis` | 생략·지시어("그거", "아까 그 돈") |
| `colloquial` | 구어체와 약관 문어체의 차이 |
| `number_date` | 숫자·날짜·금액 해석 |
| `stt_error` | STT 오인식이 섞인 발화 |
| `partial_stt` | 미확정 자막 기준 판단 |
| `similar_article` | 헷갈리는 유사 조항 구분 |
| `exception` | 일반 규정과 예외 조항 구분 |
| `multi_evidence` | 여러 조항을 함께 봐야 함 |
| `missing_evidence` | 필요한 조항이 후보에 없음 |
| `insufficient_info` | 정보 부족으로 판단 불가 |
| `stale_suggestion` | 이전 제안의 무효화 |
| `handoff` | 상담사 인계 사유 |

## 내보내기 형식 (`data/build/*.jsonl`)

빌드 스크립트가 만든다. 한 줄이 한 사례이며 `id`, `group`, `split`, `tags`, `state`, `questions`, `gold` 열을 갖는다. `state`, `questions`, `gold`는 JSON 문자열이다. `questions`는 질문 ID를 키로 한 객체이고, `gold`는 질문 ID별로 `{"type": ..., "label": ...}`를 갖는다. noul의 라벨은 `"true"`/`"false"` 문자열이다.

이 형식은 Hugging Face의 `LocalLLaMA/typed-decisions` 데이터셋의 열 구성을 따른 것이다. Unsloth `build_dataset`이 이 형식을 그대로 받는지는 GPU 환경에서 아직 확인하지 않았다.

## 분할

- group 단위로 train 60%, val 20%, test 20%를 목표로 나눈다. 실패 유형 태그와 action 정답이 분할마다 고르게 퍼지도록, 드문 태그를 가진 group부터 그 태그가 가장 부족한 분할에 배정한다.
- 분할 결과는 `data/splits.json`에 저장하고 커밋한다. 한 번 test에 배정된 group은 바뀌지 않으며, 이후 추가되는 group은 train과 val에만 배정된다.
- 튜닝과 임계값 선택에는 val만 쓴다. test는 모델·질문 문구·임계값을 고정한 뒤 평가에만 쓴다.
