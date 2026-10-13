package orga

// 보류용 평가 데이터(domain/org_a/customers_holdout.json)의 _case 메모를 확인하는 테스트다.
// 이 파일에는 보류용 시나리오의 기대 결과가 들어 있으므로, 엔진 개발자는 엔진 설정을 고정하기 전에 읽지 않는다
// (data/scenarios/HOLDOUT.md).

import (
	"encoding/json"
	"slices"
	"testing"
)

func loadMerged(t *testing.T) *Dataset {
	t.Helper()
	ds, err := LoadAll(dataPath, holdoutDataPath)
	if err != nil {
		t.Fatalf("고객 데이터 병합: %v", err)
	}
	return ds
}

// TestHoldoutCaseNotes는 보류용 고객 모두에 대해 _case 메모가 말하는 결과를 약관 규칙으로 확인한다.
func TestHoldoutCaseNotes(t *testing.T) {
	ds := loadMerged(t)
	type quoteCase struct {
		name     string
		customer string
		sub      string
		reason   Reason
		want     want
	}
	cases := []quoteCase{
		{"C201 체험 중 이용만 있음, 결제 3일차 → 청약철회", "C201", "S201", ReasonWithdrawal, want{
			verdict: VerdictOK, amount: 9900, lines: []string{"P201"}, articles: []string{"A-05", "A-12"}, lastUsable: "2026-10-12",
			check: func(t *testing.T, q *Quote) {
				w := q.Breakdown.Withdrawal
				if w.DayCount != 3 || w.TrialUsageExcluded != 3 || len(w.UsageDates) != 0 {
					t.Errorf("청약철회 계산 = %+v", w)
				}
			}}},
		{"C201 3.5시간 장애 → 제9조 제2항 아님", "C201", "S201", ReasonIncident, want{
			verdict: VerdictNotEligible, articles: []string{"A-09"},
			check: func(t *testing.T, q *Quote) {
				if len(q.Breakdown.Incident.QualifyingIncidentIDs) != 0 {
					t.Errorf("자격 장애 = %v", q.Breakdown.Incident.QualifyingIncidentIDs)
				}
			}}},
		{"C202 프로모션 2026-06-30 → 39,400원, 위약금 없음", "C202", "S202", ReasonAnnualCancel, want{
			verdict: VerdictOK, amount: 39400, lines: []string{"P202"}, articles: []string{"A-08", "A-11", "A-21"}, lastUsable: "2026-10-12",
			check: annualCheck(4, 39600, 0, true)}},
		{"C203 프로모션 2026-07-01 → 35,460원", "C203", "S203", ReasonAnnualCancel, want{
			verdict: VerdictOK, amount: 35460, lines: []string{"P203"}, articles: []string{"A-08", "A-11"}, lastUsable: "2026-10-12",
			check: annualCheck(4, 39600, 3940, false)}},
		{"C204 1년 지난 중복은 빼고 9월 중복만 → 9,900원", "C204", "S204", ReasonDuplicate, want{
			verdict: VerdictOK, amount: 9900, lines: []string{"P204n"}, articles: []string{"A-10"},
			check: func(t *testing.T, q *Quote) {
				g := q.Breakdown.Duplicate.Groups
				if len(g) != 2 || g[0].WithinPeriod || g[0].Deadline.String() != "2026-10-09" || !g[1].WithinPeriod || g[1].NormalPaymentID != "P204m" {
					t.Errorf("중복 묶음 = %+v", g)
				}
			}}},
		{"C204 10월 결제 뒤 이용 → 청약철회 불가", "C204", "S204", ReasonWithdrawal, want{
			verdict: VerdictNotEligible, articles: []string{"A-05", "A-06"}}},
		{"C204 10월 결제는 장애 환불 대상 아님", "C204", "S204", ReasonIncident, want{
			verdict: VerdictNotEligible, articles: []string{"A-09"}}},
		{"C205 이용 정지 계정 간편결제 중복 → 9,900원", "C205", "S205", ReasonDuplicate, want{
			verdict: VerdictOK, amount: 9900, lines: []string{"P205i"}, articles: []string{"A-10", "A-16"},
			check: func(t *testing.T, q *Quote) {
				if l := q.Refunds[0]; l.RefundMethod != RefundEasyPayCancel || l.AccountRequired {
					t.Errorf("환불 방법 = %+v", l)
				}
			}}},
		{"C205 이용 정지 계정 청약철회 → 제외", "C205", "S205", ReasonWithdrawal, want{
			verdict: VerdictNotEligible, articles: []string{"A-16", "A-05"}}},
		{"C206 결제 당시 만 18세 → 상담사 인계", "C206", "S206", ReasonMinor, want{
			verdict: VerdictHandoff, handoffReason: "minor", articles: []string{"A-13", "A-19"},
			check: func(t *testing.T, q *Quote) {
				if p := q.Breakdown.Payment; p.AgeAtPayment != 18 || !p.MinorAtPayment {
					t.Errorf("결제 당시 나이 = %+v", p)
				}
			}}},
		{"C206 연 구독 중도 해지로 보면 71,280원", "C206", "S206", ReasonAnnualCancel, want{
			verdict: VerdictOK, amount: 71280, approval: true, lines: []string{"P206"}, articles: []string{"A-08", "A-19"}, lastUsable: "2026-10-12",
			check: annualCheck(2, 19800, 7920, false)}},
		{"C207 본 구독 청약철회 → 가족 공유 포함 13,800원", "C207", "S207", ReasonWithdrawal, want{
			verdict: VerdictOK, amount: 13800, lines: []string{"P207", "P207F"}, articles: []string{"A-05", "A-18"}, lastUsable: "2026-10-12"}},
		{"C207 가족 공유만 → 환불 없음", "C207", "S207F", ReasonWithdrawal, want{
			verdict: VerdictNotEligible, articles: []string{"A-18"}}},
		{"C207 장애 환불 대상 아님", "C207", "S207", ReasonIncident, want{
			verdict: VerdictNotEligible, articles: []string{"A-09"}}},
		{"C208 휴대폰 결제 같은 달 청약철회 → 결제 취소", "C208", "S208", ReasonWithdrawal, want{
			verdict: VerdictOK, amount: 9900, lines: []string{"P208"}, articles: []string{"A-05"}, lastUsable: "2026-10-12",
			check: func(t *testing.T, q *Quote) {
				if l := q.Refunds[0]; l.RefundMethod != RefundMobileCancel || l.AccountRequired {
					t.Errorf("환불 방법 = %+v", l)
				}
			}}},
		{"C208 장애 환불 대상 아님", "C208", "S208", ReasonIncident, want{
			verdict: VerdictNotEligible, articles: []string{"A-09"}}},
		{"C209 프로모션 휴대폰 결제 1개월 → 62,190원, 계좌 환불", "C209", "S209", ReasonAnnualCancel, want{
			verdict: VerdictOK, amount: 62190, approval: true, lines: []string{"P209"}, articles: []string{"A-08", "A-11", "A-19"}, lastUsable: "2026-10-12",
			check: func(t *testing.T, q *Quote) {
				annualCheck(1, 9900, 6910, false)(t, q)
				if l := q.Refunds[0]; l.RefundMethod != RefundBankTransfer || !l.AccountRequired {
					t.Errorf("환불 방법 = %+v", l)
				}
			}}},
		{"C210 결제 당시 만 19세 → 미성년자 취소 아님", "C210", "S210", ReasonMinor, want{
			verdict: VerdictNotEligible, articles: []string{"A-13"},
			check: func(t *testing.T, q *Quote) {
				if p := q.Breakdown.Payment; p.AgeAtPayment != 19 || p.MinorAtPayment {
					t.Errorf("결제 당시 나이 = %+v", p)
				}
			}}},
		{"C210 청약철회 기한 경과", "C210", "S210", ReasonWithdrawal, want{
			verdict: VerdictNotEligible, articles: []string{"A-05"},
			check: func(t *testing.T, q *Quote) {
				if w := q.Breakdown.Withdrawal; w.WithinPeriod || w.Deadline.String() != "2026-10-10" {
					t.Errorf("청약철회 계산 = %+v", w)
				}
			}}},
		{"C211 2026-05-20 결제 5개월 → 49,500원, 승인 불필요", "C211", "S211", ReasonAnnualCancel, want{
			verdict: VerdictOK, amount: 49500, lines: []string{"P211"}, articles: []string{"A-08", "A-21"}, lastUsable: "2026-10-12",
			check: annualCheck(5, 49500, 0, true)}},
		{"C212 간편결제 4개월 → 53,460원, 승인 필요", "C212", "S212", ReasonAnnualCancel, want{
			verdict: VerdictOK, amount: 53460, approval: true, lines: []string{"P212"}, articles: []string{"A-08", "A-19"}, lastUsable: "2026-10-12",
			check: func(t *testing.T, q *Quote) {
				annualCheck(4, 39600, 5940, false)(t, q)
				if q.Refunds[0].RefundMethod != RefundEasyPayCancel {
					t.Errorf("환불 방법 = %s", q.Refunds[0].RefundMethod)
				}
			}}},
		{"C213 이의 제기 중 → 상담사 인계", "C213", "S213", ReasonWithdrawal, want{
			verdict: VerdictHandoff, handoffReason: "dispute", articles: []string{"A-19"}}},
		{"C214 결제 당일 이용 → 청약철회 불가", "C214", "S214", ReasonWithdrawal, want{
			verdict: VerdictNotEligible, articles: []string{"A-05", "A-06"}}},
		{"C215 결제 8일째 → 청약철회 기한 하루 경과", "C215", "S215", ReasonWithdrawal, want{
			verdict: VerdictNotEligible, articles: []string{"A-05"},
			check: func(t *testing.T, q *Quote) {
				if w := q.Breakdown.Withdrawal; w.DayCount != 8 || w.WithinPeriod || len(w.UsageDates) != 0 {
					t.Errorf("청약철회 계산 = %+v", w)
				}
			}}},
		{"C215 장애 환불 기한도 경과", "C215", "S215", ReasonIncident, want{
			verdict: VerdictNotEligible, articles: []string{"A-09"}}},
		{"C216 2개월 → 71,280원, 승인 필요", "C216", "S216", ReasonAnnualCancel, want{
			verdict: VerdictOK, amount: 71280, approval: true, lines: []string{"P216"}, articles: []string{"A-08", "A-19"}, lastUsable: "2026-10-12",
			check: annualCheck(2, 19800, 7920, false)}},
		{"C217 프로모션 연 구독 3일차 이용 없음 → 청약철회 79,000원", "C217", "S217", ReasonWithdrawal, want{
			verdict: VerdictOK, amount: 79000, approval: true, lines: []string{"P217"}, articles: []string{"A-05", "A-19"}, lastUsable: "2026-10-12"}},
		{"C217 중도 해지로 계산하면 62,190원", "C217", "S217", ReasonAnnualCancel, want{
			verdict: VerdictOK, amount: 62190, approval: true, lines: []string{"P217"}, articles: []string{"A-08"}, lastUsable: "2026-10-12",
			check: annualCheck(1, 9900, 6910, false)}},
		{"C218 결제 2일차 이용 없음 → 청약철회", "C218", "S218", ReasonWithdrawal, want{
			verdict: VerdictOK, amount: 9900, lines: []string{"P218"}, articles: []string{"A-05"}, lastUsable: "2026-10-12"}},
		{"C219 마지막 결제 청약철회 기한 경과", "C219", "S219", ReasonWithdrawal, want{
			verdict: VerdictNotEligible, articles: []string{"A-05"}}},
		{"C220 갱신 뒤 이용 → 청약철회 불가", "C220", "S220", ReasonWithdrawal, want{
			verdict: VerdictNotEligible, articles: []string{"A-05", "A-06"}}},
		{"C220 갱신 7일 이내 5시간 장애 → 99,000원", "C220", "S220", ReasonIncident, want{
			verdict: VerdictOK, amount: 99000, approval: true, lines: []string{"P220b"}, articles: []string{"A-09", "A-19"}, lastUsable: "2026-10-12",
			check: func(t *testing.T, q *Quote) {
				if !slices.Equal(q.Breakdown.Incident.QualifyingIncidentIDs, []string{"INC-2026-1008"}) {
					t.Errorf("자격 장애 = %v", q.Breakdown.Incident.QualifyingIncidentIDs)
				}
			}}},
		{"C220 중도 해지로 보면 80,190원", "C220", "S220", ReasonAnnualCancel, want{
			verdict: VerdictOK, amount: 80190, approval: true, lines: []string{"P220b"}, articles: []string{"A-08"}, lastUsable: "2026-10-12",
			check: annualCheck(1, 9900, 8910, false)}},
	}
	hold, err := Load(holdoutDataPath)
	if err != nil {
		t.Fatal(err)
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
	for _, c := range hold.Customers {
		if !covered[c.ID] {
			t.Errorf("보류용 고객 %s(_case: %s)의 견적 검사가 없다", c.ID, c.CaseNote)
		}
		if c.CaseNote == "" {
			t.Errorf("보류용 고객 %s에 _case 메모가 없다", c.ID)
		}
	}
}

// TestHoldoutCaseNotesCancellation은 _case 메모 중 해지 후 이용 마지막 날에 관한 내용을 확인한다.
func TestHoldoutCaseNotesCancellation(t *testing.T) {
	ds := loadMerged(t)
	none := func(string) bool { return false }
	for _, tc := range []struct {
		name      string
		customer  string
		sub       string
		end       *PeriodEnd
		last      string
		cancelled []string
	}{
		{"C201 청약철회 후 해지 → 신청일", "C201", "S201", &PeriodEnd{ReasonWithdrawal, refDay}, "2026-10-12", []string{"S201"}},
		{"C201 체험 종료 뒤 단순 해지 → 다음 결제일 전날", "C201", "S201", nil, "2026-11-08", []string{"S201"}},
		{"C204 단순 해지 → 2026-11-08", "C204", "S204", nil, "2026-11-08", []string{"S204"}},
		{"C207 청약철회 후 본 구독 해지 → 가족 공유도 신청일 종료", "C207", "S207", &PeriodEnd{ReasonWithdrawal, refDay}, "2026-10-12", []string{"S207", "S207F"}},
		{"C207 가족 공유만 해지 → 2026-11-08", "C207", "S207F", nil, "2026-11-08", []string{"S207F"}},
		{"C208 환불 없이 해지 → 2026-11-09", "C208", "S208", nil, "2026-11-09", []string{"S208"}},
		{"C210 해지하면 2026-11-02", "C210", "S210", nil, "2026-11-02", []string{"S210"}},
		{"C211 연 구독 해지 → 신청일", "C211", "S211", &PeriodEnd{ReasonAnnualCancel, refDay}, "2026-10-12", []string{"S211"}},
		{"C214 해지하면 2026-11-10", "C214", "S214", nil, "2026-11-10", []string{"S214"}},
		{"C215 해지하면 2026-11-03", "C215", "S215", nil, "2026-11-03", []string{"S215"}},
		{"C219 해지하면 2026-10-14", "C219", "S219", nil, "2026-10-14", []string{"S219"}},
		{"C220 장애 환불 후 해지 → 신청일", "C220", "S220", &PeriodEnd{ReasonIncident, refDay}, "2026-10-12", []string{"S220"}},
	} {
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
		})
	}
}

