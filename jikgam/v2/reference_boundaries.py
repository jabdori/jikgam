"""참고 경계를 Codex로 표시한다(docs/evaluation-v2.md §2.2, §5 보완).

덩어리마다 Codex 작업 하나를 만든다. 지시문에 맥락 줄과 덩어리 줄을 줄 번호와 함께 넣고, 덩어리의 둘째 줄부터
끝 줄까지에서 새 구간이 시작되는 줄 번호를 JSON으로 받는다. 참고 경계는 `gpt-6-astra`가 표시하고,
`gpt-6.1-sol`이 같은 덩어리를 따로 표시해 두 모델의 일치를 보고한다. 판정과 값 고르기에는 쓰지 않는다.

열람 규칙(§2.6): 개발자는 test 실행 전까지 표시 결과를 열지 않는다. 그래서 이 모듈은 경계 값을 출력하지 않고
형식이 맞는지와 개수만 출력한다. 작업 폴더(지시문에 원문이 들어간다)는 data/v2/local/codex/ 아래에 둔다.

    uv run python -m jikgam.v2.reference_boundaries prepare
    uv run python -m jikgam.v2.reference_boundaries run --model astra      # Orca 터미널에서 돌린다
    uv run python -m jikgam.v2.reference_boundaries collect
"""

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from jikgam.ingest.corpus import ROOT, load_docs
from jikgam.v2 import codex

SEGMENTS = ROOT / "data" / "v2" / "boundary" / "segments.json"
WORK = ROOT / "data" / "v2" / "local" / "codex" / "boundary"
OUT = ROOT / "data" / "v2" / "boundary"
MODELS = {"astra": "gpt-6-astra", "sol": "gpt-6.1-sol"}
ROLE = {"astra": "reference", "sol": "second"}  # 참고 경계, 일치를 보는 두 번째 표시
EFFORT = "high"
PARALLEL = 3

# 경계 정의는 docs/evaluation-v2.md §2.2의 문장 그대로다. 분할 프롬프트에도 같은 문장을 넣는다.
DEFINITION = """- 경계는 "앞 내용을 찾는 사람에게 뒤 내용이 필요 없어지는 곳"이다.
- 번호, 제목, 문단 같은 겉모양이 바뀌어도 내용이 이어지면 경계가 아니다.
- 경계는 새 구간이 시작되는 줄로 표시한다. 줄 중간에서 내용이 바뀌면 다음 줄에 표시한다."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["boundaries"],
          "properties": {"boundaries": {"type": "array", "items": {"type": "integer"}}}}


def prompt(seg: dict, title: str, kind: str, lines: list) -> str:
    ctx = [ln for ln in lines if seg["context_start"] <= ln.no < seg["start"]]
    body = [ln for ln in lines if seg["start"] <= ln.no <= seg["end"]]
    fmt = lambda rows: "\n".join(f"{ln.no}\t{ln.text}" for ln in rows)  # noqa: E731
    ctx_part = f"<맥락>\n{fmt(ctx)}\n</맥락>\n\n" if ctx else ""
    return f"""문서를 검색용 구간으로 나눌 때의 경계를 표시하는 작업이다. 필요한 글은 모두 이 지시 안에 있다. 도구, 파일, 인터넷을 쓰지 않는다. 명령을 실행하지 않는다.

아래 글은 「{title}」({kind}) PDF에서 뽑은 글의 일부다. 각 줄 앞의 숫자는 줄 번호이고 탭으로 나뉜다. PDF 줄바꿈이 단어 중간에 있을 수 있다. 표는 칸을 " | "로 나눠 적었다.

할 일: {seg["start"] + 1}~{seg["end"]}줄에서 새 구간이 시작되는 줄 번호를 모두 고른다.
- <맥락>의 줄은 앞 내용을 알려 주려는 것이고, 고르지 않는다.
- {seg["start"]}줄은 판단 대상이 아니다.
- 경계가 없으면 빈 목록으로 답한다.

{DEFINITION}

