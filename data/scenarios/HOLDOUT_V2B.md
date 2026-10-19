# v2 미공개(보류용) 엔드투엔드 시나리오 2차 묶음(v2b)

`org_a_v2b_holdout.jsonl`은 v2 평가의 엔드투엔드 판정(`docs/evaluation-v2.md` §2.5, §4.4, §8.4)에 쓰는 두 번째 미공개 시나리오 묶음이다. 엔진 설정을 고정한 뒤 확정 측정에 한 번만 쓴다. 상황은 조직 A(가상 구독 서비스) 상담 중에 법령·고시 근거가 필요한 경우다. 첫 v2 묶음(`org_a_v2_holdout.jsonl`, hv01~hv12)과는 따로 만든 별개의 묶음이다.

형식은 개발용 `org_a.jsonl`과 같다. 최상위 키와 순서, `caller`, `persona`, `mid_call_events`의 키가 같다. `expected`에는 v1 키(`intent`, `articles`, `outcome`, `refund_amount`, `tool_calls`, `forbidden`, `must_say`, `action`) 뒤에 두 키가 더 있다.

- `law_quotes`: 작성자가 원문에서 옮긴 법령·고시 근거
- `law_evidence`: `jikgam.v2.holdout map`이 `law_quotes`를 원문 위치(문서 id, 글자 범위)로 바꾼 것

## 작성 규칙

- 엔진 개발에 참여하지 않은 별도 작성자(독립 작성 에이전트)가 썼다. 엔진을 통과시키거나 떨어뜨리려 하지 않고, 약관과 법령이 정하는 현실적인 상담 상황을 썼다.
- **작성자가 본 것:**
  - `CLAUDE.md`, `docs/evaluation.md`, `docs/evaluation-v2.md`, `docs/data-format.md`, `docs/design.md`, `docs/glossary.md`, `docs/overview.md`
  - `domain/org_a/terms.md`, `domain/org_a/api.md`, `domain/org_a/customers.json`, `domain/org_a/customers_holdout.json`, `domain/org_a/customers_v2_holdout.json`(형식 참고와 ID·휴대폰 번호·장애 기록 확인)
  - `bizapi/` 코드와 테스트. 재생 테스트 형식은 `bizapi/internal/server/holdout_v2_test.go`를 참고했다.
  - `data/scenarios/org_a.jsonl`(형식 참고), `data/scenarios/HOLDOUT.md`, `data/scenarios/HOLDOUT_V2.md`
  - `data/scenarios/org_a_v2_holdout.jsonl`: 형식 참고와, 같은 사실관계·물음을 되풀이하지 않으려고 제목, 기대 행동, 근거 조항 ID, 법령 인용 문서와 첫머리를 보았다.
  - `data/v2/local/holdout-v2/strip_standard_terms.py`, `data/v2/local/holdout-v2/rederive/`의 `README.md`·`stripped_fields.json`(묶음 만드는 방식 참고)
  - 추출 원문: `data/v2/extracted/*.jsonl`, `data/v2/extracted/summary.json`, `data/v2/local/{game-terms,ecom-terms}/extracted.jsonl`
- **작성자가 열지 않은 것:**
  - 엔진·평가 코드: `jikgam/` 아래 전부. `jikgam.v2.holdout map`과 `jikgam.v2.terms_scan`은 실행만 했다. 대체 근거 하나가 위치를 못 찾았을 때 원인을 보려고 변형 인용을 담은 시험 파일로 `map`을 저장소 경로에서 한 번 돌렸고, 그 파일은 덮어썼다.
  - `tests/`, `scripts/`, `config/` 전부, `data/question_sets/`
  - 분할·구간 정보·메타데이터·검색 결과: `data/v2/chunks/`, `data/v2/meta/`, `data/v2/law-meta/`, `data/v2/qa/`, `data/v2/check/`, `data/v2/boundary/`, `data/v2/local/` 아래에서 위에 적은 것 외 전부, `results/`
  - `docs/adr/`, `docs/v2-*.md`, `docs/evaluation-v2-r5.md`, `PROGRESS.md`, `data/scenarios/org_a_v2.jsonl`(v2 개발용)과 실행 로그
  - git 기록. git 명령은 쓰지 않았다. 다만 작업 세션 맥락에 최근 커밋 제목 5개가 자동으로 붙어 있어 보았다. 시나리오 내용에는 쓰지 않았다.
  - 그 커밋 제목들에 엔진 법령 프로필의 방식이 한 줄로 적혀 있었다.
  - 디렉터리 목록(파일 이름)만 본 곳: `docs/`, `data/v2/`, `data/v2/local/`, `data/v2/local/holdout-v2/`
