# v2 미공개(보류용) 엔드투엔드 시나리오

`org_a_v2_holdout.jsonl`은 v2 평가의 엔드투엔드 판정(`docs/evaluation-v2.md` §2.5, §4.4, §8.4)에 쓰는 미공개 시나리오다. 엔진 설정을 고정한 뒤 한 번만 돌린다. 상황은 조직 A(가상 구독 서비스) 상담 중에 법령·고시 근거가 필요한 경우다.

형식은 개발용 `org_a.jsonl`과 같다. 최상위 키와 순서, `caller`, `persona`, `mid_call_events`의 키가 같다. `expected`에는 v1 키(`intent`, `articles`, `outcome`, `refund_amount`, `tool_calls`, `forbidden`, `must_say`, `action`) 뒤에 두 키가 더 있다.

- `law_quotes`: 작성자가 원문에서 옮긴 법령·고시 근거
- `law_evidence`: `jikgam.v2.holdout map`이 `law_quotes`를 원문 위치(문서 id, 글자 범위)로 바꾼 것

## 작성 규칙

- 엔진 개발에 참여하지 않은 별도 작성자(독립 작성 에이전트)가 썼다. 엔진을 통과시키거나 떨어뜨리려 하지 않고, 약관과 법령이 정하는 현실적인 상담 상황을 썼다.
- **작성자가 본 것:**
  - `CLAUDE.md`, `docs/evaluation-v2.md`, `docs/evaluation.md`, `docs/data-format.md`, `docs/design.md`
  - `domain/org_a/terms.md`, `domain/org_a/api.md`, `domain/org_a/customers.json`(형식 참고)
  - `bizapi/` 코드. v1 보류용 재생 테스트(`bizapi/internal/server/holdout_test.go`)는 재생 테스트 형식을 참고하려고 읽었다.
  - `data/scenarios/org_a.jsonl`(형식 참고), `data/scenarios/HOLDOUT.md`
  - 추출 원문: `data/v2/extracted/*.jsonl`, `data/v2/local/{game-terms,ecom-terms}/extracted.jsonl`
- **작성자가 열지 않은 것:**
  - 엔진·평가 코드: `jikgam/engine/`, `jikgam/baselines/`, `jikgam/v2/`, `jikgam/ingest/`. `jikgam.v2.holdout map`은 실행만 했다. 출력 형식을 보려고 시험 시나리오 1개로 한 번 돌렸고, 그 파일은 덮어썼다.
  - 조직 설정: `config/orgs/`, `config/v2/`
  - 분할·구간 정보·검색 결과: `data/v2/chunks/`, `data/v2/meta/`, `data/v2/qa/`, `data/v2/check/`, `data/v2/boundary/`, `data/v2/local/` 아래의 qa·check·codex·meta·runs, `results/`
  - `data/scenarios/org_a_v2.jsonl`(v2 개발용), `data/scenarios/org_a_holdout.jsonl`, `domain/org_a/customers_holdout.json`(v1 보류용. 서버에 함께 올리기만 했다), `PROGRESS.md`
- **조건 변경:**
  - 모든 시나리오에 `condition_change: true` 이벤트를 하나 이상 넣었다.
  - 무효화(`invalidates_prior: true`) 이벤트는 고객이 접수에 동의하기 전에 일어나도록 이벤트 문구에 시점을 적었다. 빠른 정정(`docs/evaluation.md` §3.3 보완)으로 보내도 쓰기 API와 경합하지 않게 하려는 것이다.
  - 고객 진술을 시스템 기록과 맞게 고치는 정정은 본인 확인 전(첫 발화 직후)에 일어나게 했다. 기록을 조회한 뒤에 기록을 받아들이는 것은 조건 변경이 아니기 때문이다(`docs/data-format.md` condition_changed).
