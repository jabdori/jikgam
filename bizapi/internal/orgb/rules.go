package orgb

import (
	"errors"
	"fmt"
	"slices"
	"strings"
	"time"
)

// 약관 수치. 제2조 제8항, 제8조, 제10조, 제15조, 부칙 제1조.
const (
	CoolingOffDays    = 14     // 결제일 다음 날을 1일째로 세어 14일째까지(제2조 제8항, 제5조)
	ReviewThreshold   = 30_000 // 이 금액 이상이면 상담사 승인(제15조 제2항). 조직 A는 50,000원 "초과"다
	DoubleChargeMonth = 6      // 중복 결제 환불 신청 기한(제10조 제2항)
	RepeatRefundCount = 2      // 최근 12개월 환불 횟수가 이 값 이상이면 상담사(제15조 제1항)
)

// 요금 개정 전 결제(이 날 이전, 포함)는 부칙 제1조 제2항·제3항의 종전 요금 연 이용권이다.
var priceRevisionCutoff = NewDate(2026, time.August, 31)

// RefundType은 조직 B의 환불 유형이다.
type RefundType string

const (
	TypeCoolingOff    RefundType = "COOLING_OFF"    // 청약철회(제5조)
	TypeYearlyProrate RefundType = "YEARLY_PRORATE" // 연 이용권 중도 해지 일할 환불(제8조)
	TypeDoubleCharge  RefundType = "DOUBLE_CHARGE"  // 중복 결제(제10조)
)

func (t RefundType) Valid() bool {
	return t == TypeCoolingOff || t == TypeYearlyProrate || t == TypeDoubleCharge
}

// EndsService는 이 유형으로 환불하면 이용 기간이 신청일에 끝나는지다(제5조 제3항, 제8조 제4항).
func (t RefundType) EndsService() bool { return t == TypeCoolingOff || t == TypeYearlyProrate }

// 환불 경로(제13조).
const (
	RouteCardCancel   = "CARD_CANCEL"
	RoutePhoneCancel  = "PHONE_CANCEL"
	RouteBankTransfer = "BANK_TRANSFER"
	RoutePointRestore = "POINT_RESTORE"
)

// 상담사 연결 코드(제15조 제1항).
const (
	EscalateChargeback   = "CHARGEBACK"
	EscalateRepeatRefund = "REPEAT_REFUND"
)

var (
	ErrNotFound = errors.New("no such resource")
	ErrBadInput = errors.New("bad input")
)

// Outcome은 견적 판단 결과다. 서버는 이것을 200, NOT_REFUNDABLE, NEEDS_AGENT로 바꾼다.
type Outcome int

const (
	OutcomeOK Outcome = iota
	OutcomeNotRefundable
	OutcomeNeedsAgent
)

type EstimateRequest struct {
	MemberNo string
	PassNo   string
	Type     RefundType
	AsOf     Date
}

// Estimate는 환불 견적이다. 금액과 자격은 모두 여기서 정한다.
type Estimate struct {
	EstimateNo    string         `json:"estimate_no,omitempty"`
	MemberNo      string         `json:"member_no"`
	PassNo        string         `json:"pass_no"`
	Type          RefundType     `json:"refund_type"`
	AsOf          Date           `json:"as_of"`
	Refundable    bool           `json:"refundable"` // 환불할 금액이 있는지(TotalKRW > 0)
	TotalKRW      int64          `json:"total_krw"`
	Items         []EstimateItem `json:"items"`
	Calc          Calc           `json:"calc"`
	Clauses       []string       `json:"clauses"`
	NeedsReview   bool           `json:"needs_review"` // 30,000원 이상(제15조 제2항)
	Escalate      *Escalation    `json:"escalate"`
	ServiceEndsOn *Date          `json:"service_ends_on,omitempty"`
	Note          string         `json:"note"`
	Outcome       Outcome        `json:"-"`
}

type EstimateItem struct {
	ChargeNo         string `json:"charge_no"`
	PassNo           string `json:"pass_no"`
	KRW              int64  `json:"krw"`
	PayType          string `json:"pay_type"`
	Route            string `json:"route"`
	NeedsBankAccount bool   `json:"needs_bank_account"`
	ETAText          string `json:"eta_text"`
}

type Escalation struct {
	Code   string `json:"code"`
	Reason string `json:"reason"`
}

