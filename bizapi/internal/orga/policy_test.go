package orga

import (
	"errors"
	"slices"
	"testing"
	"time"
)

const dataPath = "../../../domain/org_a/customers.json"

// 시나리오와 고객 메모의 기준일.
var refDay = NewDate(2026, time.October, 12)

func loadData(t *testing.T) *Dataset {
	t.Helper()
	ds, err := Load(dataPath)
	if err != nil {
		t.Fatalf("고객 데이터 읽기: %v", err)
	}
	return ds
}

func d(s string) Date {
	v, err := ParseDate(s)
	if err != nil {
		panic(err)
	}
	return v
}

type want struct {
	verdict       Verdict
	amount        int64
	approval      bool
	handoffReason string
	lines         []string // 환불 대상 결제 ID
	articles      []string // 반드시 포함할 조항
	lastUsable    string   // "" 이면 없어야 한다
	check         func(t *testing.T, q *Quote)
}

func assertQuote(t *testing.T, q *Quote, w want) {
	t.Helper()
	if q.Verdict != w.verdict {
		t.Fatalf("verdict = %d, want %d (message: %s)", q.Verdict, w.verdict, q.Message)
	}
	if q.RefundAmount != w.amount {
		t.Errorf("refund_amount = %d, want %d", q.RefundAmount, w.amount)
	}
	if q.Eligible != (w.amount > 0 && w.verdict == VerdictOK) {
		t.Errorf("eligible = %v, amount %d, verdict %d", q.Eligible, w.amount, w.verdict)
	}
	if q.ApprovalRequired != w.approval {
		t.Errorf("approval_required = %v, want %v", q.ApprovalRequired, w.approval)
	}
	if q.HandoffRequired != (w.verdict == VerdictHandoff) || q.HandoffReason != w.handoffReason {
		t.Errorf("handoff = %v/%q, want reason %q", q.HandoffRequired, q.HandoffReason, w.handoffReason)
	}
	var got []string
	for _, l := range q.Refunds {
		got = append(got, l.PaymentID)
	}
	if !slices.Equal(got, w.lines) {
		t.Errorf("환불 대상 결제 = %v, want %v", got, w.lines)
	}
	for _, a := range w.articles {
		if !slices.Contains(q.ArticleIDs, a) {
			t.Errorf("article_ids %v에 %s가 없다", q.ArticleIDs, a)
		}
	}
	switch {
	case w.lastUsable == "" && q.LastUsableDate != nil:
		t.Errorf("last_usable_date = %s, want 없음", q.LastUsableDate)
	case w.lastUsable != "" && (q.LastUsableDate == nil || q.LastUsableDate.String() != w.lastUsable):
		t.Errorf("last_usable_date = %v, want %s", q.LastUsableDate, w.lastUsable)
	}
	if w.check != nil {
		w.check(t, q)
	}
}

