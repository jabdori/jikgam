"""학습 전 토큰화 확인: 한국어 글자당 토큰 수, 바이트 조각 분할 여부, train·val 입력의 최대 토큰 길이.

WSL의 Unsloth Studio venv에서 실행한다(transformers, unsloth 필요).

    PYTHONPATH=<repo> python -m jikgam.train.tokens --out results/training/tokenizer.json

입력 길이는 Unsloth가 실제로 만드는 입력(`encode_record`: 시스템 프롬프트 + state + 질문·선택지 + 접미어)으로 잰다.
val은 길이 확인에만 쓰고, test는 열지 않는다.
"""

import argparse
import json
import re
import statistics
from pathlib import Path

from jikgam.train.data import BASE_MODELS as MODELS, ROOT, TRAIN, load_korean, load_typed_decisions, load_val

HANGUL = re.compile(r"[가-힣]")


def korean_stats(tok, texts: list[str]) -> dict:
    """한국어 발화 텍스트의 글자당 토큰 수와, 한 글자가 여러 토큰(UTF-8 바이트 조각)으로 쪼개진 비율."""
    tokens = chars = hangul = split_hangul = fragment_tokens = 0
    for text in texts:
        enc = tok(text, add_special_tokens=False, return_offsets_mapping=True)
        ids, offsets = enc["input_ids"], enc["offset_mapping"]
        tokens += len(ids)
        chars += len(text)
        # 토큰 하나를 따로 디코드했을 때 U+FFFD가 나오면 글자 하나를 다 담지 못한 바이트 조각이다.
        frag = [("�" in tok.decode([i])) for i in ids]
        fragment_tokens += sum(frag)
        covering: dict[int, int] = {}
        for (s, e), is_frag in zip(offsets, frag):
            for p in range(s, max(e, s + 1)):
                covering[p] = covering.get(p, 0) + 1
        for p, ch in enumerate(text):
            if HANGUL.fullmatch(ch):
                hangul += 1
                split_hangul += covering.get(p, 0) > 1
    return {"texts": len(texts), "chars": chars, "hangul_chars": hangul, "tokens": tokens,
            "tokens_per_char": round(tokens / chars, 3), "tokens_per_hangul_char": round(tokens / hangul, 3),
            "byte_fragment_tokens": fragment_tokens, "byte_fragment_token_ratio": round(fragment_tokens / tokens, 4),
            "hangul_chars_split_over_tokens": split_hangul,
            "hangul_split_ratio": round(split_hangul / hangul, 4)}


def record_lengths(tok, rows: list[dict]) -> list[int]:
    """Unsloth `build_dataset`과 같은 경로로 입력 토큰 수를 잰다(잘림 없이)."""
    from unsloth.models._decision_common import _clef_question, _parsed
    from unsloth.models.clef import encode_record

    out = []
    for r in rows:
        questions = {str(k): _clef_question(q) for k, q in _parsed(r["questions"]).items()}
        rec = encode_record(tok, {"state": _parsed(r["state"]), "questions": questions}, max_length=10**7)
        out.append(len(rec.input_ids))
    return out


def summary(xs: list[int]) -> dict:
    xs = sorted(xs)
    return {"n": len(xs), "min": xs[0], "p50": int(statistics.median(xs)),
            "p95": xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))], "max": xs[-1]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="results/training/tokenizer.json")
    ap.add_argument("--samples", type=int, default=5, help="글자당 토큰을 잴 train 사례 수(앞에서부터)")
    args = ap.parse_args()

    from transformers import AutoTokenizer
    from unsloth.models._decision_common import _parsed
    from unsloth.models.clef import render

    toks = {m: AutoTokenizer.from_pretrained(m, revision=rev) for m, rev in MODELS.items()}
    (m0, tok), (m1, tok1) = list(toks.items())
    same_vocab = tok.get_vocab() == tok1.get_vocab()

    train = load_korean(TRAIN)
    val_rows = load_val()

    sample = train[: args.samples]
    utter = [u["text"] for r in sample for u in json.loads(r["state"])["conversation"]]
    rendered = [render(_parsed(r["state"])) for r in sample]
    escaped = sum("\\u" in s for s in rendered)
    state_tokens = [len(tok(s, add_special_tokens=False).input_ids) for s in rendered]
    state_chars = [len(s) for s in rendered]

    td = load_typed_decisions()
    result = {
        "tokenizer": {m: {"revision": rev, "vocab_size": len(t)} for (m, rev), t in zip(MODELS.items(), toks.values())},
        "same_vocab_0.8b_2b": same_vocab,
        "sample_cases": [r["id"] for r in sample],
        "rendered_state_has_unicode_escapes": escaped,
        "korean_utterances": korean_stats(tok, utter),
        "rendered_state": {"chars": state_chars, "tokens": state_tokens,
                           "tokens_per_char": round(sum(state_tokens) / sum(state_chars), 3)},
        "record_tokens": {
            "train_ko": summary(record_lengths(tok, train)),
            "val_ko": summary(record_lengths(tok, val_rows)),
            "typed_decisions_train": summary(record_lengths(tok, td)),
        },
    }
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
