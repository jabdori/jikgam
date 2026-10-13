package orga

import (
	"errors"
	"fmt"
	"slices"
	"strings"
	"time"
)

// 약관 수치. 별표 1(T-01), 제2조 제7항, 제8조, 제9조, 제13조, 제19조 제2항, 부칙 제1조.
const (
	NormalMonthlyFee  int64 = 9_900  // 정상 월 요금(T-01)
	AnnualNormalPrice int64 = 99_000 // 연 구독 정상 요금(T-01)
	ApprovalThreshold int64 = 50_000 // 이 금액을 넘으면 상담사 승인(제19조 제2항)

	withdrawalDays   = 7  // 결제일 다음 날을 1일째로 세어 7일째까지(제2조 제7항)
	penaltyRatePct   = 10 // 연 구독 중도 해지 위약금(제8조 제1항)
	minorAgeLimit    = 19 // 결제 당시 만 19세 미만(제13조 제1항)
	duplicateMonths  = 12 // 중복 결제 환불 신청 기한 1년(제10조 제2항)
	incidentMinHours = 4  // 연속 4시간 이상 중단(제9조)
)

// addendumCutoff 이전(포함)에 결제한 연 구독에는 위약금을 적용하지 않는다(부칙 제1조 제2항).
var addendumCutoff = NewDate(2026, time.June, 30)

// Reason은 quote_refund의 환불 사유다(api.md).
type Reason string

const (
	ReasonWithdrawal   Reason = "withdrawal"    // 청약철회(제5조)
	ReasonAnnualCancel Reason = "annual_cancel" // 연 구독 중도 해지(제8조)
	ReasonDuplicate    Reason = "duplicate"     // 중복 결제(제10조)
	ReasonIncident     Reason = "incident"      // 장애 환불(제9조 제2항)
	ReasonMinor        Reason = "minor"         // 미성년자 결제 취소(제13조)
	ReasonTrialCancel  Reason = "trial_cancel"  // 무료 체험 해지(제12조 제1항)
	ReasonPlanChange   Reason = "plan_change"   // 월→연 변경 일할 차감(제17조 제1항)
)

func (r Reason) Valid() bool {
	switch r {
	case ReasonWithdrawal, ReasonAnnualCancel, ReasonDuplicate, ReasonIncident, ReasonMinor, ReasonTrialCancel, ReasonPlanChange:
		return true
	}
	return false
}

// EndsPeriod는 이 사유로 환불하면 이용 기간이 신청일에 끝나는지다(제5조 제3항, 제8조 제4항, 제9조 제2항).
func (r Reason) EndsPeriod() bool {
	return r == ReasonWithdrawal || r == ReasonAnnualCancel || r == ReasonIncident
}

// 환불 방법(제14조).
const (
	RefundCardCancel    = "card_cancel"     // 카드 승인 취소
	RefundMobileCancel  = "mobile_cancel"   // 휴대폰 결제 취소(결제한 달 안에 접수)
	RefundBankTransfer  = "bank_transfer"   // 회원 본인 명의 계좌 입금(휴대폰 결제, 결제한 달이 지남)
	RefundEasyPayCancel = "easy_pay_cancel" // 간편결제 서비스의 취소 절차
	RefundOriginal      = "original_method" // 그 밖의 수단: 원래 결제 수단
)

var (
	ErrNotFound = errors.New("not found")
	ErrInvalid  = errors.New("invalid request")
)

// Verdict는 견적 판단 결과다. 서버는 이것을 HTTP 응답(200, NOT_ELIGIBLE, HANDOFF_REQUIRED)으로 바꾼다.
type Verdict int

const (
	VerdictOK          Verdict = iota // 약관 규정이 적용된다. 환불액이 0원일 수도 있다(제8조 제3항, 체험 해지).
	VerdictNotEligible                // 약관상 대상이 아니다.
	VerdictHandoff                    // 제19조 제1항 사유로 상담사가 처리한다.
)

type QuoteRequest struct {
	CustomerID     string
	SubscriptionID string
	Reason         Reason
	RequestDate    Date
}