// Calc는 판단에 쓴 사실과 계산 과정이다. 유형에 해당하는 항목만 채운다.
type Calc struct {
	Charge        *ChargeFacts    `json:"charge,omitempty"`
	CoolingOff    *CoolingOffCalc `json:"cooling_off,omitempty"`
	Prorate       *ProrateCalc    `json:"prorate,omitempty"`
	DoubleCharge  *DoubleCalc     `json:"double_charge,omitempty"`
	RecentRefunds *int            `json:"recent_refunds_12m,omitempty"`
}

type ChargeFacts struct {
	ChargeNo     string `json:"charge_no"`
	ChargedOn    Date   `json:"charged_on"`
	PeriodEndsOn Date   `json:"period_ends_on"` // 이용 기간 마지막 날(다음 결제일 전날)
	KRW          int64  `json:"krw"`
	PayType      string `json:"pay_type"`
	Chargeback   bool   `json:"chargeback"`
}

type CoolingOffCalc struct {
	Deadline      Date     `json:"deadline"`
	DayCount      int      `json:"day_count"`
	WithinPeriod  bool     `json:"within_period"`
	ReadingDates  []Date   `json:"reading_dates"`
	ReadByMembers bool     `json:"read_by_family_members"`
	ReadingKinds  []string `json:"reading_kinds"`
}

type ProrateCalc struct {
	ChargeKRW     int64 `json:"charge_krw"`
	PeriodDays    int   `json:"period_days"`
	RemainingDays int   `json:"remaining_days"`
	RefundKRW     int64 `json:"refund_krw"` // 10원 미만 버림
	OldPrice      bool  `json:"old_price_b21"`
}

type DoubleCalc struct {
	Groups []DoubleGroup `json:"groups"`
}

type DoubleGroup struct {
	PeriodStart   Date     `json:"period_start"`
	PeriodEndsOn  Date     `json:"period_ends_on"`
	NormalCharge  string   `json:"normal_charge"`
	DoubleCharges []string `json:"double_charges"`
	Deadline      Date     `json:"deadline"`
	WithinPeriod  bool     `json:"within_period"`
}

// period는 정상 결제 하나가 여는 이용 기간과 그 안의 중복 결제다.
type period struct {
	normal  Charge
	end     Date // 다음 결제일(이 날은 포함하지 않는다)
	doubles []Charge
}

func cycleMonths(product string) int {
	if product == ProductYearly {
		return 12
	}
	return 1
}

// periods는 이용권의 결제를 이용 기간으로 묶는다. 앞 정상 결제의 이용 기간 안에 들어온 결제는 날짜가 달라도 중복이다(제10조 제1항).
func periods(m *Member, p *Pass) []period {
	var out []period
	for _, c := range m.Charges {
		if c.PassNo != p.No {
			continue
		}
		if n := len(out); n > 0 && c.Date().Before(out[n-1].end) {
			out[n-1].doubles = append(out[n-1].doubles, c)
			continue
		}
		out = append(out, period{normal: c, end: c.Date().AddMonths(cycleMonths(p.Product))})
	}
	return out
}

// currentPeriod는 asOf 날짜까지의 마지막 이용 기간이다.
func currentPeriod(m *Member, p *Pass, asOf Date) (period, bool) {
	var cur period
	found := false
	for _, per := range periods(m, p) {
		if per.normal.Date().After(asOf) {
			break
		}
		cur, found = per, true
	}
	return cur, found
}

// Estimate는 환불 견적을 낸다. 시계를 보지 않고 AsOf만 기준으로 판단한다.
func (ds *Dataset) Estimate(req EstimateRequest) (*Estimate, error) {
	m, ok := ds.Member(req.MemberNo)
	if !ok {
		return nil, fmt.Errorf("%w: 회원 %q가 없다", ErrNotFound, req.MemberNo)
	}
	p, ok := m.Pass(req.PassNo)
	if !ok {
		return nil, fmt.Errorf("%w: 회원 %s에게 이용권 %q가 없다", ErrNotFound, m.No, req.PassNo)
	}
	if !req.Type.Valid() {
		return nil, fmt.Errorf("%w: 알 수 없는 refund_type %q", ErrBadInput, req.Type)
	}
	if req.AsOf.IsZero() || req.AsOf.Before(p.StartedOn) {
		return nil, fmt.Errorf("%w: as_of %s가 비었거나 이용권 시작일 %s보다 앞선다", ErrBadInput, req.AsOf, p.StartedOn)
	}
	e := &Estimate{MemberNo: m.No, PassNo: p.No, Type: req.Type, AsOf: req.AsOf, Items: []EstimateItem{}, Clauses: []string{}}
	switch req.Type {
	case TypeCoolingOff:
		decideCoolingOff(e, m, p)
	case TypeYearlyProrate:
		decideProrate(e, m, p)
	case TypeDoubleCharge:
		decideDouble(e, m, p)
	}
	if e.Outcome == OutcomeOK {
		e.settle()
	}
	return e, nil
}

