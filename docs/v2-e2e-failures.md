# v2 엔드투엔드 미공개 실행의 실패 원인(항목 9·10)

> 처음 읽는다면: [전체 흐름 안내](overview.md) · [용어집](glossary.md)

- 날짜: 2026-10-19
- 대상: v2 엔드투엔드 미공개 실행 `holdout-v2-r0`, `holdout-v2-r4`(고정 커밋 `728a02f`, ADR 0027·0028)
- 범위: 이미 끝난 실행의 기록만 분석했다. 새 실험과 유료 호출은 하지 않았고, 엔진·설정·평가 문서·결과 수치와 ADR 0028의 판정은 바꾸지 않았다.

## 무엇을 나눴나

항목 9(`must_say`를 모두 안내) 또는 항목 10(근거에 조직 A 조항과 법령·고시 정답 근거가 모두 들어감)에서 실패한 유효 실행마다, 정답 법령 구간이 빠진 단계를 하나로 정했다.

1. **검색 후보에 없음:** 어느 턴의 전체 법령 검색 순위에도 없다.
2. **후보에 있었지만 상위 8개 밖:** 전체 순위에는 있지만 엔진이 남기는 상위 8개(`retrieval.done`의 `law_candidates`)에 든 적이 없다.
3. **상위 8개 안이었지만 판별에서 0.5 미만:** 후보였던 턴에서 `law_applies` 확률이 늘 0.5 미만이라 근거로 고르지 않았다(`applies_threshold = 0.5`).
4. **근거로 골랐지만 답변에서 안내하지 않음:** 근거로 골라 생성에 넘겼지만, 그 법령 내용에 대응하는 `must_say`를 안내하지 않았다.

- 시나리오의 정답 법령 항목(`law_evidence`, 대체 근거 포함)마다 도달한 가장 먼 단계를 보고, 실행의 등급은 빠진 항목들 중 가장 앞 단계로 했다. 항목을 덮었는지는 `docs/evaluation.md` §4.2의 기준(대체 근거 하나의 공백 제외 글자 절반 이상)으로 봤다.
- 법령 항목이 모두 근거에 들어가고 안내됐는데도 실패한 실행은 넷 중 하나로 넣지 않고 **법령 구간 아님(다른 원인)**으로 따로 셌다.
- 로그에는 상위 8개만 남는다(`jikgam/engine/law.py`). 1과 2를 가르려고, 로그에 남은 법령 검색어로 고정 커밋의 검색을 이 PC에서 다시 돌려 전체 순위를 냈다. R0는 BM25 규칙 검색, R4는 로컬 임베딩 모델을 썼고 원격 호출은 없었다. 다시 낸 상위 8개가 기록된 `law_candidates`와 순서까지 같은 비율은 R0 366/366, R4 373/373이다(검색 이벤트 기준).
- 미공개 시나리오 기대값과 세션 원문은 측정과 분리된 보관 에이전트만 읽었다. 이 문서의 발췌에는 구간 id, 순위, 확률만 있고 고객 발화·답변·표준약관 문장은 없다.

## 결과

| 구분 | R0 | R4 |
|---|---|---|
| 유효 실행 | 31 | 30 |
| 통과 | 5 | 0 |
| 항목 9 또는 10 실패 | 25 | 30 |
| ㄴ 9만 / 10만 / 둘 다 | 6 / 1 / 18 | 9 / 0 / 21 |
| **1 검색 후보에 없음** | 0개(0%) | 0개(0%) |
| **2 후보에 있었지만 상위 8개 밖** | 12개(48%) | 18개(60%) |
| **3 상위 8개 안이었지만 판별 0.5 미만** | 4개(16%) | 0개(0%) |
| **4 근거로 골랐지만 답변에서 안내하지 않음** | 2개(8%) | 7개(23%) |
| **법령 구간 아님(다른 원인)** | 7개(28%) | 5개(17%) |
| ㄴ 법령 외 must_say | 4 | 3 |
| ㄴ 조직 A 조항 누락 + 법령 외 must_say | 3 | 2 |

