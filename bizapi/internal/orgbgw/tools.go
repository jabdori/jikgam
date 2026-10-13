package orgbgw

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// ---- 값 변환표(조직 B → 공통) ----

var (
	planOf = map[string]string{"MONTHLY": "monthly", "YEARLY": "annual", "FAMILY": "family"}
	// 공통 reason ↔ 조직 B 환불 유형
	refundTypeOf = map[string]string{"withdrawal": "COOLING_OFF", "annual_cancel": "YEARLY_PRORATE", "duplicate": "DOUBLE_CHARGE"}
	reasonOf     = map[string]string{"COOLING_OFF": "withdrawal", "YEARLY_PRORATE": "annual_cancel", "DOUBLE_CHARGE": "duplicate"}
	methodOf     = map[string]string{"CARD": "card", "PHONE": "mobile", "POINT": "point"}
	routeOf      = map[string]string{"CARD_CANCEL": "card_cancel", "PHONE_CANCEL": "mobile_cancel", "BANK_TRANSFER": "bank_transfer",
		"POINT_RESTORE": "point_restore"}
	refundStatusOf = map[string]string{"DONE": "confirmed", "REVIEW": "pending_approval"}
	memberStateOf  = map[string]string{"NORMAL": "active", "SUSPENDED": "suspended"}
	passStateOf    = map[string]string{"ACTIVE": "active", "CANCELLED": "cancelled"}
	causeOf        = map[string]string{"OURS": "company", "VENDOR": "vendor"}
	// 조직 B 상담사 연결 코드 → 공통 인계 사유. 이의 제기는 조직 A와 같은 dispute로 둔다.
	handoffReasonOf = map[string]string{"CHARGEBACK": "dispute", "REPEAT_REFUND": "repeat_refund"}
	// 조직 B 계좌 거절 코드 → 공통 reject_reason
	rejectOf = map[string]string{"OWNER_MISMATCH": "holder_not_customer"}
)

// unsupportedReasons는 공통 계약에는 있지만 조직 B 약관에 없는 견적 사유다. 업무 시스템을 부르지 않고 NOT_ELIGIBLE로 답한다.
var unsupportedReasons = map[string]struct {
	msg      string
	articles []string
}{
	"incident":     {"조직 B 약관에는 장애를 이유로 한 환불이 없다. 장애는 이용 기간 연장으로만 보상한다(제9조 제2항).", []string{"B-09"}},
	"minor":        {"조직 B 이용권은 만 19세 이상만 결제할 수 있어 미성년자 결제 취소 규정이 없다(제3조 제1항).", []string{"B-03"}},
	"trial_cancel": {"조직 B에는 무료 체험이 없다.", nil},
	"plan_change":  {"조직 B는 이용권 변경 때 이미 결제한 요금을 차감하거나 환불하지 않고 남은 기간을 더한다(제11조 제1항).", []string{"B-11"}},
}

func mapped(m map[string]string, v string) string {
	if out, ok := m[v]; ok {
		return out
	}
	return strings.ToLower(v)
}

// kstDate는 조직 B 시각(RFC 3339, +09:00)을 KST 달력 날짜(YYYY-MM-DD)로, kstMinute는 YYYY-MM-DDTHH:MM으로 바꾼다.
var kstZone = time.FixedZone("KST", 9*60*60)

func kstTime(s string) (time.Time, error) {
	t, err := time.Parse(time.RFC3339, s)
	if err != nil {
		return time.Time{}, err
	}
	return t.In(kstZone), nil
}

func validDate(s string) bool {
	_, err := time.Parse("2006-01-02", s)
	return err == nil
}

func (g *Gateway) get(ctx context.Context, sid, path, endpoint string, query url.Values, out any) *apiError {
	data, be, ae := g.do(ctx, call{method: http.MethodGet, path: path, query: query, sid: sid, endpoint: endpoint})
	if ae != nil {
		return ae
	}
	if be != nil {
		return g.translate(endpoint, be, "")
	}
	if err := json.Unmarshal(data, out); err != nil {
		return newError(codeInternal, "조직 B 응답을 해석할 수 없다: "+err.Error())
	}
	return nil
}

func memberPath(id string, rest string) string { return "/api/v2/members/" + url.PathEscape(id) + rest }

// ---- 본인 확인 ----

type verifyRequest struct {
	Phone string `json:"phone"`
	OTP   string `json:"otp"`
}

