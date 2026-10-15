"""한국어 조사 고르기. 화면 문장을 자료 낱말로 이을 때 쓴다."""


def _jong(word: str) -> int | None:
    last = word.rstrip()[-1:] if word else ""
    if "가" <= last <= "힣":
        return (ord(last) - ord("가")) % 28
    return None


def ro(word: str) -> str:
    """조사 (으)로: 받침이 없거나 ㄹ 받침이면 "로"."""
    j = _jong(word)
    return "로" if j is None or j in (0, 8) else "으로"


def eul(word: str) -> str:
    """조사 을/를."""
    j = _jong(word)
    return "를" if j is None or j == 0 else "을"
