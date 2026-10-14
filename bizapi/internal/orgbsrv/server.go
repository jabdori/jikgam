// Package orgbsrv는 가상 조직 B(책솔) 업무 시스템의 모의 HTTP 서버다. 계약은 domain/org_b/api.md의 "조직 B 업무 API"다.
//
// 조직 A 모의 서버(internal/server)와 일부러 다르게 만든다: 자원 경로와 GET 조회, 시스템 인증 헤더 X-Api-Key, 본인 확인 후
// 받는 회원 토큰(Authorization: Bearer), 멱등 키 헤더(Idempotency-Key), 응답 봉투 {"status": "ok", "data": ...}와
// 자체 오류 코드. 상담 엔진은 이 서버를 직접 부르지 않고 internal/orgbgw 어댑터를 거친다.
// 상태(토큰, 실패 횟수, 견적, 접수, 멱등 키, 장애 주입)는 메모리에만 둔다.
package orgbsrv

import (
	"bytes"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/jabdori/jikgam/bizapi/internal/orgb"
)

const (
	apiKeyHeader     = "X-Api-Key"
	contactHeader    = "X-Contact-Ref" // 상담 건 식별자. 본인 확인 실패 횟수를 이 단위로 센다
	idemHeader       = "Idempotency-Key"
	maxBodyBytes     = 1 << 20
	maxRefLen        = 128
	maxVerifyFailure = 3 // 본인 확인 3회 실패면 잠근다(제15조 제1항). 조직 A는 2회다
	ticketWait       = 7 // 상담사 연결 예상 대기(분), 모의 값
)

// 조직 B 오류 코드.
const (
	ErrBadInput            = "BAD_INPUT"
	ErrAPIKeyInvalid       = "API_KEY_INVALID"
	ErrAuthRequired        = "AUTH_REQUIRED"
	ErrAuthLocked          = "AUTH_LOCKED"
	ErrNoSuchResource      = "NO_SUCH_RESOURCE"
	ErrAlreadyDone         = "ALREADY_DONE"
	ErrIdempotencyMismatch = "IDEMPOTENCY_MISMATCH"
	ErrNotRefundable       = "NOT_REFUNDABLE"
	ErrNeedsAgent          = "NEEDS_AGENT"
	ErrBankAccountMissing  = "BANK_ACCOUNT_MISSING"
	ErrServerError         = "SERVER_ERROR"
	ErrMaintenance         = "MAINTENANCE"
	ErrUpstreamTimeout     = "UPSTREAM_TIMEOUT"
)

func statusFor(code string) int {
	switch code {
	case ErrBadInput:
		return http.StatusBadRequest
	case ErrAPIKeyInvalid, ErrAuthRequired:
		return http.StatusUnauthorized
	case ErrAuthLocked:
		return http.StatusLocked
	case ErrNoSuchResource:
		return http.StatusNotFound
	case ErrAlreadyDone, ErrIdempotencyMismatch:
		return http.StatusConflict
	case ErrNotRefundable, ErrNeedsAgent, ErrBankAccountMissing:
		return http.StatusUnprocessableEntity
	case ErrMaintenance:
		return http.StatusServiceUnavailable
	case ErrUpstreamTimeout:
		return http.StatusGatewayTimeout
	}
	return http.StatusInternalServerError
}

// bErr는 오류 응답 {"status": "error", "error_code": ..., "detail": ...}이다.
type bErr struct {
	Code     string         `json:"error_code"`
	Detail   string         `json:"detail"`
	Clauses  []string       `json:"clauses,omitempty"`
	Estimate *orgb.Estimate `json:"estimate,omitempty"` // NOT_REFUNDABLE·NEEDS_AGENT 견적
	Existing any            `json:"existing,omitempty"` // ALREADY_DONE의 기존 접수
}

func newErr(code, detail string, clauses ...string) *bErr {
	return &bErr{Code: code, Detail: detail, Clauses: clauses}
}