- **법령 질문:** 고객이 상담 중에 법령·고시 물음을 자연스럽게 한다. 언제 무엇을 묻는지는 `reveal_rules`에 적었다. 그 물음에 대한 기대 안내는 `outcome`과 `must_say`에 적었다.
- **약관과 법령이 어긋나 보이는 경우(2개):**
  - 생성 모델이 판단하지 않는다는 원칙(§2.5)대로 기대값을 적었다. 금액·환불 자격은 업무 API(약관) 결과를 따르고, 법령 내용은 있는 그대로 알리며, 상담사 확인이 필요하다고 안내한다.
  - 두 시나리오 모두 고객이 확답을 요구하므로 최종 행동을 `hold`로 적었다(`docs/data-format.md` action: 조항 충돌과 확답 요구).
- **인계:** 최종 행동이 `handoff`인 시나리오는 1개다. 시뮬레이터의 상담사 연결 결함(결정 기록 0015) 때문에 2개 이하로 두었다.
- **금액과 처리:** 환불액, 승인 여부, 환불 방법, 이용 마지막 날, 해지되는 구독, 도구 호출은 손계산이 아니라 모의 업무 API로 냈다.
- **장애 기록:** v2 고객 파일에는 장애 기록을 새로 더하지 않았다. 장애가 필요한 곳은 개발용 파일에 있는 장애 기록을 썼다.
- **법령 근거(`law_quotes`):**
  - 근거 항목 하나는 답에 필요한 내용 하나이고, 원문의 이어진 한 부분이다.
  - 줄바꿈은 빼고 이어 붙였다. 표는 원문 줄 순서대로 옮겼다. 필요한 줄 사이의 상관없는 줄(다른 칸의 글)은 건너뛰고 그 자리에 "…"를 넣었다(25줄 안).
  - 적용 조건(조·항의 머리, 표의 업종 줄)을 담고, 문장 중간에서 끊지 않았다. 다만 조건 줄까지 넣으면 상관없는 호가 범위에 많이 들어오는 두 곳은 해당 호만 옮겼다.
  - `same_elsewhere`는 같은 내용이 다른 문서나 다른 곳에도 적힌 경우에만 적었다. 적용 범위가 게임 전용인 조항은 넣지 않았다. 업종이 다른 분쟁해결기준 표(모바일콘텐츠업)나, 요건 표현은 조금 다르지만 그 시나리오의 사실관계에서 같은 결론을 내는 곳을 대체 근거로 둔 항목이 있다.
- **표준약관:**
  - 다시 배포해도 되는지 확인하지 못한 문서라 원문 문장을 저장소에 두지 않는다(`CLAUDE.md` 고정 규칙, 결정 기록 0023).
  - 표준약관은 대표 근거로 쓰지 않고 대체 근거로만 썼다.
  - 저장소 파일의 `law_quotes`에서는 표준약관 인용문을 뺐다. `law_evidence`에는 원문 위치(수치)만 남겼다.
  - 인용문이 든 전체본은 저장소 밖 `data/v2/local/holdout-v2/`에 있다(아래 "실행").
- 모든 고객과 대화는 가상이다. 실제 기업 약관 문구나 개인정보를 쓰지 않았다. 법령·고시 원문 인용은 예외다.

## 열람 규칙

엔진과 분할·검색을 만드는 사람(코디네이터와 구현 에이전트)은 엔진 설정을 고정하기 전에 다음을 열지 않는다. 기대 결과나 정답 근거가 들어 있다. 열람 금지는 test 분할과 같이 지시로만 지킨다(`docs/evaluation-v2.md` §2.6).

- `data/scenarios/org_a_v2_holdout.jsonl`
- `domain/org_a/customers_v2_holdout.json`
- `bizapi/internal/server/holdout_v2_test.go`
- `data/v2/local/holdout-v2/`(저장소 밖 전체본)

설정 고정 후 한 번만 평가한다. 평가 뒤에는 이 파일들을 고치지 않고, 문제가 있으면 진행 기록에 남긴다.

## 실행

업무 API는 개발용, v1 보류용, v2 보류용 고객 파일을 함께 올려 띄운다. 기대값도 이 구성으로 냈다.

