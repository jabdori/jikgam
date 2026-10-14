"""AI 고객 음성 시나리오 실행기(docs/evaluation-voice.md §3.1).

    uv run python -m jikgam.voice.sim --policy C --scenarios s20 --runs 1 --bizapi-url http://127.0.0.1:8111
    uv run python -m jikgam.voice.sim --policy A --scenario-file data/scenarios/org_a_holdout.jsonl --scenarios all \
        --runs 3 --customers domain/org_a/customers.json --customers domain/org_a/customers_holdout.json \
        --bizapi-url http://127.0.0.1:8112 --run-id voice-holdout-A

채팅 실행기(jikgam/sim/runner.py)와 같은 시나리오, 같은 고객 AI 규칙, 같은 판정기를 쓰고, 고객 말과 상담 답이 음성으로
오간다.

- 세션: A는 개선형 RAG, C는 엔진(jikgam/voice/replay.py의 make_voice_session). 음성 계층은 jikgam/voice/pipeline.py이고
  A는 stale_guard=False로 엔진의 음성 출력 규칙을 끈다(docs/evaluation-voice.md §2.1).
- 고객 말: 고객 AI(음성 판 프롬프트)가 쓴 문장을 Gemini TTS(config/voice.toml [customer_tts])로 읽혀 16kHz로 바꾸고, 앞뒤
  무음을 잘라 앞 200ms·뒤 300ms 무음을 붙여 시험 전송으로 실시간으로 넣는다. 인증번호 숫자는 한 자리씩 한글 읽기로 바꿔
  TTS에 넣는다(tts_text). 넣은 구간마다 voice.segment(말하려던 문장, TTS에 넣은 문장, 기준 발화 시작·종료)를 세션 로그에
  남긴다.
- 고객이 듣는 것: 재생이 시작된 답의 문장. 끊긴 답에는 [끊김]을 붙인다(heard).
- 말하는 때:
  - 보통: 그 말의 확정 자막이 오고, 세션 처리·응답 재생이 모두 끝나 조용해진 상태가 gap_ms(700ms) 동안 이어진 뒤.
    고객 AI와 TTS가 문장을 만드는 시간은 그 뒤에 더해진다.
  - 빠른 정정(조건 변경 사건): 고객 AI가 직전 말과 사건 말을 함께 쓴다. 둘을 먼저 합성하고, 직전 말의 기준 발화 종료
    fast_delay_ms(1,000ms) 뒤에 사건 말의 음성이 시작되게 넣는다. 답을 기다리지 않는다.
- 끝: 고객 done, 상담사 연결(채팅 실행기의 handed_off와 같은 규칙), 고객 말 max_turns(16)개.
- 녹음: <세션>.wav(왼쪽 고객, 오른쪽 실제로 재생된 응답). 시작 시각·해시·길이는 세션 로그의 voice.recording.

결과: <out-dir>/<run_id>/<세션>.jsonl(엔진·음성 계층 이벤트), <세션>.sim.jsonl(시뮬레이터 기록), <세션>.judge.json(AI 판정),
<세션>.metrics.json(음성 지표), <세션>.wav와 pipecat.log(저장소 밖), summary.json.
보고서: uv run python -m jikgam.voice.report <run_dir>...
"""

import argparse
import asyncio
import hashlib
import os
import re
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any

from loguru import logger

from jikgam.decision.registry import load_config, secrets
from jikgam.engine import events as ev
from jikgam.engine.chat import session_cost
from jikgam.engine.org import ROOT, OrgConfig, load_org
from jikgam.sim import customer as cust
from jikgam.sim import judge
from jikgam.sim.gemini import GeminiJson
from jikgam.sim.runner import (DEFAULT_MAX_TURNS, FAST_DELAY_MS, Message, SimLog, customer_seed, customers_file,
                               engine_costs, git_commit, handed_off, inject_fault, reset_bizapi, select, write_summary)