// TestCaseNotes는 customers.json의 모든 고객에 대해 _case 메모가 말하는 결과를 약관 규칙으로 확인한다.
// 메모와 약관이 다르면 약관을 따른다.
func TestCaseNotes(t *testing.T) {
	ds := loadData(t)
	cases := []struct {
		name     string
		customer string
		sub      string
		reason   Reason
		want     want
	}{
		{"C001 월 구독 3일차 이용 없음 → 청약철회 전액", "C001", "S001", ReasonWithdrawal, want{
			verdict: VerdictOK, amount: 9900, lines: []string{"P001"}, articles: []string{"A-05"}, lastUsable: "2026-10-12",
			check: func(t *testing.T, q *Quote) {
				if w := q.Breakdown.Withdrawal; w.DayCount != 3 || !w.WithinPeriod || len(w.UsageDates) != 0 {
					t.Errorf("청약철회 계산 = %+v", w)
				}
				if q.Refunds[0].RefundMethod != RefundCardCancel {
					t.Errorf("환불 방법 = %s", q.Refunds[0].RefundMethod)
				}
			}}},
		{"C002 이용 있음 → 일반 청약철회 불가", "C002", "S002", ReasonWithdrawal, want{
			verdict: VerdictNotEligible, articles: []string{"A-05", "A-06"}}},
		{"C002 결제 당일 5시간 장애 → 제9조 제2항 전액", "C002", "S002", ReasonIncident, want{
			verdict: VerdictOK, amount: 9900, lines: []string{"P002"}, articles: []string{"A-09"}, lastUsable: "2026-10-12",
			check: func(t *testing.T, q *Quote) {
				if !slices.Equal(q.Breakdown.Incident.QualifyingIncidentIDs, []string{"INC-2026-1008"}) {
					t.Errorf("장애 = %v", q.Breakdown.Incident.QualifyingIncidentIDs)
				}
			}}},
		{"C003 연 구독 3개월 → 62,370원, 승인 필요", "C003", "S003", ReasonAnnualCancel, want{
			verdict: VerdictOK, amount: 62370, approval: true, lines: []string{"P003"},
			articles: []string{"A-08", "A-21", "A-19", "T-01"}, lastUsable: "2026-10-12",
			check: annualCheck(3, 29700, 6930, false)}},
		{"C004 프로모션 2개월 → 53,280원, 승인 필요", "C004", "S004", ReasonAnnualCancel, want{
			verdict: VerdictOK, amount: 53280, approval: true, lines: []string{"P004"},
			articles: []string{"A-08", "A-11", "A-19"}, lastUsable: "2026-10-12",
			check: annualCheck(2, 19800, 5920, false)}},
		{"C005 6/30 이전 결제 8개월 → 19,800원, 위약금 없음", "C005", "S005", ReasonAnnualCancel, want{
			verdict: VerdictOK, amount: 19800, lines: []string{"P005"}, articles: []string{"A-08", "A-21"}, lastUsable: "2026-10-12",
			check: annualCheck(8, 79200, 0, true)}},
		{"C006 중복 결제 → 9,900원, 구독 유지", "C006", "S006", ReasonDuplicate, want{
			verdict: VerdictOK, amount: 9900, lines: []string{"P006b"}, articles: []string{"A-10"},
			check: func(t *testing.T, q *Quote) {
				g := q.Breakdown.Duplicate.Groups
				if len(g) != 1 || g[0].NormalPaymentID != "P006a" {
					t.Errorf("중복 묶음 = %+v", g)
				}
			}}},
		{"C007 휴대폰 결제 당월 5일차 → 청약철회, 결제 취소", "C007", "S007", ReasonWithdrawal, want{
			verdict: VerdictOK, amount: 9900, lines: []string{"P007"}, articles: []string{"A-05"}, lastUsable: "2026-10-12",
			check: func(t *testing.T, q *Quote) {
				if l := q.Refunds[0]; l.RefundMethod != RefundMobileCancel || l.AccountRequired {
					t.Errorf("환불 방법 = %s, 계좌 필요 = %v", l.RefundMethod, l.AccountRequired)
				}
			}}},
		{"C008 휴대폰 중복 결제, 결제 달 지남 → 본인 계좌 환불", "C008", "S008", ReasonDuplicate, want{
			verdict: VerdictOK, amount: 9900, lines: []string{"P008b"}, articles: []string{"A-10"},
			check: func(t *testing.T, q *Quote) {
				if l := q.Refunds[0]; l.RefundMethod != RefundBankTransfer || !l.AccountRequired {
					t.Errorf("환불 방법 = %s, 계좌 필요 = %v", l.RefundMethod, l.AccountRequired)
				}
			}}},
		{"C009 만 17세 결제 → 상담사 인계", "C009", "S009", ReasonMinor, want{
			verdict: VerdictHandoff, handoffReason: "minor", articles: []string{"A-13", "A-19"},
			check: func(t *testing.T, q *Quote) {
				if p := q.Breakdown.Payment; p.AgeAtPayment != 17 || !p.MinorAtPayment {
					t.Errorf("결제 당시 나이 = %+v", p)
				}
			}}},
		{"C010 6일차 이용 있음, 7일 이내 장애 → 전액", "C010", "S010", ReasonIncident, want{
			verdict: VerdictOK, amount: 9900, lines: []string{"P010"}, articles: []string{"A-09"}, lastUsable: "2026-10-12"}},
		{"C011 이용 정지 계정 연 구독 → 환불 대상 아님", "C011", "S011", ReasonAnnualCancel, want{
			verdict: VerdictNotEligible, articles: []string{"A-16", "A-08"}}},
		{"C012 무료 체험 중 해지 → 과금 없음", "C012", "S012", ReasonTrialCancel, want{
			verdict: VerdictOK, amount: 0, articles: []string{"A-12"}, lastUsable: "2026-10-19",
			check: func(t *testing.T, q *Quote) {
				if tc := q.Breakdown.TrialCancel; !tc.InTrial || tc.Charge != 0 {
					t.Errorf("체험 계산 = %+v", tc)
				}
			}}},
		{"C013 체험 후 첫 결제 2일차, 체험 중 이용만 → 청약철회", "C013", "S013", ReasonWithdrawal, want{
			verdict: VerdictOK, amount: 9900, lines: []string{"P013"}, articles: []string{"A-05", "A-12"}, lastUsable: "2026-10-12",
			check: func(t *testing.T, q *Quote) {
				if q.Breakdown.Withdrawal.TrialUsageExcluded != 1 {
					t.Errorf("체험 중 이용 제외 수 = %d", q.Breakdown.Withdrawal.TrialUsageExcluded)
				}
			}}},
		{"C014 가족 공유 부가 상품만 → 환불 없음", "C014", "S014F", ReasonWithdrawal, want{
			verdict: VerdictNotEligible, articles: []string{"A-18"}}},
		{"C015 카드사 이의 제기 → 상담사 인계", "C015", "S015", ReasonWithdrawal, want{
			verdict: VerdictHandoff, handoffReason: "dispute", articles: []string{"A-19"}}},
		{"C016 월→연 전환 → 남은 20일 일할 차감 6,380원", "C016", "S016", ReasonPlanChange, want{
			verdict: VerdictOK, amount: 6380, articles: []string{"A-17"},
			check: func(t *testing.T, q *Quote) {
				pc := q.Breakdown.PlanChange
				if pc.PeriodDays != 31 || pc.RemainingDays != 20 || pc.ProratedCredit != 6380 || pc.AnnualCharge != 92620 {
					t.Errorf("요금제 변경 계산 = %+v", pc)
				}
			}}},
	}
	covered := map[string]bool{}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			q, err := ds.Quote(QuoteRequest{CustomerID: tc.customer, SubscriptionID: tc.sub, Reason: tc.reason, RequestDate: refDay})
			if err != nil {
				t.Fatalf("Quote: %v", err)
			}
			assertQuote(t, q, tc.want)
		})
		covered[tc.customer] = true
	}
	for _, c := range ds.Customers {
		if !covered[c.ID] {
			t.Errorf("고객 %s(_case: %s)의 견적 검사가 없다", c.ID, c.CaseNote)
		}
	}
}

