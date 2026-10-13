package server

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"

	"github.com/jabdori/jikgam/bizapi/internal/orga"
)

// expectedWaitMinutes는 상담사 인계 예상 대기 시간이다. 모의 값이다.
const expectedWaitMinutes = 5

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

// verifyIdentity는 휴대폰 번호와 모의 인증번호(번호 끝 4자리)로 본인 확인을 한다.
// 실패하면 어떤 고객인지 알려 주지 않는다. 같은 세션에서 2회 실패하면 handoff_required를 켜고,
// 그 뒤의 시도는 HANDOFF_REQUIRED로 거절한다(제19조 제1항).
func (s *Server) verifyIdentity(sid string, body []byte) (any, *apiError) {
	var req verifyRequest
	if e := decode(body, &req); e != nil {
		return nil, e
	}
	if strings.TrimSpace(req.Phone) == "" || strings.TrimSpace(req.OTP) == "" {
		return nil, invalid("phone과 otp가 필요하다")
	}
	sess := s.st.session(sid)
	if sess.locked() {
		return nil, newError(codeHandoffRequired, "이 세션은 본인 확인에 2회 실패해 더 시도할 수 없다. 상담사에게 연결해야 한다(제19조 제1항).", "A-19")
	}
	sess.attempts++
	if c, ok := s.ds.CustomerByPhone(req.Phone); ok && c.CheckOTP(req.OTP) {
		sess.verifiedCustomer = c.ID
		return verifyResponse{Verified: true, CustomerID: c.ID, Attempts: sess.attempts, FailedAttempts: sess.failed}, nil
	}
	sess.failed++
	resp := verifyResponse{Attempts: sess.attempts, FailedAttempts: sess.failed}
	if sess.locked() {
		resp.HandoffRequired = true
		resp.ArticleIDs = []string{"A-19"}
	}
	return resp, nil
}

// requireIdentity는 쓰기 작업 전에 이 세션이 그 고객의 본인 확인을 마쳤는지 본다(제15조 제2항).
func (s *Server) requireIdentity(sid, customerID string) *apiError {
	sess := s.st.session(sid)
	if customerID != "" && sess.verifiedCustomer == customerID {
		return nil
	}
	if sess.locked() {
		return newError(codeHandoffRequired, "본인 확인에 2회 실패한 세션이다. 상담사에게 연결해야 한다(제19조 제1항).", "A-19")
	}
	return newError(codeIdentityRequired, fmt.Sprintf("이 세션은 고객 %s의 본인 확인을 마치지 않았다(제15조 제2항).", customerID), "A-15")
}

func (s *Server) customer(id string) (*orga.Customer, *apiError) {
	if id == "" {
		return nil, invalid("customer_id가 필요하다")
	}
	c, ok := s.ds.Customer(id)
	if !ok {
		return nil, notFound(fmt.Sprintf("고객 %q가 없다", id))
	}
	return c, nil
}

// ---- 조회 ----

type customerRequest struct {
	CustomerID string `json:"customer_id"`
}

type customerView struct {
	CustomerID string    `json:"customer_id"`
	Name       string    `json:"name"`
	Birth      orga.Date `json:"birth"`
	Status     string    `json:"status"`
}

func (s *Server) getCustomer(_ string, body []byte) (any, *apiError) {
	var req customerRequest
	if e := decode(body, &req); e != nil {
		return nil, e
	}
	c, e := s.customer(req.CustomerID)
	if e != nil {
		return nil, e
	}
	return customerView{CustomerID: c.ID, Name: c.Name, Birth: c.Birth, Status: c.Status}, nil
}

type subscriptionView struct {
	SubscriptionID string     `json:"subscription_id"`
	Plan           string     `json:"plan"`
	Price          int64      `json:"price"`
	Promo          bool       `json:"promo"`
	Trial          bool       `json:"trial"`
	TrialEnd       *orga.Date `json:"trial_end,omitempty"`
	Start          orga.Date  `json:"start"`
	NextBilling    orga.Date  `json:"next_billing"`
	Status         string     `json:"status"` // active | cancelled
	LastUsableDate *orga.Date `json:"last_usable_date,omitempty"`
}