from jikgam.sim.scenario import (customer_events, file_sha256, followup_events, injections, load_scenarios, marker,
                                 registered_phones, scenario_path, sms_codes)

from . import bridge as vb
from .audio import resample, speech_bounds, trim_and_pad
from .config import TtsConfig, VoiceConfig, load_voice_config
from .customer_audio import LEAD_MS, SAMPLE_RATE, TAIL_MS, synthesize
from .metrics import SEGMENT, session_summary
from .replay import POLICIES, TTS_TOKENS_PER_S, Segments, make_voice_session, save_recording, write_json

OUT_DIR = ROOT / "results/voice/scenarios"
GAP_MS = 700.0  # 조용해진 뒤 고객이 말을 시작하기까지(docs/voice.md §5와 같은 값)
FINAL_TIMEOUT_S = 10.0  # 고객 말을 넣은 뒤 확정 자막을 기다리는 한도. 없으면 기록하고 넘어간다
SETTLE_TIMEOUT_S = 180.0  # 처리와 재생이 끝나기를 기다리는 한도
DIGIT_READING = "공일이삼사오육칠팔구"


def read_digits(digits: str) -> str:
    """숫자를 한 자리씩 한글로 읽는다(0016 → 공공일육)."""
    return "".join(DIGIT_READING[int(d)] for d in digits)


PHONE_SPAN = re.compile(r"(?<!\d)(01\d)[-\s.]?(\d{3,4})[-\s.]?(\d{4})(?!\d)")


def read_phone(m: re.Match) -> str:
    """휴대폰 번호를 사람이 불러 주듯 묶음마다 한 자리씩 한글로 읽고 묶음 사이를 쉰다(010-0000-0016 → 공일공, 공공공공, 공공일육)."""
    return ", ".join(read_digits(g) for g in m.groups())


def tts_text(text: str, codes: set[str]) -> str:
    """TTS에 넣을 문장. 숫자를 한 자리씩 읽어야 하는 것만 한글 읽기로 바꾸고 나머지(금액, 날짜)는 그대로 둔다.
    - 휴대폰 번호: 묶음마다 한 자리씩(read_phone). 숫자로 넣으면 TTS 읽기가 매번 달라 STT가 0을 빠뜨리는 일이 잦았다
      (개발용 s20 점검, 2026-10-14).
    - 고객이 받은 인증번호(codes): 한 자리씩. TTS가 "0016"을 "십육"처럼 읽지 않게 한다. 모의 인증번호는 휴대폰 번호의 끝
      4자리라서(jikgam/sim/scenario.py sms_codes) 번호를 먼저 바꾼 뒤 남은 숫자에만 적용한다."""
    text = PHONE_SPAN.sub(read_phone, text)
    pattern = "|".join(re.escape(c) for c in sorted(codes, key=len, reverse=True) if c)
    if not pattern:
        return text
    return re.sub(rf"(?<!\d)(?:{pattern})(?!\d)", lambda m: read_digits(m.group(0)), text)


@dataclass
class CustomerSpeech:
    text: str
    tts_text: str
    pcm: bytes
    speech_start_ms: float  # 파일 안 음성 시작(앞 무음 뒤)
    speech_end_ms: float
    usage: dict[str, Any] = field(default_factory=dict)


class CustomerVoice:
    """고객 AI의 문장을 고객 목소리 음성으로 만든다."""

    def __init__(self, cfg: VoiceConfig):
        from google import genai

        self.cfg = TtsConfig(**{**vars(cfg.customer_tts), "price_input_per_mtok": cfg.tts.price_input_per_mtok,
                                "price_audio_per_mtok": cfg.tts.price_audio_per_mtok})
        self.client = genai.Client(api_key=secrets()[self.cfg.api_key_env])

    async def speak(self, text: str, codes: set[str]) -> CustomerSpeech:
        said = tts_text(text, codes)
        pcm, rate, usage = await synthesize(self.client, self.cfg, said)
        pcm = trim_and_pad(resample(pcm, rate, SAMPLE_RATE), SAMPLE_RATE, LEAD_MS, TAIL_MS)
        b = speech_bounds(pcm, SAMPLE_RATE)
        return CustomerSpeech(text, said, pcm, float(b.start_ms), float(b.end_ms), usage)


