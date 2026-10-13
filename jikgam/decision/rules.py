"""규칙 기준선 판별기.

판별 모델(Jev, Clef, LLM 판단)과 같은 입력(state, questions)을 받아 같은 형태(Jev 응답 형식)로 답한다.
train 분할과 문서(docs/data-format.md, domain/<org>/terms.md)만 보고 만든 값싼 비교 기준이다.

쓰는 것: 대화 정규식(부정·정정 표현 등), state의 구조화된 필드(identity_verified, known_facts의 키·값과
"(고객 진술)" 표시, previous_suggestion), 약관 메타데이터(type, refs), 결정적인 날짜·금액 계산.
쓰지 않는 것: 학습한 모델, 임베딩, 네트워크 호출, LLM.

확률: 규칙은 (라벨, 분기)를 낸다. "라벨/분기"마다 train에서 맞힌 수 k와 쓰인 수 n을 BRANCH_COUNTS에 두고 그
라벨의 확률을 (k + 1) / (n + 2)로 준다. train에서 쓰이지 않은 분기는 같은 질문 유형에서 같은 라벨을 낸 분기들의
합계를 쓰고, 그 라벨을 낸 적이 없으면 근거 없음으로 보아 0.5를 준다. choice의 나머지 확률은 train 정답
분포(BASE_COUNTS)에 비례해 나눈다. 규칙을 train 사례를 읽으며 만들었으므로 train 정답률은 표본 내 수치이고,
이 확률은 val에서 과신으로 나올 수 있다. 보정은 val에서 따로 잰다.
두 표는 tests/test_rules.py가 train으로 다시 세어 일치를 확인한다(`uv run python tests/test_rules.py`로 출력).
"""

import json
import re
from calendar import monthrange
from datetime import date
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

# org_a 약관에서 옮긴 상수: 부칙 제1조(A-21) 경과 조치(2026-06-30 이전 결제), 별표 1(T-01) 요금,
# 제19조 제2항 승인 기준액, 제13조 성년 나이.
ADDENDUM_LAST_MONTH = (2026, 6)
ANNUAL_FEE, PROMO_FEE, MONTHLY_FEE = 99000, 79000, 9900
APPROVAL_LIMIT = 50000
ADULT_AGE = 19

# 질문 유형 → "라벨/분기" → (맞힌 수, 쓰인 수). train에서 센 값이며 손으로 고치지 않는다.
BRANCH_COUNTS: dict[str, dict[str, tuple[int, int]]] = {
    "action": {"answer/other_method_known": (1, 1), "answer/third_party_reject": (2, 2), "answer/verified": (30, 30),
               "ask/mobile_past_month": (2, 2), "ask/other_method_unknown": (1, 1), "ask/unverified": (9, 9),
               "handoff/handoff_reason": (5, 5), "hold/missing_article": (2, 2)},
    "applies": {"false/addendum": (12, 12), "false/exception": (35, 35), "false/general": (29, 29),
                "false/procedure": (15, 15), "true/addendum": (7, 7), "true/exception": (35, 35),
                "true/general": (35, 35), "true/procedure": (38, 38)},
    "condition_changed": {"false/none": (37, 38), "true/marker": (5, 5), "true/pause": (1, 4),
                          "true/request_changed": (9, 9)},
    "intent": {"billing_inquiry/turns": (1, 1), "cancel/turns": (4, 4), "cancel_refund/turns": (30, 30),
               "compensation/turns": (2, 2), "other/turns": (2, 2), "plan_change/turns": (4, 4),
               "refund/turns": (12, 12)},
    "next_info": {"account/mobile_past_month": (5, 5), "identity/unverified": (15, 15),
                  "none/missing_article": (3, 3), "none/other_method_known": (1, 1), "none/verified": (3, 3),
                  "payment_method/other_method_unknown": (1, 1)},
    "sufficient": {"false/missing_article": (3, 3), "false/other_method_unknown": (1, 1),
                   "false/outage_unconfirmed": (1, 1), "false/unverified": (15, 15),
                   "true/mobile_past_month": (3, 3), "true/other_method_known": (1, 1),
                   "true/third_party_reject": (1, 1), "true/verified": (23, 23)},
    "suggestion_valid": {"false/changed": (7, 7), "false/new_fact": (2, 2), "true/identity_step": (1, 1),
                         "true/same": (6, 6)},
}
# choice 질문의 train 정답 분포
BASE_COUNTS: dict[str, dict[str, int]] = {
    "action": {"answer": 33, "ask": 12, "handoff": 5, "hold": 2},
    "intent": {"billing_inquiry": 1, "cancel": 4, "cancel_refund": 30, "compensation": 2, "other": 2,
               "plan_change": 4, "refund": 12},
    "next_info": {"account": 5, "identity": 15, "none": 7, "payment_method": 1},
}


