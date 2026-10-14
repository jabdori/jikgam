"""웹 서버(Starlette). 화면 세 개와 그 화면이 쓰는 API.

화면: /console(상담 콘솔), /timeline/<로그>(턴 상세 타임라인), /dashboard(평가 대시보드). 정적 파일은 jikgam/web/static.

API
- GET  /api/config                    조직, 정책, 재생 파일, 업무 API 주소
- POST /api/sessions                  {policy, replay?, speed?} 새 콘솔 세션. replay가 있으면 모의 업무 API를 비우고 재생한다
- GET  /api/sessions/{sid}/stream     SSE. event: update {messages, view, session}, 끝나면 event: closed
- POST /api/sessions/{sid}/messages   {text} 고객 메시지(기다리지 않는다)
- POST /api/sessions/{sid}/handoff    상담사 인계 요청(엔진 정책 경로)
- POST /api/sessions/{sid}/close
- GET  /api/logs                      타임라인으로 열 수 있는 이벤트 로그 목록
- GET  /api/timeline?path=<root>/<경로> 타임라인 자료. root는 results(결과 폴더) 또는 console(콘솔 로그 폴더)
- GET  /api/dashboard                 대시보드 자료

로그 경로는 두 루트 아래의 .jsonl만 받는다(시뮬레이터 기록 *.sim.jsonl 제외). 보류용 시나리오 실행 폴더의 로그는
allow_holdout일 때만 연다(data/scenarios/HOLDOUT.md).
"""

import asyncio
import itertools
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from jikgam.engine import events as ev
from jikgam.engine.chat import make_session, reset_bizapi
from jikgam.engine.org import ROOT, OrgConfig
from jikgam.engine.retrieval import parse_terms

from . import dashboard as db
from .live import LiveSession, load_replay
from .timeline import build_timeline

STATIC = Path(__file__).parent / "static"
POLICIES = {
    "C": "엔진 + 판별 모델(조직 설정 백엔드), 확정 발화에서 판단",
    "B": "엔진 + 생성형 LLM 판단(gemini-flash)",
    "A": "개선형 RAG 기준선(판별·버전·무효화 없음)",
}
SPEEDS = (0.0, 1.0, 2.0, 4.0)


@dataclass
class WebConfig:
    org: OrgConfig
    results_root: Path = ROOT / "results"
    console_dir: Path = ROOT / "results/engine/console"
    replay_dir: Path = ROOT / "data/replays"
    allow_holdout: bool = False
    # (정책, 세션 ID, 로그 폴더) → 엔진 세션. 테스트에서 가짜 판별기·생성기를 넣을 때 바꾼다
    session_factory: Callable[[str, str, Path], Any] | None = None
    sessions: dict[str, LiveSession] = field(default_factory=dict)

    def make(self, policy: str, sid: str) -> Any:
        if self.session_factory is not None:
            return self.session_factory(policy, sid, self.console_dir)
        return make_session(policy, None, self.org, sid, self.console_dir)

    @property
    def roots(self) -> dict[str, Path]:
        return {"results": self.results_root, "console": self.console_dir}


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _page(name: str) -> Callable:
    async def page(request: Request) -> Response:
        return FileResponse(STATIC / name, media_type="text/html; charset=utf-8")
    return page


def _summary_of(dir_: Path) -> tuple[dict[str, Any] | None, bool]:
    """(summary.json 내용, 보류용으로 잠글지). 파일이 있는데 읽을 수 없으면(실행기가 세션마다 다시 쓰는 중일 수 있다)
    보류용인지 알 수 없으므로 잠근다."""
    p = dir_ / "summary.json"
    if not p.exists():
        return None, False
    try:
        summary = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, True
    return summary, db.is_holdout(summary)


def _is_event_log(path: Path) -> bool:
    if path.suffix != ".jsonl" or path.name.endswith(".sim.jsonl"):
        return False
    try:
        with path.open(encoding="utf-8") as f:
            first = f.readline()
        return json.loads(first).get("type") == ev.SESSION_START
    except (OSError, ValueError, AttributeError):
        return False


