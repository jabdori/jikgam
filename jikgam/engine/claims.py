"""고객 발화에서 진술을 코드로 뽑는다(docs/engine.md §4 2단계).

날짜, 금액, 횟수, 전화번호, 인증번호, 계좌·예금주를 결정적으로 읽는다. 모델을 쓰지 않는다.
뽑은 값은 모두 고객 진술이다. 시스템 기록과 비교하거나 믿을지 정하는 일은 여기서 하지 않는다(ADR 0003).

한국어 수 표현은 숫자("9,900원", "7월 15일")와 한자어 수사("구천구백 원", "칠월 십오일", "유월 말")를 모두 읽는다.
인증번호처럼 숫자를 하나씩 읽는 표현("공공공일")은 숫자 음절만 이어진 덩어리로 찾는다.
"""

import re
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta

# --- 수 읽기 ----------------------------------------------------------------------------------

_DIGIT = {"공": 0, "영": 0, "일": 1, "이": 2, "삼": 3, "사": 4, "오": 5, "육": 6, "륙": 6, "칠": 7, "팔": 8, "구": 9}
_SMALL_UNIT = {"십": 10, "백": 100, "천": 1000}
_BIG_UNIT = {"만": 10_000, "억": 100_000_000}
_NUM_CHARS = "".join(_DIGIT) + "".join(_SMALL_UNIT) + "".join(_BIG_UNIT)


def korean_number(s: str) -> int | None:
    """'구천구백', '칠만 구천', '6만5천', '9,900', '십오' 같은 수 표현을 정수로. 읽을 수 없으면 None."""
    s = re.sub(r"[\s,]", "", s)
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total = section = 0
    cur: int | None = None
    i = 0
    while i < len(s):
        ch = s[i]
        if ch.isdigit():
            j = i
            while j < len(s) and s[j].isdigit():
                j += 1
            cur = int(s[i:j])
            i = j
            continue
        if ch in _DIGIT:
            if cur is not None:  # '이구천'처럼 단위 없이 숫자가 이어지면 수 표현이 아니다
                return None
            cur = _DIGIT[ch]
        elif ch in _SMALL_UNIT:
            section += (1 if cur is None else cur) * _SMALL_UNIT[ch]
            cur = None
        elif ch in _BIG_UNIT:
            chunk = section + (cur or 0)
            total += (chunk or 1) * _BIG_UNIT[ch]
            section, cur = 0, None
        else:
            return None
        i += 1
    return total + section + (cur or 0)


# --- 결과 형식 --------------------------------------------------------------------------------


@dataclass(frozen=True)
class DateClaim:
    text: str
    year: int
    month: int
    day: int | None = None
    part: str | None = None  # 초, 중순, 말

    @property
    def value(self) -> str:
        """'2026-07-15', '2026-06-말', '2026-03'."""
        if self.day:
            return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"
        if self.part:
            return f"{self.year:04d}-{self.month:02d}-{self.part}"
        return f"{self.year:04d}-{self.month:02d}"


@dataclass(frozen=True)
class AmountClaim:
    text: str
    won: int


@dataclass(frozen=True)
class CountClaim:
    text: str
    n: int
    unit: str  # 번, 회, 건


@dataclass(frozen=True)
class AccountClaim:
    text: str
    bank: str | None = None
    number: str | None = None
    holder_name: str | None = None
    holder_self: bool = False
    holder_relation: str | None = None


@dataclass(frozen=True)
class Claims:
    dates: tuple[DateClaim, ...] = ()
    amounts: tuple[AmountClaim, ...] = ()
    counts: tuple[CountClaim, ...] = ()
    phones: tuple[str, ...] = ()  # 010-0000-0001 형식
    otps: tuple[str, ...] = ()  # 4~6자리. 몇 자리를 쓰는지는 조직의 본인 확인 방식이 정한다
    accounts: tuple[AccountClaim, ...] = ()
    keys: dict[str, str] = field(default_factory=dict)  # 판별 상태에 넣을 진술 키 → 값(고객 진술 표시 전)

    def is_empty(self) -> bool:
        return not (self.dates or self.amounts or self.counts or self.phones or self.otps or self.accounts)


# --- 날짜 -------------------------------------------------------------------------------------