// 엔드포인트 이름. 장애 주입과 로그에 쓴다.
const (
	EPVerify    = "auth-verify"
	EPMember    = "member"
	EPPasses    = "passes"
	EPCharges   = "charges"
	EPReading   = "reading-log"
	EPOutages   = "outages"
	EPEstimates = "refund-estimates"
	EPRefunds   = "refunds"
	EPCancel    = "pass-cancel"
	EPAccount   = "refund-account"
	EPTickets   = "agent-tickets"
)

var endpoints = []string{EPVerify, EPMember, EPPasses, EPCharges, EPReading, EPOutages, EPEstimates, EPRefunds, EPCancel,
	EPAccount, EPTickets}

type req struct {
	r    *http.Request
	body []byte
}

type handlerFunc func(s *Server, q req) (any, *bErr)

type Server struct {
	ds      *orgb.Dataset
	apiKey  string
	log     *slog.Logger
	handler http.Handler

	mu sync.Mutex
	st *state
}

// New는 조직 B 모의 서버를 만든다. apiKey는 호출 시스템(어댑터)이 X-Api-Key로 보내야 하는 값이다.
func New(ds *orgb.Dataset, apiKey string, logger *slog.Logger) *Server {
	s := &Server{ds: ds, apiKey: apiKey, log: logger, st: newState()}
	mux := http.NewServeMux()
	route := func(pattern, name string, fn handlerFunc) { mux.Handle(pattern, s.endpoint(name, fn)) }
	route("POST /api/v2/auth/verify", EPVerify, (*Server).verify)
	route("GET /api/v2/members/{member_no}", EPMember, (*Server).member)
	route("GET /api/v2/members/{member_no}/passes", EPPasses, (*Server).passes)
	route("GET /api/v2/members/{member_no}/charges", EPCharges, (*Server).charges)
	route("GET /api/v2/members/{member_no}/reading-log", EPReading, (*Server).readingLog)
	route("GET /api/v2/outages", EPOutages, (*Server).outages)
	route("POST /api/v2/refund-estimates", EPEstimates, (*Server).estimate)
	route("POST /api/v2/refunds", EPRefunds, (*Server).refund)
	route("POST /api/v2/passes/{pass_no}/cancel", EPCancel, (*Server).cancel)
	route("PUT /api/v2/members/{member_no}/refund-account", EPAccount, (*Server).refundAccount)
	route("POST /api/v2/agent-tickets", EPTickets, (*Server).ticket)
	mux.HandleFunc("POST /_mock/reset", s.handleReset)
	mux.HandleFunc("POST /_mock/faults", s.handleFaults)
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		writeErr(w, newErr(ErrNoSuchResource, "경로가 없다: "+r.Method+" "+r.URL.Path))
	})
	s.handler = s.withLog(mux)
	return s
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) { s.handler.ServeHTTP(w, r) }

// endpoint는 시스템 인증, 장애 주입, 상태 잠금을 거쳐 처리 함수를 부른다. 주입한 장애는 상태를 바꾸기 전에 돌려준다.
func (s *Server) endpoint(name string, fn handlerFunc) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if rec, ok := w.(*recorder); ok {
			rec.endpoint = name
		}
		if subtle.ConstantTimeCompare([]byte(r.Header.Get(apiKeyHeader)), []byte(s.apiKey)) != 1 {
			writeErr(w, newErr(ErrAPIKeyInvalid, "X-Api-Key가 없거나 맞지 않다"))
			return
		}
		body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBodyBytes))
		if err != nil {
			writeErr(w, newErr(ErrBadInput, "요청 본문을 읽을 수 없다"))
			return
		}
		s.mu.Lock()
		var data any
		e := s.st.takeFault(name)
		if e == nil {
			data, e = fn(s, req{r: r, body: body})
		}
		s.mu.Unlock()
		if e != nil {
			writeErr(w, e)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "data": data})
	})
}

// ---- 본인 확인 ----

type verifyBody struct {
	Mobile string `json:"mobile"`
	Code   string `json:"code"`
}