마지막 답은 주어진 JSON 스키마로만 쓴다.

{ctx_part}<덩어리>
{fmt(body)}
</덩어리>
"""


def prepare() -> None:
    segs = json.loads(SEGMENTS.read_text(encoding="utf-8"))["segments"]
    docs = load_docs({s["doc"] for s in segs})
    for seg in segs:
        d = docs[seg["doc"]]
        text = prompt(seg, d.title, d.kind, d.lines)
        for key, model in MODELS.items():
            t = WORK / key / seg["id"]
            (t / "work").mkdir(parents=True, exist_ok=True)
            (t / "prompt.md").write_text(text, encoding="utf-8", newline="\n")
            (t / "schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
            (t / "task.json").write_text(json.dumps({"model": model, "effort": EFFORT, "sandbox": "read-only",
                                                     "workdir": "work", "schema": "schema.json", "timeout_s": 1800,
                                                     "isolated": True}, indent=2), encoding="utf-8")
    print(f"작업 {len(segs) * len(MODELS)}개를 만들었다: {WORK}")


def run(key: str) -> None:
    tasks = sorted(p for p in (WORK / key).iterdir() if (p / "task.json").exists())
    todo = [t for t in tasks if not (t / "status.json").exists()
            or json.loads((t / "status.json").read_text(encoding="utf-8"))["exit"] != 0]

    def one(t: Path) -> str:
        st = codex.run(t)
        return f"{t.name} exit={st['exit']} {st['seconds']}s in={st['usage'].get('input_tokens')} out={st['usage'].get('output_tokens')}"

    with ThreadPoolExecutor(PARALLEL) as pool:
        for line in pool.map(one, todo):
            print(line, flush=True)
    print(f"{key}: {len(todo)}개 실행, 전체 {len(tasks)}개")


def collect() -> None:
    """형식을 확인하고 모델별 파일로 모은다. 경계 값은 출력하지 않는다."""
    segs = json.loads(SEGMENTS.read_text(encoding="utf-8"))["segments"]
    for key, model in MODELS.items():
        out, problems, usage = {}, [], {"input_tokens": 0, "output_tokens": 0, "seconds": 0.0}
        for seg in segs:
            t = WORK / key / seg["id"]
            st = json.loads((t / "status.json").read_text(encoding="utf-8"))
            usage["input_tokens"] += st["usage"].get("input_tokens", 0)
            usage["output_tokens"] += st["usage"].get("output_tokens", 0)
            usage["seconds"] += st["seconds"]
            try:
                b = json.loads((t / "last.json").read_text(encoding="utf-8"))["boundaries"]
            except (OSError, ValueError, KeyError):
                problems.append(f"{seg['id']}: 답을 읽지 못함")
                continue
            ok = sorted({x for x in b if isinstance(x, int) and seg["start"] < x <= seg["end"]})
            if len(ok) != len(b):
                problems.append(f"{seg['id']}: 범위 밖이거나 겹친 값 {len(b) - len(ok)}개를 뺐다")
            out[seg["id"]] = ok
        payload = {"model": model, "role": ROLE[key], "effort": EFFORT, "codex_version": st.get("codex_version"),
                   "prompt_sha256": {s["id"]: hashlib.sha256((WORK / key / s["id"] / "prompt.md").read_bytes()).hexdigest()
                                     for s in segs},
                   "usage": usage, "problems": problems, "boundaries": out}
        path = OUT / f"reference-{key}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"{key}: 덩어리 {len(out)}/{len(segs)}, 문제 {len(problems)}건, "
              f"입력 {usage['input_tokens']:,}토큰, 출력 {usage['output_tokens']:,}토큰, {usage['seconds']:.0f}초")
        for p in problems:
            print("  ", p)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["prepare", "run", "collect"])
    ap.add_argument("--model", choices=list(MODELS))
    args = ap.parse_args()
    if args.step == "prepare":
        prepare()
    elif args.step == "run":
        run(args.model)
    else:
        collect()


if __name__ == "__main__":
    main()