// TestMergeKeepsDevQuotes는 보류용 데이터(장애 기록 포함)를 합쳐 읽어도 개발용 고객의 견적이
// 하나도 바뀌지 않는지 본다. 장애 기록은 전역이라 새 장애가 개발용 고객의 판단을 바꿀 수 있기 때문이다.
func TestMergeKeepsDevQuotes(t *testing.T) {
	dev := loadData(t)
	merged := loadMerged(t)
	reasons := []Reason{ReasonWithdrawal, ReasonAnnualCancel, ReasonDuplicate, ReasonIncident, ReasonMinor, ReasonTrialCancel, ReasonPlanChange}
	n := 0
	for _, c := range dev.Customers {
		for _, s := range c.Subscriptions {
			for _, r := range reasons {
				req := QuoteRequest{CustomerID: c.ID, SubscriptionID: s.ID, Reason: r, RequestDate: refDay}
				a, errA := dev.Quote(req)
				b, errB := merged.Quote(req)
				if (errA == nil) != (errB == nil) {
					t.Fatalf("%s/%s/%s: 오류가 다르다: %v / %v", c.ID, s.ID, r, errA, errB)
				}
				ja, _ := json.Marshal(a)
				jb, _ := json.Marshal(b)
				if string(ja) != string(jb) || (a != nil && a.Verdict != b.Verdict) {
					t.Errorf("%s/%s/%s: 병합 후 견적이 바뀌었다\n%s\n%s", c.ID, s.ID, r, ja, jb)
				}
				n++
			}
		}
	}
	if n == 0 {
		t.Fatal("비교한 견적이 없다")
	}
}
