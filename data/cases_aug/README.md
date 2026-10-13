# 학습 전용 보강 판별 사례 (`cases_aug`)

한국어 판별 모델을 파인튜닝하려고 추가한 합성 사례다. **train 전용**이라 val·test에 넣지 않고 `data/splits.json`에도 배정하지 않는다.

- 파일은 `batch_a.jsonl` 하나다. 사례 104건, 대화 그룹 38개(`a001`~`a038`)이고, 한 그룹에는 같은 대화의 다른 시점을 2~3건 담았다.
- 형식과 라벨은 `docs/data-format.md`의 원본 형식과 라벨 기준을 그대로 따른다. `source`는 `synthetic-aug`다.
- 빌드: `uv run python scripts/build_dataset.py --aug`를 실행하면 `data/build/train_aug.jsonl`이 나온다. split 값은 `train_aug`이고 내보내기 형식은 train과 같다. `--aug --check`는 검증만 한다. `--aug` 없이 실행할 때의 산출물(`train/val/test.jsonl`, `summary.md`)과 `splits.json`은 바뀌지 않는다.
- 데이터는 모두 가상이다. 조직은 `org_a`만 쓴다. 고객 ID는 `C101`~`C139`로, `customers.json`의 C001~C016과 겹치지 않는다. 이름·인증번호·계좌번호도 지어낸 값이다. 장애 기록 `INC-2026-1004`와 `INC-2026-1010`은 이 사례만을 위해 만든 가상 기록이다.

## 목적

val 기준선에서 확인된 실패 유형을 겨냥해 학습 신호를 늘린다. 실패 유형은 설명만 받았고 val 사례는 보지 않았으며, 모든 사례를 새 상황과 다른 표현으로 썼다. 기존 train 64건에 적었던 `partial_stt`, `stt_error`, `next_info`의 `payment_date`·`usage`, `hold`도 보강했다. 약관 내용을 외우게 하려는 것이 아니다. 입력된 근거와 조회 기록을 해석하는 판단을 늘리려는 것이다.

## 실패 유형별 사례

"주 대상 그룹"은 그 유형을 겨냥해 설계한 그룹이다. "사례"는 그 그룹들에 속한 사례 수인데, 한 그룹이 여러 유형에 걸칠 수 있어 합계가 104를 넘는다. "라벨 지표"는 파일 전체에서 센 값이다. 조항 칸의 숫자는 true/false 순서다.