_KNUM = r"\d{1,2}|[일이삼사오육칠팔구십]{1,3}"
_MONTH_WORD = {"유": 6, "시": 10}  # 유월, 시월
_DATE = re.compile(
    rf"(?:(?P<year>20\d\d)\s*년\s*|(?P<rel_year>올해|작년|지난해)\s*)?"
    rf"(?:(?P<month>{_KNUM}|유|시)\s*월|(?P<rel_month>이번\s*달|지난\s*달|지난달|저번\s*달|저번달))"
    rf"(?:\s*(?P<day>{_KNUM})\s*일(?!\s*전)|\s*(?P<part>초|중순|말))?"
)
_WEEKDAYS = "월화수목금토일"
_WEEKDAY = re.compile(rf"(?P<week>지난\s*주|저번\s*주|지난주|저번주|이번\s*주|이번주)?\s*(?P<wd>[{_WEEKDAYS}])요일")
_RELATIVE_DAY = re.compile(r"(?P<word>어제|그제|그저께)|(?P<n>\d{1,2}|[일이삼사오육칠팔구십]{1,3})\s*일\s*전|(?P<word2>이틀|사흘|나흘)\s*전")
_BIRTH_AFTER = re.compile(r"^\s*(?:생|에\s*태어)")


def _month_number(s: str) -> int | None:
    if s in _MONTH_WORD:
        return _MONTH_WORD[s]
    n = korean_number(s)
    return n if n and 1 <= n <= 12 else None


def _dates(text: str, today: date) -> list[tuple[int, DateClaim]]:
    out: list[tuple[int, DateClaim]] = []
    taken: list[tuple[int, int]] = []
    for m in _DATE.finditer(text):
        if _BIRTH_AFTER.match(text[m.end():]):
            continue
        if m["rel_month"]:
            rel = re.sub(r"\s", "", m["rel_month"])
            month, year = (today.month, today.year) if rel == "이번달" else (
                (today.month - 2) % 12 + 1, today.year if today.month > 1 else today.year - 1)
        else:
            month = _month_number(m["month"])
            if month is None:
                continue
            year = today.year if month <= today.month else today.year - 1
        if m["year"]:
            year = int(m["year"])
        elif m["rel_year"] in ("작년", "지난해"):
            year = today.year - 1
        elif m["rel_year"] == "올해":
            year = today.year
        day = korean_number(m["day"]) if m["day"] else None
        if day is not None and not 1 <= day <= monthrange(year, month)[1]:
            day = None
        out.append((m.start(), DateClaim(m.group(0).strip(), year, month, day, m["part"])))
        taken.append(m.span())
    monday = today - timedelta(days=today.weekday())
    for m in _WEEKDAY.finditer(text):
        idx = _WEEKDAYS.index(m["wd"])
        week = re.sub(r"\s", "", m["week"] or "")
        if week in ("지난주", "저번주"):
            d = monday - timedelta(days=7) + timedelta(days=idx)
        elif week == "이번주":
            d = monday + timedelta(days=idx)
        else:  # 요일만 말하면 오늘 또는 가장 가까운 지난 그 요일
            d = today - timedelta(days=(today.weekday() - idx) % 7)
        out.append((m.start(), DateClaim(m.group(0).strip(), d.year, d.month, d.day)))
    for m in _RELATIVE_DAY.finditer(text):
        if any(a <= m.start() < b for a, b in taken):
            continue
        word = m["word"] or m["word2"]
        back = {"어제": 1, "그제": 2, "그저께": 2, "이틀": 2, "사흘": 3, "나흘": 4}.get(word or "")
        if back is None:
            back = korean_number(m["n"] or "")
        if back is None or back > 60:
            continue
        d = today - timedelta(days=back)
        out.append((m.start(), DateClaim(m.group(0).strip(), d.year, d.month, d.day)))
    return sorted(out, key=lambda x: x[0])


# --- 금액·횟수 --------------------------------------------------------------------------------

_AMOUNT = re.compile(
    rf"(?<![0-9{_NUM_CHARS}])((?:[0-9][0-9,]*|[{_NUM_CHARS}])(?:[0-9,{_NUM_CHARS}]|\s(?=[0-9{_NUM_CHARS}]))*)\s*원(?![래하해합한인])"
)
_NATIVE = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10}
# '한 번'은 '한번 봐 주세요'처럼 횟수가 아닌 경우가 많아 뺀다. 숫자 1('1회')은 읽는다.
_COUNT = re.compile(r"(?<![가-힣0-9])(다섯|여섯|일곱|여덟|아홉|두|세|네|열|\d{1,2})\s*(번|회|건)(?!째|호|도|지)")


def _amounts(text: str) -> list[tuple[int, AmountClaim]]:
    """'구천구백 원', '9,900원'. 앞 낱말이 수 표현에 섞여 읽히지 않으면('이 오천 원') 뒤쪽 낱말부터 다시 읽는다."""
    out = []
    for m in _AMOUNT.finditer(text):
        words = m.group(1).split()
        for k in range(len(words)):
            won = korean_number("".join(words[k:]))
            if won is not None:
                break
        if won is not None and won >= 10:
            out.append((m.start(), AmountClaim(m.group(0).strip(), won)))
    return out


