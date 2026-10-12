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

### intent
- 고객이 부정한 요청은 제외한다. "해지하려는 건 아니고요, 이번 달 거만 돌려받고 싶어요"는 `refund`.
- 해지 의사와 환불 요구가 모두 있으면 `cancel_refund`.
- 첫 발화가 "결제가 이상해요"처럼 모호하면 `unclear` 또는 `billing_inquiry` 중 대화 내용에 맞는 것.

### condition_changed
- `true`: 마지막 발화가 `known_facts`의 값과 다르다(정정, 번복, 이전 요청 철회).
- `false`: 같은 내용을 반복하거나, 기록되지 않은 새 사실을 처음 말한 경우.
- STT 미확정 발화(`final: false`)에서 정정처럼 들려도, 확정 발화 기준으로 판단한다. 미확정 발화만 있고 그 내용이 정정이면 `true`로 하되 `tags`에 `partial_stt`를 붙인다.

### applies
- "이 조항 없이 처리하면 틀릴 수 있는가"를 기준으로 한다.
- 적용 요건이 이미 거짓으로 확인된 예외 조항은 `false`. 예: 이용 정지가 아닌 계정에 제16조.
- 요건이 아직 확인되지 않아 결과를 바꿀 수 있는 조항은 `true`. 예: 이용 이력을 아직 모를 때 제6조.
- 처리 안내에 반드시 필요한 절차 조항(제14조 환불 방법, 제20조 안내 의무, 제19조 승인)은 그 처리를 실제로 안내·접수하는 시점이면 `true`.

### sufficient
- 처리 결과를 바꿀 수 있는 사실이 하나라도 미확인이면 `false`.
- 필요한 조항이 후보에 없으면 `false`. 리랭킹으로는 해결되지 않는 검색 누락 사례다.
- 금액 자체는 시스템이 계산하므로, 계산 입력(결제 금액, 결제일, 프로모션 여부 등)이 확인됐으면 `true`.

### next_info
- 본인 확인 전이고 고객 기록 조회가 필요하면 `identity`가 우선이다.
- 본인 확인 후에는 시스템 조회로 알 수 없는 것만 고른다. 결제일·결제 수단·이용 이력은 보통 조회로 확인되므로, 조회 결과가 `known_facts`에 없고 고객이 여러 구독을 언급해 대상이 모호할 때 등에만 고른다.
- 휴대폰 결제를 결제한 달이 지나 환불하는 경우 `account`.

### suggestion_valid
- 마지막 발화로 정책 판단이 바뀌면 `false`. 문구만 어색한 것은 `true`.

### action
- `handoff`: 제19조 제1항 사유(미성년자 취소, 이의 제기 진행 중, 본인 확인 2회 실패).
- `hold`: 후보 조항끼리 충돌하거나 필요한 근거가 후보에 없는데 고객이 확답을 요구하는 경우.
- `ask`: 처리 결과를 바꿀 정보가 미확인.
- 50,000원 초과 환불의 상담사 승인(제19조 제2항)은 접수 후 승인이므로 `answer`다.

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

- group 단위로 train 60%, val 20%, test 20%로 나눈다.
- 분할 결과는 `data/splits.json`에 저장하고 커밋한다. 한 번 test에 배정된 group은 바뀌지 않으며, 이후 추가되는 group은 train과 val에만 배정된다.
- 튜닝과 임계값 선택에는 val만 쓴다. test는 모델·질문 문구·임계값을 고정한 뒤 평가에만 쓴다.
