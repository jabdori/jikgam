package server

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"slices"
	"strings"
	"testing"

	"github.com/jabdori/jikgam/bizapi/internal/orga"
)

const (
	dataPath      = "../../../domain/org_a/customers.json"
	scenariosPath = "../../../data/scenarios/org_a.jsonl"
)

func newTestServer(t *testing.T, logOut io.Writer) *httptest.Server {
	t.Helper()
	ds, err := orga.Load(dataPath)
	if err != nil {
		t.Fatalf("고객 데이터 읽기: %v", err)
	}
	if logOut == nil {
		logOut = io.Discard
	}
	ts := httptest.NewServer(New(ds, slog.New(slog.NewJSONHandler(logOut, nil))))
	t.Cleanup(ts.Close)
	return ts
}

// response는 한 번의 호출 결과다. raw는 응답 본문 그대로다.
type response struct {
	status int
	raw    []byte
}

func (r response) into(t *testing.T, v any) {
	t.Helper()
	if err := json.Unmarshal(r.raw, v); err != nil {
		t.Fatalf("응답 해석 실패: %v\n%s", err, r.raw)
	}
}

type errorBody struct {
	Error struct {
		Code       string          `json:"code"`
		Message    string          `json:"message"`
		ArticleIDs []string        `json:"article_ids"`
		Quote      *orga.Quote     `json:"quote"`
		Receipt    json.RawMessage `json:"receipt"`
	} `json:"error"`
}

func (r response) errBody(t *testing.T) errorBody {
	t.Helper()
	var e errorBody
	r.into(t, &e)
	return e
}

func post(t *testing.T, ts *httptest.Server, path, sid string, body any) response {
	t.Helper()
	b, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	req, err := http.NewRequest(http.MethodPost, ts.URL+path, bytes.NewReader(b))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	if sid != "" {
		req.Header.Set("X-Session-ID", sid)
	}
	res, err := ts.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	raw, err := io.ReadAll(res.Body)
	if err != nil {
		t.Fatal(err)
	}
	return response{status: res.StatusCode, raw: raw}
}

func tool(t *testing.T, ts *httptest.Server, sid, name string, body any) response {
	t.Helper()
	return post(t, ts, "/v1/"+name, sid, body)
}

func expectOK(t *testing.T, r response, v any) {
	t.Helper()
	if r.status != http.StatusOK {
		t.Fatalf("status %d, want 200: %s", r.status, r.raw)
	}
	if v != nil {
		r.into(t, v)
	}
}

func expectError(t *testing.T, r response, status int, code string) errorBody {
	t.Helper()
	if r.status != status {
		t.Fatalf("status %d, want %d: %s", r.status, status, r.raw)
	}
	e := r.errBody(t)
	if e.Error.Code != code {
		t.Fatalf("code %s, want %s: %s", e.Error.Code, code, r.raw)
	}
	return e
}

func verify(t *testing.T, ts *httptest.Server, sid, customerID string) {
	t.Helper()
	phone := "010-0000-0" + customerID[1:] // 가상 고객 C0NN의 번호는 010-0000-00NN
	var v verifyResponse
	expectOK(t, tool(t, ts, sid, "verify_identity", map[string]string{"phone": phone, "otp": otpFor(phone)}), &v)
	if !v.Verified || v.CustomerID != customerID {
		t.Fatalf("본인 확인 실패: %+v", v)
	}
}

func otpFor(phone string) string {
	digits := orga.NormalizePhone(phone)
	return digits[len(digits)-4:]
}

func quote(t *testing.T, ts *httptest.Server, sid, customer, sub, reason string) *orga.Quote {
	t.Helper()
	var q orga.Quote
	expectOK(t, tool(t, ts, sid, "quote_refund", map[string]string{
		"customer_id": customer, "subscription_id": sub, "reason": reason, "request_date": "2026-10-12",
	}), &q)
	return &q
}