def _counts(text: str) -> list[tuple[int, CountClaim]]:
    out = []
    for m in _COUNT.finditer(text):
        n = int(m.group(1)) if m.group(1).isdigit() else _NATIVE[m.group(1)]
        out.append((m.start(), CountClaim(m.group(0).strip(), n, m.group(2))))
    return out


# --- 전화번호·인증번호 ------------------------------------------------------------------------

_SPOKEN_RUN = re.compile(r"[공영일이삼사오육륙칠팔구](?:(?:[\s-]|,\s*|…\s*)?[공영일이삼사오육륙칠팔구])+")
_PARTICLE_AFTER = re.compile(r"^(?:요|에요|예요|고|라고|랑|입|으로|로|은|는|를|을|$|[^가-힣])")
_PHONE = re.compile(r"(?<!\d)(01[016789])[-\s.]?(\d{3,4})[-\s.]?(\d{4})(?!\d)")
# 뒤에 마침표가 와도 문장 끝이면 인증번호로 읽는다("인증번호 000103."). 소수("1234.5")는 읽지 않는다.
_OTP = re.compile(r"(?<![\d,.-])(\d{4,6})(?![\d,-]|\.\d)(?!\s*(?:원|년|월|일|번|회|개|명|시|분|초|만|천|살|세|%))")


_COPULA_AFTER = re.compile(r"^(?:요|에요|예요|고|라고)")


def _spoken_digits(text: str) -> str:
    """숫자를 하나씩 읽은 덩어리("공일공 공공공공 공공공일", "공공공일이요", "칠, 칠, 이")를 아라비아 숫자로 바꾼 문자열.

    4자리 이상인 덩어리만 바꾼다. 일상 낱말("사이", "일이")과 겹치지 않게 하려는 것이다.
    띄어 읽은 묶음은 모두 3자 이상(공일공 공공공공)이거나 모두 한 자(공 공 공 일)여야 한다. '사이 일이' 같은 낱말 연결을 거른다.
    덩어리가 '이'로 끝나고 뒤에 '요·에요·고'가 오면 마지막 '이'가 서술격 조사일 수 있다. 한 자씩 끊어 읽다 마지막만
    '일이'처럼 두 자이거나, 붙여 읽어 '이'를 빼면 4·6자리(인증번호)나 10·11자리(전화번호)가 될 때 조사로 본다.
    """
    def convert(raw: str) -> str | None:
        groups = [g for g in re.split(r"[\s,…-]+", raw) if g]
        digits = "".join(groups)
        if len(digits) < 4:
            return None
        if len(groups) > 1 and not (all(len(g) >= 3 for g in groups) or all(len(g) == 1 for g in groups)):
            return None
        return "".join(str(_DIGIT[c]) for c in digits)

    def repl(m: re.Match) -> str:
        raw = m.group(0)
        if m.start() > 0 and re.match(r"[가-힣]", text[m.start() - 1]):
            return raw
        rest = text[m.end():]
        if raw.endswith("이") and _COPULA_AFTER.match(rest):
            groups = [g for g in re.split(r"[\s,…-]+", raw) if g]
            single_then_pair = len(groups) > 1 and all(len(g) == 1 for g in groups[:-1]) and len(groups[-1]) == 2
            joined_len = len("".join(groups)) - 1
            if single_then_pair or (len(groups[-1]) >= 3 and joined_len in (4, 6, 10, 11)):
                converted = convert(raw[:-1])
                if converted:
                    return converted + "이"
        if not _PARTICLE_AFTER.match(rest):
            return raw
        return convert(raw) or raw
    return _SPOKEN_RUN.sub(repl, text)


def _format_phone(a: str, b: str, c: str) -> str:
    return f"{a}-{b}-{c}"


# --- 계좌 -------------------------------------------------------------------------------------