// Quote는 환불 견적이다. 금액과 자격은 모두 여기서 정하고, 상담 엔진은 계산하지 않는다.
type Quote struct {
	QuoteID        string `json:"quote_id,omitempty"`
	CustomerID     string `json:"customer_id"`
	SubscriptionID string `json:"subscription_id"`
	Reason         Reason `json:"reason"`
	RequestDate    Date   `json:"request_date"`
	// Eligible은 환불할 금액이 있는지다(RefundAmount > 0). 규정은 적용되지만 금액이 0원이면 false다.
	Eligible bool `json:"eligible"`
	// RefundAmount는 환불 예정액 합계다. plan_change에서는 환불이 아니라 연 구독 결제 금액에서 빼는 일할 차감액이다.
	RefundAmount int64        `json:"refund_amount"`
	Refunds      []RefundLine `json:"refunds"`
	Breakdown    Breakdown    `json:"breakdown"`
	ArticleIDs   []string     `json:"article_ids"`
	// ApprovalRequired는 환불 예정액이 50,000원을 넘어 접수 후 상담사 승인이 필요한지다(제19조 제2항).
	ApprovalRequired bool   `json:"approval_required"`
	HandoffRequired  bool   `json:"handoff_required"`
	HandoffReason    string `json:"handoff_reason,omitempty"` // minor | dispute
	// LastUsableDate는 이 견적대로 처리했을 때 서비스를 이용할 수 있는 마지막 날이다(제20조 제3호).
	// 구독이 유지되는 사유(중복 결제, 요금제 변경)에는 없다.
	LastUsableDate *Date   `json:"last_usable_date,omitempty"`
	Message        string  `json:"message"`
	Verdict        Verdict `json:"-"`

	planChangeCredit int64
}

// RefundLine은 결제 한 건의 환불 예정 내역이다. create_refund_request는 이 단위로 접수한다.
type RefundLine struct {
	PaymentID       string `json:"payment_id"`
	SubscriptionID  string `json:"subscription_id"`
	Amount          int64  `json:"amount"`
	PaymentMethod   string `json:"payment_method"`
	RefundMethod    string `json:"refund_method"`
	AccountRequired bool   `json:"account_required"`
	ExpectedPeriod  string `json:"expected_period"`
}

// Breakdown은 판단에 쓴 사실과 계산 과정이다. 사유에 해당하는 항목만 채운다.
type Breakdown struct {
	Payment      *PaymentFacts   `json:"payment,omitempty"`
	Withdrawal   *WithdrawalCalc `json:"withdrawal,omitempty"`
	Incident     *IncidentCalc   `json:"incident,omitempty"`
	AnnualCancel *AnnualCalc     `json:"annual_cancel,omitempty"`
	Duplicate    *DuplicateCalc  `json:"duplicate,omitempty"`
	TrialCancel  *TrialCalc      `json:"trial_cancel,omitempty"`
	PlanChange   *PlanChangeCalc `json:"plan_change,omitempty"`
}

// PaymentFacts는 견적 대상 결제다.
type PaymentFacts struct {
	PaymentID      string `json:"payment_id"`
	Date           Date   `json:"date"`
	Amount         int64  `json:"amount"`
	Method         string `json:"method"`
	Dispute        bool   `json:"dispute"`
	AgeAtPayment   int    `json:"age_at_payment"`
	MinorAtPayment bool   `json:"minor_at_payment"`
}

type WithdrawalCalc struct {
	Deadline     Date `json:"deadline"`      // 결제일 다음 날을 1일째로 센 7일째
	DayCount     int  `json:"day_count"`     // 견적 기준일이 결제일 다음 날부터 며칠째인지
	WithinPeriod bool `json:"within_period"` // 결제일 ≤ 기준일 ≤ 기한
	// UsageDates는 결제일부터 기준일까지의 이용 이력이다. 무료 체험 중 이용은 빼고 센다(제12조 제2항).
	UsageDates         []Date `json:"usage_dates"`
	TrialUsageExcluded int    `json:"trial_usage_excluded"`
}

type IncidentCalc struct {
	Deadline     Date `json:"deadline"`
	DayCount     int  `json:"day_count"`
	WithinPeriod bool `json:"within_period"`
	// QualifyingIncidentIDs는 결제일부터 min(기한, 기준일) 사이에 시작한 회사 귀책 연속 4시간 이상 장애다.
	QualifyingIncidentIDs []string `json:"qualifying_incident_ids"`
}

type AnnualCalc struct {
	PaymentAmount    int64 `json:"payment_amount"`
	StartDate        Date  `json:"start_date"`
	MonthsUsed       int   `json:"months_used"`
	NormalMonthlyFee int64 `json:"normal_monthly_fee"`
	Deduction        int64 `json:"deduction"`            // 이용 개월 수 × 정상 월 요금
	AfterDeduction   int64 `json:"after_deduction"`      // max(0, 결제 금액 − 차감액)
	PenaltyRatePct   int   `json:"penalty_rate_pct"`     // 10, 부칙 경과 조치면 0
	Penalty          int64 `json:"penalty"`              // 10원 미만 버림
	AddendumApplied  bool  `json:"addendum_a21_applied"` // 2026-06-30 이전 결제라 위약금 없음
	Promo            bool  `json:"promo"`                // 프로모션 가입이어도 차감은 정상 월 요금(제11조 제2항)
	Refund           int64 `json:"refund"`               // 10원 미만 버림
}

type DuplicateCalc struct {
	Groups []DuplicateGroup `json:"groups"`
}