// verify는 휴대폰 번호와 6자리 인증번호로 본인 확인을 하고 회원 토큰을 준다. 실패 횟수는 X-Contact-Ref(상담 건)마다 센다.
func (s *Server) verify(q req) (any, *bErr) {
	ref := q.r.Header.Get(contactHeader)
	if ref == "" || len(ref) > maxRefLen {
		return nil, newErr(ErrBadInput, "X-Contact-Ref 헤더가 필요하다(1~128자)")
	}
	var b verifyBody
	if e := decode(q.body, &b); e != nil {
		return nil, e
	}
	if strings.TrimSpace(b.Mobile) == "" || strings.TrimSpace(b.Code) == "" {
		return nil, newErr(ErrBadInput, "mobile과 code가 필요하다")
	}
	if s.st.failures[ref] >= maxVerifyFailure {
		return nil, newErr(ErrAuthLocked, "이 상담 건은 본인 확인에 3회 실패해 잠겼다. 상담사가 처리한다(제15조 제1항).", "B-15")
	}
	if m, ok := s.ds.MemberByMobile(b.Mobile); ok && m.CheckOTP(b.Code) {
		tok := newToken()
		s.st.tokens[tok] = m.No
		return map[string]any{"ok": true, "member_no": m.No, "token": tok, "failures": s.st.failures[ref],
			"remaining_tries": maxVerifyFailure - s.st.failures[ref]}, nil
	}
	s.st.failures[ref]++
	f := s.st.failures[ref]
	out := map[string]any{"ok": false, "failures": f, "remaining_tries": maxVerifyFailure - f, "locked": f >= maxVerifyFailure}
	if f >= maxVerifyFailure {
		out["clauses"] = []string{"B-15"}
	}
	return out, nil
}

func newToken() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return "tk_" + hex.EncodeToString(b)
}

// requireMember는 쓰기 요청의 회원 토큰이 그 회원의 것인지 본다(제14조 제2항).
func (s *Server) requireMember(q req, memberNo string) *bErr {
	tok, ok := strings.CutPrefix(q.r.Header.Get("Authorization"), "Bearer ")
	if ok && memberNo != "" && s.st.tokens[strings.TrimSpace(tok)] == memberNo {
		return nil
	}
	return newErr(ErrAuthRequired, fmt.Sprintf("회원 %s의 본인 확인 토큰이 없다(제14조 제2항).", memberNo), "B-14")
}

func (s *Server) memberOf(no string) (*orgb.Member, *bErr) {
	m, ok := s.ds.Member(no)
	if !ok {
		return nil, newErr(ErrNoSuchResource, fmt.Sprintf("회원 %q가 없다", no))
	}
	return m, nil
}

// ---- 조회 ----

func (s *Server) member(q req) (any, *bErr) {
	m, e := s.memberOf(q.r.PathValue("member_no"))
	if e != nil {
		return nil, e
	}
	return map[string]any{"member_no": m.No, "member_name": m.Name, "birth_date": m.Birth, "state": m.State}, nil
}

type passView struct {
	PassNo        string     `json:"pass_no"`
	ProductCode   string     `json:"product_code"`
	ListPrice     int64      `json:"list_price"`
	StartedOn     orgb.Date  `json:"started_on"`
	RenewsOn      orgb.Date  `json:"renews_on"`
	State         string     `json:"state"` // ACTIVE | CANCELLED
	EndsOn        *orgb.Date `json:"ends_on,omitempty"`
	FamilyMembers int        `json:"family_members,omitempty"`
}

func (s *Server) passes(q req) (any, *bErr) {
	m, e := s.memberOf(q.r.PathValue("member_no"))
	if e != nil {
		return nil, e
	}
	items := []passView{}
	for _, p := range m.Passes {
		v := passView{PassNo: p.No, ProductCode: p.Product, ListPrice: p.ListPrice, StartedOn: p.StartedOn, RenewsOn: p.RenewsOn,
			State: "ACTIVE", FamilyMembers: p.FamilyMembers}
		if c, ok := s.st.cancels[p.No]; ok {
			end := c.ServiceEndsOn
			v.State, v.EndsOn = "CANCELLED", &end
		}
		items = append(items, v)
	}
	return map[string]any{"member_no": m.No, "items": items}, nil
}

