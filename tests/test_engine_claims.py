from datetime import date

import pytest

from jikgam.engine.claims import extract, korean_number

TODAY = date(2026, 10, 12)  # 월요일


@pytest.mark.parametrize("text,value", [
    ("구천구백", 9900), ("9,900", 9900), ("칠만 구천", 79000), ("육만 오천", 65000), ("이만 구천칠백", 29700),
    ("6만5천", 65000), ("만", 10000), ("십오", 15), ("이십오", 25), ("99000", 99000), ("이구천", None), ("가나", None),
])
def test_korean_number(text, value):
    assert korean_number(text) == value


@pytest.mark.parametrize("text,expected", [
    ("아 아니다, 칠월 십오일이었어요", ["2026-07-15"]),
    ("7월 15일에 결제했어요", ["2026-07-15"]),
    ("유월 말쯤이었던 것 같아요", ["2026-06-말"]),
    ("시월 육일에 결제", ["2026-10-06"]),
    ("이번 달 초에 결제됐어요", ["2026-10-초"]),
    ("지난달 이십오일이요", ["2026-09-25"]),
    ("11월 3일이었나", ["2025-11-03"]),  # 기준일보다 뒤의 달은 작년
    ("지난주 목요일쯤 결제가 됐는데요", ["2026-10-08"]),
    ("목요일에 서비스 몇 시간 안 됐잖아요", ["2026-10-08"]),
    ("이번 주 월요일", ["2026-10-12"]),
    ("삼 일 전에 결제했어요", ["2026-10-09"]),
    ("어제 결제했어요", ["2026-10-11"]),
    ("90년 3월 14일생이에요", []),  # 생년월일은 결제 시점이 아니다
    ("오늘 해지하면 얼마예요?", []),
    ("2025년 12월 1일", ["2025-12-01"]),
])
def test_dates(text, expected):
    assert [d.value for d in extract(text, TODAY).dates] == expected


def test_date_keys_follow_context():
    assert extract("지난주 목요일쯤 결제가 됐는데요", TODAY).keys["payment_date"] == "2026-10-08"
    assert extract("목요일에 서비스 몇 시간 안 됐잖아요", TODAY).keys["incident_date"] == "2026-10-08"
    assert extract("칠월 십오일이었어요", TODAY).keys["mentioned_date"] == "2026-07-15"


@pytest.mark.parametrize("text,expected", [
    ("이번 달 낸 구천구백 원은 그냥 날아가요?", [9900]),
    ("9,900원 결제됐어요", [9900]),
    ("할인해서 칠만 구천 원에 연간 가입했는데요, 육만 오천 원 좀 넘게 돌려받는 거 맞죠?", [79000, 65000]),
    ("이만 구천칠백 원이요", [29700]),
    ("5만 원 넘으면요?", [50000]),
    ("이 오천 원은요", [5000]),
    ("회원 가입한 적 없어요", []),
    ("원래 그런 거예요?", []),
])
def test_amounts(text, expected):
    assert [a.won for a in extract(text, TODAY).amounts] == expected


def test_amount_keys():
    assert extract("9월에 구천구백 원이 세 번이나 빠져나갔어요", TODAY).keys["payment_amount"] == "9900원"
    assert extract("육만 오천 원 좀 넘게 돌려받는 거죠?", TODAY).keys["expected_refund"] == "65000원"


@pytest.mark.parametrize("text,expected", [
    ("지난달에 똑같은 게 두 번 결제돼서", [2]),
    ("구천구백 원이 세 번이나 빠져나갔어요", [3]),
    ("2회 결제됐어요", [2]),
    ("한 번도 안 썼어요", []),
    ("한번 확인해 주세요", []),
    ("네 번호는 010-0000-0001이에요", []),
    ("두 번째 결제", []),
])
def test_counts(text, expected):
    assert [c.n for c in extract(text, TODAY).counts] == expected


def test_payment_count_key():
    assert extract("9월에 폰 결제가 두 번 됐더라고요", TODAY).keys["payment_count"] == "2회"


