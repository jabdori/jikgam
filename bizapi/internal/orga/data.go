// Package orga는 가상 조직 A(별담)의 고객 데이터와 환불·해지 약관 규칙을 담는다.
//
// 약관 원문은 domain/org_a/terms.md(약관 버전 2026.07, 문서 개정 r2)이고, 고객 데이터는
// domain/org_a/customers.json이다. 이 패키지는 상태를 갖지 않는다. 같은 입력이면 항상 같은 결과를 낸다.
package orga

import (
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"
)

// 계정 상태.
const (
	StatusActive    = "active"
	StatusSuspended = "suspended"
)

// 요금제.
const (
	PlanMonthly     = "monthly"
	PlanAnnual      = "annual"
	PlanFamilyAddon = "family_addon"
)

// 결제 수단.
const (
	MethodCard    = "card"
	MethodMobile  = "mobile"
	MethodEasyPay = "easy_pay"
)

// CauseCompany는 회사 귀책 장애다(제9조).
const CauseCompany = "company"

const incidentLayout = "2006-01-02T15:04"

type Incident struct {
	ID    string
	Start time.Time // KST 시각을 UTC 값으로 그대로 둔다
	End   time.Time
	Hours float64 // 데이터에 적힌 값. 판단에는 Start·End로 계산한 Duration을 쓴다.
	Cause string
}

func (i Incident) Duration() time.Duration { return i.End.Sub(i.Start) }

func (i Incident) StartDate() Date {
	return NewDate(i.Start.Year(), i.Start.Month(), i.Start.Day())
}

func (i Incident) EndDate() Date {
	return NewDate(i.End.Year(), i.End.Month(), i.End.Day())
}

type Customer struct {
	ID            string
	Name          string
	Phone         string
	Birth         Date
	Status        string
	Subscriptions []Subscription
	Payments      []Payment // 데이터 순서가 승인 순서다(제10조 제4항 판단에 쓴다)
	Usage         []UsageEvent
	CaseNote      string // customers.json의 _case. API로 내보내지 않는다.
}

type Subscription struct {
	ID          string
	Plan        string
	Price       int64
	Promo       bool
	Trial       bool
	TrialEnd    Date
	Start       Date
	NextBilling Date
}

type Payment struct {
	ID             string
	SubscriptionID string
	Date           Date
	Amount         int64
	Method         string
	Dispute        bool
}

type UsageEvent struct {
	Date        Date
	Type        string
	DuringTrial bool
}

// Dataset은 조직 A의 고객·장애 데이터다. 만든 뒤에는 바꾸지 않는다.
type Dataset struct {
	// ReferenceDate는 데이터 기준일이다. 해지 접수처럼 요청에 날짜가 없는 쓰기 작업의 업무 기준일로 쓴다.
	ReferenceDate Date
	Incidents     []Incident
	Customers     []*Customer

	byID    map[string]*Customer
	byPhone map[string]*Customer
}

// Load는 customers.json 파일을 읽는다.
func Load(path string) (*Dataset, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	ds, err := Parse(b)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return ds, nil
}

type rawFile struct {
	ReferenceDate string        `json:"reference_date"`
	Incidents     []rawIncident `json:"incidents"`
	Customers     []rawCustomer `json:"customers"`
}

type rawIncident struct {
	ID    string  `json:"id"`
	Start string  `json:"start"`
	End   string  `json:"end"`
	Hours float64 `json:"hours"`
	Cause string  `json:"cause"`
}

type rawCustomer struct {
	ID            string            `json:"id"`
	Name          string            `json:"name"`
	Phone         string            `json:"phone"`
	Birth         string            `json:"birth"`
	Status        string            `json:"status"`
	Subscriptions []rawSubscription `json:"subscriptions"`
	Payments      []rawPayment      `json:"payments"`
	Usage         []rawUsage        `json:"usage"`
	Case          string            `json:"_case"`
}

type rawSubscription struct {
	ID          string `json:"id"`
	Plan        string `json:"plan"`
	Price       int64  `json:"price"`
	Promo       bool   `json:"promo"`
	Trial       bool   `json:"trial"`
	TrialEnd    string `json:"trial_end"`
	Start       string `json:"start"`
	NextBilling string `json:"next_billing"`
}

type rawPayment struct {
	ID      string `json:"id"`
	Sub     string `json:"sub"`
	Date    string `json:"date"`
	Amount  int64  `json:"amount"`
	Method  string `json:"method"`
	Dispute bool   `json:"dispute"`
}

