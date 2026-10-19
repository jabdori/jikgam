package server

// v2 미공개(보류용) 2차 묶음(v2b) 엔드투엔드 시나리오(data/scenarios/org_a_v2b_holdout.jsonl)의 기대 도구 호출을 재생하는 테스트다.
// 이 파일에는 v2b 미공개 시나리오의 기대 결과가 들어 있으므로, 엔진 개발자는 엔진 설정을 고정하기 전에 읽지 않는다
// (data/scenarios/HOLDOUT_V2B.md).

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http/httptest"
	"os"
	"slices"
	"strings"
	"testing"

	"github.com/jabdori/jikgam/bizapi/internal/orga"
)

const (
	v2bHoldoutDataPath      = "../../../domain/org_a/customers_v2b_holdout.json"
	v2bHoldoutScenariosPath = "../../../data/scenarios/org_a_v2b_holdout.jsonl"
)

type v2bHoldoutScenario struct {
	ID         string `json:"id"`
	CustomerID string `json:"customer_id"`
	Expected   struct {
		Action       string `json:"action"`
		RefundAmount *int64 `json:"refund_amount"`
		ToolCalls    []struct {
			Name string         `json:"name"`
			Args map[string]any `json:"args"`
		} `json:"tool_calls"`
	} `json:"expected"`
}

func readV2BHoldoutScenarios(t *testing.T) []v2bHoldoutScenario {
	t.Helper()
	f, err := os.Open(v2bHoldoutScenariosPath)
	if err != nil {
		t.Fatalf("시나리오 파일: %v", err)
	}
	defer f.Close()
	var out []v2bHoldoutScenario
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<22), 1<<22)
	for sc.Scan() {
		if strings.TrimSpace(sc.Text()) == "" {
			continue
		}
		var s v2bHoldoutScenario
		if err := json.Unmarshal(sc.Bytes(), &s); err != nil {
			t.Fatalf("시나리오 해석: %v", err)
		}
		out = append(out, s)
	}
	if err := sc.Err(); err != nil {
		t.Fatalf("시나리오 읽기: %v", err)
	}
	return out
}

// loadAllFour는 평가 때 띄우는 서버와 같이 개발용, v1 보류용, v2 보류용, v2b 보류용 고객 데이터를 이 순서로 합친다.
func loadAllFour(t *testing.T) *orga.Dataset {
	t.Helper()
	ds, err := orga.LoadAll(dataPath, holdoutDataPath, v2HoldoutDataPath, v2bHoldoutDataPath)
	if err != nil {
		t.Fatalf("고객 데이터 병합: %v", err)
	}
	return ds
}

