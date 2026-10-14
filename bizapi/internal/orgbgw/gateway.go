// Package orgbgw는 조직 B 업무 API 어댑터다. 상담 엔진의 공통 계약(domain/org_a/api.md, bizapi/README.md의 도구 이름·필드·
// 오류 코드·X-Session-ID·멱등 키)을 받아 조직 B 업무 시스템(internal/orgbsrv, domain/org_b/api.md)의 계약으로 바꿔 부른다.
//
// 엔진은 조직 A 모의 서버와 이 어댑터를 구분하지 않는다. 조직 B 쪽 차이는 모두 여기서 흡수한다:
//   - 경로와 메서드: POST /v1/<도구> → 조직 B의 자원 경로(GET 조회, PUT 계좌 등록)
//   - 인증: 조직 B는 시스템 키(X-Api-Key)와 본인 확인 뒤 받은 회원 토큰(Bearer)을 요구한다. 어댑터가 X-Session-ID마다 토큰을
//     들고 있다가 쓰기 요청에 붙인다. 본인 확인 실패 횟수는 X-Contact-Ref(=X-Session-ID)로 조직 B가 센다.
//   - 멱등 키: 본문 idempotency_key → Idempotency-Key 헤더
//   - 필드 이름과 값: member_no→customer_id, pass_no→subscription_id, charge_no→payment_id, product_code→plan,
//     charged_at(+09:00 시각)→date, minutes→hours, fault→cause, estimate→quote, 환불 유형↔공통 reason 등
//   - 응답 봉투 {"status","data"}와 오류 코드 → 공통 오류 {"error": {"code", "message", ...}} (translateCode)
//   - 장애 주입(/_admin/faults)은 조직 B의 /_mock/faults로 넘겨, 조직 B 오류(UPSTREAM_TIMEOUT, MAINTENANCE)가 어댑터의 변환을
//     거쳐 TIMEOUT·UNAVAILABLE이 되게 한다.
//
// 어댑터는 금액과 자격을 계산하지 않는다(ADR 0004). 조직 B 약관에 없는 공통 사유(incident, minor, trial_cancel,
// plan_change)만 업무 시스템을 부르지 않고 NOT_ELIGIBLE로 답한다(unsupportedReasons).
package orgbgw

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

const (
	sessionHeader   = "X-Session-ID"
	maxSessionIDLen = 128
	maxBodyBytes    = 1 << 20
)

// 공통 오류 코드(domain/org_a/api.md, bizapi/README.md).
const (
	codeIdentityRequired = "IDENTITY_REQUIRED"
	codeNotEligible      = "NOT_ELIGIBLE"
	codeHandoffRequired  = "HANDOFF_REQUIRED"
	codeTimeout          = "TIMEOUT"
	codeUnavailable      = "UNAVAILABLE"
	codeDuplicateRequest = "DUPLICATE_REQUEST"
	codeInvalidRequest   = "INVALID_REQUEST"
	codeNotFound         = "NOT_FOUND"
	codeAccountRequired  = "ACCOUNT_REQUIRED"
	codeInternal         = "INTERNAL"
)

// translateCode는 조직 B 오류 코드를 공통 오류 코드로 바꾼다. 모르는 코드는 INTERNAL이다.
var translateCode = map[string]string{
	"BAD_INPUT":            codeInvalidRequest,
	"IDEMPOTENCY_MISMATCH": codeInvalidRequest,
	"API_KEY_INVALID":      codeInternal, // 어댑터 설정 오류. 엔진은 일시 장애처럼 다룬다
	"AUTH_REQUIRED":        codeIdentityRequired,
	"AUTH_LOCKED":          codeHandoffRequired,
	"NO_SUCH_RESOURCE":     codeNotFound,
	"ALREADY_DONE":         codeDuplicateRequest,
	"NOT_REFUNDABLE":       codeNotEligible,
	"NEEDS_AGENT":          codeHandoffRequired,
	"BANK_ACCOUNT_MISSING": codeAccountRequired,
	"SERVER_ERROR":         codeInternal,
	"MAINTENANCE":          codeUnavailable,
	"UPSTREAM_TIMEOUT":     codeTimeout,
}

func statusFor(code string) int {
	switch code {
	case codeInvalidRequest:
		return http.StatusBadRequest
	case codeIdentityRequired:
		return http.StatusForbidden
	case codeNotFound:
		return http.StatusNotFound
	case codeDuplicateRequest:
		return http.StatusConflict
	case codeNotEligible, codeHandoffRequired, codeAccountRequired:
		return http.StatusUnprocessableEntity
	case codeUnavailable:
		return http.StatusServiceUnavailable
	case codeTimeout:
		return http.StatusGatewayTimeout
	}
	return http.StatusInternalServerError
}

