"""병합 모델을 같은 프로세스에서 답하는 얇은 Jev 호환 서버(POST /v1/systemone). 지연 비교용 서빙 방식이다.

    bash jikgam/train/run_wsl.sh serve_direct --run q35-2b-d-mix --port 8893      # 앞에서 실행, Ctrl+C로 종료

Unsloth Studio의 Decision API(PyTorch)와 같은 방식으로 모델을 올리고(`FastDecisionModel.from_pretrained`,
max_seq_length 16,384, 16비트) 같은 함수(`unsloth.models.decision._clef_decide`, Studio의
core/systemone/clef_runtime.py `_decide`)로 답한다. 차이는 Studio가 요청마다 거치는 경로(설정·키 확인,
스레드 풀, 별도 작업 프로세스와의 파이프)를 빼고 모델을 서버 프로세스에서 직접 부른다는 것뿐이다.
응답 형식은 Studio(`laya_runtime._wire_answer`)와 같다. 요청은 한 번에 하나씩 처리한다.
"""

import argparse
import json
import os
import threading
import hmac
from pathlib import Path

HOME = Path.home() / "jikgam-train"
SERVE_MAX_LEN = 16384


def authorize(authorization: str, key: str) -> None:
    if not key or not hmac.compare_digest(authorization.encode(), f"Bearer {key}".encode()):
        raise PermissionError("invalid key")


def model_info(merged: Path, name: str) -> dict:
    from jikgam.train.serve import weights_sha16

    return {"model": name, "weights_sha256_16": weights_sha16(merged)}


def wire_answer(answer: dict) -> dict:
    if answer["type"] == "noul":
        return {"type": "noul", "noul": float(answer["noul"])}
    if answer["type"] == "choice":
        return {"type": "choice", "choice": answer["choice"], "confidence": float(answer["confidence"]),
                "probabilities": {k: float(v) for k, v in answer["probabilities"].items()}}
    return {"type": "score", "score": float(answer["score"]), "confidence": float(answer["confidence"]),
            "legend": answer["legend"], "probabilities": {k: float(v) for k, v in answer["probabilities"].items()}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--port", type=int, default=8893)
    ap.add_argument("--name", default="direct-local", help="응답의 model 값")
    args = ap.parse_args()

    import unsloth  # noqa: F401
    import uvicorn
    from fastapi import FastAPI, Header, HTTPException
    from unsloth import FastDecisionModel
    from unsloth.models._decision_common import _clef_question
    from unsloth.models.decision import _clef_decide

    runs = Path(os.environ.get("JIKGAM_RUNS", HOME / "runs"))
    run_dir = Path(args.run) if Path(args.run).is_dir() else runs / args.run
    merged = run_dir / "merged" if (run_dir / "merged").is_dir() else run_dir
    info = model_info(merged, args.name)
    model, processor = FastDecisionModel.from_pretrained(str(merged), max_seq_length=SERVE_MAX_LEN,
                                                         load_in_4bit=False, use_gradient_checkpointing=False)
    FastDecisionModel.for_inference(model)
    if model_info(merged, args.name) != info:
        raise SystemExit("모델을 올리는 동안 가중치 파일이 바뀌었습니다")
    tokenizer = getattr(processor, "tokenizer", processor)
    key = (HOME / "secrets" / "studio_api_key").read_text(encoding="utf-8").strip()
    lock = threading.Lock()
    app = FastAPI()

    def check_key(authorization):
        try:
            authorize(authorization, key)
        except PermissionError:
            raise HTTPException(401, "invalid key") from None

    @app.get("/v1/info")
    def get_info(authorization: str = Header(default="")):
        check_key(authorization)
        return info

    @app.post("/v1/systemone")
    def systemone(body: dict, authorization: str = Header(default="")):
        check_key(authorization)
        questions = {str(k): _clef_question(q) for k, q in body["questions"].items()}
        with lock:
            result = _clef_decide(model, tokenizer, body["state"], questions, max_length=SERVE_MAX_LEN)
        if result["truncated"]:
            raise HTTPException(422, "state and questions exceed the context window")
        return {"model": args.name, "answers": {k: wire_answer(result["answers"][k]) for k in questions},
                "usage": {"input_tokens": int(result["input_tokens"]), "output_tokens": 0}}

    print(json.dumps({"port": args.port, "merged": str(merged), "name": args.name}), flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
