// Package orgb는 가상 조직 B(책솔)의 회원 데이터와 환불·해지 약관 규칙을 담는다.
//
// 약관 원문은 domain/org_b/terms.md(2026.09, r1), 데이터는 domain/org_b/customers.json이다. 조직 B 업무 시스템의
// 자체 용어(회원 번호, 이용권, 결제 건, 환불 유형)를 쓰며, 상담 엔진의 공통 계약은 모른다. 공통 계약으로의 변환은
// internal/orgbgw(어댑터)가 한다. 이 패키지는 상태를 갖지 않는다.
package orgb

import (
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"
	"time"
)

// 이용권 상품 코드.
const (
	ProductMonthly = "MONTHLY"
	ProductYearly  = "YEARLY"
	ProductFamily  = "FAMILY"
)

// 결제 수단.
const (
	PayCard  = "CARD"
	PayPhone = "PHONE"
	PayPoint = "POINT"
)

// 장애 귀책.
const (
	FaultOurs   = "OURS"
	FaultVendor = "VENDOR"
)

const timeLayout = "2006-01-02T15:04"

type Outage struct {
	No      string
	Began   time.Time // KST 시각을 UTC 값으로 그대로 둔다
	Ended   time.Time
	Fault   string
	Minutes int
}

type Member struct {
	No            string
	Name          string
	Mobile        string
	Birth         Date
	State         string // NORMAL | SUSPENDED
	Passes        []Pass
	Charges       []Charge // 결제 시각 순
	Reading       []ReadingEvent
	RefundHistory []Date // 이 데이터 이전에 받은 환불(제15조 제1항 두 번째 사유)
	CaseNote      string
}

type Pass struct {
	No            string
	Product       string
	ListPrice     int64
	StartedOn     Date
	RenewsOn      Date
	FamilyMembers int
}

type Charge struct {
	No         string
	PassNo     string
	At         time.Time
	Amount     int64
	PayType    string
	Chargeback bool
}

// Date는 결제일(KST 달력 날짜)이다.
func (c Charge) Date() Date { return dateOf(c.At) }

type ReadingEvent struct {
	On   Date
	Kind string // EBOOK_OPEN | AUDIO_PLAY | DOWNLOAD
	By   string // OWNER | MEMBER(가족 이용권 구성원)
}

// Dataset은 조직 B의 회원·장애 데이터다. 만든 뒤에는 바꾸지 않는다.
type Dataset struct {
	AsOf    Date // 업무 기준일. 해지처럼 요청에 날짜가 없는 쓰기의 기준일이다.
	Outages []Outage
	Members []*Member

	byNo     map[string]*Member
	byMobile map[string]*Member
}

type rawFile struct {
	AsOf    string      `json:"as_of"`
	Outages []rawOutage `json:"outages"`
	Members []rawMember `json:"members"`
}

type rawOutage struct {
	No    string `json:"outage_no"`
	Began string `json:"began_at"`
	Ended string `json:"ended_at"`
	Fault string `json:"fault"`
}

type rawMember struct {
	No            string       `json:"member_no"`
	Name          string       `json:"member_name"`
	Mobile        string       `json:"mobile"`
	Birth         string       `json:"birth_date"`
	State         string       `json:"state"`
	Passes        []rawPass    `json:"passes"`
	Charges       []rawCharge  `json:"charges"`
	Reading       []rawReading `json:"reading_log"`
	RefundHistory []rawRefund  `json:"refund_history"`
	Case          string       `json:"_case"`
}

type rawPass struct {
	No            string `json:"pass_no"`
	Product       string `json:"product_code"`
	ListPrice     int64  `json:"list_price"`
	StartedOn     string `json:"started_on"`
	RenewsOn      string `json:"renews_on"`
	FamilyMembers int    `json:"family_members"`
}

type rawCharge struct {
	No         string `json:"charge_no"`
	PassNo     string `json:"pass_no"`
	At         string `json:"charged_at"`
	Amount     int64  `json:"amount_krw"`
	PayType    string `json:"pay_type"`
	Chargeback bool   `json:"chargeback"`
}