type verifyResponse struct {
	Verified        bool     `json:"verified"`
	CustomerID      string   `json:"customer_id"`
	Attempts        int      `json:"attempts"`
	FailedAttempts  int      `json:"failed_attempts"`
	HandoffRequired bool     `json:"handoff_required"`
	ArticleIDs      []string `json:"article_ids,omitempty"`
}

// verifyIdentity: phone·otp → 조직 B mobile·code. 성공하면 회원 토큰을 세션에 둔다. 조직 B는 시도 수를 돌려주지 않으므로
// attempts는 어댑터가 세션마다 센다. 조직 B가 잠금(AUTH_LOCKED)을 돌려주면 HANDOFF_REQUIRED다.
func (g *Gateway) verifyIdentity(ctx context.Context, sid string, body []byte) (any, *apiError) {
	var req verifyRequest
	if e := decode(body, &req); e != nil {
		return nil, e
	}
	if strings.TrimSpace(req.Phone) == "" || strings.TrimSpace(req.OTP) == "" {
		return nil, invalid("phone과 otp가 필요하다")
	}
	data, be, ae := g.do(ctx, call{method: http.MethodPost, path: "/api/v2/auth/verify", sid: sid, endpoint: "auth-verify",
		body: map[string]string{"mobile": req.Phone, "code": req.OTP}})
	if ae != nil {
		return nil, ae
	}
	if be != nil {
		return nil, g.translate("verify_identity", be, "")
	}
	var b struct {
		OK       bool     `json:"ok"`
		MemberNo string   `json:"member_no"`
		Token    string   `json:"token"`
		Failures int      `json:"failures"`
		Locked   bool     `json:"locked"`
		Clauses  []string `json:"clauses"`
	}
	if err := json.Unmarshal(data, &b); err != nil {
		return nil, newError(codeInternal, "조직 B 본인 확인 응답을 해석할 수 없다")
	}
	s := g.session(sid)
	g.mu.Lock()
	s.attempts++
	if b.OK {
		s.memberNo, s.token = b.MemberNo, b.Token
	}
	attempts := s.attempts
	g.mu.Unlock()
	resp := verifyResponse{Verified: b.OK, Attempts: attempts, FailedAttempts: b.Failures, HandoffRequired: b.Locked}
	if b.OK {
		resp.CustomerID = b.MemberNo
	}
	if b.Locked {
		resp.ArticleIDs = b.Clauses
	}
	return resp, nil
}

// ---- 조회 ----

type customerRequest struct {
	CustomerID string `json:"customer_id"`
}

func (g *Gateway) customerArg(body []byte) (string, *apiError) {
	var req customerRequest
	if e := decode(body, &req); e != nil {
		return "", e
	}
	if req.CustomerID == "" {
		return "", invalid("customer_id가 필요하다")
	}
	return req.CustomerID, nil
}

func (g *Gateway) getCustomer(ctx context.Context, sid string, body []byte) (any, *apiError) {
	id, e := g.customerArg(body)
	if e != nil {
		return nil, e
	}
	var m struct {
		MemberNo   string `json:"member_no"`
		MemberName string `json:"member_name"`
		BirthDate  string `json:"birth_date"`
		State      string `json:"state"`
	}
	if e := g.get(ctx, sid, memberPath(id, ""), "member", nil, &m); e != nil {
		return nil, e
	}
	return map[string]any{"customer_id": m.MemberNo, "name": m.MemberName, "birth": m.BirthDate,
		"status": mapped(memberStateOf, m.State)}, nil
}

type subscriptionView struct {
	SubscriptionID string  `json:"subscription_id"`
	Plan           string  `json:"plan"`
	Price          int64   `json:"price"`
	Promo          bool    `json:"promo"`
	Trial          bool    `json:"trial"`
	Start          string  `json:"start"`
	NextBilling    string  `json:"next_billing"`
	Status         string  `json:"status"`
	LastUsableDate *string `json:"last_usable_date,omitempty"`
	FamilyMembers  int     `json:"family_members,omitempty"`
}

