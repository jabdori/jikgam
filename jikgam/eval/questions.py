"""질문 세트 교체. 빌드 파일의 질문은 그대로 두고, 평가할 때 같은 질문 ID를 다른 질문 세트로 다시 만든다.

질문 문구는 빌드 파일에도 들어 있으므로 빌드를 다시 하면 고정된 test 파일의 해시가 바뀐다.
그래서 문구 실험은 빌드 파일을 바꾸지 않고 이 함수로 한다. 렌더링 규칙은 scripts/build_dataset.py의 question_def와 같다.
"""

import json
from pathlib import Path
from typing import Any


def load_bank(path: Path) -> tuple[str, dict[str, dict[str, Any]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["version"], data["questions"]


def render(qids: list[str], bank: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = {}
    for qid in qids:
        base, _, arg = qid.partition(":")
        q = json.loads(json.dumps(bank[base]))
        if base == "applies":
            q["instructions"] = q["instructions"].replace("{article_id}", arg)
        out[qid] = q
    return out