- R4에서 1은 구조상 나올 수 없다. 임베딩이 378개 구간 모두에 점수를 매기기 때문이다. R0의 전체 순위는 BM25 점수가 0보다 큰 구간(검색당 218~812개, 전체 829개)이라 1이 나올 수 있었지만 0건이었다.
- R0에는 항목 9·10은 통과하고 다른 항목에서만 실패한 실행이 1개(hv09 1회) 더 있다. 범위 밖이라 표에서 뺐다.

법령 항목 단위(실패 실행 안의 정답 법령 항목 수):

| 단계(법령 항목 단위) | R0 | R4 |
|---|---|---|
| 2 후보에 있었지만 상위 8개 밖 | 17 | 23 |
| 3 상위 8개 안이었지만 판별 0.5 미만 | 7 | 0 |
| 4 근거로 골랐지만 답변에서 안내하지 않음 | 4 | 12 |
| 선택·안내됨(손실 없음) | 15 | 16 |

단계 2에 멈춘 법령 항목이 그 세션에서 가장 높이 올라간 전체 순위:

| 단계 2에 멈춘 법령 항목의 가장 좋은 전체 순위 | R0 | R4 |
|---|---|---|
| 9~16위 | 4 | 4 |
| 17~32위 | 3 | 8 |
| 33~100위 | 8 | 8 |
| 100위 밖 | 2 | 3 |

- R0: 9, 14, 14, 15, 19, 19, 19, 34, 71, 73, 77, 77, 77, 84, 87, 102, 104
- R4: 13, 15, 16, 16, 17, 17, 18, 20, 24, 30, 30, 30, 45, 46, 58, 74, 75, 75, 92, 92, 163, 170, 179

### 시나리오별

| 시나리오 | R0 1회 | R0 2회 | R0 3회 | R4 1회 | R4 2회 | R4 3회 |
|---|---|---|---|---|---|---|
| hv01 | 무효 | 무효 | 무효 | 무효 | 무효 | 무효 |
| hv02 | 통과 | 통과 | 통과 | 4 | 4 | 4 |
| hv03 | 4 | 기타 가 | 통과 | 2 | 2 | 2 |
| hv04 | 2 | 무효 | 무효 | 무효 | 무효 | 2 |
| hv05 | 2 | 3 | 3 | 4 | 4 | 4 |
| hv06 | 2 | 2 | 2 | 2 | 2 | 2 |
| hv07 | 기타 나 | 기타 나 | 기타 나 | 기타 나 | 4 | 기타 나 |
| hv08 | 2 | 2 | 2 | 무효 | 2 | 2 |
| hv09 | 9·10 밖 실패 | 4 | 통과 | 2 | 2 | 2 |
| hv10 | 기타 가 | 기타 가 | 기타 가 | 기타 가 | 기타 가 | 기타 가 |
| hv11 | 2 | 2 | 2 | 2 | 2 | 2 |
| hv12 | 3 | 3 | 2 | 2 | 2 | 2 |

- 칸의 숫자는 등급이다. 기타 가 = 법령 외 `must_say`, 기타 나 = 조직 A 조항 누락 + 법령 외 `must_say`. 통과는 모든 항목을 통과한 실행이다.

## 읽은 것

- **가장 많이 빠진 곳은 검색 상위 8개다(등급 2).** 실패 실행 중 R0 25개 중 12개, R4 30개 중 18개다. 이 항목들의 정답 구간은 대부분 16위보다 뒤에 있었다. 단계 2에 멈춘 항목 중 전체 순위 16위 안에 든 적이 있는 것은 R0 17개 중 4개, R4 23개 중 4개뿐이다(재계산 순위로 센 값이고, 후보 수를 바꿔 실험하지는 않았다).
- **R4는 상위 8개에 들면 판별에서 골랐다(등급 3이 0개).** R0는 상위 8개에 든 정답 구간을 판별이 0.5 미만으로 본 실행이 4개 있다.
- **R4는 근거로 고르고도 안내하지 않은 실행이 7개다(등급 4).** hv02 3회와 hv05 3회가 모두 여기에 든다. 같은 시나리오에서 R0는 hv02가 3회 모두 통과했고, hv05는 검색·판별 단계에서 빠졌다(등급 2·3).
- **법령과 관계없는 실패도 있다.** R0 7개, R4 5개다. hv10은 두 구성 모두 법령 밖 `must_say` 때문에 실패했고, hv07은 조직 A 조항 누락이 겹쳤다.

