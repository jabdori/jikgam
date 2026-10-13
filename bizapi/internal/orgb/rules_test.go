package orgb

import (
	"slices"
	"testing"
)

const dataPath = "../../../domain/org_b/customers.json"

func load(t *testing.T) *Dataset {
	t.Helper()
	ds, err := Load(dataPath)
	if err != nil {
		t.Fatal(err)
	}
	return ds
}

func est(t *testing.T, ds *Dataset, member, pass string, typ RefundType) *Estimate {
	t.Helper()
	e, err := ds.Estimate(EstimateRequest{MemberNo: member, PassNo: pass, Type: typ, AsOf: ds.AsOf})
	if err != nil {
		t.Fatal(err)
	}
	return e
}

func TestDatasetLoads(t *testing.T) {
	ds := load(t)
	if ds.AsOf.String() != "2026-10-13" {
		t.Fatalf("as_of %s", ds.AsOf)
	}
	if len(ds.Members) < 8 || len(ds.Members) > 12 {
		t.Fatalf("회원 수 %d는 8~12명이어야 한다", len(ds.Members))
	}
	for _, m := range ds.Members {
		if m.CaseNote == "" {
			t.Errorf("%s: _case가 없다", m.No)
		}
	}
	if o := ds.Outages[0]; o.Minutes != 180 {
		t.Fatalf("OUT-1008 %d분", o.Minutes)
	}
}

func TestMockOTPIsLastSixDigits(t *testing.T) {
	ds := load(t)
	m, ok := ds.MemberByMobile("01050000101")
	if !ok || m.No != "M1001" || m.OTP() != "000101" || !m.CheckOTP(" 000101 ") || m.CheckOTP("0101") {
		t.Fatalf("인증번호 규칙: %+v", m)
	}
}

// 고객별 _case 기대값.

func TestM1001CoolingOffDay8(t *testing.T) {
	e := est(t, load(t), "M1001", "PS1001", TypeCoolingOff)
	if e.Outcome != OutcomeOK || !e.Refundable || e.TotalKRW != 11900 || e.NeedsReview {
		t.Fatalf("%+v", e)
	}
	if e.Calc.CoolingOff.DayCount != 8 || e.Calc.CoolingOff.Deadline.String() != "2026-10-19" {
		t.Fatalf("calc %+v", e.Calc.CoolingOff)
	}
	it := e.Items[0]
	if it.ChargeNo != "CH1001-2" || it.Route != RouteCardCancel || it.NeedsBankAccount {
		t.Fatalf("item %+v", it)
	}
	if e.ServiceEndsOn == nil || e.ServiceEndsOn.String() != "2026-10-13" || !slices.Equal(e.Clauses, []string{"B-05"}) {
		t.Fatalf("ends %v clauses %v", e.ServiceEndsOn, e.Clauses)
	}
}

func TestM1002ReadingBlocksCoolingOff(t *testing.T) {
	ds := load(t)
	e := est(t, ds, "M1002", "PS1002", TypeCoolingOff)
	if e.Outcome != OutcomeNotRefundable || !slices.Contains(e.Clauses, "B-06") || len(e.Items) != 0 {
		t.Fatalf("%+v", e)
	}
	m, _ := ds.Member("M1002")
	p, _ := m.Pass("PS1002")
	if c := ds.Cancel(p, nil); c.ServiceEndsOn.String() != "2026-10-31" || c.Clauses[0] != "B-07" {
		t.Fatalf("해지 %+v", c)
	}
	// 연 이용권이 아니면 일할 환불 대상이 아니다
	if e := est(t, ds, "M1002", "PS1002", TypeYearlyProrate); e.Outcome != OutcomeNotRefundable {
		t.Fatalf("%+v", e)
	}
}

func TestM1003YearlyProrate(t *testing.T) {
	e := est(t, load(t), "M1003", "PS1003", TypeYearlyProrate)
	c := e.Calc.Prorate
	if e.Outcome != OutcomeOK || c.PeriodDays != 365 || c.RemainingDays != 331 || c.RefundKRW != 107910 || c.OldPrice {
		t.Fatalf("%+v %+v", e, c)
	}
	if e.TotalKRW != 107910 || !e.NeedsReview || !slices.Equal(e.Clauses, []string{"B-08", "B-15"}) {
		t.Fatalf("%+v", e)
	}
	if e.ServiceEndsOn.String() != "2026-10-13" {
		t.Fatalf("ends %v", e.ServiceEndsOn)
	}
}

func TestM1004OldPriceProrate(t *testing.T) {
	e := est(t, load(t), "M1004", "PS1004", TypeYearlyProrate)
	c := e.Calc.Prorate
	if c.RemainingDays != 310 || c.RefundKRW != 84080 || !c.OldPrice || !slices.Contains(e.Clauses, "B-21") || !e.NeedsReview {
		t.Fatalf("%+v %+v", e, c)
	}
}

// 별표 2의 세 번째 예시(가상 날짜)도 같은 식으로 계산한다.
func TestProrateTableExample(t *testing.T) {
	start := NewDate(2027, 1, 5)
	end := start.AddMonths(12)
	asOf := NewDate(2027, 12, 20)
	days, rem := DaysBetween(start, end), DaysBetween(asOf, end)-1
	if days != 365 || rem != 15 || floor10(119000*int64(rem)/int64(days)) != 4890 {
		t.Fatalf("%d %d", days, rem)
	}
}