// kst는 데이터의 KST 벽시계 값을 +09:00 시각으로 쓴다.
func kst(t time.Time) string { return t.Format("2006-01-02T15:04:05") + "+09:00" }

func (s *Server) charges(q req) (any, *bErr) {
	m, e := s.memberOf(q.r.PathValue("member_no"))
	if e != nil {
		return nil, e
	}
	items := []map[string]any{}
	for _, c := range m.Charges {
		v := map[string]any{"charge_no": c.No, "pass_no": c.PassNo, "charged_at": kst(c.At), "amount_krw": c.Amount,
			"pay_type": c.PayType, "chargeback": c.Chargeback}
		if rf, ok := s.st.refunds[c.No]; ok {
			v["refund_no"], v["refund_state"] = rf.RefundNo, rf.State
		}
		items = append(items, v)
	}
	return map[string]any{"member_no": m.No, "items": items}, nil
}

func queryDate(q req, key string) (orgb.Date, *bErr) {
	v := q.r.URL.Query().Get(key)
	if v == "" {
		return orgb.Date{}, nil
	}
	d, err := orgb.ParseDate(v)
	if err != nil {
		return orgb.Date{}, newErr(ErrBadInput, key+": "+err.Error())
	}
	return d, nil
}

func (s *Server) readingLog(q req) (any, *bErr) {
	m, e := s.memberOf(q.r.PathValue("member_no"))
	if e != nil {
		return nil, e
	}
	from, e := queryDate(q, "from")
	if e != nil {
		return nil, e
	}
	items := []map[string]any{}
	for _, r := range m.Reading {
		if !from.IsZero() && r.On.Before(from) {
			continue
		}
		items = append(items, map[string]any{"on": r.On, "kind": r.Kind, "by": r.By})
	}
	return map[string]any{"member_no": m.No, "items": items}, nil
}

func (s *Server) outages(q req) (any, *bErr) {
	from, e := queryDate(q, "from")
	if e != nil {
		return nil, e
	}
	to, e := queryDate(q, "to")
	if e != nil {
		return nil, e
	}
	items := []map[string]any{}
	for _, o := range s.ds.Outages {
		began := orgb.NewDate(o.Began.Year(), o.Began.Month(), o.Began.Day())
		ended := orgb.NewDate(o.Ended.Year(), o.Ended.Month(), o.Ended.Day())
		if (!from.IsZero() && ended.Before(from)) || (!to.IsZero() && began.After(to)) {
			continue
		}
		items = append(items, map[string]any{"outage_no": o.No, "began_at": kst(o.Began), "ended_at": kst(o.Ended),
			"minutes": o.Minutes, "fault": o.Fault})
	}
	return map[string]any{"items": items}, nil
}

// ---- 견적 ----

type estimateBody struct {
	MemberNo   string `json:"member_no"`
	PassNo     string `json:"pass_no"`
	RefundType string `json:"refund_type"`
	AsOf       string `json:"as_of"`
}