def list_logs(cfg: WebConfig) -> list[dict[str, Any]]:
    """두 루트 아래 이벤트 로그를 폴더별로 모은다. 보류용 실행 폴더는 allow_holdout이 아니면 잠금으로만 보인다."""
    groups = []
    console = cfg.console_dir.resolve()
    for key, root in cfg.roots.items():
        if not root.is_dir():
            continue
        dirs = {p.parent for p in root.rglob("*.jsonl")}
        for d in dirs:
            if key == "results" and (d.resolve() == console or console in d.resolve().parents):
                continue  # 콘솔 로그는 console 루트로만 보인다
            summary, holdout = _summary_of(d)
            rel_dir = d.relative_to(root).as_posix()
            group = {"root": key, "dir": rel_dir if rel_dir != "." else "", "holdout": holdout,
                     "locked": holdout and not cfg.allow_holdout,
                     "policy": (summary or {}).get("policy"), "mtime": d.stat().st_mtime, "files": []}
            if not group["locked"]:
                for p in sorted(d.glob("*.jsonl")):
                    if _is_event_log(p):
                        group["files"].append({"path": f"{key}/{p.relative_to(root).as_posix()}", "name": p.name,
                                               "size": p.stat().st_size})
                if not group["files"]:
                    continue
            groups.append(group)
    return sorted(groups, key=lambda g: -g["mtime"])


def resolve_log(cfg: WebConfig, path: str) -> Path:
    """'<root>/<상대 경로>'를 실제 파일로. 루트 밖, .jsonl이 아닌 것, 시뮬레이터 기록, 잠긴 보류용 로그는 거절한다."""
    key, _, rel = path.partition("/")
    root = cfg.roots.get(key)
    if root is None or not rel:
        raise PermissionError("알 수 없는 로그 경로다")
    p = (root / rel).resolve()
    if root.resolve() not in p.parents:
        raise PermissionError("로그 폴더 밖의 경로다")
    if p.suffix != ".jsonl" or p.name.endswith(".sim.jsonl"):
        raise PermissionError("엔진 이벤트 로그(.jsonl)만 열 수 있다")
    _, holdout = _summary_of(p.parent)
    if holdout and not cfg.allow_holdout:
        raise PermissionError("보류용 시나리오 실행은 엔진 설정 고정 전 열람 금지다(data/scenarios/HOLDOUT.md)")
    if not p.is_file():
        raise FileNotFoundError(path)
    return p


