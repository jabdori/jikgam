# 웹 콘솔과 디자인 하네스

> 처음 읽는다면: [전체 흐름 안내](../overview.md) · [용어집](../glossary.md)

웹 화면은 두 제품으로 나뉜다(ADR 0021).

- **고객사 콘솔(`/app/`):** 상담 운영팀이 매일 쓰는 화면이다. 밝은 테마이고 엔진 용어와 수치를 넣지 않는다.
- **관리 콘솔(`/admin/`):** 직감 팀 내부용이다. 어두운 테마에 ADMIN 표시를 단다. 모델, 평가, 진단을 다룬다.

화면을 만들기 전에 일관성과 불필요한 요소를 검사로 잡는 장치(디자인 하네스)를 먼저 만들었다(ADR 0022). 하네스를 통과하지 않은 화면은 완료로 보지 않는다.

## 띄우기

```sh
uv run python -m jikgam.web                  # http://127.0.0.1:8100/app, /admin
uv run python -m jikgam.web --allow-holdout  # 미공개 시나리오 실행도 계산(설정 고정 뒤에만)
```

- 계정은 저장소 밖 `.env`나 환경 변수 `JIKGAM_WEB_ACCOUNTS`에 둔다.
  - 형식: `이름:비밀번호:admin` 또는 `이름:비밀번호:tenant:조직`을 세미콜론으로 잇는다.
  - 설정이 없으면 관리자 하나와 고객사마다 하나씩 임의 비밀번호로 만들고, 터미널에만 보여 준다.
- 인증은 로컬 시연용 HTTP 기본 인증이다.
- 화면은 결과 폴더의 원본 로그와 녹음을 읽기만 한다. 녹음 WAV는 git에 넣지 않으므로, 파일이 없는 PC에서는 "녹음 파일이 없습니다"로 보인다.
- 예외는 관리 콘솔에서 확인을 거쳐 시작하는 벤치마크 실행이다. 하위 프로세스로 돌고, 출력은 `outputs/benchmarks/`에 남는다.

## 화면

| 콘솔 | 화면 | 경로 | 시안 |
|---|---|---|---|
| 고객사 | 실시간 통화 | `/app/live` | `mock/Main.dc.html` |
| 고객사 | 통화 기록(목록·상세, 대화 전체와 녹음) | `/app/calls`, `/app/calls/<통화>` | `mock/Compare.dc.html` |
| 고객사 | 운영 리포트 | `/app/report` | `mock/Report.dc.html` |
| 고객사 | 지식·정책 | `/app/knowledge` | `mock/Knowledge.dc.html` |
| 고객사 | 연동 | `/app/integrations` | `mock/Integrations.dc.html` |
| 관리 | 모델·벤치마크 | `/admin/models` | `mock/Quality.dc.html` |
| 관리 | 통화 리플레이 | `/admin/replay` | `mock/Trace.dc.html` |
| 관리 | 고객사 관리 | `/admin/tenants` | `mock/Tenants.dc.html` |

화면 수치가 무엇을 세는지와 어느 파일에서 오는지는 [data.md](data.md)에 있다.

## 코드 배치

| 위치 | 하는 일 |
|---|---|
| `jikgam/web/static/tokens.css` | 색, 글꼴, 크기, 간격, 모서리, 테두리 토큰. 두 테마가 같은 이름을 쓴다 |
| `jikgam/web/static/base.css`, `components.css` | 기본 스타일과 컴포넌트 모양. 토큰만 쓴다 |
| `jikgam/web/static/console.js` | 동작만 붙인다(드로어 닫기, 녹음과 자막 맞추기, 실시간 갱신, 통화 듣기). 글자를 만들지 않는다 |
| `jikgam/web/ui/html.py` | 가장 낮은 수준의 HTML 함수. 컴포넌트와 숫자 형식만 쓴다 |
| `jikgam/web/ui/components.py` | 공용 컴포넌트([components.md](components.md)) |
| `jikgam/web/ui/fmt.py` | 숫자 형식. 화면의 숫자는 모두 여기를 거쳐 `data-num` 요소로 나간다 |
| `jikgam/web/settings.py` | 화면 코드가 쓰는 고정 값(쪽당 행 수, 정답 일치도의 k 등) |
| `jikgam/web/data/` | 결과 파일에서 화면 자료를 만든다(HTML 없음) |
| `jikgam/web/saas/`, `jikgam/web/admin/` | 화면 코드. 컴포넌트와 fmt만 쓴다 |
| `config/web/tenants.toml` | 고객사 이름, 통화 기록으로 보일 실행 폴더, 실시간 통화 재생 피드 |

