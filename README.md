# jikgam (직감)

한국어 실시간 상담을 위한 저지연 AI 판별 시스템.

통화·채팅 중 고객의 말이 바뀔 때마다 빠른 판별 모델로 "무엇이 바뀌었는가, 어떤 조항이 적용되는가, 지금 답할 수 있는가"를 판단하고, 근거가 충분할 때만 생성형 LLM을 호출해 근거 있는 응답을 만든다.

이름은 판별 모델이 하는 일, 즉 맥락을 보고 몇 초 안에 내리는 직관적 판단에서 따왔다.

## 왜

질문 하나에 검색 한 번, 생성 한 번을 하는 RAG는 대화가 계속 바뀌는 상담에 맞지 않는다. 매번 생성형 LLM으로 판단하면 느리고 비싸서 판단을 덜 하게 되고, 그 사이 이미 틀린 제안이 남는다. 또 일반 규정, 예외, 부칙이 함께 적용되는 질문에서 오류가 많다.

이 프로젝트는 판단을 싸고 빠르게 만들어 더 자주 하면 이 두 문제가 줄어드는지를 측정한다. 자세한 내용은 [설계 문서](docs/design.md)에 있다.

## 상태

판별 모델 기준선 측정(val)을 마치고 상담 엔진과 한국어 판별 모델 학습을 진행하는 단계. 평가 기준은 측정 전에 [평가 기준 문서](docs/evaluation.md)에 고정했고, 측정 결과와 결정은 [진행 기록](PROGRESS.md)과 [결정 기록](docs/adr/README.md)에 있다. test 분할은 설정을 고정한 뒤 한 번만 평가하며 아직 평가하지 않았다.

이 저장소의 수치는 측정한 것만 기재하고, 원본 로그(`results/`)와 함께 둔다. 모든 데이터는 가상이며 합성 테스트 결과는 실제 운영 성과가 아니다.

## 실행

```sh
uv sync                                   # Python 3.12 환경
uv run pytest                             # 단위 테스트
cd bizapi && go run ./cmd/bizapi          # 조직 A 모의 업무 API (127.0.0.1:8081)
uv run python -m jikgam.eval.run_decisions --split val --backends jev,gemini-flash   # 판별 평가(.env에 API 키 필요)
uv run python -m jikgam.eval.report results/decisions/val/<run_id>                  # 원본 로그로 표 생성
uv run python -m jikgam.sim.runner --org org_a --scenarios all --policy C           # AI 고객 시나리오 실행(모의 업무 API와 .env 키 필요)
uv run python -m jikgam.sim.report results/engine/scenarios/<run_id>               # 시나리오 판정, 근거 갱신 시간, 무효 노출, 비용
uv run python -m jikgam.web --bizapi-url http://127.0.0.1:8101 --start-bizapi      # 웹 화면: /console, /timeline, /dashboard (127.0.0.1:8100)
```

## 구성

| 경로 | 내용 |
|---|---|
| `docs/design.md` | 문제, 가설, 시스템 설계, 실험 원칙 |
| `docs/data-format.md` | 판별 데이터 형식과 라벨 기준 |
| `domain/org_a/` | 가상 조직 A의 약관, 업무 API 계약, 가상 고객 데이터 |
| `docs/labeling-review.md` | 정답 교차 검토 과정, 일치율, 불일치 판정 |
| `data/questions.json` | 판별 모델에 던지는 고정 질문 세트 |
| `data/cases/` | 판별 사례 원본 (105건, 대화 그룹 40개) |
| `data/build/` | 빌드 결과: train/val/test 분할과 요약 |
| `data/splits.json` | 대화 그룹별 분할 배정 (test는 고정) |
| `data/scenarios/org_a.jsonl` | AI 고객 시뮬레이션 시나리오 30개 |
| `scripts/build_dataset.py` | 판별 데이터 검증, 조항 본문 채우기, 분할 |
| `docs/evaluation.md` | 측정 전에 고정한 평가 기준과 성공 수치 |
| `docs/engine.md` | 상담 엔진 구현 설계 |
| `docs/adr/` | 결정 기록 |
| `docs/research/` | 조사 기록(한국어 STT·TTS 등) |
| `data/question_sets/` | 질문 문구 실험용 질문 세트(빌드 파일은 바꾸지 않는다) |
| `config/decision_models.toml` | 판별 백엔드 설정(버전 고정, 단가와 출처) |
| `jikgam/decision/` | 판별 모델 공통 인터페이스와 백엔드(Jev 형식 API, 생성형 LLM 판단, 규칙·다수 라벨 기준선) |
| `jikgam/eval/` | 판별 평가 실행기와 보고서, 오답 목록 |
| `jikgam/engine/` | 상담 엔진(채팅). `uv run python -m jikgam.engine.chat --org org_a`로 터미널 채팅, `--replay`로 고정 대화 재생 |
| `jikgam/web/` | 웹 화면 세 개(ADR 0005): 상담 콘솔, 턴 상세 타임라인, 평가 대시보드. Starlette 서버와 정적 HTML·JS(빌드 단계 없음). 의존성은 uv 그룹 `web` |
| `jikgam/sim/` | AI 고객 시뮬레이터, 시나리오 실행기(정책 B·C), 엔진 지표(평가 기준 §3.2·§3.3), 시나리오 판정(§4, 코드 + AI 평가) |
| `config/orgs/org_a.toml` | 조직 A 엔진 설정(약관, 업무 API 주소, 판별 백엔드·질문 세트, 검색·정책 설정) |
| `data/replays/` | 엔진 개발용 고정 대화(고객 발화) |
| `bizapi/` | 조직 A 모의 업무 API (Go) |
| `results/` | 측정 원본 로그와 보고서 |