| 유형 | 주 대상 그룹 | 사례 | 라벨 지표 |
|---|---|---|---|
| 1. next_info 우선순위 | a001 a002 a015 a017 a025 a030 a033 | 20 | 본인 확인 전 `identity` 22건. 고객이 결제일·수단·이용 여부·계좌를 먼저 말한 경우도 여기에 넣었다. 결제 목록이 조회된 뒤의 next_info는 none 36, plan_type 3, account 2, usage 2이고 payment_date는 0이다. |
| 2. action (ask/answer, handoff/answer) | a009 a010 a011 a018 a020 a034 a037 | 18 | answer 59, ask 29, hold 6, handoff 5. 본인 확인 전 사례만 보면 ask 21, answer 3(제3자 거절, 가입 전 문의), hold 1, handoff 1이다. |
| 3. 기록과 고객 주장 | a003 a004 a007 a023 a028 a031 a032 a033 | 21 | 본인 확인 후 `applies:A-10`은 true 10, false 12다. 기록에 중복이 없으면 false다. 같은 날 다른 상품, 연도가 다른 갱신 결제, 이용 기간이 다른 결제를 세는 사례와 기록에 실제 중복이 있는 사례가 함께 들어 있다. |
| 4. 복수 구독과 생략 | a005 a006 a038 | 9 | next_info `plan_type` 3, `ellipsis` 태그 7 |
| 5. 첫 발화 모호성 | a007 a008 a027 | 8 | intent unclear 1, billing_inquiry 5. 모두 refund로 보지 않았다. |
| 6. 절차 조항 시점 | a001 a004 a010 a014 a018 a019 | 18 | A-14 16/9, A-15 17/12, A-19 21/13, A-20 15/4 |
| 7. 날짜·기간 예외 | a002 a007 a012 a013 a014 a015 a024 a026 a028 | 26 | A-09 5/1, A-12 5/1, A-21 10/11, A-02 3/1. 장애 환불 기간의 마지막 날과 그다음 날, 체험 중 이용, 6월 30일·7월 1일·7월 2일 결제, 결제 전 이용을 다룬다. |
| 8. condition_changed 오탐 | a002 a003 a006 a008 a009 a011 a013 a015 a016 a017 a026 a029 a031 a038 | 41 | false 62, true 22. false에는 반복, 기록되지 않은 새 사실, 시스템 값 수용이 들어 있다. true에는 정정, 요청 변경·철회, 기록과 다른 주장이 들어 있다. |
| 9. 유사 조항 | a003 a004 a005 a016 a028 a029 a035 | 21 | A-04 2/10, A-07 19/16, A-08 19/12, A-17 1/5, A-18 9/4 |
| partial_stt | a008 a014 a017 a027 | 11 | 미확정 자막이 있는 사례 9건. 뒤에 확정 발화가 이어지는 경우와 마지막 발화가 미확정인 경우를 모두 넣었다. |
| stt_error | a010 a025 a026 a030 | 12 | `stt_error` 태그 8건. 숫자와 한글 수사가 섞인 금액·날짜·인증번호·계좌번호, '결재/해제/한 불' 같은 오인식이다. |
| next_info payment_date | a021 a023 | 6 | `payment_date` 2건. 결제 내역이 아직 조회 전이거나 조회에 실패한 상태에서 고객이 특정 결제를 말한 경우다. |
| next_info usage | a022 a024 | 4 | `usage` 2건. 이용 이력이 조회 전이거나 조회에 실패한 경우다. |
| hold | a020 a021 a022 a036 | 9 | hold 6건. 조항이 빠졌는데 고객이 확답을 요구한 경우 2건, 업무 API 장애가 known_facts에 기록된 경우 4건이다. |

## 정답 분포

| 질문 | 정답 분포 |
|---|---|
| intent | refund 18, cancel_refund 15, cancel 6, billing_inquiry 5, plan_change 3, other 3, unclear 1, compensation 1 |
| condition_changed | false 62, true 22 |
| applies | true 195, false 180 |
| sufficient | true 58, false 35 |
| next_info | none 37, identity 22, plan_type 3, account 2, payment_date 2, usage 2, payment_method 1 |
| suggestion_valid | true 4, false 1 |
| action | answer 59, ask 29, hold 6, handoff 5 |

조항별 applies 정답:

| 조항 | true | false |
|---|---|---|
| A-02 | 3 | 1 |
| A-03 | 0 | 1 |
| A-04 | 2 | 10 |
| A-05 | 21 | 31 |
| A-06 | 8 | 6 |
| A-07 | 19 | 16 |
| A-08 | 19 | 12 |
| A-09 | 5 | 1 |
| A-10 | 13 | 19 |
| A-11 | 7 | 7 |
| A-12 | 5 | 1 |
| A-13 | 3 | 2 |
| A-14 | 16 | 9 |
| A-15 | 17 | 12 |
| A-16 | 1 | 14 |
| A-17 | 1 | 5 |
| A-18 | 9 | 4 |
| A-19 | 21 | 13 |
| A-20 | 15 | 4 |
| A-21 | 10 | 11 |
| T-02 | 0 | 1 |

사례당 질문 수는 3~8개이고, 후보 조항은 5~7개다. 후보에는 정답 조항과 함께 헷갈리는 유사 조항과 무관한 조항을 섞었다.

## 태그 분포

| 태그 | 사례 |
|---|---|
| number_date | 48 |
| similar_article | 35 |
| exception | 33 |
| colloquial | 32 |
| insufficient_info | 29 |
| multi_evidence | 25 |
| correction | 21 |
| handoff | 8 |
| stt_error | 8 |
| ellipsis | 7 |
| negation | 7 |
| partial_stt | 6 |
| stale_suggestion | 5 |
| missing_evidence | 2 |

