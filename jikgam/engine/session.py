"""세션 진행: 한 턴의 처리 순서, 대화 버전, 작업 취소와 결과 폐기. docs/engine.md §3·§4.

고객 확정 발화가 들어오면:
 1. 버전을 올리고(version.bumped) 이전 버전의 진행 작업을 취소한다.
 2. 발화에서 진술을 뽑는다(코드). 판별 상태의 known_facts에는 넣지 않는다.
 3. 검색과 구조 확장.
 4. 판별 1회(intent, condition_changed, applies:*, sufficient, next_info, action, suggestion_valid, customer_agreed).
 5. 근거 선택(applies 확률 ≥ 임계값). 이전 제안 무효화.
 6. evidence.updated.
 7. 정책. 도구 결과가 시스템 사실을 바꾸면 버전을 올리고 3번부터 다시 한다.
 8. 생성. 끝났을 때 버전이 바뀌었으면 버린다.
 9. 제안 생성(suggestion.created)과 노출(agent.say, suggestion.exposed).

한 버전의 3~9단계는 asyncio 작업 하나(파이프라인)로 돈다. 새 고객 발화가 오면 그 작업을 취소하고(task.cancelled),
취소되지 않고 끝난 단계의 결과가 지난 버전이면 쓰지 않는다(result.discarded). 서버 상태를 바꾸는 호출 묶음(쓰기 API,
본인 확인)은 shield로 감싸 취소하지 않는다. 묶음이 끝났을 때 그것을 시작한 파이프라인이 이미 없으면, 그 결과로 버전을
올리고 최신 발화로 다시 진행한다.

응답의 근거 조항(cited_articles)은 그 응답을 만들 때 엔진이 실제로 쓴 근거 집합이다: 정책 규칙의 조항, 상담 상태로 붙인
절차 조항, 판별이 고른 조항, 업무 API가 돌려준 근거 조항. 생성 입력에 이 조항을 모두 넣는다. 생성기가 문장의 근거로 든
조항은 generator_cited로 따로 남긴다(docs/engine.md §12).

이전 제안 무효화 규칙(5단계): suggestion_valid가 거짓이거나 condition_changed가 참이면 무효화한다(docs/engine.md §4).
여기에 코드 규칙 하나를 더한다: 이번 판단의 intent가 제안을 만들 때의 intent와 다르면 무효화한다. 요청이 바뀌면 정책
판단이 바뀌므로 그 제안은 맞지 않는다(docs/data-format.md suggestion_valid 기준).

고객 동의(8단계의 쓰기 조건)는 무효화와 따로 본다. 마지막으로 노출한 제안에 접수 내용이 있고, condition_changed가
참이 아니고, customer_agreed가 임계값 이상이고 customer_declined가 임계값 미만이면 동의 후보다. 정책은 지금 만든 접수
내용의 서명(쓰기 인자와 금액)이 노출한 것과 같을 때만 쓴다. "네, 그래도 해지해 주세요"처럼 동의하면서 요청 라벨이
cancel_refund에서 cancel로 바뀌어 제안이 무효화되는 경우가 있어서다(개발용 재생 s20에서 확인). customer_declined가
임계값 이상이면 같은 접수 내용을 이번 발화에서 다시 묻지 않는다(정책 규칙 12).

통화자 관계(caller)는 첫 판단마다 묻고 코드 규칙으로 갱신한다(_update_caller). 제3자·법정대리인으로 바꾸는 것은
caller_threshold, 제3자로 본 뒤 본인으로 돌아가는 것은 더 높은 owner_return_threshold가 필요하다. 잘못 본인으로
돌아가면 제3자에게 본인 확인을 묻게 되고, 잘못 제3자로 보면 본인에게 거절 안내를 하는 데 그치기 때문이다. 통화자가
바뀌면 이전 제안을 무효화한다(s29: 딸이 거절 안내를 받은 뒤 본인이 전화를 이어받음).

세션 시작 예열(start): 판별 백엔드에 측정 데이터가 아닌 고정 요청을 한 번 보내 연결을 미리 맺는다. warmup 이벤트로
지연을 남기고 decision.* 이벤트로는 남기지 않는다. 실행기는 세션을 연 직후 start를 부른다.

빠른 정정(앞 턴 진행 중 새 발화, docs/evaluation.md §3.3 보완): 버전은 발화가 도착할 때 바로 올리고 진행 중인 작업을
취소한다(예열을 기다리는 중이어도). 예열 뒤에는 가장 최근 발화만 파이프라인을 돌리고 앞 발화는 양보한다(_drive). 앞 턴이
시작한 쓰기와 본인 확인은 취소하지 않으며, 새 발화의 파이프라인은 정책 단계 전에 그 묶음이 끝나기를 기다린다(writes.wait).
묶음이 끝나면 결과로 버전이 올라 새 파이프라인이 취소되고 최신 발화로 다시 진행한다. 그래서 이미 접수 중인 내용을 다시
안내하지 않고, 진행 중이던 본인 확인 결과를 잃지 않는다. 답하지 못한 앞 발화의 인증번호로 본인 확인을 시작하기 전이었으면
그 인증번호를 새 발화로 넘긴다(_carry_otp, docs/engine.md §13).

업무 API 장애(재시도 예산 소진)는 세션이 끝날 때까지 기억한다. 같은 세션에서 그 도구를 다시 부르지 않고 보류한다
(api.md "재시도 예산 내 1회 재시도 후 접수 보류 안내", 시나리오 s19 "2회를 넘는 quote_refund 호출" 금지).
"""

import asyncio
import copy
import hashlib
import json
import traceback
from pathlib import Path
from typing import Any

from jikgam.decision import llm
from jikgam.decision.registry import make_client
from jikgam.decision.types import DecisionClient, DecisionResult, label_and_prob
from jikgam.eval.questions import load_bank, render

from . import events as ev
from . import generate as gen
from . import policy
from .bizapi import WRITE_TOOLS, BizApiClient, ToolResult, mask_args
from .claims import extract
from .events import EventLog, now_ms, parse_utterance
from .org import OrgConfig
from .retrieval import ArticleIndex, RetrievalContext, parse_terms
from .state import GUARDIAN, OWNER, THIRD_PARTY, Evidence, SessionState, Suggestion, quote_summary

MAX_PASSES = 6  # 한 턴에서 도구 결과로 3단계부터 다시 하는 최대 횟수(무한 반복 방지)
DECISION_RETRYABLE = ("HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504", "ConnectError", "ReadTimeout",
                      "ConnectTimeout", "RemoteProtocolError", "ReadError")