def heard(customer_lines: list[tuple[float, str]], playbacks: list[vb.Playback]) -> list[dict[str, str]]:
    """고객 AI에게 줄 대화: 고객이 한 말과, 재생이 시작된 답(끊겼으면 [끊김]). 시각 순서."""
    items = [(t, "customer", text) for t, text in customer_lines]
    items += [(pb.start_ms, "agent", pb.text + (" [끊김]" if pb.interrupted else ""))
              for pb in playbacks if pb.start_ms is not None]
    return [{"speaker": s, "text": text} for _, s, text in sorted(items, key=lambda x: x[0])]


async def settle(bridge: vb.EngineBridge, seg: dict[str, Any], gap_ms: float, notes: list[str]) -> None:
    """그 구간의 확정 자막이 오고, 처리·재생이 끝나 조용한 상태가 gap_ms 동안 이어질 때까지 기다린다."""
    if not await bridge.wait_until(lambda: any(f["received_ms"] >= seg["feed_start_ms"] for f in bridge.finals),
                                   FINAL_TIMEOUT_S):
        notes.append(f"고객 {seg['line']}번째 말의 확정 자막이 {FINAL_TIMEOUT_S:.0f}초 안에 오지 않았다")
    deadline = ev.now_ms() + SETTLE_TIMEOUT_S * 1000

    def quiet() -> bool:
        return bridge.idle() and not bridge.customer_turn_open()

    while True:
        # 확정 자막 기다리기(customer_turn_open)는 시간이 지나면 저절로 끝나 알림이 없을 수 있으므로 짧게 나눠 다시 본다
        while not quiet():
            if ev.now_ms() >= deadline:
                notes.append(f"고객 {seg['line']}번째 말 뒤 처리·재생이 {SETTLE_TIMEOUT_S:.0f}초 안에 끝나지 않았다")
                return
            await bridge.wait_until(quiet, 0.2)
        n_final, n_play = len(bridge.finals), len(bridge.playbacks)
        await asyncio.sleep(gap_ms / 1000)
        if quiet() and len(bridge.finals) == n_final and len(bridge.playbacks) == n_play:
            return


@dataclass
class _State:
    turn: int = 0
    executed: set[int] = field(default_factory=set)
    lines: list[tuple[float, str]] = field(default_factory=list)  # (기준 발화 시작, 고객이 말한 문장)
    fast: list[dict[str, Any]] = field(default_factory=list)
    fast_missing: list[int] = field(default_factory=list)
    customer_tts_usd: float = 0.0