- **새로 쓴 상황:** 첫 v2 묶음과 같은 사실관계를 되풀이하지 않았다. 고객, 결제 사실, 조건 변경, 법령 물음을 새로 정했다. 첫 묶음이 대표 근거로 쓴 법령·고시의 조·항·호는 대표 근거로 다시 쓰지 않았다. 같은 항의 다른 호를 대표 근거로 쓴 곳이 1곳 있고, 첫 묶음의 근거와 대체 근거로 겹치는 곳이 있다.
- **조건 변경:**
  - 모든 시나리오에 `condition_change: true` 이벤트를 하나 이상 넣었다.
  - 무효화(`invalidates_prior: true`) 이벤트는 고객이 접수에 동의하기 전에 일어나도록 이벤트 문구에 시점을 적었다. 빠른 정정(`docs/evaluation.md` §3.3 보완)으로 보내도 쓰기 API와 경합하지 않게 하려는 것이다.
  - 고객 진술을 시스템 기록과 맞게 고치는 정정은 본인 확인 전(첫 발화 직후)에 일어나게 했다(`docs/data-format.md` condition_changed).
- **법령 질문:** 고객이 상담 중에 법령·고시 물음을 자연스럽게 한다. 언제 무엇을 묻는지는 `reveal_rules`에 적었다. 그 물음에 대한 기대 안내는 `outcome`과 `must_say`에 적었다.
- **약관과 법령이 어긋나 보이는 경우(8개, 태그 `law_conflict`):**
  - 생성 모델이 판단하지 않는다는 원칙(`docs/evaluation-v2.md` §2.5)대로 기대값을 적었다. 금액·환불 자격은 업무 API(약관) 결과를 따르고, 법령·고시 내용은 있는 그대로 알리며, 상담사 확인이 필요하다고 안내한다.
  - 고객이 확답을 요구하는 5개는 최종 행동을 `hold`로 적었다(`docs/data-format.md` action: 조항 충돌과 확답 요구).
  - 고객이 확답을 요구하지 않고 업무 API 결과대로 진행하거나 통화를 마치는 3개는 그 결과대로 적었다(`answer` 2, `refuse` 1).
- **인계:** 최종 행동이 `handoff`인 시나리오는 2개다. 시뮬레이터의 상담사 연결 결함(`docs/evaluation-v2.md` §2.5) 때문에 4개 이하로 두었다.
- **금액과 처리:** 환불액, 승인 여부, 환불 방법, 이용 마지막 날, 해지되는 구독, 요금제 변경 일할 차감액, 도구 호출은 손계산이 아니라 모의 업무 API로 냈다(아래 "실행"의 네 파일).
- **환불 경로가 둘인 사실관계를 피했다:** 견적 사유(`reason`)가 도구 호출의 핵심 인자라서, 청약철회와 장애 환불처럼 두 사유가 모두 받아들여지는 결제일은 쓰지 않았다. 장애 기록이 전역이므로 결제일을 고를 때 기존 장애 기록과의 관계를 확인했다.
- **장애 기록:** v2b 고객 파일에는 장애 기록을 새로 더하지 않았다. 장애가 필요한 곳은 개발용 파일과 v1 보류용 파일에 있는 장애 기록을 썼다.
- **법령 근거(`law_quotes`):**
  - 근거 항목 하나는 답에 필요한 내용 하나이고, 원문의 이어진 한 부분이다(시나리오당 1~2개). 같은 문서에서 3줄 안에 붙은 두 내용은 `map`이 하나로 합치므로 처음부터 한 항목으로 두었다.
  - 인용문은 손으로 옮기지 않고 추출 원문의 줄을 그대로 이어 붙여 만들었다. 줄바꿈은 빼고 이어 붙였다. 표는 원문 줄 순서대로 옮겼고, 필요한 줄 사이의 상관없는 줄(다른 칸의 글)은 건너뛰고 "…"를 넣었다(25줄 안).
  - 적용 조건(조·항의 머리)을 담고, 문장 중간에서 끊지 않았다. 다만 분쟁해결기준 표의 업종 줄은 해당 행에서 25줄 넘게 떨어져 있어 해당 행만 옮겼다. 정의 조항은 해당 호만 옮겼다.
  - 표 인용 하나는 첫 조각과 같은 글이 다른 표에도 있어 `map`이 그 위치를 잡지 못했다. 그래서 그 비고가 붙은 줄 전체를 첫 조각으로 두었다(다른 행의 글이 첫 조각에 들어간다).
  - `same_elsewhere`는 같은 내용이 다른 문서나 다른 곳에도 적힌 경우에만 적었다. 적용 범위가 게임 전용인 조항은 넣지 않았다. 업종이 다른 분쟁해결기준 표(모바일콘텐츠업), 시행령의 세부 규정, 요건 표현은 조금 다르지만 그 시나리오의 사실관계에서 같은 결론을 내는 곳을 대체 근거로 둔 항목이 있다.