func chargeFacts(per period) *ChargeFacts {
	c := per.normal
	return &ChargeFacts{ChargeNo: c.No, ChargedOn: c.Date(), PeriodEndsOn: per.end.AddDays(-1), KRW: c.Amount,
		PayType: c.PayType, Chargeback: c.Chargeback}
}

// 청약철회(제5조, 제6조, 제2조 제7항·제8항).
func decideCoolingOff(e *Estimate, m *Member, p *Pass) {
	per, ok := currentPeriod(m, p, e.AsOf)
	if !ok {
		e.reject(fmt.Sprintf("견적 기준일 %s까지 이 이용권의 결제가 없다.", e.AsOf), "B-05")
		return
	}
	e.Calc.Charge = chargeFacts(per)
	if per.normal.Chargeback {
		e.escalate(EscalateChargeback, fmt.Sprintf("결제 %s는 카드사·결제대행사 이의 제기가 진행 중이라 상담사가 처리한다(제15조 제1항).", per.normal.No))
		return
	}
	paid := per.normal.Date()
	calc := &CoolingOffCalc{Deadline: paid.AddDays(CoolingOffDays), DayCount: DaysBetween(paid, e.AsOf),
		ReadingDates: []Date{}, ReadingKinds: []string{}}
	calc.WithinPeriod = !e.AsOf.Before(paid) && !e.AsOf.After(calc.Deadline)
	for _, r := range m.Reading {
		if r.On.Before(paid) || r.On.After(e.AsOf) {
			continue
		}
		calc.ReadingDates = append(calc.ReadingDates, r.On)
		calc.ReadingKinds = append(calc.ReadingKinds, r.Kind)
		if r.By == "MEMBER" {
			calc.ReadByMembers = true
		}
	}
	e.Calc.CoolingOff = calc
	switch {
	case !calc.WithinPeriod:
		e.reject(fmt.Sprintf("청약철회 기간(결제일 %s 다음 날부터 14일째인 %s까지)이 지났다(제5조 제1항, 제2조 제8항).", paid, calc.Deadline), "B-05", "B-02")
	case len(calc.ReadingDates) > 0:
		who := ""
		arts := []string{"B-05", "B-06"}
		if calc.ReadByMembers {
			who = " 가족 이용권 구성원의 열람도 열람 이력이다(제2조 제7항)."
			arts = append(arts, "B-02")
		}
		e.reject(fmt.Sprintf("결제일 이후 열람 이력(%s)이 있어 청약철회를 할 수 없다(제6조).%s", joinDates(calc.ReadingDates), who), arts...)
	default:
		e.ok(fmt.Sprintf("결제일 %s로부터 %d일째이고 결제 후 열람 이력이 없어 결제 금액 전액을 환불한다. 이용 기간은 신청일에 끝나고 이용권도 해지된다(제5조).", paid, calc.DayCount), "B-05")
		e.addItem(per.normal, per.normal.Amount)
		d := e.AsOf
		e.ServiceEndsOn = &d
		e.checkRepeat(m)
	}
}

