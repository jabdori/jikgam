"""AI 고객: 시나리오의 고객 부분만 받아 한 턴에 고객 메시지를 쓴다(docs/evaluation.md §3.2, §3.3, §4.2).

- 모델: config/decision_models.toml의 이름(기본 gemini-flash = gemini-3.8-flash, 사고 수준 low). 엔진의 생성 모델과
  같은 모델이지만 고객 역할이라 판정에는 쓰지 않는다. 판정 모델은 jikgam/sim/judge.py가 따로 쓴다.
- 입력: scenario.customer_view(caller, persona, goal, opening, known_to_customer, reveal_rules, 고객이 실행할
  mid_call_events와 전달 방식)와 지금까지의 대화, 진행 정보(등록된 번호로 온 인증 문자 포함). expected는 주지 않는다.
- 출력(JSON): {say, followup_say, executed_event: 사건 번호|null, done}.
  - 보통은 say 하나를 보낸다.
  - send_as_followup이 참인 사건(조건 변경)은 빠른 정정으로 보낸다(docs/evaluation.md §3.3 보완): say는 직전 메시지,
    followup_say는 사건 메시지다. 실행기가 say를 보내고 1,000ms 뒤에 엔진 응답을 기다리지 않고 followup_say를 보낸다.
  - 사건 번호와 전달 방식은 이 모듈이 검사한다(check_turn). 맞지 않으면 기록을 남기고 실행기가 쓸 수 있는 꼴로 고친다.
- 첫 메시지(opening)는 실행기가 시나리오 문장 그대로 보낸다. 이 모듈은 두 번째 메시지부터 쓴다.
- seed: 실행기가 (시나리오, 회차)로 정한 값을 generationConfig.seed로 보낸다. 같은 출력을 보장하지 않으며 기록용이다.
프롬프트나 입력 구성을 바꾸면 PROMPT_VERSION을 올린다.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from .gemini import GeminiJson
from .scenario import customer_events, customer_view, followup_events, sms_codes

PROMPT_VERSION = "sim-customer-2026.10.14-3"  # -3: 빠른 정정(followup_say), 등록된 번호에만 인증 문자
DEFAULT_MODEL = "gemini-flash"

SYSTEM_PROMPT = """당신은 구독 서비스 '별담 플러스' 고객센터 채팅 상담에서 고객을 연기한다. 상대는 상담원(자동 응대일 수 있다)이다. 사용자 메시지에 시나리오(scenario), 지금까지의 대화(conversation), 진행 정보(progress)가 JSON으로 주어진다. 다음에 보낼 고객 메시지를 쓴다.

규칙:
1. scenario.persona의 말투와 행동을 따른다. 채팅이므로 한 메시지는 짧게(보통 1~2문장) 자연스러운 구어체 한국어로 쓴다. 상담원처럼 말하거나 상담원의 안내를 요약·평가하지 않는다.
2. scenario.known_to_customer에 있는 것만 안다. 없는 사실(번호, 날짜, 금액, 계좌 등)을 지어내지 않는다. 모르는 것을 물으면 모른다고 한다.
3. scenario.reveal_rules를 지킨다. 규칙이 정한 때가 아니면 그 정보를 먼저 말하지 않는다. 규칙이 정한 행동(묻기, 되묻기, 정해진 반응)은 그 상황이 오면 빠뜨리지 않는다. 상담원이 먼저 안내해서 그 행동이 필요 없어졌으면 규칙의 뜻에 맞게 반응한다. 상담원이 물은 것에는 known_to_customer 안에서 답한다.
4. scenario.mid_call_events는 대화 중에 실행할 사건이다.
   - 각 사건은 한 번만 실행하고, 한 번의 출력에서 사건은 하나만 실행한다.
   - after_turn이 N이면 고객 메시지 N개를 보낸 뒤(N+1번째 메시지부터) 실행할 수 있다. 사건이 상담원의 특정 말이나 행동을 전제하면(예: 계좌가 거절되면, 상담원이 설명하는 도중) 그 일이 일어난 뒤 자연스러운 때 실행한다. 실행할 수 있게 되면 미루지 않는다.
   - 사건을 실행하는 출력에서는 executed_event에 그 사건의 index를 넣는다. 아니면 null로 둔다.
   - send_as_followup이 false인 사건은 say에서 실행하고 followup_say는 빈 문자열로 둔다.
   - send_as_followup이 true인 사건은 메시지 두 개를 연달아 보내는 것으로 실행한다. say에는 사건 직전에 보낼 평소 메시지(상담원의 마지막 말에 대한 답이나 짧은 반응)를 쓰고, followup_say에 사건 메시지를 쓴다. 고객이 첫 메시지를 보내고 1초쯤 뒤 상담원의 답을 보기 전에 생각을 바꾸거나 정정하며 이어 보내는 채팅처럼 자연스럽게 이어져야 한다. 사건 내용은 followup_say에만 넣는다.
   - 사건을 실행하지 않는 출력에서 followup_say는 빈 문자열로 둔다.
   - 실행하지 않은 사건(progress.remaining_events)이 남아 있으면 done을 true로 하지 않는다. 상담원이 상담사 연결로 상담을 넘겼거나 대화가 더 이어질 수 없을 때만 예외다.