// TestHoldoutV2BToolCalls는 v2b 미공개 시나리오의 기대 도구 호출을 네 데이터 파일을 합쳐 띄운 서버에 순서대로 재생해,
// 모든 호출이 기대대로 받아들여지거나 거절되고, 기대 환불액·승인 여부·환불 방법·이용 마지막 날·해지 구독·인계가 맞는지 확인한다.
// 시나리오 인자에 없는 otp, idempotency_key, quote_id는 테스트가 채운다.
func TestHoldoutV2BToolCalls(t *testing.T) {
	scenarios := readV2BHoldoutScenarios(t)
	if len(scenarios) != 24 {
		t.Fatalf("시나리오 %d개, want 24", len(scenarios))
	}
	seenCustomer := map[string]bool{}
	for i, s := range scenarios {
		if want := fmt.Sprintf("hw%02d", i+1); s.ID != want {
			t.Fatalf("%d번째 시나리오 ID %q, want %q", i, s.ID, want)
		}
		if want := fmt.Sprintf("C%d", 401+i); s.CustomerID != want {
			t.Fatalf("%s: 고객 %s, want %s", s.ID, s.CustomerID, want)
		}
		if seenCustomer[s.CustomerID] {
			t.Fatalf("%s: 고객 %s를 다른 시나리오도 쓴다", s.ID, s.CustomerID)
		}
		seenCustomer[s.CustomerID] = true
	}

	// 200이 아닌 결과를 기대하는 호출(시나리오, 도구) → 오류 코드.
	wantCode := map[[2]string]string{
		{"hw10", "quote_refund"}: codeNotEligible, // 청약철회 기간 경과(제5조)
		{"hw11", "quote_refund"}: codeNotEligible, // 결제 후 이용 이력(제6조)
		{"hw12", "quote_refund"}: codeNotEligible, // 이용 정지 계정(제16조)
		{"hw14", "quote_refund"}: codeNotEligible, // 결제 후 이용 이력(제6조)
		{"hw16", "quote_refund"}: codeNotEligible, // 부가 상품만 환불하지 않음(제18조 제2항)
		{"hw19", "quote_refund"}: codeNotEligible, // 장애 전액 환불 신청 기간 경과(제9조 제2항)
		{"hw22", "quote_refund"}: codeNotEligible, // 청약철회 기간 경과(제5조)
	}
	// 본인 확인에 일부러 실패하는 시나리오(인증번호를 잘못 읽는다).
	failVerify := map[string]bool{"hw05": true}
	wantLast := map[string]string{
		"hw01": "2026-10-12", "hw02": "2026-10-12", "hw04": "2026-10-12", "hw07": "2026-10-19", "hw08": "2026-10-12",
		"hw09": "2026-10-12", "hw15": "2026-10-19", "hw17": "2026-10-12", "hw20": "2026-10-12", "hw21": "2026-10-31",
		"hw23": "2026-10-12", "hw24": "2026-10-12",
	}
	wantCancelled := map[string][]string{"hw23": {"S423", "S423F"}}
	pending := map[string]bool{"hw02": true, "hw04": true, "hw08": true, "hw09": true, "hw20": true}
	wantMethod := map[string]string{
		"hw01": orga.RefundCardCancel, "hw02": orga.RefundCardCancel, "hw03": orga.RefundBankTransfer, "hw04": orga.RefundCardCancel,
		"hw08": orga.RefundCardCancel, "hw09": orga.RefundCardCancel, "hw17": orga.RefundCardCancel, "hw20": orga.RefundCardCancel,
		"hw23": orga.RefundCardCancel, "hw24": orga.RefundEasyPayCancel,
	}
	wantHandoff := map[string][2]string{"hw05": {"", "identity_failed"}, "hw13": {"C413", "dispute"}}
	// 요금제 변경 견적(환불이 아니라 일할 차감): 시나리오 → {일할 차감액, 연 구독 결제 금액}.
	wantPlanChange := map[string][2]int64{"hw18": {6060, 92940}}
	// 장애 기록 확인: 시나리오 → 있어야 하는 장애 ID. hw19는 조회 구간에 4시간 이상 장애가 없어야 한다.
	wantIncident := map[string]string{"hw04": "INC-2026-1008", "hw19": "INC-2026-1009", "hw23": "INC-2026-1008"}
	writeCall := map[string]bool{"create_cancellation": true, "create_refund_request": true, "register_refund_account": true}

	ts := httptest.NewServer(New(loadAllFour(t), slog.New(slog.NewJSONHandler(io.Discard, nil))))
	t.Cleanup(ts.Close)
	for _, s := range scenarios {
		t.Run(s.ID, func(t *testing.T) {
			expectOK(t, post(t, ts, "/_admin/reset", "", map[string]any{}), nil)
			sid := "v2b-" + s.ID
			var lastQuote *orga.Quote
			var refunded int64
			var last string
			var cancelled []string
			verified, handedOff, accountRegistered := false, false, false
			failures := 0
			refundedPayments := map[string]int{}
			for i, call := range s.Expected.ToolCalls {
				args := map[string]any{}
				for k, v := range call.Args {
					args[k] = v
				}
				if writeCall[call.Name] {
					if !verified {
						t.Fatalf("%d번째 호출 %s: 본인 확인 전 쓰기 호출", i, call.Name)
					}
					if s.Expected.Action != "answer" {
						t.Fatalf("%d번째 호출 %s: 최종 행동 %s인데 쓰기 호출이 있다", i, call.Name, s.Expected.Action)
					}
				}
				if id, ok := args["customer_id"]; ok && id != s.CustomerID {
					t.Fatalf("%d번째 호출 %s: customer_id %v, want %s", i, call.Name, id, s.CustomerID)
				}
				switch call.Name {
				case "verify_identity":
					otp := otpFor(args["phone"].(string))
					if failVerify[s.ID] {
						otp = wrongOTP(otp)
					}
					args["otp"] = otp
				case "quote_refund":
					if args["request_date"] != "2026-10-12" {
						t.Errorf("request_date = %v", args["request_date"])
					}
				case "create_refund_request":
					if lastQuote == nil {
						t.Fatalf("%d번째 호출: 견적 없이 환불 접수", i)
					}
					args["quote_id"] = lastQuote.QuoteID
					args["idempotency_key"] = fmt.Sprintf("%s-refund-%s", s.ID, args["payment_id"])
				case "create_cancellation", "register_refund_account":
					args["idempotency_key"] = fmt.Sprintf("%s-%s", s.ID, call.Name)
				}
				r := tool(t, ts, sid, call.Name, args)
				if code, ok := wantCode[[2]string{s.ID, call.Name}]; ok {
					e := expectError(t, r, statusFor(code), code)
					if e.Error.Quote == nil {
						t.Errorf("%s 거절에 견적 판단 내역이 없다", call.Name)
					}
					continue
				}
				expectOK(t, r, nil)
				switch call.Name {
				case "verify_identity":
					var v verifyResponse
					r.into(t, &v)
					if failVerify[s.ID] {
						failures++
						if v.Verified || v.CustomerID != "" || v.FailedAttempts != failures {
							t.Fatalf("본인 확인 실패 결과 = %+v", v)
						}
						if v.HandoffRequired != (failures >= 2) {
							t.Fatalf("%d회 실패 뒤 handoff_required = %v", failures, v.HandoffRequired)
						}
						continue
					}
					if !v.Verified || v.CustomerID != s.CustomerID {
						t.Fatalf("본인 확인 결과 = %+v", v)
					}
					verified = true
				case "quote_refund":
					lastQuote = &orga.Quote{}
					r.into(t, lastQuote)
				case "list_incidents":
					var body struct {
						Incidents []incidentView `json:"incidents"`
					}
					r.into(t, &body)
					want := wantIncident[s.ID]
					if want != "" && !slices.ContainsFunc(body.Incidents, func(v incidentView) bool {
						return v.IncidentID == want && v.Cause == orga.CauseCompany
					}) {
						t.Errorf("장애 기록에 %s(회사 귀책)가 없다: %+v", want, body.Incidents)
					}
					if s.ID == "hw19" {
						for _, v := range body.Incidents {
							if v.Hours >= 4 {
								t.Errorf("조회 구간에 4시간 이상 장애가 있다: %+v", v)
							}
						}
					}
				case "register_refund_account":
					var ar AccountReceipt
					r.into(t, &ar)
					if !ar.Registered {
						t.Errorf("계좌 등록 결과 = %+v", ar)
					}
					accountRegistered = true
				case "create_refund_request":
					var rr RefundReceipt
					r.into(t, &rr)
					refunded += rr.Amount
					refundedPayments[rr.PaymentID]++
					if (rr.Status == refundPendingApproval) != pending[s.ID] {
						t.Errorf("환불 상태 = %s", rr.Status)
					}
					if rr.RefundMethod != wantMethod[s.ID] {
						t.Errorf("환불 방법 = %s, want %s", rr.RefundMethod, wantMethod[s.ID])
					}
				case "create_cancellation":
					var cr CancellationReceipt
					r.into(t, &cr)
					last = cr.LastUsableDate.String()
					cancelled = cr.CancelledSubscriptionIDs
				case "create_handoff":
					var hr HandoffReceipt
					r.into(t, &hr)
					want, ok := wantHandoff[s.ID]
					if !ok || hr.HandoffID == "" || hr.CustomerID != want[0] || hr.Reason != want[1] {
						t.Errorf("인계 접수 = %+v, want %v", hr, want)
					}
					handedOff = true
				}
			}
			for p, n := range refundedPayments {
				if n != 1 {
					t.Errorf("결제 %s의 환불 접수 %d건", p, n)
				}
			}
			if lastQuote != nil {
				for _, l := range lastQuote.Refunds {
					if l.AccountRequired && !accountRegistered {
						t.Errorf("결제 %s가 계좌 환불 대상인데 계좌 등록 호출이 없다", l.PaymentID)
					}
				}
			}
			if want, ok := wantPlanChange[s.ID]; ok {
				pc := lastQuote.Breakdown.PlanChange
				if pc == nil || pc.ProratedCredit != want[0] || pc.AnnualCharge != want[1] || refunded != 0 {
					t.Errorf("요금제 변경 견적 = %+v, 접수 %d, want 차감 %d·결제 %d·접수 0", pc, refunded, want[0], want[1])
				}
			}
			switch want := s.Expected.RefundAmount; {
			case want != nil && s.Expected.Action == "answer":
				if lastQuote == nil || lastQuote.RefundAmount != *want || refunded != *want {
					t.Errorf("환불액: 견적 %v, 접수 %d, want %d", lastQuote, refunded, *want)
				}
			case want != nil:
				t.Errorf("최종 행동 %s인데 기대 환불액 %d가 있다", s.Expected.Action, *want)
			case refunded != 0:
				t.Errorf("환불액이 없어야 하는데 %d원 접수", refunded)
			}
			if last != wantLast[s.ID] {
				t.Errorf("이용 마지막 날 = %q, want %q", last, wantLast[s.ID])
			}
			if want, ok := wantCancelled[s.ID]; ok && !slices.Equal(cancelled, want) {
				t.Errorf("해지 구독 = %v, want %v", cancelled, want)
			}
			if handedOff != (s.Expected.Action == "handoff") {
				t.Errorf("인계 접수 %v, 최종 행동 %s", handedOff, s.Expected.Action)
			}
		})
	}
}