func (s *Server) estimate(q req) (any, *bErr) {
	var b estimateBody
	if e := decode(q.body, &b); e != nil {
		return nil, e
	}
	if b.MemberNo == "" || b.PassNo == "" || b.RefundType == "" || b.AsOf == "" {
		return nil, newErr(ErrBadInput, "member_no, pass_no, refund_type, as_of가 모두 필요하다")
	}
	asOf, err := orgb.ParseDate(b.AsOf)
	if err != nil {
		return nil, newErr(ErrBadInput, "as_of: "+err.Error())
	}
	est, err := s.ds.Estimate(orgb.EstimateRequest{MemberNo: b.MemberNo, PassNo: b.PassNo, Type: orgb.RefundType(b.RefundType), AsOf: asOf})
	if err != nil {
		if errors.Is(err, orgb.ErrNotFound) {
			return nil, newErr(ErrNoSuchResource, err.Error())
		}
		return nil, newErr(ErrBadInput, err.Error())
	}
	switch est.Outcome {
	case orgb.OutcomeNotRefundable:
		e := newErr(ErrNotRefundable, est.Note, est.Clauses...)
		e.Estimate = est
		return nil, e
	case orgb.OutcomeNeedsAgent:
		e := newErr(ErrNeedsAgent, est.Note, est.Clauses...)
		e.Estimate = est
		return nil, e
	}
	est.EstimateNo = s.st.nextID("EST")
	s.st.estimates[est.EstimateNo] = est
	return est, nil
}

// ---- 쓰기 ----

type refundBody struct {
	MemberNo   string `json:"member_no"`
	EstimateNo string `json:"estimate_no"`
	ChargeNo   string `json:"charge_no"`
}

// Refund는 환불 접수다. 만든 뒤에는 바꾸지 않는다.
type Refund struct {
	RefundNo      string          `json:"refund_no"`
	MemberNo      string          `json:"member_no"`
	ChargeNo      string          `json:"charge_no"`
	PassNo        string          `json:"pass_no"`
	EstimateNo    string          `json:"estimate_no"`
	RefundType    orgb.RefundType `json:"refund_type"`
	KRW           int64           `json:"krw"`
	State         string          `json:"state"` // DONE | REVIEW(상담사 승인 대기)
	Route         string          `json:"route"`
	ETAText       string          `json:"eta_text"`
	ReceivedOn    orgb.Date       `json:"received_on"`
	ServiceEndsOn *orgb.Date      `json:"service_ends_on,omitempty"`
	Clauses       []string        `json:"clauses"`
}

func (s *Server) refund(q req) (any, *bErr) {
	var b refundBody
	if e := decode(q.body, &b); e != nil {
		return nil, e
	}
	if b.MemberNo == "" || b.EstimateNo == "" || b.ChargeNo == "" {
		return nil, newErr(ErrBadInput, "member_no, estimate_no, charge_no가 모두 필요하다")
	}
	return s.idempotent(q, EPRefunds, b, func() (any, *bErr) {
		if _, e := s.memberOf(b.MemberNo); e != nil {
			return nil, e
		}
		est, ok := s.st.estimates[b.EstimateNo]
		if !ok {
			return nil, newErr(ErrNoSuchResource, fmt.Sprintf("견적 %q가 없다", b.EstimateNo))
		}
		if est.MemberNo != b.MemberNo {
			return nil, newErr(ErrBadInput, fmt.Sprintf("견적 %s는 회원 %s의 견적이 아니다", est.EstimateNo, b.MemberNo))
		}
		if !est.Refundable {
			return nil, newErr(ErrNotRefundable, "환불 예정액이 0원이라 접수하지 않는다. "+est.Note, est.Clauses...)
		}
		var item *orgb.EstimateItem
		for i := range est.Items {
			if est.Items[i].ChargeNo == b.ChargeNo {
				item = &est.Items[i]
			}
		}
		if item == nil {
			return nil, newErr(ErrBadInput, fmt.Sprintf("결제 %s는 견적 %s의 환불 대상이 아니다", b.ChargeNo, est.EstimateNo))
		}
		if prev, ok := s.st.refunds[item.ChargeNo]; ok {
			e := newErr(ErrAlreadyDone, fmt.Sprintf("결제 %s는 이미 환불 접수(%s)되었다", item.ChargeNo, prev.RefundNo))
			e.Existing = prev
			return nil, e
		}
		if item.NeedsBankAccount {
			if _, ok := s.st.accounts[b.MemberNo]; !ok {
				return nil, newErr(ErrBankAccountMissing, "결제한 달이 지난 휴대폰 결제는 회원 본인 명의 계좌로 환불한다. 환불 계좌를 먼저 등록해야 한다(제13조 제3항).", "B-13")
			}
		}
		state := "DONE"
		if est.NeedsReview {
			state = "REVIEW"
		}
		rf := &Refund{RefundNo: s.st.nextID("RFB"), MemberNo: b.MemberNo, ChargeNo: item.ChargeNo, PassNo: item.PassNo,
			EstimateNo: est.EstimateNo, RefundType: est.Type, KRW: item.KRW, State: state, Route: item.Route, ETAText: item.ETAText,
			ReceivedOn: est.AsOf, Clauses: append(append([]string{}, est.Clauses...), "B-13", "B-16")}
		if est.Type.EndsService() {
			d := est.AsOf
			rf.ServiceEndsOn = &d
		}
		s.st.refunds[item.ChargeNo] = rf
		return rf, nil
	}, b.MemberNo)
}

