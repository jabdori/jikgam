"""한 명령으로 시나리오 실행 → 비공개 원본 보존 → 공개 사본 → 원문 검사를 한다.

uv run python -m jikgam.v2.e2e_run -- --policy C --law r0 --run-id 개발실행 ...
나머지 인자는 jikgam.sim.runner와 같다. 미공개 잠금도 같은 실행기가 검사한다.
"""

import subprocess
import sys
from pathlib import Path

from jikgam.engine.org import ROOT
from jikgam.sim.runner import OUT_DIR
from jikgam.v2.e2e_logs import publish
from jikgam.v2.terms_scan import load_fragment_sets


def run(args):
    args = list(args)
    if args and args[0] == "--":
        args.pop(0)
    if "--run-id" not in args:
        raise SystemExit("원본을 보존할 실행 이름 --run-id가 필요합니다")
    run_id = args[args.index("--run-id") + 1]
    if Path(run_id).name != run_id or run_id in (".", ".."):
        raise SystemExit("실행 이름에는 경로를 쓸 수 없습니다")
    out = Path(args[args.index("--out-dir") + 1]) if "--out-dir" in args else OUT_DIR
    run_dir = out / run_id
    if run_dir.exists():
        raise SystemExit("기존 실행을 덮어쓸 수 없습니다. 새 --run-id를 지정하세요")
    needles, _ = load_fragment_sets()
    result = subprocess.run([sys.executable, "-m", "jikgam.sim.runner", *args], cwd=ROOT, check=False)
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
