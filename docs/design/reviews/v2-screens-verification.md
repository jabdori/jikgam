# T2 웹 문서 처리·검색 비교 검증

2026-10-18, 작업 `task_cfa8df268c5e`. 커밋·푸시는 하지 않았다. 독립 디자인 리뷰는 코디네이터가 별도 수행한다.

## 변경

- `jikgam/web/data/processing.py`: PDF 추출, 비용 산정, 조직별 작업 저장, 분할·정보·색인·검색·채점 작업 큐. `ingest`, `v2`, `engine` 코드는 변경하지 않았다.
- `jikgam/web/saas/knowledge.py`, `saas/common.py`, `data/knowledge.py`: 처리 문서 목록, 원문·fields·related, 문서 유지 검색, 비용 확인, 단계 상태. 기존 약관 열람도 유지한다.
- `jikgam/web/admin/retrieval.py`, `admin/models.py`: 다섯 고정 검색 구성과 웹 실행 결과, 분할 모델 선택, 비용 근거와 확인, 진행 상태.
- `jikgam/web/app.py`, `__main__.py`, `settings.py`: 새 라우트, 표시 분할·지표·업로드 설정, 실행 안내.
- `jikgam/web/ui/components.py`, `static/components.css`: 파일 입력, 비활성 선택지, 줄바꿈 보존 원문, 검색 문서 유지. 디자인 토큰을 새로 만들지 않았다.
- `tests/test_web_processing.py`: 13개 회귀 검사. 역할·조직 격리, 확인 전 비용 발생 방지, test/check 차단, Qwen 비활성, 기준 비용 계산, 네이티브 채점 연결, 고정 파일 보존, 실패 비용 보존, 원문·정보 열람을 검사한다.
- `docs/design/components.md`, `data.md`, `scripts/ui_check.py` 화면 목록 및 이 폴더의 스크린샷·변경 기록.

## 라우트

| 경로 | 동작 |
|---|---|
| `POST /app/knowledge/upload` | PDF 추출과 예상 비용. 유료 호출 없음 |
| `POST /app/knowledge/process` | 비용 확인 후 해당 조직 문서 처리 |
| `POST /admin/retrieval` | val 비용 확인과 실행. test/check 등 다른 분할은 거부 |
| `GET /app/knowledge?doc=…&seg=…&q=…` | 기존 경로의 처리 문서·구간·검색 확장 |
| `GET /admin/models?retrieval=jev` | 비용 확인 화면의 읽기 전용 진입점 |

## 검증

- `uv run pytest -q`: **560 passed**, 81.69초. 기존 음성 의존성의 `audioop` 사용 중단 경고 1개.
- `uv run python scripts/ui_check.py`: **12개 화면 × 2개 폭 = 24개 검사 통과**, axe 심각 이상 0건, 고객사 금지어 0건, 가로 넘침 없음. 키보드 흐름 11개 통과.
- `git diff --check -- jikgam/web tests/test_web_processing.py docs/design scripts/ui_check.py`: 통과.
- 브라우저 실행 중 Windows Proactor 연결 종료 콜백의 `WinError 10022`가 출력됐지만, 검사 결과는 모두 통과했고 종료 코드는 0이었다.

## 실제 유료 실행

고정 산출물을 덮어쓰지 않고 화면 라우트를 HTTP로 호출했다. 작은 PDF는 새로 만든 가상 안내문이며 표준약관 문장을 포함하지 않는다.

| 실행 | 결과 경로 | API 사용량 기준 비용(USD) |
|---|---|---:|
| PDF 1개: 추출 → flash 분할 → full 정보 → BM25+snow 색인 | `data/v2/local/web/uploads/org_a/b7cac53c865a48379ef2a2573138f9e2/` | 0.001377 |
| c-Jev val 1회: 분할 → full 정보 → 색인 → Jev 8 검색 → 채점 | `data/v2/local/web/jobs/1491934f428b47acafb8fb99a46e3639/` | 0.756365562 |
| **합계** | | **0.757742562** |

val 실행의 예상 비용은 USD 0.75285였고 실제로는 구간 392개, 검색 질문 84개(근거가 있는 채점 질문 77개)가 완료됐다. 구간 정보비 USD 0.70014075, 분할비 USD 0.026547696, 예열 포함 검색·리랭킹비 USD 0.029677116이다. API 응답의 사용량과 기존 클라이언트의 고정 단가로 계산하며 Gemini 암묵 캐시 할인은 반영하지 않는다. GPU 비용은 포함하지 않는다.

해당 실행의 단일 근거 재현율@2,000은 0.9535, 전체 근거 재현율@2,000은 0.8051, nDCG@10은 0.9218, 검색 p95는 69.61ms, 리랭킹 p95는 264.21ms, 리랭킹 오류는 0개다. 시연 기능 검증용 val 실행이며 최종 성능 주장이 아니다.

## 스크린샷

- `saas-knowledge-1440.png`, `saas-knowledge-390.png`: 문서 목록과 업로드 입력.
- `saas-knowledge-law-1440.png`, `saas-knowledge-law-390.png`: 법령 원문·구간 정보. 표준약관 상세는 촬영하지 않았다.
- `admin-models-1440.png`, `admin-models-390.png`: 비교 세트와 새 실행 결과.
- `admin-retrieval-confirm-1440.png`, `admin-retrieval-confirm-390.png`: 비용별 예상치와 계산 근거, 확인·취소.
- 전체 24개 검사 기록과 화소 변화: `changes.md`.

## 남은 범위

- Qwen 서버가 없어 실제 Qwen 실행은 하지 않았다. 선택 비활성과 서버 쪽 차단은 자동 검사로 검증했다. 전체 flash val 실행도 반복하지 않았다.
- 완료된 색인과 상담 엔진의 게시·적용은 별개다. 이번 범위는 문서 처리와 검색 평가 시연이다.
- 단계 상태는 새로고침 버튼으로 갱신한다. 서버 재시작 후 중단 작업 자동 재개, 경계 편집, 게시 승인 흐름은 구현하지 않았다.
- 로컬 snow 색인은 기존 환경처럼 CUDA와 embed 의존성 그룹이 필요하다. 없는 환경에서는 색인 단계가 실패로 기록된다.
- 독립 디자인 리뷰 및 `PROGRESS.md` 통합 기록은 코디네이터가 담당한다.
