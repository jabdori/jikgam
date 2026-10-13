package server

// 보류용 평가 시나리오(data/scenarios/org_a_holdout.jsonl)의 기대 도구 호출을 재생하는 테스트다.
// 이 파일에는 보류용 시나리오의 기대 결과가 들어 있으므로, 엔진 개발자는 엔진 설정을 고정하기 전에 읽지 않는다
// (data/scenarios/HOLDOUT.md).

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
	holdoutDataPath      = "../../../domain/org_a/customers_holdout.json"
	holdoutScenariosPath = "../../../data/scenarios/org_a_holdout.jsonl"
)

type holdoutScenario struct {
	scenario
	MidCallEvents []struct {
		Inject *struct {
			Tool  string `json:"tool"`
			Code  string `json:"code"`
			Count int    `json:"count"`
		} `json:"inject"`
	} `json:"mid_call_events"`
}

func newMergedTestServer(t *testing.T) *httptest.Server {
	t.Helper()
	ds, err := orga.LoadAll(dataPath, holdoutDataPath)
	if err != nil {
		t.Fatalf("고객 데이터 병합: %v", err)
	}
	ts := httptest.NewServer(New(ds, slog.New(slog.NewJSONHandler(io.Discard, nil))))
	t.Cleanup(ts.Close)
	return ts
}

func readHoldoutScenarios(t *testing.T) []holdoutScenario {
	t.Helper()
	f, err := os.Open(holdoutScenariosPath)
	if err != nil {
		t.Fatalf("시나리오 파일: %v", err)
	}
	defer f.Close()
	var out []holdoutScenario
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	for sc.Scan() {
		if strings.TrimSpace(sc.Text()) == "" {
			continue
		}
		var s holdoutScenario
		if err := json.Unmarshal(sc.Bytes(), &s); err != nil {
			t.Fatalf("시나리오 해석: %v", err)
		}
		out = append(out, s)
	}
	return out
}

var writeTools = map[string]bool{"create_cancellation": true, "create_refund_request": true, "register_refund_account": true}

// TestHoldoutScenarioToolCalls는 보류용 시나리오의 기대 도구 호출을 순서대로 재생해, 두 데이터 파일을 합쳐 띄운 서버가
// 엔진이 할 호출을 모두 받아 주고 기대 환불액·승인 여부·이용 마지막 날을 돌려주는지 확인한다.
// mid_call_events의 inject는 시나리오 시작 전에 주입하고, 주입 횟수만큼 해당 도구 호출이 그 오류를 돌려줘야 한다.
// 시나리오 인자에 없는 otp, idempotency_key, quote_id는 테스트가 채운다. 재시도는 같은 멱등 키로 보낸다.
func TestHoldoutScenarioToolCalls(t *testing.T) {
	scenarios := readHoldoutScenarios(t)
	if len(scenarios) != 20 {
		t.Fatalf("시나리오 %d개, want 20", len(scenarios))
	}
	failVerify := map[string]bool{"h14": true}
	wantLast := map[string]string{
		"h01": "2026-10-12", "h02": "2026-10-12", "h03": "2026-10-12", "h07": "2026-10-12", "h08": "2026-10-12",
		"h09": "2026-10-12", "h11": "2026-10-12", "h12": "2026-10-12", "h15": "2026-11-03", "h16": "2026-10-12",
		"h17": "2026-10-12", "h20": "2026-10-12",
	}
	wantCancelled := map[string][]string{"h07": {"S207", "S207F"}}
	pending := map[string]bool{"h09": true, "h12": true, "h16": true, "h17": true, "h20": true}

	ts := newMergedTestServer(t)
	for _, s := range scenarios {
		t.Run(s.ID, func(t *testing.T) {
			expectOK(t, post(t, ts, "/_admin/reset", "", map[string]any{}), nil)
			remaining := map[string]int{}
			codes := map[string]string{}
			for _, ev := range s.MidCallEvents {
				if inj := ev.Inject; inj != nil {
					expectOK(t, post(t, ts, "/_admin/faults", "", map[string]any{"tool": inj.Tool, "code": inj.Code, "count": inj.Count}), nil)
					remaining[inj.Tool], codes[inj.Tool] = inj.Count, inj.Code
				}
			}
			var lastQuote *orga.Quote
			var refunded int64
			var last string
			var cancelled []string
			verified := false
			refundedPayments := map[string]int{}
			for i, call := range s.Expected.ToolCalls {
				args := map[string]any{}
				for k, v := range call.Args {
					args[k] = v
				}
				if writeTools[call.Name] && !verified {
					t.Fatalf("%d번째 호출 %s: 본인 확인 전 쓰기 호출", i, call.Name)
				}
				switch call.Name {
				case "verify_identity":
					args["otp"] = otpFor(args["phone"].(string))
					if failVerify[s.ID] {
						args["otp"] = "7310"
					}
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
				r := tool(t, ts, s.ID, call.Name, args)
				if remaining[call.Name] > 0 {
					remaining[call.Name]--
					expectError(t, r, statusFor(codes[call.Name]), codes[call.Name])
					continue
				}
				expectOK(t, r, nil)
				switch call.Name {
				case "verify_identity":
					var v verifyResponse
					r.into(t, &v)
					if v.Verified == failVerify[s.ID] {
						t.Errorf("본인 확인 결과 = %+v", v)
					}
					verified = verified || v.Verified
				case "quote_refund":
					lastQuote = &orga.Quote{}
					r.into(t, lastQuote)
				case "register_refund_account":
					var ar AccountReceipt
					r.into(t, &ar)
					if !ar.Registered {
						t.Errorf("계좌 등록 거절: %+v", ar)
					}
				case "create_refund_request":
					var rr RefundReceipt
					r.into(t, &rr)
					refunded += rr.Amount
					refundedPayments[rr.PaymentID]++
					if (rr.Status == refundPendingApproval) != pending[s.ID] {
						t.Errorf("환불 상태 = %s", rr.Status)
					}
				case "create_cancellation":
					var cr CancellationReceipt
					r.into(t, &cr)
					last = cr.LastUsableDate.String()
					cancelled = cr.CancelledSubscriptionIDs
				case "create_handoff":
					var hr HandoffReceipt
					r.into(t, &hr)
					if hr.HandoffID == "" {
						t.Errorf("인계 접수 = %+v", hr)
					}
				}
			}
			for p, n := range refundedPayments {
				if n != 1 {
					t.Errorf("결제 %s의 환불 접수 %d건", p, n)
				}
			}
			if s.Expected.RefundAmount != nil {
				if lastQuote == nil || lastQuote.RefundAmount != *s.Expected.RefundAmount || refunded != *s.Expected.RefundAmount {
					t.Errorf("환불액: 견적 %v, 접수 %d, want %d", lastQuote, refunded, *s.Expected.RefundAmount)
				}
			} else if refunded != 0 {
				t.Errorf("환불액이 없어야 하는데 %d원 접수", refunded)
			}
			if last != wantLast[s.ID] {
				t.Errorf("이용 마지막 날 = %q, want %q", last, wantLast[s.ID])
			}
			if want, ok := wantCancelled[s.ID]; ok && !slices.Equal(cancelled, want) {
				t.Errorf("해지 구독 = %v, want %v", cancelled, want)
			}
		})
	}
}