5. 본인 확인: 상담원이 인증번호를 요청하면 progress.sms에 적힌 번호가 그 휴대폰에 문자로 온 것으로 본다. progress.sms에 없는 번호로는 문자가 오지 않았다. 그 휴대폰을 지금 가진 사람만 문자를 볼 수 있다(scenario.caller와 known_to_customer로 판단한다). 인증번호는 요청받기 전에 말하지 않는다. 휴대폰 번호를 물으면 known_to_customer의 번호를 말한다.
6. 상담원이 무엇을 확인하거나 이대로 진행할지 물으면 goal과 persona에 맞게 답한다.
7. done: 목표가 이뤄졌다는 안내(예: 접수 완료)를 받았거나, 더 진행할 수 없다고 판단했거나, persona상 대화를 끝낼 때 true로 한다. done이 true인 출력의 메시지는 고객의 마지막 메시지로 상담원에게 전달된다. 마지막에 할 말이 없으면 say를 빈 문자열로 둔다.
설명 없이 지정된 JSON만 출력한다."""

# 음성 판(docs/evaluation-voice.md §3.1): 규칙은 채팅 판과 같고 말투, 빠른 정정의 묘사, 숫자 쓰기만 전화 통화에 맞게 바꾼다.
# 바꾸는 문장은 채팅 판에 정확히 하나씩 있어야 한다(없으면 가져올 때 멈춘다).
VOICE_PROMPT_VERSION = "sim-customer-voice-2026.10.14-1"
VOICE_REPLACEMENTS = (
    ("고객센터 채팅 상담에서 고객을 연기한다.", "고객센터 전화 상담에서 고객을 연기한다. 고객의 말은 음성 합성으로 읽혀 상담원에게 들린다."),
    ("채팅이므로 한 메시지는 짧게(보통 1~2문장) 자연스러운 구어체 한국어로 쓴다.",
     "전화 통화이므로 한 번에 하는 말은 짧게(보통 1~2문장) 소리 내어 말하는 자연스러운 구어체 한국어로 쓴다. 이모지, 괄호, 목록 기호는 쓰지 않는다. 휴대폰 번호와 인증번호는 숫자로 쓴다(예: 010-1234-5678, 0016)."),
    ("고객이 첫 메시지를 보내고 1초쯤 뒤 상담원의 답을 보기 전에 생각을 바꾸거나 정정하며 이어 보내는 채팅처럼 자연스럽게 이어져야 한다.",
     "고객이 첫 말을 마치고 1초쯤 뒤, 상담원이 답하기 전에 생각을 바꾸거나 정정하며 이어 말하는 것처럼 자연스럽게 이어져야 한다."),
    ("다음에 보낼 고객 메시지를 쓴다.",
     "다음에 할 고객의 말을 쓴다. conversation의 상담원 말은 고객이 실제로 들은 것이다. 끝에 [끊김] 표시가 있으면 고객이 끼어들어 그 뒤를 듣지 못했다는 뜻이다."),
)


def _voice_prompt(base: str) -> str:
    for old, new in VOICE_REPLACEMENTS:
        if base.count(old) != 1:
            raise ValueError(f"음성 판으로 바꿀 문장이 채팅 프롬프트에 정확히 하나 있어야 한다: {old}")
        base = base.replace(old, new)
    return base


VOICE_SYSTEM_PROMPT = _voice_prompt(SYSTEM_PROMPT)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "say": {"type": "string"},
        "followup_say": {"type": "string"},
        "executed_event": {"type": ["integer", "null"]},
        "done": {"type": "boolean"},
    },
    "required": ["say", "followup_say", "executed_event", "done"],
}


@dataclass
class CustomerTurn:
    """실행기가 보낼 메시지. followup_say가 있으면 say 다음에 빠른 정정으로 보내고 표시는 followup_say에 붙인다.
    followup_say가 없으면 표시(사건이 있으면)는 say에 붙인다."""

    say: str
    executed_event: int | None
    done: bool
    usage: dict[str, Any] = field(default_factory=dict)  # 모델, 지연, 토큰, 비용, 오류
    rejected_event: dict[str, Any] | None = None  # 받아들이지 않은 사건 번호와 이유
    error: str | None = None
    followup_say: str | None = None
    notes: list[str] = field(default_factory=list)  # 전달 방식을 고친 기록(빠른 정정 미적용 등)
    fast_missing: int | None = None  # 빠른 정정이어야 할 사건을 한 메시지로 실행함(지시 위반)


class Customer(Protocol):
    model: str
    prompt_version: str

    async def next_turn(self, conversation: list[dict[str, str]], turn_no: int, executed: set[int]) -> CustomerTurn:
        """conversation: [{"speaker": "customer"|"agent", "text": ...}]. turn_no: 지금 쓸 고객 메시지의 번호(1부터)."""
        ...

    async def aclose(self) -> None: ...


def user_message(scn: dict[str, Any], conversation: list[dict[str, str]], turn_no: int, executed: set[int],
                 registered: frozenset[str]) -> str:
    remaining = [i for i, _ in customer_events(scn) if i not in executed]
    payload = {
        "scenario": customer_view(scn),
        "conversation": [{"speaker": "고객" if t["speaker"] == "customer" else "상담원", "text": t["text"]}
                         for t in conversation],
        "progress": {"next_message_no": turn_no, "executed_events": sorted(executed), "remaining_events": remaining,
                     "sms": {phone: f"인증번호 {code}" for phone, code in sms_codes(scn, registered).items()}},
    }
    return "```json\n" + json.dumps(payload, ensure_ascii=False, indent=1) + "\n```"


def check_event(scn: dict[str, Any], index: Any, executed: set[int]) -> tuple[int | None, dict[str, Any] | None]:
    """고객 AI가 실행했다고 한 사건 번호를 검사한다. (받아들인 번호, 거절 기록)."""
    if index is None:
        return None, None
    allowed = {i for i, _ in customer_events(scn)}
    if not isinstance(index, int) or isinstance(index, bool) or index not in allowed:
        return None, {"index": index, "reason": "고객이 실행할 사건 번호가 아니다"}
    if index in executed:
        return None, {"index": index, "reason": "이미 실행한 사건이다"}
    return index, None


def check_turn(scn: dict[str, Any], d: dict[str, Any], executed: set[int], usage: dict[str, Any]) -> CustomerTurn:
    """모델 출력(JSON)을 검사해 실행기가 보낼 꼴로 만든다."""
    say, followup, done = d.get("say"), d.get("followup_say", ""), d.get("done")
    if not isinstance(say, str) or not isinstance(followup, str) or not isinstance(done, bool):
        return CustomerTurn("", None, False, usage, error=f"출력 형식 오류: {json.dumps(d, ensure_ascii=False)[:200]}")
    say, followup = say.strip(), followup.strip()
    index, rejected = check_event(scn, d.get("executed_event"), executed)
    turn = CustomerTurn(say, index, done, usage, rejected)
    fast = index is not None and index in followup_events(scn)
    if fast and followup and say:
        turn.followup_say = followup
    elif fast:  # 메시지가 하나뿐이면 그 메시지로 사건을 실행한 것으로 보내고, 빠른 정정 미적용을 기록한다
        turn.say = say or followup
        turn.fast_missing = index
        turn.notes.append(f"사건 #{index}은 빠른 정정이어야 하는데 메시지가 하나뿐이다")
    elif followup:  # 빠른 정정이 아닌데 두 번째 메시지가 있으면 한 메시지로 이어 보낸다
        turn.say = f"{say} {followup}".strip()
        turn.notes.append("빠른 정정 사건이 아닌데 followup_say가 있어 say에 이어 붙였다")
    return turn


class GeminiCustomer:
    def __init__(self, scn: dict[str, Any], client: GeminiJson, registered: frozenset[str], seed: int | None = None,
                 channel: str = "chat"):
        """channel="voice"면 음성 판 프롬프트(VOICE_SYSTEM_PROMPT)를 쓴다."""
        self.scn = scn
        self.client = client
        self.registered = registered
        self.seed = seed
        self.model = client.model
        self.channel = channel
        self.system = VOICE_SYSTEM_PROMPT if channel == "voice" else SYSTEM_PROMPT
        self.prompt_version = VOICE_PROMPT_VERSION if channel == "voice" else PROMPT_VERSION

    async def next_turn(self, conversation: list[dict[str, str]], turn_no: int, executed: set[int]) -> CustomerTurn:
        reply = await self.client.ask(self.system, user_message(self.scn, conversation, turn_no, executed,
                                                                  self.registered), RESPONSE_SCHEMA, seed=self.seed)
        usage = reply.usage()
        if reply.data is None:
            return CustomerTurn("", None, False, usage, error=reply.error or "응답 없음")
        return check_turn(self.scn, reply.data, executed, usage)

    async def aclose(self) -> None:
        await self.client.aclose()


def make_customer(scn: dict[str, Any], registered: frozenset[str], model: str = DEFAULT_MODEL,
                  seed: int | None = None, channel: str = "chat") -> GeminiCustomer:
    return GeminiCustomer(scn, GeminiJson.from_config(model), registered, seed, channel)
