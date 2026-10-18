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
- POST /app/knowledge/upload        PDF 추출과 예상 비용
- POST /app/knowledge/process       비용 확인 뒤 문서 처리
- GET  /app/integrations            연동

관리 콘솔
- GET  /admin/models                모델·벤치마크. POST /admin/models(등록), POST /admin/benchmarks(실행)
- POST /admin/retrieval             검색 val 실행의 비용 확인과 시작
- GET  /admin/replay[/{run}/{session}][?at=]  통화 리플레이, /admin/replay/{run}/{session}/audio 녹음
- GET  /admin/tenants[?org=]        고객사 관리
- GET  /admin/voice-test[?call=&customer=]  음성 시험(이 PC와 같은 Tailscale 네트워크에서만). POST /admin/voice-test/offer(브라우저 WebRTC 제안 → 응답),
  POST /admin/voice-test/stop(종료)
"""

import ipaddress
import time
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit
from pathlib import Path
from typing import Any, Awaitable, Callable

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from jikgam.engine.org import ROOT

from . import auth, settings
from .admin import models as admin_models
from .admin import replay as admin_replay
from .admin import tenants as admin_tenants
from .admin import voicetest as admin_voicetest
from .admin.common import Ctx as AdminCtx
from .data import benchmarks as bm
from .data import processing as proc
from .data import replay as replay_data
from .data import voicetest as voice_data
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
    # 기본값은 실제 문서 공급자다. 테스트에서는 고정된 문서 상태를 주입한다.
    knowledge_documents: Callable[[Path, str], list[dict]] | None = None


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
    processing = proc.Processing(cfg.root)
    voice_tests = voice_data.VoiceTests(cfg.root)

    def ctx(request: Request) -> Ctx:
        acct: auth.Account = request.state.account
        tenant = tenants.get(acct.org or "")
        if tenant is None:
            raise PermissionError(f"계정 {acct.name}의 조직 {acct.org}가 고객사 목록에 없습니다.")
        return Ctx(cfg.root, tenant, feeds, cfg.allow_holdout, dict(request.query_params), acct.name,
                   cfg.knowledge_documents or proc.documents)

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

    async def knowledge_upload(request: Request) -> Response:
        import asyncio

        c = ctx(request)
        try:
            async with request.form(max_files=1, max_fields=2) as form:
                upload = form.get("pdf")
                if upload is None or not hasattr(upload, "read"):
                    raise ValueError("PDF 문서를 선택하세요.")
                payload = await upload.read(settings.PDF_MAX_BYTES + 1)
                title = Path((upload.filename or "문서.pdf").replace("\\", "/")).name
            state = await asyncio.to_thread(processing.prepare_pdf, c.tenant.org, title, payload)
            c.query["doc"] = state["id"]
            return html(saas_knowledge.page(c, pending=state))
        except Exception:
            return html(saas_knowledge.page(c, message="PDF를 읽거나 비용을 계산하지 못했습니다. 파일과 처리 기준 자료를 확인하세요."), 400)

    async def knowledge_process(request: Request) -> Response:
        c = ctx(request)
        form = await request.form()
        if form.get("confirm") != "yes":
            return html(saas_knowledge.page(c, message="예상 비용을 확인하고 처리를 시작하세요."), 400)
        try:
            state = processing.confirm_pdf(c.tenant.org, str(form.get("upload", "")))
        except ValueError as exc:
            return html(saas_knowledge.page(c, message=str(exc)), 400)
        return RedirectResponse("/app/knowledge?doc=" + state["id"], status_code=303)

    async def integrations_page(request: Request) -> Response:
        return html(saas_integrations.page(ctx(request)))

    async def models_page(request: Request) -> Response:
        confirm = request.query_params.get("retrieval")
        if confirm not in settings.RETRIEVAL_MODELS:
            confirm = None
        return html(admin_models.page(actx(request), retrieval_jobs=processing.recent(), retrieval_confirm=confirm))

    async def retrieval_run(request: Request) -> Response:
        form = await request.form()
        model, split = str(form.get("boundary_model", "")), str(form.get("split", "val"))
        try:
            proc.validate(model, split)
            if form.get("confirm") != "yes":
                return html(admin_models.page(actx(request), retrieval_confirm=model, retrieval_jobs=processing.recent()))
            processing.submit(model, split)
        except ValueError as exc:
            return html(admin_models.page(actx(request, str(exc)), retrieval_jobs=processing.recent()), 400)
        return RedirectResponse("/admin/models", status_code=303)

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

    voice_nets = [ipaddress.ip_network(n) for n in settings.VOICE_TEST_ALLOWED_NETS]

    def local_only(request: Request) -> Response | None:
        """음성 시험은 이 PC와 같은 Tailscale 네트워크의 기기에서만 연다. tailscale serve를 거치면 웹 서버가 실제 접속 기기
        주소(X-Forwarded-For, 이 PC의 프록시가 보낸 것만 믿는다)를 본다. 쓰기 요청은 같은 출처에서 온 것만 받는다(다른 사이트가
        통화를 열지 못하게)."""
        try:
            addr = ipaddress.ip_address(request.client.host if request.client else "")
        except ValueError:
            addr = None
        if addr is None or not any(addr in n for n in voice_nets):
            return PlainTextResponse("음성 시험은 이 PC와 같은 Tailscale 네트워크의 기기에서만 열 수 있습니다.", status_code=403)
        origin = request.headers.get("origin")
        if request.method == "POST" and origin and urlsplit(origin).netloc != request.url.netloc:
            return PlainTextResponse("다른 출처의 요청은 받지 않습니다.", status_code=403)
        return None

    async def voice_test_page(request: Request) -> Response:
        return local_only(request) or html(admin_voicetest.page(actx(request), voice_tests))

    async def voice_test_offer(request: Request) -> Response:
        if (deny := local_only(request)) is not None:
            return deny
        try:
            body = await request.json()
            sdp, sdp_type = str(body["sdp"]), str(body["type"])
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "브라우저 연결 제안(SDP)이 없습니다."}, status_code=400)
        try:
            answer = await voice_tests.start(sdp, sdp_type, body.get("customer"))
        except voice_data.VoiceTestError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        return JSONResponse(answer)

    async def voice_test_stop(request: Request) -> Response:
        if (deny := local_only(request)) is not None:
            return deny
        try:
            call_id = str((await request.json()).get("call") or "")
        except ValueError:
            call_id = ""
        call = await voice_tests.stop(call_id, "시험자가 종료를 눌렀다") if call_id else None
        return JSONResponse({"call": call.id if call else None, "status": call.status if call else None})

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
        Route("/app/knowledge/upload", knowledge_upload, methods=["POST"]),
        Route("/app/knowledge/process", knowledge_process, methods=["POST"]),
        Route("/app/integrations", integrations_page),
        Route("/admin/models", models_page),
        Route("/admin/models", models_register, methods=["POST"]),
        Route("/admin/benchmarks", benchmarks_run, methods=["POST"]),
        Route("/admin/retrieval", retrieval_run, methods=["POST"]),
        Route("/admin/replay", replay_page),
        Route("/admin/tenants", tenants_page),
        Route("/admin", admin_home),
        Route("/admin/replay/audio", replay_audio),
        Route("/admin/voice-test", voice_test_page),
        Route("/admin/voice-test/offer", voice_test_offer, methods=["POST"]),
        Route("/admin/voice-test/stop", voice_test_stop, methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    return Starlette(routes=routes, middleware=[Middleware(AuthMiddleware, accounts=cfg.accounts)])