func annualCheck(months int, deduction, penalty int64, addendum bool) func(*testing.T, *Quote) {
	return func(t *testing.T, q *Quote) {
		t.Helper()
		a := q.Breakdown.AnnualCancel
		if a.MonthsUsed != months || a.Deduction != deduction || a.Penalty != penalty || a.AddendumApplied != addendum {
			t.Errorf("연 구독 계산 = %+v, want 개월 %d 차감 %d 위약금 %d 부칙 %v", a, months, deduction, penalty, addendum)
		}
	}
}

// TestCaseNotesCancellation은 _case 메모 중 해지 후 이용 마지막 날에 관한 내용을 확인한다.
func TestCaseNotesCancellation(t *testing.T) {
	ds := loadData(t)
	none := func(string) bool { return false }
	cases := []struct {
		name      string
		customer  string
		sub       string
		end       *PeriodEnd
		last      string
		cancelled []string
		article   string
	}{
		{"C001 청약철회 접수 후 해지 → 신청일 종료", "C001", "S001", &PeriodEnd{ReasonWithdrawal, refDay}, "2026-10-12", []string{"S001"}, "A-05"},
		{"C001 환불 없이 단순 해지 → 다음 결제일 전날", "C001", "S001", nil, "2026-11-08", []string{"S001"}, "A-07"},
		{"C002 장애 환불 접수 후 해지 → 신청일 종료", "C002", "S002", &PeriodEnd{ReasonIncident, refDay}, "2026-10-12", []string{"S002"}, "A-09"},
		{"C002 장애를 고려하지 않은 단순 해지 → 2026-11-07(잘못된 안내의 근거)", "C002", "S002", nil, "2026-11-07", []string{"S002"}, "A-07"},
		{"C003 연 구독 중도 해지 → 신청일 종료", "C003", "S003", &PeriodEnd{ReasonAnnualCancel, refDay}, "2026-10-12", []string{"S003"}, "A-08"},
		{"C011 연 구독 환불 없이 해지 → 업무 기준일 종료", "C011", "S011", nil, "2026-10-12", []string{"S011"}, "A-08"},
		{"C012 체험 중 해지 → 체험 종료 전날", "C012", "S012", nil, "2026-10-19", []string{"S012"}, "A-12"},
		{"C014 부가 상품만 해지 → 2026-10-30 종료, 10-29까지 이용", "C014", "S014F", nil, "2026-10-29", []string{"S014F"}, "A-18"},
		{"C014 본 구독 해지 → 부가 상품도 함께 해지", "C014", "S014", nil, "2026-10-29", []string{"S014", "S014F"}, "A-18"},
		{"C016 월 구독 단순 해지 → 2026-11-01까지", "C016", "S016", nil, "2026-11-01", []string{"S016"}, "A-07"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			c, _ := ds.Customer(tc.customer)
			sub, _ := c.Subscription(tc.sub)
			res := ds.Cancel(c, sub, tc.end, none)
			if res.LastUsableDate.String() != tc.last {
				t.Errorf("last_usable_date = %s, want %s", res.LastUsableDate, tc.last)
			}
			if !slices.Equal(res.CancelledSubscriptionIDs, tc.cancelled) {
				t.Errorf("해지 구독 = %v, want %v", res.CancelledSubscriptionIDs, tc.cancelled)
			}
			if !slices.Contains(res.ArticleIDs, tc.article) {
				t.Errorf("article_ids %v에 %s가 없다", res.ArticleIDs, tc.article)
			}
		})
	}
}