## 하네스

| 검사 | 하는 일 | 돌리는 곳 |
|---|---|---|
| 1. 토큰 하나 | 토큰 파일 밖의 색상값·px·style 속성, 정의되지 않은 토큰, 두 테마의 이름 차이를 잡는다 | `tests/test_design_lint.py` |
| 2. 공용 컴포넌트 | 화면 코드가 낮은 수준 HTML 함수를 직접 부르면 실패한다 | `tests/test_design_lint.py` |
| 3. 고객사 화면 문구 | [forbidden-terms.md](forbidden-terms.md)의 낱말이 고객사 화면에 나오면 실패한다 | `tests/test_saas_terms.py`, `scripts/ui_check.py` |
| 4. 숫자 출처 | 화면 코드의 숫자 문자열·상수를 막고(정적), 테스트 자료 두 벌로 렌더링해 숫자가 따라 바뀌는지 본다(동적) | `tests/test_design_lint.py`, `tests/test_number_source.py` |
| 5. 스크린샷 | 화면마다 1440px·390px 전체 페이지를 `screens/`에 남기고 이전 것과 화소를 비교한다 | `scripts/ui_check.py` |
| 6. 접근성 | axe-core 4.12.1로 심각 이상 위반 0건, 화면마다 키보드만으로 주요 흐름을 해 본다 | `scripts/ui_check.py` |
| 7. 독립 리뷰 | 구현을 보지 않은 에이전트가 스크린샷과 시안만 보고 검토한다 | [reviews/](reviews/) |

```sh
uv run pytest tests/test_design_lint.py tests/test_saas_terms.py tests/test_number_source.py
uv run python scripts/ui_check.py              # 브라우저 검사와 스크린샷(바뀐 화소는 screens/changes.md)
uv run python scripts/ui_check.py --no-shots   # 스크린샷 파일을 바꾸지 않고 검사만
```

- **브라우저:** Playwright 1.63.0과 그에 맞는 Chromium(빌드 1243)을 쓴다. 이 PC에 받아 둔 브라우저와 맞춘 버전이다.
- **axe-core:** `axe-playwright-python` 꾸러미에 든 `axe.min.js`를 UTF-8로 직접 읽어 넣는다. 그 꾸러미의 불러오기 코드는 이 PC 기본 인코딩(cp949)에서 실패하기 때문이다.
- **실시간 통화 스크린샷:** 재생 피드의 시계를 고정해 찍는다.

### 검사의 예외

예외는 아래 표가 전부다. 예외를 더할 때는 이 표와 ADR 0022를 함께 고친다.

| 예외 | 이유 |
|---|---|
| `style="--pct: …%"`, `--at`, `--len`, `--alpha` | 자료에서 오는 막대 길이·위치·색 진하기다. 컴포넌트의 `_style_vars`·`_alpha`만 만든다 |
| 컴포넌트의 `h1`~`h4`, `tabindex` 값 `0`·`-1`, `utf-8`, 화면 폭 메타 | 숫자가 아니라 구조 표기다 |
| 컴포넌트의 숫자 상수 `2`(제목 수준 기본값), `100`(백분율) | 같은 이유 |
| 문자열 속 `p50`·`p95`·`p99`, `1회`, `1M` | 지표·단위 이름이다(관리 콘솔 열 이름 "p95", "1회 비용") |
| 숫자 종류 `index`, `rank`, `axis`, `criterion` | 동적 검사에서 뺀다. 목록의 몇 번째, 순위, 재생 띠 눈금, 평가 기준 문서의 사전 기준 값은 결과 자료가 아니다 |