// TestFlowAnnualCancel은 본인 확인 → 견적 → 환불 접수 → 해지의 전체 흐름을 HTTP로 확인한다(C003, 시나리오 s03).
func TestFlowAnnualCancel(t *testing.T) {
	ts := newTestServer(t, nil)
	const sid = "flow-c003"
	verify(t, ts, sid, "C003")

	q := quote(t, ts, sid, "C003", "S003", "annual_cancel")
	if q.RefundAmount != 62370 || !q.ApprovalRequired || q.QuoteID == "" {
		t.Fatalf("견적 = %+v", q)
	}

	var rr RefundReceipt
	expectOK(t, tool(t, ts, sid, "create_refund_request", map[string]string{
		"customer_id": "C003", "payment_id": "P003", "quote_id": q.QuoteID, "idempotency_key": "k-refund",
	}), &rr)
	if rr.Amount != 62370 || rr.Status != refundPendingApproval || rr.RefundMethod != orga.RefundCardCancel {
		t.Errorf("환불 접수 = %+v", rr)
	}
	if rr.LastUsableDate == nil || rr.LastUsableDate.String() != "2026-10-12" {
		t.Errorf("환불 접수의 이용 마지막 날 = %v", rr.LastUsableDate)
	}

	var cr CancellationReceipt
	expectOK(t, tool(t, ts, sid, "create_cancellation", map[string]string{
		"customer_id": "C003", "subscription_id": "S003", "idempotency_key": "k-cancel",
	}), &cr)
	if cr.LastUsableDate.String() != "2026-10-12" || !slices.Contains(cr.ArticleIDs, "A-08") {
		t.Errorf("해지 접수 = %+v", cr)
	}

	var subs struct {
		Subscriptions []subscriptionView `json:"subscriptions"`
	}
	expectOK(t, tool(t, ts, sid, "list_subscriptions", map[string]string{"customer_id": "C003"}), &subs)
	if s := subs.Subscriptions[0]; s.Status != "cancelled" || s.LastUsableDate.String() != "2026-10-12" {
		t.Errorf("구독 상태 = %+v", s)
	}
	var pays struct {
		Payments []paymentView `json:"payments"`
	}
	expectOK(t, tool(t, ts, sid, "list_payments", map[string]string{"customer_id": "C003"}), &pays)
	if p := pays.Payments[0]; p.RefundID != rr.RefundID || p.RefundStatus != refundPendingApproval {
		t.Errorf("결제 환불 상태 = %+v", p)
	}
}

func TestIdentityRequired(t *testing.T) {
	ts := newTestServer(t, nil)
	writes := []struct {
		name string
		body map[string]string
	}{
		{"create_cancellation", map[string]string{"customer_id": "C001", "subscription_id": "S001", "idempotency_key": "k1"}},
		{"create_refund_request", map[string]string{"customer_id": "C001", "payment_id": "P001", "quote_id": "Q-x", "idempotency_key": "k2"}},
		{"register_refund_account", map[string]string{"customer_id": "C001", "bank": "별담은행", "account": "111-222-333", "holder": "김하늘", "idempotency_key": "k3"}},
	}
	for _, w := range writes {
		e := expectError(t, tool(t, ts, "s-none", w.name, w.body), http.StatusForbidden, codeIdentityRequired)
		if !slices.Contains(e.Error.ArticleIDs, "A-15") {
			t.Errorf("%s: 근거 조항 %v", w.name, e.Error.ArticleIDs)
		}
	}
	// 다른 고객으로 본인 확인한 세션, 본인 확인한 세션과 다른 세션도 거절한다.
	verify(t, ts, "s-c003", "C003")
	expectError(t, tool(t, ts, "s-c003", "create_cancellation", writes[0].body), http.StatusForbidden, codeIdentityRequired)
	verify(t, ts, "s-c001", "C001")
	expectError(t, tool(t, ts, "s-other", "create_cancellation", writes[0].body), http.StatusForbidden, codeIdentityRequired)
	expectOK(t, tool(t, ts, "s-c001", "create_cancellation", writes[0].body), nil)
	// 상담사 인계는 본인 확인 없이 접수한다(제3자 문의, 본인 확인 실패).
	expectOK(t, tool(t, ts, "s-none", "create_handoff", map[string]string{"customer_id": "C009", "reason": "minor"}), nil)
}