- **표준약관:**
  - 다시 배포해도 되는지 확인하지 못한 문서라 원문 문장을 저장소에 두지 않는다(`CLAUDE.md` 고정 규칙).
  - 표준약관은 대표 근거로 쓰지 않고 대체 근거로만 썼다.
  - 저장소 파일의 `law_quotes`에서는 표준약관 인용문을 뺐다. `law_evidence`에는 원문 위치(수치)만 남겼다.
  - 인용문이 든 전체본은 저장소 밖 `data/v2/local/holdout-v2b/`에 있다(아래 "실행").
- 모든 고객과 대화는 가상이다. 실제 기업 약관 문구나 개인정보를 쓰지 않았다. 법령·고시 원문 인용은 예외다.

## 열람 규칙

엔진과 분할·검색을 만드는 사람(코디네이터와 구현 에이전트)은 엔진 설정을 고정하기 전에 다음을 열지 않는다. 기대 결과나 정답 근거가 들어 있다. 열람 금지는 test 분할과 같이 지시로만 지킨다(`docs/evaluation-v2.md` §2.6).

- `data/scenarios/org_a_v2b_holdout.jsonl`
- `domain/org_a/customers_v2b_holdout.json`
- `bizapi/internal/server/holdout_v2b_test.go`
- `data/v2/local/holdout-v2b/`(저장소 밖 전체본, 표준약관 처리 스크립트, 재도출용 가린 묶음)

설정 고정 후 한 번만 평가한다. 평가 뒤에는 이 파일들을 고치지 않고, 문제가 있으면 진행 기록에 남긴다.

## 실행

업무 API는 개발용, v1 보류용, v2 보류용, v2b 보류용 고객 파일을 이 순서로 합쳐 띄운다. 기대값도 이 구성으로 냈다. 확정 측정도 이 네 파일, 이 순서로 띄운다(아래 포트는 작성 때 쓴 값이다).

```sh
cd bizapi
go run ./cmd/bizapi -addr 127.0.0.1:8151 -data ../domain/org_a/customers.json -data ../domain/org_a/customers_holdout.json -data ../domain/org_a/customers_v2_holdout.json -data ../domain/org_a/customers_v2b_holdout.json
```

`mid_call_events`에 `inject`(업무 API 장애 주입)는 없다.

기대 도구 호출 재생 테스트:

```sh
cd bizapi
go test ./internal/server -run HoldoutV2B -v
```

`law_evidence`를 다시 만들 때는 저장소 파일이 아니라 저장소 밖 전체본으로 돌린다. 저장소 파일에는 표준약관 인용문이 없어서, 그대로 돌리면 실패는 0이지만 표준약관 대체 근거 4곳이 빠진다. 저장소 루트에서 다음 순서로 한다.

```sh
cp data/v2/local/holdout-v2b/org_a_v2b_holdout.full.jsonl data/scenarios/org_a_v2b_holdout.jsonl
uv run python -m jikgam.v2.holdout map --file data/scenarios/org_a_v2b_holdout.jsonl
cp data/scenarios/org_a_v2b_holdout.jsonl data/v2/local/holdout-v2b/org_a_v2b_holdout.full.jsonl
uv run python data/v2/local/holdout-v2b/strip_standard_terms.py
```

## 구성