// apiError는 공통 오류 본문 {"error": {...}}의 내용이다(조직 A 모의 서버와 같은 모양).
type apiError struct {
	Code       string   `json:"code"`
	Message    string   `json:"message"`
	ArticleIDs []string `json:"article_ids,omitempty"`
	Quote      any      `json:"quote,omitempty"`
	Receipt    any      `json:"receipt,omitempty"`
}

func newError(code, msg string, articleIDs ...string) *apiError {
	return &apiError{Code: code, Message: msg, ArticleIDs: articleIDs}
}

func invalid(msg string) *apiError { return newError(codeInvalidRequest, msg) }

// Config는 어댑터 설정이다.
type Config struct {
	BackendURL string        // 조직 B 업무 시스템 주소
	APIKey     string        // 조직 B 시스템 키(X-Api-Key)
	Timeout    time.Duration // 조직 B 호출 하나의 제한 시간. 넘으면 TIMEOUT
}

// session은 X-Session-ID 하나에 대해 어댑터가 들고 있는 조직 B 회원 토큰이다.
type session struct {
	memberNo string
	token    string
	attempts int
}

type toolFunc func(g *Gateway, ctx context.Context, sid string, body []byte) (any, *apiError)

type Gateway struct {
	cfg     Config
	client  *http.Client
	log     *slog.Logger
	tools   map[string]toolFunc
	handler http.Handler

	mu       sync.Mutex
	sessions map[string]*session
}

func New(cfg Config, logger *slog.Logger) *Gateway {
	if cfg.Timeout <= 0 {
		cfg.Timeout = 3 * time.Second
	}
	cfg.BackendURL = strings.TrimRight(cfg.BackendURL, "/")
	g := &Gateway{cfg: cfg, client: &http.Client{Timeout: cfg.Timeout}, log: logger, sessions: map[string]*session{}}
	g.tools = map[string]toolFunc{
		"verify_identity":         (*Gateway).verifyIdentity,
		"get_customer":            (*Gateway).getCustomer,
		"list_subscriptions":      (*Gateway).listSubscriptions,
		"list_payments":           (*Gateway).listPayments,
		"get_usage":               (*Gateway).getUsage,
		"list_incidents":          (*Gateway).listIncidents,
		"quote_refund":            (*Gateway).quoteRefund,
		"create_cancellation":     (*Gateway).createCancellation,
		"create_refund_request":   (*Gateway).createRefundRequest,
		"register_refund_account": (*Gateway).registerRefundAccount,
		"create_handoff":          (*Gateway).createHandoff,
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/{tool}", g.handleTool)
	mux.HandleFunc("/_admin/faults", g.handleFaults)
	mux.HandleFunc("/_admin/reset", g.handleReset)
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		writeError(w, newError(codeNotFound, "경로가 없다: "+r.URL.Path))
	})
	g.handler = g.withLog(mux)
	return g
}

func (g *Gateway) ServeHTTP(w http.ResponseWriter, r *http.Request) { g.handler.ServeHTTP(w, r) }