func TestIdempotency(t *testing.T) {
	ts := newTestServer(t, nil)
	const sid = "idem"
	verify(t, ts, sid, "C006")
	q := quote(t, ts, sid, "C006", "S006", "duplicate")
	body := map[string]string{"customer_id": "C006", "payment_id": "P006b", "quote_id": q.QuoteID, "idempotency_key": "same-key"}

	first := tool(t, ts, sid, "create_refund_request", body)
	expectOK(t, first, nil)
	second := tool(t, ts, sid, "create_refund_request", body)
	expectOK(t, second, nil)
	if !bytes.Equal(first.raw, second.raw) {
		t.Errorf("같은 키의 응답이 다르다:\n%s\n%s", first.raw, second.raw)
	}

	// 같은 대상에 다른 키 → DUPLICATE_REQUEST와 기존 접수 내역.
	other := map[string]string{"customer_id": "C006", "payment_id": "P006b", "quote_id": q.QuoteID, "idempotency_key": "other-key"}
	e := expectError(t, tool(t, ts, sid, "create_refund_request", other), http.StatusConflict, codeDuplicateRequest)
	var orig, dup RefundReceipt
	first.into(t, &orig)
	if err := json.Unmarshal(e.Error.Receipt, &dup); err != nil || dup.RefundID != orig.RefundID {
		t.Errorf("기존 접수 내역 = %s (err %v), want %s", e.Error.Receipt, err, orig.RefundID)
	}

	// 같은 키를 다른 내용에 다시 쓰면 거절한다.
	reused := map[string]string{"customer_id": "C006", "payment_id": "P006a", "quote_id": q.QuoteID, "idempotency_key": "same-key"}
	expectError(t, tool(t, ts, sid, "create_refund_request", reused), http.StatusBadRequest, codeInvalidRequest)

	// 해지도 같은 규칙을 따른다.
	cancel := map[string]string{"customer_id": "C006", "subscription_id": "S006", "idempotency_key": "c1"}
	c1 := tool(t, ts, sid, "create_cancellation", cancel)
	expectOK(t, c1, nil)
	c2 := tool(t, ts, sid, "create_cancellation", cancel)
	if !bytes.Equal(c1.raw, c2.raw) {
		t.Errorf("해지 재응답이 다르다")
	}
	cancel["idempotency_key"] = "c2"
	expectError(t, tool(t, ts, sid, "create_cancellation", cancel), http.StatusConflict, codeDuplicateRequest)
}

// TestIdempotentRetryAfterFault는 일시 장애 후 같은 키로 재시도하면 한 번만 접수되는지 본다.
func TestIdempotentRetryAfterFault(t *testing.T) {
	ts := newTestServer(t, nil)
	const sid = "retry"
	verify(t, ts, sid, "C001")
	q := quote(t, ts, sid, "C001", "S001", "withdrawal")
	expectOK(t, post(t, ts, "/_admin/faults", "", map[string]any{"tool": "create_refund_request", "code": "UNAVAILABLE", "count": 1}), nil)
	body := map[string]string{"customer_id": "C001", "payment_id": "P001", "quote_id": q.QuoteID, "idempotency_key": "r1"}
	expectError(t, tool(t, ts, sid, "create_refund_request", body), http.StatusServiceUnavailable, codeUnavailable)
	var rr RefundReceipt
	expectOK(t, tool(t, ts, sid, "create_refund_request", body), &rr)
	if rr.RefundID != "RF-000001" || rr.Status != refundConfirmed || rr.Amount != 9900 {
		t.Errorf("재시도 접수 = %+v", rr)
	}
}

