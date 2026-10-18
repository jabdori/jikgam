"""한 명령으로 시나리오 실행 → 비공개 원본 보존 → 공개 사본 → 원문 검사를 한다.

uv run python -m jikgam.v2.e2e_run -- --policy C --law r0 --run-id 개발실행 ...
나머지 인자는 jikgam.sim.runner와 같다. 미공개 잠금도 같은 실행기가 검사한다.
"""

import json
import os
import secrets
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

from jikgam.engine.org import ROOT
from jikgam.sim.runner import OUT_DIR
from jikgam.v2.e2e_logs import publish
from jikgam.v2.terms_scan import load_fragment_sets


def record_digest(value):
    import hashlib
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def live_instance(url):
    try:
        response = httpx.get(url.rstrip("/") + "/_admin/instance", timeout=2, trust_env=False)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("상태 응답은 객체여야 합니다")
        return value
    except (httpx.HTTPError, ValueError) as exc:
        raise ValueError("현재 업무 API 기동을 확인할 수 없습니다") from exc


def verify_live_instance(record, root=ROOT):
    if not isinstance(record.get("nonce"), str) or not record["nonce"]:
        raise ValueError("현재 업무 API 기동 식별자가 없습니다")
    status = live_instance(record["url"])
    expected = {"nonce": record["nonce"], "binary_sha256": record["binary_sha256"],
                "data_files": {str((root / p).resolve()): h for p, h in record["data_files"].items()},
                "data_paths": [str((root / p).resolve()) for p in record["data_paths"]]}
    if any(status.get(k) != v for k, v in expected.items()):
        raise ValueError("현재 업무 API의 기동 식별자·바이너리·데이터 해시가 다릅니다")


def verify_bizapi_record(path, args, contract, root=ROOT):
    from jikgam.v2.freeze import sha256

    record = json.loads(Path(path).read_text(encoding="utf-8"))
    if record.get("contract_digest") != record_digest(contract) or record.get("url") != args.bizapi_url:
        raise ValueError("업무 API 기록의 계약 또는 주소가 다릅니다")
    if sha256(root / record["binary"]) != record["binary_sha256"]:
        raise ValueError("업무 API 바이너리 해시가 다릅니다")
    expected_paths = contract["arguments"].get("bizapi_data", contract["arguments"]["customers"])
    expected_data = {p: contract["files"][p] for p in expected_paths}
    if record.get("data_paths") != expected_paths:
        raise ValueError("업무 API 데이터 파일 목록 또는 순서가 계약과 다릅니다")
    if record.get("data_files") != expected_data:
        raise ValueError("업무 API가 적재한 고객 파일이 계약과 다릅니다")
    expected_sources = {p: h for p, h in contract["files"].items() if p.startswith("bizapi/")}
    if record.get("source_files") != expected_sources:
        raise ValueError("업무 API 빌드 소스가 계약과 다릅니다")
    for p, h in {**record["source_files"], **record["data_files"]}.items():
        if sha256(root / p) != h:
            raise ValueError("업무 API 소스 또는 데이터 해시가 바뀌었습니다")
    expected_command = [str((root / record["binary"]).resolve()), "-addr", record["url"].removeprefix("http://"),
                        "-instance-nonce", record.get("nonce", "")]
    for p in expected_data:
        expected_command += ["-data", str((root / p).resolve())]
    if record.get("command") != expected_command:
        raise ValueError("업무 API 실행 명령이 기록과 다릅니다")
    verify_live_instance(record, root)
    return record


