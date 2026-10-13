import math

from sim_logs import LogBuilder, mark

from jikgam.sim import metrics as mt


def test_refresh_latency_uses_first_evidence_at_or_after_utterance_version():
    b = LogBuilder()
    b.utt("처음")
    b.evidence()
    b.suggest("s1")
    b.utt("바꿀게요", marker=mark("x#0"), t=1000.0)  # 버전 2
    b.evidence(version=1, t=1050.0)  # 지난 버전으로 계산한 근거는 t1이 아니다
    b.evidence(version=2, t=1230.0)
    b.bump_tool()
    b.evidence(version=3, t=1400.0)
    rows = mt.refresh_latencies(b.events)
    assert len(rows) == 1
    r = rows[0]
    assert (r["event"], r["utt_id"], r["version"], r["t0"], r["t1"]) == ("x#0", "u2", 2, 1000.0, 1230.0)
    assert r["latency_ms"] == 230.0 and r["invalidates_prior"] is True


def test_refresh_miss_when_no_evidence_before_next_customer_utterance():
    b = LogBuilder()
    b.utt("바꿀게요", marker=mark("x#0"), t=100.0)
    b.suggest("s1")  # 근거 갱신 없이 응답만 했다
    b.utt("다음", t=500.0)
    b.evidence(t=600.0)  # 다음 발화 뒤의 갱신은 앞 발화의 t1이 아니다
    rows = mt.refresh_latencies(b.events)
    assert [r["latency_ms"] for r in rows] == [None]


def test_refresh_miss_at_session_end_and_unmarked_or_non_change_utterances_ignored():
    b = LogBuilder()
    b.utt("평범")
    b.evidence()
    b.utt("끼어들기", marker=mark("x#0", cc=False, ip=False))
    b.evidence()
    b.utt("마지막 변경", marker=mark("x#1", ip=False))
    rows = mt.refresh_latencies(b.events)
    assert [(r["event"], r["latency_ms"]) for r in rows] == [("x#1", None)]


def test_percentile_inf_and_latency_stats():
    assert mt.percentile_inf([100.0, 200.0, 300.0, 400.0], 50) == 250.0
    assert mt.percentile_inf([100.0, 200.0, math.inf], 50) == 200.0
    assert math.isinf(mt.percentile_inf([100.0, 200.0, math.inf], 95))
    assert math.isinf(mt.percentile_inf([math.inf, math.inf], 50))
    assert mt.percentile_inf([7.0], 95) == 7.0
    assert math.isnan(mt.percentile_inf([], 50))
    st = mt.latency_stats([100.0, None, 300.0])
    assert st["events"] == 3 and st["misses"] == 1 and st["p50"] == 300.0 and math.isinf(st["p95"])


def test_stale_exposure_numerator_denominator_event_level_and_unconfirmed():
    b = LogBuilder()
    b.utt("처음")  # v1
    b.suggest("s1", t=500.0)  # 변경 전 노출: 분모 아님
    b.utt("해지할게요", marker=mark("x#0"), t=1000.0)  # v2, invalidates_prior
    b.suggest("s2", basis=1, t=1500.0)  # 변경 전 버전으로 만든 제안을 변경 뒤에 노출 → 무효
    b.suggest("s3", basis=2, t=2000.0)  # 새 버전 기준 → 유효
    b.suggest("s3c", basis=1, channel="console", t=2100.0)  # 다른 채널의 무효 노출
    b.utt("결제일은 유월이에요", marker=mark("x#1", ip=False), t=2500.0)  # v3, invalidates_prior 거짓
    b.suggest("s4", basis=2, t=3000.0)  # 다시 판단하지 않고 노출 → 미재확인(무효 아님)
    st = mt.stale_exposure(b.events)
    chat = st["by_channel"]["chat"]
    assert (chat["invalid"], chat["denominator"], chat["unconfirmed"]) == (1, 3, 1)
    assert [c["suggestion_id"] for c in chat["cases"]] == ["s2", "s4"]
    console = st["by_channel"]["console"]
    assert (console["invalid"], console["denominator"]) == (1, 1)
    assert st["events"] == [{"event": "x#0", "invalid_exposures": 2}]


def test_stale_exposure_without_invalidating_event_has_empty_denominator():
    b = LogBuilder()
    b.utt("처음")
    b.suggest("s1")
    b.utt("변경", marker=mark("x#0", ip=False))
    b.suggest("s2", basis=1)
    st = mt.stale_exposure(b.events)
    assert st["by_channel"]["chat"]["denominator"] == 0 and st["by_channel"]["chat"]["invalid"] == 0
    assert st["by_channel"]["chat"]["unconfirmed"] == 1
    assert st["events"] == []


def test_aggregate_sums_sessions():
    a = LogBuilder("a")
    a.utt("변경", marker=mark("a#0"), t=100.0)
    a.evidence(t=150.0)
    a.suggest("a1", basis=0, t=200.0)
    c = LogBuilder("c")
    c.utt("변경", marker=mark("c#0"), t=100.0)
    c.suggest("c1", t=300.0)
    agg = mt.aggregate([mt.session_metrics(a.events), mt.session_metrics(c.events)])
    st = agg["refresh"]["stats"]
    assert st["events"] == 2 and st["misses"] == 1 and st["p50"] == math.inf
    ch = agg["stale"]["by_channel"]["chat"]
    assert (ch["invalid"], ch["denominator"]) == (1, 2)
    assert (agg["stale"]["events_with_invalid"], agg["stale"]["events_total"]) == (1, 2)