type rawUsage struct {
	Date        string `json:"date"`
	Type        string `json:"type"`
	DuringTrial bool   `json:"_during_trial"`
}

// Parse는 customers.json 내용을 읽고 검증한다.
func Parse(b []byte) (*Dataset, error) {
	var raw rawFile
	if err := json.Unmarshal(b, &raw); err != nil {
		return nil, err
	}
	ref, err := ParseDate(raw.ReferenceDate)
	if err != nil {
		return nil, fmt.Errorf("reference_date: %w", err)
	}
	var incidents []Incident
	for _, ri := range raw.Incidents {
		start, err := time.ParseInLocation(incidentLayout, ri.Start, time.UTC)
		if err != nil {
			return nil, fmt.Errorf("장애 %s start: %w", ri.ID, err)
		}
		end, err := time.ParseInLocation(incidentLayout, ri.End, time.UTC)
		if err != nil {
			return nil, fmt.Errorf("장애 %s end: %w", ri.ID, err)
		}
		incidents = append(incidents, Incident{ID: ri.ID, Start: start, End: end, Hours: ri.Hours, Cause: ri.Cause})
	}
	var customers []*Customer
	for _, rc := range raw.Customers {
		c, err := convertCustomer(rc)
		if err != nil {
			return nil, fmt.Errorf("고객 %s: %w", rc.ID, err)
		}
		customers = append(customers, c)
	}
	return NewDataset(ref, incidents, customers)
}

func convertCustomer(rc rawCustomer) (*Customer, error) {
	birth, err := ParseDate(rc.Birth)
	if err != nil {
		return nil, fmt.Errorf("birth: %w", err)
	}
	c := &Customer{ID: rc.ID, Name: rc.Name, Phone: rc.Phone, Birth: birth, Status: rc.Status, CaseNote: rc.Case}
	for _, rs := range rc.Subscriptions {
		s := Subscription{ID: rs.ID, Plan: rs.Plan, Price: rs.Price, Promo: rs.Promo, Trial: rs.Trial}
		if s.Start, err = ParseDate(rs.Start); err != nil {
			return nil, fmt.Errorf("구독 %s start: %w", rs.ID, err)
		}
		if s.NextBilling, err = ParseDate(rs.NextBilling); err != nil {
			return nil, fmt.Errorf("구독 %s next_billing: %w", rs.ID, err)
		}
		if rs.TrialEnd != "" {
			if s.TrialEnd, err = ParseDate(rs.TrialEnd); err != nil {
				return nil, fmt.Errorf("구독 %s trial_end: %w", rs.ID, err)
			}
		}
		c.Subscriptions = append(c.Subscriptions, s)
	}
	for _, rp := range rc.Payments {
		d, err := ParseDate(rp.Date)
		if err != nil {
			return nil, fmt.Errorf("결제 %s date: %w", rp.ID, err)
		}
		c.Payments = append(c.Payments, Payment{ID: rp.ID, SubscriptionID: rp.Sub, Date: d, Amount: rp.Amount, Method: rp.Method, Dispute: rp.Dispute})
	}
	for _, ru := range rc.Usage {
		d, err := ParseDate(ru.Date)
		if err != nil {
			return nil, fmt.Errorf("이용 이력 date: %w", err)
		}
		c.Usage = append(c.Usage, UsageEvent{Date: d, Type: ru.Type, DuringTrial: ru.DuringTrial})
	}
	return c, nil
}

