"""병합 모델을 Studio의 llama.cpp 서빙용 GGUF로 바꾼다. 양자화마다 서빙 폴더를 따로 만든다.

    bash jikgam/train/run_wsl.sh export_gguf --run q35-2b-d-mix --quant bf16

Studio는 `UNSLOTH_SYSTEMONE_MODEL` 폴더의 `gguf/export.json`이 현재 가중치와 맞으면(판별 헤드·설정 파일의 지문)
llama.cpp로 서빙한다. export.json에 양자화가 여럿이면 Q8_0 > F16 > BF16 순으로 하나만 고르므로,
양자화별로 `~/jikgam-train/serve/<실행>-<양자화>/`를 만들고 병합 폴더의 파일을 심볼릭 링크로 걸어 둔다.
가중치를 복사하지 않고, 지문은 링크를 따라가 원본과 같게 계산된다.
"""

import argparse
import json
import os
import time
from pathlib import Path

HOME = Path.home() / "jikgam-train"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--quant", required=True, choices=["bf16", "f16", "q8_0", "q6_k", "q5_k_m", "q4_k_m"])
    args = ap.parse_args()

    import unsloth  # noqa: F401  (unsloth_zoo보다 먼저)
    from unsloth.models.decision_gguf import export_decision_gguf

    runs = Path(os.environ.get("JIKGAM_RUNS", HOME / "runs"))
    merged = (runs / args.run / "merged").resolve()
    serve = HOME / "serve" / f"{args.run}-{args.quant}"
    serve.mkdir(parents=True, exist_ok=True)
    for f in merged.iterdir():
        link = serve / f.name
        if f.is_file() and not link.exists():
            link.symlink_to(f)
    t0 = time.perf_counter()
    data = export_decision_gguf(str(serve), args.quant, output_dir=str(serve / "gguf"), source_folder=str(serve))
    sizes = {p.name: round(p.stat().st_size / 2**30, 2) for p in (serve / "gguf").glob("*.gguf")}
    print(json.dumps({"serve_folder": str(serve), "quantizations": data["quantizations"], "files_gb": sizes,
                      "seconds": round(time.perf_counter() - t0, 1)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