@cache
def article_meta(org: str) -> dict[str, dict[str, Any]]:
    """terms.md의 조항 ID → {type, refs}. 조항 제목 바로 아래 메타데이터 주석만 읽는다."""
    text = (ROOT / "domain" / org / "terms.md").read_text(encoding="utf-8")
    pat = re.compile(r"^## \[(\S+)\][^\n]*\n<!-- type: (\w+); refs:(.*?)-->", re.M)
    return {m[1]: {"type": m[2], "refs": {r.strip() for r in m[3].split(",") if r.strip()}}
            for m in pat.finditer(text)}


# --- 발화 패턴 -------------------------------------------------------------------------------
# 부정한 요청은 먼저 지우고 긍정 패턴을 찾는다("해지하려는 건 아니고요" → 해지 아님).
_CANCEL_NEG = re.compile(r"해지\s*(?:하려는|할려는|하자는)?\s*(?:건|게)\s*아니|해지\s*(?:는|를|도)?\s*(?:말고|안\s*하|안\s*할)"
                         r"|계속\s*(?:볼|쓸|이용)|구독\s*(?:은|는)?\s*(?:유지|계속)|끊기면\s*안|본\s*구독\s*말고")
_CANCEL_POS = re.compile(r"해지|해제|끊어|끊을|탈퇴|취소\s*(?:하|해|돼|되|할)")
_REFUND_NEG = re.compile(r"돌려\s*(?:달라는|받으려는|받자는)\s*(?:건|게)\s*아니|환불\s*(?:은|는)?\s*(?:필요\s*없|안\s*받)|해지만")
_REFUND_POS = re.compile(r"환불|한\s*불|돌려\s*(?:받|주|줘|달|드)|청약|철회|남은\s*(?:돈|거|건|금액)"
                         r"|얼마.{0,8}(?:들어|나와|나오|돌려)|취소\s*(?:하|해|돼|되|할)")
_SPECIAL = [  # 같은 발화에 있으면 해지·환불 표현보다 우선한다
    ("other", re.compile(r"가입\s*(?:은|을)?\s*(?:아직\s*)?안\s*했|가입하기\s*전|가입\s*전")),
    ("compensation", re.compile(r"보상|연장|늘려")),
    ("plan_change", re.compile(r"(?:구독|간|요금제)\s*(?:으로|로)\s*(?:바꾸|바꿔|변경)|요금제|가족\s*(?:공유|거|것)|부가\s*상품")),
    ("billing_inquiry", re.compile(r"다음\s*결제|결제\s*(?:예정|언제)|언제\s*얼마|얼마\s*나가는지")),
]
_LABEL = re.compile(r"(cancel_refund|cancel|refund|plan_change|billing_inquiry|compensation|other)\b")
_CORRECTION = re.compile(r"아\s*아니다|아니\s*아니|(?:이|가)\s*아니라|잘못\s*(?:말|알|봤|들)|헷갈|정정|사실은|생각해\s*보니|깜빡")
_PAUSE = re.compile(r"잠깐만|잠시만")  # 약한 신호: train에서 정정으로 이어진 비율이 낮다
_THIRD = re.compile(r"(?:우리|저희)\s*(?:딸|아들|애|남편|아내|와이프|엄마|아빠)|(?:딸|아들)\s*(?:이름|계정|명의)"
                    r"|대신\s*(?:전화|해|신청)|제가\s*(?:얘|걔|애)?\s*(?:엄마|아빠|어머니|아버지)")
