"""Codex 작업 하나를 비대화로 돌린다(docs/evaluation-v2.md §5, 다른 계열 모델).

작업 폴더에 task.json(모델, 추론 강도, 샌드박스, 출력 스키마 파일)과 prompt.md를 두고 이 모듈을 실행한다.
Orca 작업 공간의 터미널에서 돌리고(`orca terminal create --command ...`), 코디네이터는 터미널 출력이 아니라
결과 파일만 읽는다. Codex의 진행 기록(events.jsonl)과 마지막 답(last.json)은 작업 폴더에만 쓰고,
이 모듈은 상태와 토큰 수만 출력한다. 열람을 제한한 작업(참고 경계, 점검용 질문)의 내용이 화면에 나오지 않게 하려는 것이다.

    uv run python -m jikgam.v2.codex <작업 폴더>
"""

import json
import subprocess
import sys
import time
import tomllib
from pathlib import Path

USER_CONFIG = Path.home() / ".codex" / "config.toml"
# 격리 실행에서 사용자 설정 가운데 넘기는 값(모델 목록과 중계 주소). MCP 서버, 플러그인, 브라우저 같은 도구는 넘기지 않는다.
KEEP = ("openai_base_url", "model_catalog_json")


def usage_of(events_path: Path) -> dict[str, int]:
    """진행 기록에서 토큰 사용량을 모은다(마지막 turn.completed 기준, 없으면 0)."""
    usage: dict[str, int] = {}
    if not events_path.exists():
        return usage
    for raw in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "turn.completed" and isinstance(ev.get("usage"), dict):
            usage = {k: int(v) for k, v in ev["usage"].items() if isinstance(v, (int, float))}
    return usage


def isolation_args() -> list[str]:
    """사용자 설정을 읽지 않고(--ignore-user-config) 모델 연결에 필요한 값만 넘긴다. 인증은 CODEX_HOME의 것을 쓴다."""
    cfg = tomllib.loads(USER_CONFIG.read_text(encoding="utf-8")) if USER_CONFIG.exists() else {}
    args = ["--ignore-user-config"]
    for key in KEEP:
        if key in cfg:
            args += ["-c", f"{key}={json.dumps(cfg[key])}"]
    return args


def run(task_dir: Path) -> dict:
    task = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    prompt = (task_dir / "prompt.md").read_text(encoding="utf-8")
    work = task_dir / task.get("workdir", ".")
    cmd = ["codex", "exec", "-m", task["model"], "-c", f'model_reasoning_effort="{task["effort"]}"',
           "-C", str(work.resolve()), "-s", task.get("sandbox", "read-only"), "--skip-git-repo-check", "--ephemeral",
           "--json", "--color", "never", "-o", str((task_dir / "last.json").resolve())]
    if task.get("isolated", True):
        cmd[2:2] = isolation_args()
    if task.get("schema"):
        cmd += ["--output-schema", str((task_dir / task["schema"]).resolve())]
    cmd.append("-")
    t0 = time.time()
    with (task_dir / "events.jsonl").open("w", encoding="utf-8") as out, \
            (task_dir / "stderr.txt").open("w", encoding="utf-8") as err:
        proc = subprocess.run(cmd, input=prompt, text=True, encoding="utf-8", stdout=out, stderr=err,
                              timeout=task.get("timeout_s", 3600))
    status = {"model": task["model"], "effort": task["effort"], "isolated": task.get("isolated", True),
              "exit": proc.returncode,
              "seconds": round(time.time() - t0, 1), "usage": usage_of(task_dir / "events.jsonl"),
              "last_message": (task_dir / "last.json").exists(),
              "codex_version": subprocess.run(["codex", "--version"], capture_output=True, text=True).stdout.strip()}
    (task_dir / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return status


def main() -> None:
    status = run(Path(sys.argv[1]))
    print(json.dumps({k: status[k] for k in ("model", "effort", "exit", "seconds", "usage", "last_message")}))
    sys.exit(status["exit"])


if __name__ == "__main__":
    main()
