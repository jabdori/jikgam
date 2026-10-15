"""웹 서버(jikgam/web/app.py): 계정과 권한, 경로, 실시간 통화 넘겨받기, 모델 등록, 벤치마크 실행 확인(ADR 0021).

외부 API와 하위 프로세스는 부르지 않는다. 벤치마크 실행기는 가짜를 넣는다.
"""

import re
import time
import tomllib

import pytest
from starlette.testclient import TestClient
from web_fixture import build

from jikgam.web import auth
from jikgam.web.app import WebConfig, create_app
from jikgam.web.data import benchmarks as bm

T = auth.Account("t", "pw-t", "tenant", "org_t")
U = auth.Account("u", "pw-u", "tenant", "org_u")
A = auth.Account("a", "pw-a", "admin")


def hdr(acct: auth.Account) -> dict[str, str]:
    return {"Authorization": auth.basic_header(acct.name, acct.password)}


@pytest.fixture()
def env(tmp_path):
    root = build(tmp_path, 0)
    ran = []

    def runner(job, concurrency, duration):
        ran.append((job.model, job.suite, concurrency, duration))
        return True

    cfg = WebConfig(root=root, accounts={"t": T, "u": U, "a": A}, clock=lambda: 0.0, job_runner=runner)
    return TestClient(create_app(cfg)), root, ran


def test_accounts_parse_and_reject_bad_specs():
    accts = auth.parse_accounts("ops:pw1:admin; byeoldam:pw2:tenant:org_a")
    assert accts["ops"].role == "admin" and accts["byeoldam"].org == "org_a"
    for bad in ("x:pw:tenant", "x:pw:admin:org_a", "x::admin", "x:pw:root"):
        with pytest.raises(ValueError):
            auth.parse_accounts(bad)
    gen = auth.generate_accounts(["org_a", "org_b"])
    assert {a.role for a in gen.values()} == {"admin", "tenant"} and len(gen) == 3
    assert auth.check(auth.basic_header("admin", gen["admin"].password), gen) == gen["admin"]
    assert auth.check(auth.basic_header("admin", "틀림"), gen) is None and auth.check("Bearer x", gen) is None