// NewDataset은 색인을 만들고 데이터의 일관성을 검사한다.
func NewDataset(ref Date, incidents []Incident, customers []*Customer) (*Dataset, error) {
	ds := &Dataset{
		ReferenceDate: ref,
		Incidents:     incidents,
		Customers:     customers,
		byID:          map[string]*Customer{},
		byPhone:       map[string]*Customer{},
	}
	for _, inc := range incidents {
		if !inc.End.After(inc.Start) {
			return nil, fmt.Errorf("장애 %s: 종료 시각이 시작 시각보다 늦어야 한다", inc.ID)
		}
	}
	subIDs := map[string]bool{}
	payIDs := map[string]bool{}
	for _, c := range customers {
		if c.ID == "" || ds.byID[c.ID] != nil {
			return nil, fmt.Errorf("고객 ID가 비었거나 중복이다: %q", c.ID)
		}
		phone := NormalizePhone(c.Phone)
		if phone == "" || ds.byPhone[phone] != nil {
			return nil, fmt.Errorf("고객 %s: 휴대폰 번호가 비었거나 중복이다", c.ID)
		}
		if c.Status != StatusActive && c.Status != StatusSuspended {
			return nil, fmt.Errorf("고객 %s: 알 수 없는 계정 상태 %q", c.ID, c.Status)
		}
		own := map[string]bool{}
		for _, s := range c.Subscriptions {
			if subIDs[s.ID] {
				return nil, fmt.Errorf("구독 ID 중복: %s", s.ID)
			}
			switch s.Plan {
			case PlanMonthly, PlanAnnual, PlanFamilyAddon:
			default:
				return nil, fmt.Errorf("구독 %s: 알 수 없는 요금제 %q", s.ID, s.Plan)
			}
			if s.Trial && s.TrialEnd.IsZero() {
				return nil, fmt.Errorf("구독 %s: 체험 구독에는 trial_end가 필요하다", s.ID)
			}
			subIDs[s.ID], own[s.ID] = true, true
		}
		for _, p := range c.Payments {
			if payIDs[p.ID] {
				return nil, fmt.Errorf("결제 ID 중복: %s", p.ID)
			}
			if !own[p.SubscriptionID] {
				return nil, fmt.Errorf("결제 %s: 고객 %s의 구독이 아닌 %q를 가리킨다", p.ID, c.ID, p.SubscriptionID)
			}
			if p.Amount <= 0 {
				return nil, fmt.Errorf("결제 %s: 금액은 0보다 커야 한다", p.ID)
			}
			payIDs[p.ID] = true
		}
		ds.byID[c.ID] = c
		ds.byPhone[phone] = c
	}
	return ds, nil
}

func (ds *Dataset) Customer(id string) (*Customer, bool) {
	c, ok := ds.byID[id]
	return c, ok
}

// CustomerByPhone은 숫자만 비교한다. "010-0000-0001"과 "01000000001"은 같은 번호다.
func (ds *Dataset) CustomerByPhone(phone string) (*Customer, bool) {
	c, ok := ds.byPhone[NormalizePhone(phone)]
	return c, ok
}

func (c *Customer) Subscription(id string) (*Subscription, bool) {
	for i := range c.Subscriptions {
		if c.Subscriptions[i].ID == id {
			return &c.Subscriptions[i], true
		}
	}
	return nil, false
}

func (c *Customer) Payment(id string) (*Payment, bool) {
	for i := range c.Payments {
		if c.Payments[i].ID == id {
			return &c.Payments[i], true
		}
	}
	return nil, false
}

// currentPayment는 on 날짜까지의 결제 중 그 구독의 가장 최근 결제를 돌려준다.
// 같은 날 결제가 여러 건이면 먼저 승인된 결제(데이터 순서상 앞)를 정상 결제로 본다(제10조 제4항).
func (c *Customer) currentPayment(subID string, on Date) (Payment, bool) {
	var cur Payment
	found := false
	for _, p := range c.Payments {
		if p.SubscriptionID != subID || p.Date.After(on) {
			continue
		}
		if !found || p.Date.After(cur.Date) {
			cur, found = p, true
		}
	}
	return cur, found
}

// NormalizePhone은 번호에서 숫자만 남긴다.
func NormalizePhone(s string) string {
	var b strings.Builder
	for _, r := range s {
		if r >= '0' && r <= '9' {
			b.WriteRune(r)
		}
	}
	return b.String()
}

// OTP는 모의 본인 확인 번호다. 실제 문자 발송이 없으므로 등록된 휴대폰 번호 숫자의 마지막 4자리를
// 인증번호로 본다. 예: 010-0000-0001 → "0001".
func (c *Customer) OTP() string {
	digits := NormalizePhone(c.Phone)
	if len(digits) < 4 {
		return digits
	}
	return digits[len(digits)-4:]
}

// CheckOTP는 모의 인증번호가 맞는지 본다.
func (c *Customer) CheckOTP(otp string) bool {
	return subtle.ConstantTimeCompare([]byte(strings.TrimSpace(otp)), []byte(c.OTP())) == 1
}

// HolderMatches는 예금주가 회원 본인인지 본다(제14조 제3항: 회원 본인 명의 계좌). 공백은 무시한다.
func (c *Customer) HolderMatches(holder string) bool {
	return strings.Join(strings.Fields(holder), "") == strings.Join(strings.Fields(c.Name), "")
}