type rawReading struct {
	On   string `json:"on"`
	Kind string `json:"kind"`
	By   string `json:"by"`
}

type rawRefund struct {
	On  string `json:"refunded_on"`
	KRW int64  `json:"krw"`
}

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

// Parse는 데이터를 읽고 일관성을 검사한다.
func Parse(b []byte) (*Dataset, error) {
	var raw rawFile
	if err := json.Unmarshal(b, &raw); err != nil {
		return nil, err
	}
	asOf, err := ParseDate(raw.AsOf)
	if err != nil {
		return nil, fmt.Errorf("as_of: %w", err)
	}
	ds := &Dataset{AsOf: asOf, byNo: map[string]*Member{}, byMobile: map[string]*Member{}}
	seenOutage := map[string]bool{}
	for _, ro := range raw.Outages {
		began, err1 := time.ParseInLocation(timeLayout, ro.Began, time.UTC)
		ended, err2 := time.ParseInLocation(timeLayout, ro.Ended, time.UTC)
		if err1 != nil || err2 != nil || !ended.After(began) {
			return nil, fmt.Errorf("장애 %s: 시각 형식이 틀렸거나 종료가 시작보다 이르다", ro.No)
		}
		if ro.No == "" || seenOutage[ro.No] {
			return nil, fmt.Errorf("장애 번호가 비었거나 중복이다: %q", ro.No)
		}
		seenOutage[ro.No] = true
		ds.Outages = append(ds.Outages, Outage{No: ro.No, Began: began, Ended: ended, Fault: ro.Fault,
			Minutes: int(ended.Sub(began).Minutes())})
	}
	passSeen, chargeSeen := map[string]bool{}, map[string]bool{}
	for _, rm := range raw.Members {
		m, err := convertMember(rm, passSeen, chargeSeen)
		if err != nil {
			return nil, fmt.Errorf("회원 %s: %w", rm.No, err)
		}
		mobile := NormalizeMobile(m.Mobile)
		if m.No == "" || ds.byNo[m.No] != nil {
			return nil, fmt.Errorf("회원 번호가 비었거나 중복이다: %q", m.No)
		}
		if mobile == "" || ds.byMobile[mobile] != nil {
			return nil, fmt.Errorf("회원 %s: 휴대폰 번호가 비었거나 중복이다", m.No)
		}
		ds.byNo[m.No], ds.byMobile[mobile] = m, m
		ds.Members = append(ds.Members, m)
	}
	return ds, nil
}