func TestRefundAccountHolder(t *testing.T) {
	ts := newTestServer(t, nil)
	const sid = "acct"
	verify(t, ts, sid, "C008")
	q := quote(t, ts, sid, "C008", "S008", "duplicate")
	if l := q.Refunds[0]; l.PaymentID != "P008b" || !l.AccountRequired || l.RefundMethod != orga.RefundBankTransfer {
		t.Fatalf("견적 환불 내역 = %+v", l)
	}
	refund := map[string]string{"customer_id": "C008", "payment_id": "P008b", "quote_id": q.QuoteID, "idempotency_key": "r1"}
	expectError(t, tool(t, ts, sid, "create_refund_request", refund), http.StatusUnprocessableEntity, codeAccountRequired)

	// 아내 명의 계좌는 거절하고 저장하지 않는다.
	acct := map[string]string{"customer_id": "C008", "bank": "별담은행", "account": "111-222-333", "holder": "김아내", "idempotency_key": "a1"}
	var rej AccountReceipt
	expectOK(t, tool(t, ts, sid, "register_refund_account", acct), &rej)
	if rej.Registered || rej.RejectReason != "holder_not_customer" || rej.RegistrationID != "" {
		t.Errorf("거절 결과 = %+v", rej)
	}
	expectError(t, tool(t, ts, sid, "create_refund_request", refund), http.StatusUnprocessableEntity, codeAccountRequired)

	// 거절은 멱등 결과로 남지 않으므로 같은 키로 본인 계좌를 다시 보낼 수 있다.
	acct["account"], acct["holder"] = "444-555-666", "임태윤"
	var ok AccountReceipt
	expectOK(t, tool(t, ts, sid, "register_refund_account", acct), &ok)
	if !ok.Registered || ok.AccountMasked != "***-***-666" {
		t.Errorf("등록 결과 = %+v", ok)
	}
	acct["idempotency_key"] = "a2"
	expectError(t, tool(t, ts, sid, "register_refund_account", acct), http.StatusConflict, codeDuplicateRequest)

	var rr RefundReceipt
	expectOK(t, tool(t, ts, sid, "create_refund_request", refund), &rr)
	if rr.RefundMethod != orga.RefundBankTransfer || rr.Status != refundConfirmed || rr.LastUsableDate != nil {
		t.Errorf("환불 접수 = %+v", rr)
	}
}

func TestFaultInjection(t *testing.T) {
	ts := newTestServer(t, nil)
	const sid = "fault"
	body := map[string]string{"customer_id": "C004", "subscription_id": "S004", "reason": "annual_cancel", "request_date": "2026-10-12"}
	expectOK(t, post(t, ts, "/_admin/faults", "", map[string]any{"tool": "quote_refund", "code": "TIMEOUT", "count": 2}), nil)
	expectError(t, tool(t, ts, sid, "quote_refund", body), http.StatusGatewayTimeout, codeTimeout)
	expectError(t, tool(t, ts, sid, "quote_refund", body), http.StatusGatewayTimeout, codeTimeout)
	var q orga.Quote
	expectOK(t, tool(t, ts, sid, "quote_refund", body), &q)
	if q.RefundAmount != 53280 {
		t.Errorf("장애 해제 후 견적 = %d", q.RefundAmount)
	}

	expectOK(t, post(t, ts, "/_admin/faults", "", map[string]any{"tool": "get_customer", "code": "UNAVAILABLE"}), nil)
	expectError(t, tool(t, ts, sid, "get_customer", map[string]string{"customer_id": "C001"}), http.StatusServiceUnavailable, codeUnavailable)
	expectOK(t, tool(t, ts, sid, "get_customer", map[string]string{"customer_id": "C001"}), nil)

	expectError(t, post(t, ts, "/_admin/faults", "", map[string]any{"tool": "quote_refund", "code": "NOT_ELIGIBLE"}), http.StatusBadRequest, codeInvalidRequest)
	expectError(t, post(t, ts, "/_admin/faults", "", map[string]any{"tool": "no_such_tool", "code": "TIMEOUT"}), http.StatusBadRequest, codeInvalidRequest)

	// reset은 장애 주입과 세션을 지운다.
	expectOK(t, post(t, ts, "/_admin/faults", "", map[string]any{"tool": "get_customer", "code": "TIMEOUT", "count": 5}), nil)
	verify(t, ts, sid, "C001")
	expectOK(t, post(t, ts, "/_admin/reset", "", map[string]any{}), nil)
	expectOK(t, tool(t, ts, sid, "get_customer", map[string]string{"customer_id": "C001"}), nil)
	expectError(t, tool(t, ts, sid, "create_cancellation", map[string]string{"customer_id": "C001", "subscription_id": "S001", "idempotency_key": "k"}),
		http.StatusForbidden, codeIdentityRequired)
}

