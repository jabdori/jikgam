"""웹 서버(Starlette): 고객사 콘솔(/app/)과 관리 콘솔(/admin/)(ADR 0021).

화면은 서버가 공용 컴포넌트로 HTML을 만들고(jikgam/web/saas, jikgam/web/admin), 정적 파일은 jikgam/web/static이다.
모든 화면과 녹음은 HTTP 기본 인증 뒤에 있다. admin 계정은 /admin/만, tenant 계정은 자기 조직의 /app/만 연다.

고객사 콘솔
- GET  /app/live[?call=]            실시간 통화(재생 피드). /app/live/fragment는 갱신 조각
- POST /app/live/{feed}/takeover    상담사가 넘겨받기
- GET  /app/live/{feed}/audio       그 통화 녹음
- GET  /app/calls[?outcome=&changed=&period=&q=&page=&call=]   통화 기록
- GET  /app/calls/{call}            대화 전체와 녹음 다시 듣기, /app/calls/{call}/audio 녹음 파일
- GET  /app/report[?period=]        운영 리포트
- GET  /app/knowledge[?doc=&seg=&q=] 지식·정책
- GET  /app/integrations            연동

관리 콘솔
- GET  /admin/models                모델·벤치마크. POST /admin/models(등록), POST /admin/benchmarks(실행)
- GET  /admin/replay[/{run}/{session}][?at=]  통화 리플레이, /admin/replay/{run}/{session}/audio 녹음
- GET  /admin/tenants[?org=]        고객사 관리
"""

import time
from dataclasses import dataclass, field
from urllib.parse import quote
from pathlib import Path
from typing import Any, Awaitable, Callable

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from jikgam.engine.org import ROOT

from . import auth, settings
from .admin import models as admin_models
from .admin import replay as admin_replay
from .admin import tenants as admin_tenants
from .admin.common import Ctx as AdminCtx
from .data import benchmarks as bm
from .data import replay as replay_data
from .data.live import Feeds
from .data.tenants import calls_of, load_tenants
from .saas import calls as saas_calls
from .saas import integrations as saas_integrations
from .saas import knowledge as saas_knowledge
from .saas import live as saas_live
from .saas import report as saas_report
from .saas.common import Ctx

STATIC = Path(__file__).parent / "static"


@dataclass
class WebConfig:
    root: Path = ROOT
    allow_holdout: bool = False
    accounts: dict[str, auth.Account] = field(default_factory=dict)
    clock: Callable[[], float] = time.monotonic
    # 벤치마크 실행기(작업, 동시 요청, 시간) → 성공 여부. 테스트에서 하위 프로세스 대신 넣는다
    job_runner: Callable[[Any, int, float], bool] | None = None


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, accounts: dict[str, auth.Account]):
        super().__init__(app)
        self.accounts = accounts

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        path = request.url.path
        if path.startswith("/static/"):
            return await call_next(request)
        acct = auth.check(request.headers.get("authorization"), self.accounts)
        if acct is None:
            return PlainTextResponse("로그인이 필요합니다.", status_code=401,
                                     headers={"WWW-Authenticate": 'Basic realm="jikgam", charset="UTF-8"'})
        if path.startswith("/admin") and acct.role != "admin":
            return PlainTextResponse("관리 콘솔은 직감 팀 계정만 열 수 있습니다.", status_code=403)
        if path.startswith("/app") and acct.role != "tenant":
            return PlainTextResponse("고객사 콘솔은 고객사 계정으로 엽니다.", status_code=403)
        request.state.account = acct
        return await call_next(request)


