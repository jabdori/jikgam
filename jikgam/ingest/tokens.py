"""v2 토큰 세기: Qwen3.5 토크나이저(docs/evaluation-v2.md §2.1, docs/training.md §4).

분할 길이, 창 크기, 검색 예산(2,000토큰)을 모두 이 토크나이저로 센다. 학습 때와 같은 커밋을 쓴다(0.8B와 2B는 어휘가 같다).
"""

from functools import lru_cache

TOKENIZER = ("unsloth/Qwen3.5-2B", "fffbc0d8711cf87df6d1f97938c5e50ef94aadc7")  # jikgam/train/data.py BASE_MODELS


@lru_cache(maxsize=1)
def tokenizer():
    from tokenizers import Tokenizer

    name, revision = TOKENIZER
    return Tokenizer.from_pretrained(name, revision=revision)


def count(text: str) -> int:
    return len(tokenizer().encode(text, add_special_tokens=False).ids)


def counts(texts: list[str]) -> list[int]:
    return [len(e.ids) for e in tokenizer().encode_batch(texts, add_special_tokens=False)]


def cut(text: str, budget: int) -> int:
    """text의 앞에서 budget토큰까지 든 글자 수. 토큰이 글자 중간에서 끝나면 그 토큰이 끝나는 글자까지 센다."""
    if budget <= 0:
        return 0
    enc = tokenizer().encode(text, add_special_tokens=False)
    if len(enc.ids) <= budget:
        return len(text)
    return enc.offsets[budget - 1][1]