// 합성 데이터용 도우미.
func synth(t *testing.T, incidents []Incident, customers ...*Customer) *Dataset {
	t.Helper()
	ds, err := NewDataset(refDay, incidents, customers)
	if err != nil {
		t.Fatalf("NewDataset: %v", err)
	}
	return ds
}

func annualCustomer(id string, price int64, promo bool, paid string) *Customer {
	return &Customer{
		ID: id, Name: "가상" + id, Phone: "010-9999-" + id[len(id)-4:], Birth: d("1990-01-01"), Status: StatusActive,
		Subscriptions: []Subscription{{ID: "S" + id, Plan: PlanAnnual, Price: price, Promo: promo, Start: d(paid), NextBilling: d(paid).AddMonths(12)}},
		Payments:      []Payment{{ID: "P" + id, SubscriptionID: "S" + id, Date: d(paid), Amount: price, Method: MethodCard}},
	}
}

// TestAnnualTableT02는 별표 2의 계산 예시 네 줄을 그대로 재현한다.
func TestAnnualTableT02(t *testing.T) {
	rows := []struct {
		name      string
		price     int64
		promo     bool
		paid      string
		request   string
		months    int
		deduction int64
		penalty   int64
		refund    int64
	}{
		{"정상 요금, 2026-07-01 이후 결제", 99000, false, "2026-07-01", "2026-09-15", 3, 29700, 6930, 62370},
		{"프로모션 요금, 2026-07-01 이후 결제", 79000, true, "2026-07-01", "2026-09-15", 3, 29700, 4930, 44370},
		{"정상 요금, 2026-06-30 이전 결제", 99000, false, "2026-06-01", "2026-08-15", 3, 29700, 0, 69300},
		{"정상 요금, 2026-07-01 이후 결제, 11개월", 99000, false, "2026-07-01", "2027-05-15", 11, 108900, 0, 0},
	}
	for i, r := range rows {
		t.Run(r.name, func(t *testing.T) {
			id := "T00" + string(rune('1'+i))
			ds := synth(t, nil, annualCustomer(id, r.price, r.promo, r.paid))
			q, err := ds.Quote(QuoteRequest{CustomerID: id, SubscriptionID: "S" + id, Reason: ReasonAnnualCancel, RequestDate: d(r.request)})
			if err != nil {
				t.Fatal(err)
			}
			a := q.Breakdown.AnnualCancel
			if a.MonthsUsed != r.months || a.Deduction != r.deduction || a.Penalty != r.penalty || a.Refund != r.refund || q.RefundAmount != r.refund {
				t.Errorf("계산 = %+v, 환불 %d; want 개월 %d 차감 %d 위약금 %d 환불 %d", a, q.RefundAmount, r.months, r.deduction, r.penalty, r.refund)
			}
			if q.Verdict != VerdictOK || q.Eligible != (r.refund > 0) {
				t.Errorf("verdict %d eligible %v", q.Verdict, q.Eligible)
			}
			if q.LastUsableDate == nil || q.LastUsableDate.String() != r.request {
				t.Errorf("이용 기간은 해지 신청일에 끝나야 한다: %v", q.LastUsableDate)
			}
		})
	}
}