def test_login_required_and_roles_are_separate(env):
    c, _, _ = env
    assert c.get("/app/calls").status_code == 401
    assert c.get("/app/calls").headers["www-authenticate"].startswith("Basic")
    assert c.get("/static/tokens.css").status_code == 200  # 정적 파일은 로그인 없이
    assert c.get("/admin/models", headers=hdr(T)).status_code == 403
    assert c.get("/app/calls", headers=hdr(A)).status_code == 403
    r = c.get("/", headers=hdr(A), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/models"
    r = c.get("/", headers=hdr(T), follow_redirects=False)
    assert r.headers["location"] == "/app/calls"


def test_tenant_sees_only_its_own_calls(env):
    c, _, _ = env
    mine = c.get("/app/calls?period=0", headers=hdr(T)).text
    other = c.get("/app/calls?period=0", headers=hdr(U)).text
    assert "가상 회사 티" in mine and "가상 회사 유" not in mine
    assert "가상 회사 유" in other
    t_ids = set(re.findall(r"call=([^\"&]+)", mine))
    u_ids = set(re.findall(r"call=([^\"&]+)", other))
    assert t_ids and u_ids and not t_ids & u_ids
    some = sorted(t_ids)[0]
    assert c.get(f"/app/calls/{some}", headers=hdr(U)).status_code == 404  # 다른 고객사의 통화는 열 수 없다


@pytest.mark.parametrize("path", ["/app/live", "/app/calls", "/app/report", "/app/knowledge", "/app/integrations"])
def test_saas_pages_render(env, path):
    c, _, _ = env
    r = c.get(path, headers=hdr(T))
    assert r.status_code == 200 and 'data-theme="saas"' in r.text and "ADMIN" not in r.text


@pytest.mark.parametrize("path", ["/admin/models", "/admin/tenants", "/admin/replay?s=rp-C/s20-r1-rp-C"])
def test_admin_pages_render(env, path):
    c, _, _ = env
    r = c.get(path, headers=hdr(A))
    assert r.status_code == 200 and 'data-theme="admin"' in r.text and "ADMIN" in r.text


def test_live_takeover_stops_the_feed(env):
    c, _, _ = env
    page = c.get("/app/live", headers=hdr(T)).text
    fid = re.search(r"/app/live/([\w-]+)/takeover", page).group(1)
    assert "상담사 응대 중" not in page and 'data-live="/app/live?call=' in page
    r = c.post(f"/app/live/{fid}/takeover", headers=hdr(T), follow_redirects=False)
    assert r.status_code == 303
    after = c.get(f"/app/live?call={fid}", headers=hdr(T)).text
    assert "상담사 응대 중" in after and "넘겨받음" in after
    assert c.post(f"/app/live/{fid}/takeover", headers=hdr(U)).status_code == 404  # 다른 고객사의 피드


def test_missing_recordings_are_not_served(env):
    c, _, _ = env
    page = c.get("/app/calls?period=0", headers=hdr(T)).text
    cid = sorted(set(re.findall(r"call=([^\"&]+)", page)))[0]
    assert c.get(f"/app/calls/{cid}/audio", headers=hdr(T)).status_code == 404
    assert c.get("/admin/replay/audio?s=rp-C/s20-r1-rp-C", headers=hdr(A)).status_code == 404


def test_register_model_writes_one_config_file(env):
    c, root, _ = env
    form = {"name": "new-local", "adapter": "local", "url": "http://127.0.0.1:9100/v1/systemone", "model": "laya-x",
            "key_env": "", "price_in": "", "price_out": ""}
    r = c.post("/admin/models", headers=hdr(A), data=form, follow_redirects=False)
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    cfg = tomllib.loads((root / "config/decision_models.d/new-local.toml").read_text(encoding="utf-8"))
    assert cfg["new-local"]["kind"] == "systemone" and cfg["new-local"]["model"] == "laya-x"
    assert "new-local" in bm.models(root)
    again = c.post("/admin/models", headers=hdr(A), data=form, follow_redirects=False)
    assert "error=" in again.headers["location"]  # 같은 이름은 거절
    bad = dict(form, name="Bad Name", adapter="local", url="http://example.com/x")
    assert "error=" in c.post("/admin/models", headers=hdr(A), data=bad, follow_redirects=False).headers["location"]


@pytest.mark.parametrize("field, value", [("url", "ftp://x"), ("adapter", "nope"), ("key_env", "lower"), ("price_in", "-1"),
                                          ("model", " ")])
def test_registration_validation(tmp_path, field, value):
    root = build(tmp_path, 0)
    r = bm.Registration("m1", "openai", "https://api.example.invalid/v1", "gpt-x", "API_KEY", "0.1", "0.2")
    setattr(r, field, value)
    ok, _ = bm.register(root, r)
    assert not ok


def test_openai_registration_kind(tmp_path):
    root = build(tmp_path, 0)
    ok, _ = bm.register(root, bm.Registration("gen-x", "openai", "https://api.example.invalid/v1", "gpt-x-2026", "API_KEY", "1", "2"))
    assert ok and bm.models(root)["gen-x"]["kind"] == "openai"
    assert bm.adapter(bm.models(root)["gen-x"]) == "OpenAI 호환 생성형"


def test_benchmark_needs_confirmation_then_runs(env):
    c, _, ran = env
    form = {"model": "jev", "suite": ["decisions", "load"], "concurrency": "4", "duration": "9"}
    r = c.post("/admin/benchmarks", headers=hdr(A), data=form)
    assert r.status_code == 200 and "실행 확인" in r.text and not ran  # 첫 요청은 예상 비용만 보인다
    r = c.post("/admin/benchmarks", headers=hdr(A), data={**form, "confirm": "yes"}, follow_redirects=False)
    assert r.status_code == 303
    for _ in range(200):  # 실행은 서버의 작업 스레드에서 차례로 돈다
        if len(ran) == 2:
            break
        time.sleep(0.01)
    assert sorted(ran) == [("jev", "decisions", 4, 9.0), ("jev", "load", 4, 9.0)]
    page = c.get("/admin/models", headers=hdr(A)).text
    assert "이 서버에서 시작한 벤치마크" in page


def test_benchmark_rejects_unknown_model_or_suite(env):
    c, _, ran = env
    for form in ({"model": "nope", "suite": "decisions"}, {"model": "jev", "suite": "test-split"}):
        r = c.post("/admin/benchmarks", headers=hdr(A), data=form, follow_redirects=False)
        assert r.status_code == 303 and "error=" in r.headers["location"]
    assert not ran


def test_benchmark_commands_never_use_test_split_or_holdout(tmp_path):
    jobs = bm.Jobs(tmp_path)
    for suite in bm.SUITES:
        cmds = jobs.commands(bm.Job("x", "jev", suite), 2, 3.0, "http://127.0.0.1:1")
        flat = " ".join(" ".join(c) for c in cmds)
        assert "test" not in flat.split() and "holdout" not in flat
        if suite == "decisions":
            assert "--split val" in flat


def test_holdout_replays_are_locked_without_flag(env):
    c, root, _ = env
    (root / "results/voice/scenarios/voice-holdout-C").mkdir(parents=True)
    (root / "results/voice/scenarios/voice-holdout-C/h01-r1-voice-holdout-C.jsonl").write_text("{}\n", encoding="utf-8")
    r = c.get("/admin/replay?s=scenarios/voice-holdout-C/h01-r1-voice-holdout-C", headers=hdr(A))
    assert r.status_code == 200 and "열 수 없습니다" in r.text