func (g *Gateway) listSubscriptions(ctx context.Context, sid string, body []byte) (any, *apiError) {
	id, e := g.customerArg(body)
	if e != nil {
		return nil, e
	}
	var b struct {
		MemberNo string `json:"member_no"`
		Items    []struct {
			PassNo        string  `json:"pass_no"`
			ProductCode   string  `json:"product_code"`
			ListPrice     int64   `json:"list_price"`
			StartedOn     string  `json:"started_on"`
			RenewsOn      string  `json:"renews_on"`
			State         string  `json:"state"`
			EndsOn        *string `json:"ends_on"`
			FamilyMembers int     `json:"family_members"`
		} `json:"items"`
	}
	if e := g.get(ctx, sid, memberPath(id, "/passes"), "passes", nil, &b); e != nil {
		return nil, e
	}
	subs := []subscriptionView{}
	for _, p := range b.Items {
		subs = append(subs, subscriptionView{SubscriptionID: p.PassNo, Plan: mapped(planOf, p.ProductCode), Price: p.ListPrice,
			Start: p.StartedOn, NextBilling: p.RenewsOn, Status: mapped(passStateOf, p.State), LastUsableDate: p.EndsOn,
			FamilyMembers: p.FamilyMembers})
	}
	return map[string]any{"customer_id": b.MemberNo, "subscriptions": subs}, nil
}

type paymentView struct {
	PaymentID      string `json:"payment_id"`
	SubscriptionID string `json:"subscription_id"`
	Date           string `json:"date"`
	PaidAt         string `json:"paid_at"`
	Amount         int64  `json:"amount"`
	Method         string `json:"method"`
	Dispute        bool   `json:"dispute"`
	RefundID       string `json:"refund_id,omitempty"`
	RefundStatus   string `json:"refund_status,omitempty"`
}

// listPayments: charged_at(+09:00 시각) → date(KST 날짜)와 paid_at(KST 분 단위). 엔진은 date를 날짜로 비교한다.
func (g *Gateway) listPayments(ctx context.Context, sid string, body []byte) (any, *apiError) {
	id, e := g.customerArg(body)
	if e != nil {
		return nil, e
	}
	var b struct {
		MemberNo string `json:"member_no"`
		Items    []struct {
			ChargeNo    string `json:"charge_no"`
			PassNo      string `json:"pass_no"`
			ChargedAt   string `json:"charged_at"`
			AmountKRW   int64  `json:"amount_krw"`
			PayType     string `json:"pay_type"`
			Chargeback  bool   `json:"chargeback"`
			RefundNo    string `json:"refund_no"`
			RefundState string `json:"refund_state"`
		} `json:"items"`
	}
	if e := g.get(ctx, sid, memberPath(id, "/charges"), "charges", nil, &b); e != nil {
		return nil, e
	}
	pays := []paymentView{}
	for _, c := range b.Items {
		t, err := kstTime(c.ChargedAt)
		if err != nil {
			return nil, newError(codeInternal, fmt.Sprintf("조직 B 결제 %s의 charged_at을 해석할 수 없다", c.ChargeNo))
		}
		v := paymentView{PaymentID: c.ChargeNo, SubscriptionID: c.PassNo, Date: t.Format("2006-01-02"),
			PaidAt: t.Format("2006-01-02T15:04"), Amount: c.AmountKRW, Method: mapped(methodOf, c.PayType), Dispute: c.Chargeback}
		if c.RefundNo != "" {
			v.RefundID, v.RefundStatus = c.RefundNo, mapped(refundStatusOf, c.RefundState)
		}
		pays = append(pays, v)
	}
	return map[string]any{"customer_id": b.MemberNo, "payments": pays}, nil
}

type usageRequest struct {
	CustomerID string `json:"customer_id"`
	Since      string `json:"since"`
}

// getUsage: 조직 B 열람 이력(reading-log). 가족 이용권 구성원의 열람은 reader=family_member로 둔다.
func (g *Gateway) getUsage(ctx context.Context, sid string, body []byte) (any, *apiError) {
	var req usageRequest
	if e := decode(body, &req); e != nil {
		return nil, e
	}
	if req.CustomerID == "" {
		return nil, invalid("customer_id가 필요하다")
	}
	if req.Since != "" && !validDate(req.Since) {
		return nil, invalid("since는 YYYY-MM-DD여야 한다")
	}
	q := url.Values{}
	if req.Since != "" {
		q.Set("from", req.Since)
	}
	var b struct {
		MemberNo string `json:"member_no"`
		Items    []struct {
			On   string `json:"on"`
			Kind string `json:"kind"`
			By   string `json:"by"`
		} `json:"items"`
	}
	if e := g.get(ctx, sid, memberPath(req.CustomerID, "/reading-log"), "reading-log", q, &b); e != nil {
		return nil, e
	}
	usage := []map[string]any{}
	for _, r := range b.Items {
		reader := "owner"
		if r.By == "MEMBER" {
			reader = "family_member"
		}
		usage = append(usage, map[string]any{"date": r.On, "type": strings.ToLower(r.Kind), "reader": reader, "during_trial": false})
	}
	return map[string]any{"customer_id": b.MemberNo, "since": req.Since, "usage": usage}, nil
}