## 확인 방법

1. 형식: `build_dataset.py`의 기존 검증기(`validate`)를 그대로 적용했다. 이에 더해 `data/cases`와 id·group이 겹치지 않는지 확인한다. 결과는 형식 오류 0건이다.
2. 라벨 작성: `docs/data-format.md`의 라벨 기준만 근거로 삼았다. 날짜 계산(7일은 결제 다음 날을 1일째로 센다), 이용 개월 수, 위약금, 환불액은 사례마다 직접 계산해 rationale에 적었다.
3. 규칙 일관성 점검: 104건 전체를 기계적으로 검사했고 위반은 0건이었다. 검사 항목은 다음과 같다.
   - 본인 확인 전 next_info는 identity여야 한다(가입 전 문의는 예외).
   - 본인 확인 후 A-15는 false여야 한다.
   - handoff인데 A-19가 false이면 안 된다.
   - sufficient가 false인데 action이 answer이면 안 된다.
   - sufficient false와 next_info none이 함께 나오면 action은 hold여야 한다.
   - next_info가 account이면 action은 ask여야 한다.
   - A-04와 A-07이 동시에 true이면 안 된다.
4. 블라인드 재판정: 무작위로 26건(25%, seed 20261014)을 뽑아 정답과 rationale을 지우고 state와 질문만 남겼다. 이 파일을 생성 과정을 모르는 별도 에이전트에게 주고, `docs/data-format.md`, `terms.md`, `questions.json`만 보고 다시 판정하게 했다. 186개 판단이 모두 일치했고(26건 모두 완전 일치) 고친 사례는 없다. 재판정자가 해석이 갈릴 수 있다고 꼽은 판단은 아래와 같다. 모두 rationale에 근거를 적었다.
   - a011-1 action: 법정대리인 동의 여부가 미확인이라 ask로 했다. 미성년자 취소 요청 자체를 인계 사유로 보면 handoff가 된다.
   - a031-1 condition_changed: 조회된 1건과 다른 주장이므로 true로 했다.
   - a003-2, a031-1 applies:A-10: 기록상 중복이 없으므로 false로 했다.
   - 그 밖에 a027-1·a012-2 applies:A-07, a019-2 applies:A-19, a008-2 action, a016-1 applies:A-05, a037-2 condition_changed.

   재판정자와 작성자가 같은 계열 모델이라 같은 해석 편향을 공유할 수 있다. 그래서 이 일치율이 독립 검토를 대신하지는 않는다.
5. 작성 중 열지 않은 파일: `data/build/val.jsonl`, `data/build/test.jsonl`, `data/build/summary.md`, `data/cases/`, `data/splits.json`, `docs/labeling-review.md`, 그리고 `results/` 아래 전부. 참고한 파일은 `CLAUDE.md`, `docs/data-format.md`, `docs/design.md` §6, `domain/org_a/terms.md`, `domain/org_a/api.md`, `domain/org_a/customers.json`, `data/questions.json`, `scripts/build_dataset.py`, `data/build/train.jsonl`이다.

## 한계

- 작성과 재판정을 모두 AI가 했다. 정답은 "라벨 기준 문서에 맞춘 일관된 라벨"이며 상담 실무자가 확인한 정답이 아니다.
- 실패 유형은 설명만 보고 만들었으므로 val의 실제 오답과 비율이 다를 수 있다. 이 데이터로 학습한 효과는 val로만 측정하고, test는 모든 설정을 고정한 뒤 평가에만 쓴다.
- 일부 판단에는 라벨 기준을 적용하는 해석이 들어간다. 예를 들어 이미 조회된 결제 목록과 다른 고객 주장은 `condition_changed` true로, 정지 계정의 중복 결제 환불에서 제16조는 false로 했다. 해당 사례의 rationale에 이유를 적었다.
- `next_info`의 account, payment_date, usage, payment_method와 action의 hold, handoff는 아직 각 1~6건뿐이다.