## 대표 세션 발췌

★는 정답 구간이다. 구간 id에서 앞의 묶음 이름(`article256:`, `b-W4000-C800-raw:`)은 뺐다. 법령 후보는 순위 순서다.

**hv05 R0 2회 — 등급 3** (정답 구간: content-guide:0007, dispute-std:0119, dispute-std:0126, ecom-act:0014)

```text
  0.2s v1 retrieval.done    | 법령 후보 8개: dispute-std:0131 ecom-terms:0010 dispute-std:0031 dispute-std:0105 game-terms:0018 dispute-std:0108 dispute-std:0133 ★dispute-std:0119 | 정답 전체 순위: content-guide:0007 12, dispute-std:0119 8, dispute-std:0126 9, ecom-act:0014 13
  0.4s v1 evidence.updated  | 정답 p: dispute-std:0119 0.30 | 근거: A-15 A-13 A-19 A-08
  2.8s v1 suggestion.created | 인용: A-13 A-15 A-19 A-08
  5.0s v2 retrieval.done    | 법령 후보 8개: dispute-std:0131 ecom-terms:0010 game-terms:0018 dispute-std:0105 dispute-std:0031 dispute-std:0139 ecom-decree:0038 dispute-std:0133 | 정답 전체 순위: content-guide:0007 9, dispute-std:0119 13, dispute-std:0126 12, ecom-act:0014 11
  9.2s v3 retrieval.done    | 법령 후보 8개: dispute-std:0131 ecom-terms:0010 dispute-std:0031 game-terms:0018 dispute-std:0105 dispute-std:0139 ★content-guide:0007 ★ecom-act:0014 | 정답 전체 순위: content-guide:0007 7, dispute-std:0119 13, dispute-std:0126 12, ecom-act:0014 8
  9.5s v3 evidence.updated  | 정답 p: content-guide:0007 0.27, ecom-act:0014 0.24 | 근거: A-15 A-20
 10.3s v6 evidence.updated  | 정답 p: content-guide:0007 0.31, ecom-act:0014 0.38 | 근거: A-13 A-19 A-20 A-15
 12.5s v7 suggestion.created | 인용: A-13 A-15 A-19 A-20
```

**hv05 R4 2회 — 등급 4** (정답 구간: content-guide:0004, dispute-std:0059, dispute-std:0061, ecom-act:0008)

```text
  0.3s v1 retrieval.done    | 법령 후보 8개: ★content-guide:0004 game-terms:0007 ★dispute-std:0059 ★dispute-std:0061 dispute-std:0062 dispute-std:0064 dispute-std:0052 ecom-terms:0004 | 정답 전체 순위: content-guide:0004 1, dispute-std:0059 3, dispute-std:0061 4, ecom-act:0008 11
  0.5s v1 evidence.updated  | 정답 p: content-guide:0004 0.61, dispute-std:0059 0.54, dispute-std:0061 0.50 | 근거: A-15 A-13 A-19 A-08 ★content-guide:0004 ★dispute-std:0059 dispute-std:0062 game-terms:0007 ★dispute-std:0061
  2.6s v1 suggestion.created | 인용: A-13 A-15 A-19 A-08 ★content-guide:0004 ★dispute-std:0059 dispute-std:0062 game-terms:0007 ★dispute-std:0061
  4.9s v2 evidence.updated  | 정답 p: content-guide:0004 0.43, dispute-std:0059 0.50, dispute-std:0061 0.36 | 근거: A-15 ★dispute-std:0059
  7.2s v2 suggestion.created | 인용: A-13 A-15 A-19 A-20 ★dispute-std:0059
  9.4s v4 evidence.updated  | 정답 p: content-guide:0004 0.45, dispute-std:0059 0.47, dispute-std:0061 0.31 | 근거: A-20 A-13 A-19 A-15
 10.0s v6 evidence.updated  | 정답 p: content-guide:0004 0.53, dispute-std:0059 0.62, dispute-std:0061 0.36 | 근거: A-13 A-19 A-15 ★dispute-std:0059 dispute-std:0064 ★content-guide:0004 game-terms:0007
 12.2s v7 suggestion.created | 인용: A-13 A-15 A-19 A-20 ★dispute-std:0059 dispute-std:0064 game-terms:0007 ★content-guide:0004
```

