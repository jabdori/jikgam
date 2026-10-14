"""웹 서버 실행.

    uv run python -m jikgam.web --bizapi-url http://127.0.0.1:8101 --start-bizapi
    uv run python -m jikgam.web --results-root <다른 작업 사본>/results     # 다른 위치의 결과 폴더로 대시보드·타임라인

--bizapi-url은 환경 변수 JIKGAM_BIZAPI_URL로 넘긴다(조직 설정의 bizapi_url보다 우선, docs/engine.md §9).
--start-bizapi는 모의 업무 API(Go)를 임시 폴더에 빌드해 그 주소에서 자식 프로세스로 띄우고, 서버가 끝나면 그 프로세스만
끝낸다. 띄우지 않으면 따로 실행해 둔다: cd bizapi && go run ./cmd/bizapi -addr 127.0.0.1:8101
콘솔 세션의 이벤트 로그는 --console-dir(기본 results/engine/console, git에 넣지 않음)에 남는다.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from jikgam.engine.org import ROOT


def start_bizapi(url: str) -> tuple[subprocess.Popen, Path]:
    """모의 업무 API를 임시 폴더에 빌드해 띄운다. (프로세스, 임시 폴더)."""
    parts = urlsplit(url)
    if parts.hostname not in ("127.0.0.1", "localhost") or not parts.port:
        raise SystemExit(f"--start-bizapi는 로컬 주소와 포트가 있는 URL에만 쓴다: {url}")
    go = shutil.which("go")
    if go is None:
        raise SystemExit("go가 없어 모의 업무 API를 빌드할 수 없다")
    tmp = Path(tempfile.mkdtemp(prefix="jikgam-web-bizapi-"))
    exe = tmp / "bizapi.exe"
    subprocess.run([go, "build", "-o", str(exe), "./cmd/bizapi"], cwd=ROOT / "bizapi", check=True, timeout=300)
    proc = subprocess.Popen([str(exe), "-addr", f"{parts.hostname}:{parts.port}",
                             "-data", str(ROOT / "domain/org_a/customers.json")])
    for _ in range(100):
        try:
            if httpx.post(f"{url}/_admin/reset", timeout=1).status_code == 200:
                return proc, tmp
        except httpx.TransportError:
            time.sleep(0.1)
    proc.terminate()
    raise SystemExit(f"모의 업무 API가 {url}에서 뜨지 않았다")


def main() -> None:
    ap = argparse.ArgumentParser(description="상담 콘솔, 턴 상세 타임라인, 평가 대시보드")
    ap.add_argument("--org", default="org_a")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--bizapi-url", help="모의 업무 API 주소(JIKGAM_BIZAPI_URL로 넘긴다)")
    ap.add_argument("--start-bizapi", action="store_true", help="--bizapi-url 주소에 모의 업무 API를 자식 프로세스로 띄운다")
    ap.add_argument("--results-root", type=Path, default=ROOT / "results")
    ap.add_argument("--console-dir", type=Path, default=ROOT / "results/engine/console")
    ap.add_argument("--allow-holdout", action="store_true",
                    help="보류용 시나리오 실행도 계산·표시한다(엔진 설정 고정 뒤에만, data/scenarios/HOLDOUT.md)")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if args.bizapi_url:
        os.environ["JIKGAM_BIZAPI_URL"] = args.bizapi_url
    if args.start_bizapi and not args.bizapi_url:
        raise SystemExit("--start-bizapi에는 --bizapi-url이 필요하다")

    import uvicorn

    from jikgam.engine.org import load_org

    from .app import WebConfig, create_app

    org = load_org(args.org)
    proc, tmp = start_bizapi(org.bizapi_url) if args.start_bizapi else (None, None)
    try:
        cfg = WebConfig(org, results_root=args.results_root.resolve(), console_dir=args.console_dir.resolve(),
                        allow_holdout=args.allow_holdout)
        print(f"조직 {org.org}, 모의 업무 API {org.bizapi_url}, 결과 폴더 {cfg.results_root}")
        print(f"http://{args.host}:{args.port}/console  /timeline  /dashboard")
        # SSE 연결은 스스로 끝나지 않으므로 종료 대기를 짧게 둔다(남은 콘솔 세션은 lifespan 종료에서 닫는다)
        uvicorn.run(create_app(cfg), host=args.host, port=args.port, log_level="warning", timeout_graceful_shutdown=2)
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=10)
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
