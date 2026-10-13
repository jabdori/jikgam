package server

import (
	"fmt"

	"github.com/jabdori/jikgam/bizapi/internal/orga"
)

// maxFailedVerifications번 본인 확인에 실패하면 자동 응대를 멈추고 상담사에게 연결한다(제19조 제1항).
const maxFailedVerifications = 2

// state는 서버의 바뀌는 상태다. 모두 메모리에 두고 /_admin/reset으로 비운다.
// Server.mu를 잡은 상태에서만 접근한다.
type state struct {
	sessions map[string]*session
	quotes   map[string]*orga.Quote          // quote_id → 견적
	refunds  map[string]*RefundReceipt       // payment_id → 환불 접수
	cancels  map[string]*CancellationReceipt // subscription_id → 해지 접수(함께 해지된 부가 상품 포함)
	accounts map[string]*AccountReceipt      // customer_id → 환불 계좌 등록
	idem     map[string]idemEntry            // 도구 이름 + 멱등 키 → 처음 결과
	faults   map[string]*fault               // 도구 이름 → 주입한 장애
	seq      map[string]int                  // 접수 번호 접두어 → 마지막 번호
}

func newState() *state {
	return &state{
		sessions: map[string]*session{},
		quotes:   map[string]*orga.Quote{},
		refunds:  map[string]*RefundReceipt{},
		cancels:  map[string]*CancellationReceipt{},
		accounts: map[string]*AccountReceipt{},
		idem:     map[string]idemEntry{},
		faults:   map[string]*fault{},
		seq:      map[string]int{},
	}
}

// session은 X-Session-ID 하나의 본인 확인 상태다.
type session struct {
	verifiedCustomer string // 마지막으로 본인 확인에 성공한 고객
	attempts         int    // verify_identity 호출 수
	failed           int    // 실패 수(누적)
}

func (s *session) locked() bool { return s.failed >= maxFailedVerifications }

func (st *state) session(id string) *session {
	s, ok := st.sessions[id]
	if !ok {
		s = &session{}
		st.sessions[id] = s
	}
	return s
}

func (st *state) nextID(prefix string) string {
	st.seq[prefix]++
	return fmt.Sprintf("%s-%06d", prefix, st.seq[prefix])
}

// idemEntry는 성공한 쓰기 요청의 처음 결과다. 거절·오류는 저장하지 않으므로 같은 키로 다시 시도할 수 있다.
type idemEntry struct {
	fingerprint string
	resp        any
}

func idemKey(tool, key string) string { return tool + "\x00" + key }

type fault struct {
	code      string
	remaining int
}

// takeFault는 도구에 주입한 장애가 남아 있으면 하나 소비하고 오류를 돌려준다.
func (st *state) takeFault(tool string) *apiError {
	f, ok := st.faults[tool]
	if !ok || f.remaining <= 0 {
		return nil
	}
	f.remaining--
	if f.remaining == 0 {
		delete(st.faults, tool)
	}
	return newError(f.code, "주입된 장애: "+tool)
}

// RefundReceipt는 환불 접수 내역이다. 만든 뒤에는 바꾸지 않는다(멱등 재응답이 같아야 하므로).
type RefundReceipt struct {
	RefundID       string      `json:"refund_id"`
	CustomerID     string      `json:"customer_id"`
	PaymentID      string      `json:"payment_id"`
	SubscriptionID string      `json:"subscription_id"`
	QuoteID        string      `json:"quote_id"`
	Reason         orga.Reason `json:"reason"`
	Amount         int64       `json:"amount"`
	Status         string      `json:"status"` // confirmed | pending_approval
	RefundMethod   string      `json:"refund_method"`
	ExpectedPeriod string      `json:"expected_period"`
	ReceivedOn     orga.Date   `json:"received_on"`
	LastUsableDate *orga.Date  `json:"last_usable_date,omitempty"`
	ArticleIDs     []string    `json:"article_ids"`
}

const (
	refundConfirmed       = "confirmed"
	refundPendingApproval = "pending_approval"
)

type CancellationReceipt struct {
	CancellationID           string    `json:"cancellation_id"`
	CustomerID               string    `json:"customer_id"`
	SubscriptionID           string    `json:"subscription_id"`
	LastUsableDate           orga.Date `json:"last_usable_date"`
	CancelledSubscriptionIDs []string  `json:"cancelled_subscription_ids"`
	ArticleIDs               []string  `json:"article_ids"`
}

type AccountReceipt struct {
	RegistrationID string   `json:"registration_id,omitempty"`
	CustomerID     string   `json:"customer_id"`
	Registered     bool     `json:"registered"`
	Bank           string   `json:"bank"`
	AccountMasked  string   `json:"account_masked"`
	Holder         string   `json:"holder"`
	RejectReason   string   `json:"reject_reason,omitempty"` // holder_not_customer
	Message        string   `json:"message,omitempty"`
	ArticleIDs     []string `json:"article_ids"`
}

type HandoffReceipt struct {
	HandoffID           string `json:"handoff_id"`
	CustomerID          string `json:"customer_id,omitempty"`
	Reason              string `json:"reason"`
	Summary             string `json:"summary,omitempty"`
	ExpectedWaitMinutes int    `json:"expected_wait_minutes"`
}