def create_app(cfg: WebConfig) -> Starlette:
    tenants = load_tenants(cfg.root)
    feeds = Feeds(cfg.root, tenants, cfg.clock)
    jobs = bm.Jobs(cfg.root, cfg.job_runner)

    def ctx(request: Request) -> Ctx:
        acct: auth.Account = request.state.account
        tenant = tenants.get(acct.org or "")
        if tenant is None:
            raise PermissionError(f"계정 {acct.name}의 조직 {acct.org}가 고객사 목록에 없습니다.")
        return Ctx(cfg.root, tenant, feeds, cfg.allow_holdout, dict(request.query_params), acct.name)

    def actx(request: Request, message: str | None = None) -> AdminCtx:
        q = dict(request.query_params)
        msg = message or q.get("ok") or q.get("error")
        return AdminCtx(cfg.root, tenants, cfg.allow_holdout, q, request.state.account.name, jobs, msg)

    def html(markup: Any, status: int = 200) -> HTMLResponse:
        return HTMLResponse(str(markup), status_code=status)

    def not_found() -> Response:
        return PlainTextResponse("찾을 수 없습니다.", status_code=404)

    async def home(request: Request) -> Response:
        return RedirectResponse("/admin/models" if request.state.account.role == "admin" else "/app/calls", status_code=303)

    async def admin_home(request: Request) -> Response:
        return RedirectResponse("/admin/models", status_code=303)

    async def app_home(request: Request) -> Response:
        return RedirectResponse("/app/live", status_code=303)

    async def live_page(request: Request) -> Response:
        return html(saas_live.page(ctx(request)))

    async def live_takeover(request: Request) -> Response:
        c = ctx(request)
        feed = feeds.get(c.tenant.org, request.path_params["fid"])
        if feed is None:
            return not_found()
        feed.take_over()
        return RedirectResponse(f"/app/live?call={feed.id}", status_code=303)

    async def live_audio(request: Request) -> Response:
        c = ctx(request)
        feed = feeds.get(c.tenant.org, request.path_params["fid"])
        if feed is None or not feed.recording or not feed.recording.get("available"):
            return not_found()
        return FileResponse(feed.recording["path"], media_type="audio/wav")

    async def calls_page(request: Request) -> Response:
        return html(saas_calls.page(ctx(request)))

    async def call_detail(request: Request) -> Response:
        out = saas_calls.detail(ctx(request), request.path_params["cid"])
        return html(out) if out is not None else not_found()

    async def call_audio(request: Request) -> Response:
        c = ctx(request)
        call = next((x for x in calls_of(c.root, c.tenant, c.allow_holdout) if x.id == request.path_params["cid"]), None)
        rec = call.recording if call else None
        if rec is None or not rec.get("available"):
            return not_found()
        return FileResponse(rec["path"], media_type="audio/wav")

    async def report_page(request: Request) -> Response:
        return html(saas_report.page(ctx(request)))

    async def knowledge_page(request: Request) -> Response:
        return html(saas_knowledge.page(ctx(request)))

    async def integrations_page(request: Request) -> Response:
        return html(saas_integrations.page(ctx(request)))

    async def models_page(request: Request) -> Response:
        return html(admin_models.page(actx(request)))

    async def models_register(request: Request) -> Response:
        form = await request.form()
        reg = bm.Registration(*(str(form.get(k, "")).strip() for k in
                                ("name", "adapter", "url", "model", "key_env", "price_in", "price_out")))
        ok, msg = bm.register(cfg.root, reg)
        if ok:
            return RedirectResponse(f"/admin/models?ok={quote(msg)}&model={quote(reg.name)}", status_code=303)
        keep = "&".join(f"{k}={quote(str(form.get(k, '')))}" for k in ("name", "adapter", "url", "model", "key_env",
                                                                          "price_in", "price_out"))
        return RedirectResponse(f"/admin/models?error={quote(msg)}&{keep}", status_code=303)

    async def benchmarks_run(request: Request) -> Response:
        form = await request.form()
        model = str(form.get("model", ""))
        suites = [s for s in form.getlist("suite") if s in bm.SUITES]
        try:
            concurrency = int(str(form.get("concurrency") or settings.LOAD_CONCURRENCY))
            duration = float(str(form.get("duration") or settings.LOAD_DURATION_S))
        except ValueError:
            return RedirectResponse(f"/admin/models?error={quote('동시 요청과 시간은 숫자다.')}", status_code=303)
        if model not in bm.models(cfg.root) or not suites or concurrency < 1 or duration <= 0:
            return RedirectResponse(f"/admin/models?error={quote('모델과 벤치마크를 하나 이상 고른다.')}", status_code=303)
        if form.get("confirm") != "yes":
            return html(admin_models.page(actx(request), confirm=(model, suites, concurrency, duration)))
        jobs.submit(model, suites, concurrency, duration)
        return RedirectResponse(f"/admin/models?ok={quote(f'{model} 벤치마크 {len(suites)}개를 시작했다.')}", status_code=303)

    async def tenants_page(request: Request) -> Response:
        orgs = {a.org for a in cfg.accounts.values() if a.role == "tenant" and a.org}
        return html(admin_tenants.page(actx(request), orgs))

    async def replay_page(request: Request) -> Response:
        return html(admin_replay.page(actx(request)))

    async def replay_audio(request: Request) -> Response:
        run, _, name = request.query_params.get("s", "").rpartition("/")
        s = replay_data.find(cfg.root, run, name, cfg.allow_holdout)
        if s is None:
            return not_found()
        rec = replay_data.recording_of(cfg.root, replay_data.ev.read_log(s.path), s.path.parent)
        if rec is None or not rec.get("available"):
            return not_found()
        return FileResponse(rec["path"], media_type="audio/wav")

    routes = [
        Route("/", home),
        Route("/app", app_home),
        Route("/app/live", live_page),
        Route("/app/live/{fid}/takeover", live_takeover, methods=["POST"]),
        Route("/app/live/{fid}/audio", live_audio),
        Route("/app/calls", calls_page),
        Route("/app/calls/{cid}", call_detail),
        Route("/app/calls/{cid}/audio", call_audio),
        Route("/app/report", report_page),
        Route("/app/knowledge", knowledge_page),
        Route("/app/integrations", integrations_page),
        Route("/admin/models", models_page),
        Route("/admin/models", models_register, methods=["POST"]),
        Route("/admin/benchmarks", benchmarks_run, methods=["POST"]),
        Route("/admin/replay", replay_page),
        Route("/admin/tenants", tenants_page),
        Route("/admin", admin_home),
        Route("/admin/replay/audio", replay_audio),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    return Starlette(routes=routes, middleware=[Middleware(AuthMiddleware, accounts=cfg.accounts)])