type incidentRequest struct {
	From string `json:"from"`
	To   string `json:"to"`
}

// listIncidents: 조직 B 장애 기록(outages). minutes → hours, fault → cause, +09:00 시각 → KST 분 단위.
func (g *Gateway) listIncidents(ctx context.Context, sid string, body []byte) (any, *apiError) {
	var req incidentRequest
	if e := decode(body, &req); e != nil {
		return nil, e
	}
	q := url.Values{}
	for k, v := range map[string]string{"from": req.From, "to": req.To} {
		if v == "" {
			continue
		}
		if !validDate(v) {
			return nil, invalid(k + "는 YYYY-MM-DD여야 한다")
		}
		q.Set(k, v)
	}
	var b struct {
		Items []struct {
			OutageNo string `json:"outage_no"`
			BeganAt  string `json:"began_at"`
			EndedAt  string `json:"ended_at"`
			Minutes  int    `json:"minutes"`
			Fault    string `json:"fault"`
		} `json:"items"`
	}
	if e := g.get(ctx, sid, "/api/v2/outages", "outages", q, &b); e != nil {
		return nil, e
	}
	incidents := []map[string]any{}
	for _, o := range b.Items {
		began, err1 := kstTime(o.BeganAt)
		ended, err2 := kstTime(o.EndedAt)
		if err1 != nil || err2 != nil {
			return nil, newError(codeInternal, fmt.Sprintf("조직 B 장애 %s의 시각을 해석할 수 없다", o.OutageNo))
		}
		incidents = append(incidents, map[string]any{"incident_id": o.OutageNo, "start": began.Format("2006-01-02T15:04"),
			"end": ended.Format("2006-01-02T15:04"), "hours": float64(o.Minutes) / 60, "cause": mapped(causeOf, o.Fault)})
	}
	return map[string]any{"from": req.From, "to": req.To, "incidents": incidents}, nil
}

// ---- 견적 ----

type bItem struct {
	ChargeNo         string `json:"charge_no"`
	PassNo           string `json:"pass_no"`
	KRW              int64  `json:"krw"`
	PayType          string `json:"pay_type"`
	Route            string `json:"route"`
	NeedsBankAccount bool   `json:"needs_bank_account"`
	ETAText          string `json:"eta_text"`
}

type bEstimate struct {
	EstimateNo  string          `json:"estimate_no"`
	MemberNo    string          `json:"member_no"`
	PassNo      string          `json:"pass_no"`
	RefundType  string          `json:"refund_type"`
	AsOf        string          `json:"as_of"`
	Refundable  bool            `json:"refundable"`
	TotalKRW    int64           `json:"total_krw"`
	Items       []bItem         `json:"items"`
	Calc        json.RawMessage `json:"calc"`
	Clauses     []string        `json:"clauses"`
	NeedsReview bool            `json:"needs_review"`
	Escalate    *struct {
		Code   string `json:"code"`
		Reason string `json:"reason"`
	} `json:"escalate"`
	ServiceEndsOn *string `json:"service_ends_on"`
	Note          string  `json:"note"`
}

// quote는 공통 견적(quote_refund 응답, 오류의 error.quote)이다. 필드 뜻은 bizapi/README.md의 quote_refund 표를 따른다.
type quote struct {
	QuoteID          string          `json:"quote_id,omitempty"`
	CustomerID       string          `json:"customer_id"`
	SubscriptionID   string          `json:"subscription_id"`
	Reason           string          `json:"reason"`
	RequestDate      string          `json:"request_date"`
	Eligible         bool            `json:"eligible"`
	RefundAmount     int64           `json:"refund_amount"`
	Refunds          []refundLine    `json:"refunds"`
	Breakdown        json.RawMessage `json:"breakdown,omitempty"`
	ArticleIDs       []string        `json:"article_ids"`
	ApprovalRequired bool            `json:"approval_required"`
	HandoffRequired  bool            `json:"handoff_required"`
	HandoffReason    string          `json:"handoff_reason,omitempty"`
	LastUsableDate   *string         `json:"last_usable_date,omitempty"`
	Message          string          `json:"message"`
}