func (g *Gateway) handleTool(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("tool")
	fn, ok := g.tools[name]
	if !ok {
		writeError(w, newError(codeNotFound, "알 수 없는 도구: "+name))
		return
	}
	if !requirePost(w, r) {
		return
	}
	sid := r.Header.Get(sessionHeader)
	if sid == "" || len(sid) > maxSessionIDLen {
		writeError(w, invalid("X-Session-ID 헤더가 필요하다(1~128자)"))
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBodyBytes))
	if err != nil {
		writeError(w, invalid("요청 본문을 읽을 수 없다"))
		return
	}
	resp, aerr := fn(g, r.Context(), sid, body)
	if aerr != nil {
		writeError(w, aerr)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// ---- 조직 B 호출 ----

// bError는 조직 B 오류 봉투 {"status": "error", "error_code", "detail", "clauses", "estimate", "existing"}다.
type bError struct {
	Code     string          `json:"error_code"`
	Detail   string          `json:"detail"`
	Clauses  []string        `json:"clauses"`
	Estimate *bEstimate      `json:"estimate"`
	Existing json.RawMessage `json:"existing"`
}

type bEnvelope struct {
	Status string          `json:"status"`
	Data   json.RawMessage `json:"data"`
	bError
}

type call struct {
	method   string
	path     string
	query    url.Values
	body     any
	sid      string
	token    string // 회원 토큰(쓰기)
	idemKey  string
	endpoint string // 로그용
}

// do는 조직 B를 한 번 부른다. 성공이면 data를, 조직 B가 오류를 돌려주면 bError를, 연결·형식 문제면 공통 오류를 돌려준다.
// 재시도는 하지 않는다. 재시도 예산은 엔진의 업무 API 클라이언트가 공통 오류 코드(TIMEOUT 등)를 보고 쓴다.
func (g *Gateway) do(ctx context.Context, c call) (json.RawMessage, *bError, *apiError) {
	var rd io.Reader
	if c.body != nil {
		b, err := json.Marshal(c.body)
		if err != nil {
			return nil, nil, newError(codeInternal, "요청을 만들 수 없다: "+err.Error())
		}
		rd = bytes.NewReader(b)
	}
	u := g.cfg.BackendURL + c.path
	if len(c.query) > 0 {
		u += "?" + c.query.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, c.method, u, rd)
	if err != nil {
		return nil, nil, newError(codeInternal, "요청을 만들 수 없다: "+err.Error())
	}
	req.Header.Set("X-Api-Key", g.cfg.APIKey)
	req.Header.Set("X-Contact-Ref", c.sid)
	if c.body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if c.token != "" {
		req.Header.Set("Authorization", "Bearer "+c.token)
	}
	if c.idemKey != "" {
		req.Header.Set("Idempotency-Key", c.idemKey)
	}
	resp, err := g.client.Do(req)
	if err != nil {
		var ne net.Error
		if errors.Is(err, context.DeadlineExceeded) || (errors.As(err, &ne) && ne.Timeout()) {
			return nil, nil, newError(codeTimeout, "조직 B 업무 시스템이 제한 시간 안에 답하지 않았다")
		}
		return nil, nil, newError(codeUnavailable, "조직 B 업무 시스템에 연결할 수 없다: "+err.Error())
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return nil, nil, newError(codeUnavailable, "조직 B 응답을 읽을 수 없다: "+err.Error())
	}
	var env bEnvelope
	if err := json.Unmarshal(raw, &env); err != nil || (env.Status != "ok" && env.Status != "error") {
		return nil, nil, newError(codeInternal, fmt.Sprintf("조직 B 응답 형식이 아니다(HTTP %d)", resp.StatusCode))
	}
	if env.Status == "error" {
		be := env.bError
		return nil, &be, nil
	}
	return env.Data, nil, nil
}

// translate는 조직 B 오류를 공통 오류로 바꾼다. 견적 판단(estimate)은 공통 견적으로, 기존 접수(existing)는 공통 접수 내역으로
// 바꿔 붙인다. reason은 견적 오류에 붙일 공통 사유다.
func (g *Gateway) translate(tool string, be *bError, reason string) *apiError {
	code, ok := translateCode[be.Code]
	if !ok {
		code = codeInternal
	}
	e := newError(code, be.Detail, be.Clauses...)
	if be.Estimate != nil {
		e.Quote = mapEstimate(*be.Estimate, reason)
	}
	if len(be.Existing) > 0 && code == codeDuplicateRequest {
		if rec, err := mapExisting(tool, be.Existing); err == nil {
			e.Receipt = rec
		}
	}
	return e
}

// decode는 공통 요청 본문을 엄격하게 읽는다. 모르는 필드가 있으면 INVALID_REQUEST(조직 A 서버와 같은 규칙).
func decode(body []byte, v any) *apiError {
	if len(bytes.TrimSpace(body)) == 0 {
		body = []byte("{}")
	}
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return invalid("요청 본문이 올바른 JSON이 아니다: " + err.Error())
	}
	if dec.More() {
		return invalid("요청 본문에 JSON 값이 두 개 이상 있다")
	}
	return nil
}

func (g *Gateway) session(sid string) *session {
	g.mu.Lock()
	defer g.mu.Unlock()
	s, ok := g.sessions[sid]
	if !ok {
		s = &session{}
		g.sessions[sid] = s
	}
	return s
}

// tokenFor는 이 세션이 그 고객으로 본인 확인을 마쳤으면 조직 B 회원 토큰을 돌려준다. 없으면 빈 값이고, 조직 B가 AUTH_REQUIRED로
// 거절하면 IDENTITY_REQUIRED가 된다(어댑터는 인증을 스스로 판단하지 않고 조직 B에 맡긴다).
func (g *Gateway) tokenFor(sid, customerID string) string {
	g.mu.Lock()
	defer g.mu.Unlock()
	if s, ok := g.sessions[sid]; ok && s.memberNo == customerID {
		return s.token
	}
	return ""
}

// ---- 관리 ----

