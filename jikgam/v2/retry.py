"""평가용 판별 호출만 2·4·8초 뒤 다시 보낸다. 실시간 경로에는 적용하지 않는다."""

import asyncio
import time
from dataclasses import replace

DELAYS = (2, 4, 8)


async def decide(client, state, questions):
    started = time.perf_counter()
    tin = tout = 0
    cost = 0.0
    for attempt, delay in enumerate((0, *DELAYS), 1):
        if delay:
            await asyncio.sleep(delay)
        reply = await client.decide(state, questions)
        tin += reply.input_tokens
        tout += reply.output_tokens
        cost += reply.cost_usd
        if not reply.error:
            break
    return replace(reply, input_tokens=tin, output_tokens=tout, cost_usd=cost,
                   meta={**reply.meta, "attempts": attempt, "retries": attempt - 1,
                         "total_ms": (time.perf_counter() - started) * 1000})


async def ask_json(client, *args, **kwargs):
    """v2 생성·판정의 재시도도 같은 횟수로 묶는다. 내부 재시도를 끈 클라이언트를 쓴다."""
    validate = kwargs.pop("validate", None)
    started = time.perf_counter()
    tin = tout = thoughts = cached = 0
    cost = 0.0
    for attempt, delay in enumerate((0, *DELAYS), 1):
        if delay:
            await asyncio.sleep(delay)
        reply = await client.ask(*args, **kwargs)
        if not reply.error and validate is not None:
            reply = replace(reply, error=validate(reply.data))
        tin += reply.input_tokens
        tout += reply.output_tokens
        thoughts += reply.thoughts_tokens
        cached += reply.cached_tokens
        cost += reply.cost_usd
        if not reply.error:
            break
    return replace(reply, attempts=attempt, latency_ms=(time.perf_counter() - started) * 1000,
                   input_tokens=tin, output_tokens=tout, thoughts_tokens=thoughts, cached_tokens=cached, cost_usd=cost)