type refundLine struct {
	PaymentID       string `json:"payment_id"`
	SubscriptionID  string `json:"subscription_id"`
	Amount          int64  `json:"amount"`
	PaymentMethod   string `json:"payment_method"`
	RefundMethod    string `json:"refund_method"`
	AccountRequired bool   `json:"account_required"`
	ExpectedPeriod  string `json:"expected_period"`
}

func mapEstimate(e bEstimate, reason string) quote {
	if reason == "" {
		reason = mapped(reasonOf, e.RefundType)
	}
	q := quote{QuoteID: e.EstimateNo, CustomerID: e.MemberNo, SubscriptionID: e.PassNo, Reason: reason, RequestDate: e.AsOf,
		Eligible: e.Refundable && e.TotalKRW > 0, RefundAmount: e.TotalKRW, Refunds: []refundLine{}, Breakdown: e.Calc,
		ArticleIDs: e.Clauses, ApprovalRequired: e.NeedsReview, LastUsableDate: e.ServiceEndsOn, Message: e.Note}
	if q.ArticleIDs == nil {
		q.ArticleIDs = []string{}
	}
	for _, it := range e.Items {
		q.Refunds = append(q.Refunds, refundLine{PaymentID: it.ChargeNo, SubscriptionID: it.PassNo, Amount: it.KRW,
			PaymentMethod: mapped(methodOf, it.PayType), RefundMethod: mapped(routeOf, it.Route),
			AccountRequired: it.NeedsBankAccount, ExpectedPeriod: it.ETAText})
	}
	if e.Escalate != nil {
		q.HandoffRequired, q.HandoffReason = true, mapped(handoffReasonOf, e.Escalate.Code)
	}
	return q
}

type quoteRequest struct {
	CustomerID     string `json:"customer_id"`
	SubscriptionID string `json:"subscription_id"`
	Reason         string `json:"reason"`
	RequestDate    string `json:"request_date"`
}

func (g *Gateway) quoteRefund(ctx context.Context, sid string, body []byte) (any, *apiError) {
	var req quoteRequest
	if e := decode(body, &req); e != nil {
		return nil, e
	}
	if req.CustomerID == "" || req.SubscriptionID == "" || req.Reason == "" || req.RequestDate == "" {
		return nil, invalid("customer_id, subscription_id, reason, request_date가 모두 필요하다")
	}
	if !validDate(req.RequestDate) {
		return nil, invalid("request_date는 YYYY-MM-DD여야 한다")
	}
	if u, ok := unsupportedReasons[req.Reason]; ok {
		return nil, newError(codeNotEligible, u.msg, u.articles...)
	}
	typ, ok := refundTypeOf[req.Reason]
	if !ok {
		return nil, invalid(fmt.Sprintf("알 수 없는 reason %q", req.Reason))
	}
	data, be, ae := g.do(ctx, call{method: http.MethodPost, path: "/api/v2/refund-estimates", sid: sid, endpoint: "refund-estimates",
		body: map[string]string{"member_no": req.CustomerID, "pass_no": req.SubscriptionID, "refund_type": typ, "as_of": req.RequestDate}})
	if ae != nil {
		return nil, ae
	}
	if be != nil {
		return nil, g.translate("quote_refund", be, req.Reason)
	}
	var e bEstimate
	if err := json.Unmarshal(data, &e); err != nil {
		return nil, newError(codeInternal, "조직 B 견적 응답을 해석할 수 없다")
	}
	return mapEstimate(e, req.Reason), nil
}

// ---- 쓰기 ----

type bRefund struct {
	RefundNo      string   `json:"refund_no"`
	MemberNo      string   `json:"member_no"`
	ChargeNo      string   `json:"charge_no"`
	PassNo        string   `json:"pass_no"`
	EstimateNo    string   `json:"estimate_no"`
	RefundType    string   `json:"refund_type"`
	KRW           int64    `json:"krw"`
	State         string   `json:"state"`
	Route         string   `json:"route"`
	ETAText       string   `json:"eta_text"`
	ReceivedOn    string   `json:"received_on"`
	ServiceEndsOn *string  `json:"service_ends_on"`
	Clauses       []string `json:"clauses"`
}

