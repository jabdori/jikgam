package orgbsrv

import (
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/jabdori/jikgam/bizapi/internal/orgb"
)

const testKey = "test-key"

func newTestServer(t *testing.T) *httptest.Server {
	t.Helper()
	ds, err := orgb.Load("../../../domain/org_b/customers.json")
	if err != nil {
		t.Fatal(err)
	}
	ts := httptest.NewServer(New(ds, testKey, slog.New(slog.NewJSONHandler(io.Discard, nil))))
	t.Cleanup(ts.Close)
	return ts
}

type resp struct {
	status int
	body   map[string]any
}

func (r resp) data() map[string]any { d, _ := r.body["data"].(map[string]any); return d }
func (r resp) code() string         { c, _ := r.body["error_code"].(string); return c }

func do(t *testing.T, ts *httptest.Server, method, path string, body any, hdr map[string]string) resp {
	t.Helper()
	var rd io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rd = bytes.NewReader(b)
	}
	req, _ := http.NewRequest(method, ts.URL+path, rd)
	req.Header.Set("X-Api-Key", testKey)
	for k, v := range hdr {
		if v == "" {
			req.Header.Del(k)
		} else {
			req.Header.Set(k, v)
		}
	}
	r, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(r.Body).Decode(&out)
	return resp{r.StatusCode, out}
}

func verify(t *testing.T, ts *httptest.Server, ref, mobile, code string) resp {
	return do(t, ts, "POST", "/api/v2/auth/verify", map[string]string{"mobile": mobile, "code": code}, map[string]string{"X-Contact-Ref": ref})
}

func TestAPIKeyRequired(t *testing.T) {
	ts := newTestServer(t)
	r := do(t, ts, "GET", "/api/v2/members/M1001", nil, map[string]string{"X-Api-Key": ""})
	if r.status != 401 || r.code() != ErrAPIKeyInvalid {
		t.Fatalf("%+v", r)
	}
	r = do(t, ts, "GET", "/api/v2/members/M1001", nil, nil)
	if r.status != 200 || r.body["status"] != "ok" || r.data()["member_name"] != "한서준" {
		t.Fatalf("%+v", r)
	}
}

func TestVerifyLocksAfterThreeFailures(t *testing.T) {
	ts := newTestServer(t)
	for i := 1; i <= 3; i++ {
		r := verify(t, ts, "c1", "010-5000-0101", "999999")
		if r.status != 200 || r.data()["ok"] != false || int(r.data()["failures"].(float64)) != i {
			t.Fatalf("%d: %+v", i, r)
		}
		if locked := r.data()["locked"] == true; locked != (i == 3) {
			t.Fatalf("%d: locked=%v", i, locked)
		}
	}
	if r := verify(t, ts, "c1", "010-5000-0101", "000101"); r.status != 423 || r.code() != ErrAuthLocked {
		t.Fatalf("%+v", r)
	}
	// 다른 상담 건은 따로 센다
	if r := verify(t, ts, "c2", "010-5000-0101", "000101"); r.data()["ok"] != true || r.data()["token"] == "" {
		t.Fatalf("%+v", r)
	}
	if r := do(t, ts, "POST", "/api/v2/auth/verify", map[string]string{"mobile": "x", "code": "y"}, nil); r.code() != ErrBadInput {
		t.Fatalf("X-Contact-Ref 없음: %+v", r)
	}
}

func estimate(t *testing.T, ts *httptest.Server, member, pass, typ string) resp {
	return do(t, ts, "POST", "/api/v2/refund-estimates",
		map[string]string{"member_no": member, "pass_no": pass, "refund_type": typ, "as_of": "2026-10-13"}, nil)
}