// 연 이용권 중도 해지 일할 환불(제8조, 부칙 제1조).
func decideProrate(e *Estimate, m *Member, p *Pass) {
	if p.Product != ProductYearly {
		e.reject("연 이용권이 아니라 일할 환불 대상이 아니다. 월 이용권과 가족 이용권은 해지해도 이미 결제한 요금을 환불하지 않는다(제7조 제3항).", "B-08", "B-07")
		return
	}
	per, ok := currentPeriod(m, p, e.AsOf)
	if !ok {
		e.reject(fmt.Sprintf("견적 기준일 %s까지 이 이용권의 결제가 없다.", e.AsOf), "B-08")
		return
	}
	e.Calc.Charge = chargeFacts(per)
	if per.normal.Chargeback {
		e.escalate(EscalateChargeback, fmt.Sprintf("결제 %s는 카드사·결제대행사 이의 제기가 진행 중이라 상담사가 처리한다(제15조 제1항).", per.normal.No))
		return
	}
	c := per.normal
	calc := &ProrateCalc{ChargeKRW: c.Amount, PeriodDays: DaysBetween(c.Date(), per.end),
		RemainingDays: max(0, DaysBetween(e.AsOf, per.end)-1), OldPrice: !c.Date().After(priceRevisionCutoff)}
	if calc.PeriodDays > 0 {
		calc.RefundKRW = floor10(c.Amount * int64(calc.RemainingDays) / int64(calc.PeriodDays))
	}
	e.Calc.Prorate = calc
	arts := []string{"B-08"}
	if calc.OldPrice {
		arts = append(arts, "B-21")
	}
	e.ok(fmt.Sprintf("결제 금액 %s원 × 남은 %d일 ÷ 이용 기간 %d일 = 환불 예정액 %s원(10원 미만 버림, 위약금 없음). 이용 기간은 해지 신청일에 끝난다(제8조).",
		won(c.Amount), calc.RemainingDays, calc.PeriodDays, won(calc.RefundKRW)), arts...)
	if calc.RefundKRW > 0 {
		e.addItem(c, calc.RefundKRW)
	}
	d := e.AsOf
	e.ServiceEndsOn = &d
	if calc.RefundKRW > 0 {
		e.checkRepeat(m)
	}
}

// 중복 결제(제10조).
func decideDouble(e *Estimate, m *Member, p *Pass) {
	calc := &DoubleCalc{Groups: []DoubleGroup{}}
	var payable []Charge
	for _, per := range periods(m, p) {
		var ds []Charge
		for _, d := range per.doubles {
			if !d.Date().After(e.AsOf) {
				ds = append(ds, d)
			}
		}
		if len(ds) == 0 || per.normal.Date().After(e.AsOf) {
			continue
		}
		g := DoubleGroup{PeriodStart: per.normal.Date(), PeriodEndsOn: per.end.AddDays(-1), NormalCharge: per.normal.No,
			DoubleCharges: []string{}}
		g.Deadline = ds[0].Date().AddMonths(DoubleChargeMonth)
		g.WithinPeriod = !e.AsOf.After(g.Deadline)
		for _, d := range ds {
			g.DoubleCharges = append(g.DoubleCharges, d.No)
			if !e.AsOf.After(d.Date().AddMonths(DoubleChargeMonth)) {
				payable = append(payable, d)
			}
		}
		calc.Groups = append(calc.Groups, g)
	}
	e.Calc.DoubleCharge = calc
	if len(payable) == 0 {
		msg := "같은 이용 기간에 두 번 이상 결제된 기록이 없다(제10조 제1항)."
		if len(calc.Groups) > 0 {
			msg = "중복 결제 환불은 결제일로부터 6개월 이내에 신청해야 하는데 그 기한이 지났다(제10조 제2항)."
		}
		e.reject(msg, "B-10")
		return
	}
	for _, d := range payable {
		if d.Chargeback {
			e.escalate(EscalateChargeback, fmt.Sprintf("중복 결제 %s에 이의 제기가 진행 중이라 상담사가 처리한다(제15조 제1항).", d.No))
			return
		}
	}
	ids := make([]string, len(payable))
	for i, d := range payable {
		ids[i] = d.No
	}
	e.ok(fmt.Sprintf("같은 이용 기간에 결제가 두 번 이상 이루어져 먼저 결제된 건을 정상 결제로 보고 중복 결제(%s)를 열람 이력과 관계없이 전액 환불한다. 이용권은 해지되지 않는다(제10조).", strings.Join(ids, ", ")), "B-10")
	for _, d := range payable {
		e.addItem(d, d.Amount)
	}
}

// checkRepeat는 최근 12개월 환불이 2회 이상이면 상담사 연결로 바꾼다(제15조 제1항 두 번째 사유). 중복 결제에는 쓰지 않는다.
func (e *Estimate) checkRepeat(m *Member) {
	from := e.AsOf.AddMonths(-12)
	n := 0
	for _, d := range m.RefundHistory {
		if d.After(from) && !d.After(e.AsOf) {
			n++
		}
	}
	e.Calc.RecentRefunds = &n
	if n >= RepeatRefundCount {
		e.escalate(EscalateRepeatRefund, fmt.Sprintf("최근 12개월 안에 환불을 %d회 받은 회원의 환불 요청이라 상담사가 처리한다(제15조 제1항).", n))
	}
}