// DuplicateGroup은 같은 날(같은 이용 기간)에 이루어진 결제 묶음이다.
type DuplicateGroup struct {
	Date                Date     `json:"date"`
	NormalPaymentID     string   `json:"normal_payment_id"`     // 가장 먼저 승인된 결제
	DuplicatePaymentIDs []string `json:"duplicate_payment_ids"` // 나머지
	Deadline            Date     `json:"deadline"`              // 결제일로부터 1년
	WithinPeriod        bool     `json:"within_period"`
}

type TrialCalc struct {
	TrialEnd Date  `json:"trial_end"`
	InTrial  bool  `json:"in_trial"`
	Charge   int64 `json:"charge"`
}

type PlanChangeCalc struct {
	PaymentID      string `json:"payment_id"`
	MonthlyPaid    int64  `json:"monthly_paid"`
	PeriodStart    Date   `json:"period_start"`
	NextBilling    Date   `json:"next_billing"`
	PeriodDays     int    `json:"period_days"`    // 결제일부터 다음 결제일 전날까지
	RemainingDays  int    `json:"remaining_days"` // 변경 신청일 다음 날부터 다음 결제일 전날까지
	ProratedCredit int64  `json:"prorated_credit"`
	AnnualPrice    int64  `json:"annual_price"` // 정상 요금(제17조 제2항)
	AnnualCharge   int64  `json:"annual_charge"`
}

// Quote는 환불 견적을 낸다. 시계를 보지 않고 RequestDate만 기준으로 판단한다.
// 입력 오류는 ErrNotFound·ErrInvalid로 돌려주고, 약관 판단 결과는 Quote.Verdict에 담는다.
func (ds *Dataset) Quote(req QuoteRequest) (*Quote, error) {
	c, ok := ds.Customer(req.CustomerID)
	if !ok {
		return nil, fmt.Errorf("%w: 고객 %q가 없다", ErrNotFound, req.CustomerID)
	}
	sub, ok := c.Subscription(req.SubscriptionID)
	if !ok {
		return nil, fmt.Errorf("%w: 고객 %s에게 구독 %q가 없다", ErrNotFound, c.ID, req.SubscriptionID)
	}
	if !req.Reason.Valid() {
		return nil, fmt.Errorf("%w: 알 수 없는 reason %q", ErrInvalid, req.Reason)
	}
	if req.RequestDate.IsZero() {
		return nil, fmt.Errorf("%w: request_date가 필요하다", ErrInvalid)
	}
	if req.RequestDate.Before(sub.Start) {
		return nil, fmt.Errorf("%w: request_date %s가 구독 시작일 %s보다 앞선다", ErrInvalid, req.RequestDate, sub.Start)
	}
	q := &Quote{
		CustomerID:     c.ID,
		SubscriptionID: sub.ID,
		Reason:         req.Reason,
		RequestDate:    req.RequestDate,
		Refunds:        []RefundLine{},
		ArticleIDs:     []string{},
	}
	ds.decide(q, c, sub)
	if q.Verdict == VerdictOK {
		q.settle()
	}
	return q, nil
}

func (ds *Dataset) decide(q *Quote, c *Customer, sub *Subscription) {
	if sub.Plan == PlanFamilyAddon && q.Reason != ReasonDuplicate {
		q.reject("가족 공유 부가 상품만 해지하면 이미 결제한 부가 상품 요금은 환불하지 않는다(제18조 제2항). "+
			"본 구독을 해지할 때의 부가 상품 환불은 본 구독 견적에 함께 계산한다(제18조 제3항).", "A-18")
		return
	}
	switch q.Reason {
	case ReasonDuplicate:
		ds.decideDuplicate(q, c, sub)
		return
	case ReasonTrialCancel:
		decideTrial(q, sub)
		return
	}

	// 이하 사유는 견적 기준일 현재 이용 기간의 결제 한 건이 대상이다.
	p, ok := c.currentPayment(sub.ID, q.RequestDate)
	if !ok {
		arts := []string{primaryArticle(q.Reason)}
		if sub.Trial {
			arts = append(arts, "A-12")
		}
		q.reject(fmt.Sprintf("견적 기준일 %s까지 이 구독의 결제 내역이 없다.", q.RequestDate), arts...)
		return
	}
	age := AgeOn(c.Birth, p.Date)
	q.Breakdown.Payment = &PaymentFacts{
		PaymentID: p.ID, Date: p.Date, Amount: p.Amount, Method: p.Method, Dispute: p.Dispute,
		AgeAtPayment: age, MinorAtPayment: age < minorAgeLimit,
	}
	if p.Dispute {
		q.handoff("dispute", fmt.Sprintf("결제 %s는 카드사 또는 결제대행사에 이의 제기가 진행 중이라 자동 응대로 처리하지 않는다(제19조 제1항).", p.ID), "A-19")
		return
	}
	if q.Reason == ReasonMinor {
		if age < minorAgeLimit {
			q.handoff("minor", fmt.Sprintf("결제 당시 만 %d세였다. 미성년자 결제 취소는 법정대리인 관계 확인 서류가 필요해 상담사가 처리한다(제13조 제2항, 제19조 제1항). 법정대리인 동의 여부는 시스템에서 확인할 수 없다.", age), "A-13", "A-19")
		} else {
			q.reject(fmt.Sprintf("결제 당시 만 %d세로 미성년자 결제 취소 대상이 아니다(제13조 제1항).", age), "A-13")
		}
		return
	}
	if c.Status == StatusSuspended && q.Reason.EndsPeriod() {
		q.reject("약관 위반으로 이용이 정지된 계정은 제5조, 제8조, 제9조 제2항에 따른 환불 대상에서 제외한다(제16조).", "A-16", primaryArticle(q.Reason))
		return
	}
	switch q.Reason {
	case ReasonWithdrawal:
		ds.decideWithdrawal(q, c, sub, p)
	case ReasonIncident:
		ds.decideIncident(q, c, sub, p)
	case ReasonAnnualCancel:
		decideAnnual(q, sub, p)
	case ReasonPlanChange:
		decidePlanChange(q, sub, p)
	}
}