# 은행 이름 중 '우리', '하나'처럼 일상 낱말과 같은 것이 있어, 뒤에 은행·뱅크·계좌나 계좌번호가 올 때만 은행으로 본다.
_BANK = re.compile(
    r"(카카오\s*뱅크|토스\s*뱅크|케이\s*뱅크|새마을\s*금고|SC\s*제일|KB\s*국민|NH\s*농협|IBK\s*기업|"
    r"국민|신한|우리|하나|농협|기업|카카오|토스|씨티|우체국|수협|부산|대구|광주|전북|경남|제주|산업)"
    r"(?=\s*(?:은행|계좌|\d)|(?<=뱅크)|(?<=금고))(\s*은행)?"
)
# 목록에 없는 은행("별담은행"): '…은행'으로 붙여 말한 이름. 계좌번호가 같은 발화에 있을 때만 쓴다(개발용 시나리오 s08).
_BANK_ANY = re.compile(r"(?<![가-힣])([가-힣]{2,6})은행")
_NOT_BANK = {"어느", "무슨", "다른", "같은", "주거래", "본인", "제", "그", "이", "저", "해당", "시중", "인터넷", "온라인"}
_ACCOUNT_NUMBER = re.compile(r"(?<![\d-])(\d[\d-]{7,20}\d)(?![\d-])")
# 계좌번호 자릿수. 은행 이름이 같은 발화에 있으면 9자리부터 받는다(가상 은행 계좌 444-555-666). 8자리는 날짜(2026-10-12)와
# 겹치므로 받지 않는다.
_ACCOUNT_DIGITS = (10, 16)
_ACCOUNT_DIGITS_WITH_BANK = (9, 16)
_SURNAMES = set("김이박최정강조윤장임한오서신권황안송류전홍고문양손배백허유남심노하곽성차주우구민진지엄채원천방공현함변염여추도소석선설마길연위표명기반왕금옥육인맹제모탁국어은편용예봉경사부")
_HOLDER_LABEL = re.compile(r"(?:예금주|명의자|받는\s*분)\s*(?:는|은|가|이|:)?\s*([가-힣]{2,4}?)(?=이요|요|입니다|이에요|예요|님|씨|이고|고|\s|$|[.,!?])")
_HOLDER_BEFORE = re.compile(r"([가-힣]{2,4}?)\s*(?:명의|이름으로|앞으로)")
_AFTER_NUMBER_PREFIX = re.compile(r"^\s*(?:이고|이요|이에요|예요|요|,)?\s*(?:예금주\s*(?:는|은)?\s*)?")
_HOLDER_AFTER_NUMBER = re.compile(r"^([가-힣]{2,4}?)(?=이요|요|입니다|이에요|예요|님|씨|이고|으로|로|\s|$|[.,!?])")
_HOLDER_SELF = re.compile(r"(?<![가-힣])(?:제|저|본인|내)\s*(?:명의|거|꺼|계좌|이름)")  # '아내 명의'의 '내 명의'는 아니다
_HOLDER_RELATION = re.compile(r"(?<![가-힣])(아내|와이프|남편|엄마|어머니|아빠|아버지|딸|아들|배우자|친구|동생|언니|누나|형|오빠)\s*(?:명의|거|꺼|계좌|이름)")
_ACCOUNT_CONTEXT = re.compile(r"계좌|은행|뱅크|입금|예금주|명의")
_NOT_NAMES = {"남편", "아내", "엄마", "아빠", "본인", "와이프", "배우자", "동생", "언니", "누나", "오빠", "아들", "주민",
              "어머니", "아버지", "고객", "회원", "명의", "계좌", "번호", "이름", "예금주", "제거", "하나", "우리",
              "이요", "이고", "이에요", "입니다"}


def _is_name(s: str) -> bool:
    return 2 <= len(s) <= 4 and s[0] in _SURNAMES and s not in _NOT_NAMES and not re.search(r"은행|뱅크|계좌|명의|번호", s)


_NUMBER_CONTEXT = re.compile(r"계좌|예금주")


def _bank(text: str) -> tuple[str | None, re.Match | None]:
    """은행 이름과 위치. 목록의 은행은 '은행'을 뺀 이름('우리'), 목록에 없는 은행은 말한 그대로('별담은행')."""
    m = _BANK.search(text)
    if m:
        return re.sub(r"\s", "", m.group(1)), m
    for m in _BANK_ANY.finditer(text):
        if m.group(1) not in _NOT_BANK:
            return m.group(0), m
    return None, None


def _account_number(phones_removed: str, with_bank: bool) -> re.Match | None:
    """계좌번호로 볼 수 있는 마지막 숫자 덩어리."""
    lo, hi = _ACCOUNT_DIGITS_WITH_BANK if with_bank else _ACCOUNT_DIGITS
    found = None
    for m in _ACCOUNT_NUMBER.finditer(phones_removed):
        if lo <= len(re.sub(r"-", "", m.group(1))) <= hi and not _PHONE.fullmatch(m.group(1)):
            found = m
    return found