| 항목 | 수 |
|---|---|
| 시나리오 | 24 (hw01~hw24) |
| v2b 보류용 고객 | 24 (C401~C424). 다른 파일 고객 사용 0, 새 장애 기록 0 |
| `condition_change: true` 이벤트가 있는 시나리오 | 24 (이벤트 24개) |
| 그중 `invalidates_prior: true` 이벤트가 있는 시나리오 | 24 (이벤트 24개) |
| `inject`(업무 API 장애 주입)가 있는 시나리오 | 0 |
| 제3자 통화자가 있는 시나리오 | 1 |
| 기대 행동 | answer 14, hold 5, refuse 3, handoff 2 |
| 기대 환불액이 있는 시나리오 | 10 (상담사 승인 대상 5). 기대 행동은 모두 answer |
| 약관과 법령이 어긋나 보이는 시나리오 | 8 (hold 5, answer 2, refuse 1) |
| 법령·고시 근거 항목 | 31 (항목 1개인 시나리오 17, 2개인 시나리오 7) |
| 근거 위치(대체 근거 포함) | 46 |
| 대체 근거가 있는 항목 | 9 |
| 기대 도구 호출 | 100 (쓰기 호출 24) |

근거 항목의 문서 묶음별 수:

| 묶음 | 대표 근거가 이 묶음인 항목 | 대체 근거까지 포함해 이 묶음 위치가 있는 항목 |
|---|---|---|
| 법령 | 18 | 18 |
| 고시 | 13 | 18 |
| 표준약관 | 0 | 4 |

항목이 걸친 묶음 조합은 법령만 13, 고시만 12, 법령+고시+표준약관 3, 법령+고시 2, 고시+표준약관 1이다.

문서별 근거 위치 수(대체 근거 포함, 합계 46):

| 문서 id | 대표 근거 | 전체 위치 |
|---|---|---|
| ecom-act | 10 | 10 |
| ecom-decree | 3 | 4 |
| ecom-rule | 0 | 0 |
| terms-act | 4 | 4 |
| consumer-act | 0 | 0 |
| content-act | 1 | 1 |
| dispute-std | 0 | 5 |
| content-guide | 13 | 18 |
| ecom-terms | 0 | 4 |
| game-terms | 0 | 0 |

`tags`에는 v1 태그 외에 `law`(24개 모두)와 `law_conflict`(약관과 법령이 어긋나 보이는 8개)를 더했다.

`condition_change`, `invalidates_prior` 표시는 작성자가 붙였다(사람 검토 전).

## 기대값 검증

- **업무 API 재생:**
  - 모든 시나리오의 `expected.tool_calls`를 위 네 파일로 띄운 서버(127.0.0.1:8151)에 순서대로 보냈다(2026-10-19).
  - 모든 호출이 기대대로 받아들여지거나 거절됐다. 약관상 대상이 아니라서 `NOT_ELIGIBLE`을 기대하는 견적 6건은 그 오류를 돌려줬다. 본인 확인 실패를 기대하는 호출 2건(한 시나리오)은 실패했고, 두 번째 실패 응답에 인계 필요 표시가 있었다.
  - 환불액, 승인 상태, 환불 방법, 이용 마지막 날, 해지 구독, 인계 사유, 요금제 변경 일할 차감액이 기대와 같았다.
  - 같은 결제에 접수가 두 건 생기지 않았다. 쓰기 호출은 본인 확인 뒤에만 있고, 최종 행동이 `answer`인 시나리오에만 있다.
- **`bizapi/internal/server/holdout_v2b_test.go`:**
  - `TestHoldoutV2BToolCalls`는 위 재생을 테스트로 고정한 것이다.
  - `TestHoldoutV2BKeepsOtherQuotes`는 v2b 파일을 더해 읽어도 기존 고객의 모든 견적(고객 × 구독 × 사유)이 바뀌지 않는지 확인한다. 비교는 두 가지다: 개발용 대 개발용+v2b, 개발용+v1 보류용+v2 보류용 대 네 파일. v2b 파일에 장애 기록이 없는지와 고객 ID가 C401~C424인지도 확인한다.
  - `go test ./...` 전체가 통과했다.
- **근거 위치:**
  - 전체본 기준 `uv run python -m jikgam.v2.holdout map --file data/scenarios/org_a_v2b_holdout.jsonl`의 실패는 0이다.
  - `map`이 낸 위치 46곳을 원문 글자 범위로 되돌려, 각 위치가 의도한 인용(대표 근거 또는 대체 근거)의 첫 조각으로 시작하고 끝 조각으로 끝나며 모든 조각을 순서대로 담는지 확인했다. 어긋난 곳 0, 위치를 못 찾은 인용 0이다.
