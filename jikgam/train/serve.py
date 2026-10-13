"""학습한 병합 모델을 Unsloth Studio의 Decision API(Jev 호환, POST /v1/systemone)로 띄운다.

    bash jikgam/train/run_wsl.sh serve --run q35-0.8b-c-mix --port 8888
    bash jikgam/train/run_wsl.sh serve --stop --port 8888

하는 일:
1. `UNSLOTH_SYSTEMONE_MODEL=<병합 폴더> unsloth studio -H 127.0.0.1 -p <포트>`를 백그라운드로 띄운다
   (Unsloth 문서 "Use it in the Decision API"). Windows의 평가 실행기는 WSL2 localhost 전달로 접속한다.
2. 관리자 로그인 → Decision API 켜기(런타임 pytorch, 장치 gpu) → API 키 발급(처음 한 번).
   관리자 비밀번호와 API 키는 ~/jikgam-train/secrets/에 둔다(권한 600). 저장소와 로그에 남기지 않는다.
3. 예열 요청을 보내 모델을 올린다. 응답의 model은 "laya-local", 헤더 x-unsloth-decision-backend는 "pytorch"여야 한다.

요청 모델 이름은 별칭(laya, default, jev-latest) 대신 "laya-local"을 쓴다. 이 이름은 UNSLOTH_SYSTEMONE_MODEL로
띄운 폴더에만 연결되고, 설정 화면에서 다른 모델을 골라도 바뀌지 않는다.
"""

import argparse
import hashlib
import json
import os
import secrets
import signal
import subprocess
import time
from pathlib import Path

import httpx

HOME = Path.home() / "jikgam-train"
SECRETS = HOME / "secrets"
WARM_STATE = {"conversation": [{"speaker": "customer", "text": "예열용 요청입니다.", "final": True}]}
WARM_QUESTIONS = {"warm": {"type": "noul", "instructions": "고객이 인사를 했는가?"}}


def secret_file(name: str, make=None) -> str:
    SECRETS.mkdir(parents=True, exist_ok=True)
    os.chmod(SECRETS, 0o700)
    path = SECRETS / name
    if not path.exists() and make is not None:
        path.write_text(make(), encoding="utf-8")
        os.chmod(path, 0o600)
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def weights_sha16(merged: Path) -> str:
    """병합 모델 식별용: 백본 가중치와 판별 헤드 파일을 이름순으로 이어 sha256 앞 16자리."""
    h = hashlib.sha256()
    for path in sorted(merged.glob("model*.safetensors")) + [merged / "joint_head.safetensors"]:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 24), b""):
                h.update(chunk)
    return h.hexdigest()[:16]


def pid_file(port: int) -> Path:
    return HOME / f"studio-{port}.pid"


def stop(port: int) -> None:
    pf = pid_file(port)
    if not pf.exists():
        print(f"포트 {port}: 기록된 서버 없음")
        return
    pid = int(pf.read_text())
    try:
        os.killpg(pid, signal.SIGTERM)
        for _ in range(30):
            os.kill(pid, 0)
            time.sleep(1)
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    pf.unlink(missing_ok=True)
    print(f"포트 {port}: 서버 종료")


def wait_up(base: str, proc: subprocess.Popen, timeout_s: float = 600) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"Studio가 종료됐다(코드 {proc.returncode}). 로그: {HOME / 'logs'}")
        try:
            if httpx.get(f"{base}/api/health", timeout=5).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    raise SystemExit(f"{base} 가 {timeout_s:.0f}초 안에 뜨지 않았다")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", help="실행 이름(JIKGAM_RUNS 아래) 또는 병합 모델 경로")
    ap.add_argument("--port", type=int, default=8888)
    ap.add_argument("--stop", action="store_true")
    args = ap.parse_args()
    if args.stop:
        stop(args.port)
        return

    runs = Path(os.environ.get("JIKGAM_RUNS", HOME / "runs"))
    run_dir = (Path(args.run) if Path(args.run).is_dir() else runs / args.run).resolve()
    merged = run_dir / "merged" if (run_dir / "merged").is_dir() else run_dir
    if not (merged / "joint_head.safetensors").is_file():
        raise SystemExit(f"병합 모델이 아니다: {merged}")
    stop(args.port)

    # --password(UNSLOTH_STUDIO_PASSWORD)는 처음 한 번만 받는다. 이미 정한 뒤에 다시 주면 Studio가 시작을 거부한다.
    first = not (SECRETS / "studio_admin_password").exists()
    password = secret_file("studio_admin_password", make=lambda: secrets.token_urlsafe(24))
    env = {**os.environ, "UNSLOTH_SYSTEMONE_MODEL": str(merged), "UNSLOTH_SYSTEMONE_DEVICE": "gpu"}
    env.pop("UNSLOTH_STUDIO_PASSWORD", None)
    if first:
        env["UNSLOTH_STUDIO_PASSWORD"] = password
    log = (HOME / "logs" / f"studio-{args.port}.log").open("w")
    proc = subprocess.Popen(["unsloth", "studio", "-H", "127.0.0.1", "-p", str(args.port)], env=env,
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    pid_file(args.port).write_text(str(proc.pid))
    base = f"http://127.0.0.1:{args.port}"
    wait_up(base, proc)

    r = httpx.post(f"{base}/api/auth/login", json={"username": "unsloth", "password": password}, timeout=30)
    r.raise_for_status()
    token = r.json()
    if token.get("must_change_password"):
        raise SystemExit("관리자 비밀번호를 먼저 바꿔야 한다고 응답했다. UI에서 한 번 로그인해 바꾼 뒤 secrets 파일을 고친다.")
    admin = {"Authorization": f"Bearer {token['access_token']}"}
    # 장치는 UNSLOTH_SYSTEMONE_DEVICE로 고정했으므로 설정으로 보내지 않는다(보내면 400).
    r = httpx.put(f"{base}/api/settings/systemone", headers=admin, timeout=30,
                  json={"enabled": True, "backend": "pytorch"})
    if r.status_code != 200:
        raise SystemExit(f"Decision API 설정 실패 HTTP {r.status_code}: {r.text[:300]}")

    def mint() -> str:
        rr = httpx.post(f"{base}/api/auth/api-keys", headers=admin, json={"name": "jikgam-eval"}, timeout=30)
        rr.raise_for_status()
        return rr.json()["key"]

    key = secret_file("studio_api_key", make=mint)
    t0 = time.perf_counter()
    for _ in range(120):
        r = httpx.post(f"{base}/v1/systemone", headers={"Authorization": f"Bearer {key}"}, timeout=600,
                       json={"model": "laya-local", "state": WARM_STATE, "questions": WARM_QUESTIONS})
        if r.status_code == 200:
            break
        if r.status_code not in (503, 529):
            raise SystemExit(f"예열 실패 HTTP {r.status_code}: {r.text[:300]}")
        time.sleep(5)
    print(json.dumps({"port": args.port, "merged": str(merged), "weights_sha256_16": weights_sha16(merged),
                      "pid": proc.pid,
                      "warm_status": r.status_code, "load_seconds": round(time.perf_counter() - t0, 1),
                      "response_model": r.json().get("model") if r.status_code == 200 else None,
                      "backend_header": r.headers.get("x-unsloth-decision-backend")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
