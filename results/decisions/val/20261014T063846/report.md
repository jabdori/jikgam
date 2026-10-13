# 판별 평가 결과: val / 20261014T063846

`jikgam/eval/report.py`가 실행 폴더의 `raw.jsonl`, `meta.json`과 정답 파일로 만든다. 손으로 고치지 않는다.

## 실행 조건: 20261014T063846 (q35-2b-ko-gguf-q4km)

- 시각(KST): 2026-10-14T06:38:46+09:00 ~ 2026-10-14T06:39:21+09:00
- 코드: `3f2e21b9e483`
- 데이터: `data/build/val.jsonl` sha256 앞 16자리 `a169861b4dc863da`, 사례 21건
- 질문 세트 `q-2026.10.14-mix`, LLM 프롬프트 `llm-judge-2026.10.14`
- 반복: q35-2b-ko-gguf-q4km 5회, 동시 실행 1
- 네트워크(측정 전): Cloudflare 접속 지점 ICN (KR), TCP 연결 중앙값 127.0.0.1:8892 14.5ms
- 클라이언트: Python 3.12.11, httpx 0.28.1, Windows 11 10.0.22631

## 요약

정확도는 회차별 정확도의 평균이고, 괄호는 회차별 정답 수(회차마다 다르면 최소~최대)/판단 수다. 지연은 클라이언트에서 잰 요청 전송부터 응답 수신까지이며 성공한 모든 호출로 계산한다. 비용은 성공한 호출의 평균이다.

| 백엔드 | 응답 모델 | 정확도 | ECE | p50 ms | p95 ms | 평균 입력 토큰 | 요청당 비용 USD | 1,000건당 USD | 오류 | 재시도 | 회차 간 라벨 일치 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| q35-2b-ko-gguf-q4km | laya-local | 86.8% (132/152) | 0.090 | 212 | 486 | 2351 | 0.000000 | 0.000 | 0/105 | 0 | 100.0% |

## 질문 유형별 정확도

| | q35-2b-ko-gguf-q4km |
|---|---|
| intent | 94.1% (16/17) |
| condition_changed | 100.0% (17/17) |
| applies | 80.6% (54/67) |
| sufficient | 90.0% (18/20) |
| next_info | 80.0% (8/10) |
| suggestion_valid | 100.0% (3/3) |
| action | 88.9% (16/18) |

## noul 질문: 균형 정확도 / 예측 true 비율

균형 정확도는 true 재현율과 false 재현율의 평균이다. 모든 판단을 true로 내면 50%가 된다.

| | q35-2b-ko-gguf-q4km |
|---|---|
| condition_changed (정답 true 23.5%) | 100.0% / 예측 true 23.5% |
| applies (정답 true 56.7%) | 80.4% / 예측 true 55.2% |
| sufficient (정답 true 60.0%) | 89.6% / 예측 true 60.0% |
| suggestion_valid (정답 true 0.0%) | 정의 안 됨(정답이 한 종류) / 예측 true 0.0% |

## 질문 유형별 ECE

top-label ECE, 10개 등간격 구간, 모든 회차를 합쳐 계산한다.

| | q35-2b-ko-gguf-q4km |
|---|---|
| intent | 0.085 |
| condition_changed | 0.069 |
| applies | 0.163 |
| sufficient | 0.037 |
| next_info | 0.172 |
| suggestion_valid | 0.008 |
| action | 0.111 |

## 상태 길이 구간별 정확도

상태 길이는 내보내기 파일의 state JSON 문자 수다.

| | q35-2b-ko-gguf-q4km |
|---|---|
| ≤2,200자 (5건) | 76.7% (23/30) |
| 2,201~2,600자 (11건) | 89.3% (75/84) |
| >2,600자 (5건) | 89.5% (34/38) |

## 실패 유형 태그별 정확도

태그가 붙은 사례의 모든 판단을 센다. 한 사례가 여러 태그에 들어간다.

| | q35-2b-ko-gguf-q4km |
|---|---|
| colloquial (7건) | 89.8% (44/49) |
| correction (4건) | 93.5% (29/31) |
| ellipsis (1건) | 57.1% (4/7) |
| exception (10건) | 86.1% (62/72) |
| handoff (2건) | 78.6% (11/14) |
| insufficient_info (3건) | 66.7% (12/18) |
| missing_evidence (1건) | 71.4% (5/7) |
| multi_evidence (10건) | 92.2% (71/77) |
| negation (3건) | 91.7% (22/24) |
| number_date (5건) | 92.1% (35/38) |
| partial_stt (1건) | 83.3% (5/6) |
| similar_article (5건) | 78.4% (29/37) |
| stale_suggestion (3건) | 100.0% (24/24) |
| stt_error (2건) | 91.7% (11/12) |