@pytest.mark.parametrize("text,phones,otps", [
    ("네, 휴대폰 번호는 010-0000-0001이에요.", ["010-0000-0001"], []),
    ("01000000016이요", ["010-0000-0016"], []),
    ("010 0000 0010", ["010-0000-0010"], []),
    ("공일공 공공공공 공공일육이요", ["010-0000-0016"], []),
    ("인증번호는 0001이요", [], ["0001"]),
    ("공공공일이요", [], ["0001"]),
    ("인증번호는 공공일육이에요", [], ["0016"]),
    ("공 공 일 공", [], ["0010"]),
    ("공공공이요", [], ["0002"]),
    ("010-0000-0016이고 인증번호는 공공일육이에요", ["010-0000-0016"], ["0016"]),
    ("칠, 칠, 이, 공, 오, 일이요", [], ["772051"]),
    ("사칠이… 공일구요", [], ["472019"]),
    ("인증번호 472019", [], ["472019"]),
    ("인증번호 472019.", [], ["472019"]),  # 문장 끝 마침표(조직 B 재생 b02에서 드러남)
    ("인증번호 공공공일공삼.", [], ["000103"]),
    ("1234.5배요", [], []),  # 소수는 인증번호가 아니다
    ("일, 이 두 개요", [], []),
    ("2026년에 9900원 결제", [], []),
    ("그 사이 일이 생겨서요", [], []),
    ("오늘 이 일 이 사람이", [], []),
])
def test_phone_and_otp(text, phones, otps):
    c = extract(text, TODAY)
    assert list(c.phones) == phones
    assert list(c.otps) == otps


@pytest.mark.parametrize("text,phones,otps", [
    # 개발용 음성 재생(results/voice/20261014-voice-r1·r2)의 STT 확정 자막. TTS가 "공일공에 …"로 읽은 번호
    ("네, 휴대폰 번호는 010에 0000에 0001이에요.", ["010-0000-0001"], []),
    ("0100000에 0110이요.", ["010-0000-0110"], []),  # 앞 두 묶음이 붙어 적힘(s21, 숫자 자체는 오인식)
    ("010-0000-0110요.", ["010-0000-0110"], []),
    ("인증 번호는 0001이요.", [], ["0001"]),
    ("인증 번호는 0016이에요.", [], ["0016"]),
    ("001020", [], ["001020"]),
    # 같은 모양의 다른 표기
    ("010 에 0000 에 0001", ["010-0000-0001"], []),
    ("010의 0000의 0001이요", ["010-0000-0001"], []),
    ("010에0000에0016", ["010-0000-0016"], []),
    ("010에 000에 0001이요", ["010-000-0001"], []),
    ("010-0000에 0016이고 인증번호는 0016이에요", ["010-0000-0016"], ["0016"]),
    ("공일공에 공공공공에 공공공일이에요", ["010-0000-0001"], []),
    ("공일공에 공공공공에 공공일육이고 인증번호는 공공일육이요", ["010-0000-0016"], ["0016"]),
    ("공일공의 공공공공의 공공일공이요", ["010-0000-0010"], []),
])
def test_spoken_phone_separators_from_stt(text, phones, otps):
    c = extract(text, TODAY)
    assert list(c.phones) == phones
    assert list(c.otps) == otps


@pytest.mark.parametrize("text,dates,amounts,phones,otps", [
    # '에'·'의'를 구분으로 읽는 것은 01X로 시작하는 전화번호 모양 안에서만이다. 아래 값은 모두 이 규칙을 더하기 전과 같다
    # (개발용 재생·시나리오 로그·train·val 고객 발화 1,130개에서 바뀐 것은 위 STT 번호 2개뿐이었다)
    ("10월 12일에 9,900원 결제됐어요", ["2026-10-12"], [9900], [], []),
    ("2026년에 9900원 결제", [], [9900], [], []),
    ("1월에 0000원이요", ["2026-01"], [], [], []),
    ("2010에 1234에 5678", [], [], [], ["2010", "1234", "5678"]),  # 01X로 시작하지 않는다(기존 인증번호 규칙)
    ("0105에 1234", [], [], [], ["0105", "1234"]),  # 묶음 수가 모자란다
    ("지난주 목요일에 9,900원 결제됐는데 010에 가입했어요", ["2026-10-08"], [9900], [], []),
    ("공일공에 사이에 일이", [], [], [], []),  # 낱말 연결
    ("일이에 공공공공에 공공공일", [], [], [], ["0001"]),  # 공일X로 시작하지 않는다(기존 규칙대로 읽힌다)
])
def test_spoken_phone_separators_do_not_read_dates_or_amounts(text, dates, amounts, phones, otps):
    c = extract(text, TODAY)
    assert [d.value for d in c.dates] == dates
    assert [a.won for a in c.amounts] == amounts
    assert list(c.phones) == phones
    assert list(c.otps) == otps


