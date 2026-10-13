"""로컬 서빙 모델의 참고 비용(docs/evaluation.md §2.6)을 실행기 원본 로그로 계산한다.

    uv run python -m jikgam.train.gpu_cost results/decisions/val/<run_id> --backend ko-2b

§2.6: 측정 구간의 GPU 점유 시간 × 같은 GPU급 클라우드 인스턴스의 시간당 단가 ÷ 요청 수. 전기료·장비비는 넣지 않는다.
점유 시간을 두 가지로 적는다.
- 처리 시간 기준: 그 백엔드 호출의 지연 합(요청을 쉬지 않고 이어 보낼 때의 점유 시간에 가깝다).
- 측정 구간 기준: 실행 전체 시간. 다른 백엔드를 번갈아 부르는 동안에도 GPU에 모델을 올려 두었으므로 상한이다.
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

# RunPod 가격표 https://www.runpod.io/pricing (페이지 수정일 2026-10-08, 2026-10-14 확인). 3090 Ti는 목록에 없어 RTX 3090.
PRICES = {"runpod-rtx3090-secure": 0.50, "runpod-rtx3090-community": 0.22}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--backend", required=True)
    args = ap.parse_args()
    run = Path(args.run_dir)
    meta = json.loads((run / "meta.json").read_text(encoding="utf-8"))
    calls = [json.loads(line) for line in (run / "raw.jsonl").read_text(encoding="utf-8").splitlines()]
    calls = [c for c in calls if c["backend"] == args.backend]
    busy_h = sum(c["latency_ms"] for c in calls) / 1000 / 3600
    window_h = (datetime.fromisoformat(meta["finished_kst"]) - datetime.fromisoformat(meta["started_kst"])).total_seconds() / 3600
    out = {"run": meta["run_id"], "backend": args.backend, "requests": len(calls),
           "busy_seconds": round(busy_h * 3600, 1), "window_seconds": round(window_h * 3600, 1)}
    for name, usd_h in PRICES.items():
        out[name] = {"usd_per_hour": usd_h,
                     "usd_per_request_busy": round(busy_h * usd_h / len(calls), 8),
                     "usd_per_request_window_upper": round(window_h * usd_h / len(calls), 8)}
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
