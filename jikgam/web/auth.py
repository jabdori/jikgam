"""웹 콘솔 계정과 권한(ADR 0021). 로컬 시연용 HTTP 기본 인증이며 운영용 인증이 아니다.

계정은 저장소 밖 환경 파일(.env) 또는 환경 변수 JIKGAM_WEB_ACCOUNTS에 둔다. 형식은 계정마다 "이름:비밀번호:역할[:조직]"을
세미콜론으로 잇는다. 역할은 admin(관리 콘솔) 또는 tenant(그 조직의 고객사 콘솔)다.

    JIKGAM_WEB_ACCOUNTS="ops:<비밀번호>:admin;byeoldam:<비밀번호>:tenant:org_a;chaeksol:<비밀번호>:tenant:org_b"

설정이 없으면 generate_accounts가 관리자 하나와 고객사마다 하나씩 임의 비밀번호로 만든다(서버가 터미널에만 보여 준다).
"""

import base64
import binascii
import hmac
import secrets
from dataclasses import dataclass

ENV = "JIKGAM_WEB_ACCOUNTS"
ROLES = ("admin", "tenant")


@dataclass(frozen=True)
class Account:
    name: str
    password: str
    role: str
    org: str | None = None


def parse_accounts(spec: str) -> dict[str, Account]:
    out = {}
    for item in spec.split(";"):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) < 3 or parts[2] not in ROLES or (parts[2] == "tenant") != (len(parts) == 4) or len(parts) > 4:
            raise ValueError(f"{ENV}의 항목 형식이 맞지 않다(이름:비밀번호:admin 또는 이름:비밀번호:tenant:조직)")
        if not parts[0] or not parts[1]:
            raise ValueError(f"{ENV}에 이름이나 비밀번호가 빈 항목이 있다")
        out[parts[0]] = Account(parts[0], parts[1], parts[2], parts[3] if len(parts) == 4 else None)
    return out


def generate_accounts(orgs: list[str]) -> dict[str, Account]:
    accts = [Account("admin", secrets.token_urlsafe(12), "admin")]
    accts += [Account(org.replace("_", "-"), secrets.token_urlsafe(12), "tenant", org) for org in orgs]
    return {a.name: a for a in accts}


def check(header: str | None, accounts: dict[str, Account]) -> Account | None:
    """Authorization 헤더 → 맞는 계정. 없거나 틀리면 None."""
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(header[len("basic "):].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    name, sep, password = raw.partition(":")
    acct = accounts.get(name)
    if not sep or acct is None or not hmac.compare_digest(acct.password.encode(), password.encode()):
        return None
    return acct


def basic_header(name: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{name}:{password}".encode()).decode()
