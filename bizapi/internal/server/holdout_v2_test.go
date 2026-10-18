package server

// v2 미공개(보류용) 엔드투엔드 시나리오(data/scenarios/org_a_v2_holdout.jsonl)의 기대 도구 호출을 재생하는 테스트다.
// 이 파일에는 v2 미공개 시나리오의 기대 결과가 들어 있으므로, 엔진 개발자는 엔진 설정을 고정하기 전에 읽지 않는다
// (data/scenarios/HOLDOUT_V2.md).

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
	v2HoldoutDataPath      = "../../../domain/org_a/customers_v2_holdout.json"
	v2HoldoutScenariosPath = "../../../data/scenarios/org_a_v2_holdout.jsonl"
)

type v2HoldoutScenario struct {
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

func readV2HoldoutScenarios(t *testing.T) []v2HoldoutScenario {
	t.Helper()
	f, err := os.Open(v2HoldoutScenariosPath)
	if err != nil {
		t.Fatalf("시나리오 파일: %v", err)
	}
	defer f.Close()
	var out []v2HoldoutScenario
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<22), 1<<22)
	for sc.Scan() {
		if strings.TrimSpace(sc.Text()) == "" {
			continue
		}
		var s v2HoldoutScenario
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

// loadAllThree는 평가 때 띄우는 서버와 같이 개발용, v1 보류용, v2 보류용 고객 데이터를 합친다.
func loadAllThree(t *testing.T) *orga.Dataset {
	t.Helper()
	ds, err := orga.LoadAll(dataPath, holdoutDataPath, v2HoldoutDataPath)
	if err != nil {
		t.Fatalf("고객 데이터 병합: %v", err)
	}
	return ds
}

// TestHoldoutV2ToolCalls는 v2 미공개 시나리오의 기대 도구 호출을 세 데이터 파일을 합쳐 띄운 서버에 순서대로 재생해,
// 모든 호출이 받아들여지고 기대 환불액·승인 여부·환불 방법·이용 마지막 날·해지 구독이 맞는지 확인한다.
// 시나리오 인자에 없는 otp, idempotency_key, quote_id는 테스트가 채운다.
func TestHoldoutV2ToolCalls(t *testing.T) {
	scenarios := readV2HoldoutScenarios(t)
	if len(scenarios) != 12 {
		t.Fatalf("시나리오 %d개, want 12", len(scenarios))
	}
	seenCustomer := map[string]bool{}
	for i, s := range scenarios {
		if want := fmt.Sprintf("hv%02d", i+1); s.ID != want {
			t.Fatalf("%d번째 시나리오 ID %q, want %q", i, s.ID, want)
		}
		if seenCustomer[s.CustomerID] {
			t.Fatalf("%s: 고객 %s를 다른 시나리오도 쓴다", s.ID, s.CustomerID)
		}
		seenCustomer[s.CustomerID] = true
	}

	// 200이 아닌 결과를 기대하는 호출(시나리오, 도구) → 오류 코드.
	wantCode := map[[2]string]string{
		{"hv03", "quote_refund"}: codeNotEligible, // 결제 후 이용 이력(제6조)
		{"hv07", "quote_refund"}: codeNotEligible, // 청약철회 기간 경과(제5조)
	}
	wantLast := map[string]string{
		"hv01": "2026-10-12", "hv02": "2026-10-12", "hv04": "2026-10-12", "hv06": "2026-10-12",
		"hv11": "2026-10-16", "hv12": "2026-10-12",
	}
	wantCancelled := map[string][]string{"hv11": {"S311", "S311F"}}
	pending := map[string]bool{"hv06": true, "hv12": true}
	wantMethod := map[string]string{
		"hv01": orga.RefundCardCancel, "hv02": orga.RefundMobileCancel, "hv04": orga.RefundCardCancel,
		"hv06": orga.RefundCardCancel, "hv08": orga.RefundCardCancel, "hv12": orga.RefundCardCancel,
	}
	writeCall := map[string]bool{"create_cancellation": true, "create_refund_request": true, "register_refund_account": true}

	ts := httptest.NewServer(New(loadAllThree(t), slog.New(slog.NewJSONHandler(io.Discard, nil))))
	t.Cleanup(ts.Close)
	for _, s := range scenarios {
		t.Run(s.ID, func(t *testing.T) {
			expectOK(t, post(t, ts, "/_admin/reset", "", map[string]any{}), nil)
			sid := "v2-" + s.ID
			var lastQuote *orga.Quote
			var refunded int64
			var last string
			var cancelled []string
			verified, handedOff := false, false
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
					args["otp"] = otpFor(args["phone"].(string))
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
					expectError(t, r, statusFor(code), code)
					continue
				}
				expectOK(t, r, nil)
				switch call.Name {
				case "verify_identity":
					var v verifyResponse
					r.into(t, &v)
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
					if s.ID == "hv10" && !slices.ContainsFunc(body.Incidents, func(v incidentView) bool {
						return v.IncidentID == "INC-2026-1008" && v.Hours == 5 && v.Cause == orga.CauseCompany
					}) {
						t.Errorf("장애 기록에 INC-2026-1008(5시간, 회사 귀책)이 없다: %+v", body.Incidents)
					}
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
					if hr.HandoffID == "" || hr.CustomerID != s.CustomerID || hr.Reason != "minor" {
						t.Errorf("인계 접수 = %+v", hr)
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
					if l.AccountRequired {
						t.Errorf("결제 %s가 계좌 환불 대상이다. 시나리오는 계좌 등록을 기대하지 않는다", l.PaymentID)
					}
				}
			}
			switch want := s.Expected.RefundAmount; {
			case want != nil && s.Expected.Action == "answer":
				if lastQuote == nil || lastQuote.RefundAmount != *want || refunded != *want {
					t.Errorf("환불액: 견적 %v, 접수 %d, want %d", lastQuote, refunded, *want)
				}
			case want != nil:
				// 확답 보류·거절: 견적 금액은 안내하지만 접수하지 않는다.
				if lastQuote == nil || lastQuote.RefundAmount != *want || refunded != 0 {
					t.Errorf("견적 금액 %v, 접수 %d, want 견적 %d·접수 0", lastQuote, refunded, *want)
				}
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

// TestHoldoutV2KeepsOtherQuotes는 v2 보류용 고객 파일을 더해 읽어도 기존 고객(개발용, v1 보류용)의 모든 견적
// (고객 × 구독 × 사유)이 바뀌지 않는지 확인한다. 장애 기록이 전역이어서다. v2 파일에는 장애 기록을 두지 않는다.
func TestHoldoutV2KeepsOtherQuotes(t *testing.T) {
	v2, err := orga.Load(v2HoldoutDataPath)
	if err != nil {
		t.Fatalf("v2 보류용 고객 데이터: %v", err)
	}
	if len(v2.Incidents) != 0 {
		t.Fatalf("v2 보류용 파일에 장애 기록 %d건이 있다", len(v2.Incidents))
	}
	dev, err := orga.Load(dataPath)
	if err != nil {
		t.Fatal(err)
	}
	devV2, err := orga.LoadAll(dataPath, v2HoldoutDataPath)
	if err != nil {
		t.Fatal(err)
	}
	withV1, err := orga.LoadAll(dataPath, holdoutDataPath)
	if err != nil {
		t.Fatal(err)
	}
	all := loadAllThree(t)
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
	if compare("개발용", dev, devV2) == 0 || compare("개발용+v1 보류용", withV1, all) == 0 {
		t.Fatal("비교한 견적이 없다")
	}
}