func (s *Server) listSubscriptions(_ string, body []byte) (any, *apiError) {
	var req customerRequest
	if e := decode(body, &req); e != nil {
		return nil, e
	}
	c, e := s.customer(req.CustomerID)
	if e != nil {
		return nil, e
	}
	views := []subscriptionView{}
	for _, sub := range c.Subscriptions {
		v := subscriptionView{
			SubscriptionID: sub.ID, Plan: sub.Plan, Price: sub.Price, Promo: sub.Promo, Trial: sub.Trial,
			Start: sub.Start, NextBilling: sub.NextBilling, Status: "active",
		}
		if sub.Trial {
			te := sub.TrialEnd
			v.TrialEnd = &te
		}
		if cr, ok := s.st.cancels[sub.ID]; ok {
			last := cr.LastUsableDate
			v.Status, v.LastUsableDate = "cancelled", &last
		}
		views = append(views, v)
	}
	return map[string]any{"customer_id": c.ID, "subscriptions": views}, nil
}

type paymentView struct {
	PaymentID      string    `json:"payment_id"`
	SubscriptionID string    `json:"subscription_id"`
	Date           orga.Date `json:"date"`
	Amount         int64     `json:"amount"`
	Method         string    `json:"method"`
	Dispute        bool      `json:"dispute"`
	RefundID       string    `json:"refund_id,omitempty"`
	RefundStatus   string    `json:"refund_status,omitempty"`
}

func (s *Server) listPayments(_ string, body []byte) (any, *apiError) {
	var req customerRequest
	if e := decode(body, &req); e != nil {
		return nil, e
	}
	c, e := s.customer(req.CustomerID)
	if e != nil {
		return nil, e
	}
	views := []paymentView{}
	for _, p := range c.Payments {
		v := paymentView{PaymentID: p.ID, SubscriptionID: p.SubscriptionID, Date: p.Date, Amount: p.Amount, Method: p.Method, Dispute: p.Dispute}
		if rr, ok := s.st.refunds[p.ID]; ok {
			v.RefundID, v.RefundStatus = rr.RefundID, rr.Status
		}
		views = append(views, v)
	}
	return map[string]any{"customer_id": c.ID, "payments": views}, nil
}

type usageRequest struct {
	CustomerID string `json:"customer_id"`
	Since      string `json:"since"`
}

type usageView struct {
	Date        orga.Date `json:"date"`
	Type        string    `json:"type"`
	DuringTrial bool      `json:"during_trial"`
}

// getUsage는 since(포함) 이후의 이용 이력을 돌려준다. since를 빼면 전부다.
func (s *Server) getUsage(_ string, body []byte) (any, *apiError) {
	var req usageRequest
	if e := decode(body, &req); e != nil {
		return nil, e
	}
	c, e := s.customer(req.CustomerID)
	if e != nil {
		return nil, e
	}
	var since orga.Date
	if req.Since != "" {
		d, err := orga.ParseDate(req.Since)
		if err != nil {
			return nil, invalid("since: " + err.Error())
		}
		since = d
	}
	views := []usageView{}
	for _, u := range c.Usage {
		if !since.IsZero() && u.Date.Before(since) {
			continue
		}
		views = append(views, usageView{Date: u.Date, Type: u.Type, DuringTrial: u.DuringTrial})
	}
	return map[string]any{"customer_id": c.ID, "since": req.Since, "usage": views}, nil
}

type incidentRequest struct {
	From string `json:"from"`
	To   string `json:"to"`
}

type incidentView struct {
	IncidentID string  `json:"incident_id"`
	Start      string  `json:"start"`
	End        string  `json:"end"`
	Hours      float64 `json:"hours"`
	Cause      string  `json:"cause"`
}