type cancelBody struct {
	MemberNo string `json:"member_no"`
}

// Cancel은 이용권 해지 접수다.
type Cancel struct {
	CancelNo      string    `json:"cancel_no"`
	MemberNo      string    `json:"member_no"`
	PassNo        string    `json:"pass_no"`
	ServiceEndsOn orgb.Date `json:"service_ends_on"`
	MembersEnded  int       `json:"members_ended"`
	Clauses       []string  `json:"clauses"`
}

func (s *Server) cancel(q req) (any, *bErr) {
	var b cancelBody
	if e := decode(q.body, &b); e != nil {
		return nil, e
	}
	passNo := q.r.PathValue("pass_no")
	if b.MemberNo == "" {
		return nil, newErr(ErrBadInput, "member_no가 필요하다")
	}
	key := struct {
		cancelBody
		PassNo string `json:"pass_no"`
	}{b, passNo}
	return s.idempotent(q, EPCancel, key, func() (any, *bErr) {
		m, e := s.memberOf(b.MemberNo)
		if e != nil {
			return nil, e
		}
		p, ok := m.Pass(passNo)
		if !ok {
			return nil, newErr(ErrNoSuchResource, fmt.Sprintf("회원 %s에게 이용권 %q가 없다", m.No, passNo))
		}
		if prev, ok := s.st.cancels[p.No]; ok {
			e := newErr(ErrAlreadyDone, fmt.Sprintf("이용권 %s는 이미 해지 접수(%s)되었다", p.No, prev.CancelNo))
			e.Existing = prev
			return nil, e
		}
		var end *orgb.ServiceEnd
		for _, c := range m.Charges {
			if rf, ok := s.st.refunds[c.No]; ok && c.PassNo == p.No && rf.RefundType.EndsService() {
				end = &orgb.ServiceEnd{Type: rf.RefundType, On: rf.ReceivedOn}
			}
		}
		res := s.ds.Cancel(p, end)
		c := &Cancel{CancelNo: s.st.nextID("CNB"), MemberNo: m.No, PassNo: p.No, ServiceEndsOn: res.ServiceEndsOn,
			MembersEnded: res.MembersEnded, Clauses: res.Clauses}
		s.st.cancels[p.No] = c
		return c, nil
	}, b.MemberNo)
}

type accountBody struct {
	BankName  string `json:"bank_name"`
	AccountNo string `json:"account_no"`
	OwnerName string `json:"owner_name"`
}

// Account는 환불 계좌 등록 결과다.
type Account struct {
	Accepted      bool     `json:"accepted"`
	RegNo         string   `json:"reg_no,omitempty"`
	MemberNo      string   `json:"member_no"`
	BankName      string   `json:"bank_name"`
	AccountMasked string   `json:"account_masked"`
	OwnerName     string   `json:"owner_name"`
	RejectCode    string   `json:"reject_code,omitempty"` // OWNER_MISMATCH
	Detail        string   `json:"detail,omitempty"`
	Clauses       []string `json:"clauses"`
}