func (e *Estimate) settle() {
	e.TotalKRW = 0
	for _, it := range e.Items {
		e.TotalKRW += it.KRW
	}
	e.Refundable = e.TotalKRW > 0
	if e.TotalKRW >= ReviewThreshold {
		e.NeedsReview = true
		e.addClauses("B-15")
	}
}

func (e *Estimate) ok(note string, clauses ...string) {
	e.Outcome = OutcomeOK
	e.Note = note
	e.addClauses(clauses...)
}

func (e *Estimate) reject(note string, clauses ...string) {
	e.Outcome = OutcomeNotRefundable
	e.Note = note
	e.Items = []EstimateItem{}
	e.ServiceEndsOn = nil
	e.addClauses(clauses...)
}

func (e *Estimate) escalate(code, note string) {
	e.Outcome = OutcomeNeedsAgent
	e.Escalate = &Escalation{Code: code, Reason: note}
	e.Note = note
	e.Items = []EstimateItem{}
	e.ServiceEndsOn = nil
	e.addClauses("B-15")
}

func (e *Estimate) addClauses(ids ...string) {
	for _, id := range ids {
		if id != "" && !slices.Contains(e.Clauses, id) {
			e.Clauses = append(e.Clauses, id)
		}
	}
}

func (e *Estimate) addItem(c Charge, krw int64) {
	route, account, eta := Route(c, e.AsOf)
	e.Items = append(e.Items, EstimateItem{ChargeNo: c.No, PassNo: c.PassNo, KRW: krw, PayType: c.PayType, Route: route,
		NeedsBankAccount: account, ETAText: eta})
}

// Route는 환불 경로, 계좌 필요 여부, 예상 소요 기간 안내다(제13조). receipt는 환불 접수일이다.
func Route(c Charge, receipt Date) (route string, needsAccount bool, eta string) {
	switch c.PayType {
	case PayCard:
		return RouteCardCancel, false, "환불 접수일로부터 3~5영업일 안에 카드 승인 취소"
	case PayPhone:
		if c.Date().SameMonth(receipt) {
			return RoutePhoneCancel, false, "결제한 달 안에 접수해 휴대폰 결제 취소"
		}
		return RouteBankTransfer, true, "회원 본인 명의 계좌 정보를 받은 날부터 3영업일 안에 입금"
	case PayPoint:
		return RoutePointRestore, false, "환불 접수 즉시 책솔 포인트로 복원"
	}
	return RouteCardCancel, false, "원래 결제 수단으로 환불"
}

// ServiceEnd는 이용 기간을 신청일에 끝내는 환불 접수다(청약철회, 연 이용권 일할 환불).
type ServiceEnd struct {
	Type RefundType
	On   Date
}

type Cancellation struct {
	ServiceEndsOn Date
	MembersEnded  int
	Clauses       []string
}

// Cancel은 해지했을 때 이용이 끝나는 날을 정한다. 상태를 바꾸지 않는다.
//   - 이용 기간을 끝내는 환불 접수가 있으면 그 신청일(제5조 제3항, 제8조 제4항)
//   - 연 이용권이면 해지 신청일, 즉 업무 기준일(제8조 제4항)
//   - 월 이용권·가족 이용권이면 다음 결제일 전날(제7조 제2항). 가족 이용권은 구성원도 같은 날 끝난다(제12조 제2항)
func (ds *Dataset) Cancel(p *Pass, end *ServiceEnd) Cancellation {
	var res Cancellation
	switch {
	case end != nil && end.Type.EndsService():
		res.ServiceEndsOn = end.On
		if end.Type == TypeCoolingOff {
			res.Clauses = []string{"B-05"}
		} else {
			res.Clauses = []string{"B-08"}
		}
	case p.Product == ProductYearly:
		res.ServiceEndsOn = ds.AsOf
		res.Clauses = []string{"B-08"}
	default:
		res.ServiceEndsOn = p.RenewsOn.AddDays(-1)
		res.Clauses = []string{"B-07"}
	}
	if p.Product == ProductFamily {
		res.MembersEnded = p.FamilyMembers
		res.Clauses = append(res.Clauses, "B-12")
	}
	res.Clauses = append(res.Clauses, "B-16")
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
	var b strings.Builder
	for i, r := range s {
		if i > 0 && (len(s)-i)%3 == 0 {
			b.WriteByte(',')
		}
		b.WriteRune(r)
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