@contextmanager
def managed_bizapi(parsed, contract, root=ROOT, port=8130):
    """그 자리에서 Go 소스로 빌드한 서버만 띄우고, 같은 데이터로 실행·기록한다."""
    from jikgam.v2.claim_check import atomic_json
    from jikgam.v2.freeze import sha256

    if not 1 <= port <= 65535:
        raise ValueError("업무 API 포트가 올바르지 않습니다")
    dest = root / "data/v2/local/e2e" / parsed.run_id / "bizapi"
    dest.mkdir(parents=True, exist_ok=True)
    binary = dest / ("bizapi.exe" if os.name == "nt" else "bizapi")
    build = ["go", "build", "-o", str(binary), "./cmd/bizapi"]
    subprocess.run(build, cwd=root / "bizapi", check=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))
    nonce = secrets.token_hex(32)
    command = [str(binary.resolve()), "-addr", f"127.0.0.1:{port}", "-instance-nonce", nonce]
    data_paths = contract["arguments"].get("bizapi_data", contract["arguments"]["customers"])
    data = {p: contract["files"][p] for p in data_paths}
    for p in data:
        command += ["-data", str((root / p).resolve())]
    url = f"http://127.0.0.1:{port}"
    record = {"build_command": build, "command": command, "cwd": "bizapi", "nonce": nonce,
              "url": url, "binary": binary.relative_to(root).as_posix(), "binary_sha256": sha256(binary),
              "contract_digest": record_digest(contract), "data_files": data,
              "data_paths": data_paths,
              "source_files": {p: h for p, h in contract["files"].items() if p.startswith("bizapi/")}}
    with (dest / "server.jsonl").open("wb") as output:
        process = subprocess.Popen(command, cwd=root / "bizapi", stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("업무 API가 시작 중 종료됐습니다")
                try:
                    verify_live_instance(record, root)
                    break
                except ValueError:
                    time.sleep(.1)
            else:
                raise RuntimeError("업무 API 시작 확인 시간이 지났습니다")
            record["pid"] = process.pid
            path = dest / "record.json"
            atomic_json(path, record)
            yield url, path
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def run(args):
    args = list(args)
    if args and args[0] == "--":
        args.pop(0)
    port = 8130
    if "--bizapi-port" in args:
        index = args.index("--bizapi-port")
        port = int(args[index + 1])
        del args[index:index + 2]
    if "--run-id" not in args:
        raise SystemExit("원본을 보존할 실행 이름 --run-id가 필요합니다")
    run_id = args[args.index("--run-id") + 1]
    if Path(run_id).name != run_id or run_id in (".", ".."):
        raise SystemExit("실행 이름에는 경로를 쓸 수 없습니다")
    out = Path(args[args.index("--out-dir") + 1]) if "--out-dir" in args else OUT_DIR
    run_dir = out / run_id
    if run_dir.exists():
        raise SystemExit("기존 실행을 덮어쓸 수 없습니다. 새 --run-id를 지정하세요")
    from jikgam.sim.runner import argument_parser
    from jikgam.v2.freeze import require_e2e_frozen
    from jikgam.v2.e2e_contract import build_contract
    parsed = argument_parser().parse_args(args)
    scenario = ROOT / (parsed.scenario_file or f"data/scenarios/{parsed.org}.jsonl")
    contract = require_e2e_frozen(scenario, parsed.frozen, ROOT, parsed)
    if parsed.law:
        contract = contract or build_contract(parsed, ROOT)
    if parsed.bizapi_record:
        raise SystemExit("--bizapi-record는 e2e_run이 직접 만듭니다")
    needles, _ = load_fragment_sets()
    def execute(extra):
        return subprocess.run([sys.executable, "-m", "jikgam.sim.runner", *args, *extra], cwd=ROOT, check=False)

    if parsed.law:
        if parsed.bizapi_url:
            raise SystemExit("법령 실행의 업무 API는 e2e_run이 띄웁니다. 주소 대신 --bizapi-port를 쓰세요")
        with managed_bizapi(parsed, contract, port=port) as (url, record):
            result = execute(["--bizapi-url", url, "--bizapi-record", str(record)])
    else:
        result = execute([])
    if (run_dir / "summary.json").exists():
        publish(run_dir, needles)
    elif result.returncode == 0:
        raise RuntimeError("실행기는 종료했지만 실행 요약이 없습니다")
    if result.returncode:
        raise SystemExit(result.returncode)
    print("실행과 원본 보존, 공개 사본 검사 완료")


def main():
    run(sys.argv[1:])


if __name__ == "__main__":
    main()