def _accounts(text: str, phones_removed: str) -> list[tuple[int, AccountClaim]]:
    bank, bank_m = _bank(text)
    context = _NUMBER_CONTEXT.search(text) is not None
    if bank_m is not None and bank_m.re is _BANK_ANY and not context and _account_number(phones_removed, True) is None:
        bank, bank_m = None, None  # 목록에 없는 '…은행'은 계좌번호나 '계좌'와 함께 말할 때만 은행으로 본다
    num_m = _account_number(phones_removed, bank is not None or context)
    if not _ACCOUNT_CONTEXT.search(text) and num_m is None:
        return []
    number = num_m.group(1) if num_m else None
    holder = None
    for pat in (_HOLDER_LABEL, _HOLDER_BEFORE):
        hm = pat.search(text)
        if hm and _is_name(hm.group(1)):
            holder = hm.group(1)
            break
    if holder is None and num_m is not None:
        rest = phones_removed[num_m.end():]
        hm = _HOLDER_AFTER_NUMBER.match(rest[_AFTER_NUMBER_PREFIX.match(rest).end():])
        if hm and _is_name(hm.group(1)):
            holder = hm.group(1)
    rel = _HOLDER_RELATION.search(text)
    holder_self = bool(_HOLDER_SELF.search(text)) and holder is None
    if not (bank or number or holder or rel or holder_self):
        return []
    pos = min(x for x in (bank_m.start() if bank_m else None, num_m.start() if num_m else None) if x is not None) \
        if (bank_m or num_m) else 0
    return [(pos, AccountClaim(text, bank, number, holder, holder_self, rel.group(1) if rel else None))]


# --- 진술 키 ----------------------------------------------------------------------------------

_CTX_PAYMENT = re.compile(r"결제|냈|빠져\s*나|찍혔|청구|가입")
_CTX_INCIDENT = re.compile(r"장애|안\s*됐|안\s*되|먹통|끊겼|접속")
_CTX_REFUND = re.compile(r"돌려|환불|남은|나오|받")


def _keys(text: str, dates, amounts, counts, accounts) -> dict[str, str]:
    """판별 상태의 known_facts에 넣을 진술 키. 발화 안의 단서 낱말로 무엇에 대한 값인지 정한다."""
    keys: dict[str, str] = {}
    for _, d in dates:
        if _CTX_INCIDENT.search(text) and not _CTX_PAYMENT.search(text):
            keys["incident_date"] = d.value
        elif _CTX_PAYMENT.search(text):
            keys["payment_date"] = d.value
        else:
            keys["mentioned_date"] = d.value
    for _, a in amounts:
        if _CTX_REFUND.search(text) and not _CTX_PAYMENT.search(text):
            keys["expected_refund"] = f"{a.won}원"
        elif _CTX_PAYMENT.search(text):
            keys["payment_amount"] = f"{a.won}원"
        else:
            keys["mentioned_amount"] = f"{a.won}원"
    for _, c in counts:
        if _CTX_PAYMENT.search(text):
            keys["payment_count"] = f"{c.n}회"
    for _, acc in accounts:
        parts = [p for p in (acc.bank, f"끝자리 {re.sub(r'-', '', acc.number)[-3:]}" if acc.number else None) if p]
        holder = acc.holder_name or ("본인 명의" if acc.holder_self else f"{acc.holder_relation} 명의" if acc.holder_relation else None)
        if holder:
            parts.append(holder)
        if parts:
            keys["refund_account"] = " ".join(parts)
    return keys


def extract(text: str, today: date) -> Claims:
    """발화 하나에서 진술을 뽑는다. 같은 종류가 여러 개면 발화 순서대로 담는다."""
    spoken = _spoken_digits(text)
    phones: list[tuple[int, str]] = [(m.start(), _format_phone(*m.groups())) for m in _PHONE.finditer(spoken)]
    without_phones = _PHONE.sub(" ", spoken)
    dates = _dates(text, today)
    amounts = _amounts(text)
    counts = _counts(text)
    accounts = _accounts(text, without_phones)
    account_numbers = {re.sub(r"-", "", a.number) for _, a in accounts if a.number}
    otps = []
    for m in _OTP.finditer(without_phones):
        if any(m.group(1) in n for n in account_numbers):
            continue
        otps.append((m.start(), m.group(1)))
    return Claims(
        dates=tuple(d for _, d in dates), amounts=tuple(a for _, a in amounts), counts=tuple(c for _, c in counts),
        phones=tuple(p for _, p in phones), otps=tuple(o for _, o in otps), accounts=tuple(a for _, a in accounts),
        keys=_keys(text, dates, amounts, counts, accounts),
    )
