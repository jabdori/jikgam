# 판별 데이터 요약

`scripts/build_dataset.py`가 생성한다. 손으로 고치지 않는다.

## 분할

| 분할 | 사례 | 대화 그룹 |
|---|---|---|
| train | 64 | 24 |
| val | 21 | 9 |
| test | 20 | 7 |
| 합계 | 105 | 40 |

## 실패 유형별 사례 수

| 태그 | train | val | test | 합계 |
|---|---|---|---|---|
| colloquial | 22 | 7 | 7 | 36 |
| correction | 15 | 4 | 6 | 25 |
| ellipsis | 4 | 1 | 2 | 7 |
| exception | 16 | 10 | 6 | 32 |
| handoff | 9 | 2 | 2 | 13 |
| insufficient_info | 10 | 3 | 2 | 15 |
| missing_evidence | 4 | 1 | 1 | 6 |
| multi_evidence | 30 | 10 | 14 | 54 |
| negation | 8 | 3 | 2 | 13 |
| number_date | 20 | 5 | 7 | 32 |
| partial_stt | 2 | 1 | 0 | 3 |
| similar_article | 15 | 5 | 6 | 26 |
| stale_suggestion | 10 | 3 | 2 | 15 |
| stt_error | 8 | 2 | 3 | 13 |

## 질문별 정답 분포

| 질문 | 정답 분포 |
|---|---|
| action | answer 55, ask 21, handoff 9, hold 3 |
| applies | true 194, false 146 |
| condition_changed | false 65, true 26 |
| intent | cancel_refund 49, refund 18, cancel 7, plan_change 6, billing_inquiry 2, compensation 2, other 2 |
| next_info | identity 22, none 13, account 8, plan_type 2, payment_method 1, usage 1 |
| sufficient | true 50, false 35 |
| suggestion_valid | false 14, true 8 |