func mapRefund(r bRefund) map[string]any {
	out := map[string]any{"refund_id": r.RefundNo, "customer_id": r.MemberNo, "payment_id": r.ChargeNo,
		"subscription_id": r.PassNo, "quote_id": r.EstimateNo, "reason": mapped(reasonOf, r.RefundType), "amount": r.KRW,
		"status": mapped(refundStatusOf, r.State), "refund_method": mapped(routeOf, r.Route), "expected_period": r.ETAText,
		"received_on": r.ReceivedOn, "article_ids": nonNil(r.Clauses)}
	if r.ServiceEndsOn != nil {
		out["last_usable_date"] = *r.ServiceEndsOn
	}
	return out
}

type bCancel struct {
	CancelNo      string   `json:"cancel_no"`
	MemberNo      string   `json:"member_no"`
	PassNo        string   `json:"pass_no"`
	ServiceEndsOn string   `json:"service_ends_on"`
	MembersEnded  int      `json:"members_ended"`
	Clauses       []string `json:"clauses"`
}

func mapCancel(c bCancel) map[string]any {
	return map[string]any{"cancellation_id": c.CancelNo, "customer_id": c.MemberNo, "subscription_id": c.PassNo,
		"last_usable_date": c.ServiceEndsOn, "cancelled_subscription_ids": []string{c.PassNo},
		"family_members_ended": c.MembersEnded, "article_ids": nonNil(c.Clauses)}
}

type bAccount struct {
	Accepted      bool     `json:"accepted"`
	RegNo         string   `json:"reg_no"`
	MemberNo      string   `json:"member_no"`
	BankName      string   `json:"bank_name"`
	AccountMasked string   `json:"account_masked"`
	OwnerName     string   `json:"owner_name"`
	RejectCode    string   `json:"reject_code"`
	Detail        string   `json:"detail"`
	Clauses       []string `json:"clauses"`
}

func mapAccount(a bAccount) map[string]any {
	out := map[string]any{"customer_id": a.MemberNo, "registered": a.Accepted, "bank": a.BankName,
		"account_masked": a.AccountMasked, "holder": a.OwnerName, "article_ids": nonNil(a.Clauses)}
	if a.RegNo != "" {
		out["registration_id"] = a.RegNo
	}
	if a.RejectCode != "" {
		out["reject_reason"], out["message"] = mapped(rejectOf, a.RejectCode), a.Detail
	}
	return out
}

// mapExisting은 ALREADY_DONE의 기존 접수를 공통 접수 내역으로 바꾼다.
func mapExisting(tool string, raw json.RawMessage) (any, error) {
	switch tool {
	case "create_refund_request":
		var r bRefund
		err := json.Unmarshal(raw, &r)
		return mapRefund(r), err
	case "create_cancellation":
		var c bCancel
		err := json.Unmarshal(raw, &c)
		return mapCancel(c), err
	case "register_refund_account":
		var a bAccount
		err := json.Unmarshal(raw, &a)
		return mapAccount(a), err
	}
	return nil, fmt.Errorf("기존 접수를 바꿀 수 없는 도구: %s", tool)
}

func nonNil(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}

// write는 쓰기 도구 하나를 조직 B로 보낸다. 멱등 키는 헤더로, 회원 토큰은 Bearer로 붙인다.
func (g *Gateway) write(ctx context.Context, tool, sid, customerID string, c call, out any) *apiError {
	c.sid, c.token = sid, g.tokenFor(sid, customerID)
	data, be, ae := g.do(ctx, c)
	if ae != nil {
		return ae
	}
	if be != nil {
		return g.translate(tool, be, "")
	}
	if err := json.Unmarshal(data, out); err != nil {
		return newError(codeInternal, "조직 B 접수 응답을 해석할 수 없다")
	}
	return nil
}

type refundRequest struct {
	CustomerID     string `json:"customer_id"`
	PaymentID      string `json:"payment_id"`
	QuoteID        string `json:"quote_id"`
	IdempotencyKey string `json:"idempotency_key"`
}

func (g *Gateway) createRefundRequest(ctx context.Context, sid string, body []byte) (any, *apiError) {
	var req refundRequest
	if e := decode(body, &req); e != nil {
		return nil, e
	}
	if req.CustomerID == "" || req.PaymentID == "" || req.QuoteID == "" || req.IdempotencyKey == "" {
		return nil, invalid("customer_id, payment_id, quote_id, idempotency_key가 모두 필요하다")
	}
	var r bRefund
	if e := g.write(ctx, "create_refund_request", sid, req.CustomerID, call{method: http.MethodPost, path: "/api/v2/refunds",
		endpoint: "refunds", idemKey: req.IdempotencyKey,
		body: map[string]string{"member_no": req.CustomerID, "estimate_no": req.QuoteID, "charge_no": req.PaymentID}}, &r); e != nil {
		return nil, e
	}
	return mapRefund(r), nil
}

