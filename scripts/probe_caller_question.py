"""엔진 전용 질문 caller의 문구 두 개를 개발용 시나리오 발화로 비교한다(Jev, 사례·문구마다 3회).

    uv run python scripts/probe_caller_question.py results/engine/dev/<run_id>/caller_probe.jsonl

V1은 engine-extra-2026.10.14-2의 문구, V2는 법정대리인 선택지에 "회원이 미성년자라는 말이 대화에 있음"을 요건으로 넣은
문구다(engine-extra-2026.10.14-3). 개발용 재생 s22(try1)에서 V1이 성인 자녀의 어머니를 법정대리인으로 보아 본인 확인을
물은 것이 계기다. 사례는 개발용 시나리오 30개(data/scenarios/org_a.jsonl)의 첫 발화와 통화자 교체 발화, 그리고 미성년
회원 본인 발화 하나다. val·test 데이터가 아니며, 이 발화를 보고 문구를 골랐으므로 정확도 주장에는 쓰지 않는다.
원본 응답(확률 분포, 모델, 지연, 비용)을 JSONL로 남긴다.
"""

import asyncio
import json
import sys
from pathlib import Path

from jikgam.decision.registry import make_client

INSTRUCTIONS = "마지막 고객 발화를 한 사람은 처리하려는 계정의 소유자와 어떤 관계인가? 통화 중에 전화를 바꿨으면 바꾼 뒤의 사람을 기준으로 한다."
VARIANTS = {
    "V1": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": {
        "owner": "계정 소유자 본인이거나, 본인이 아니라는 단서가 없다",
        "third_party": "가족이나 지인 등 다른 사람이 계정 소유자를 대신해 요청한다(성인 회원의 가족 포함)",
        "guardian": "미성년자(만 19세 미만) 회원의 부모 등 법정대리인이 그 미성년자의 결제나 구독에 대해 요청한다"}},
    "V2": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": {
        "owner": "계정 소유자 본인이다. 본인이 아니라는 말이 없으면 본인으로 본다",
        "third_party": "가족이나 지인 등 다른 사람이 계정 소유자를 대신해 요청한다. 회원이 미성년자라는 말이 없으면 부모나 배우자도 여기에 해당한다",
        "guardian": "회원이 미성년자(만 19세 미만)라는 말이 대화에 있고, 그 부모 등 법정대리인이 요청한다"}},
}
REFUSE = "해지와 환불은 계정 소유자 본인만 신청할 수 있어 가족의 요청으로는 접수할 수 없습니다."
S29 = "아버지 계정으로 연간 결제한 걸 해지하려는데요, 아버지가 전화를 잘 못 하셔서 제가 대신 전화했어요."
CASES = [
    ("s09 u1", "guardian", [("customer", "저희 딸이 제 카드로 몰래 연간 결제를 했어요. 고등학생인데요, 이거 전부 취소해 주세요.")]),
    ("s22 u1", "third_party", [("customer", "우리 딸이 무슨 체험을 신청했다는데 돈 나가기 전에 해지 좀 해주세요. 딸이 회사에 있어서 제가 전화했어요.")]),
    ("s17 u1", "third_party", [("customer", "아내 계정으로 연간 결제한 거 있는데요, 아내가 바빠서 제가 대신 해지하려고요. 아내 번호랑 생년월일 다 알아요.")]),
    ("s29 u1", "third_party", [("customer", S29)]),
    ("s29 u3", "owner", [("customer", S29), ("agent", REFUSE),
                         ("customer", "아버지 번호는 010-0000-0005예요. 딸인 제가 대신 처리하면 안 될까요?"), ("agent", REFUSE),
                         ("customer", "잠깐만요, 아버지 바꿔 드릴게요. 여보세요, 전화 바꿨습니다. 제가 정민준입니다. 연간 구독 해지하고 남은 돈 돌려받고 싶어요.")]),
    ("s01 u1", "owner", [("customer", "안녕하세요, 지난주 목요일쯤 별담 플러스 결제가 됐는데요, 안 쓸 거라서 취소하고 환불받고 싶어요.")]),
    ("s14 u1", "owner", [("customer", "가족 공유 추가한 거 있잖아요, 그것만 빼고 싶어요. 이번 달 낸 거는 돌려주시는 거죠?")]),
    ("s21 u1", "owner", [("customer", "목요일에 서비스 몇 시간 안 됐잖아요. 그거 보상 같은 거 없어요?")]),
    ("s25 u1", "owner", [("customer", "카드 명세서에 별 뭐시기 구천구백 원이 찍혔는데 나는 가입한 적이 없어요. 돌려주세요.")]),
    ("s20 u1", "owner", [("customer", "해지하려는 건 아니고요, 다음 결제가 언제 되는지만 알고 싶어서요.")]),
    ("minor self", "owner", [("customer", "제가 고등학생인데 엄마 몰래 연간 결제를 했어요. 이거 취소할 수 있어요?")]),
]
REPEATS = 3


def state(turns):
    return {"org": "org_a", "today": "2026-10-12", "identity_verified": False,
            "conversation": [{"speaker": s, "text": t, "final": True} for s, t in turns], "known_facts": {},
            "articles": []}


async def main(out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    c = make_client("jev")
    await c.decide(state([("customer", "안녕하세요")]), {"caller": VARIANTS["V1"]})  # 예열(기록하지 않음)
    correct = {v: 0 for v in VARIANTS}
    cost = 0.0
    with out.open("w", encoding="utf-8", newline="\n") as f:
        for rep in range(REPEATS):  # 회차 → 사례 → 문구 순으로 번갈아 부른다
            for name, gold, turns in CASES:
                for vname, q in VARIANTS.items():
                    r = await c.decide(state(turns), {"caller": q})
                    a = r.answers.get("caller")
                    label = a["choice"] if a else None
                    correct[vname] += label == gold
                    cost += r.cost_usd
                    f.write(json.dumps({"rep": rep, "case": name, "gold": gold, "variant": vname, "label": label,
                                        "answer": a, "model": r.model, "latency_ms": round(r.latency_ms, 1),
                                        "cost_usd": r.cost_usd, "error": r.error}, ensure_ascii=False) + "\n")
    await c.aclose()
    n = REPEATS * len(CASES)
    print(" ".join(f"{v} {k}/{n}" for v, k in correct.items()), f"cost ${cost:.5f}", f"log {out}")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