func convertMember(rm rawMember, passSeen, chargeSeen map[string]bool) (*Member, error) {
	birth, err := ParseDate(rm.Birth)
	if err != nil {
		return nil, fmt.Errorf("birth_date: %w", err)
	}
	if rm.State != "NORMAL" && rm.State != "SUSPENDED" {
		return nil, fmt.Errorf("알 수 없는 회원 상태 %q", rm.State)
	}
	m := &Member{No: rm.No, Name: rm.Name, Mobile: rm.Mobile, Birth: birth, State: rm.State, CaseNote: rm.Case}
	own := map[string]bool{}
	for _, rp := range rm.Passes {
		switch rp.Product {
		case ProductMonthly, ProductYearly, ProductFamily:
		default:
			return nil, fmt.Errorf("이용권 %s: 알 수 없는 상품 %q", rp.No, rp.Product)
		}
		if rp.No == "" || passSeen[rp.No] {
			return nil, fmt.Errorf("이용권 번호가 비었거나 중복이다: %q", rp.No)
		}
		p := Pass{No: rp.No, Product: rp.Product, ListPrice: rp.ListPrice, FamilyMembers: rp.FamilyMembers}
		if p.StartedOn, err = ParseDate(rp.StartedOn); err != nil {
			return nil, fmt.Errorf("이용권 %s started_on: %w", rp.No, err)
		}
		if p.RenewsOn, err = ParseDate(rp.RenewsOn); err != nil {
			return nil, fmt.Errorf("이용권 %s renews_on: %w", rp.No, err)
		}
		passSeen[rp.No], own[rp.No] = true, true
		m.Passes = append(m.Passes, p)
	}
	for _, rc := range rm.Charges {
		at, err := time.ParseInLocation(timeLayout, rc.At, time.UTC)
		if err != nil {
			return nil, fmt.Errorf("결제 %s charged_at: %w", rc.No, err)
		}
		if rc.No == "" || chargeSeen[rc.No] {
			return nil, fmt.Errorf("결제 번호가 비었거나 중복이다: %q", rc.No)
		}
		if !own[rc.PassNo] {
			return nil, fmt.Errorf("결제 %s: 이 회원의 이용권이 아닌 %q를 가리킨다", rc.No, rc.PassNo)
		}
		if rc.Amount <= 0 {
			return nil, fmt.Errorf("결제 %s: 금액은 0보다 커야 한다", rc.No)
		}
		switch rc.PayType {
		case PayCard, PayPhone, PayPoint:
		default:
			return nil, fmt.Errorf("결제 %s: 알 수 없는 결제 수단 %q", rc.No, rc.PayType)
		}
		chargeSeen[rc.No] = true
		m.Charges = append(m.Charges, Charge{No: rc.No, PassNo: rc.PassNo, At: at, Amount: rc.Amount, PayType: rc.PayType,
			Chargeback: rc.Chargeback})
	}
	sort.SliceStable(m.Charges, func(i, j int) bool { return m.Charges[i].At.Before(m.Charges[j].At) })
	for _, rr := range rm.Reading {
		d, err := ParseDate(rr.On)
		if err != nil {
			return nil, fmt.Errorf("열람 이력: %w", err)
		}
		m.Reading = append(m.Reading, ReadingEvent{On: d, Kind: rr.Kind, By: rr.By})
	}
	for _, rr := range rm.RefundHistory {
		d, err := ParseDate(rr.On)
		if err != nil {
			return nil, fmt.Errorf("환불 이력: %w", err)
		}
		m.RefundHistory = append(m.RefundHistory, d)
	}
	return m, nil
}

func (ds *Dataset) Member(no string) (*Member, bool) {
	m, ok := ds.byNo[no]
	return m, ok
}

// MemberByMobile은 숫자만 비교한다.
func (ds *Dataset) MemberByMobile(mobile string) (*Member, bool) {
	m, ok := ds.byMobile[NormalizeMobile(mobile)]
	return m, ok
}

func (m *Member) Pass(no string) (*Pass, bool) {
	for i := range m.Passes {
		if m.Passes[i].No == no {
			return &m.Passes[i], true
		}
	}
	return nil, false
}

func NormalizeMobile(s string) string {
	var b strings.Builder
	for _, r := range s {
		if r >= '0' && r <= '9' {
			b.WriteRune(r)
		}
	}
	return b.String()
}

// OTPLength는 조직 B 인증번호 자릿수다(제14조 제2항).
const OTPLength = 6

// OTP는 모의 인증번호다. 실제 문자 발송이 없으므로 등록된 휴대폰 번호 숫자의 마지막 6자리를 인증번호로 본다.
// 예: 010-5000-0101 → "000101". 조직 A(마지막 4자리)와 다르다.
func (m *Member) OTP() string {
	d := NormalizeMobile(m.Mobile)
	if len(d) < OTPLength {
		return d
	}
	return d[len(d)-OTPLength:]
}

func (m *Member) CheckOTP(code string) bool {
	return subtle.ConstantTimeCompare([]byte(strings.TrimSpace(code)), []byte(m.OTP())) == 1
}

// OwnerMatches는 예금주가 회원 본인인지 본다. 공백은 무시한다.
func (m *Member) OwnerMatches(owner string) bool {
	return strings.Join(strings.Fields(owner), "") == strings.Join(strings.Fields(m.Name), "")
}
