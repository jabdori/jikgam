"""학습 데이터 변환과 구성.

표준 라이브러리만 쓴다. Windows의 평가 환경과 WSL의 학습 환경(Unsloth Studio venv) 양쪽에서 import한다.
typed-decisions를 읽을 때만 `datasets`가 필요하다.

빌드 파일(`data/build/*.jsonl`)을 Unsloth `build_dataset` 행으로 바꿀 때 두 가지를 맞춘다.

- 질문 ID: Unsloth는 질문 ID를 프롬프트에 그대로 넣는다(`ID: applies:A-05`). 평가 실행기는 모든 Jev 형식
  백엔드에 `applies__A-05`로 보내므로(`jikgam.decision.types.wire_id`), 학습 데이터도 같은 키로 바꿔야
  학습과 서빙의 입력이 같다.
- 질문 문구: 평가 실행기와 같은 방식(`jikgam.eval.questions.render`)으로 질문 세트에서 다시 만든다.

state는 JSON 문자열 그대로 둔다. Unsloth가 파싱한 뒤 서빙과 같은 규칙(키 정렬, 공백 없음, ensure_ascii=False)으로
다시 직렬화한다.
"""

import hashlib
import json
import random
from pathlib import Path
from typing import Any

from jikgam.decision.types import wire_id
from jikgam.eval.questions import load_bank, render

ROOT = Path(__file__).resolve().parents[2]
TRAIN = ROOT / "data/build/train.jsonl"
TRAIN_AUG = ROOT / "data/build/train_aug.jsonl"
SPLITS = ROOT / "data/splits.json"
# 엔진 질문 세트(ADR 0011). 학습과 평가에 같은 문구를 쓴다. 처음 학습한 q35-0.8b-a-v1만 data/questions.json(q-2026.10.12).
QUESTIONS = ROOT / "data/question_sets/q-2026.10.14-mix.json"
QUESTION_SETS = {"v1": ROOT / "data/questions.json", "mix": QUESTIONS}

# 기반 모델과 고정 리비전(2026-10-14 확인, 둘 다 Apache-2.0).
BASE_MODELS = {
    "unsloth/Qwen3.5-0.8B": "23c69c53358a07516b5827588b3fdb12ae78fd65",
    "unsloth/Qwen3.5-2B": "fffbc0d8711cf87df6d1f97938c5e50ef94aadc7",
}

TD_REPO = "LocalLLaMA/typed-decisions"
TD_REVISION = "c806f6c69d734f29436f25536138547031a673d6"  # 2026-10-13 수정본, 2026-10-14 확인
SEED = 3407

# 구성별 (한국어 파일 목록, typed-decisions 사용 여부, 한국어 반복 횟수)
# 반복 4회는 (c)를 처음 학습하기 전에 정했다: typed-decisions 1,200건 옆에서 한국어 64건이 묻히지 않게 하되,
# 2 에폭 동안 한 사례를 8번 보는 정도로 둔다. (d)는 사례당 노출을 (c)와 같게 하려고 같은 4회를 쓴다.
# (e)는 진단용(typed-decisions 없이 한국어만, 사례가 늘면 나아지는지).
COMPOSITIONS: dict[str, tuple[tuple[Path, ...], bool, int]] = {
    "a": ((TRAIN,), False, 1),
    "b": ((), True, 0),
    "c": ((TRAIN,), True, 4),
    "d": ((TRAIN, TRAIN_AUG), True, 4),
    "e": ((TRAIN, TRAIN_AUG), False, 1),
}
TRAIN_SPLITS = {"train", "train_aug"}  # train_aug: 보강 사례(a001~a038), 고정 분할 밖에서 train에만 쓴다


def sha16(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def non_train_groups() -> set[str]:
    groups = json.loads(SPLITS.read_text(encoding="utf-8"))["groups"]
    return {g for g, split in groups.items() if split != "train"}


def to_unsloth_row(r: dict[str, Any], bank: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """빌드 파일 한 줄 → Unsloth 행. 질문은 질문 세트로 다시 만들고 키를 요청용 키로 바꾼다."""
    qids = list(json.loads(r["questions"]))
    gold = json.loads(r["gold"])
    questions = render(qids, bank)
    return {
        "id": r["id"],
        "tags": r.get("tags", []),
        "state": r["state"],
        "questions": json.dumps({wire_id(q): v for q, v in questions.items()}, ensure_ascii=False),
        "gold": json.dumps({wire_id(q): gold[q] for q in qids}, ensure_ascii=False),
    }


def load_korean(path: Path, questions_path: Path = QUESTIONS) -> list[dict[str, Any]]:
    """학습용 빌드 파일 → Unsloth 행. train·train_aug가 아닌 행이나 val·test 그룹의 행이 있으면 멈춘다."""
    _, bank = load_bank(questions_path)
    blocked = non_train_groups()
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r.get("split") not in TRAIN_SPLITS or r["group"] in blocked:
            raise SystemExit(f"{path.name}: train이 아닌 행 {r['id']} (split={r.get('split')}, group={r['group']})")
        rows.append(to_unsloth_row(r, bank))
    return rows


def load_val(questions_path: Path = QUESTIONS) -> list[dict[str, Any]]:
    """선별·보정·길이 확인용 val 행. test는 이 모듈 어디서도 읽지 않는다."""
    _, bank = load_bank(questions_path)
    path = ROOT / "data/build/val.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    if any(r.get("split") != "val" for r in rows):
        raise SystemExit("val 파일에 val이 아닌 행이 있다")
    return [to_unsloth_row(r, bank) for r in rows]


def load_typed_decisions(split: str = "train") -> list[dict[str, Any]]:
    """typed-decisions(Apache-2.0, 영어) `all` 구성. test 분할은 학습에 쓰지 않는다."""
    from datasets import load_dataset

    ds = load_dataset(TD_REPO, "all", split=split, revision=TD_REVISION)
    return [{"id": f"td-{split}-{i}", "state": r["state"], "questions": r["questions"], "gold": r["gold"]}
            for i, r in enumerate(ds)]


def compose(name: str, seed: int = SEED, questions_path: Path = QUESTIONS) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """구성 이름 → (섞은 학습 행, 기록용 요약). 한국어 행은 정해진 횟수만큼 반복한다."""
    files, use_td, repeat = COMPOSITIONS[name]
    korean = [row for f in files for row in load_korean(f, questions_path)]
    td = load_typed_decisions() if use_td else []
    rows = td + korean * max(repeat, 1 if korean else 0)
    random.Random(seed).shuffle(rows)

    def decisions(rs: list[dict[str, Any]]) -> int:
        return sum(len(json.loads(r["gold"])) for r in rs)

    summary = {
        "composition": name,
        "korean_files": {str(f.relative_to(ROOT)).replace("\\", "/"): sha16(f) for f in files},
        "korean_cases": len(korean), "korean_decisions": decisions(korean), "korean_repeat": repeat,
        "typed_decisions": {"repo": TD_REPO, "revision": TD_REVISION, "config": "all", "split": "train",
                            "cases": len(td), "decisions": decisions(td)} if use_td else None,
        "rows": len(rows), "decisions": decisions(rows), "seed": seed,
        "questions_file": str(questions_path.relative_to(ROOT)).replace("\\", "/"),
        "questions_version": load_bank(questions_path)[0],
    }
    return rows, summary


def write_jsonl(rows: list[dict[str, Any]], path: Path) -> str:
    """학습에 실제로 넣은 행을 그대로 남기고 sha256 앞 16자리를 돌려준다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return sha16(path)