func primaryArticle(r Reason) string {
	switch r {
	case ReasonWithdrawal:
		return "A-05"
	case ReasonAnnualCancel:
		return "A-08"
	case ReasonIncident:
		return "A-09"
	case ReasonDuplicate:
		return "A-10"
	case ReasonTrialCancel:
		return "A-12"
	case ReasonMinor:
		return "A-13"
	case ReasonPlanChange:
		return "A-17"
	}
	return ""
}

// 청약철회(제5조, 제6조, 제12조 제2항).
func (ds *Dataset) decideWithdrawal(q *Quote, c *Customer, sub *Subscription, p Payment) {
	calc := withdrawalCalc(c, p, q.RequestDate)
	q.Breakdown.Withdrawal = &calc
	arts := []string{"A-05"}
	if calc.TrialUsageExcluded > 0 {
		arts = append(arts, "A-12")
	}
	switch {
	case !calc.WithinPeriod:
		q.reject(fmt.Sprintf("청약철회 기간(결제일 %s 다음 날부터 7일째인 %s까지)이 지났다(제5조 제1항, 제2조 제7항).", p.Date, calc.Deadline), arts...)
	case len(calc.UsageDates) > 0:
		q.reject(fmt.Sprintf("결제일 이후 이용 이력(%s)이 있어 청약철회를 할 수 없다(제6조).", joinDates(calc.UsageDates)), append(arts, "A-06")...)
	default:
		q.ok(fmt.Sprintf("결제일 %s로부터 %d일째이고 결제 후 이용 이력이 없어 결제 금액 전액을 환불한다. 청약철회로 이용 기간은 신청일에 끝난다(제5조).", p.Date, calc.DayCount), arts...)
		q.addLine(p, p.Amount)
		q.endsOn(q.RequestDate)
		ds.addAddonLines(q, c, sub)
	}
}

func withdrawalCalc(c *Customer, p Payment, on Date) WithdrawalCalc {
	deadline := p.Date.AddDays(withdrawalDays)
	calc := WithdrawalCalc{
		Deadline:     deadline,
		DayCount:     DaysBetween(p.Date, on),
		WithinPeriod: !on.Before(p.Date) && !on.After(deadline),
		UsageDates:   []Date{},
	}
	for _, u := range c.Usage {
		if u.Date.After(on) {
			continue
		}
		if u.DuringTrial {
			calc.TrialUsageExcluded++
			continue
		}
		if !u.Date.Before(p.Date) {
			calc.UsageDates = append(calc.UsageDates, u.Date)
		}
	}
	return calc
}

// 장애 환불(제9조 제2항·제3항).
func (ds *Dataset) decideIncident(q *Quote, c *Customer, sub *Subscription, p Payment) {
	calc := ds.incidentCalc(p, q.RequestDate)
	q.Breakdown.Incident = &calc
	switch {
	case !calc.WithinPeriod:
		q.reject(fmt.Sprintf("장애 전액 환불은 결제일로부터 7일 이내(%s까지)에 해지를 신청한 경우에만 적용한다(제9조 제2항).", calc.Deadline), "A-09")
	case len(calc.QualifyingIncidentIDs) == 0:
		q.reject(fmt.Sprintf("결제일 %s부터 견적 기준일까지 회사 귀책으로 연속 4시간 이상 서비스가 중단된 장애 기록이 없다(제9조 제2항·제3항).", p.Date), "A-09")
	default:
		q.ok(fmt.Sprintf("결제일로부터 7일 이내에 회사 귀책 연속 4시간 이상 장애(%s)가 있었고 기간 안에 해지를 신청해 이용 이력과 관계없이 결제 금액 전액을 환불한다. 이용 기간은 해지 신청일에 끝난다(제9조 제2항).", strings.Join(calc.QualifyingIncidentIDs, ", ")), "A-09")
		q.addLine(p, p.Amount)
		q.endsOn(q.RequestDate)
		ds.addAddonLines(q, c, sub)
	}
}