// listIncidents는 [from, to] 날짜 구간과 겹치는 장애 공지 기록을 돌려준다(제9조 제3항). 둘 다 생략할 수 있다.
func (s *Server) listIncidents(_ string, body []byte) (any, *apiError) {
	var req incidentRequest
	if e := decode(body, &req); e != nil {
		return nil, e
	}
	var from, to orga.Date
	var err error
	if req.From != "" {
		if from, err = orga.ParseDate(req.From); err != nil {
			return nil, invalid("from: " + err.Error())
		}
	}
	if req.To != "" {
		if to, err = orga.ParseDate(req.To); err != nil {
			return nil, invalid("to: " + err.Error())
		}
	}
	views := []incidentView{}
	for _, inc := range s.ds.Incidents {
		if !from.IsZero() && inc.EndDate().Before(from) {
			continue
		}
		if !to.IsZero() && inc.StartDate().After(to) {
			continue
		}
		views = append(views, incidentView{
			IncidentID: inc.ID,
			Start:      inc.Start.Format("2006-01-02T15:04"),
			End:        inc.End.Format("2006-01-02T15:04"),
			Hours:      inc.Duration().Hours(),
			Cause:      inc.Cause,
		})
	}
	return map[string]any{"from": req.From, "to": req.To, "incidents": views}, nil
}

// ---- 환불 견적 ----

type quoteRequest struct {
	CustomerID     string `json:"customer_id"`
	SubscriptionID string `json:"subscription_id"`
	Reason         string `json:"reason"`
	RequestDate    string `json:"request_date"`
}

// quoteRefund는 약관 규칙으로 환불 견적을 낸다. 서버 시계를 쓰지 않고 request_date만 기준으로 한다.
// 약관상 대상이 아니면 NOT_ELIGIBLE, 제19조 제1항 사유면 HANDOFF_REQUIRED를 돌려주고,
// 두 경우 모두 판단 내역을 error.quote에 담는다.
func (s *Server) quoteRefund(_ string, body []byte) (any, *apiError) {
	var req quoteRequest
	if e := decode(body, &req); e != nil {
		return nil, e
	}
	if req.CustomerID == "" || req.SubscriptionID == "" || req.Reason == "" || req.RequestDate == "" {
		return nil, invalid("customer_id, subscription_id, reason, request_date가 모두 필요하다")
	}
	date, err := orga.ParseDate(req.RequestDate)
	if err != nil {
		return nil, invalid("request_date: " + err.Error())
	}
	q, err := s.ds.Quote(orga.QuoteRequest{
		CustomerID: req.CustomerID, SubscriptionID: req.SubscriptionID, Reason: orga.Reason(req.Reason), RequestDate: date,
	})
	switch {
	case errors.Is(err, orga.ErrNotFound):
		return nil, notFound(err.Error())
	case err != nil:
		return nil, invalid(err.Error())
	}
	switch q.Verdict {
	case orga.VerdictNotEligible:
		e := newError(codeNotEligible, q.Message, q.ArticleIDs...)
		e.Quote = q
		return nil, e
	case orga.VerdictHandoff:
		e := newError(codeHandoffRequired, q.Message, q.ArticleIDs...)
		e.Quote = q
		return nil, e
	}
	q.QuoteID = quoteID(q)
	if prev, ok := s.st.quotes[q.QuoteID]; ok {
		return prev, nil
	}
	s.st.quotes[q.QuoteID] = q
	return q, nil
}

// quoteID는 견적 입력에서 정해진다. 견적은 고객 데이터와 입력만으로 정해지므로 같은 입력이면 같은 ID다.
func quoteID(q *orga.Quote) string {
	sum := sha256.Sum256([]byte(strings.Join([]string{q.CustomerID, q.SubscriptionID, string(q.Reason), q.RequestDate.String()}, "|")))
	return "Q-" + hex.EncodeToString(sum[:6])
}

// ---- 쓰기 ----

type refundRequest struct {
	CustomerID     string `json:"customer_id"`
	PaymentID      string `json:"payment_id"`
	QuoteID        string `json:"quote_id"`
	IdempotencyKey string `json:"idempotency_key"`
}