func (s *Server) refundAccount(q req) (any, *bErr) {
	var b accountBody
	if e := decode(q.body, &b); e != nil {
		return nil, e
	}
	memberNo := q.r.PathValue("member_no")
	if strings.TrimSpace(b.BankName) == "" || strings.TrimSpace(b.OwnerName) == "" || !validAccount(b.AccountNo) {
		return nil, newErr(ErrBadInput, "bank_name, owner_name과 숫자·하이픈으로 된 account_no(6~20자리)가 필요하다")
	}
	key := struct {
		accountBody
		MemberNo string `json:"member_no"`
	}{b, memberNo}
	return s.idempotent(q, EPAccount, key, func() (any, *bErr) {
		m, e := s.memberOf(memberNo)
		if e != nil {
			return nil, e
		}
		if prev, ok := s.st.accounts[m.No]; ok {
			e := newErr(ErrAlreadyDone, fmt.Sprintf("회원 %s의 환불 계좌는 이미 등록(%s)되었다", m.No, prev.RegNo))
			e.Existing = prev
			return nil, e
		}
		a := &Account{MemberNo: m.No, BankName: b.BankName, AccountMasked: maskAccount(b.AccountNo), OwnerName: b.OwnerName,
			Clauses: []string{"B-13"}}
		if !m.OwnerMatches(b.OwnerName) {
			a.RejectCode = "OWNER_MISMATCH"
			a.Detail = "환불 계좌는 회원 본인 명의여야 한다(제13조 제3항)."
			return a, errNotStored
		}
		a.Accepted, a.RegNo = true, s.st.nextID("ACB")
		s.st.accounts[m.No] = a
		return a, nil
	}, memberNo)
}

type ticketBody struct {
	MemberNo string `json:"member_no"`
	Topic    string `json:"topic"`
	Memo     string `json:"memo"`
}

// ticket은 상담사 연결 요청이다. 제3자 문의와 본인 확인 실패에도 쓰므로 회원 토큰을 요구하지 않는다.
func (s *Server) ticket(q req) (any, *bErr) {
	var b ticketBody
	if e := decode(q.body, &b); e != nil {
		return nil, e
	}
	if strings.TrimSpace(b.Topic) == "" {
		return nil, newErr(ErrBadInput, "topic이 필요하다")
	}
	if b.MemberNo != "" {
		if _, e := s.memberOf(b.MemberNo); e != nil {
			return nil, e
		}
	}
	create := func() (any, *bErr) {
		return map[string]any{"ticket_no": s.st.nextID("TK"), "member_no": b.MemberNo, "topic": b.Topic, "memo": b.Memo,
			"eta_minutes": ticketWait}, nil
	}
	if q.r.Header.Get(idemHeader) == "" {
		return create()
	}
	return s.idempotent(q, EPTickets, b, create, "")
}

// errNotStored는 성공 응답이지만 멱등 결과로 저장하지 않는다는 표시다(계좌 예금주 불일치 거절).
var errNotStored = &bErr{Code: "__not_stored__"}

// idempotent는 Idempotency-Key 헤더로 쓰기를 한 번만 실행한다. 같은 키에 다른 내용이면 IDEMPOTENCY_MISMATCH다.
// memberNo가 있으면 회원 토큰을 먼저 확인한다. 오류와 거절은 저장하지 않는다.
func (s *Server) idempotent(q req, endpoint string, fingerprintOf any, run func() (any, *bErr), memberNo string) (any, *bErr) {
	key := q.r.Header.Get(idemHeader)
	if key == "" || len(key) > 200 {
		return nil, newErr(ErrBadInput, "Idempotency-Key 헤더가 필요하다")
	}
	if memberNo != "" {
		if e := s.requireMember(q, memberNo); e != nil {
			return nil, e
		}
	}
	fp, _ := json.Marshal(fingerprintOf)
	k := endpoint + "\x00" + key
	if prev, ok := s.st.idem[k]; ok {
		if prev.fingerprint != string(fp) {
			return nil, newErr(ErrIdempotencyMismatch, "이 Idempotency-Key는 다른 요청 내용으로 이미 쓰였다")
		}
		return prev.resp, nil
	}
	resp, e := run()
	if e == errNotStored {
		return resp, nil
	}
	if e != nil {
		return nil, e
	}
	s.st.idem[k] = idemEntry{fingerprint: string(fp), resp: resp}
	return resp, nil
}