func (ds *Dataset) incidentCalc(p Payment, on Date) IncidentCalc {
	deadline := p.Date.AddDays(withdrawalDays)
	calc := IncidentCalc{
		Deadline:              deadline,
		DayCount:              DaysBetween(p.Date, on),
		WithinPeriod:          !on.Before(p.Date) && !on.After(deadline),
		QualifyingIncidentIDs: []string{},
	}
	last := deadline
	if on.Before(last) {
		last = on
	}
	for _, inc := range ds.Incidents {
		if inc.Cause != CauseCompany || inc.Duration() < incidentMinHours*time.Hour {
			continue
		}
		if sd := inc.StartDate(); sd.Before(p.Date) || sd.After(last) {
			continue
		}
		calc.QualifyingIncidentIDs = append(calc.QualifyingIncidentIDs, inc.ID)
	}
	return calc
}

// addAddonLines는 본 구독을 청약철회·장애 환불할 때 함께 해지되는 가족 공유 부가 상품의 환불을 더한다.
// 부가 상품 환불 여부는 본 구독에 적용되는 규정을 따르므로(제18조 제3항), 같은 규정을 부가 상품 결제에 적용한다.
func (ds *Dataset) addAddonLines(q *Quote, c *Customer, main *Subscription) {
	if main.Plan == PlanFamilyAddon {
		return
	}
	for _, a := range c.Subscriptions {
		if a.Plan != PlanFamilyAddon {
			continue
		}
		ap, ok := c.currentPayment(a.ID, q.RequestDate)
		if !ok {
			continue
		}
		var eligible bool
		switch q.Reason {
		case ReasonWithdrawal:
			calc := withdrawalCalc(c, ap, q.RequestDate)
			eligible = calc.WithinPeriod && len(calc.UsageDates) == 0
		case ReasonIncident:
			calc := ds.incidentCalc(ap, q.RequestDate)
			eligible = calc.WithinPeriod && len(calc.QualifyingIncidentIDs) > 0
		}
		if !eligible {
			continue
		}
		if ap.Dispute {
			q.handoff("dispute", fmt.Sprintf("함께 환불할 부가 상품 결제 %s에 이의 제기가 진행 중이라 자동 응대로 처리하지 않는다(제19조 제1항).", ap.ID), "A-19")
			return
		}
		q.addLine(ap, ap.Amount)
		q.addArticles("A-18")
	}
}

// 연 구독 중도 해지(제8조, 제11조 제2항, 부칙 제1조 제2항).
func decideAnnual(q *Quote, sub *Subscription, p Payment) {
	if sub.Plan != PlanAnnual {
		q.reject("연 구독이 아니라 제8조 중도 해지 환불 대상이 아니다. 월 구독은 해지해도 이미 결제한 월 요금을 환불하지 않는다(제7조 제3항).", "A-08", "A-07")
		return
	}
	calc := annualCalc(sub, p, q.RequestDate)
	q.Breakdown.AnnualCancel = &calc
	arts := []string{"A-08"}
	if sub.Promo {
		arts = append(arts, "A-11")
	}
	arts = append(arts, "A-21", "T-01")
	if calc.Refund == 0 {
		q.ok(fmt.Sprintf("이용 %d개월 차감액 %s원이 결제 금액 %s원 이상이라 위약금 없이 환불하지 않는다(제8조 제3항). 해지하면 이용 기간은 해지 신청일에 끝난다(제8조 제4항).",
			calc.MonthsUsed, won(calc.Deduction), won(calc.PaymentAmount)), arts...)
	} else {
		penaltyNote := fmt.Sprintf("위약금 %s원(10%%)", won(calc.Penalty))
		if calc.AddendumApplied {
			penaltyNote = "2026-06-30 이전 결제라 위약금 없음(부칙 제1조 제2항)"
		}
		q.ok(fmt.Sprintf("결제 금액 %s원 − 이용 %d개월 × 정상 월 요금 %s원 = %s원, %s → 환불 예정액 %s원. 이용 기간은 해지 신청일에 끝난다(제8조).",
			won(calc.PaymentAmount), calc.MonthsUsed, won(calc.NormalMonthlyFee), won(calc.AfterDeduction), penaltyNote, won(calc.Refund)), arts...)
		q.addLine(p, calc.Refund)
	}
	q.endsOn(q.RequestDate)
}