func TestRefundNeedsMemberTokenAndIdempotencyKey(t *testing.T) {
	ts := newTestServer(t)
	est := estimate(t, ts, "M1001", "PS1001", "COOLING_OFF")
	no := est.data()["estimate_no"].(string)
	body := map[string]string{"member_no": "M1001", "estimate_no": no, "charge_no": "CH1001-2"}
	if r := do(t, ts, "POST", "/api/v2/refunds", body, map[string]string{"Idempotency-Key": "k1"}); r.status != 401 || r.code() != ErrAuthRequired {
		t.Fatalf("토큰 없음: %+v", r)
	}
	tok := verify(t, ts, "c1", "01050000101", "000101").data()["token"].(string)
	auth := map[string]string{"Authorization": "Bearer " + tok}
	if r := do(t, ts, "POST", "/api/v2/refunds", body, auth); r.code() != ErrBadInput {
		t.Fatalf("멱등 키 없음: %+v", r)
	}
	auth["Idempotency-Key"] = "k1"
	first := do(t, ts, "POST", "/api/v2/refunds", body, auth)
	again := do(t, ts, "POST", "/api/v2/refunds", body, auth)
	if first.status != 200 || first.data()["state"] != "DONE" || first.data()["refund_no"] != again.data()["refund_no"] {
		t.Fatalf("%+v %+v", first, again)
	}
	auth["Idempotency-Key"] = "k2"
	if r := do(t, ts, "POST", "/api/v2/refunds", body, auth); r.status != 409 || r.code() != ErrAlreadyDone || r.body["existing"] == nil {
		t.Fatalf("다른 키로 같은 결제: %+v", r)
	}
	auth["Idempotency-Key"] = "k1"
	body["charge_no"] = "CH1001-1"
	if r := do(t, ts, "POST", "/api/v2/refunds", body, auth); r.code() != ErrIdempotencyMismatch {
		t.Fatalf("같은 키 다른 내용: %+v", r)
	}
	// 청약철회 접수 뒤 해지하면 이용은 신청일에 끝난다
	c := do(t, ts, "POST", "/api/v2/passes/PS1001/cancel", map[string]string{"member_no": "M1001"},
		map[string]string{"Authorization": "Bearer " + tok, "Idempotency-Key": "c1"})
	if c.data()["service_ends_on"] != "2026-10-13" {
		t.Fatalf("%+v", c)
	}
	// 토큰은 그 회원에게만 쓴다
	if r := do(t, ts, "POST", "/api/v2/passes/PS1002/cancel", map[string]string{"member_no": "M1002"},
		map[string]string{"Authorization": "Bearer " + tok, "Idempotency-Key": "c2"}); r.code() != ErrAuthRequired {
		t.Fatalf("%+v", r)
	}
}

func TestEstimateErrors(t *testing.T) {
	ts := newTestServer(t)
	r := estimate(t, ts, "M1002", "PS1002", "COOLING_OFF")
	if r.status != 422 || r.code() != ErrNotRefundable || r.body["estimate"] == nil {
		t.Fatalf("%+v", r)
	}
	r = estimate(t, ts, "M1009", "PS1009", "COOLING_OFF")
	esc := r.body["estimate"].(map[string]any)["escalate"].(map[string]any)
	if r.code() != ErrNeedsAgent || esc["code"] != "REPEAT_REFUND" {
		t.Fatalf("%+v", r)
	}
	if r := estimate(t, ts, "M9999", "PS1001", "COOLING_OFF"); r.status != 404 || r.code() != ErrNoSuchResource {
		t.Fatalf("%+v", r)
	}
	if r := estimate(t, ts, "M1001", "PS1001", "INCIDENT"); r.code() != ErrBadInput {
		t.Fatalf("%+v", r)
	}
	r = estimate(t, ts, "M1003", "PS1003", "YEARLY_PRORATE")
	if r.data()["total_krw"].(float64) != 107910 || r.data()["needs_review"] != true {
		t.Fatalf("%+v", r)
	}
}