func TestAddendumBoundary(t *testing.T) {
	for _, tc := range []struct {
		paid    string
		applied bool
	}{{"2026-06-30", true}, {"2026-07-01", false}} {
		ds := synth(t, nil, annualCustomer("B0001", 99000, false, tc.paid))
		q, err := ds.Quote(QuoteRequest{CustomerID: "B0001", SubscriptionID: "SB0001", Reason: ReasonAnnualCancel, RequestDate: d(tc.paid).AddDays(10)})
		if err != nil {
			t.Fatal(err)
		}
		if q.Breakdown.AnnualCancel.AddendumApplied != tc.applied {
			t.Errorf("%s 결제: 부칙 적용 = %v, want %v", tc.paid, q.Breakdown.AnnualCancel.AddendumApplied, tc.applied)
		}
	}
}

func TestMonthsUsed(t *testing.T) {
	for _, tc := range []struct {
		start, end string
		want       int
	}{
		{"2026-07-15", "2026-07-15", 1}, // 신청일이 시작일과 같아도 1개월
		{"2026-07-15", "2026-07-16", 1},
		{"2026-07-15", "2026-08-15", 1}, // 정확히 1개월 경과
		{"2026-07-15", "2026-08-16", 2}, // 나머지 하루도 1개월
		{"2026-07-15", "2026-10-12", 3},
		{"2026-01-31", "2026-02-28", 1}, // 말일 보정
		{"2026-01-31", "2026-03-01", 2},
		{"2026-01-31", "2026-03-31", 2},
	} {
		if got := MonthsUsed(d(tc.start), d(tc.end)); got != tc.want {
			t.Errorf("MonthsUsed(%s, %s) = %d, want %d", tc.start, tc.end, got, tc.want)
		}
	}
}

func monthlyCustomer(id, paid string, usage ...string) *Customer {
	c := &Customer{
		ID: id, Name: "가상" + id, Phone: "010-8888-" + id[len(id)-4:], Birth: d("1990-01-01"), Status: StatusActive,
		Subscriptions: []Subscription{{ID: "S" + id, Plan: PlanMonthly, Price: 9900, Start: d(paid), NextBilling: d(paid).AddMonths(1)}},
		Payments:      []Payment{{ID: "P" + id, SubscriptionID: "S" + id, Date: d(paid), Amount: 9900, Method: MethodCard}},
	}
	for _, u := range usage {
		c.Usage = append(c.Usage, UsageEvent{Date: d(u), Type: "play"})
	}
	return c
}

// TestWithdrawalSevenDays는 제2조 제7항의 예시(10월 5일 결제는 10월 12일까지)와 이용 이력 기준을 확인한다.
func TestWithdrawalSevenDays(t *testing.T) {
	ds := synth(t, nil, monthlyCustomer("W0001", "2026-10-05"), monthlyCustomer("W0002", "2026-10-05", "2026-10-05"))
	for _, tc := range []struct {
		customer, request string
		verdict           Verdict
	}{
		{"W0001", "2026-10-12", VerdictOK},          // 7일째
		{"W0001", "2026-10-13", VerdictNotEligible}, // 8일째
		{"W0002", "2026-10-06", VerdictNotEligible}, // 결제일 당일 이용도 이용 이력
	} {
		q, err := ds.Quote(QuoteRequest{CustomerID: tc.customer, SubscriptionID: "S" + tc.customer, Reason: ReasonWithdrawal, RequestDate: d(tc.request)})
		if err != nil {
			t.Fatal(err)
		}
		if q.Verdict != tc.verdict {
			t.Errorf("%s %s: verdict %d, want %d (%s)", tc.customer, tc.request, q.Verdict, tc.verdict, q.Message)
		}
	}
}