func annualCalc(sub *Subscription, p Payment, on Date) AnnualCalc {
	// 구독 시작일은 해당 결제로 이용 기간이 시작된 날이다(제2조 제4항). 결제일을 쓴다.
	months := MonthsUsed(p.Date, on)
	calc := AnnualCalc{
		PaymentAmount:    p.Amount,
		StartDate:        p.Date,
		MonthsUsed:       months,
		NormalMonthlyFee: NormalMonthlyFee,
		Deduction:        int64(months) * NormalMonthlyFee,
		PenaltyRatePct:   penaltyRatePct,
		AddendumApplied:  !p.Date.After(addendumCutoff),
		Promo:            sub.Promo,
	}
	if calc.AddendumApplied {
		calc.PenaltyRatePct = 0
	}
	if rest := p.Amount - calc.Deduction; rest > 0 {
		calc.AfterDeduction = rest
		calc.Penalty = floor10(rest * int64(calc.PenaltyRatePct) / 100)
		calc.Refund = floor10(rest - calc.Penalty)
	}
	return calc
}

// MonthsUsed는 구독 시작일부터 해지 신청일까지 경과한 개월 수다. 다 채우지 못한 나머지 기간은 1개월로 보고,
// 신청일이 시작일과 같아도 1개월로 본다(제8조 제2항). 신청일이 n개월째 되는 같은 날이면 n개월이다.
func MonthsUsed(start, end Date) int {
	full := 0
	for full < 1200 && !start.AddMonths(full+1).After(end) {
		full++
	}
	if full == 0 || start.AddMonths(full).Before(end) {
		return full + 1
	}
	return full
}

// 중복 결제(제10조, 제16조 단서).
func (ds *Dataset) decideDuplicate(q *Quote, c *Customer, sub *Subscription) {
	calc := DuplicateCalc{Groups: []DuplicateGroup{}}
	var dupPayments []Payment
	// 같은 날 결제를 같은 이용 기간으로 묶는다. 묶음 안의 순서는 데이터 순서(승인 순서)를 따른다.
	var order []string
	byDate := map[string][]Payment{}
	for _, p := range c.Payments {
		if p.SubscriptionID != sub.ID || p.Date.After(q.RequestDate) {
			continue
		}
		key := p.Date.String()
		if _, seen := byDate[key]; !seen {
			order = append(order, key)
		}
		byDate[key] = append(byDate[key], p)
	}
	for _, key := range order {
		ps := byDate[key]
		if len(ps) < 2 {
			continue
		}
		d := ps[0].Date
		g := DuplicateGroup{Date: d, NormalPaymentID: ps[0].ID, DuplicatePaymentIDs: []string{}, Deadline: d.AddMonths(duplicateMonths)}
		g.WithinPeriod = !q.RequestDate.After(g.Deadline)
		for _, dp := range ps[1:] {
			g.DuplicatePaymentIDs = append(g.DuplicatePaymentIDs, dp.ID)
			if g.WithinPeriod {
				dupPayments = append(dupPayments, dp)
			}
		}
		calc.Groups = append(calc.Groups, g)
	}
	q.Breakdown.Duplicate = &calc
	arts := []string{"A-10"}
	if c.Status == StatusSuspended {
		arts = append(arts, "A-16")
	}
	if len(dupPayments) == 0 {
		msg := "같은 이용 기간에 두 번 이상 결제된 기록이 없다(제10조 제1항)."
		if len(calc.Groups) > 0 {
			msg = "중복 결제는 결제일로부터 1년 이내에 신청해야 하는데 그 기한이 지났다(제10조 제2항)."
		}
		q.reject(msg, arts...)
		return
	}
	for _, dp := range dupPayments {
		if dp.Dispute {
			q.handoff("dispute", fmt.Sprintf("중복 결제 %s에 이의 제기가 진행 중이라 자동 응대로 처리하지 않는다(제19조 제1항).", dp.ID), "A-19")
			return
		}
	}
	ids := make([]string, len(dupPayments))
	for i, dp := range dupPayments {
		ids[i] = dp.ID
	}
	q.ok(fmt.Sprintf("같은 이용 기간에 결제가 두 번 이상 이루어져 가장 먼저 승인된 결제를 정상 결제로 보고, 중복 결제(%s)를 이용 이력과 관계없이 전액 환불한다. 구독은 해지되지 않는다(제10조).", strings.Join(ids, ", ")), arts...)
	for _, dp := range dupPayments {
		q.addLine(dp, dp.Amount)
	}
}