- **표준약관 문장:**
  - `strip_standard_terms.py`의 고유 조각 검사(표준약관 줄의 25자 조각 중 공개 문서·조직 A 약관에 공백 무시로 없는 것): 저장소 시나리오 파일에 0.
  - `uv run python -m jikgam.v2.terms_scan`을 저장소 시나리오 파일, v2b 고객 파일, 이 문서, 재생 테스트에 돌려 고유 조각 0, 종료 코드 0이었다.
- **형식:** 모든 줄이 개발용 파일과 같은 키 집합·순서인지 확인했다(최상위, `caller`, `persona`, `mid_call_events`, `expected`는 v1 키 뒤에 `law_quotes`, `law_evidence`).
- **하지 않은 것:** 블라인드 재도출과 대조는 작성자가 하지 않았다. 별도 도출자와 대조자가 하도록 `data/v2/local/holdout-v2b/rederive/`에 가린 묶음(`expected`를 뺀 것)과 설명을 두었다.

## 해석 여지

시나리오는 아래 지점에서 기대값이 갈리지 않게 쓰려 했다. 남는 여지는 그대로 적는다.

- **어긋나 보이는 경우의 보류:** 고객이 확답을 요구하는 5개는 `hold`로 적었다. 엔진이 업무 API 결과를 따라 약관상 거절(`refuse`)로 끝내거나 상담사 연결(`handoff`)로 처리할 여지가 있다. 어느 쪽이든 쓰기 접수는 없다.
- **어긋나 보이지만 확답을 요구하지 않는 경우:** 3개는 고객이 업무 API 결과대로 진행하거나 통화를 마치므로 `answer`(접수 있음) 또는 `refuse`로 적었다. 엔진이 충돌 규칙에 따라 `hold`로 처리할 여지가 있다. 그중 하나는 업무 API가 전액 환불을 주는데도 약관과 고시의 표현이 달라 보여 `law_conflict`를 붙였다.
- **기록을 다투는 거절:** 시스템 이용 기록을 고객이 부인하는 시나리오는 기록을 근거로 `refuse`로 적었다(`docs/data-format.md` 시스템 기록 우선). 엔진이 `hold`나 `handoff`로 처리할 여지가 있다.
- **이용 기간 연장 보상:** 연장 보상을 접수하는 API가 없다. 보상 요구 시나리오의 기대 도구 호출은 조회뿐이다.
- **요금제 변경:** 변경을 접수하는 API가 없어 견적 안내로 끝난다. 견적의 `refund_amount` 필드는 환불이 아니라 일할 차감액이므로 시나리오의 `refund_amount`는 `null`로 두었다. 재생 테스트는 차감액과 결제 금액을 따로 확인한다.
- **본인 확인 실패 인계:** 기대 호출은 정정한 번호로 본인 확인 두 번과 고객 ID 없는 인계다. 엔진이 정정 전 번호로도 시도하면 실패 횟수가 하나 늘어난다.
- **제3자 통화:** 기대 호출이 없다. 엔진이 일반 조회를 더 하면 추가 조회 호출로 허용된다. 계정 소유자 번호로 본인 확인을 시도하는 것은 하면 안 되는 말·처리에 넣었다.
- **안내 질문의 약관 조항:** 자동 갱신 알림을 묻는 두 시나리오의 `articles`에는 알림 조항(제4조)을 넣었다. 처리 결과를 정하는 조항은 아니지만 고객 물음에 직접 답하는 약관 근거라서다.
- **근거 범위:** 건너뜀 표시("…")가 있는 인용은 첫 조각부터 끝 조각까지가 근거 범위다(§2.3 보완). 표에서는 다른 칸의 글도 범위에 들어간다. 첫 조각에 다른 행의 글이 든 대체 근거 1곳은 그 줄의 시작부터 범위다.
- **대체 근거의 범위:** 업종이 다른 표, 시행령의 세부 규정, 요건 표현이 조금 다른 고시 조항을 대체 근거로 둔 항목이 있다. 반대로, 대체 근거로 넣지 않은 다른 문서의 같은 내용을 엔진이 근거로 쓰면 10번에서 놓친 것으로 셀 수 있다.
- **효력 발생 시점:** 업무 API는 견적 기준일(통화일)로 청약철회 기간을 보지만, 법률은 서면 청약철회의 효력을 발송한 날로 정한다. 두 기준이 갈리는 시나리오는 어긋나 보이는 경우로 적었다. 그 밖의 시나리오는 결제일 기준과 서면 수령일 기준이 갈리지 않는 사실관계로 썼다.
- **장애 기록:** 장애 기록은 전역이라 네 파일을 합치면 다른 파일의 장애 기록도 조회된다. 기대값은 네 파일을 합친 서버에서 냈다.
- **표준약관 대체 근거:** 저장소 파일만으로 `map`을 다시 돌리면 빠진다(위 "실행").