func TestM1005FamilyMemberReadingAndCancel(t *testing.T) {
	ds := load(t)
	e := est(t, ds, "M1005", "PS1005", TypeCoolingOff)
	if e.Outcome != OutcomeNotRefundable || !e.Calc.CoolingOff.ReadByMembers || !slices.Contains(e.Clauses, "B-02") {
		t.Fatalf("%+v", e)
	}
	m, _ := ds.Member("M1005")
	p, _ := m.Pass("PS1005")
	c := ds.Cancel(p, nil)
	if c.ServiceEndsOn.String() != "2026-11-03" || c.MembersEnded != 2 || !slices.Contains(c.Clauses, "B-12") {
		t.Fatalf("%+v", c)
	}
}

func TestM1006DoubleChargeOnDifferentDays(t *testing.T) {
	ds := load(t)
	e := est(t, ds, "M1006", "PS1006", TypeDoubleCharge)
	if e.Outcome != OutcomeOK || e.TotalKRW != 11900 || len(e.Items) != 1 || e.Items[0].ChargeNo != "CH1006-4" {
		t.Fatalf("%+v", e)
	}
	if e.ServiceEndsOn != nil || e.NeedsReview {
		t.Fatalf("중복 결제는 이용권을 유지한다: %+v", e)
	}
	g := e.Calc.DoubleCharge.Groups[0]
	if g.NormalCharge != "CH1006-3" || g.PeriodEndsOn.String() != "2026-11-01" {
		t.Fatalf("%+v", g)
	}
	// 정상 결제만 있는 회원은 중복이 아니다
	if e := est(t, ds, "M1001", "PS1001", TypeDoubleCharge); e.Outcome != OutcomeNotRefundable {
		t.Fatalf("%+v", e)
	}
}

func TestM1007PhoneDoubleChargeNeedsAccount(t *testing.T) {
	e := est(t, load(t), "M1007", "PS1007", TypeDoubleCharge)
	if e.Outcome != OutcomeOK || e.TotalKRW != 11900 {
		t.Fatalf("%+v", e)
	}
	it := e.Items[0]
	if it.ChargeNo != "CH1007-2" || it.Route != RouteBankTransfer || !it.NeedsBankAccount {
		t.Fatalf("%+v", it)
	}
}

func TestM1008ChargebackEscalates(t *testing.T) {
	ds := load(t)
	for _, typ := range []RefundType{TypeYearlyProrate, TypeCoolingOff} {
		e := est(t, ds, "M1008", "PS1008", typ)
		if e.Outcome != OutcomeNeedsAgent || e.Escalate == nil || e.Escalate.Code != EscalateChargeback {
			t.Fatalf("%s: %+v", typ, e)
		}
	}
}

func TestM1009RepeatRefundEscalates(t *testing.T) {
	e := est(t, load(t), "M1009", "PS1009", TypeCoolingOff)
	if e.Outcome != OutcomeNeedsAgent || e.Escalate.Code != EscalateRepeatRefund || *e.Calc.RecentRefunds != 2 {
		t.Fatalf("%+v", e)
	}
	if e.Calc.CoolingOff == nil || !e.Calc.CoolingOff.WithinPeriod {
		t.Fatalf("청약철회 요건 계산은 남는다: %+v", e.Calc)
	}
}

func TestM1010OutagesKeptAsRecords(t *testing.T) {
	ds := load(t)
	e := est(t, ds, "M1010", "PS1010", TypeCoolingOff)
	if e.Outcome != OutcomeNotRefundable {
		t.Fatalf("%+v", e)
	}
	want := map[string]int{"OUT-1008": 180, "OUT-1010": 150, "OUT-1011": 60}
	for _, o := range ds.Outages {
		if want[o.No] != o.Minutes {
			t.Fatalf("%s %d분", o.No, o.Minutes)
		}
	}
}

func TestM1011PointRestore(t *testing.T) {
	e := est(t, load(t), "M1011", "PS1011", TypeCoolingOff)
	if e.Outcome != OutcomeOK || e.Items[0].Route != RoutePointRestore || e.TotalKRW != 11900 {
		t.Fatalf("%+v", e)
	}
}

func TestCancelAfterServiceEndingRefund(t *testing.T) {
	ds := load(t)
	m, _ := ds.Member("M1001")
	p, _ := m.Pass("PS1001")
	c := ds.Cancel(p, &ServiceEnd{Type: TypeCoolingOff, On: ds.AsOf})
	if c.ServiceEndsOn.String() != "2026-10-13" || c.Clauses[0] != "B-05" {
		t.Fatalf("%+v", c)
	}
	m3, _ := ds.Member("M1003")
	p3, _ := m3.Pass("PS1003")
	if c := ds.Cancel(p3, nil); c.ServiceEndsOn.String() != "2026-10-13" || c.Clauses[0] != "B-08" {
		t.Fatalf("%+v", c)
	}
}

func TestEstimateInputErrors(t *testing.T) {
	ds := load(t)
	cases := []EstimateRequest{
		{MemberNo: "M9999", PassNo: "PS1001", Type: TypeCoolingOff, AsOf: ds.AsOf},
		{MemberNo: "M1001", PassNo: "PS1003", Type: TypeCoolingOff, AsOf: ds.AsOf},
		{MemberNo: "M1001", PassNo: "PS1001", Type: "INCIDENT", AsOf: ds.AsOf},
		{MemberNo: "M1001", PassNo: "PS1001", Type: TypeCoolingOff, AsOf: NewDate(2026, 1, 1)},
	}
	for i, req := range cases {
		if _, err := ds.Estimate(req); err == nil {
			t.Errorf("%d: 오류가 나야 한다", i)
		}
	}
}
