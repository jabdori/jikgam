# 판별 평가 결과: test / 20261014T184152

`jikgam/eval/report.py`가 실행 폴더의 `raw.jsonl`, `meta.json`과 정답 파일로 만든다. 손으로 고치지 않는다.

## 실행 조건: 20261014T184152 (gemini-flash)

- 시각(KST): 2026-10-14T18:41:52+09:00 ~ 2026-10-14T18:46:00+09:00
- 코드: `5ff6aa5c4fff`
- 데이터: `data/build/test.jsonl` sha256 앞 16자리 `5251d7d814279bf4`, 사례 20건
- 질문 세트 `q-2026.10.14-en`, LLM 프롬프트 `llm-judge-2026.10.14`
- 반복: gemini-flash 3회, 동시 실행 1
- 네트워크(측정 전): Cloudflare 접속 지점 ICN (KR), TCP 연결 중앙값 generativelanguage.googleapis.com 58.3ms
- 클라이언트: Python 3.12.11, httpx 0.28.1, Windows 11 10.0.22631

## 요약

정확도는 회차별 정확도의 평균이고, 괄호는 회차별 정답 수(회차마다 다르면 최소~최대)/판단 수다. 지연은 클라이언트에서 잰 요청 전송부터 응답 수신까지이며 성공한 모든 호출로 계산한다. 비용은 성공한 호출의 평균이다.

| 백엔드 | 응답 모델 | 정확도 | ECE | p50 ms | p95 ms | 평균 입력 토큰 | 요청당 비용 USD | 1,000건당 USD | 오류 | 재시도 | 회차 간 라벨 일치 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| gemini-flash | gemini-3.8-flash | 90.9% (132~134/146) | 0.064 | 3880 | 5980 | 3123 | 0.003472 | 3.472 | 0/60 | 0 | 95.9% |

## 질문 유형별 정확도

| | gemini-flash |
|---|---|
| intent | 83.3% (11~12/14) |
| condition_changed | 100.0% (18/18) |
| applies | 97.5% (65~66/67) |
| sufficient | 72.5% (12~13/17) |
| next_info | 88.9% (8/9) |
| suggestion_valid | 100.0% (3/3) |
| action | 79.6% (14~15/18) |

## noul 질문: 균형 정확도 / 예측 true 비율

균형 정확도는 true 재현율과 false 재현율의 평균이다. 모든 판단을 true로 내면 50%가 된다.

| | gemini-flash |
|---|---|
| condition_changed (정답 true 33.3%) | 100.0% / 예측 true 33.3% |
| applies (정답 true 61.2%) | 97.0% / 예측 true 62.7% |
| sufficient (정답 true 58.8%) | 70.2% / 예측 true 66.7% |
| suggestion_valid (정답 true 33.3%) | 100.0% / 예측 true 33.3% |

## 질문 유형별 ECE

top-label ECE, 10개 등간격 구간, 모든 회차를 합쳐 계산한다.

| | gemini-flash |
|---|---|
| intent | 0.148 |
| condition_changed | 0.014 |
| applies | 0.011 |
| sufficient | 0.252 |
| next_info | 0.090 |
| suggestion_valid | 0.011 |
| action | 0.167 |

## 상태 길이 구간별 정확도

상태 길이는 내보내기 파일의 state JSON 문자 수다.

| | gemini-flash |
|---|---|
| ≤2,200자 (11건) | 89.6% (69/77) |
| 2,201~2,600자 (7건) | 90.1% (48~50/54) |
| >2,600자 (2건) | 100.0% (15/15) |

## 실패 유형 태그별 정확도

태그가 붙은 사례의 모든 판단을 센다. 한 사례가 여러 태그에 들어간다.

| | gemini-flash |
|---|---|
| colloquial (7건) | 94.1% (47~50/51) |
| correction (6건) | 99.2% (43~44/44) |
| ellipsis (2건) | 73.8% (10~11/14) |
| exception (6건) | 85.3% (36~37/43) |
| handoff (2건) | 91.1% (13~14/15) |
| insufficient_info (2건) | 66.7% (9~10/14) |
| missing_evidence (1건) | 66.7% (4/6) |
| multi_evidence (14건) | 90.7% (93~96/104) |
| negation (2건) | 100.0% (15/15) |
| number_date (7건) | 93.5% (47~49/51) |
| similar_article (6건) | 86.7% (38~40/45) |
| stale_suggestion (2건) | 97.9% (15~16/16) |
| stt_error (3건) | 100.0% (22/22) |