## 해시

sha256 앞 16자리(LF 줄바꿈, 아래 "보완(2026-10-19): 독립 재도출 대조 판정" 반영 뒤 기준):

- `data/scenarios/org_a_v2b_holdout.jsonl`: `bf127957ea7ed789` (작성 시점 `b6690f5dd25958d2`)
- `domain/org_a/customers_v2b_holdout.json`: `2757c54bf1e0c5c5` (작성 시점과 같음)

## 보완(2026-10-19): 독립 재도출 대조 판정

**대조 방법.**
- 가린 묶음(`data/v2/local/holdout-v2b/rederive/packet.jsonl`, `expected`·`title`·`tags`를 뺀 것)만 보고 별도 도출자가 다시 도출한 기대값(`rederive/derived.jsonl`)을 작성자 전체본과 항목별로 맞대었다. 항목은 최종 행동, 도구 호출, 환불액, 반드시 안내, 하면 안 되는 말, 근거 조항, 법령·고시 근거의 7개이고, 24개 시나리오에서 168칸이다.
- 분류는 일치 / 같은 뜻의 표현 차이 / 불일치다. 법령·고시 근거는 도출자 인용을 `jikgam.v2.holdout map`으로 원문 위치로 바꿔(실패 0) 같은 조·항·호나 같은 표 줄을 가리키는지(대체 근거 포함) 봤다. 도구 호출은 조회 호출 목록만 달라도 통과 판정 8번이 갈리므로 불일치로 셌다.
- 불일치는 문서(약관, 법령·고시 추출 원문, `docs/evaluation.md`, `docs/evaluation-v2.md`, `docs/data-format.md`, `domain/org_a/api.md`)를 다시 읽어 (가) 작성자 값 유지, (나) 작성자 값 수정, (다) 어느 쪽도 정할 수 없음으로 판정했다.
- 금액·자격·도구 호출 응답은 위 "실행"의 네 고객 파일로 띄운 업무 API(127.0.0.1:8153)로 다시 확인했다.
- 대조자는 엔진 개발에 참여하지 않았고, 위 "열람 규칙"의 파일과 형식 참고 자료만 읽었다. 엔진·평가 코드, 분할·구간 정보, 실패 분석·결정 기록, git 기록은 열지 않았다. `map`이 저장소 경로만 받아, 도출자 인용의 위치를 낼 때 저장소 사본 자리에 잠시 시험 파일을 두고 돌린 뒤 원래 파일로 되돌렸다(되돌린 뒤 해시가 같음을 확인).
- 칸별 분류와 판정 근거는 `rederive/adjudication.md`, 개수는 `rederive/summary_counts.json`에 있다(저장소 밖).

**분류별 개수(168칸).** 일치 60, 같은 뜻의 표현 차이 39, 불일치 69.

| 항목 | 일치 | 표현 차이 | 불일치 |
|---|---|---|---|
| 최종 행동 | 23 | 0 | 1 |
| 도구 호출 | 2 | 0 | 22 |
| 환불액 | 24 | 0 | 0 |
| 반드시 안내 | 0 | 13 | 11 |
| 하면 안 되는 말 | 0 | 13 | 11 |
| 근거 조항 | 7 | 0 | 17 |
| 법령·고시 근거 | 4 | 13 | 7 |

**판정별 개수(불일치 69칸).** (가) 67, (나) 2, (다) 0.

