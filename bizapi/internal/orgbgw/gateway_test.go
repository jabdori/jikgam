package orgbgw

import (
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"slices"
	"testing"
	"time"

	"github.com/jabdori/jikgam/bizapi/internal/orgb"
	"github.com/jabdori/jikgam/bizapi/internal/orgbsrv"
)

const key = "gw-test-key"

var quiet = slog.New(slog.NewJSONHandler(io.Discard, nil))

// stack은 조직 B 모의 서버와 그 앞의 어댑터를 띄운다. 엔진은 어댑터 주소만 안다.
func stack(t *testing.T) (gw *httptest.Server, backend *httptest.Server) {
	t.Helper()
	ds, err := orgb.Load("../../../domain/org_b/customers.json")
	if err != nil {
		t.Fatal(err)
	}
	backend = httptest.NewServer(orgbsrv.New(ds, key, quiet))
	t.Cleanup(backend.Close)
	gw = httptest.NewServer(New(Config{BackendURL: backend.URL, APIKey: key, Timeout: 2 * time.Second}, quiet))
	t.Cleanup(gw.Close)
	return gw, backend
}

type result struct {
	status int
	body   map[string]any
}

func (r result) err() map[string]any { e, _ := r.body["error"].(map[string]any); return e }
func (r result) code() string        { c, _ := r.err()["code"].(string); return c }