_MINOR = re.compile(r"미성[년연]|열\s*[한두세네다여일]\S*\s*살|고등학생|중학생|만\s*1[0-8]\s*세")
_ADULT = re.compile(r"서른|마흔|쉰|성인|직장\s*다니|만\s*19\s*세가?\s*(?:지났|넘)|미성년자?\s*(?:가|는|이)?\s*아니")
_CONSENT = re.compile(r"하라고\s*했|동의\s*했|허락\s*(?:했|해\s*줬)")
_OUTAGE = re.compile(r"장애|(?:앱|접속|서비스|재생|화면)[^.?!]{0,12}(?:안\s*(?:됐|되더|열리)|먹통|불가|끊겼)")
_DUP = re.compile(r"두\s*번|중복|이중|2\s*회|세\s*건")
_MOBILE = re.compile(r"소액\s*결[제재]|(?:휴대|핸드)?폰\s*(?:소액)?\s*결[제재]|핸드폰|휴대폰")
_RECENT = re.compile(r"지난\s*주|이번\s*주|어제|그제|그저께|며칠\s*전|[일이삼사오육칠]\s*일\s*전|이틀|사흘|나흘|닷새"
                     r"|얼마\s*안\s*됐|하자마자")
_USAGE_Q = re.compile(r"로그인|목록|(?:쓴|본|이용한)\s*걸로")  # 제2조 제5호(이용 이력의 정의)를 묻는 표현
_NEW_FACT = [_OUTAGE, _MINOR, _DUP, re.compile(r"이의\s*제기|카드사에\s*신고")]
_NUM = r"\d{1,2}|[일이삼사오유육칠팔구시십]{1,3}"
_DATE = re.compile(rf"(?:({_NUM})월|(이번|지난|저번)\s*달)\s*(?:(\d{{1,2}}|[일이삼사오육칠팔구십]{{1,3}})\s*일)?")
_BIRTH = re.compile(rf"([공영일이삼사오육칠팔구\d]{{2}}|(?:19|20)\d\d)\s*년\s*(?:({_NUM})\s*월)?\s*생")
_YMD = re.compile(r"(20\d\d)-(\d\d)(?:-(\d\d))?")
_PAIR = re.compile(r"(20\d\d-\d\d-\d\d)\D{1,16}?(\d{4,6})(?!\d)")  # "9900원"처럼 한글이 붙어도 읽는다
_KDIGIT = {"공": 0, "영": 0, "일": 1, "이": 2, "삼": 3, "사": 4, "오": 5, "육": 6, "유": 6, "칠": 7, "팔": 8, "구": 9,
           "시": 10}  # 유월·시월


def _num(s: str) -> int | None:
    """'15', '십오', '이십', '유'(유월), '시'(시월) 같은 수 표현을 정수로 바꾼다."""
    if s.isdigit():
        return int(s)
    total = cur = 0
    for ch in s:
        if ch == "십":
            total, cur = total + (cur or 1) * 10, 0
        elif ch in _KDIGIT:
            cur = _KDIGIT[ch]
        else:
            return None
    return total + cur


def _text_dates(text: str, today: date) -> list[tuple[int, int, int | None]]:
    """발화 속 결제 시점(칠월 십오일, 지난달 이십오일, 3월 초)을 (연, 월, 일|None)으로 읽는다. 생년월일은 뺀다."""
    out = []
    for m in _DATE.finditer(_BIRTH.sub(" ", text)):
        month = _num(m[1]) if m[1] else (today.month if m[2] == "이번" else (today.month - 2) % 12 + 1)
        if not month or month > 12:
            continue
        year = today.year if month <= today.month else today.year - 1
        day = _num(m[3]) if m[3] else None
        out.append((year, month, day if day and day <= monthrange(year, month)[1] else None))
    return out