// ---- 관리 ----

func (s *Server) handleReset(w http.ResponseWriter, _ *http.Request) {
	s.mu.Lock()
	s.st = newState()
	s.mu.Unlock()
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "data": map[string]any{"reset": true}})
}

type faultBody struct {
	Endpoint string `json:"endpoint"`
	Mode     string `json:"mode"` // timeout | maintenance
	Times    int    `json:"times"`
}

// handleFaults는 다음 times번의 endpoint 호출이 UPSTREAM_TIMEOUT(timeout) 또는 MAINTENANCE(maintenance)를 돌려주게 한다.
func (s *Server) handleFaults(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBodyBytes))
	if err != nil {
		writeErr(w, newErr(ErrBadInput, "요청 본문을 읽을 수 없다"))
		return
	}
	var b faultBody
	if e := decode(body, &b); e != nil {
		writeErr(w, e)
		return
	}
	known := false
	for _, ep := range endpoints {
		known = known || ep == b.Endpoint
	}
	if !known || (b.Mode != "timeout" && b.Mode != "maintenance") {
		writeErr(w, newErr(ErrBadInput, "endpoint는 알려진 이름, mode는 timeout 또는 maintenance여야 한다"))
		return
	}
	if b.Times == 0 {
		b.Times = 1
	}
	if b.Times < 0 || b.Times > 1000 {
		writeErr(w, newErr(ErrBadInput, "times는 1~1000이어야 한다"))
		return
	}
	code := ErrUpstreamTimeout
	if b.Mode == "maintenance" {
		code = ErrMaintenance
	}
	s.mu.Lock()
	s.st.faults[b.Endpoint] = &fault{code: code, remaining: b.Times}
	s.mu.Unlock()
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "data": b})
}

// ---- 공통 ----

func decode(body []byte, v any) *bErr {
	if len(bytes.TrimSpace(body)) == 0 {
		body = []byte("{}")
	}
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return newErr(ErrBadInput, "요청 본문이 올바른 JSON이 아니다: "+err.Error())
	}
	if dec.More() {
		return newErr(ErrBadInput, "요청 본문에 JSON 값이 두 개 이상 있다")
	}
	return nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(v)
}

func writeErr(w http.ResponseWriter, e *bErr) {
	if rec, ok := w.(*recorder); ok {
		rec.code = e.Code
	}
	out := map[string]any{"status": "error", "error_code": e.Code, "detail": e.Detail}
	if len(e.Clauses) > 0 {
		out["clauses"] = e.Clauses
	}
	if e.Estimate != nil {
		out["estimate"] = e.Estimate
	}
	if e.Existing != nil {
		out["existing"] = e.Existing
	}
	writeJSON(w, statusFor(e.Code), out)
}

type recorder struct {
	http.ResponseWriter
	status   int
	code     string
	endpoint string
}

func (rec *recorder) WriteHeader(status int) {
	rec.status = status
	rec.ResponseWriter.WriteHeader(status)
}

// withLog는 요청마다 JSON 로그 한 줄을 남긴다. 개인정보가 있으므로 본문은 남기지 않는다.
func (s *Server) withLog(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &recorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		s.log.LogAttrs(r.Context(), slog.LevelInfo, "request",
			slog.String("endpoint", rec.endpoint), slog.String("method", r.Method), slog.Int("status", rec.status),
			slog.String("error_code", rec.code), slog.Float64("latency_ms", float64(time.Since(start).Microseconds())/1000))
	})
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

// maskAccount는 계좌번호의 마지막 숫자 3개만 남긴다.
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