def test_account_with_label():
    a = extract("국민은행 123456-01-234567이고 예금주는 김하늘이에요", TODAY).accounts[0]
    assert (a.bank, a.number, a.holder_name, a.holder_self) == ("국민", "123456-01-234567", "김하늘", False)


def test_account_holder_after_number():
    a = extract("신한 110-123-456789 임태윤이요", TODAY).accounts[0]
    assert (a.bank, a.number, a.holder_name) == ("신한", "110-123-456789", "임태윤")


def test_account_relation_and_self():
    a = extract("아내 명의 계좌로 받을게요. 농협 352-1234-5678-91이요", TODAY).accounts[0]
    assert (a.bank, a.holder_relation, a.holder_name, a.holder_self) == ("농협", "아내", None, False)
    b = extract("아 제 거로 할게요. 우리은행 1002-123-456789요", TODAY).accounts[0]
    assert (b.bank, b.holder_self, b.holder_name) == ("우리", True, None)
    assert "refund_account" in extract("아 제 거로 할게요. 우리은행 1002-123-456789요", TODAY).keys


def test_account_spouse_word_is_not_a_name():
    a = extract("남편 명의 계좌인데요 카카오뱅크 3333-01-1234567", TODAY).accounts[0]
    assert (a.bank, a.holder_relation, a.holder_name) == ("카카오뱅크", "남편", None)


@pytest.mark.parametrize("text", ["하나만 돌려주세요", "우리 딸이 결제했어요", "제 이름은 김하늘이에요"])
def test_no_account_without_context(text):
    assert extract(text, TODAY).accounts == ()


def test_account_number_is_not_otp():
    c = extract("기업은행 123-456789-01-012 김하늘", TODAY)
    assert c.otps == ()
    assert c.accounts[0].number == "123-456789-01-012"


@pytest.mark.parametrize("text,expected", [
    # 개발용 시나리오 s08(시뮬레이터 실행 20261014T040727-C)에서 은행·계좌번호를 읽지 못한 발화들
    ("별담은행 111-222-333 임소라로 넣어주세요.", ("별담은행", "111-222-333", "임소라")),
    ("아 제 거로 할게요. 별담은행 444-555-666 임태윤입니다.", ("별담은행", "444-555-666", "임태윤")),
    ("별담은행 444-555-666 임태윤이요.", ("별담은행", "444-555-666", "임태윤")),
    ("별담은행 444-555-666 예금주 임태윤 맞습니다. 처리해 주세요.", ("별담은행", "444-555-666", "임태윤")),
    ("계좌번호는 444-555-666이에요", (None, "444-555-666", None)),  # '계좌'가 있으면 9자리도 계좌번호
])
def test_unlisted_bank_and_nine_digit_account(text, expected):
    a = extract(text, TODAY).accounts[0]
    assert (a.bank, a.number, a.holder_name) == expected


@pytest.mark.parametrize("text", [
    "어느 은행으로 해야 돼요?",           # 은행 이름이 아니다
    "2026-10-12 결제한 거 계좌로 받을래요",  # 날짜(8자리)는 계좌번호가 아니다
    "주문번호 444-555-666 확인해 주세요",     # 은행·계좌 맥락 없는 9자리
])
def test_no_false_bank_or_account(text):
    for a in extract(text, TODAY).accounts:
        assert a.number is None and a.bank is None