def _signals(text: str) -> tuple[int, int, str | None]:
    """한 발화의 (해지 +1/-1/0, 환불 +1/-1/0, 특수 의도)."""
    special = next((label for label, pat in _SPECIAL if pat.search(text)), None)
    t_c, t_r = _CANCEL_NEG.sub(" ", text), _REFUND_NEG.sub(" ", text)
    c = 1 if _CANCEL_POS.search(t_c) else -1 if t_c != text else 0
    r = 1 if _REFUND_POS.search(t_r) else -1 if t_r != text else 0
    return c, r, special


def _intent(texts: list[str]) -> str:
    """발화를 순서대로 읽으며 요청 상태를 갱신한다. 부정은 지우고, 해지만 새로 말하면 앞선 환불 요청을 대체한다."""
    cancel = refund = False
    special = None
    for text in texts:
        c, r, s = _signals(text)
        if s:
            special = s
        elif c > 0:
            special = None
        if c > 0 and r == 0 and refund and not cancel:
            refund = False
        cancel = cancel if c == 0 else c > 0
        refund = refund if r == 0 else r > 0
    if special:
        return special
    return {(True, True): "cancel_refund", (True, False): "cancel", (False, True): "refund"}.get((cancel, refund), "unclear")


# --- 상태 해석 -------------------------------------------------------------------------------
def _customer_turns(conv: list[dict[str, Any]]) -> tuple[list[str], str]:
    """(판단에 쓰는 고객 발화, 마지막 상담사 발화 뒤의 고객 발화). 뒤에 확정 발화가 있는 미확정 자막은 버린다."""
    last_agent = max((i for i, u in enumerate(conv) if u["speaker"] == "agent"), default=-1)
    kept = [(i, u["text"]) for i, u in enumerate(conv) if u["speaker"] == "customer"
            and (u.get("final", True) or not any(v.get("final", True) for v in conv[i + 1:]))]
    return [t for _, t in kept], " ".join(t for i, t in kept if i > last_agent)


def _facts_date(facts: dict[str, Any], stated: bool) -> tuple[int, int, int | None] | None:
    """결제 관련 키에서 첫 날짜. stated=True면 고객 진술 값, False면 시스템 값만 본다."""
    for key, value in facts.items():
        s = json.dumps(value, ensure_ascii=False)
        if "pay" in key and ("고객 진술" in s) == stated and (m := _YMD.search(s)):
            y, mo, d = int(m[1]), int(m[2]), int(m[3]) if m[3] else None
            if 1 <= mo <= 12 and (d is None or 1 <= d <= monthrange(y, mo)[1]):
                return y, mo, d
    return None


def _minor(facts_text: str, text: str, today: date) -> bool | None:
    """계정 소유자가 미성년자인가(결제 당시 대신 오늘 기준 나이로 근사). 나이·생년 > 성인 표현 > 미성년 표현. 모르면 None."""
    if age := re.search(r'"(?:owner_)?age"\W{0,4}(?:만\s*)?(\d{1,3})', facts_text):
        return int(age[1]) < ADULT_AGE
    if birth := re.search(r'"birth"\W{0,4}((?:19|20)\d\d)-(\d\d)', facts_text) or _BIRTH.search(text):
        y = int("".join(str(_KDIGIT.get(ch, ch)) for ch in birth[1]))  # "공칠" → 7
        y = y if y > 100 else y + (2000 if y <= today.year % 100 else 1900)
        return today.year - y - (today.month < (_num(birth[2] or "1") or 1)) < ADULT_AGE
    if _ADULT.search(text):
        return False
    return True if _MINOR.search(text) else None