```sh
cd bizapi
go run ./cmd/bizapi -addr 127.0.0.1:8141 -data ../domain/org_a/customers.json -data ../domain/org_a/customers_holdout.json -data ../domain/org_a/customers_v2_holdout.json
```

`mid_call_events`에 `inject`(업무 API 장애 주입)는 없다.

기대 도구 호출 재생 테스트:

```sh
cd bizapi
go test ./internal/server -run HoldoutV2 -v
```

`law_evidence`를 다시 만들 때는 저장소 파일이 아니라 저장소 밖 전체본으로 돌린다. 저장소 파일에는 표준약관 인용문이 없어서, 그대로 돌리면 실패는 0이지만 표준약관 대체 근거 3곳이 빠진다. 저장소 루트에서 다음 순서로 한다.

```sh
cp data/v2/local/holdout-v2/org_a_v2_holdout.full.jsonl data/scenarios/org_a_v2_holdout.jsonl
uv run python -m jikgam.v2.holdout map
cp data/scenarios/org_a_v2_holdout.jsonl data/v2/local/holdout-v2/org_a_v2_holdout.full.jsonl
uv run python data/v2/local/holdout-v2/strip_standard_terms.py
```

## 구성

| 항목 | 수 |
|---|---|
| 시나리오 | 12 (hv01~hv12) |
| v2 보류용 고객 | 12 (C301~C312). 개발용·v1 보류용 고객 사용 0, 새 장애 기록 0 |
| `condition_change: true` 이벤트가 있는 시나리오 | 12 (이벤트 13개) |
| 그중 `invalidates_prior: true` 이벤트가 있는 시나리오 | 12 (이벤트 12개) |
| `inject`(업무 API 장애 주입)가 있는 시나리오 | 0 |
| 제3자 통화자가 있는 시나리오 | 1 |
| 기대 행동 | answer 8, hold 2, handoff 1, refuse 1 |
| 기대 환불액이 있는 시나리오 | 7 (상담사 승인 대상 3, 그중 접수 없이 견적만 안내 1) |
| 약관과 법령이 어긋나 보이는 시나리오 | 2 (기대 행동 모두 hold) |
| 법령·고시 근거 항목 | 21 (시나리오당 1~2개) |
| 근거 위치(대체 근거 포함) | 40 |
| 대체 근거가 있는 항목 | 14 |

근거 항목의 문서 묶음별 수:

| 묶음 | 대표 근거가 이 묶음인 항목 | 대체 근거까지 포함해 이 묶음 위치가 있는 항목 |
|---|---|---|
| 법령 | 18 | 18 |
| 고시 | 3 | 13 |
| 표준약관 | 0 | 3 |

항목이 걸친 묶음 조합은 법령만 8, 법령+고시 7, 법령+고시+표준약관 3, 고시만 3이다.

문서별 근거 위치 수(대체 근거 포함, 합계 40):

| 문서 id | 대표 근거 | 전체 위치 |
|---|---|---|
| ecom-act | 11 | 11 |
| ecom-decree | 2 | 2 |
| ecom-rule | 1 | 1 |
| terms-act | 1 | 1 |
| consumer-act | 2 | 2 |
| content-act | 1 | 2 |
| dispute-std | 3 | 7 |
| content-guide | 0 | 11 |
| ecom-terms | 0 | 3 |
| game-terms | 0 | 0 |

`tags`에는 v1 태그 외에 `law`(12개 모두)와 `law_conflict`(약관과 법령이 어긋나 보이는 2개)를 더했다.

`condition_change`, `invalidates_prior` 표시는 작성자가 붙였다(사람 검토 전).

## 기대값 검증

- **업무 API 재생:**
  - 모든 시나리오의 `expected.tool_calls`를 위 세 파일로 띄운 서버에 순서대로 보냈다(2026-10-18).
  - 모든 호출이 받아들여졌다. 약관상 대상이 아니라서 `NOT_ELIGIBLE`을 기대하는 견적 2건은 그 오류를 돌려줬다.
  - 환불액, 승인 상태, 환불 방법, 이용 마지막 날, 해지 구독이 기대와 같았다.
  - 같은 결제에 접수가 두 건 생기지 않았다. 쓰기 호출은 본인 확인 뒤에만 있다.
