"""노출 제안의 주장 판정. 원문과 주장 글은 저장소 밖 data/v2/local/e2e에만 쓴다."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from jikgam.v2.e2e_logs import read_log
from jikgam.engine.org import ROOT
from jikgam.sim.gemini import GeminiJson
from jikgam.v2 import answer
from jikgam.v2.retry import ask_json

# 답변 판정의 주장 정의와 supported 지시를 그대로 쓰고 정답 판정 지시만 뺀다.
SYSTEM = answer.JUDGE_SYSTEM.replace("두 가지를 판정한다.", "근거 유무를 판정한다.")
SYSTEM = "\n".join(line for line in SYSTEM.splitlines() if "correctness:" not in line)
SCHEMA = {"type": "object", "properties": {"claims": {"type": "array", "items": {
    "type": "object", "properties": {"claim": {"type": "string"}, "supported": {"type": "boolean"},
                                     "reason": {"type": "string"}},
    "required": ["claim", "supported", "reason"]}}}, "required": ["claims"]}
DEFINITION = "generation-input-v2"


def validation_error(data):
    if not isinstance(data, dict) or not isinstance(data.get("claims"), list):
        return "주장 판정 구조 오류: claims 배열 필요"
    for claim in data["claims"]:
        if (not isinstance(claim, dict) or type(claim.get("claim")) is not str
                or type(claim.get("supported")) is not bool or type(claim.get("reason")) is not str):
            return "주장 판정 구조 오류: claim·reason 문자열과 supported 불리언 필요"
    return None


def cache_valid(record):
    return isinstance(record, dict) and not record.get("error") and validation_error(record) is None


def read_cache(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (ValueError, UnicodeError):
        return {}


def exposed_inputs(events, articles=None):
    """실제 생성 입력 전체를 요청 ID로 연결한다. 이전 로그를 조항 글로 복원하지 않는다."""
    exposed = {e["suggestion_id"] for e in events if e["type"] == "suggestion.exposed"}
    conversation = []
    requests, versions = {}, {}
    for e in events:
        if e["type"] == "utterance.end" and e.get("speaker", "customer") == "customer":
            conversation.append(e["text"])
        if e["type"] == "generation.request":
            if e.get("request_id"):
                requests[e["request_id"]] = e
            versions[e.get("version")] = e
        if e["type"] != "suggestion.created" or e["suggestion_id"] not in exposed:
            continue
        if e.get("source") in ("engine_error", "checked_fallback"):
            yield {"suggestion_id": e["suggestion_id"], "excluded": True, "source": e["source"],
                   "input_missing": False}
            continue
        request = (requests.get(e["generation_request_id"]) if e.get("generation_request_id")
                   else versions.get(e.get("basis_version", e.get("version"))))
        value = request.get("input") if request else None
        missing = not isinstance(value, dict)
        evidence = "" if missing else json.dumps(value, ensure_ascii=False, indent=1, default=str)
        feedback = request.get("feedback") if request else None
        if feedback and not missing:
            evidence += "\n\n이전 출력의 문제(고쳐서 다시 쓴다): " + feedback
        yield {"suggestion_id": e["suggestion_id"], "question": "\n".join(conversation),
               "answer": e["text"], "evidence": evidence, "generation_input": value,
               "generation_request_id": request.get("request_id") if request else None,
               "input_missing": missing, "definition": DEFINITION}


def fingerprint(row, model):
    return hashlib.sha256(json.dumps([row, model, SYSTEM, SCHEMA], ensure_ascii=False,
                                    sort_keys=True).encode()).hexdigest()


def local_dir(run_id, root=ROOT):
    if Path(run_id).name != run_id or run_id in (".", ".."):
        raise ValueError("실행 이름에는 경로를 쓸 수 없습니다")
    return root / "data/v2/local/e2e" / run_id


def article_map(summary):
    """기존 호출 호환용. 이전 정의의 조항 복원은 더 이상 사용하지 않는다."""
    return {}


async def run(run_dir: Path, max_cost: float, retry_failed=False, concurrency=8):
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    articles = article_map(summary)
    inputs = {s["session"]: [r for r in exposed_inputs(read_log(run_dir / f"{s['session']}.jsonl"))
                            if not r.get("excluded")]
              for s in summary["sessions"]}
    if any(r.get("input_missing", False) for rows in inputs.values() for r in rows):
        raise ValueError("입력 없음(이전 정의): 생성 입력 전체가 없는 실행은 주장 판정을 하지 않습니다")
    if not any(inputs.values()):
        return 0.0
    dest = local_dir(summary["run_id"])
    dest.mkdir(parents=True, exist_ok=True)
    client = GeminiJson.from_config(answer.JUDGE_MODEL, timeout_s=600, retry_delays_s=())
    spent = 0.0
    reserved = 0.0
    sem = asyncio.Semaphore(concurrency)

    async def one(row, sess):
        nonlocal spent, reserved
        key = fingerprint(row, client.model)
        path = dest / f"{key}.json"
        prior_cost = 0.0
        if path.exists():
            old = read_cache(path)
            if not cache_valid(old):
                if not retry_failed:
                    raise RuntimeError(f"저장된 실패 판정은 --retry-failed로만 다시 돌립니다: {path.name}")
                prior_cost = old.get("usage", {}).get("cost_usd", 0)
            else:
                return
        async with sem:
            # 진행 중 호출의 여유분까지 포함하여 새 호출을 제한한다.
            if spent + reserved + 0.20 > max_cost:
                raise RuntimeError(f"주장 판정 예산 부족: 이번 실행 ${spent:.4f}")
            reserved += 0.20
            try:
                user = (f"## 질문\n{row['question']}\n\n## 답변을 만들 때 넘긴 근거 글\n"
                        f"{row['evidence'] or '(없음)'}\n\n## 판정할 답변\n{row['answer']}")
                reply = await ask_json(client, SYSTEM, user, SCHEMA, validate=validation_error)
                spent += reply.cost_usd
                usage = reply.usage()
                usage["cost_usd"] += prior_cost
                usage["previous_cost_usd"] = prior_cost
                record = {**row, "session": sess["session"], "scenario": sess["scenario"], "hash": key,
                          "model": client.model, "claims": (reply.data or {}).get("claims"),
                          "usage": usage, "error": reply.error}
                path.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
                if reply.error or record["claims"] is None:
                    raise RuntimeError(f"주장 판정 실패: {reply.error or 'claims 누락'}")
            finally:
                reserved -= 0.20
    try:
        for sess in summary["sessions"]:
            # 실패하더라도 진행 중 호출을 회수하고 과금 기록을 남긴 뒤 멈춘다.
            results = await asyncio.gather(*(one(row, sess) for row in inputs[sess["session"]]),
                                           return_exceptions=True)
            print(f"{sess['session']}: 주장 판정 누적 ${spent:.4f}", flush=True)
            failure = next((r for r in results if isinstance(r, BaseException)), None)
            if failure:
                raise failure
    finally:
        await client.aclose()
    return spent


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--max-cost", type=float, required=True)
    ap.add_argument("--retry-failed", action="store_true", help="저장된 실패 판정만 명시적으로 재시도한다")
    ap.add_argument("--concurrency", type=int, default=8, help="동시에 판정할 제안 수(기본 8)")
    args = ap.parse_args()
    if args.concurrency < 1:
        ap.error("동시 호출 수는 1 이상이어야 합니다")
    print(f"이번 호출 비용: ${asyncio.run(run(args.run_dir, args.max_cost, args.retry_failed, args.concurrency)):.4f}")


if __name__ == "__main__":
    main()