async def _speak(scn: dict[str, Any], session, transport, segs: Segments, log: SimLog, st: _State,
                 speeches: list[CustomerSpeech], msgs: list[Message], done: bool, info: dict[str, Any],
                 fast_delay_ms: float, bridge: vb.EngineBridge) -> dict[str, Any]:
    """말 하나를 넣는다. 둘이면 빠른 정정: 첫 말의 기준 발화 종료 fast_delay_ms 뒤에 둘째 말의 음성이 시작되게 넣는다.
    마지막으로 넣은 구간을 돌려준다."""
    from .transport import sleep_until

    seg: dict[str, Any] = {}
    prev_end: float | None = None
    for k, (m, sp) in enumerate(zip(msgs, speeches)):
        if k > 0 and prev_end is not None:
            # 입력은 20ms 조각 경계에서 시작하므로 한 조각 일찍 넣는다(실제 간격은 fast 기록에 남긴다)
            await sleep_until(prev_end + fast_delay_ms - sp.speech_start_ms - 20)
        st.turn += 1
        mark = None
        if m.event is not None:
            mark = marker(scn, m.event)
            st.executed.add(m.event)
        seg = {"line": st.turn, "file": None, "text": m.text, "tts_text": sp.tts_text, "marker": mark,
               "barge_in_planned": False, "fast_followup": k > 0, "version_before": session.version}
        fed = transport.input().enqueue(sp.pcm)
        t_start = await fed.started
        seg.update(feed_start_ms=t_start, speech_start_ms=round(t_start + sp.speech_start_ms, 3),
                   speech_end_ms=round(t_start + sp.speech_end_ms, 3))
        segs.items.append(seg)
        st.lines.append((seg["speech_start_ms"], m.text))
        st.customer_tts_usd += sp.usage.get("cost_usd") or 0
        log.emit("customer.turn", turn=st.turn, utt_id=f"line{st.turn}", text=m.text, tts_text=sp.tts_text,
                 executed_event=m.event, marker=mark, done=done and k == len(msgs) - 1, fast_followup=k > 0,
                 pair_size=len(msgs), speech_start_ms=seg["speech_start_ms"], speech_end_ms=seg["speech_end_ms"],
                 tts=sp.usage, **(info if k == 0 else {}))
        if k > 0 and prev_end is not None:
            started_between = any(pb.start_ms is not None and prev_end <= pb.start_ms < seg["speech_start_ms"]
                                  for pb in bridge.playbacks)
            st.fast.append({"event": f"{scn['id']}#{m.event}", "gap_ms": round(seg["speech_start_ms"] - prev_end, 1),
                            "reply_started_before_followup": started_between})
        prev_end = seg["speech_end_ms"]
        seg["feed_end_ms"] = await fed.finished + 20
        session.log.emit(SEGMENT, session.version, t_ms=t_start, **seg)
    return seg