// TestIdentityAttempts는 본인 확인 2회 실패 시 인계로 가는지 본다(제19조 제1항, 시나리오 s18).
func TestIdentityAttempts(t *testing.T) {
	ts := newTestServer(t, nil)
	const sid = "attempts"
	wrong := map[string]string{"phone": "010-0000-0005", "otp": "9999"}

	var v verifyResponse
	expectOK(t, tool(t, ts, sid, "verify_identity", wrong), &v)
	if v.Verified || v.CustomerID != "" || v.Attempts != 1 || v.FailedAttempts != 1 || v.HandoffRequired {
		t.Errorf("1회 실패 = %+v", v)
	}
	expectOK(t, tool(t, ts, sid, "verify_identity", wrong), &v)
	if v.Verified || v.Attempts != 2 || v.FailedAttempts != 2 || !v.HandoffRequired {
		t.Errorf("2회 실패 = %+v", v)
	}
	// 세 번째는 맞는 번호여도 받지 않는다.
	right := map[string]string{"phone": "010-0000-0005", "otp": "0005"}
	expectError(t, tool(t, ts, sid, "verify_identity", right), http.StatusUnprocessableEntity, codeHandoffRequired)
	expectError(t, tool(t, ts, sid, "create_cancellation", map[string]string{"customer_id": "C005", "subscription_id": "S005", "idempotency_key": "k"}),
		http.StatusUnprocessableEntity, codeHandoffRequired)
	var h HandoffReceipt
	expectOK(t, tool(t, ts, sid, "create_handoff", map[string]string{"customer_id": "C005", "reason": "identity_failed"}), &h)
	if h.HandoffID == "" || h.ExpectedWaitMinutes <= 0 {
		t.Errorf("인계 접수 = %+v", h)
	}

	// 다른 세션: 등록되지 않은 번호도 실패로 센다(s25). 한 번 실패 후 성공하면 확인된다.
	expectOK(t, tool(t, ts, "other", "verify_identity", map[string]string{"phone": "010-0000-0099", "otp": "0099"}), &v)
	if v.Verified || v.FailedAttempts != 1 {
		t.Errorf("없는 번호 = %+v", v)
	}
	expectOK(t, tool(t, ts, "other", "verify_identity", right), &v)
	if !v.Verified || v.CustomerID != "C005" || v.Attempts != 2 || v.HandoffRequired {
		t.Errorf("실패 후 성공 = %+v", v)
	}
}