func callTool(t *testing.T, url, sid, tool string, body any) result {
	t.Helper()
	b, _ := json.Marshal(body)
	req, _ := http.NewRequest("POST", url+"/v1/"+tool, bytes.NewReader(b))
	if sid != "" {
		req.Header.Set("X-Session-ID", sid)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return result{resp.StatusCode, out}
}

func post(t *testing.T, url string, body any) int {
	t.Helper()
	b, _ := json.Marshal(body)
	resp, err := http.Post(url, "application/json", bytes.NewReader(b))
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	return resp.StatusCode
}

func verified(t *testing.T, url, sid, phone, otp string) {
	t.Helper()
	r := callTool(t, url, sid, "verify_identity", map[string]string{"phone": phone, "otp": otp})
	if r.status != 200 || r.body["verified"] != true {
		t.Fatalf("본인 확인 실패: %+v", r)
	}
}

func quoteOf(t *testing.T, url, sid, cid, sub, reason string) result {
	return callTool(t, url, sid, "quote_refund", map[string]string{"customer_id": cid, "subscription_id": sub, "reason": reason,
		"request_date": "2026-10-13"})
}

func TestLookupFieldMapping(t *testing.T) {
	gw, _ := stack(t)
	r := callTool(t, gw.URL, "s1", "verify_identity", map[string]string{"phone": "010-5000-0106", "otp": "000106"})
	if r.body["verified"] != true || r.body["customer_id"] != "M1006" || r.body["attempts"].(float64) != 1 ||
		r.body["failed_attempts"].(float64) != 0 || r.body["handoff_required"] != false {
		t.Fatalf("verify: %+v", r.body)
	}
	c := callTool(t, gw.URL, "s1", "get_customer", map[string]string{"customer_id": "M1006"}).body
	if c["customer_id"] != "M1006" || c["name"] != "신예준" || c["birth"] != "1990-01-15" || c["status"] != "active" {
		t.Fatalf("customer: %+v", c)
	}
	subs := callTool(t, gw.URL, "s1", "list_subscriptions", map[string]string{"customer_id": "M1006"}).body["subscriptions"].([]any)
	s := subs[0].(map[string]any)
	if s["subscription_id"] != "PS1006" || s["plan"] != "monthly" || s["price"].(float64) != 11900 ||
		s["next_billing"] != "2026-11-02" || s["status"] != "active" || s["trial"] != false {
		t.Fatalf("subscription: %+v", s)
	}
	pays := callTool(t, gw.URL, "s1", "list_payments", map[string]string{"customer_id": "M1006"}).body["payments"].([]any)
	p := pays[3].(map[string]any)
	if p["payment_id"] != "CH1006-4" || p["subscription_id"] != "PS1006" || p["date"] != "2026-10-03" ||
		p["paid_at"] != "2026-10-03T21:15" || p["method"] != "card" || p["dispute"] != false || p["amount"].(float64) != 11900 {
		t.Fatalf("payment: %+v", p)
	}
	u := callTool(t, gw.URL, "s1", "get_usage", map[string]string{"customer_id": "M1005", "since": "2026-10-01"}).body["usage"].([]any)
	if e := u[0].(map[string]any); e["date"] != "2026-10-06" || e["type"] != "audio_play" || e["reader"] != "family_member" {
		t.Fatalf("usage: %+v", u)
	}
	inc := callTool(t, gw.URL, "s1", "list_incidents", map[string]string{"from": "2026-10-03", "to": "2026-10-13"}).body["incidents"].([]any)
	want := []struct {
		id, start, cause string
		hours            float64
	}{{"OUT-1008", "2026-10-08T13:00", "company", 3}, {"OUT-1010", "2026-10-10T20:00", "vendor", 2.5}, {"OUT-1011", "2026-10-11T01:00", "company", 1}}
	for i, w := range want {
		e := inc[i].(map[string]any)
		if e["incident_id"] != w.id || e["start"] != w.start || e["cause"] != w.cause || e["hours"].(float64) != w.hours {
			t.Fatalf("incident %d: %+v", i, e)
		}
	}
}

func TestQuoteMapping(t *testing.T) {
	gw, _ := stack(t)
	q := quoteOf(t, gw.URL, "s1", "M1003", "PS1003", "annual_cancel")
	b := q.body
	if q.status != 200 || b["reason"] != "annual_cancel" || b["eligible"] != true || b["refund_amount"].(float64) != 107910 ||
		b["approval_required"] != true || b["handoff_required"] != false || b["last_usable_date"] != "2026-10-13" ||
		b["customer_id"] != "M1003" || b["subscription_id"] != "PS1003" || b["request_date"] != "2026-10-13" {
		t.Fatalf("%+v", b)
	}
	if !slices.Equal(toStrings(b["article_ids"]), []string{"B-08", "B-15"}) || b["quote_id"] == "" {
		t.Fatalf("%+v", b)
	}
	line := b["refunds"].([]any)[0].(map[string]any)
	if line["payment_id"] != "CH1003-1" || line["amount"].(float64) != 107910 || line["refund_method"] != "card_cancel" ||
		line["payment_method"] != "card" || line["account_required"] != false || line["expected_period"] == "" {
		t.Fatalf("%+v", line)
	}
	if b["breakdown"].(map[string]any)["prorate"].(map[string]any)["remaining_days"].(float64) != 331 {
		t.Fatalf("breakdown: %+v", b["breakdown"])
	}
	// 중복 결제: 이용권 유지(이용 마지막 날 없음), 날짜가 다른 중복
	d := quoteOf(t, gw.URL, "s1", "M1006", "PS1006", "duplicate").body
	if d["eligible"] != true || d["refund_amount"].(float64) != 11900 || d["last_usable_date"] != nil {
		t.Fatalf("%+v", d)
	}
}

func toStrings(v any) []string {
	var out []string
	for _, x := range v.([]any) {
		out = append(out, x.(string))
	}
	return out
}

func TestErrorTranslation(t *testing.T) {
	gw, _ := stack(t)
	cases := []struct {
		name, cid, sub, reason, code, handoff string
		status                                int
		article                               string
	}{
		{"열람 있음 → NOT_REFUNDABLE → NOT_ELIGIBLE", "M1002", "PS1002", "withdrawal", "NOT_ELIGIBLE", "", 422, "B-06"},
		{"환불 반복 → NEEDS_AGENT → HANDOFF_REQUIRED", "M1009", "PS1009", "withdrawal", "HANDOFF_REQUIRED", "repeat_refund", 422, "B-15"},
		{"이의 제기 → NEEDS_AGENT(CHARGEBACK) → dispute", "M1008", "PS1008", "annual_cancel", "HANDOFF_REQUIRED", "dispute", 422, "B-15"},
		{"월 이용권 일할 → NOT_ELIGIBLE", "M1002", "PS1002", "annual_cancel", "NOT_ELIGIBLE", "", 422, "B-08"},
		{"없는 회원 → NO_SUCH_RESOURCE → NOT_FOUND", "M9999", "PS1001", "withdrawal", "NOT_FOUND", "", 404, ""},
		{"조직 B에 없는 사유", "M1010", "PS1010", "incident", "NOT_ELIGIBLE", "", 422, "B-09"},
		{"모르는 사유", "M1010", "PS1010", "foo", "INVALID_REQUEST", "", 400, ""},
	}
	for _, c := range cases {
		r := quoteOf(t, gw.URL, "s1", c.cid, c.sub, c.reason)
		if r.status != c.status || r.code() != c.code {
			t.Errorf("%s: %d %+v", c.name, r.status, r.body)
			continue
		}
		if c.article != "" && !slices.Contains(toStrings(r.err()["article_ids"]), c.article) {
			t.Errorf("%s: 근거 조항 %v", c.name, r.err()["article_ids"])
		}
		if c.code == "HANDOFF_REQUIRED" || (c.code == "NOT_ELIGIBLE" && c.reason != "incident") {
			q, ok := r.err()["quote"].(map[string]any)
			if !ok || q["reason"] != c.reason {
				t.Errorf("%s: error.quote %+v", c.name, r.err())
				continue
			}
			if c.handoff != "" && (q["handoff_required"] != true || q["handoff_reason"] != c.handoff) {
				t.Errorf("%s: %+v", c.name, q)
			}
		}
	}
}

// 조직 B의 모든 오류 코드가 공통 코드로 바뀐다.
func TestEveryOrgBCodeTranslated(t *testing.T) {
	codes := []string{orgbsrv.ErrBadInput, orgbsrv.ErrAPIKeyInvalid, orgbsrv.ErrAuthRequired, orgbsrv.ErrAuthLocked,
		orgbsrv.ErrNoSuchResource, orgbsrv.ErrAlreadyDone, orgbsrv.ErrIdempotencyMismatch, orgbsrv.ErrNotRefundable,
		orgbsrv.ErrNeedsAgent, orgbsrv.ErrBankAccountMissing, orgbsrv.ErrServerError, orgbsrv.ErrMaintenance,
		orgbsrv.ErrUpstreamTimeout}
	common := []string{codeIdentityRequired, codeNotEligible, codeHandoffRequired, codeTimeout, codeUnavailable,
		codeDuplicateRequest, codeInvalidRequest, codeNotFound, codeAccountRequired, codeInternal}
	for _, c := range codes {
		if to, ok := translateCode[c]; !ok || !slices.Contains(common, to) {
			t.Errorf("%s → %q", c, to)
		}
	}
	if len(translateCode) != len(codes) {
		t.Errorf("변환표 %d개, 조직 B 코드 %d개", len(translateCode), len(codes))
	}
}

func TestWritesUseSessionTokenAndIdempotencyKey(t *testing.T) {
	gw, _ := stack(t)
	q := quoteOf(t, gw.URL, "w1", "M1001", "PS1001", "withdrawal").body
	refund := map[string]string{"customer_id": "M1001", "payment_id": "CH1001-2", "quote_id": q["quote_id"].(string),
		"idempotency_key": "idem-1"}
	if r := callTool(t, gw.URL, "w1", "create_refund_request", refund); r.status != 403 || r.code() != "IDENTITY_REQUIRED" {
		t.Fatalf("본인 확인 전: %+v", r)
	}
	verified(t, gw.URL, "w1", "01050000101", "000101")
	first := callTool(t, gw.URL, "w1", "create_refund_request", refund)
	again := callTool(t, gw.URL, "w1", "create_refund_request", refund)
	b := first.body
	if first.status != 200 || b["status"] != "confirmed" || b["reason"] != "withdrawal" || b["amount"].(float64) != 11900 ||
		b["payment_id"] != "CH1001-2" || b["subscription_id"] != "PS1001" || b["refund_method"] != "card_cancel" ||
		b["last_usable_date"] != "2026-10-13" || b["refund_id"] != again.body["refund_id"] {
		t.Fatalf("%+v / %+v", first, again)
	}
	// 같은 대상, 다른 키 → DUPLICATE_REQUEST와 공통 접수 내역
	refund["idempotency_key"] = "idem-2"
	dup := callTool(t, gw.URL, "w1", "create_refund_request", refund)
	if dup.status != 409 || dup.code() != "DUPLICATE_REQUEST" || dup.err()["receipt"].(map[string]any)["refund_id"] != b["refund_id"] {
		t.Fatalf("%+v", dup)
	}
	// 같은 키, 다른 내용 → INVALID_REQUEST
	refund["idempotency_key"], refund["payment_id"] = "idem-1", "CH1001-1"
	if r := callTool(t, gw.URL, "w1", "create_refund_request", refund); r.code() != "INVALID_REQUEST" {
		t.Fatalf("%+v", r)
	}
	// 다른 세션은 토큰이 없다
	cancel := map[string]string{"customer_id": "M1001", "subscription_id": "PS1001", "idempotency_key": "c-1"}
	if r := callTool(t, gw.URL, "other", "create_cancellation", cancel); r.code() != "IDENTITY_REQUIRED" {
		t.Fatalf("%+v", r)
	}
	cn := callTool(t, gw.URL, "w1", "create_cancellation", cancel).body
	if cn["last_usable_date"] != "2026-10-13" || !slices.Equal(toStrings(cn["cancelled_subscription_ids"]), []string{"PS1001"}) {
		t.Fatalf("%+v", cn)
	}
	s := callTool(t, gw.URL, "w1", "list_subscriptions", map[string]string{"customer_id": "M1001"}).body["subscriptions"].([]any)[0].(map[string]any)
	if s["status"] != "cancelled" || s["last_usable_date"] != "2026-10-13" {
		t.Fatalf("%+v", s)
	}
	p := callTool(t, gw.URL, "w1", "list_payments", map[string]string{"customer_id": "M1001"}).body["payments"].([]any)[1].(map[string]any)
	if p["refund_status"] != "confirmed" || p["refund_id"] != b["refund_id"] {
		t.Fatalf("%+v", p)
	}
}

func TestApprovalAndFamilyCancel(t *testing.T) {
	gw, _ := stack(t)
	verified(t, gw.URL, "a1", "010-5000-0103", "000103")
	q := quoteOf(t, gw.URL, "a1", "M1003", "PS1003", "annual_cancel").body
	r := callTool(t, gw.URL, "a1", "create_refund_request", map[string]string{"customer_id": "M1003", "payment_id": "CH1003-1",
		"quote_id": q["quote_id"].(string), "idempotency_key": "k"}).body
	if r["status"] != "pending_approval" || r["amount"].(float64) != 107910 {
		t.Fatalf("%+v", r)
	}
	verified(t, gw.URL, "f1", "010-5000-0105", "000105")
	c := callTool(t, gw.URL, "f1", "create_cancellation", map[string]string{"customer_id": "M1005", "subscription_id": "PS1005",
		"idempotency_key": "k"}).body
	if c["last_usable_date"] != "2026-11-03" || c["family_members_ended"].(float64) != 2 ||
		!slices.Contains(toStrings(c["article_ids"]), "B-12") {
		t.Fatalf("%+v", c)
	}
}

func TestAccountFlow(t *testing.T) {
	gw, _ := stack(t)
	verified(t, gw.URL, "p1", "010-5000-0107", "000107")
	q := quoteOf(t, gw.URL, "p1", "M1007", "PS1007", "duplicate").body
	line := q["refunds"].([]any)[0].(map[string]any)
	if line["account_required"] != true || line["refund_method"] != "bank_transfer" || line["payment_method"] != "mobile" {
		t.Fatalf("%+v", line)
	}
	refund := map[string]string{"customer_id": "M1007", "payment_id": "CH1007-2", "quote_id": q["quote_id"].(string), "idempotency_key": "r"}
	if r := callTool(t, gw.URL, "p1", "create_refund_request", refund); r.status != 422 || r.code() != "ACCOUNT_REQUIRED" {
		t.Fatalf("%+v", r)
	}
	acc := map[string]string{"customer_id": "M1007", "bank": "책솔은행", "account": "123-456-789", "holder": "권민수", "idempotency_key": "a"}
	if r := callTool(t, gw.URL, "p1", "register_refund_account", acc).body; r["registered"] != false || r["reject_reason"] != "holder_not_customer" {
		t.Fatalf("%+v", r)
	}
	acc["holder"] = "권나연"
	if r := callTool(t, gw.URL, "p1", "register_refund_account", acc).body; r["registered"] != true || r["account_masked"] != "***-***-789" || r["registration_id"] == nil {
		t.Fatalf("%+v", r)
	}
	acc["idempotency_key"] = "a2"
	if r := callTool(t, gw.URL, "p1", "register_refund_account", acc); r.code() != "DUPLICATE_REQUEST" || r.err()["receipt"].(map[string]any)["registered"] != true {
		t.Fatalf("%+v", r)
	}
	if r := callTool(t, gw.URL, "p1", "create_refund_request", refund).body; r["refund_method"] != "bank_transfer" || r["status"] != "confirmed" {
		t.Fatalf("%+v", r)
	}
}

func TestVerifyLockMapsToHandoff(t *testing.T) {
	gw, _ := stack(t)
	for i := 1; i <= 3; i++ {
		r := callTool(t, gw.URL, "v1", "verify_identity", map[string]string{"phone": "010-5000-0101", "otp": "0101"})
		if r.body["verified"] != false || r.body["customer_id"] != "" || r.body["attempts"].(float64) != float64(i) ||
			r.body["failed_attempts"].(float64) != float64(i) || (r.body["handoff_required"] == true) != (i == 3) {
			t.Fatalf("%d: %+v", i, r.body)
		}
		if i == 3 && !slices.Equal(toStrings(r.body["article_ids"]), []string{"B-15"}) {
			t.Fatalf("%+v", r.body)
		}
	}
	if r := callTool(t, gw.URL, "v1", "verify_identity", map[string]string{"phone": "010-5000-0101", "otp": "000101"}); r.code() != "HANDOFF_REQUIRED" {
		t.Fatalf("%+v", r)
	}
	h := callTool(t, gw.URL, "v1", "create_handoff", map[string]string{"reason": "identity_failed", "summary": "본인 확인 3회 실패"}).body
	if h["handoff_id"] == nil || h["expected_wait_minutes"].(float64) != 7 || h["reason"] != "identity_failed" {
		t.Fatalf("%+v", h)
	}
}

func TestCommonContractStrictness(t *testing.T) {
	gw, _ := stack(t)
	if r := callTool(t, gw.URL, "", "get_customer", map[string]string{"customer_id": "M1001"}); r.code() != "INVALID_REQUEST" {
		t.Fatalf("세션 헤더 없음: %+v", r)
	}
	if r := callTool(t, gw.URL, "s", "get_customer", map[string]string{"customer_id": "M1001", "member_no": "M1001"}); r.code() != "INVALID_REQUEST" {
		t.Fatalf("모르는 필드: %+v", r)
	}
	if r := callTool(t, gw.URL, "s", "nope", map[string]string{}); r.status != 404 || r.code() != "NOT_FOUND" {
		t.Fatalf("%+v", r)
	}
	resp, _ := http.Get(gw.URL + "/v1/get_customer")
	resp.Body.Close()
	if resp.StatusCode != 405 {
		t.Fatalf("GET: %d", resp.StatusCode)
	}
}

func TestFaultsForwardedAndTranslated(t *testing.T) {
	gw, _ := stack(t)
	if s := post(t, gw.URL+"/_admin/faults", map[string]any{"tool": "quote_refund", "code": "TIMEOUT", "count": 1}); s != 200 {
		t.Fatalf("%d", s)
	}
	if r := quoteOf(t, gw.URL, "f", "M1001", "PS1001", "withdrawal"); r.status != 504 || r.code() != "TIMEOUT" {
		t.Fatalf("%+v", r)
	}
	if r := quoteOf(t, gw.URL, "f", "M1001", "PS1001", "withdrawal"); r.status != 200 {
		t.Fatalf("장애는 한 번만: %+v", r)
	}
	post(t, gw.URL+"/_admin/faults", map[string]any{"tool": "list_payments", "code": "UNAVAILABLE"})
	if r := callTool(t, gw.URL, "f", "list_payments", map[string]string{"customer_id": "M1001"}); r.status != 503 || r.code() != "UNAVAILABLE" {
		t.Fatalf("%+v", r)
	}
	if s := post(t, gw.URL+"/_admin/faults", map[string]any{"tool": "nope", "code": "TIMEOUT"}); s != 400 {
		t.Fatalf("%d", s)
	}
	if s := post(t, gw.URL+"/_admin/faults", map[string]any{"tool": "quote_refund", "code": "INTERNAL"}); s != 400 {
		t.Fatalf("%d", s)
	}
}

func TestResetClearsTokensOnBothSides(t *testing.T) {
	gw, _ := stack(t)
	verified(t, gw.URL, "r1", "010-5000-0102", "000102")
	if s := post(t, gw.URL+"/_admin/reset", nil); s != 200 {
		t.Fatalf("%d", s)
	}
	r := callTool(t, gw.URL, "r1", "create_cancellation", map[string]string{"customer_id": "M1002", "subscription_id": "PS1002", "idempotency_key": "k"})
	if r.code() != "IDENTITY_REQUIRED" {
		t.Fatalf("%+v", r)
	}
}

func TestBackendFailuresBecomeCommonCodes(t *testing.T) {
	// 시스템 키가 틀리면 조직 B는 API_KEY_INVALID → INTERNAL(엔진은 일시 장애로 다룬다)
	_, backend := stack(t)
	bad := httptest.NewServer(New(Config{BackendURL: backend.URL, APIKey: "wrong", Timeout: time.Second}, quiet))
	defer bad.Close()
	if r := callTool(t, bad.URL, "s", "get_customer", map[string]string{"customer_id": "M1001"}); r.status != 500 || r.code() != "INTERNAL" {
		t.Fatalf("%+v", r)
	}
	// 조직 B가 내려가 있으면 UNAVAILABLE
	down := httptest.NewServer(http.NotFoundHandler())
	downURL := down.URL
	down.Close()
	gw := httptest.NewServer(New(Config{BackendURL: downURL, APIKey: key, Timeout: time.Second}, quiet))
	defer gw.Close()
	if r := callTool(t, gw.URL, "s", "get_customer", map[string]string{"customer_id": "M1001"}); r.status != 503 || r.code() != "UNAVAILABLE" {
		t.Fatalf("%+v", r)
	}
	// 제한 시간을 넘기면 TIMEOUT
	slow := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { time.Sleep(300 * time.Millisecond) }))
	defer slow.Close()
	gw2 := httptest.NewServer(New(Config{BackendURL: slow.URL, APIKey: key, Timeout: 50 * time.Millisecond}, quiet))
	defer gw2.Close()
	if r := callTool(t, gw2.URL, "s", "get_customer", map[string]string{"customer_id": "M1001"}); r.status != 504 || r.code() != "TIMEOUT" {
		t.Fatalf("%+v", r)
	}
	// 조직 B 봉투가 아닌 응답은 INTERNAL
	odd := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte("<html>")) }))
	defer odd.Close()
	gw3 := httptest.NewServer(New(Config{BackendURL: odd.URL, APIKey: key, Timeout: time.Second}, quiet))
	defer gw3.Close()
	if r := callTool(t, gw3.URL, "s", "get_customer", map[string]string{"customer_id": "M1001"}); r.code() != "INTERNAL" {
		t.Fatalf("%+v", r)
	}
}