// 무료 체험 해지(제12조 제1항).
func decideTrial(q *Quote, sub *Subscription) {
	if !sub.Trial {
		q.reject("무료 체험 구독이 아니다(제12조).", "A-12")
		return
	}
	calc := TrialCalc{TrialEnd: sub.TrialEnd, InTrial: q.RequestDate.Before(sub.TrialEnd)}
	q.Breakdown.TrialCancel = &calc
	if !calc.InTrial {
		q.reject(fmt.Sprintf("무료 체험 기간이 %s에 끝났다. 체험 후 첫 결제는 일반 결제와 같이 취급해 제5조를 적용한다(제12조 제2항).", sub.TrialEnd), "A-12", "A-05")
		return
	}
	last := sub.TrialEnd.AddDays(-1)
	q.ok(fmt.Sprintf("무료 체험 기간 중 해지하면 요금이 청구되지 않고, 체험 종료일 전날인 %s까지 이용할 수 있다(제12조 제1항).", last), "A-12")
	q.endsOn(last)
}

// 월 구독에서 연 구독으로 변경(제17조 제1항·제2항).
func decidePlanChange(q *Quote, sub *Subscription, p Payment) {
	if sub.Plan != PlanMonthly {
		q.reject("일할 차감은 월 구독에서 연 구독으로 바꾸는 경우에만 한다. 연 구독에서 월 구독으로 바꾸려면 제8조에 따라 해지한 뒤 새로 가입해야 한다(제17조).", "A-17")
		return
	}
	if !q.RequestDate.Before(sub.NextBilling) {
		q.reject(fmt.Sprintf("견적 기준일이 현재 결제 기간(다음 결제일 %s 전날까지)을 벗어났다(제17조 제1항).", sub.NextBilling), "A-17")
		return
	}
	calc := PlanChangeCalc{
		PaymentID:     p.ID,
		MonthlyPaid:   p.Amount,
		PeriodStart:   p.Date,
		NextBilling:   sub.NextBilling,
		PeriodDays:    DaysBetween(p.Date, sub.NextBilling),
		RemainingDays: max(0, DaysBetween(q.RequestDate, sub.NextBilling)-1),
		AnnualPrice:   AnnualNormalPrice,
	}
	if calc.PeriodDays > 0 {
		calc.ProratedCredit = floor10(p.Amount * int64(calc.RemainingDays) / int64(calc.PeriodDays))
	}
	calc.AnnualCharge = calc.AnnualPrice - calc.ProratedCredit
	q.Breakdown.PlanChange = &calc
	q.planChangeCredit = calc.ProratedCredit
	q.ok(fmt.Sprintf("남은 %d일(결제 기간 %d일 중)분 일할 금액 %s원을 연 구독 정상 요금 %s원에서 빼 %s원을 결제한다. 프로모션 요금은 신규 가입에만 적용한다(제17조).",
		calc.RemainingDays, calc.PeriodDays, won(calc.ProratedCredit), won(calc.AnnualPrice), won(calc.AnnualCharge)), "A-17", "T-01")
}

func (q *Quote) settle() {
	if q.Reason == ReasonPlanChange {
		q.RefundAmount = q.planChangeCredit
	} else {
		q.RefundAmount = 0
		for _, l := range q.Refunds {
			q.RefundAmount += l.Amount
		}
		if q.RefundAmount > ApprovalThreshold {
			q.ApprovalRequired = true
			q.addArticles("A-19")
		}
	}
	q.Eligible = q.RefundAmount > 0
}

func (q *Quote) ok(msg string, arts ...string) {
	q.Verdict = VerdictOK
	q.Message = msg
	q.addArticles(arts...)
}

func (q *Quote) reject(msg string, arts ...string) {
	q.Verdict = VerdictNotEligible
	q.Message = msg
	q.Refunds = []RefundLine{}
	q.LastUsableDate = nil
	q.addArticles(arts...)
}

func (q *Quote) handoff(reason, msg string, arts ...string) {
	q.Verdict = VerdictHandoff
	q.HandoffRequired = true
	q.HandoffReason = reason
	q.Message = msg
	q.Refunds = []RefundLine{}
	q.LastUsableDate = nil
	q.addArticles(arts...)
}

func (q *Quote) addArticles(ids ...string) {
	for _, id := range ids {
		if id != "" && !slices.Contains(q.ArticleIDs, id) {
			q.ArticleIDs = append(q.ArticleIDs, id)
		}
	}
}

func (q *Quote) addLine(p Payment, amount int64) {
	method, account := RefundRoute(p, q.RequestDate)
	q.Refunds = append(q.Refunds, RefundLine{
		PaymentID:       p.ID,
		SubscriptionID:  p.SubscriptionID,
		Amount:          amount,
		PaymentMethod:   p.Method,
		RefundMethod:    method,
		AccountRequired: account,
		ExpectedPeriod:  ExpectedPeriod(method),
	})
}

func (q *Quote) endsOn(d Date) { q.LastUsableDate = &d }