func TestQuoteErrors(t *testing.T) {
	ts := newTestServer(t, nil)
	const sid = "qerr"
	q := func(customer, sub, reason string) response {
		return tool(t, ts, sid, "quote_refund", map[string]string{"customer_id": customer, "subscription_id": sub, "reason": reason, "request_date": "2026-10-12"})
	}
	e := expectError(t, q("C011", "S011", "annual_cancel"), http.StatusUnprocessableEntity, codeNotEligible)
	if !slices.Contains(e.Error.ArticleIDs, "A-16") || e.Error.Quote == nil || e.Error.Quote.QuoteID != "" || e.Error.Quote.Eligible {
		t.Errorf("NOT_ELIGIBLE 본문 = %+v", e.Error)
	}
	e = expectError(t, q("C015", "S015", "withdrawal"), http.StatusUnprocessableEntity, codeHandoffRequired)
	if e.Error.Quote == nil || e.Error.Quote.HandoffReason != "dispute" || !e.Error.Quote.HandoffRequired {
		t.Errorf("HANDOFF_REQUIRED 본문 = %+v", e.Error)
	}
	e = expectError(t, q("C009", "S009", "minor"), http.StatusUnprocessableEntity, codeHandoffRequired)
	if e.Error.Quote.HandoffReason != "minor" {
		t.Errorf("미성년자 인계 사유 = %s", e.Error.Quote.HandoffReason)
	}
	expectError(t, q("C001", "S001", "refund"), http.StatusBadRequest, codeInvalidRequest)
	expectError(t, q("C999", "S001", "withdrawal"), http.StatusNotFound, codeNotFound)
	expectError(t, tool(t, ts, sid, "quote_refund", map[string]string{"customer_id": "C001", "subscription_id": "S001", "reason": "withdrawal"}),
		http.StatusBadRequest, codeInvalidRequest)
	expectError(t, tool(t, ts, sid, "quote_refund", map[string]string{"customer_id": "C001", "subscription_id": "S001", "reason": "withdrawal", "request_date": "2026-10-12", "amount": "9900"}),
		http.StatusBadRequest, codeInvalidRequest)

	// 같은 입력이면 같은 견적 ID다.
	a, b := quote(t, ts, sid, "C001", "S001", "withdrawal"), quote(t, ts, "another", "C001", "S001", "withdrawal")
	if a.QuoteID != b.QuoteID {
		t.Errorf("견적 ID가 다르다: %s, %s", a.QuoteID, b.QuoteID)
	}
	// 0원 견적과 요금제 변경 견적은 환불 접수할 수 없다.
	verify(t, ts, sid, "C016")
	pc := quote(t, ts, sid, "C016", "S016", "plan_change")
	if pc.RefundAmount != 6380 || len(pc.Refunds) != 0 {
		t.Errorf("요금제 변경 견적 = %+v", pc)
	}
	expectError(t, tool(t, ts, sid, "create_refund_request", map[string]string{"customer_id": "C016", "payment_id": "P016", "quote_id": pc.QuoteID, "idempotency_key": "x"}),
		http.StatusUnprocessableEntity, codeNotEligible)
}

func TestTransportErrors(t *testing.T) {
	ts := newTestServer(t, nil)
	expectError(t, tool(t, ts, "", "get_customer", map[string]string{"customer_id": "C001"}), http.StatusBadRequest, codeInvalidRequest)
	expectError(t, tool(t, ts, "s", "no_such_tool", map[string]string{}), http.StatusNotFound, codeNotFound)
	expectError(t, post(t, ts, "/v2/get_customer", "s", map[string]string{}), http.StatusNotFound, codeNotFound)
	res, err := ts.Client().Get(ts.URL + "/v1/get_customer")
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := io.ReadAll(res.Body)
	res.Body.Close()
	expectError(t, response{status: res.StatusCode, raw: raw}, http.StatusMethodNotAllowed, codeInvalidRequest)
}