// createRefundRequest는 견적의 결제 한 건을 환불 접수한다. 접수일은 견적 기준일이다.
// 환불 예정액이 50,000원을 넘는 견적이면 pending_approval로 접수만 한다(제19조 제2항).
func (s *Server) createRefundRequest(sid string, body []byte) (any, *apiError) {
	const tool = "create_refund_request"
	var req refundRequest
	if e := decode(body, &req); e != nil {
		return nil, e
	}
	if req.CustomerID == "" || req.PaymentID == "" || req.QuoteID == "" || req.IdempotencyKey == "" {
		return nil, invalid("customer_id, payment_id, quote_id, idempotency_key가 모두 필요하다")
	}
	c, e := s.customer(req.CustomerID)
	if e != nil {
		return nil, e
	}
	if e := s.requireIdentity(sid, c.ID); e != nil {
		return nil, e
	}
	fp := fingerprint(refundRequest{CustomerID: req.CustomerID, PaymentID: req.PaymentID, QuoteID: req.QuoteID})
	if resp, e, ok := s.st.replay(tool, req.IdempotencyKey, fp); ok {
		return resp, e
	}
	q, ok := s.st.quotes[req.QuoteID]
	if !ok {
		return nil, notFound(fmt.Sprintf("견적 %q가 없다. quote_refund로 견적을 먼저 받아야 한다", req.QuoteID))
	}
	if q.CustomerID != c.ID {
		return nil, invalid(fmt.Sprintf("견적 %s는 고객 %s의 견적이 아니다", q.QuoteID, c.ID))
	}
	if q.Reason == orga.ReasonPlanChange {
		return nil, newError(codeNotEligible, "요금제 변경의 일할 차감액은 환불이 아니라 연 구독 결제 금액에서 뺀다(제17조 제1항).", "A-17")
	}
	if !q.Eligible {
		return nil, newError(codeNotEligible, "환불 예정액이 0원이라 환불을 접수하지 않는다. "+q.Message, q.ArticleIDs...)
	}
	idx := slices.IndexFunc(q.Refunds, func(l orga.RefundLine) bool { return l.PaymentID == req.PaymentID })
	if idx < 0 {
		return nil, invalid(fmt.Sprintf("결제 %s는 견적 %s의 환불 대상이 아니다", req.PaymentID, q.QuoteID))
	}
	line := q.Refunds[idx]
	if existing, ok := s.st.refunds[line.PaymentID]; ok {
		e := newError(codeDuplicateRequest, fmt.Sprintf("결제 %s는 이미 환불 접수(%s)되었다.", line.PaymentID, existing.RefundID))
		e.Receipt = existing
		return nil, e
	}
	if line.AccountRequired {
		if _, ok := s.st.accounts[c.ID]; !ok {
			return nil, newError(codeAccountRequired, "휴대폰 결제 후 결제한 달이 지나 회원 본인 명의 계좌로 환불한다. register_refund_account로 계좌를 먼저 등록해야 한다(제14조 제3항).", "A-14")
		}
	}
	status := refundConfirmed
	if q.ApprovalRequired {
		status = refundPendingApproval
	}
	receipt := &RefundReceipt{
		RefundID:       s.st.nextID("RF"),
		CustomerID:     c.ID,
		PaymentID:      line.PaymentID,
		SubscriptionID: line.SubscriptionID,
		QuoteID:        q.QuoteID,
		Reason:         q.Reason,
		Amount:         line.Amount,
		Status:         status,
		RefundMethod:   line.RefundMethod,
		ExpectedPeriod: line.ExpectedPeriod,
		ReceivedOn:     q.RequestDate,
		ArticleIDs:     append(slices.Clone(q.ArticleIDs), "A-14"),
	}
	if q.Reason.EndsPeriod() {
		d := q.RequestDate
		receipt.LastUsableDate = &d
	}
	s.st.refunds[line.PaymentID] = receipt
	s.st.remember(tool, req.IdempotencyKey, fp, receipt)
	return receipt, nil
}

type cancelRequest struct {
	CustomerID     string `json:"customer_id"`
	SubscriptionID string `json:"subscription_id"`
	IdempotencyKey string `json:"idempotency_key"`
}