- 도구 호출 불일치 22칸 중 21칸은 견적·쓰기·인계 호출의 이름과 핵심 인자가 같고 작성자 쪽에만 조회 호출이 더 있는 경우다. 조회 호출은 고객이 잘못 말하거나 정정한 사실을 시스템 기록으로 확인하는 것이고 개발용 파일의 형식과 같아 유지했다.
- 근거 조항 불일치는 주로 도출자가 접수 없는 시나리오에서 본인 확인 절차 조항을 뺀 것, 업무 API 견적이 결과를 정하는 데 쓰지 않는 조항이나 계산 예시 별표를 더한 것이다. `docs/data-format.md` applies 기준과 `docs/evaluation.md` §4.1 10 보완에 따라 작성자 값을 유지했다.
- 최종 행동 불일치 1칸은 약관과 법령이 어긋나 보이지만 고객이 확답을 요구하지 않는 경우다. `docs/evaluation-v2.md` §2.5(업무 API의 결정적 규칙)와 위 "작성 규칙"대로 작성자 값을 유지했다.
- 반드시 안내·하면 안 되는 말·법령 근거의 불일치는 도출자가 같은 물음에 다른 조항을 고르거나 다른 안내·금지 항목을 둔 경우다. 작성자 값이 원문과 맞고 고객 물음에 직접 답하는지, 어긋나는 쪽이 통과 판정의 다른 칸으로 이미 걸리는지를 보고 모두 유지했다. 다른 조항이 비슷한 결론을 내는 곳은 위 "해석 여지"(대체 근거의 범위)에 해당한다.

**고친 항목과 이유(시나리오 1개, 칸 2개).**
- hw19 `expected.tool_calls`(도구 호출 1칸): 업무 API 견적 호출 하나를 더했다. 고객의 첫 환불 요청에 대한 자격 판단은 업무 API 견적이 한다(`domain/org_a/api.md`). 같은 구조의 다른 거절·보류 시나리오와 개발용 선례는 그 견적을 기대 호출에 넣었는데 이 시나리오만 빠져 있었다.
- hw19 `expected.articles`(근거 조항 1칸): 본인 확인 절차 조항을 더했다. 고객이 본인 확인 전에 환불을 요청하므로 `docs/data-format.md` applies 기준으로 필요한 조항이고, 작성자는 같은 구조의 나머지 시나리오에 모두 넣었다(v2 1차 묶음 대조와 같은 기준). 도출자가 더한 다른 조항은 받아들이지 않았다.
- 같은 시나리오의 서술 필드 `expected.outcome` 한 문장에 견적 결과 표기를 덧붙였다. 시나리오 상황(페르소나, 공개 규칙, 이벤트, 고객 기록)과 다른 시나리오는 바꾸지 않았다.
- 재생 테스트(`bizapi/internal/server/holdout_v2b_test.go`)에 더한 견적의 거절(`NOT_ELIGIBLE`) 기대를 더했다.
- 이에 따라 위 "구성"의 기대 도구 호출은 100에서 101이 되고(쓰기 호출 24는 그대로), 위 "기대값 검증"의 `NOT_ELIGIBLE`을 기대하는 견적은 6건에서 7건이 된다.

**확인.**
- 전체본을 "실행"의 절차대로 저장소 경로에 복사해 `uv run python -m jikgam.v2.holdout map --file data/scenarios/org_a_v2b_holdout.jsonl`을 돌렸고 실패는 0이다. 결과를 전체본으로 되돌리고 `strip_standard_terms.py`로 저장소 사본을 다시 만들었다(표준약관 인용 4곳 제거, 고유 조각 0). `law_evidence`는 바뀌지 않았다.
- 고치기 전후 차이는 전체본과 저장소 사본 모두 hw19의 위 세 필드뿐이다.
- 고친 기대 도구 호출 전체를 위 네 파일로 띄운 업무 API(127.0.0.1:8153)에 순서대로 보내 기대대로 받아들여지거나 거절되는 것을 확인했고, 끝난 뒤 서버를 껐다.
- `uv run python -m jikgam.v2.terms_scan data/scenarios/org_a_v2b_holdout.jsonl domain/org_a/customers_v2b_holdout.json data/scenarios/HOLDOUT_V2B.md bizapi/internal/server/holdout_v2b_test.go`의 종료 코드는 0이다(고유 조각 0).
- `cd bizapi && go test ./...` 전체가 통과했다.
- 저장소 사본의 모든 줄이 개발용 파일과 같은 키 집합·순서다(`expected`는 v1 키 뒤에 `law_quotes`, `law_evidence`).

**바뀐 해시(sha256 앞 16자리, LF).**
- `data/scenarios/org_a_v2b_holdout.jsonl`: `b6690f5dd25958d2` → `bf127957ea7ed789`
- `domain/org_a/customers_v2b_holdout.json`: `2757c54bf1e0c5c5` (그대로)