// TestIncidentThreshold는 연속 4시간 기준, 회사 귀책, 결제 전 장애 제외를 확인한다.
func TestIncidentThreshold(t *testing.T) {
	at := func(s string) time.Time {
		v, err := time.ParseInLocation("2006-01-02T15:04", s, time.UTC)
		if err != nil {
			panic(err)
		}
		return v
	}
	for _, tc := range []struct {
		name    string
		inc     Incident
		verdict Verdict
	}{
		{"정확히 4시간", Incident{ID: "I1", Start: at("2026-10-06T10:00"), End: at("2026-10-06T14:00"), Cause: CauseCompany}, VerdictOK},
		{"3시간 59분", Incident{ID: "I2", Start: at("2026-10-06T10:00"), End: at("2026-10-06T13:59"), Cause: CauseCompany}, VerdictNotEligible},
		{"회사 귀책 아님", Incident{ID: "I3", Start: at("2026-10-06T10:00"), End: at("2026-10-06T18:00"), Cause: "external"}, VerdictNotEligible},
		{"결제일 전 장애", Incident{ID: "I4", Start: at("2026-10-04T10:00"), End: at("2026-10-04T18:00"), Cause: CauseCompany}, VerdictNotEligible},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ds := synth(t, []Incident{tc.inc}, monthlyCustomer("N0001", "2026-10-05", "2026-10-06"))
			q, err := ds.Quote(QuoteRequest{CustomerID: "N0001", SubscriptionID: "SN0001", Reason: ReasonIncident, RequestDate: d("2026-10-08")})
			if err != nil {
				t.Fatal(err)
			}
			if q.Verdict != tc.verdict {
				t.Errorf("verdict %d, want %d (%s)", q.Verdict, tc.verdict, q.Message)
			}
		})
	}
}

func duplicateCustomer(id, paid string, amount int64, status string) *Customer {
	return &Customer{
		ID: id, Name: "가상" + id, Phone: "010-7777-" + id[len(id)-4:], Birth: d("1990-01-01"), Status: status,
		Subscriptions: []Subscription{{ID: "S" + id, Plan: PlanMonthly, Price: amount, Start: d(paid), NextBilling: d(paid).AddMonths(1)}},
		Payments: []Payment{
			{ID: "P" + id + "a", SubscriptionID: "S" + id, Date: d(paid), Amount: amount, Method: MethodCard},
			{ID: "P" + id + "b", SubscriptionID: "S" + id, Date: d(paid), Amount: amount, Method: MethodCard},
		},
	}
}

// TestDuplicateRules는 중복 결제의 1년 기한, 이용 정지 계정 예외, 승인 기준(50,000원 초과)을 확인한다.
func TestDuplicateRules(t *testing.T) {
	ds := synth(t, nil,
		duplicateCustomer("D0001", "2025-10-12", 9900, StatusActive),
		duplicateCustomer("D0002", "2026-10-01", 9900, StatusSuspended),
		duplicateCustomer("D0003", "2026-10-01", 50000, StatusActive),
		duplicateCustomer("D0004", "2026-10-01", 50010, StatusActive),
	)
	for _, tc := range []struct {
		name, customer, request string
		want                    want
	}{
		{"결제일로부터 1년째 되는 날", "D0001", "2026-10-12", want{verdict: VerdictOK, amount: 9900, lines: []string{"PD0001b"}}},
		{"1년 경과", "D0001", "2026-10-13", want{verdict: VerdictNotEligible, articles: []string{"A-10"}}},
		{"이용 정지 계정도 중복 결제 환불", "D0002", "2026-10-12", want{verdict: VerdictOK, amount: 9900, lines: []string{"PD0002b"}, articles: []string{"A-10", "A-16"}}},
		{"정확히 50,000원은 승인 불필요", "D0003", "2026-10-12", want{verdict: VerdictOK, amount: 50000, lines: []string{"PD0003b"}}},
		{"50,010원은 승인 필요", "D0004", "2026-10-12", want{verdict: VerdictOK, amount: 50010, approval: true, lines: []string{"PD0004b"}, articles: []string{"A-19"}}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			q, err := ds.Quote(QuoteRequest{CustomerID: tc.customer, SubscriptionID: "S" + tc.customer, Reason: ReasonDuplicate, RequestDate: d(tc.request)})
			if err != nil {
				t.Fatal(err)
			}
			assertQuote(t, q, tc.want)
		})
	}
}