// handleReset은 어댑터의 세션 토큰을 비우고 조직 B 모의 서버도 초기화한다.
func (g *Gateway) handleReset(w http.ResponseWriter, r *http.Request) {
	if !requirePost(w, r) {
		return
	}
	g.mu.Lock()
	g.sessions = map[string]*session{}
	g.mu.Unlock()
	if _, be, ae := g.do(r.Context(), call{method: http.MethodPost, path: "/_mock/reset", sid: "admin", endpoint: "_mock/reset"}); ae != nil || be != nil {
		writeError(w, newError(codeUnavailable, "조직 B 모의 서버를 초기화하지 못했다"))
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"reset": true})
}

// faultEndpoint는 공통 도구 이름 → 조직 B 엔드포인트 이름(장애 주입용)이다.
var faultEndpoint = map[string]string{
	"verify_identity": "auth-verify", "get_customer": "member", "list_subscriptions": "passes", "list_payments": "charges",
	"get_usage": "reading-log", "list_incidents": "outages", "quote_refund": "refund-estimates",
	"create_refund_request": "refunds", "create_cancellation": "pass-cancel", "register_refund_account": "refund-account",
	"create_handoff": "agent-tickets",
}

type faultRequest struct {
	Tool  string `json:"tool"`
	Code  string `json:"code"`
	Count int    `json:"count"`
}

// handleFaults는 공통 장애 주입 요청({"tool", "code", "count"})을 조직 B의 /_mock/faults로 넘긴다.
// TIMEOUT → timeout(조직 B UPSTREAM_TIMEOUT), UNAVAILABLE → maintenance(조직 B MAINTENANCE).
func (g *Gateway) handleFaults(w http.ResponseWriter, r *http.Request) {
	if !requirePost(w, r) {
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBodyBytes))
	if err != nil {
		writeError(w, invalid("요청 본문을 읽을 수 없다"))
		return
	}
	var req faultRequest
	if e := decode(body, &req); e != nil {
		writeError(w, e)
		return
	}
	ep, ok := faultEndpoint[req.Tool]
	if !ok {
		writeError(w, invalid("알 수 없는 도구: "+req.Tool))
		return
	}
	mode := map[string]string{codeTimeout: "timeout", codeUnavailable: "maintenance"}[req.Code]
	if mode == "" {
		writeError(w, invalid("code는 TIMEOUT 또는 UNAVAILABLE이어야 한다"))
		return
	}
	if req.Count == 0 {
		req.Count = 1
	}
	if req.Count < 0 || req.Count > 1000 {
		writeError(w, invalid("count는 1~1000이어야 한다"))
		return
	}
	body2 := map[string]any{"endpoint": ep, "mode": mode, "times": req.Count}
	if _, be, ae := g.do(r.Context(), call{method: http.MethodPost, path: "/_mock/faults", body: body2, sid: "admin", endpoint: "_mock/faults"}); ae != nil || be != nil {
		writeError(w, newError(codeUnavailable, "조직 B 모의 서버에 장애를 넣지 못했다"))
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"tool": req.Tool, "code": req.Code, "remaining": req.Count, "backend_endpoint": ep})
}

// ---- 공통 ----

func requirePost(w http.ResponseWriter, r *http.Request) bool {
	if r.Method == http.MethodPost {
		return true
	}
	w.Header().Set("Allow", http.MethodPost)
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(http.StatusMethodNotAllowed)
	_ = json.NewEncoder(w).Encode(map[string]*apiError{"error": invalid("POST만 허용한다")})
	return false
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, e *apiError) {
	if rec, ok := w.(*recorder); ok {
		rec.code = e.Code
	}
	writeJSON(w, statusFor(e.Code), map[string]*apiError{"error": e})
}

type recorder struct {
	http.ResponseWriter
	status int
	code   string
}

func (rec *recorder) WriteHeader(status int) {
	rec.status = status
	rec.ResponseWriter.WriteHeader(status)
}

// withLog는 요청마다 JSON 로그 한 줄을 남긴다: session, tool, status, code, latency_ms. 본문은 남기지 않는다.
func (g *Gateway) withLog(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &recorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		sid := r.Header.Get(sessionHeader)
		if len(sid) > maxSessionIDLen {
			sid = sid[:maxSessionIDLen]
		}
		g.log.LogAttrs(r.Context(), slog.LevelInfo, "request",
			slog.String("session", sid), slog.String("tool", strings.TrimPrefix(r.URL.Path, "/v1/")),
			slog.Int("status", rec.status), slog.String("code", rec.code),
			slog.Float64("latency_ms", float64(time.Since(start).Microseconds())/1000))
	})
}
