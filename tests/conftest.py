"""공용 fixture: 모의 업무 API(Go)를 빌드해 임의 포트에서 띄운다.

`go run`은 Windows에서 자식 프로세스가 남을 수 있어, 바이너리를 임시 폴더에 빌드해 직접 실행하고 끝나면 종료한다.
go가 없으면 이 fixture를 쓰는 테스트는 건너뛴다.
"""

import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def bizapi_url(tmp_path_factory):
    go = shutil.which("go")
    if go is None:
        pytest.skip("go가 없어 모의 업무 API를 띄울 수 없다")
    exe = tmp_path_factory.mktemp("bizapi") / "bizapi.exe"
    subprocess.run([go, "build", "-o", str(exe), "./cmd/bizapi"], cwd=ROOT / "bizapi", check=True,
                   capture_output=True, timeout=300)
    port = _free_port()
    proc = subprocess.Popen([str(exe), "-addr", f"127.0.0.1:{port}", "-data", str(ROOT / "domain/org_a/customers.json")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.post(f"{url}/_admin/reset", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.1)
        else:
            raise RuntimeError("모의 업무 API가 뜨지 않았다")
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture
def bizapi(bizapi_url):
    """테스트마다 서버 상태(세션, 접수, 멱등 키, 장애 주입)를 비운다."""
    httpx.post(f"{bizapi_url}/_admin/reset", timeout=5).raise_for_status()
    return bizapi_url


def inject_fault(url: str, tool: str, code: str, count: int) -> None:
    httpx.post(f"{url}/_admin/faults", json={"tool": tool, "code": code, "count": count}, timeout=5).raise_for_status()