async def run_voice_scenario(scn: dict[str, Any], policy: str, org: OrgConfig, cfg: VoiceConfig, voice: CustomerVoice,
                             run_dir: Path, session_id: str, customer: cust.Customer, registered: frozenset[str], *,
                             max_turns: int = DEFAULT_MAX_TURNS, run: int = 1, seed: int | None = None,
                             fast_delay_ms: float = FAST_DELAY_MS, gap_ms: float = GAP_MS) -> dict[str, Any]:
    from pipecat.transports.base_transport import TransportParams
    from pipecat.workers.runner import WorkerRunner

    from .pipeline import IN_RATE, OUT_RATE, build_voice_pipeline
    from .transport import PacedAudioTransport, Recorder

    reset_bizapi(org.bizapi_url)
    log = SimLog(run_dir / f"{session_id}.sim.jsonl", session_id)
    log.emit("sim.start", scenario=scn["id"], policy=policy, channel=vb.CHANNEL, run=run, customer_model=customer.model,
             customer_prompt=customer.prompt_version, customer_seed=seed, max_turns=max_turns,
             fast_delay_ms=fast_delay_ms, gap_ms=gap_ms, followup_events=sorted(followup_events(scn)),
             bizapi_url=org.bizapi_url)
    injected = []
    for idx, inj in injections(scn):
        resp = inject_fault(org.bizapi_url, inj)
        injected.append({"event": f"{scn['id']}#{idx}", **inj})
        log.emit("sim.inject", event=f"{scn['id']}#{idx}", applied="start", response=resp, **inj)
    codes = set(sms_codes(scn, registered).values())
    t_start = ev.now_ms()
    session = make_voice_session(policy, org, session_id, run_dir)
    await session.start()
    recorder = Recorder()
    transport = PacedAudioTransport(TransportParams(audio_in_enabled=True, audio_in_sample_rate=IN_RATE,
                                                    audio_out_enabled=True, audio_out_sample_rate=OUT_RATE), recorder)
    segs = Segments()
    vp = build_voice_pipeline(session, cfg, transport, marker_for=segs.marker_for, stale_guard=policy == "C")
    bridge = vp.bridge
    started = asyncio.Event()

    @vp.worker.event_handler("on_pipeline_started")
    async def _started(_worker, _frame):
        started.set()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(vp.worker)
    pipe = asyncio.create_task(runner.run())
    t_open = ev.now_ms()
    st = _State()
    notes: list[str] = []
    customer_usd = 0.0
    end_reason, error = "runner_error", None
    try:
        await asyncio.wait_for(started.wait(), 30)
        session.log.emit(vb.VOICE_SESSION, session.version, **cfg.log(), pipecat=pkg_version("pipecat-ai"),
                         google_genai=pkg_version("google-genai"), policy=policy, stale_guard=bridge.stale_guard,
                         decision_backend=getattr(session, "backend", None), scenario=scn["id"], run=run,
                         gap_ms=gap_ms, fast_delay_ms=fast_delay_ms,
                         customer_tts={"model": voice.cfg.model, "voice": voice.cfg.voice},
                         clock="engine monotonic ms; t_end_ms = VAD stop decision (wall→engine) - stop_secs")
        await asyncio.sleep(0.5)
        msgs, done = [Message(scn["opening"])], False
        info: dict[str, Any] = {"llm": {"source": "opening"}}
        while True:
            speeches = [await voice.speak(m.text, codes) for m in msgs]
            seg = await _speak(scn, session, transport, segs, log, st, speeches, msgs, done, info, fast_delay_ms, bridge)
            await settle(bridge, seg, gap_ms, notes)
            if done:
                end_reason = "customer_done"
                break
            if handed_off(bridge.turn_outputs):
                end_reason = "handoff"
                break
            if st.turn >= max_turns:
                end_reason = "max_turns"
                break
            ct = await customer.next_turn(heard(st.lines, bridge.playbacks), st.turn + 1, st.executed)
            customer_usd += ct.usage.get("cost_usd") or 0
            if ct.error:
                end_reason, error = "customer_error", ct.error
                log.emit("customer.error", turn=st.turn + 1, error=ct.error, llm=ct.usage)
                break
            if ct.fast_missing is not None:
                st.fast_missing.append(ct.fast_missing)
            if not ct.say:
                if ct.done:
                    log.emit("customer.done", turn=st.turn + 1, llm=ct.usage, rejected_event=ct.rejected_event)
                    end_reason = "customer_done"
                else:
                    end_reason, error = "customer_error", "빈 메시지"
                    log.emit("customer.error", turn=st.turn + 1, error=error, llm=ct.usage)
                break
            if ct.followup_say:
                msgs = [Message(ct.say), Message(ct.followup_say, ct.executed_event)]
            else:
                msgs = [Message(ct.say, ct.executed_event)]
            done = ct.done
            info = {"llm": ct.usage, "rejected_event": ct.rejected_event, "notes": ct.notes}
    except Exception as e:  # 실행기·음성 계층 오류. 세션은 아래에서 정리하고 기록을 남긴다
        end_reason, error = "runner_error", f"{type(e).__name__}: {e}"
        log.emit("runner.error", error=error)
    finally:
        if not await bridge.drain(60):
            notes.append("끝낼 때 처리나 응답 재생이 남아 있었다")
        t_close = ev.now_ms()
        await vp.worker.end()
        try:
            await asyncio.wait_for(pipe, 30)
        except TimeoutError:
            notes.append("파이프라인이 30초 안에 끝나지 않았다")
            await vp.worker.cancel()
        rec = save_recording(recorder, session, run_dir / f"{session_id}.wav", t_open)
        await session.aclose()
        missing = [i for i, _ in customer_events(scn) if i not in st.executed]
        log.emit("sim.end", reason=end_reason, error=error, customer_turns=st.turn, executed_events=sorted(st.executed),
                 missing_events=missing, fast_corrections=st.fast, fast_missing=st.fast_missing,
                 customer_usd=customer_usd, customer_tts_usd=round(st.customer_tts_usd, 6), notes=notes,
                 duration_ms=round(ev.now_ms() - t_start, 1))
        log.close()
    events = ev.read_log(run_dir / f"{session_id}.jsonl")
    metrics = session_summary(events)
    agent_s = sum(len(pcm) / 2 / r for _, pcm, r in recorder.agent)
    stream_min = (t_close - t_open) / 60000
    costs = {"customer_usd": customer_usd, "customer_tts_usd": round(st.customer_tts_usd, 6),
             **engine_costs(run_dir / f"{session_id}.jsonl"), "engine_usd": round(session_cost(events), 6),
             "stt_estimate_usd": round(stream_min * cfg.stt.price_usd_per_min, 6),
             "tts_estimate_usd": round(agent_s * TTS_TOKENS_PER_S * cfg.tts.price_audio_per_mtok / 1e6, 6)}
    write_json(run_dir / f"{session_id}.metrics.json", {"session": session_id, **metrics})
    return {"scenario": scn["id"], "session": session_id, "run": run, "policy": policy, "customer_seed": seed,
            "end_reason": end_reason, "error": error, "customer_turns": st.turn, "executed_events": sorted(st.executed),
            "missing_events": missing, "fast_corrections": st.fast, "fast_missing": st.fast_missing,
            "injections": injected, "notes": notes, "duration_ms": round(ev.now_ms() - t_start, 1),
            "stt_stream_min": round(stream_min, 2), "tts_audio_s": round(agent_s, 1), "recording": rec, "costs": costs}