// TestCancellationRules는 해지 후 이용 마지막 날과 부가 상품 동시 해지를 HTTP로 확인한다.
func TestCancellationRules(t *testing.T) {
	ts := newTestServer(t, nil)
	cancel := func(sid, customer, sub string) response {
		return tool(t, ts, sid, "create_cancellation", map[string]string{"customer_id": customer, "subscription_id": sub, "idempotency_key": sid + sub})
	}
	for _, tc := range []struct{ customer, sub, last string }{
		{"C012", "S012", "2026-10-19"}, // 체험 종료 전날
		{"C016", "S016", "2026-11-01"}, // 다음 결제일 전날
		{"C005", "S005", "2026-10-12"}, // 연 구독: 해지 신청일
	} {
		verify(t, ts, tc.customer, tc.customer)
		var cr CancellationReceipt
		expectOK(t, cancel(tc.customer, tc.customer, tc.sub), &cr)
		if cr.LastUsableDate.String() != tc.last {
			t.Errorf("%s: last_usable_date %s, want %s", tc.sub, cr.LastUsableDate, tc.last)
		}
	}

	// 본 구독을 해지하면 부가 상품도 함께 해지되고, 이후 부가 상품 해지는 중복이다.
	verify(t, ts, "c014", "C014")
	var cr CancellationReceipt
	expectOK(t, cancel("c014", "C014", "S014"), &cr)
	if !slices.Equal(cr.CancelledSubscriptionIDs, []string{"S014", "S014F"}) || cr.LastUsableDate.String() != "2026-10-29" {
		t.Errorf("본 구독 해지 = %+v", cr)
	}
	expectError(t, cancel("c014", "C014", "S014F"), http.StatusConflict, codeDuplicateRequest)

	// 해지를 먼저 접수하고 청약철회 환불을 나중에 접수해도, 환불 접수가 이용 마지막 날(신청일)을 알려 준다.
	verify(t, ts, "c013", "C013")
	var first CancellationReceipt
	expectOK(t, cancel("c013", "C013", "S013"), &first)
	if first.LastUsableDate.String() != "2026-11-09" {
		t.Errorf("환불 전 해지 = %s", first.LastUsableDate)
	}
	q := quote(t, ts, "c013", "C013", "S013", "withdrawal")
	var rr RefundReceipt
	expectOK(t, tool(t, ts, "c013", "create_refund_request", map[string]string{"customer_id": "C013", "payment_id": "P013", "quote_id": q.QuoteID, "idempotency_key": "r"}), &rr)
	if rr.LastUsableDate == nil || rr.LastUsableDate.String() != "2026-10-12" {
		t.Errorf("청약철회 환불 접수의 이용 마지막 날 = %v", rr.LastUsableDate)
	}
}

func TestRequestLog(t *testing.T) {
	var buf bytes.Buffer
	ts := newTestServer(t, &buf)
	tool(t, ts, "log-session", "get_customer", map[string]string{"customer_id": "C999"})
	line := strings.TrimSpace(buf.String())
	if strings.Count(line, "\n") != 0 {
		t.Fatalf("요청 하나에 로그 한 줄이어야 한다:\n%s", line)
	}
	var entry map[string]any
	if err := json.Unmarshal([]byte(line), &entry); err != nil {
		t.Fatalf("로그가 JSON이 아니다: %v\n%s", err, line)
	}
	for _, k := range []string{"time", "session", "tool", "status", "code", "latency_ms"} {
		if _, ok := entry[k]; !ok {
			t.Errorf("로그에 %s가 없다: %s", k, line)
		}
	}
	if entry["session"] != "log-session" || entry["tool"] != "get_customer" || entry["status"] != float64(404) || entry["code"] != codeNotFound {
		t.Errorf("로그 내용 = %s", line)
	}
}

// ---- 시나리오 재생 ----

type scenario struct {
	ID       string `json:"id"`
	Expected struct {
		RefundAmount *int64 `json:"refund_amount"`
		ToolCalls    []struct {
			Name string         `json:"name"`
			Args map[string]any `json:"args"`
		} `json:"tool_calls"`
	} `json:"expected"`
}