**hv11 R4 1회 — 등급 2** (정답 구간: ecom-act:0002, ecom-act:0023)

```text
  0.3s v1 retrieval.done    | 법령 후보 8개: dispute-std:0054 dispute-std:0052 dispute-std:0062 dispute-std:0142 dispute-std:0059 dispute-std:0061 dispute-std:0068 game-terms:0011 | 정답 전체 순위: ecom-act:0002 135, ecom-act:0023 30
  0.5s v1 evidence.updated  | 근거: A-15 A-18
  2.9s v1 suggestion.created | 인용: A-15 A-18
  5.0s v2 retrieval.done    | 법령 후보 8개: dispute-std:0052 game-terms:0006 dispute-std:0062 game-terms:0002 dispute-std:0060 dispute-std:0059 dispute-std:0053 dispute-std:0061 | 정답 전체 순위: ecom-act:0002 189, ecom-act:0023 43
  9.6s v3 retrieval.done    | 법령 후보 8개: dispute-std:0052 game-terms:0002 game-terms:0006 dispute-std:0059 dispute-std:0062 dispute-std:0060 dispute-std:0053 game-terms:0009 | 정답 전체 순위: ecom-act:0002 126, ecom-act:0023 94
 15.9s v7 retrieval.done    | 법령 후보 8개: dispute-std:0052 game-terms:0009 game-terms:0006 dispute-std:0059 game-terms:0002 dispute-std:0062 dispute-std:0054 dispute-std:0053 | 정답 전체 순위: ecom-act:0002 102, ecom-act:0023 49
 16.2s v7 evidence.updated  | 근거: A-18 A-07 dispute-std:0059
 21.3s v8 retrieval.done    | 법령 후보 8개: dispute-std:0059 game-terms:0009 dispute-std:0062 dispute-std:0068 dispute-std:0052 dispute-std:0053 game-terms:0002 game-terms:0006 | 정답 전체 순위: ecom-act:0002 92, ecom-act:0023 43
 24.2s v9 suggestion.created | 인용: A-20 A-07 A-18 dispute-std:0059
 29.4s v10 suggestion.created | 인용: A-20 A-07 A-18 dispute-std:0059
```

- hv05 R0 2회는 v3에서 두 정답 구간이 후보 7·8위에 들었지만 판별 확률이 0.24~0.38이라 근거로 고르지 않았다.
- hv05 R4 2회는 v1과 v6에서 정답 구간을 근거로 골라 인용까지 했지만, 판정기는 그 내용의 `must_say`를 안내하지 않았다고 봤다.
- hv11 R4 1회는 두 정답 구간이 어느 턴에서도 상위 8개에 들지 않았다.

## 한계

- 4와 '안내함' 여부는 AI 판정기의 `must_say` 판정을 그대로 썼다. 사람 확인(ADR 0028)에서 항목 9 표본 4개는 모두 일치했다.
- 정답 법령 항목과 `must_say`의 대응은 보관 에이전트가 정했다. hv03·hv04·hv05·hv11은 법령 항목 두 개가 `must_say` 하나에 대응해 안내 여부를 함께 쓴다.
- 대체 근거도 정답으로 셌다. hv12 R0 1·2회의 3은 표준약관 쪽 대체 구간만 상위 8개에 든 경우이고, 법령·고시 쪽 대체 구간만 보면 2다.
- 분석은 미공개 시나리오 12개, 구성별 유효 실행 30~31개다. hv01은 두 구성 모두 세 회차가 무효였다.

관련: `docs/evaluation-v2.md` §8.4, ADR 0027, ADR 0028
