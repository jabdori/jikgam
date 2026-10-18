"""웹 서버 실행(ADR 0021).

    uv run python -m jikgam.web                       # 127.0.0.1:8100, /app(고객사 콘솔), /admin(관리 콘솔)
    uv run python -m jikgam.web --allow-holdout       # 미공개 시나리오 실행도 계산·표시(설정 고정 뒤에만)

계정은 저장소 밖 .env 또는 환경 변수 JIKGAM_WEB_ACCOUNTS에 둔다(jikgam/web/auth.py). 없으면 관리자 하나와 고객사마다
하나씩 임의 비밀번호로 만들고 이 터미널에만 보여 준다.

화면은 기본적으로 결과 폴더를 읽는다. 비용 확인 뒤에는 PDF 처리와 val 검색 벤치마크를 실행하며 새 자료는
data/v2/local/web/에 둔다. 기존 관리 벤치마크의 하위 프로세스 출력은 outputs/benchmarks/에 둔다.
상담 시나리오·음성 재생 벤치마크는 모의 업무 API(Go)를 임시로 빌드해 띄운다(start_bizapi).
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
        raise SystemExit(f"모의 업무 API는 로컬 주소와 포트가 있는 URL에만 띄운다: {url}")
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
    ap = argparse.ArgumentParser(description="고객사 콘솔(/app)과 관리 콘솔(/admin)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--root", type=Path, default=ROOT, help="결과 폴더와 설정을 읽을 저장소 루트")
    ap.add_argument("--allow-holdout", action="store_true",
                    help="미공개 시나리오 실행도 계산·표시한다(엔진 설정 고정 뒤에만, data/scenarios/HOLDOUT.md)")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    import uvicorn

    from jikgam.decision.registry import secrets

    from . import auth
    from .app import WebConfig, create_app
    from .data.tenants import load_tenants

    spec = os.environ.get(auth.ENV) or secrets().get(auth.ENV)  # 환경 변수가 .env보다 우선
    if spec:
        accounts = auth.parse_accounts(spec)
        print(f"계정 {len(accounts)}개({auth.ENV})")
    else:
        accounts = auth.generate_accounts(list(load_tenants(args.root.resolve())))
        print(f"{auth.ENV}가 없어 이번 실행에만 쓰는 계정을 만들었다:")
        for a in accounts.values():
            print(f"  {a.name} / {a.password}  ({a.role}{' ' + a.org if a.org else ''})")
    cfg = WebConfig(root=args.root.resolve(), allow_holdout=args.allow_holdout, accounts=accounts)
    print(f"http://{args.host}:{args.port}/app  /admin")
    uvicorn.run(create_app(cfg), host=args.host, port=args.port, log_level="warning", timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
