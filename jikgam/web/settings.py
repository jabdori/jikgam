"""화면 코드가 쓰는 고정 값. 화면 코드와 컴포넌트에는 0과 1 말고는 숫자를 직접 쓰지 않는다(ADR 0022).
값마다 뜻을 docs/design/data.md에 적는다."""

# 통화 기록 한 쪽에 보이는 줄 수
CALLS_PAGE_SIZE = 12
# 통화 리플레이 정답 일치도: 시나리오 정답 조항 중 판단 순위 상위 몇 위 안에 든 개수를 센다(ADR 0021)
REPLAY_TOP_K = 4
# 통화 리플레이 순위 막대에 보이는 조항 수(판별에 넘기는 후보 상한 max_candidates와 같은 8)
REPLAY_RANK_ROWS = 8
# 통화 리플레이 순위 표에 보이는 조항 수(정답 조항을 먼저, 나머지는 한 번이라도 선택된 조항)
REPLAY_HEAT_ROWS = 10
# 순위 표 칸의 글자를 어둡게 바꾸는 확률(배경이 진해지는 값)
HEAT_DARK_ABOVE = 0.45
# 통화 리플레이: 처음 여는 통화(개발용 재생 s20의 음성 2회차)
REPLAY_DEFAULT = "20261014-voice-r2/s20-voice-20261014-voice-r2"
# 통화 리플레이: 음성 세션인지 볼 때 읽는 로그 앞부분 줄 수(voice.session은 세션 시작 직후에 있다)
REPLAY_HEAD_LINES = 12
# 통화 리플레이: 재생 띠에서 판단 표시를 하나로 묶는 거리(전체 길이 대비 %)
REPLAY_MARK_MERGE_PCT = 2.5
# 재생 띠 아래 눈금 간격(초)
REPLAY_TICK_S = 30
# 통화 기록·운영 리포트 기간 고르기(일). 1은 오늘, 0은 전체. 기준은 자료의 마지막 통화 날짜다
REPORT_PERIODS = (1, 7, 30, 0)
DEFAULT_PERIOD = 7
# 실시간 통화: 화면이 새 내용을 받아 오는 간격(밀리초). 바뀐 줄과 상태만 화면에 반영한다
LIVE_REFRESH_MS = 250
# 실시간 통화: 말하는 중인 AI 답의 글자를 드러내는 속도(초당 글자). 그 통화 기록에 끝까지 재생된 답이 없을 때만 쓰는
# 표시용 값이다. 있으면 그 통화의 실제 재생 길이에서 구한다(jikgam/web/data/live.py)
LIVE_SPEECH_CHARS_PER_S = 7.0
# 실시간 통화: 통화가 끝난 뒤 처음부터 다시 흐르기까지 쉬는 시간(초)
LIVE_LOOP_PAUSE_S = 8.0
# 실시간 통화: 재생 피드가 처음 시작할 위치(통화 시작 뒤 초). 화면을 열자마자 대화가 보이게 한다
LIVE_START_OFFSETS = (60.0, 70.0, 25.0)
# 모델·벤치마크: 부하 측정 기본 동시 요청 수와 시간(초)
LOAD_CONCURRENCY = 10
# 모델·벤치마크: 판별 결과표의 1회 비용 소수 자릿수(행마다 같은 자릿수로 맞춘다)
COST_DIGITS = 6
# 모델·벤치마크: 부하 예상 비용을 보여 주는 호출 수 단위
COST_PER_N_CALLS = 1000
LOAD_DURATION_S = 300

# 검색 결과의 표시 분할. 실행은 이 값과 별개로 val만 허용한다.
RETRIEVAL_SPLIT = "test"
RETRIEVAL_FIXED_COMMIT = "89eca24"
RETRIEVAL_BUDGET = 2000
RETRIEVAL_NDCG_K = 10
RETRIEVAL_METRIC_DIGITS = 4
RETRIEVAL_CHUNKS = "b-W4000-C800-raw"
RETRIEVAL_MODELS = {
    "flash": ("flash = (b)", "llm", "b-W4000-C800-raw", 0.5),
    "jev": ("Jev = (c-Jev, θ 0.65)", "jev", "c-jev-W4000-C800-raw-t0.65", 0.65),
    "qwen": ("조정한 Qwen 2B = (c-Qwen, θ 0.55)", "qwen", "c-qwen-W4000-C800-raw-t0.55", 0.55),
}
PDF_MAX_BYTES = 20 * 1024 * 1024
KNOWLEDGE_PAGE_SIZE = 12
KNOWLEDGE_STAGE_COUNT = 4
RETRIEVAL_PIPELINE = "full · snow+RRF · Jev 8"