CHANNEL = "chat"  # 기본 채널
# 엔진이 응답을 낸 시각(agent.say)이 곧 노출인 채널. 엔진이 suggestion.exposed를 남긴다(채팅 전송, 콘솔 표시 시작).
# 그 밖의 채널(음성)은 노출 시각을 채널 계층이 알므로(TTS 재생 시작) 그 계층이 남긴다(docs/evaluation.md §3.3).
ENGINE_EXPOSED_CHANNELS = frozenset({"chat"})
# 서버 상태를 바꾸는 호출. 쓰기와 같이 취소하지 않고 끝까지 보낸 뒤 결과를 반영한다(본인 확인은 시도 횟수와 확인 상태를 바꾼다)
SHIELDED_CALLS = frozenset({"verify_identity"})
CONVERSATION_FOR_GENERATION = 8  # 생성 입력에 넣는 최근 대화 수
WARMUP_TEXT = "안녕하세요"  # 예열 요청의 고정 발화(측정 데이터가 아니다)


PREJUDGE_MAX_IN_FLIGHT = 2  # 정책 D: 동시에 진행하는 선판단 호출 수 상한


def _decision_key(state: dict[str, Any], questions: dict[str, Any]) -> str:
    """판별 입력(상태와 질문)이 같은지 보는 키."""
    return hashlib.sha256(json.dumps({"state": state, "questions": questions}, ensure_ascii=False,
                                     sort_keys=True).encode("utf-8")).hexdigest()


