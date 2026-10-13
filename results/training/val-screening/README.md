# val 선별 결과

`jikgam/train/screening_table.py`가 `*/summary.json`으로 만든다. 손으로 고치지 않는다.

val을 쓴 선별 구성: 2개(보정 변형은 아래 열로 함께 적고 따로 세지 않는다). 모델은 서빙과 같은 방식(병합 모델, 16비트, 최대 16,384 토큰)으로 올려 `FastDecisionModel.predict`로 답했다. 정확도·ECE는 저장소 정의(`jikgam.eval.metrics`)다. 보정 후 ECE는 val 전체로 맞춘 온도로 다시 예측한 표본 내 값이고, Unsloth 교차 적합 ECE는 `FastDecisionModel.calibrate`가 val 사례를 반씩 나눠 계산한 값(Unsloth 정의)이다.

| 선별 | 기반 모델 | 학습 구성 | 학습 문구 | 평가 문구 | 정확도 | ECE | 보정 후 ECE(표본 내) | Unsloth 교차 적합 ECE | 학습 분 | 최대 GPU GB |
|---|---|---|---|---|---|---|---|---|---|---|
| q35-0.8b-a-mix__q-mix | unsloth/Qwen3.5-0.8B | a | q-2026.10.14-mix | q-2026.10.14-mix | 64.5% (98/152) | 0.082 | 0.080 | 0.056 | 2.2 | 2.69 |
| q35-0.8b-a-v1__q-v1 | unsloth/Qwen3.5-0.8B | a | q-2026.10.12 | q-2026.10.12 | 69.7% (106/152) | 0.061 | 0.045 | 0.048 | 2.6 | 2.42 |

## 질문 유형별 정확도(보정 전)

| 선별 | intent | condition_changed | applies | sufficient | next_info | suggestion_valid | action |
|---|---|---|---|---|---|---|---|
| q35-0.8b-a-mix__q-mix | 11/17 | 13/17 | 47/67 | 12/20 | 4/10 | 0/3 | 11/18 |
| q35-0.8b-a-v1__q-v1 | 12/17 | 13/17 | 49/67 | 12/20 | 5/10 | 2/3 | 13/18 |

## noul 질문: 균형 정확도 / 예측 true 비율

| 선별 | condition_changed | applies | sufficient |
|---|---|---|---|
| q35-0.8b-a-mix__q-mix | 50.0% / 0.0% | 70.8% / 47.8% | 50.0% / 100.0% |
| q35-0.8b-a-v1__q-v1 | 50.0% / 0.0% | 75.5% / 35.8% | 50.0% / 100.0% |

## 실패 유형 태그별 정확도(보정 전)

| 선별 | colloquial | correction | ellipsis | exception | handoff | insufficient_info | missing_evidence | multi_evidence | negation | number_date | partial_stt | similar_article | stale_suggestion | stt_error |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| q35-0.8b-a-mix__q-mix | 36/49 | 16/31 | 3/7 | 54/72 | 8/14 | 8/18 | 4/7 | 52/77 | 15/24 | 26/38 | 4/6 | 23/37 | 14/24 | 9/12 |
| q35-0.8b-a-v1__q-v1 | 40/49 | 19/31 | 3/7 | 52/72 | 9/14 | 9/18 | 5/7 | 55/77 | 18/24 | 26/38 | 4/6 | 26/37 | 15/24 | 9/12 |