def _refund_estimate(c: SimpleNamespace) -> int | None:
    """제8조·부칙 제1조로 계산한 환불 예정액. 시스템 견적이 있으면 그 값. 계산 입력을 모르면 None."""
    if c.quote is not None:
        return c.quote
    if not c.refund_topic or (c.suspended and not c.duplicate):
        return 0
    if c.plan != "annual" or c.dup_only:
        return c.amount or MONTHLY_FEE
    if not c.pay or not c.pay[2]:
        return None
    start, today = date(*c.pay), c.today
    full = (today.year - start.year) * 12 + today.month - start.month - (today.day < start.day)
    months = full if full > 0 and today.day == start.day else full + 1  # 채우지 못한 기간은 1개월
    base = (c.amount or (PROMO_FEE if c.promo else ANNUAL_FEE)) - months * MONTHLY_FEE
    if base <= 0:
        return 0
    return base - (0 if c.before_addendum else base // 100 * 10)  # 위약금 10%, 10원 미만 버림


def _context(state: dict[str, Any]) -> SimpleNamespace:
    """state를 규칙이 쓰는 사실로 바꾼다. 값이 None이면 아직 확인되지 않은 것이다."""
    facts = state.get("known_facts") or {}
    ft = json.dumps(facts, ensure_ascii=False)
    today = date.fromisoformat(state["today"])
    turns, last = _customer_turns(state.get("conversation") or [])
    text = " ".join(turns)
    c = SimpleNamespace(org=state.get("org", "org_a"), today=today, verified=bool(state.get("identity_verified")),
                        text=text, last=last, missing=False)

    req = str(facts.get("request") or facts.get("inquiry") or "")
    req_intent = (m[1] if (m := _LABEL.match(req)) else _intent([req])) if req else "unclear"
    c.request_intent = None if req_intent == "unclear" else req_intent
    c.intent, c.intent_source = _intent(turns), "turns"
    if c.intent == "unclear":  # 대화에 신호가 없으면 기록된 요청을 쓴다
        c.intent, c.intent_source = req_intent, "request" if c.request_intent else "none"

    # 결제일: 시스템 값 > 마지막 차례의 진술 > 기록된 고객 진술 > 그 전 발화의 진술
    c.pay = (_facts_date(facts, stated=False) if c.verified else None) or next(iter(_text_dates(last, today)[-1:]), None)
    c.pay = c.pay or _facts_date(facts, stated=True) or next(iter(_text_dates(text, today)[-1:]), None)
    c.recent = bool(_RECENT.search(text))
    if c.pay:
        y, mo, d = c.pay
        delta = (today - date(y, mo, d or monthrange(y, mo)[1])).days  # 일자를 모르면 그 달 말일로 본다
        c.within7 = False if delta > 7 else (0 <= delta if d else None)  # 제2조 제7호
        c.before_addendum = (y, mo) <= ADDENDUM_LAST_MONTH
    else:
        c.within7, c.before_addendum = None, (False if c.recent else None)

    system_pay = [s for k, v in facts.items() if "pay" in k and "고객 진술" not in (s := json.dumps(v, ensure_ascii=False))]
    pairs = [p for s in system_pay for p in _PAIR.findall(s)]
    if c.verified and system_pay:  # 조회된 결제 기록이 있으면 그것이 기준이다
        c.duplicate = any(pairs.count(p) > 1 for p in pairs)
    else:
        c.duplicate = bool(_DUP.search(text) or "duplicate" in ft)
    amounts = [int(a) for _, a in pairs] + [int(n) for k, v in facts.items() if re.search("amount|price", k)
                                            for n in re.findall(r"\d{4,6}", str(v).replace(",", ""))]
    c.amount = max(amounts) if amounts else None
    c.quote = int(q[1].replace(",", "")) if (q := re.search(r"refund(?:_quote)?\W{0,4}(\d[\d,]*)", ft)) else None

    usage_vals = [v for k, v in facts.items() if "usage" in k]
    usage_str = re.sub(r"[^\"\[\],]*체험 기간[^\"\[\],]*", "", json.dumps(usage_vals, ensure_ascii=False))  # 제12조 제2항
    c.usage = bool(re.search(r"play|download|재생|다운로드", usage_str)) if usage_vals else None
    status = " ".join(str(v) for k, v in facts.items() if "status" in k)
    c.suspended = (True if re.search(r"suspend|정지", status) else False if status
                   else True if re.search(r"계정\S{0,2}\s*정지", text) else None)
    c.minor, c.consent = _minor(ft, text, today), bool(_CONSENT.search(text))
    caller = str(facts.get("caller", ""))
    c.third_party = True if _THIRD.search(last) else ("본인" not in caller) if caller else (
        not c.verified and bool(_THIRD.search(text)))

    promo = re.search(r"promo\W{0,4}(true|false)", ft)
    c.promo = promo[1] == "true" if promo else None
    plan = re.search(r"\b(annual|monthly)\b", ft)
    c.plan = plan[1] if plan else "annual" if re.search(r"연간|연관|연\s*구독|일\s*년", text) else (
        "monthly" if re.search(r"월\s*구독|한\s*달짜리|월간", text) else None)
    c.outage_claim = bool(_OUTAGE.search(text))
    c.outage_record = any(("incident" in k or "outage" in k) and "고객 진술" not in str(v) for k, v in facts.items())
    c.trial = bool(re.search(r'"trial"\W{0,4}true|체험', ft) or "체험" in text)
    c.addon = bool(re.search(r"addon|family", ft) or re.search(r"가족\s*(?:공유|거|것)|부가\s*상품", text))
    c.mobile = True if re.search("mobile", ft) or _MOBILE.search(text) else False if re.search("card|카드", ft + text) else None
    c.dispute = bool(re.search(r"dispute\W{0,4}true|이의\s*제기\s*진행", ft))
    c.identity_failed = bool(re.search(r"[2-9]\s*회\s*실패", ft))  # 제19조 제1항: 본인 확인 2회 실패
    c.api_failed = bool(re.search(r"조회\s*실패|api_error|timeout|시스템\s*오류", ft))

    # 파생 사실
    side = c.intent == "other" or (c.intent == "plan_change" and not c.addon)
    c.refund_topic = c.intent in ("refund", "cancel_refund") or (side and bool(_REFUND_POS.search(_REFUND_NEG.sub(" ", text))))
    c.processing = c.intent in ("cancel", "refund", "cancel_refund") or (  # 연→월 변경은 연 구독 해지를 거친다
        c.intent == "plan_change" and c.plan == "annual" and not c.addon)
    c.dup_only = c.duplicate and c.intent == "refund"
    c.minor_exception = bool(c.minor) and not c.consent
    c.reject_third = c.third_party and c.minor is not None and not c.minor_exception  # 제15조 제3항 거절 안내
    c.handoff = c.minor_exception or c.dispute or c.identity_failed
    c.refund_estimate = _refund_estimate(c)
    return c


# --- 조항별 적용 요건 (terms.md 본문과 docs/data-format.md의 applies 기준) ------------------------
_REQUEST = ("cancel", "cancel_refund", "refund", "plan_change")
_APPLIES = {
    "A-02": lambda c: bool(_USAGE_Q.search(c.last)),
    "A-04": lambda c: c.intent == "billing_inquiry",  # 해지 효과는 제7조가 직접 규정하므로 결제 예정 문의에만
    "A-05": lambda c: c.refund_topic and not c.dup_only and c.within7 is not False and c.usage is not True,
    "A-06": lambda c: c.refund_topic and not c.dup_only and c.within7 is not False and c.usage is not False,
    "A-07": lambda c: c.intent in ("cancel", "cancel_refund") and c.plan != "annual" and not c.trial,
    "A-08": lambda c: c.plan == "annual" and c.intent in _REQUEST and not c.addon,
    "A-09": lambda c: (c.outage_claim or c.outage_record) and c.intent in ("refund", "cancel_refund", "cancel", "compensation"),
    "A-10": lambda c: c.duplicate,
    "A-11": lambda c: c.plan == "annual" and c.promo is not False and c.intent in _REQUEST,
    "A-12": lambda c: c.trial,
    "A-13": lambda c: c.minor_exception or (c.minor is None and c.third_party),
    "A-14": lambda c: c.refund_topic,
    "A-15": lambda c: c.third_party or (not c.verified and c.intent in _REQUEST + ("compensation",)),
    "A-16": lambda c: c.suspended is not False and c.intent in ("refund", "cancel_refund") and not c.dup_only
                      and not c.minor_exception,
    "A-17": lambda c: c.intent == "plan_change" and not c.addon,
    "A-18": lambda c: c.addon,
    "A-19": lambda c: c.handoff or (not c.reject_third and c.refund_topic
                                    and (c.refund_estimate is None or c.refund_estimate > APPROVAL_LIMIT)),
    "A-20": lambda c: c.processing,
    "A-21": lambda c: c.plan == "annual" and c.intent in ("refund", "cancel_refund", "plan_change") and not c.addon
                      and c.before_addendum is not False and not c.minor_exception,
}


def _applies(c: SimpleNamespace, aid: str) -> bool | None:
    """요건 규칙이 없는 조항(목적, 요금 안내, 별표 등)은 None."""
    rule = _APPLIES.get(aid)
    return None if rule is None else bool(rule(c))


def _missing(c: SimpleNamespace, candidates: set[str]) -> bool:
    """후보와 참조로 이어진 예외·부칙 조항 중 지금 적용되는데 후보에 없는 것이 있는가(검색 누락)."""
    meta = article_meta(c.org)
    near = {r for a in candidates for r in meta.get(a, {}).get("refs", ())}
    near |= {a for a, m in meta.items() if m["refs"] & candidates}
    return any(a not in candidates and meta[a]["type"] in ("exception", "addendum") and _applies(c, a)
               for a in near if a in meta)


# --- 질문별 규칙: (라벨, 분기) ----------------------------------------------------------------
def _condition_changed(c: SimpleNamespace) -> tuple[str, str]:
    if _CORRECTION.search(c.last):
        return "true", "marker"
    if c.request_intent and c.request_intent != c.intent:  # known_facts.request와 지금 요청이 다르다
        return "true", "request_changed"
    if _PAUSE.search(c.last):
        return "true", "pause"
    return "false", "none"


# 정보 상태 → (sufficient, next_info, action). _status가 위에서부터 처음 맞는 상태를 고른다.
_STATUS_COLUMNS = ("sufficient", "next_info", "action")
_STATUS = {
    "other_method_known": ("true", "none", "answer"),  # 가입 전 문의: 환불 방법은 결제 수단에 달렸다(제14조)
    "other_method_unknown": ("false", "payment_method", "ask"),
    "third_party_reject": ("true", "none", "answer"),  # 제15조 제3항 거절 안내
    "unverified": ("false", "identity", "ask"),
    "missing_article": ("false", "none", "hold"),  # 결과를 바꿀 조항이 후보에 없으면 정보 문제가 아니다
    "outage_unconfirmed": ("false", "none", "ask"),  # 장애 기록도 known_facts에 있어야 확인된 것이다
    "usage_unknown": ("false", "usage", "ask"),
    "mobile_past_month": ("true", "account", "ask"),  # 제14조 제3항: 실행에 필요한 값이라 충분성과 무관
    "verified": ("true", "none", "answer"),
}


def _status(c: SimpleNamespace) -> str:
    if c.intent == "other":
        return "other_method_known" if c.mobile is not None else "other_method_unknown"
    if c.reject_third:
        return "third_party_reject"
    if not c.verified:
        return "unverified"
    if c.missing:
        return "missing_article"
    if c.outage_claim and not c.outage_record and c.intent != "plan_change":
        return "outage_unconfirmed"
    if c.refund_topic and not c.dup_only and c.within7 is not False and c.usage is None:
        return "usage_unknown"
    if c.refund_topic and c.mobile and c.pay and c.pay[:2] != (c.today.year, c.today.month):
        return "mobile_past_month"
    return "verified"


def _suggestion_valid(c: SimpleNamespace, suggestion: str) -> tuple[str, str]:
    if not c.verified and re.search(r"인증|본인\s*확인", suggestion):
        return "true", "identity_step"  # 본인 확인 단계 제안은 조건이 바뀌어도 그대로 필요하다
    if _condition_changed(c)[1] in ("marker", "request_changed"):
        return "false", "changed"
    if any(p.search(c.last) for p in _NEW_FACT):
        return "false", "new_fact"
    return "true", "same"


def rule_branches(state: dict[str, Any], questions: dict[str, dict[str, Any]]) -> dict[str, tuple[str, str]]:
    """질문 ID → (규칙 라벨, 분기). 확률을 붙이기 전 값이며 실패 분석과 표 재계산에 쓴다."""
    c = _context(state)
    c.missing = _missing(c, {a["id"] for a in state.get("articles") or []})
    status = _status(c)
    out = {}
    for qid in questions:
        base, _, arg = qid.partition(":")
        if base == "applies":
            v = _applies(c, arg)
            kind = article_meta(c.org).get(arg, {}).get("type", "unknown")
            out[qid] = ("true" if v else "false"), kind if v is not None else f"{kind}:none"
        elif base == "intent":
            out[qid] = c.intent, c.intent_source
        elif base == "condition_changed":
            out[qid] = _condition_changed(c)
        elif base == "suggestion_valid":
            out[qid] = _suggestion_valid(c, str(state.get("previous_suggestion") or ""))
        elif base == "action" and (c.handoff or c.api_failed):  # 제19조 제1항 사유, 업무 API 장애
            out[qid] = ("handoff", "handoff_reason") if c.handoff else ("hold", "api_failed")
        elif base in _STATUS_COLUMNS:
            out[qid] = _STATUS[status][_STATUS_COLUMNS.index(base)], status
        else:
            raise ValueError(f"규칙이 없는 질문 '{qid}' (data/questions.json의 질문 ID만 받는다)")
    return out


def _to_answer(qtype: str, question: dict[str, Any], label: str, branch: str) -> dict[str, Any]:
    base = BASE_COUNTS.get(qtype, {})
    labels = list(question.get("criteria") or ("true", "false"))
    if label not in labels:
        label = max(labels, key=lambda x: base.get(x, 0))
    counts = BRANCH_COUNTS.get(qtype, {})
    same_label = [kn for key, kn in counts.items() if key.startswith(f"{label}/")]
    k, n = counts.get(f"{label}/{branch}") or (sum(k for k, _ in same_label), sum(n for _, n in same_label))
    p = (k + 1) / (n + 2)
    if question["type"] == "noul":
        return {"type": "noul", "noul": p if label == "true" else 1 - p}
    weights = {x: base.get(x, 0) + 1 for x in labels if x != label}
    total = sum(weights.values())
    probs = {x: p if x == label else (1 - p) * weights[x] / total for x in labels} if weights else {label: 1.0}
    return {"type": "choice", "choice": max(probs, key=lambda x: probs[x]), "probabilities": probs}


def decide(state: dict[str, Any], questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """state와 질문 정의를 받아 질문 ID별 Jev 형식 답을 낸다. 순수 함수이며 외부 호출이 없다."""
    raw = rule_branches(state, questions)
    return {qid: _to_answer(qid.partition(":")[0], q, *raw[qid]) for qid, q in questions.items()}
