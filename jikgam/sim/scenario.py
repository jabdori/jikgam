"""시나리오 파일(data/scenarios/<org>.jsonl) 읽기와 고객 AI에 줄 부분 고르기.

고객 AI는 caller, persona, goal, opening, known_to_customer, reveal_rules, mid_call_events만 받는다. expected는 물론
title, tags, customer_id도 주지 않는다(제목과 태그에 기대 결과가 드러난다).

mid_call_events 중 `inject`가 있는 것은 고객 발화가 아니라 업무 API 장애 주입이다. 고객 AI에게 주지 않고 실행기가
`/_admin/faults`로 실행한다. 사건 번호(`s19#1`의 1)는 파일의 원래 순서를 그대로 쓴다.

condition_change가 참인 사건은 "빠른 정정"으로 보낸다(docs/evaluation.md §3.3 보완, 2026-10-14): 고객 AI는 직전 메시지와
사건 메시지를 한 번에 쓰고, 실행기는 직전 메시지를 보낸 뒤 1,000ms 뒤에 엔진 응답을 기다리지 않고 사건 메시지를 보낸다.
고객 AI에게는 condition_change 표시 대신 전달 방식(send_as_followup)만 알려 준다.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from jikgam.engine.org import ROOT

CUSTOMER_FIELDS = ("caller", "persona", "goal", "opening", "known_to_customer", "reveal_rules")
PHONE = re.compile(r"01[016789]-?\d{3,4}-?\d{4}")


def scenario_path(org: str) -> Path:
    return ROOT / "data/scenarios" / f"{org}.jsonl"


def normalize_selection(spec):
    """선택 순서를 보존하고 같은 세션을 두 번 실행하는 중복은 거절한다."""
    if spec == "all":
        return "all"
    values = spec.split(",") if isinstance(spec, str) else spec
    selected = [s.strip() for s in values if s.strip()]
    if not selected:
        raise ValueError("실행할 시나리오가 없습니다")
    if len(set(selected)) != len(selected):
        raise ValueError("시나리오 ID 중복은 허용하지 않습니다")
    return selected


def load_scenarios(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [r["id"] for r in rows]
    if len(set(ids)) != len(ids):
        raise ValueError(f"시나리오 ID가 겹친다: {path}")
    return rows


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def registered_phones(customers_json: Path) -> frozenset[str]:
    """모의 업무 API 고객 데이터(domain/<org>/customers.json)에 등록된 휴대폰 번호."""
    data = json.loads(customers_json.read_text(encoding="utf-8"))
    return frozenset(_norm_phone(c["phone"]) for c in data["customers"] if c.get("phone"))


def _norm_phone(phone: str) -> str:
    digits = phone.replace("-", "")
    return f"{digits[:3]}-{digits[3:-4]}-{digits[-4:]}"


def customer_events(scn: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """고객 AI가 실행할 사건(번호, 사건). 장애 주입은 뺀다."""
    return [(i, e) for i, e in enumerate(scn.get("mid_call_events", [])) if "inject" not in e]


def injections(scn: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """실행기가 실행할 장애 주입(번호, {tool, code, count})."""
    return [(i, e["inject"]) for i, e in enumerate(scn.get("mid_call_events", [])) if "inject" in e]


def followup_events(scn: dict[str, Any]) -> frozenset[int]:
    """빠른 정정으로 보낼 사건 번호(고객이 실행하는 사건 중 condition_change가 참인 것)."""
    return frozenset(i for i, e in customer_events(scn) if e.get("condition_change"))


def event_id(scn: dict[str, Any], index: int) -> str:
    return f"{scn['id']}#{index}"


def marker(scn: dict[str, Any], index: int) -> dict[str, Any]:
    """사건을 실행한 발화에 붙이는 표시(docs/engine.md §2). 엔진은 읽지 않고 로그에만 남긴다."""
    e = scn["mid_call_events"][index]
    return {"condition_change": bool(e.get("condition_change")), "invalidates_prior": bool(e.get("invalidates_prior")),
            "event": event_id(scn, index)}


def sms_codes(scn: dict[str, Any], registered: frozenset[str]) -> dict[str, str]:
    """known_to_customer에 나온 휴대폰 번호 중 등록된 번호 → 그 번호로 오는 모의 인증번호(번호 숫자의 끝 4자리,
    bizapi/README.md).

    실제 상담에서 고객은 문자로 인증번호를 받는다. 모의 업무 API는 문자를 보내지 않으므로 시뮬레이터가 그 문자 내용을
    고객 AI에게 알려 준다. 등록되지 않은 번호(s25)로는 문자가 오지 않는다. 그 휴대폰을 지금 누가 가졌는지는
    known_to_customer와 caller로 고객 AI가 판단한다.
    """
    out: dict[str, str] = {}
    for value in (scn.get("known_to_customer") or {}).values():
        for m in PHONE.findall(str(value)):
            phone = _norm_phone(m)
            if phone in registered:
                out[phone] = phone[-4:]
    return out


def customer_view(scn: dict[str, Any]) -> dict[str, Any]:
    """고객 AI에게 주는 시나리오 부분. 사건에는 번호, after_turn, 내용, 전달 방식만 둔다(조건 변경 표시는 주지 않는다)."""
    view = {k: scn[k] for k in CUSTOMER_FIELDS if k in scn}
    fast = followup_events(scn)
    view["mid_call_events"] = [{"index": i, "after_turn": e.get("after_turn"), "event": e["event"],
                                "send_as_followup": i in fast} for i, e in customer_events(scn)]
    return view