// createCancellation은 구독을 해지한다. 이용 가능 마지막 날은 orga.Dataset.Cancel이 정한다.
// 같은 구독에 청약철회·장애·연 구독 중도 해지 환불 접수가 있으면 그 신청일에 이용이 끝난다.
func (s *Server) createCancellation(sid string, body []byte) (any, *apiError) {
	const tool = "create_cancellation"
	var req cancelRequest
	if e := decode(body, &req); e != nil {
		return nil, e
	}
	if req.CustomerID == "" || req.SubscriptionID == "" || req.IdempotencyKey == "" {
		return nil, invalid("customer_id, subscription_id, idempotency_key가 모두 필요하다")
	}
	c, e := s.customer(req.CustomerID)
	if e != nil {
		return nil, e
	}
	if e := s.requireIdentity(sid, c.ID); e != nil {
		return nil, e
	}
	fp := fingerprint(cancelRequest{CustomerID: req.CustomerID, SubscriptionID: req.SubscriptionID})
	if resp, e, ok := s.st.replay(tool, req.IdempotencyKey, fp); ok {
		return resp, e
	}
	sub, ok := c.Subscription(req.SubscriptionID)
	if !ok {
		return nil, notFound(fmt.Sprintf("고객 %s에게 구독 %q가 없다", c.ID, req.SubscriptionID))
	}
	if existing, ok := s.st.cancels[sub.ID]; ok {
		e := newError(codeDuplicateRequest, fmt.Sprintf("구독 %s는 이미 해지 접수(%s)되었다.", sub.ID, existing.CancellationID))
		e.Receipt = existing
		return nil, e
	}
	res := s.ds.Cancel(c, sub, s.periodEnd(c, sub.ID), func(id string) bool {
		_, done := s.st.cancels[id]
		return done
	})
	receipt := &CancellationReceipt{
		CancellationID:           s.st.nextID("CN"),
		CustomerID:               c.ID,
		SubscriptionID:           sub.ID,
		LastUsableDate:           res.LastUsableDate,
		CancelledSubscriptionIDs: res.CancelledSubscriptionIDs,
		ArticleIDs:               res.ArticleIDs,
	}
	for _, id := range res.CancelledSubscriptionIDs {
		s.st.cancels[id] = receipt
	}
	s.st.remember(tool, req.IdempotencyKey, fp, receipt)
	return receipt, nil
}

// periodEnd는 구독의 결제 중 이용 기간을 끝내는 환불 접수가 있으면 가장 최근 것을 돌려준다.
func (s *Server) periodEnd(c *orga.Customer, subID string) *orga.PeriodEnd {
	var end *orga.PeriodEnd
	for _, p := range c.Payments {
		if p.SubscriptionID != subID {
			continue
		}
		if rr, ok := s.st.refunds[p.ID]; ok && rr.Reason.EndsPeriod() {
			end = &orga.PeriodEnd{Reason: rr.Reason, Date: rr.ReceivedOn}
		}
	}
	return end
}

type accountRequest struct {
	CustomerID     string `json:"customer_id"`
	Bank           string `json:"bank"`
	Account        string `json:"account"`
	Holder         string `json:"holder"`
	IdempotencyKey string `json:"idempotency_key"`
}

// registerRefundAccount는 환불 계좌를 등록한다. 예금주가 회원 본인이 아니면 registered=false로 거절하고
// 저장하지 않는다(제14조 제3항). 거절은 멱등 결과로 남기지 않으므로 같은 키로 고쳐서 다시 보낼 수 있다.
func (s *Server) registerRefundAccount(sid string, body []byte) (any, *apiError) {
	const tool = "register_refund_account"
	var req accountRequest
	if e := decode(body, &req); e != nil {
		return nil, e
	}
	if req.CustomerID == "" || strings.TrimSpace(req.Bank) == "" || strings.TrimSpace(req.Holder) == "" || req.IdempotencyKey == "" {
		return nil, invalid("customer_id, bank, account, holder, idempotency_key가 모두 필요하다")
	}
	if !validAccount(req.Account) {
		return nil, invalid("account는 숫자와 하이픈으로 된 계좌번호여야 한다")
	}
	c, e := s.customer(req.CustomerID)
	if e != nil {
		return nil, e
	}
	if e := s.requireIdentity(sid, c.ID); e != nil {
		return nil, e
	}
	fp := fingerprint(accountRequest{CustomerID: req.CustomerID, Bank: req.Bank, Account: req.Account, Holder: req.Holder})
	if resp, e, ok := s.st.replay(tool, req.IdempotencyKey, fp); ok {
		return resp, e
	}
	if existing, ok := s.st.accounts[c.ID]; ok {
		e := newError(codeDuplicateRequest, fmt.Sprintf("고객 %s의 환불 계좌는 이미 등록(%s)되었다.", c.ID, existing.RegistrationID))
		e.Receipt = existing
		return nil, e
	}
	receipt := &AccountReceipt{
		CustomerID:    c.ID,
		Bank:          req.Bank,
		AccountMasked: maskAccount(req.Account),
		Holder:        req.Holder,
		ArticleIDs:    []string{"A-14"},
	}
	if !c.HolderMatches(req.Holder) {
		receipt.RejectReason = "holder_not_customer"
		receipt.Message = "환불 계좌는 회원 본인 명의여야 한다. 예금주가 회원 본인과 다르다(제14조 제3항)."
		return receipt, nil
	}
	receipt.RegistrationID = s.st.nextID("AC")
	receipt.Registered = true
	s.st.accounts[c.ID] = receipt
	s.st.remember(tool, req.IdempotencyKey, fp, receipt)
	return receipt, nil
}