// wrongOTP는 맞는 인증번호와 다른 네 자리 번호다.
func wrongOTP(otp string) string {
	if otp == "0000" {
		return "1111"
	}
	return "0000"
}

// TestHoldoutV2BKeepsOtherQuotes는 v2b 보류용 고객 파일을 더해 읽어도 기존 고객(개발용, v1 보류용, v2 보류용)의 모든 견적
// (고객 × 구독 × 사유)이 바뀌지 않는지 확인한다. 장애 기록이 전역이어서다. v2b 파일에는 장애 기록을 두지 않는다.
func TestHoldoutV2BKeepsOtherQuotes(t *testing.T) {
	v2b, err := orga.Load(v2bHoldoutDataPath)
	if err != nil {
		t.Fatalf("v2b 보류용 고객 데이터: %v", err)
	}
	if len(v2b.Incidents) != 0 {
		t.Fatalf("v2b 보류용 파일에 장애 기록 %d건이 있다", len(v2b.Incidents))
	}
	if len(v2b.Customers) != 24 {
		t.Fatalf("v2b 보류용 고객 %d명, want 24", len(v2b.Customers))
	}
	for i, c := range v2b.Customers {
		if want := fmt.Sprintf("C%d", 401+i); c.ID != want {
			t.Fatalf("%d번째 고객 %s, want %s", i, c.ID, want)
		}
	}
	dev, err := orga.Load(dataPath)
	if err != nil {
		t.Fatal(err)
	}
	devV2B, err := orga.LoadAll(dataPath, v2bHoldoutDataPath)
	if err != nil {
		t.Fatal(err)
	}
	three, err := orga.LoadAll(dataPath, holdoutDataPath, v2HoldoutDataPath)
	if err != nil {
		t.Fatal(err)
	}
	all := loadAllFour(t)
	ref, err := orga.ParseDate("2026-10-12")
	if err != nil {
		t.Fatal(err)
	}
	reasons := []orga.Reason{orga.ReasonWithdrawal, orga.ReasonAnnualCancel, orga.ReasonDuplicate, orga.ReasonIncident,
		orga.ReasonMinor, orga.ReasonTrialCancel, orga.ReasonPlanChange}
	compare := func(name string, base, merged *orga.Dataset) int {
		n := 0
		for _, c := range base.Customers {
			for _, s := range c.Subscriptions {
				for _, r := range reasons {
					req := orga.QuoteRequest{CustomerID: c.ID, SubscriptionID: s.ID, Reason: r, RequestDate: ref}
					a, errA := base.Quote(req)
					b, errB := merged.Quote(req)
					if (errA == nil) != (errB == nil) {
						t.Fatalf("%s %s/%s/%s: 오류가 다르다: %v / %v", name, c.ID, s.ID, r, errA, errB)
					}
					ja, _ := json.Marshal(a)
					jb, _ := json.Marshal(b)
					if string(ja) != string(jb) || (a != nil && a.Verdict != b.Verdict) {
						t.Errorf("%s %s/%s/%s: 병합 후 견적이 바뀌었다\n%s\n%s", name, c.ID, s.ID, r, ja, jb)
					}
					n++
				}
			}
		}
		return n
	}
	if compare("개발용", dev, devV2B) == 0 || compare("개발용+v1 보류용+v2 보류용", three, all) == 0 {
		t.Fatal("비교한 견적이 없다")
	}
}