class Session:
    def __init__(self, org: OrgConfig, session_id: str, log_dir: Path, decision_backend: str | None = None, *,
                 decision_client: DecisionClient | None = None, generator: gen.Generator | None = None,
                 bizapi: BizApiClient | None = None, channel: str = CHANNEL):
        """decision_client, generator, bizapi는 테스트에서 가짜를 넣을 때만 쓴다. 기본은 조직 설정으로 만든다.

        channel은 응답을 내보내는 채널 이름이다(session.start에 남긴다). ENGINE_EXPOSED_CHANNELS(채팅)면 엔진이 응답을 낼 때
        suggestion.exposed를 그 채널로 남기고, 음성처럼 노출 시각을 채널 계층이 아는 채널이면 남기지 않는다(음성 계층이
        TTS 재생 시작에 남긴다). 엔진의 판단·도구·생성·제안 처리는 채널과 관계없이 같다.
        """
        self.org = org
        self.session_id = session_id
        self.channel = channel
        self.backend = decision_backend or org.decision_backend
        self.log = EventLog(Path(log_dir) / f"{session_id}.jsonl", session_id)
        self.state = SessionState(org.org, org.today)
        self.articles = parse_terms(org.terms)
        self.index = ArticleIndex(dict(self.articles), org.retrieval)
        # v2 법령·고시 묶음: 구간을 종류 "law"인 조항으로 사전에 더한다. 약관 색인(위)은 약관만 본다
        self.law = None
        if org.law is not None:
            from jikgam.engine import law as law_bundle

            self.law = law_bundle.bundle(org.law)
            self.articles.update(self.law.articles)
        base_version, base = load_bank(org.question_set)
        extra_version, extra = load_bank(org.extra_question_set)
        if set(base) & set(extra):
            raise ValueError(f"추가 질문 세트의 ID가 기본 질문 세트와 겹친다: {sorted(set(base) & set(extra))}")
        self.bank = {**base, **extra}
        if self.law is not None and org.law.question_set is not None:
            # 법령 구간의 판별 질문(law_applies). 법령 묶음을 켠 세션만 더한다
            _, law_bank = load_bank(org.law.question_set)
            if set(law_bank) & set(self.bank):
                raise ValueError(f"법령 질문 세트의 ID가 겹친다: {sorted(set(law_bank) & set(self.bank))}")
            self.bank.update(law_bank)
        self.decider = decision_client or make_client(self.backend)
        self.generator = generator or gen.make_generator(org.generator)
        self.biz = bizapi or BizApiClient(org.bizapi_url, session_id)
        self._task: asyncio.Task | None = None  # 현재 버전의 파이프라인
        self._latest_utt: str | None = None
        self._writes: set[asyncio.Task] = set()  # 취소하지 않는 호출 묶음(쓰기, 본인 확인). _start_bundle
        self._warmup: asyncio.Future | None = None
        self._drivers = 0  # _drive를 돌리고 있거나 예열 뒤 곧 돌릴 호출 수(request_handoff가 본다)
        self._rerun = False  # request_handoff가 진행 중인 _drive에 같은 발화로 다시 진행하라고 남긴 표시
        self._addon_asked: str | None = None  # addon_only를 이미 물은 발화
        self._answered_utt: str | None = None  # 마지막으로 답(제안 노출)을 낸 고객 발화
        self._n_suggestions = 0
        self._n_agent = 0
        self._n_calls = 0
        self._prejudged: dict[str, tuple[str, asyncio.Future]] = {}  # 정책 D 선판단: 판별 입력 키 → (발화, 결과)
        self.log.emit(ev.SESSION_START, 0, org=org.org, today=org.today.isoformat(), decision_backend=self.backend,
                      generator=getattr(self.generator, "model", None), question_sets=[base_version, extra_version],
                      generation_prompt=gen.PROMPT_VERSION, llm_judge_prompt=llm.PROMPT_VERSION,
                      applies_threshold=org.applies_threshold, agreement_threshold=org.agreement_threshold,
                      thresholds={"decline": org.decline_threshold, "caller": org.caller_threshold,
                                  "owner_return": org.owner_return_threshold, "addon": org.addon_threshold,
                                  "identity_unavailable": org.identity_unavailable_threshold},
                      max_candidates=org.retrieval.max_candidates, bizapi_url=org.bizapi_url, channel=channel,
                      engine_exposure=channel in ENGINE_EXPOSED_CHANNELS)

    @property
    def version(self) -> int:
        """현재 대화 버전(실행기가 자기 이벤트를 같은 로그에 남길 때 쓴다)."""
        return self.state.version

    # --- 예열 --------------------------------------------------------------------------------

    async def start(self) -> None:
        """세션을 연 직후 부른다. 판별 백엔드에 고정 요청을 한 번 보내 연결을 미리 맺는다.

        첫 턴이 연결 수립(TLS 등) 비용을 내지 않게 하려는 것이다. 요청과 지연은 warmup 이벤트로 남기고 턴 판단으로 세지
        않는다. 예열이 끝나기 전에 고객 발화가 오면 handle이 예열을 기다린다(새 연결을 따로 열지 않도록).
        """
        if self._warmup is None:
            self._warmup = asyncio.ensure_future(self._send_warmup())
        await asyncio.shield(self._warmup)

    async def _send_warmup(self) -> None:
        state = {"org": self.org.org, "today": self.org.today.isoformat(), "identity_verified": False,
                 "conversation": [{"speaker": "customer", "text": WARMUP_TEXT, "final": True}], "known_facts": {},
                 "articles": []}
        t0 = now_ms()
        try:
            res = await self.decider.decide(state, render(["intent"], self.bank))
        except Exception as e:  # 예열 실패는 세션을 막지 않는다
            res = DecisionResult(self.backend, "응답 없음", {}, error=f"{type(e).__name__}: {e}")
        self.log.emit(ev.WARMUP, self.state.version, backend=res.backend, model=res.model,
                      latency_ms=round(res.latency_ms, 1), wall_ms=round(now_ms() - t0, 1), input_tokens=res.input_tokens,
                      cost_usd=res.cost_usd, error=res.error)

    # --- 입력 --------------------------------------------------------------------------------

    async def handle(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """입력 이벤트 하나를 처리하고 이 턴의 출력 이벤트(로그에 남긴 것과 같은 dict)를 돌려준다.

        고객 확정 발화면 처리가 끝날 때까지 기다린다. 처리 중에 더 새로운 발화가 들어오면 그때까지의 이벤트만 돌려준다.
        """
        received = now_ms()
        utt = parse_utterance(event, received)
        out: list[dict[str, Any]] = []
        if not utt.final:  # 미확정 자막은 버전을 올리지 않는다(정책 D의 선판단은 음성 계층이 prejudge로 따로 부른다)
            out.append(self.log.emit(ev.STT_PARTIAL, self.state.version, utt_id=utt.utt_id, text=utt.text))
            return out
        out.append(self.log.emit(ev.UTTERANCE_END, self.state.version, t_ms=utt.t_end_ms, utt_id=utt.utt_id,
                                 speaker=utt.speaker, text=utt.text, final=True, marker=utt.marker, received_ms=received))
        if utt.speaker != "customer":  # 사람 상담사 발화는 대화에만 남긴다
            self.state.add_turn(utt.utt_id, utt.speaker, utt.text)
            return out
        out.append(self._bump("utterance", utt_id=utt.utt_id))
        carried = self._carry_otp(self._latest_utt)
        self.state.add_turn(utt.utt_id, "customer", utt.text)
        claims = extract(utt.text, self.org.today)
        self.state.add_claims(utt.utt_id, claims)
        out.append(self.log.emit(ev.CLAIMS_EXTRACTED, self.state.version, utt_id=utt.utt_id, keys=claims.keys,
                                 phones=list(claims.phones), otps=["*" * len(o) for o in claims.otps],
                                 accounts=len(claims.accounts),
                                 **({"carried_otp_from": carried} if carried else {})))
        self._latest_utt = utt.utt_id
        # 다른 발화로 미리 한 판별은 더 쓰지 않는다(진행 중인 호출은 끝나게 두고 비용은 기록한다)
        self._prejudged = {k: v for k, v in self._prejudged.items() if v[0] == utt.utt_id}
        self._drivers += 1
        try:
            if self._warmup is not None and not self._warmup.done():  # 예열 중이면 같은 연결을 쓰도록 기다린다
                await asyncio.shield(self._warmup)
            await self._drive(utt.utt_id, out)
        finally:
            self._drivers -= 1
        return out

    async def request_handoff(self, reason: str = "agent_request") -> list[dict[str, Any]]:
        """상담사가 인계를 요청했다(웹 상담 콘솔의 인계 버튼). 인계는 엔진 정책이 진행한다.

        인계 사유를 시스템 사실에 두고(이미 있으면 그 사유를 쓴다) 버전을 올린 뒤 마지막 고객 발화로 다시 진행한다.
        create_handoff 호출과 안내 문구는 정책 규칙 1과 생성이 정한다(통화자가 제3자면 규칙 0이 앞선다). 고객 발화를
        처리하는 _drive가 있으면(예열을 기다리는 중이거나, 파이프라인이 막 끝나 아직 깨어나지 않은 경우 포함) 버전을 올리고
        다시 진행하라는 표시(_rerun)만 남긴다. 그 _drive가 새 버전으로 다시 진행하므로 파이프라인을 따로 만들지 않는다
        (같은 버전의 제안이 두 번 나가지 않게). 고객 발화가 아직 없으면 아무것도 하지 않는다.
        """
        out: list[dict[str, Any]] = []
        if self._latest_utt is None:
            return out
        if self.state.facts.handoff_reason is None:
            self.state.facts.handoff_reason = reason
        out.append(self._bump("agent_handoff", reason=reason))
        if self._drivers > 0:
            self._rerun = True
            return out
        self._drivers += 1
        try:
            await self._drive(self._latest_utt, out)
        finally:
            self._drivers -= 1
        return out

    async def aclose(self) -> None:
        if self._warmup is not None and not self._warmup.done():
            await asyncio.wait({self._warmup})
        pending = [f for _, f in self._prejudged.values() if not f.done()]
        if pending:  # 판별 클라이언트를 닫기 전에 선판단 호출을 끝낸다
            await asyncio.wait(pending)
        if self._writes:
            await asyncio.gather(*self._writes, return_exceptions=True)
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.wait({self._task})
        await self.decider.aclose()
        await self.generator.aclose()
        await self.biz.aclose()
        self.log.close()

    # --- 버전과 작업 --------------------------------------------------------------------------

    def _bump(self, cause: str, cancel: bool = True, **info: Any) -> dict[str, Any]:
        old = self.state.version
        self.state.version += 1
        event = self.log.emit(ev.VERSION_BUMPED, self.state.version, cause=cause, **{"from": old, "to": self.state.version},
                              **info)
        if cancel and self._task is not None and not self._task.done():
            self._task.cancel()
        return event

    def _stale(self, version: int) -> bool:
        return self.state.version != version

    def _carry_otp(self, prev: str | None) -> str | None:
        """새 고객 발화가 왔는데 앞 고객 발화(prev)에 아직 답하지 못했고 그 발화의 인증번호로 본인 확인을 시작하지
        않았으면, 그 인증번호를 넘겨 둔다(state.carried_otp). 넘겨 둔 발화 ID를 돌려준다.

        앞 발화의 파이프라인은 새 발화로 취소되고 정책은 이번 발화의 진술을 보므로, 넘겨 두지 않으면 고객이 말한 인증번호가
        사라진다. 빠른 정정(채팅에서 연달아 보냄), 음성에서 한 말이 확정 자막 두 개로 나뉘는 경우에 생긴다. 이미 본인 확인을
        시작했으면 그 호출은 취소되지 않고 결과가 반영되므로(SHIELDED_CALLS) 넘기지 않는다.
        """
        if prev is None or prev == self._answered_utt or self.state.identity.verified:
            return None
        claims = self.state.claims_by_utt.get(prev)
        if claims is None or not claims.otps or prev in self.state.identity.tried_utt.values():
            return None
        self.state.carried_otp = (prev, claims.otps[-1])
        return prev

    def _discard(self, stage: str, version: int, **info: Any) -> dict[str, Any]:
        return self.log.emit(ev.RESULT_DISCARDED, self.state.version, stage=stage, started_version=version, **info)

    async def _drive(self, utt_id: str, out: list[dict[str, Any]]) -> None:
        while True:
            if self._latest_utt != utt_id:  # 예열을 기다리는 사이 더 새로운 고객 발화가 왔다. 그 발화가 진행한다
                return
            self._rerun = False
            task = asyncio.create_task(self._pipeline(self.state.version, out))
            self._task = task
            await asyncio.wait({task})
            if not task.cancelled() and not self._rerun:  # _rerun은 request_handoff만 켠다
                return
            if self._latest_utt != utt_id:  # 더 새로운 고객 발화가 이어받았다
                return
            # 시작한 파이프라인이 끝난 뒤 완료된 쓰기 결과로 버전이 올랐다. 같은 발화로 다시 진행한다.

    # --- 파이프라인 ----------------------------------------------------------------------------

    async def _pipeline(self, version: int, out: list[dict[str, Any]]) -> None:
        my_v = version
        stage = "retrieval"
        try:
            latest = self.state.latest_suggestion()
            pending = latest.proposal if latest else None
            agreed = declined = identity_unavailable = False
            plan: policy.Plan | None = None
            for n_pass in range(MAX_PASSES):
                first = n_pass == 0
                # 3. 검색
                stage = "retrieval"
                t0 = now_ms()
                result = self.index.search(self._retrieval_context())
                law_extra: dict[str, Any] = {}
                if self.law is not None:
                    # 법령만 전체 고객 발화와 기존 의도·표현 확장을 쓴다. 조직 약관 검색어는 그대로다.
                    law_query = self.index.query_text(self._retrieval_context(), all_turns=True) \
                        if self.law.cfg.query_scope == "all" else result.query
                    law_ids, law_ms = self.law.search(law_query)
                    law_ids = [a for a in law_ids if a not in result.candidates]
                    result.candidates = result.candidates + law_ids
                    result.sources.update({a: "law" for a in law_ids})
                    law_extra = {"law_candidates": law_ids, "law_ms": round(law_ms, 3),
                                 "law_profile": self.law.cfg.profile, "law_query": law_query}
                out.append(self.log.emit(ev.RETRIEVAL_DONE, self.state.version, started_version=my_v,
                                         duration_ms=round(now_ms() - t0, 3), candidates=result.candidates,
                                         sources=result.sources, query=result.query, **law_extra))
                if self._stale(my_v):
                    out.append(self._discard(stage, my_v))
                    return
                # 4. 판별
                stage = "decision"
                res, qids = await self._decide(result.candidates, first, latest, my_v, out)
                if self._stale(my_v):
                    out.append(self._discard(stage, my_v, backend=res.backend))
                    return
                labels, probs = self._labels(res, qids)
                # 5. 근거 선택과 이전 제안 무효화
                selected = [a for a in result.candidates if probs.get(self._applies_qid(a), 0.0) >= self.org.applies_threshold]
                if self.law is not None and self.law.cfg.mode == "r4":
                    # R4: 법령 근거는 판별 확률 순(리랭킹). 약관 근거는 v1처럼 검색 순서다
                    law_sel = [a for a in selected if result.sources.get(a) == "law"]
                    law_sel.sort(key=lambda a: -probs.get(self._applies_qid(a), 0.0))
                    selected = [a for a in selected if result.sources.get(a) != "law"] + law_sel
                if "addon_only" in qids and "addon_only" in labels:
                    self.state.addon_only = probs["addon_only"] >= self.org.addon_threshold
                if first:
                    intent = labels.get("intent") or self.state.request
                    old_caller = self.state.caller
                    self._update_caller(labels.get("caller"), probs.get("caller", 0.0))
                    self._invalidate(latest, labels, intent, old_caller, my_v, out)
                    # 동의는 노출한 접수 내용에 대한 것이다. 제안이 suggestion_valid나 요청 라벨 변화로 무효가 되었어도
                    # 조건 변경이 없고 정책이 지금 만드는 접수 내용(쓰기와 금액)이 같으면 동의로 본다(같은지는 정책이 본다).
                    # 거절은 조건 변경과 함께 올 수 있으므로("환불이 안 되면 해지 안 해요") 조건 변경과 관계없이 본다.
                    if pending is not None:
                        declined = probs.get("customer_declined", 0.0) >= self.org.decline_threshold
                        agreed = labels.get("condition_changed") != "true" and not declined and \
                            probs.get("customer_agreed", 0.0) >= self.org.agreement_threshold
                    identity_unavailable = probs.get("identity_unavailable", 0.0) >= self.org.identity_unavailable_threshold
                    self.state.request = intent
                sufficient = labels.get("sufficient")
                attached = [a for a in result.candidates if result.sources.get(a) == "rule"]
                self.state.evidence = Evidence(my_v, result.candidates, selected,
                                               {a: probs.get(self._applies_qid(a), 0.0) for a in result.candidates},
                                               None if sufficient is None else sufficient == "true",
                                               labels.get("next_info"), labels.get("action"), self.state.request,
                                               attached)
                # 6. 근거 갱신
                out.append(self.log.emit(ev.EVIDENCE_UPDATED, my_v, articles=selected, candidates=result.candidates,
                                         applies={a: round(p, 3) for a, p in self.state.evidence.applies.items()},
                                         sufficient=self.state.evidence.sufficient, next_info=labels.get("next_info"),
                                         action=labels.get("action"), intent=self.state.request, caller=self.state.caller,
                                         addon_only=self.state.addon_only, pass_no=n_pass, decision_error=res.error,
                                         attached=attached))
                # 7. 정책. 앞 턴이 시작한 쓰기나 본인 확인이 아직 진행 중이면 끝날 때까지 기다린다. 그 결과 없이 정책을
                #    정하면 이미 접수 중인 내용을 다시 안내하거나 본인 확인을 다시 묻게 된다(빠른 정정 테스트). 묶음이 끝나면
                #    그 결과로 버전이 올라 이 작업은 취소되고 최신 발화로 다시 진행한다(_run_bundle).
                stage = "policy"
                in_flight = [w for w in self._writes if not w.done()]
                if in_flight:
                    out.append(self.log.emit(ev.WRITES_WAIT, my_v, writes=len(in_flight)))
                    await asyncio.wait(in_flight)
                    if self._stale(my_v):  # 쓰기 결과로 버전이 올랐는데 아직 취소되지 않았으면 스스로 다시 시작한다
                        raise asyncio.CancelledError
                plan = policy.decide(self.state, policy.PolicyInput(self.state.request, labels.get("action"),
                                                                    labels.get("next_info"), agreed, pending,
                                                                    declined, identity_unavailable),
                                     self.org.policy, self.org.required_notices)
                # 본인 확인 보류는 새 번호나 인증번호가 오기 전까지 유지한다(정책 규칙 4)
                if plan.rule == "identity_unavailable":
                    self.state.identity.unavailable = True
                elif plan.rule == "verify_identity" or self.state.current_claims().phones:
                    self.state.identity.unavailable = False
                out.append(self.log.emit(ev.POLICY_DECIDED, my_v, agreed=agreed, declined=declined,
                                         identity_unavailable=identity_unavailable, caller=self.state.caller,
                                         **plan.log()))
                if not plan.has_tool_work:
                    break
                stage = "tools"
                tools = await self._run_tools(plan, my_v, out)
                if self._stale(my_v):
                    out.append(self._discard(stage, my_v, tools=tools))
                    return
                out.append(self._bump("tool", cancel=False, tools=tools))
                my_v = self.state.version
                agreed = False  # 동의는 쓰기 한 번에만 쓴다
                identity_unavailable = False  # 도구를 부른 뒤에는(본인 확인 시도 등) 이번 발화의 판단을 다시 쓰지 않는다
            else:
                plan = policy.Plan("pass_limit", "hold")
                out.append(self.log.emit(ev.POLICY_DECIDED, my_v, agreed=False, **plan.log()))
            # 8. 생성
            stage = "generation"
            evidence = self._answer_evidence(plan)
            payload = self._generation_payload(plan, evidence)
            request_ids = []

            def record_generation(value, feedback, attempt):
                request_id = f"{self.session_id}-g{my_v}-{attempt}"
                request_ids.append(request_id)
                out.append(self.log.emit(ev.GENERATION_REQUEST, my_v, model=self.generator.model, action=plan.action,
                                         rule=plan.rule, articles=[a["id"] for a in value["articles"]],
                                         request_id=request_id, input=value, feedback=feedback, attempt=attempt))

            g = await gen.generate_checked(self.generator, payload, self.org.today, on_request=record_generation)
            out.append(self.log.emit(ev.GENERATION_RESPONSE, self.state.version, request_version=my_v, model=g.model,
                                     latency_ms=round(g.latency_ms, 1), input_tokens=g.input_tokens,
                                     output_tokens=g.output_tokens, cost_usd=g.cost_usd, attempts=g.attempts,
                                     fallback=g.fallback, violations=g.violations, error=g.error))
            if self._stale(my_v):
                out.append(self._discard(stage, my_v))
                return
            # 9. 제안과 노출
            self._expose(plan, g, my_v, out, evidence, generation_request_id=request_ids[-1])
        except asyncio.CancelledError:
            out.append(self.log.emit(ev.TASK_CANCELLED, self.state.version, stage=stage, started_version=my_v))
            raise
        except Exception as e:  # 엔진 오류로 세션이 멈추지 않게 보류로 답하고 기록한다
            out.append(self.log.emit(ev.ENGINE_ERROR, self.state.version, stage=stage, started_version=my_v,
                                     error=f"{type(e).__name__}: {e}", traceback=traceback.format_exc(limit=5)))
            if not self._stale(my_v):
                self._expose(policy.Plan("engine_error", "hold"), gen.fallback("hold", None, "fallback"), my_v, out, {})

    def _retrieval_context(self, state: SessionState | None = None) -> RetrievalContext:
        """검색 상태. state는 선판단(prejudge)이 미리 계산할 때 넘기는 사본이다."""
        s = state or self.state
        f = s.facts
        system_ids: list[str] = []
        ordered = sorted(f.quotes.values(), key=lambda q: q.status != "eligible")
        for q in ordered:
            system_ids += [a for a in q.article_ids if a not in system_ids]
        for rec in f.receipts.values():
            system_ids += [a for a in rec.get("article_ids", []) if a not in system_ids]
        return RetrievalContext(
            customer_texts=tuple(s.customer_texts()), intent=s.request,
            identity_verified=s.identity.verified, identity_failed=s.identity.failed > 0,
            approval_required=any((q.quote or {}).get("approval_required") for q in f.quotes.values()
                                  if q.status == "eligible"),
            handoff_reason=f.handoff_reason is not None, system_article_ids=tuple(system_ids))

    # --- 판별 --------------------------------------------------------------------------------

    def _question_ids(self, candidates: list[str], first: bool, latest: Suggestion | None,
                      state: SessionState | None = None, utt_id: str | None = None, mark_addon: bool = True) -> list[str]:
        """state·utt_id는 선판단이 사본과 아직 넘겨받지 않은 발화로 계산할 때 준다. 그때는 addon_only를 물었다는 표시를
        남기지 않는다(mark_addon=False)."""
        s = state or self.state
        utt = utt_id or self._latest_utt
        qids: list[str] = []
        if first:
            qids += ["intent", "caller"]
            if s.known_facts():
                qids.append("condition_changed")
        qids += [self._applies_qid(a) for a in candidates]
        if candidates:
            qids.append("sufficient")
        qids += ["next_info", "action"]
        if first and latest is not None:
            qids.append("suggestion_valid")
            if latest.proposal is not None:
                qids += ["customer_agreed", "customer_declined"]
        # 고객이 본인 확인을 마칠 수 없다고 하는가: 본인 확인을 물었거나 한 번 실패한 뒤에만 묻는다
        last_rule = s.suggestions[-1].rule if s.suggestions else None
        if first and not s.identity.verified and (s.identity.failed or last_rule == "identity_required"):
            qids.append("identity_unavailable")
        # 해지 대상이 부가 상품뿐인가: 유효한 부가 상품 구독이 조회됐을 때만, 발화마다 한 번(조회 직후 패스에서도) 묻는다
        if utt != self._addon_asked and policy.addon_subscription(s, self.org.policy) is not None:
            qids.append("addon_only")
            if mark_addon:
                self._addon_asked = utt
        return qids

    def _update_caller(self, label: str | None, p: float) -> None:
        """통화자 관계를 갱신한다. 제3자·법정대리인으로는 caller_threshold, 제3자·법정대리인에서 본인으로는 더 높은
        owner_return_threshold가 필요하다(잘못 본인으로 돌아가면 제3자에게 본인 확인을 묻게 된다)."""
        if label in (THIRD_PARTY, GUARDIAN) and p >= self.org.caller_threshold:
            self.state.caller = label
        elif label == OWNER and self.state.caller != OWNER and p >= self.org.owner_return_threshold:
            self.state.caller = OWNER

    async def _decide(self, candidates: list[str], first: bool, latest: Suggestion | None, my_v: int,
                      out: list[dict[str, Any]]) -> tuple[DecisionResult, list[str]]:
        qids = self._question_ids(candidates, first, latest)
        prev = latest.text if (first and latest is not None) else None
        state = self.state.decision_state([self.articles[a] for a in candidates], prev)
        questions = render(qids, self.bank)
        out.append(self.log.emit(ev.DECISION_REQUEST, my_v, backend=self.backend, questions=qids,
                                 state_chars=len(json.dumps(state, ensure_ascii=False))))
        pre = self._prejudged.get(_decision_key(state, questions)) if first else None
        if pre is not None:  # 정책 D: 같은 입력으로 미리 한 판별이 있으면 그 결과를 쓴다(끝나지 않았으면 기다린다)
            t0 = now_ms()
            res = await asyncio.shield(pre[1])
            if res.answers and not res.error:
                out.append(self._decision_response(res, qids, my_v, 0, wait_ms=round(now_ms() - t0, 1)))
                return res, qids
        attempts = 0
        while True:
            attempts += 1
            try:
                res = await self.decider.decide(state, questions)
            except Exception as e:  # 백엔드가 예외를 던져도 판별 실패로 다루고 코드 규칙은 계속 쓴다
                res = DecisionResult(self.backend, "응답 없음", {}, error=f"{type(e).__name__}: {e}")
            if not (res.error and not res.answers and res.error.startswith(DECISION_RETRYABLE)) or attempts == 2:
                break
        out.append(self._decision_response(res, qids, my_v, attempts))
        return res, qids

    def _decision_response(self, res: DecisionResult, qids: list[str], my_v: int, attempts: int,
                           wait_ms: float | None = None) -> dict[str, Any]:
        """decision.response. 선판단 결과를 쓰면(wait_ms가 있음) latency_ms는 파이프라인이 실제로 기다린 시간이고,
        비용은 선판단 줄(decision.prejudge)에 이미 남겼으므로 0이다. 백엔드 지연은 backend_latency_ms에 둔다."""
        answers = {}
        for q, a in res.answers.items():
            label, p, _ = label_and_prob(a)
            answers[q] = {"label": label, "p": round(p, 3)} if a["type"] == "choice" else {"p_true": round(a["noul"], 3)}
        extra = {} if wait_ms is None else {"prejudged": True, "backend_latency_ms": round(res.latency_ms, 1)}
        return self.log.emit(ev.DECISION_RESPONSE, self.state.version, request_version=my_v, backend=res.backend,
                             model=res.model, latency_ms=round(res.latency_ms if wait_ms is None else wait_ms, 1),
                             attempts=attempts, input_tokens=res.input_tokens,
                             cost_usd=res.cost_usd if wait_ms is None else 0.0, error=res.error, answers=answers,
                             meta=res.meta, **extra)

    # --- 정책 D: 미확정 자막 선판단 ---------------------------------------------------------------

    def prejudge(self, utt_id: str, text: str) -> bool:
        """미확정 자막 text를 이번 고객 발화(utt_id)로 본 상태로 첫 판별을 미리 돌려 둔다(docs/evaluation-voice.md §9.2).

        세션 상태의 사본에 그 발화를 더해, 확정 발화가 왔을 때 첫 판별이 만들 판별 입력과 질문을 그대로 계산한다(검색 포함,
        동기 코드라 그 사이 상태가 바뀌지 않는다). 상태와 버전은 바꾸지 않는다. 나중의 첫 판별(_decide)이 같은 입력이면 이
        결과를 쓰고, 다르면 쓰지 않는다. 호출을 시작했으면 True. 예열 중이거나, 같은 입력을 이미 시작했거나, 진행 중인
        선판단이 PREJUDGE_MAX_IN_FLIGHT개면 시작하지 않는다."""
        text = text.strip()
        if not text or (self._warmup is not None and not self._warmup.done()):
            return False
        if sum(not f.done() for _, f in self._prejudged.values()) >= PREJUDGE_MAX_IN_FLIGHT:
            return False
        sim = copy.deepcopy(self.state)
        sim.version += 1
        sim.add_turn(utt_id, "customer", text)
        sim.add_claims(utt_id, extract(text, self.org.today))
        result = self.index.search(self._retrieval_context(sim))
        latest = sim.latest_suggestion()
        qids = self._question_ids(result.candidates, True, latest, state=sim, utt_id=utt_id, mark_addon=False)
        state = sim.decision_state([self.articles[a] for a in result.candidates], latest.text if latest else None)
        questions = render(qids, self.bank)
        key = _decision_key(state, questions)
        if key in self._prejudged:
            return False
        self._prejudged[key] = (utt_id, asyncio.ensure_future(self._prejudge_call(key, utt_id, text, state, questions,
                                                                                  qids)))
        return True

    async def _prejudge_call(self, key: str, utt_id: str, text: str, state: dict[str, Any], questions: dict[str, Any],
                             qids: list[str]) -> DecisionResult:
        t0 = now_ms()
        try:
            res = await self.decider.decide(state, questions)
        except Exception as e:  # 선판단 실패는 확정 발화의 판별이 다시 한다
            res = DecisionResult(self.backend, "응답 없음", {}, error=f"{type(e).__name__}: {e}")
        self.log.emit(ev.DECISION_PREJUDGE, self.state.version, t_ms=t0, utt_id=utt_id, text=text, key=key[:16],
                      backend=res.backend, model=res.model, questions=qids, latency_ms=round(res.latency_ms, 1),
                      input_tokens=res.input_tokens, cost_usd=res.cost_usd, error=res.error)
        return res

    def _labels(self, res: DecisionResult, qids: list[str]) -> tuple[dict[str, str], dict[str, float]]:
        """choice는 가장 높은 선택지, noul은 참일 확률. 라벨은 임계값 0.5(applies·동의는 조직 설정 임계값을 따로 쓴다)."""
        labels: dict[str, str] = {}
        probs: dict[str, float] = {}
        for q in qids:
            a = res.answers.get(q)
            if a is None:
                continue
            if a["type"] == "noul":
                probs[q] = float(a["noul"])
                labels[q] = "true" if probs[q] >= 0.5 else "false"
            else:
                labels[q], probs[q], _ = label_and_prob(a)
        return labels, probs

    def _invalidate(self, latest: Suggestion | None, labels: dict[str, str], intent: str | None, old_caller: str,
                    my_v: int, out: list[dict[str, Any]]) -> None:
        if latest is None or latest.status != "active":
            return
        reasons = []
        if labels.get("suggestion_valid") == "false":
            reasons.append("suggestion_valid=false")
        if labels.get("condition_changed") == "true":
            reasons.append("condition_changed=true")
        if intent is not None and latest.intent is not None and intent != latest.intent:
            reasons.append(f"intent_changed:{latest.intent}->{intent}")
        if self.state.caller != old_caller:
            reasons.append(f"caller_changed:{old_caller}->{self.state.caller}")
        if reasons:
            latest.status = "invalidated"
            out.append(self.log.emit(ev.SUGGESTION_INVALIDATED, my_v, suggestion_id=latest.id,
                                     basis_version=latest.basis_version, invalidated_version=my_v, reasons=reasons))

    # --- 도구 --------------------------------------------------------------------------------

    async def _call(self, call: policy.ToolCall, my_v: int, out: list[dict[str, Any]]) -> ToolResult:
        self._n_calls += 1
        call_id = f"c{self._n_calls}"  # 병렬 호출의 결과를 호출과 짝짓는다
        out.append(self.log.emit(ev.TOOL_CALL, self.state.version, request_version=my_v, call_id=call_id,
                                 tool=call.tool, args=mask_args(call.args), target=call.target))
        r = await self.biz.call(call.tool, call.args, target=call.target)
        # 서버 상태를 바꾸는 호출(쓰기, 본인 확인)은 버전이 지나도 결과를 반영한다. 조회는 지난 버전이면 버린다.
        stale = self._stale(my_v) and call.tool not in WRITE_TOOLS and call.tool != "verify_identity"
        out.append(self.log.emit(ev.TOOL_RESULT, self.state.version, request_version=my_v, call_id=call_id,
                                 tool=call.tool, ok=r.ok,
                                 status=r.status, error_code=r.error_code, attempts=r.attempts, latency_ms=r.latency_ms,
                                 idempotency_key=r.idempotency_key, data=r.data if r.ok else r.error))
        if stale:
            out.append(self._discard("tool", my_v, tool=call.tool))
        else:
            self.state.apply_tool_result(r)
        return r

    async def _run_tools(self, plan: policy.Plan, my_v: int, out: list[dict[str, Any]]) -> list[str]:
        """정책이 정한 도구를 실행한다. 조회는 병렬. 서버 상태를 바꾸는 호출(본인 확인)은 병렬로, 쓰기는 순서대로, 둘 다
        취소하지 않는 묶음(_start_bundle)으로 보낸다."""
        names: list[str] = []
        if plan.calls:
            for c in plan.calls:
                if c.tool == "verify_identity":
                    pair = (c.args["phone"], c.args["otp"])
                    self.state.identity.tried.add(pair)
                    # 처음 보낸 발화는 인증번호를 말한 발화다(앞 발화에서 넘겨받은 인증번호면 그 발화, policy 규칙 3)
                    src = plan.detail.get("otp_from") or self.state.last_customer_utt_id() or ""
                    self.state.identity.tried_utt.setdefault(pair, src)
                    self.state.carried_otp = None
            shielded = tuple(c for c in plan.calls if c.tool in SHIELDED_CALLS)
            if shielded:
                await asyncio.shield(self._start_bundle(
                    self._run_bundle(shielded, my_v, out, asyncio.current_task(), sequential=False)))
            rest = [c for c in plan.calls if c.tool not in SHIELDED_CALLS]
            await asyncio.gather(*(self._call(c, my_v, out) for c in rest))
            names += [c.tool for c in plan.calls]
        if plan.lookups:
            cid = self.state.identity.customer_id
            wave1 = [policy.ToolCall(t, {"customer_id": cid})
                     for t in ("get_customer", "list_subscriptions", "list_payments", "get_usage")]
            await asyncio.gather(*(self._call(c, my_v, out) for c in wave1))
            start, end = policy.incident_window(self.state)
            await self._call(policy.ToolCall("list_incidents", {"from": start, "to": end}), my_v, out)
            names += [c.tool for c in wave1] + ["list_incidents"]
        if plan.quote_reasons:
            sub, _ = policy.target_subscription(self.state, self.org.policy)
            calls = [policy.ToolCall("quote_refund", {"customer_id": self.state.identity.customer_id,
                                                      "subscription_id": sub["subscription_id"], "reason": r,
                                                      "request_date": self.org.today.isoformat()})
                     for r in plan.quote_reasons]
            await asyncio.gather(*(self._call(c, my_v, out) for c in calls))
            reasons = plan.detail.get("reasons", list(plan.quote_reasons))
            chosen = policy.choose_quote(reasons, self.state.facts.quotes, self.org.policy)
            out.append(self.log.emit(ev.QUOTE_SELECTED, self.state.version, request_version=my_v,
                                     tried={r: {"status": q.status, "refund_amount": (q.quote or {}).get("refund_amount")}
                                            for r, q in self.state.facts.quotes.items() if r in reasons},
                                     chosen=chosen.reason if chosen else None,
                                     priority=list(self.org.policy.quote_priority)))
            names += ["quote_refund"] * len(calls)
        if plan.writes:
            await asyncio.shield(self._start_bundle(
                self._run_bundle(plan.writes, my_v, out, asyncio.current_task(), sequential=True)))
            names += [c.tool for c in plan.writes]
        return names

    def _start_bundle(self, coro) -> asyncio.Future:
        """취소하지 않는 호출 묶음을 시작한다. 새 발화의 파이프라인은 정책 단계 전에 진행 중인 묶음이 끝나기를 기다린다
        (writes.wait)."""
        bundle = asyncio.ensure_future(coro)
        self._writes.add(bundle)
        bundle.add_done_callback(self._writes.discard)
        return bundle

    async def _run_bundle(self, calls: tuple[policy.ToolCall, ...], my_v: int, out: list[dict[str, Any]],
                          origin: asyncio.Task | None, sequential: bool) -> list[ToolResult]:
        """서버 상태를 바꾸는 호출 묶음. 쓰기(sequential)는 순서대로 보내고, 앞의 쓰기가 실패하면 뒤의 쓰기는 하지 않는다
        (계좌 등록 실패 후 환불 접수 같은 일을 막는다). 본인 확인은 함께 보낸다."""
        results: list[ToolResult] = []
        if sequential:
            for call in calls:
                r = await self._call(call, my_v, out)
                results.append(r)
                if not r.ok and r.error_code != "DUPLICATE_REQUEST":
                    break
                if call.tool == "register_refund_account" and not (r.data or {}).get("registered"):
                    break
        else:
            results = list(await asyncio.gather(*(self._call(c, my_v, out) for c in calls)))
        if origin is not None and (origin is not self._task or origin.done()):
            # 시작한 파이프라인이 없어졌다. 이 결과도 시스템 사실을 바꾸므로 버전을 올리고 최신 발화로 다시 진행한다.
            out.append(self._bump("tool", cancel=True, tools=[c.tool for c in calls], background=True))
        return results

    # --- 생성과 노출 ----------------------------------------------------------------------------

    def _answer_evidence(self, plan: policy.Plan) -> dict[str, list[str]]:
        """이 응답의 근거 집합: 응답을 만들 때 엔진이 실제로 쓴 조항을 출처별로(docs/engine.md §12). 생성 입력의 articles는
        이 순서의 합집합이고, 제안의 cited_articles도 이것이다. 조항 색인에 있는 것만, 앞 출처에 이미 있으면 빼고 담는다.

        - policy: 정책 규칙의 근거 조항(Plan.articles: 제3자 거절, 법정대리인, 승인 대상 환불, 부가 상품, 장애 보상)
        - rule: 상담 상태로 붙인 절차 조항(retrieval.rules: 본인 확인, 안내 의무, 승인, 인계, 본인 확인 실패)
        - decision: 판별이 고른 조항(applies ≥ 임계값)
        - system: 업무 API가 판단 근거로 돌려준 조항(견적·접수 결과의 article_ids)
        """
        f = self.state.facts
        e = self.state.evidence
        system: list[str] = []
        for q in sorted(f.quotes.values(), key=lambda q: q.status != "eligible"):
            system += list(q.article_ids)
        for rec in f.receipts.values():
            system += list(rec.get("article_ids", []))
        groups = {"policy": list(plan.articles), "rule": list(e.attached) if e else [],
                  "decision": list(e.selected) if e else [], "system": system}
        out: dict[str, list[str]] = {}
        seen: set[str] = set()
        for source, ids in groups.items():
            out[source] = [a for a in dict.fromkeys(ids) if a in self.articles and a not in seen]
            seen.update(out[source])
        return out

    def _applies_qid(self, article_id: str) -> str:
        """근거 판별 질문 ID. v2 법령 구간(종류 law)에 법령 질문 세트가 있으면 law_applies, 나머지는 v1의 applies."""
        a = self.articles.get(article_id)
        if self.law is not None and a is not None and a.type == "law" and "law_applies" in self.bank:
            return f"law_applies:{article_id}"
        return f"applies:{article_id}"

    def _generation_payload(self, plan: policy.Plan, evidence: dict[str, list[str]]) -> dict[str, Any]:
        """생성 입력. 조항은 이 응답의 근거 집합(_answer_evidence) 전부를 넣는다. 정책·상태가 붙인 조항은 판별의 applies와
        관계없이 들어간다.

        - 제3자 거절(plan.detail.refusal): 계정 정보를 알려 주지 않으므로(제15조 제3항) 시스템 사실, 견적, 고객 진술을 뺀다.
        - 인계·보류: 금액을 확정하지 않으므로 견적을 뺀다(인계 사유는 plan.detail에 있다).
        """
        f = self.state.facts
        ids = [a for source in evidence.values() for a in source]
        claims = {k: recs[-1].value for k, recs in self.state.claims.items()}
        facts: dict[str, Any] = {}
        if f.customer:
            facts["customer"] = {"name": f.customer.get("name"), "status": f.customer.get("status")}
        for k in ("subscriptions", "payments", "usage", "incidents"):
            if getattr(f, k) is not None:
                facts[k] = getattr(f, k)
        if f.receipts:
            facts["receipts"] = f.receipts
        if f.account_rejections:
            facts["refund_account_rejections"] = f.account_rejections
        if f.api_failures:
            facts["api_failures"] = f.api_failures
        # 견적의 금액이 환불인지 차감액인지(조직 설정 deduction_reasons). 차감액은 생성의 환불 금액(amount)이 될 수 없다
        quotes = {r: {**quote_summary(q), "kind": "deduction" if r in self.org.policy.deduction_reasons else "refund"}
                  for r, q in f.quotes.items()}
        if plan.detail.get("refusal"):
            facts, quotes, claims = {}, {}, {}
        elif plan.action in ("handoff", "hold"):
            quotes = {}
        return {
            "today": self.org.today.isoformat(),
            "plan": {"action": plan.action, "rule": plan.rule, "ask": plan.ask, "next_info": plan.next_info,
                     "notices": list(plan.notices), "proposal": plan.proposal.summary if plan.proposal else None,
                     "must_mention": list(plan.must_mention), "detail": plan.detail},
            "intent": self.state.request,
            "caller": self.state.caller,
            "identity_verified": self.state.identity.verified,
            "sufficient": self.state.evidence.sufficient if self.state.evidence else None,
            "articles": [{"id": a, "title": self.articles[a].title, "type": self.articles[a].type,
                          "text": self.articles[a].text} for a in ids],
            "quotes": quotes,
            "system_facts": facts,
            "customer_claims": claims,
            # 고객 계정 일을 다루는 응답이면 환불이 된다는 말에 대상 견적이 있어야 한다(generate.allowed_from)
            "refund_claims_need_quote": self.state.identity.verified or self.state.caller != OWNER or
            self.state.request in self.org.policy.records_intents,
            "conversation": [{"speaker": t.speaker, "text": t.text}
                             for t in self.state.conversation[-CONVERSATION_FOR_GENERATION:]],
            # v2 법령 묶음을 켠 세션: 약관과 법령이 어긋날 때의 결정적 검사(config/v2/law.toml)
            **({"law_guards": [{"id": g.id, "pattern": g.pattern.pattern, "min": g.min, "message": g.message}
                               for g in self.law.cfg.guards]} if self.law is not None else {}),
        }

    def _expose(self, plan: policy.Plan, g: gen.GenerationResult, my_v: int, out: list[dict[str, Any]],
                evidence: dict[str, list[str]], generation_request_id: str | None = None) -> None:
        """제안을 만들고 채널에 낸다. cited_articles는 이 응답의 근거 집합(_answer_evidence)이고, 생성기가 문장의 근거로 든
        조항은 generator_cited로 따로 남긴다(docs/engine.md §12). 고정 문구(생성 실패·검사 위반, 엔진 오류)는 근거 조항으로
        만든 문장이 아니므로 근거 집합을 비운다."""
        self._n_suggestions += 1
        sid = f"{self.session_id}-s{self._n_suggestions}"
        prev = self.state.latest_suggestion()
        if prev is not None:
            prev.status = "superseded"
        # 고정 문구로 대신했으면 접수 내용을 안내하지 못했으므로 동의 대상으로 두지 않는다
        proposal = plan.proposal if plan.rule == "proposal" and not g.fallback else None
        sources = {} if g.fallback else {k: v for k, v in evidence.items() if v}
        cited = [a for ids in sources.values() for a in ids]
        cited += [a for a in g.cited_articles if a not in cited]  # 생성기 인용은 입력 조항 안이다(generate.sanitize)
        s = Suggestion(sid, my_v, g.say, plan.action, self.state.request, plan.rule, cited, g.amount,
                       g.amount_final, proposal)
        self.state.suggestions.append(s)
        out.append(self.log.emit(ev.SUGGESTION_CREATED, my_v, suggestion_id=sid, basis_version=my_v, text=g.say,
                                 action=plan.action, rule=plan.rule, intent=self.state.request,
                                 cited_articles=cited, generator_cited=g.cited_articles, evidence_sources=sources,
                                 generation_request_id=generation_request_id,
                                 source=("engine_error" if plan.rule == "engine_error" else
                                         "checked_fallback" if g.fallback else "model"),
                                 amount=g.amount, amount_final=g.amount_final,
                                 proposal=({"signature": proposal.signature, "kind": proposal.kind,
                                            "writes": [w.tool for w in proposal.writes]} if proposal else None),
                                 fallback=g.fallback))
        self._n_agent += 1
        self.state.add_turn(f"a{self._n_agent}", "agent", g.say)  # 엔진 발화는 버전을 올리지 않는다
        s.exposed = True
        self._answered_utt = self._latest_utt
        self.state.carried_otp = None  # 답을 냈으면 앞 발화의 인증번호는 그 답(본인 확인을 다시 묻는 등)으로 처리됐다
        t = now_ms()
        out.append(self.log.emit(ev.AGENT_SAY, my_v, t_ms=t, text=g.say, suggestion_id=sid, cited_articles=cited,
                                 generator_cited=g.cited_articles, amount=g.amount, amount_final=g.amount_final))
        if self.channel in ENGINE_EXPOSED_CHANNELS:
            out.append(self.log.emit(ev.SUGGESTION_EXPOSED, my_v, t_ms=t, suggestion_id=sid, channel=self.channel,
                                     start_ms=t, end_ms=t))