type handoffRequest struct {
	CustomerID     string `json:"customer_id"`
	Reason         string `json:"reason"`
	Summary        string `json:"summary"`
	IdempotencyKey string `json:"idempotency_key"`
}

// createHandoff는 상담사 인계를 접수한다. 제3자 문의, 본인 확인 실패처럼 본인 확인 전에도 써야 하므로
// 본인 확인을 요구하지 않는다. customer_id는 알 때만 보낸다. idempotency_key는 선택이다.
func (s *Server) createHandoff(_ string, body []byte) (any, *apiError) {
	const tool = "create_handoff"
	var req handoffRequest
	if e := decode(body, &req); e != nil {
		return nil, e
	}
	if strings.TrimSpace(req.Reason) == "" {
		return nil, invalid("reason이 필요하다")
	}
	if req.CustomerID != "" {
		if _, e := s.customer(req.CustomerID); e != nil {
			return nil, e
		}
	}
	fp := fingerprint(handoffRequest{CustomerID: req.CustomerID, Reason: req.Reason, Summary: req.Summary})
	if req.IdempotencyKey != "" {
		if resp, e, ok := s.st.replay(tool, req.IdempotencyKey, fp); ok {
			return resp, e
		}
	}
	receipt := &HandoffReceipt{
		HandoffID:           s.st.nextID("HO"),
		CustomerID:          req.CustomerID,
		Reason:              req.Reason,
		Summary:             req.Summary,
		ExpectedWaitMinutes: expectedWaitMinutes,
	}
	if req.IdempotencyKey != "" {
		s.st.remember(tool, req.IdempotencyKey, fp, receipt)
	}
	return receipt, nil
}

// ---- 멱등 키 ----

// replay는 같은 키로 처리한 적이 있으면 처음 결과를 돌려준다. 키는 같은데 요청 내용이 다르면 거절한다.
func (st *state) replay(tool, key, fp string) (any, *apiError, bool) {
	e, ok := st.idem[idemKey(tool, key)]
	if !ok {
		return nil, nil, false
	}
	if e.fingerprint != fp {
		return nil, invalid("이 idempotency_key는 다른 요청 내용으로 이미 쓰였다"), true
	}
	return e.resp, nil, true
}

func (st *state) remember(tool, key, fp string, resp any) {
	st.idem[idemKey(tool, key)] = idemEntry{fingerprint: fp, resp: resp}
}

func fingerprint(v any) string {
	b, _ := json.Marshal(v)
	return string(b)
}

func validAccount(s string) bool {
	digits := 0
	for _, r := range s {
		switch {
		case r >= '0' && r <= '9':
			digits++
		case r == '-' || r == ' ':
		default:
			return false
		}
	}
	return digits >= 6 && digits <= 20
}

// maskAccount는 계좌번호의 마지막 숫자 3개만 남긴다. 예: 444-555-666 → ***-***-666.
func maskAccount(s string) string {
	total := 0
	for _, r := range s {
		if r >= '0' && r <= '9' {
			total++
		}
	}
	var b strings.Builder
	seen := 0
	for _, r := range s {
		if r >= '0' && r <= '9' {
			seen++
			if seen <= total-3 {
				b.WriteRune('*')
				continue
			}
		}
		b.WriteRune(r)
	}
	return b.String()
}