func TestBankAccountRequiredAndOwnerMismatch(t *testing.T) {
	ts := newTestServer(t)
	tok := verify(t, ts, "c7", "010-5000-0107", "000107").data()["token"].(string)
	est := estimate(t, ts, "M1007", "PS1007", "DOUBLE_CHARGE")
	body := map[string]string{"member_no": "M1007", "estimate_no": est.data()["estimate_no"].(string), "charge_no": "CH1007-2"}
	hdr := map[string]string{"Authorization": "Bearer " + tok, "Idempotency-Key": "r1"}
	if r := do(t, ts, "POST", "/api/v2/refunds", body, hdr); r.code() != ErrBankAccountMissing {
		t.Fatalf("%+v", r)
	}
	acc := map[string]string{"bank_name": "책솔은행", "account_no": "123-456-7890", "owner_name": "권나연아빠"}
	hdr2 := map[string]string{"Authorization": "Bearer " + tok, "Idempotency-Key": "a1"}
	if r := do(t, ts, "PUT", "/api/v2/members/M1007/refund-account", acc, hdr2); r.data()["accepted"] != false || r.data()["reject_code"] != "OWNER_MISMATCH" {
		t.Fatalf("%+v", r)
	}
	acc["owner_name"] = "권나연" // 거절은 저장하지 않으므로 같은 키로 고쳐 보낼 수 있다
	if r := do(t, ts, "PUT", "/api/v2/members/M1007/refund-account", acc, hdr2); r.data()["accepted"] != true || r.data()["account_masked"] != "***-***-*890" {
		t.Fatalf("%+v", r)
	}
	if r := do(t, ts, "POST", "/api/v2/refunds", body, hdr); r.data()["route"] != "BANK_TRANSFER" || r.data()["krw"].(float64) != 11900 {
		t.Fatalf("%+v", r)
	}
}

func TestFaultsAndReset(t *testing.T) {
	ts := newTestServer(t)
	f := do(t, ts, "POST", "/_mock/faults", map[string]any{"endpoint": EPEstimates, "mode": "maintenance", "times": 2}, nil)
	if f.status != 200 {
		t.Fatalf("%+v", f)
	}
	for i := 0; i < 2; i++ {
		if r := estimate(t, ts, "M1001", "PS1001", "COOLING_OFF"); r.status != 503 || r.code() != ErrMaintenance {
			t.Fatalf("%+v", r)
		}
	}
	if r := estimate(t, ts, "M1001", "PS1001", "COOLING_OFF"); r.status != 200 {
		t.Fatalf("%+v", r)
	}
	do(t, ts, "POST", "/_mock/faults", map[string]any{"endpoint": EPMember, "mode": "timeout"}, nil)
	if r := do(t, ts, "GET", "/api/v2/members/M1001", nil, nil); r.status != 504 || r.code() != ErrUpstreamTimeout {
		t.Fatalf("%+v", r)
	}
	if r := do(t, ts, "POST", "/_mock/faults", map[string]any{"endpoint": "nope", "mode": "timeout"}, nil); r.code() != ErrBadInput {
		t.Fatalf("%+v", r)
	}
	verify(t, ts, "cx", "010-5000-0101", "1")
	do(t, ts, "POST", "/_mock/reset", nil, nil)
	if r := estimate(t, ts, "M1001", "PS1001", "COOLING_OFF"); r.data()["estimate_no"] != "EST-000001" {
		t.Fatalf("초기화 뒤 번호: %+v", r)
	}
}

func TestReadsUseOrgBShapes(t *testing.T) {
	ts := newTestServer(t)
	ch := do(t, ts, "GET", "/api/v2/members/M1006/charges", nil, nil).data()["items"].([]any)
	if c := ch[3].(map[string]any); c["charged_at"] != "2026-10-03T21:15:00+09:00" || c["pay_type"] != "CARD" {
		t.Fatalf("%+v", c)
	}
	rl := do(t, ts, "GET", "/api/v2/members/M1003/reading-log?from=2026-10-01", nil, nil).data()["items"].([]any)
	if len(rl) != 1 {
		t.Fatalf("%+v", rl)
	}
	out := do(t, ts, "GET", "/api/v2/outages?from=2026-10-09&to=2026-10-13", nil, nil).data()["items"].([]any)
	if len(out) != 2 || out[0].(map[string]any)["minutes"].(float64) != 150 {
		t.Fatalf("%+v", out)
	}
	if r := do(t, ts, "GET", "/api/v2/members/M1001/nothing", nil, nil); r.status != 404 {
		t.Fatalf("%+v", r)
	}
}
