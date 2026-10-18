"""지식·정책 화면의 자료: AI가 근거로 쓰는 문서와 그 구간(ADR 0021, docs/design/data.md).

이 모듈은 기존 약관(조직 설정의 terms)과 필수 안내 항목(required_notices)을 읽는다. 이 문서의 처리 단계는
실제로 한 것만 완료로 둔다. PDF 업로드와 공개 원본 문서 처리는 같은 폴더의 processing.py가 담당한다.

- 구간: 약관 조항 하나(retrieval.parse_terms). 유형과 참조 조항은 약관 문서의 주석 메타데이터다.
- 고객 표현: v1 문서에는 구간별로 정리한 고객 표현이 없다(요약·메타데이터 단계에서 만든다). 검색용 구어 정규식은
  고객이 하는 말이 아니라서 보여 주지 않는다(독립 리뷰 지적, docs/design/reviews/saas-knowledge.md).
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jikgam.engine.retrieval import Article

from .tenants import Tenant, terms_header, terms_of

TYPE = {"general": "일반 규정", "exception": "예외", "procedure": "절차", "addendum": "부칙", "table": "별표"}


@dataclass
class Doc:
    key: str        # terms(약관), notices(필수 안내 항목)
    name: str
    file: str
    kind: str
    effective: str | None
    segments: int
    status: str


def docs(root: Path, tenant: Tenant) -> list[Doc]:
    head = terms_header(root, tenant)
    arts = terms_of(root, tenant)
    out = [Doc("terms", "이용약관", Path(tenant.raw["terms"]).name, "약관", head.get("시행일"), len(arts), "게시 중")]
    notices = tenant.raw.get("required_notices") or []
    if notices:
        out.append(Doc("notices", "필수 안내 항목", "조직 설정", "필수 안내", None, len(notices), "게시 중"))
    return out


def items(text: str) -> list[str]:
    """조항 본문을 항목별로. "1. …" 줄은 항목, 들여 쓴 "- …" 줄은 앞 항목에 붙인다."""
    out: list[str] = []
    subs = 0
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("<!--"):
            continue
        m = re.match(r"^\d+\.\s+(.*)$", s)
        if m:
            out.append(m.group(1))
            subs = 0
        elif s.startswith("- ") and out:
            out[-1] += (" " if subs == 0 else " · ") + s[2:]
            subs += 1
        elif s.startswith("|"):
            out.append(s)
        else:
            out.append(s)
    return out


def conditions(text: str) -> list[str]:
    """조항 본문을 조건 목록으로: 항 아래 "- " 줄이 있으면 그 줄들을 하나씩, 없는 항은 항 전체를 하나로."""
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("<!--"):
            continue
        if s.startswith("- "):
            out.append(s[2:])
        elif re.match(r"^\d+\.\s+", s):
            body = re.sub(r"^\d+\.\s+", "", s)
            if not body.endswith("연결한다."):  # 아래 목록을 이끄는 문장("다음의 경우 … 연결한다.")은 빼고 목록만 쓴다
                out.append(body)
    return out


def search(arts: dict[str, Article], q: str) -> list[Article]:
    q = q.strip()
    if not q:
        return []
    return [a for a in arts.values() if q in a.title or q in a.text or q == a.id]


def version(head: dict[str, str]) -> tuple[str | None, str | None, str | None]:
    """"2026.07 (문서 개정 r2, 2026-10-12: 해석이 갈리던 문구 명확화)" → (판, 개정 날짜, 개정 내용)."""
    raw = head.get("약관 버전")
    if not raw:
        return None, None, None
    m = re.match(r"^(\S+)(?:\s*\((?:문서 개정\s*\w+,\s*)?(\d{4}-\d{2}-\d{2})(?::\s*(.+?))?\))?$", raw)
    if not m:
        return raw, None, None
    return m.group(1), m.group(2), m.group(3)