// TestScenarioToolCalls는 data/scenarios/org_a.jsonl의 기대 도구 호출을 순서대로 재생해,
// 이 서버가 엔진이 할 호출을 모두 받아 주고 기대 환불액·이용 마지막 날을 돌려주는지 확인한다.
// 시나리오 인자에 없는 otp, idempotency_key, quote_id는 테스트가 채운다.
func TestScenarioToolCalls(t *testing.T) {
	f, err := os.Open(scenariosPath)
	if err != nil {
		t.Fatalf("시나리오 파일: %v", err)
	}
	defer f.Close()
	var scenarios []scenario
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	for sc.Scan() {
		if strings.TrimSpace(sc.Text()) == "" {
			continue
		}
		var s scenario
		if err := json.Unmarshal(sc.Bytes(), &s); err != nil {
			t.Fatalf("시나리오 해석: %v", err)
		}
		scenarios = append(scenarios, s)
	}
	if len(scenarios) != 30 {
		t.Fatalf("시나리오 %d개, want 30", len(scenarios))
	}

	// 200이 아닌 결과를 기대하는 호출(시나리오, 도구) → 오류 코드.
	wantCode := map[[2]string]string{
		{"s11", "quote_refund"}: codeNotEligible, // 이용 정지 계정(제16조)
		{"s27", "quote_refund"}: codeNotEligible, // 7일 경과 후 장애 환불 요청
		{"s19", "quote_refund"}: codeTimeout,     // 주입한 장애, 재시도도 실패
	}
	failVerify := map[string]bool{"s18": true, "s25": true}
	wantLast := map[string]string{
		"s01": "2026-10-12", "s02": "2026-10-12", "s03": "2026-10-12", "s04": "2026-10-12", "s05": "2026-10-12",
		"s07": "2026-10-12", "s10": "2026-10-12", "s12": "2026-10-19", "s13": "2026-10-12", "s14": "2026-10-29",
		"s20": "2026-11-01", "s21": "2026-10-12", "s24": "2026-10-12", "s26": "2026-10-29", "s29": "2026-10-12",
		"s30": "2026-10-12",
	}
	pending := map[string]bool{"s03": true, "s04": true, "s24": true}

	ts := newTestServer(t, nil)
	for _, s := range scenarios {
		t.Run(s.ID, func(t *testing.T) {
			expectOK(t, post(t, ts, "/_admin/reset", "", map[string]any{}), nil)
			if s.ID == "s19" {
				expectOK(t, post(t, ts, "/_admin/faults", "", map[string]any{"tool": "quote_refund", "code": "TIMEOUT", "count": 2}), nil)
			}
			var lastQuote *orga.Quote
			var refunded int64
			var last string
			for i, call := range s.Expected.ToolCalls {
				args := map[string]any{}
				for k, v := range call.Args {
					args[k] = v
				}
				switch call.Name {
				case "verify_identity":
					args["otp"] = otpFor(args["phone"].(string))
					if failVerify[s.ID] {
						args["otp"] = "9999"
					}
				case "create_refund_request":
					if lastQuote == nil {
						t.Fatalf("%d번째 호출: 견적 없이 환불 접수", i)
					}
					args["quote_id"] = lastQuote.QuoteID
					args["idempotency_key"] = fmt.Sprintf("%s-%d", s.ID, i)
				case "create_cancellation", "register_refund_account":
					args["idempotency_key"] = fmt.Sprintf("%s-%d", s.ID, i)
				}
				r := tool(t, ts, s.ID, call.Name, args)
				if code, ok := wantCode[[2]string{s.ID, call.Name}]; ok {
					expectError(t, r, statusFor(code), code)
					continue
				}
				expectOK(t, r, nil)
				switch call.Name {
				case "verify_identity":
					var v verifyResponse
					r.into(t, &v)
					if v.Verified == failVerify[s.ID] {
						t.Errorf("본인 확인 결과 = %+v", v)
					}
				case "quote_refund":
					lastQuote = &orga.Quote{}
					r.into(t, lastQuote)
				case "create_refund_request":
					var rr RefundReceipt
					r.into(t, &rr)
					refunded += rr.Amount
					if (rr.Status == refundPendingApproval) != pending[s.ID] {
						t.Errorf("환불 상태 = %s", rr.Status)
					}
				case "create_cancellation":
					var cr CancellationReceipt
					r.into(t, &cr)
					last = cr.LastUsableDate.String()
				}
			}
			if s.Expected.RefundAmount != nil {
				if lastQuote == nil || lastQuote.RefundAmount != *s.Expected.RefundAmount || refunded != *s.Expected.RefundAmount {
					t.Errorf("환불액: 견적 %v, 접수 %d, want %d", lastQuote, refunded, *s.Expected.RefundAmount)
				}
			}
			if last != wantLast[s.ID] {
				t.Errorf("이용 마지막 날 = %q, want %q", last, wantLast[s.ID])
			}
		})
	}
}