- **`bizapi/internal/server/holdout_v2_test.go`:**
  - `TestHoldoutV2ToolCalls`는 위 재생을 테스트로 고정한 것이다.
  - `TestHoldoutV2KeepsOtherQuotes`는 v2 파일을 더해 읽어도 기존 고객의 모든 견적(고객 × 구독 × 사유)이 바뀌지 않는지 확인한다. 비교는 두 가지다: 개발용 대 개발용+v2, 개발용+v1 보류용 대 세 파일. v2 파일에 장애 기록이 없는지도 확인한다.
  - `go test ./...` 전체가 통과했다.
- **근거 위치:** `uv run python -m jikgam.v2.holdout map`의 실패는 0이다.
- **형식:** 모든 줄이 개발용 파일과 같은 키 집합·순서인지 확인했다. 저장소 파일에 표준약관 원문 문장이 없는지도 확인했다.
- **하지 않은 것:** v1처럼 별도 에이전트가 기대값을 다시 도출해 비교하는 블라인드 재도출은 하지 않았다.

## 해석 여지

시나리오는 아래 지점에서 기대값이 갈리지 않게 쓰려 했다. 남는 여지는 그대로 적는다.

- **어긋나 보이는 경우의 최종 행동:**
  - 기대 행동을 `hold`로 적었다.
  - 엔진이 업무 API 결과를 따라 약관상 거절(`refuse`)로 끝내거나, 상담사 연결(`handoff`)로 처리할 여지가 있다. 어느 쪽이든 쓰기 접수는 없다.
- **견적만 안내하는 보류:** 확답을 보류하는 시나리오 하나는 업무 API 견적 금액을 안내하는 것이 맞다고 보고 `refund_amount`를 적었다. 그래서 통과 판정 7번은 그 금액 안내를 요구하고, 접수는 없다. 재생 테스트도 이 경우 견적 금액만 확인한다.
- **근거 범위:** 건너뜀 표시("…")가 있는 인용은 첫 조각부터 끝 조각까지가 근거 범위다(§2.3 보완). 그래서 표에서는 다른 칸의 글도 범위에 들어간다. 덮음 기준(절반 이상)은 이 범위로 잰다.
- **대체 근거의 범위:**
  - 업종이 다른 표나 요건 표현이 조금 다른 고시 조항을 대체 근거로 둔 항목이 있다.
  - 반대로, 대체 근거로 넣지 않은 다른 문서의 같은 내용을 엔진이 근거로 쓰면 10번에서 놓친 것으로 셀 수 있다.
- **청약철회 기산점:** 법률의 청약철회 기간은 계약내용에 관한 서면을 받은 날부터 세지만, 업무 API는 결제일을 기준으로 한다. 시나리오는 두 기준이 갈리지 않는 사실관계로 썼다.
- **장애 기록:**
  - 장애 기록은 전역이라 세 파일을 합치면 다른 파일의 장애 기록도 조회된다. 기대값은 세 파일을 합친 서버에서 냈다.
  - 이용 기간 연장 보상을 접수하는 API는 없다. 연장 보상은 안내만으로 `answer`로 적었다.
- **법정대리인 통화:** 제3자(법정대리인) 통화에서는 본인 확인 호출을 기대 호출에 넣지 않고 인계 호출만 필수로 두었다. 엔진이 본인 확인을 해도 추가 조회 호출로 허용된다.
- **표준약관 대체 근거:** 저장소 파일만으로 `map`을 다시 돌리면 빠진다(위 "실행").

## 해시

sha256 앞 16자리(LF 줄바꿈, 이 문서를 쓴 시점의 내용 기준):

- `data/scenarios/org_a_v2_holdout.jsonl`: `997cf9f1a63e3318`
- `domain/org_a/customers_v2_holdout.json`: `7ade634df06199fe`