def create_app(cfg: WebConfig) -> Starlette:
    titles = {a.id: a.title for a in parse_terms(cfg.org.terms).values()}
    counter = itertools.count(1)

    def live(request: Request) -> LiveSession | None:
        return cfg.sessions.get(request.path_params["sid"])

    async def config(request: Request) -> Response:
        replays = sorted(p.name for p in cfg.replay_dir.glob("*.jsonl")) if cfg.replay_dir.is_dir() else []
        return JSONResponse({
            "org": cfg.org.org, "today": cfg.org.today.isoformat(), "bizapi_url": cfg.org.bizapi_url,
            "decision_backend": cfg.org.decision_backend, "generator": cfg.org.generator,
            "applies_threshold": cfg.org.applies_threshold,
            "policies": [{"id": k, "description": v} for k, v in POLICIES.items()], "replays": replays,
            "speeds": list(SPEEDS), "allow_holdout": cfg.allow_holdout,
            "sessions": [s.state() for s in cfg.sessions.values() if not s.closed],
        })

    async def create_session(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            return _error(400, "JSON 본문이 필요하다")
        policy = body.get("policy", "C")
        if policy not in POLICIES:
            return _error(400, f"정책은 {list(POLICIES)} 중 하나다")
        rows = None
        replay = body.get("replay")
        if replay:
            path = (cfg.replay_dir / str(replay)).resolve()
            if path.parent != cfg.replay_dir.resolve() or not path.is_file():
                return _error(400, f"재생 파일이 없다: {replay}")
            try:
                rows = load_replay(path)
            except ValueError as e:
                return _error(400, str(e))
        try:
            speed = float(body.get("speed", 1.0))
        except (TypeError, ValueError):
            speed = -1.0
        if not 0 <= speed <= 100:
            return _error(400, "speed는 0(대기 없음) 이상 100 이하의 수다")
        sid = f"console-{policy}-{datetime.now(ev.KST).strftime('%Y%m%dT%H%M%S')}-{next(counter)}"
        try:
            if rows is not None:  # 재생은 깨끗한 업무 API 상태를 가정한다(chat.py --reset-bizapi와 같다)
                await asyncio.to_thread(reset_bizapi, cfg.org.bizapi_url)
            session = cfg.make(policy, sid)
        except SystemExit as e:
            return _error(400, str(e))
        except Exception as e:  # API 키 누락, 업무 API 연결 실패 등
            return _error(502, f"세션을 만들 수 없다: {type(e).__name__}: {e}")
        ls = LiveSession(sid, policy, session, titles, cfg.org.bizapi_url, replay_name=replay,
                         deduction_reasons=cfg.org.policy.deduction_reasons)
        cfg.sessions[sid] = ls
        ls.start()
        if rows is not None:
            ls.start_replay(rows, speed)
        return JSONResponse({**ls.state(), "timeline": f"/timeline/console/{ls.log_path.name}"}, status_code=201)

    async def stream(request: Request) -> Response:
        ls = live(request)
        if ls is None:
            return _error(404, "세션이 없다")
        return StreamingResponse(ls.stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def message(request: Request) -> Response:
        ls = live(request)
        if ls is None:
            return _error(404, "세션이 없다")
        try:
            body = await request.json()
            ls.say(str(body.get("text", "")))
        except ValueError as e:
            return _error(400, str(e) or "JSON 본문이 필요하다")
        except RuntimeError as e:
            return _error(409, str(e))
        return JSONResponse({"accepted": True}, status_code=202)

    async def handoff(request: Request) -> Response:
        ls = live(request)
        if ls is None:
            return _error(404, "세션이 없다")
        try:
            ls.handoff()
        except RuntimeError as e:
            return _error(409, str(e))
        return JSONResponse({"accepted": True}, status_code=202)

    async def close(request: Request) -> Response:
        ls = live(request)
        if ls is None:
            return _error(404, "세션이 없다")
        await ls.close()
        return JSONResponse(ls.state())

    async def logs(request: Request) -> Response:
        return JSONResponse(await asyncio.to_thread(list_logs, cfg))

    async def timeline(request: Request) -> Response:
        path = request.query_params.get("path", "")
        try:
            p = resolve_log(cfg, path)
            events = await asyncio.to_thread(ev.read_log, p)
        except PermissionError as e:
            return _error(403, str(e))
        except FileNotFoundError:
            return _error(404, f"로그가 없다: {path}")
        except ValueError as e:
            return _error(422, f"로그를 읽을 수 없다: {e}")
        data = build_timeline(events)
        data["path"] = path
        return JSONResponse(db.jsonable(data))

    async def dashboard(request: Request) -> Response:
        data = await asyncio.to_thread(db.dashboard, cfg.results_root, cfg.allow_holdout)
        return JSONResponse(data)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        yield
        for ls in list(cfg.sessions.values()):
            await ls.close()

    async def home(request: Request) -> Response:
        return RedirectResponse("/console")

    routes = [
        Route("/", home),
        Route("/console", _page("console.html")),
        Route("/timeline", _page("timeline.html")),
        Route("/timeline/{path:path}", _page("timeline.html")),
        Route("/dashboard", _page("dashboard.html")),
        Route("/api/config", config),
        Route("/api/sessions", create_session, methods=["POST"]),
        Route("/api/sessions/{sid}/stream", stream),
        Route("/api/sessions/{sid}/messages", message, methods=["POST"]),
        Route("/api/sessions/{sid}/handoff", handoff, methods=["POST"]),
        Route("/api/sessions/{sid}/close", close, methods=["POST"]),
        Route("/api/logs", logs),
        Route("/api/timeline", timeline),
        Route("/api/dashboard", dashboard),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    return Starlette(routes=routes, lifespan=lifespan)
