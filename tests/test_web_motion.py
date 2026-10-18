"""실시간 통화·리플레이 화면의 움직임에 필요한 서버 쪽 표시(ADR 0022, jikgam/web/static/console.js).

- 말하는 중인 AI 답은 기록된 재생 시작·끝 시각에 맞춰 드러낼 글자와 시각을 넘기고, 끊긴 답은 끊긴 지점까지만 보인다.
- 대화 줄·구분선·근거·순위 행은 이름(data-key)이 있어, 갱신 때 이미 있는 요소를 그대로 두고 새 것만 붙인다.
"""

import re
from pathlib import Path

from starlette.testclient import TestClient
from web_fixture import build

from jikgam.web import auth, settings
from jikgam.web.app import WebConfig, create_app
from jikgam.web.data.live import Feed, speech_plan
from jikgam.web.saas import live as saas_live
from jikgam.web.ui import components as ui

A = auth.Account("a", "pw-a", "admin")


def voice_events() -> list[dict]:
    """고객 두 번, AI 답 두 번(첫 답은 끝까지 1초, 둘째 답은 0.4초 만에 끼어들기로 끊김)."""
    return [
        {"type": "session.start", "t_ms": 0.0, "session": "m1", "org": "org_t"},
        {"type": "voice.session", "t_ms": 0.0},
        {"type": "utterance.end", "t_ms": 1000.0, "speaker": "customer", "text": "해지할게요"},
        {"type": "suggestion.created", "t_ms": 1400.0, "suggestion_id": "s1", "text": "가나다라마바사아자차",
         "basis_version": 1, "version": 1},
        {"type": "suggestion.exposed", "t_ms": 1500.0, "suggestion_id": "s1", "channel": "voice", "start_ms": 1500.0},
        {"type": "tts.playback_end", "t_ms": 2500.0, "suggestion_id": "s1", "start_ms": 1500.0, "end_ms": 2500.0,
         "played_ms": 1000.0, "interrupted": None},
        {"type": "utterance.end", "t_ms": 3000.0, "speaker": "customer", "text": "아니 환불만요"},
        {"type": "suggestion.created", "t_ms": 3400.0, "suggestion_id": "s2", "text": "카타파하거너더러머버",
         "basis_version": 2, "version": 2},
        {"type": "suggestion.exposed", "t_ms": 3500.0, "suggestion_id": "s2", "channel": "voice", "start_ms": 3500.0},
        {"type": "voice.barge_in", "t_ms": 3900.0, "suggestion_id": "s2", "started": True, "played_ms": 400.0},
        {"type": "tts.playback_end", "t_ms": 3900.0, "suggestion_id": "s2", "start_ms": 3500.0, "end_ms": 3900.0,
         "played_ms": 400.0, "interrupted": "barge_in"},
    ]


def feed_at(offset_s: float) -> Feed:
    return Feed("m-live-1", "org_t", Path("m1.jsonl"), voice_events(), None, offset_s, lambda: 0.0)


def test_speech_plan_follows_recorded_playback_and_cut_point():
    plan = speech_plan(voice_events())
    s1, s2 = plan["s1"], plan["s2"]
    assert (s1.start_ms, s1.end_ms, s1.chars, s1.cut) == (1500.0, 2500.0, 10, False)
    # 끝까지 재생된 답의 속도(10글자/1,000ms)로 0.4초 동안 들린 글자 수를 어림한다
    assert (s2.end_ms, s2.chars, s2.cut) == (3900.0, 4, True)
    assert [s1.shown(t) for t in (1000.0, 1500.0, 2000.0, 2500.0, 9000.0)] == [0, 0, 5, 10, 10]


def test_speaking_answer_reveals_characters_by_the_clock():
    feed = feed_at(1.0)  # 피드 시각 2,000ms: 첫 답의 절반을 말하는 중
    html = str(saas_live.conversation(None, feed, feed.call()))
    m = re.search(r'class="reveal" data-reveal-from="([\d.]+)" data-reveal-to="([\d.]+)"', html)
    assert m and (float(m.group(1)), float(m.group(2))) == (1500.0, 2500.0)
    assert html.count('class="rv on"') == 5 and html.count('class="rv"') == 5  # 아직 말하지 않은 글자는 숨긴다
    assert "가나다라마바사아자차" not in html  # 전체 문장을 한꺼번에 넣지 않는다
    assert 'data-now="2000.0"' in html and 'data-running="true"' in html
    assert 'data-follow' in html and 'role="region"' in html and 'tabindex="0"' in html


def test_cut_answer_shows_only_what_was_heard():
    feed = feed_at(3.0)  # 피드 시각 4,000ms: 둘째 답은 끊긴 뒤
    html = str(saas_live.conversation(None, feed, feed.call()))
    assert "카타파하…" in html and "카타파하거" not in html
    assert "말을 끊음" in html  # 기존 끊김 표시는 그대로
    assert "reveal" not in html  # 다 말한 답은 글자 공개를 하지 않는다


def test_rows_have_stable_keys_and_taken_over_feed_stops_the_clock():
    feed = feed_at(3.0)
    keys = re.findall(r'data-key="([^"]+)"', str(saas_live.conversation(None, feed, feed.call())))
    assert keys == ["customer:1000", "ai:1500", "customer:3000", "ai:3500"]
    feed.take_over()
    assert 'data-running="false"' in str(saas_live.conversation(None, feed, feed.call()))


def test_components_render_keys_and_current_links():
    conv = str(ui.conversation([ui.Turn("고객", "안녕하세요", key="customer:1"), ui.Divider("요청 변경", key="change:2")]))
    assert 'data-key="customer:1"' in conv and 'data-key="change:2"' in conv
    assert "data-follow" not in conv  # 통화 기록 화면 같은 곳은 따로 스크롤하지 않는다
    links = str(ui.link_list([("제7조", "/a"), ("제8조", "/b", True)], label="근거"))
    assert '<li data-key="/b" class="is-current">' in links and '<li data-key="/a">' in links


def test_live_refresh_is_short():
    assert settings.LIVE_REFRESH_MS <= 250


def test_replay_swaps_in_place_with_keyed_rank_rows(tmp_path):
    root = build(tmp_path, 0)
    c = TestClient(create_app(WebConfig(root=root, accounts={"a": A}, clock=lambda: 0.0)))
    page = c.get("/admin/replay?s=rp-C/s20-r1-rp-C", headers={"Authorization": auth.basic_header("a", "pw-a")}).text
    assert 'data-swap' in page
    rows = re.findall(r'<li class="rank-row[^"]*" data-key="([^"]+)"', page)
    assert rows and len(rows) == len(set(rows))
    assert re.search(r'data-key="(prev|next)"', page)