// TestSuspendedExclusions는 이용 정지 계정에서 제외되는 환불(제5조, 제9조 제2항)과 유지되는 환불(제13조)을 확인한다.
func TestSuspendedExclusions(t *testing.T) {
	c := monthlyCustomer("X0001", "2026-10-10")
	c.Status = StatusSuspended
	minor := monthlyCustomer("X0002", "2026-10-10")
	minor.Status, minor.Birth = StatusSuspended, d("2010-01-01")
	ds := synth(t, nil, c, minor)
	q, _ := ds.Quote(QuoteRequest{CustomerID: "X0001", SubscriptionID: "SX0001", Reason: ReasonWithdrawal, RequestDate: refDay})
	assertQuote(t, q, want{verdict: VerdictNotEligible, articles: []string{"A-16", "A-05"}})
	q, _ = ds.Quote(QuoteRequest{CustomerID: "X0002", SubscriptionID: "SX0002", Reason: ReasonMinor, RequestDate: refDay})
	assertQuote(t, q, want{verdict: VerdictHandoff, handoffReason: "minor", articles: []string{"A-13"}})
}

// TestAddonFollowsMain은 본 구독을 청약철회하면 함께 해지되는 부가 상품도 같은 규정으로 환불하는지 본다(제18조 제3항).
func TestAddonFollowsMain(t *testing.T) {
	c := monthlyCustomer("F0001", "2026-10-10")
	c.Subscriptions = append(c.Subscriptions, Subscription{ID: "SF0001F", Plan: PlanFamilyAddon, Price: 3900, Start: d("2026-10-10"), NextBilling: d("2026-11-10")})
	c.Payments = append(c.Payments, Payment{ID: "PF0001F", SubscriptionID: "SF0001F", Date: d("2026-10-10"), Amount: 3900, Method: MethodCard})
	ds := synth(t, nil, c)
	q, err := ds.Quote(QuoteRequest{CustomerID: "F0001", SubscriptionID: "SF0001", Reason: ReasonWithdrawal, RequestDate: refDay})
	if err != nil {
		t.Fatal(err)
	}
	assertQuote(t, q, want{verdict: VerdictOK, amount: 13800, lines: []string{"PF0001", "PF0001F"}, articles: []string{"A-05", "A-18"}, lastUsable: "2026-10-12"})
}

func TestMinorNotMinor(t *testing.T) {
	ds := loadData(t)
	q, err := ds.Quote(QuoteRequest{CustomerID: "C001", SubscriptionID: "S001", Reason: ReasonMinor, RequestDate: refDay})
	if err != nil {
		t.Fatal(err)
	}
	assertQuote(t, q, want{verdict: VerdictNotEligible, articles: []string{"A-13"}})
}

func TestQuoteInputErrors(t *testing.T) {
	ds := loadData(t)
	for _, tc := range []struct {
		name string
		req  QuoteRequest
		err  error
	}{
		{"없는 고객", QuoteRequest{CustomerID: "C999", SubscriptionID: "S001", Reason: ReasonWithdrawal, RequestDate: refDay}, ErrNotFound},
		{"남의 구독", QuoteRequest{CustomerID: "C001", SubscriptionID: "S002", Reason: ReasonWithdrawal, RequestDate: refDay}, ErrNotFound},
		{"모르는 사유", QuoteRequest{CustomerID: "C001", SubscriptionID: "S001", Reason: "refund", RequestDate: refDay}, ErrInvalid},
		{"시작일 이전 기준일", QuoteRequest{CustomerID: "C001", SubscriptionID: "S001", Reason: ReasonWithdrawal, RequestDate: d("2026-10-01")}, ErrInvalid},
	} {
		if _, err := ds.Quote(tc.req); !errors.Is(err, tc.err) {
			t.Errorf("%s: err = %v, want %v", tc.name, err, tc.err)
		}
	}
}

func TestOTPAndHolder(t *testing.T) {
	ds := loadData(t)
	c, ok := ds.CustomerByPhone("01000000008")
	if !ok || c.ID != "C008" {
		t.Fatalf("번호 정규화 실패: %v", c)
	}
	if !c.CheckOTP("0008") || c.CheckOTP("8000") {
		t.Error("모의 인증번호는 휴대폰 번호 끝 4자리다")
	}
	if !c.HolderMatches("임 태윤") || c.HolderMatches("김아내") {
		t.Error("예금주는 회원 이름과 같아야 한다(공백 무시)")
	}
}