type cancelRequest struct {
	CustomerID     string `json:"customer_id"`
	SubscriptionID string `json:"subscription_id"`
	IdempotencyKey string `json:"idempotency_key"`
}

func (g *Gateway) createCancellation(ctx context.Context, sid string, body []byte) (any, *apiError) {
	var req cancelRequest
	if e := decode(body, &req); e != nil {
		return nil, e
	}
	if req.CustomerID == "" || req.SubscriptionID == "" || req.IdempotencyKey == "" {
		return nil, invalid("customer_id, subscription_id, idempotency_key가 모두 필요하다")
	}
	var c bCancel
	if e := g.write(ctx, "create_cancellation", sid, req.CustomerID, call{method: http.MethodPost,
		path: "/api/v2/passes/" + url.PathEscape(req.SubscriptionID) + "/cancel", endpoint: "pass-cancel",
		idemKey: req.IdempotencyKey, body: map[string]string{"member_no": req.CustomerID}}, &c); e != nil {
		return nil, e
	}
	return mapCancel(c), nil
}

type accountRequest struct {
	CustomerID     string `json:"customer_id"`
	Bank           string `json:"bank"`
	Account        string `json:"account"`
	Holder         string `json:"holder"`
	IdempotencyKey string `json:"idempotency_key"`
}

func (g *Gateway) registerRefundAccount(ctx context.Context, sid string, body []byte) (any, *apiError) {
	var req accountRequest
	if e := decode(body, &req); e != nil {
		return nil, e
	}
	if req.CustomerID == "" || strings.TrimSpace(req.Bank) == "" || strings.TrimSpace(req.Holder) == "" ||
		req.Account == "" || req.IdempotencyKey == "" {
		return nil, invalid("customer_id, bank, account, holder, idempotency_key가 모두 필요하다")
	}
	var a bAccount
	if e := g.write(ctx, "register_refund_account", sid, req.CustomerID, call{method: http.MethodPut,
		path: memberPath(req.CustomerID, "/refund-account"), endpoint: "refund-account", idemKey: req.IdempotencyKey,
		body: map[string]string{"bank_name": req.Bank, "account_no": req.Account, "owner_name": req.Holder}}, &a); e != nil {
		return nil, e
	}
	return mapAccount(a), nil
}

type handoffRequest struct {
	CustomerID     string `json:"customer_id"`
	Reason         string `json:"reason"`
	Summary        string `json:"summary"`
	IdempotencyKey string `json:"idempotency_key"`
}

// createHandoff: 조직 B 상담사 연결(agent-tickets). 본인 확인 전에도 쓰므로 회원 토큰을 붙이지 않는다.
func (g *Gateway) createHandoff(ctx context.Context, sid string, body []byte) (any, *apiError) {
	var req handoffRequest
	if e := decode(body, &req); e != nil {
		return nil, e
	}
	if strings.TrimSpace(req.Reason) == "" {
		return nil, invalid("reason이 필요하다")
	}
	data, be, ae := g.do(ctx, call{method: http.MethodPost, path: "/api/v2/agent-tickets", sid: sid, endpoint: "agent-tickets",
		idemKey: req.IdempotencyKey, body: map[string]string{"member_no": req.CustomerID, "topic": req.Reason, "memo": req.Summary}})
	if ae != nil {
		return nil, ae
	}
	if be != nil {
		return nil, g.translate("create_handoff", be, "")
	}
	var t struct {
		TicketNo   string `json:"ticket_no"`
		EtaMinutes int    `json:"eta_minutes"`
	}
	if err := json.Unmarshal(data, &t); err != nil {
		return nil, newError(codeInternal, "조직 B 상담사 연결 응답을 해석할 수 없다")
	}
	out := map[string]any{"handoff_id": t.TicketNo, "reason": req.Reason, "expected_wait_minutes": t.EtaMinutes}
	if req.CustomerID != "" {
		out["customer_id"] = req.CustomerID
	}
	if req.Summary != "" {
		out["summary"] = req.Summary
	}
	return out, nil
}