def _sha16(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


async def run(args: argparse.Namespace) -> Path:
    org = load_org(args.org)
    if args.bizapi_url:
        org = replace(org, bizapi_url=args.bizapi_url.rstrip("/"))
        os.environ["JIKGAM_BIZAPI_URL"] = org.bizapi_url  # 조직 설정의 bizapi_url보다 우선한다(docs/engine.md §9)
    path = (ROOT / args.scenario_file) if args.scenario_file else scenario_path(org.org)
    chosen = select(load_scenarios(path), args.scenarios)
    customer_files = [ROOT / c for c in args.customers] if args.customers else [customers_file(org)]
    registered = frozenset().union(*(registered_phones(c) for c in customer_files))
    run_id = args.run_id or f"{datetime.now(ev.KST):%Y%m%dT%H%M%S}-voice-{args.policy}"
    run_dir = args.out_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    logger.add(run_dir / "pipecat.log", level="DEBUG", encoding="utf-8")
    cfg = load_voice_config()
    models = load_config()
    backend = org.decision_backend if args.policy == "C" else None
    summary: dict[str, Any] = {
        "run_id": run_id, "policy": args.policy, "channel": vb.CHANNEL, "stale_guard": args.policy == "C",
        "decision_backend": backend, "decision_model": (models.get(backend) or {}).get("model") if backend else None,
        "generator": org.generator, "generator_model": models[org.generator]["model"], "org": org.org,
        "bizapi_url": org.bizapi_url, "scenario_file": str(path.relative_to(ROOT)).replace("\\", "/"),
        "scenario_sha256": file_sha256(path), "runs": args.runs, "run_start": args.run_start, "max_turns": args.max_turns,
        "fast_delay_ms": args.fast_delay_ms, "gap_ms": args.gap_ms, "voice": cfg.log(),
        "voice_config_sha256_16": _sha16(ROOT / "config/voice.toml"),
        "customer_tts": {"model": cfg.customer_tts.model, "voice": cfg.customer_tts.voice},
        "customer_model": models[args.customer_model]["model"], "customer_prompt": cust.VOICE_PROMPT_VERSION,
        "judge_model": None if args.no_judge else models[args.judge_model]["model"],
        "judge_prompt": None if args.no_judge else judge.JUDGE_VOICE_PROMPT_VERSION,
        "git": git_commit(), "started": datetime.now(ev.KST).isoformat(timespec="seconds"), "ended": None,
        "note": ("보류용" if "holdout" in path.name else "개발용") + " 시나리오 음성 결과(docs/evaluation-voice.md). "
                "합성 음성과 AI 고객의 결과이며 운영 성과가 아니다.",
        "customer_files": [str(c.relative_to(ROOT)).replace("\\", "/") for c in customer_files],
        "sessions": [],
    }
    write_summary(run_dir, summary)
    voice = CustomerVoice(cfg)
    judge_client = None if args.no_judge else GeminiJson.from_config(args.judge_model)
    try:
        for k in range(args.run_start, args.run_start + args.runs):
            for scn in chosen:
                sid = f"{scn['id']}-r{k}-{run_id}"
                seed = customer_seed(scn["id"], k)
                customer = cust.make_customer(scn, registered, args.customer_model, seed, channel=vb.CHANNEL)
                try:
                    sess = await run_voice_scenario(scn, args.policy, org, cfg, voice, run_dir, sid, customer, registered,
                                                    max_turns=args.max_turns, run=k, seed=seed,
                                                    fast_delay_ms=args.fast_delay_ms, gap_ms=args.gap_ms)
                finally:
                    await customer.aclose()
                if judge_client is not None:
                    rec = await judge.judge_session(scn, run_dir, sid, judge_client)
                    sess["costs"]["judge_usd"] = rec["cost_usd"]
                    sess["judge_error"] = rec["error"]
                summary["sessions"].append(sess)
                write_summary(run_dir, summary)
                c = sess["costs"]
                fast = ", ".join(f"{f['event']} {f['gap_ms']:.0f}ms" for f in sess["fast_corrections"])
                total = sum(v for key, v in c.items() if key.endswith("usd") and key not in ("decision_usd", "generation_usd"))
                print(f"{sid}: {sess['end_reason']}, 고객 {sess['customer_turns']}턴, 사건 {sess['executed_events']}"
                      f"(미실행 {sess['missing_events']}), 빠른 정정 [{fast}], {sess['duration_ms'] / 1000:.0f}초, "
                      f"약 ${total:.4f}" + (f", 참고 {sess['notes']}" if sess["notes"] else ""), flush=True)
    finally:
        if judge_client is not None:
            await judge_client.aclose()
        summary["ended"] = datetime.now(ev.KST).isoformat(timespec="seconds")
        write_summary(run_dir, summary)
    return run_dir


def main() -> None:
    ap = argparse.ArgumentParser(description="AI 고객 음성 시나리오 실행기(docs/evaluation-voice.md §3.1)")
    ap.add_argument("--org", default="org_a")
    ap.add_argument("--policy", required=True, choices=POLICIES)
    ap.add_argument("--scenarios", default="all", help="쉼표로 구분한 시나리오 ID 또는 all")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--run-start", type=int, default=1)
    ap.add_argument("--scenario-file", help="시나리오 파일(저장소 기준 경로). 생략하면 data/scenarios/<org>.jsonl")
    ap.add_argument("--customers", action="append",
                    help="인증 문자를 줄 등록 번호를 읽을 고객 데이터(저장소 기준 경로, 여러 번). 생략하면 domain/<org>/customers.json")
    ap.add_argument("--run-id")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--bizapi-url", required=True, help="로컬 모의 업무 API 주소(세션마다 초기화한다)")
    ap.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    ap.add_argument("--fast-delay-ms", type=float, default=FAST_DELAY_MS)
    ap.add_argument("--gap-ms", type=float, default=GAP_MS)
    ap.add_argument("--customer-model", default=cust.DEFAULT_MODEL)
    ap.add_argument("--judge-model", default=judge.DEFAULT_JUDGE_MODEL)
    ap.add_argument("--no-judge", action="store_true")
    args = ap.parse_args()
    if args.runs < 1:
        raise SystemExit("--runs는 1 이상이어야 한다")
    sys.stdout.reconfigure(encoding="utf-8")
    run_dir = asyncio.run(run(args))
    print(f"실행 폴더: {run_dir}\n보고서: uv run python -m jikgam.voice.report {run_dir}")


if __name__ == "__main__":
    main()