// RefundRoute는 환불 방법과 계좌 필요 여부다(제14조). receipt는 환불 접수일이다.
func RefundRoute(p Payment, receipt Date) (method string, accountRequired bool) {
	switch p.Method {
	case MethodCard:
		return RefundCardCancel, false
	case MethodMobile:
		if p.Date.SameMonth(receipt) {
			return RefundMobileCancel, false
		}
		return RefundBankTransfer, true
	case MethodEasyPay:
		return RefundEasyPayCancel, false
	}
	return RefundOriginal, false
}

// ExpectedPeriod는 환불 방법별 예상 소요 기간 안내 문구다(제14조). 약관에 기간이 없는 방법은 그 사실을 적는다.
func ExpectedPeriod(method string) string {
	switch method {
	case RefundCardCancel:
		return "환불 접수일로부터 3~7영업일 안에 카드 승인 취소"
	case RefundMobileCancel:
		return "결제한 달 안에 접수해 결제 취소(약관에 소요 기간 명시 없음)"
	case RefundBankTransfer:
		return "회원 본인 명의 계좌 정보를 받은 날부터 5영업일 안에 입금"
	case RefundEasyPayCancel:
		return "해당 결제 서비스의 취소 절차에 따름"
	}
	return "원래 결제한 수단으로 환불(약관에 소요 기간 명시 없음)"
}

// PeriodEnd는 이용 기간을 신청일에 끝내는 환불 접수다(청약철회, 장애 환불, 연 구독 중도 해지).
type PeriodEnd struct {
	Reason Reason
	Date   Date
}

// Cancellation은 해지 접수 결과다.
type Cancellation struct {
	LastUsableDate           Date
	CancelledSubscriptionIDs []string
	ArticleIDs               []string
}

// Cancel은 해지했을 때 이용 가능한 마지막 날과 함께 해지되는 구독을 정한다. 상태를 바꾸지 않는다.
//   - 이용 기간을 끝내는 환불 접수가 있으면 그 신청일(제5조 제3항, 제8조 제4항, 제9조 제2항)
//   - 무료 체험 중이면 체험 종료일 전날(제12조 제1항)
//   - 연 구독이면 해지 신청일, 즉 업무 기준일(제8조 제4항)
//   - 월 구독·부가 상품이면 다음 결제일 전날(제7조 제2항, 제18조 제2항)
//
// 본 구독을 해지하면 아직 해지되지 않은 부가 상품도 함께 해지한다(제18조 제3항).
func (ds *Dataset) Cancel(c *Customer, sub *Subscription, end *PeriodEnd, alreadyCancelled func(subID string) bool) Cancellation {
	today := ds.ReferenceDate
	res := Cancellation{CancelledSubscriptionIDs: []string{sub.ID}}
	switch {
	case end != nil && end.Reason.EndsPeriod():
		res.LastUsableDate = end.Date
		res.ArticleIDs = []string{primaryArticle(end.Reason)}
	case sub.Trial && today.Before(sub.TrialEnd):
		res.LastUsableDate = sub.TrialEnd.AddDays(-1)
		res.ArticleIDs = []string{"A-12"}
	case sub.Plan == PlanAnnual:
		res.LastUsableDate = today
		res.ArticleIDs = []string{"A-08"}
	case sub.Plan == PlanFamilyAddon:
		res.LastUsableDate = sub.NextBilling.AddDays(-1)
		res.ArticleIDs = []string{"A-18"}
	default:
		res.LastUsableDate = sub.NextBilling.AddDays(-1)
		res.ArticleIDs = []string{"A-07"}
	}
	if sub.Plan != PlanFamilyAddon {
		for _, a := range c.Subscriptions {
			if a.Plan == PlanFamilyAddon && !alreadyCancelled(a.ID) {
				res.CancelledSubscriptionIDs = append(res.CancelledSubscriptionIDs, a.ID)
				if !slices.Contains(res.ArticleIDs, "A-18") {
					res.ArticleIDs = append(res.ArticleIDs, "A-18")
				}
			}
		}
	}
	return res
}

func floor10(x int64) int64 {
	if x <= 0 {
		return 0
	}
	return x - x%10
}

func won(x int64) string {
	s := fmt.Sprintf("%d", x)
	neg := strings.HasPrefix(s, "-")
	s = strings.TrimPrefix(s, "-")
	var b strings.Builder
	for i, r := range s {
		if i > 0 && (len(s)-i)%3 == 0 {
			b.WriteByte(',')
		}
		b.WriteRune(r)
	}
	if neg {
		return "-" + b.String()
	}
	return b.String()
}

func joinDates(ds []Date) string {
	parts := make([]string, len(ds))
	for i, d := range ds {
		parts[i] = d.String()
	}
	return strings.Join(parts, ", ")
}
