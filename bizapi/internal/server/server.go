// Package server는 조직 A 모의 업무 API의 HTTP 계층이다.
//
// 계약은 domain/org_a/api.md를 따른다. 모든 도구는 POST /v1/<도구 이름>이고 본문과 응답은 JSON이다.
// 세션(본인 확인), 멱등 키, 접수 내역, 장애 주입은 메모리에만 둔다. 금액과 자격 판단은 orga 패키지가 한다.
package server

import (
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/jabdori/jikgam/bizapi/internal/orga"
)

const (
	sessionHeader   = "X-Session-ID"
	maxSessionIDLen = 128
	maxBodyBytes    = 1 << 20
)

// 오류 코드. 앞의 다섯 개는 api.md의 코드이고, 나머지는 이 서버가 더한 코드다.
const (
	codeIdentityRequired = "IDENTITY_REQUIRED"
	codeNotEligible      = "NOT_ELIGIBLE"
	codeHandoffRequired  = "HANDOFF_REQUIRED"
	codeTimeout          = "TIMEOUT"
	codeUnavailable      = "UNAVAILABLE"
	codeDuplicateRequest = "DUPLICATE_REQUEST"

	codeInvalidRequest  = "INVALID_REQUEST"  // 필수 값 누락, 형식 오류, 멱등 키 재사용
	codeNotFound        = "NOT_FOUND"        // 고객·구독·결제·견적·경로 없음
	codeAccountRequired = "ACCOUNT_REQUIRED" // 계좌 환불인데 환불 계좌가 등록되지 않음(제14조 제3항)
	codeInternal        = "INTERNAL"
)

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

// apiError는 오류 응답 본문 {"error": {...}}의 내용이다.
// code와 message는 항상 있고, 상황에 따라 근거 조항, 견적, 기존 접수 내역을 덧붙인다.
type apiError struct {
	Status     int         `json:"-"`
	Code       string      `json:"code"`
	Message    string      `json:"message"`
	ArticleIDs []string    `json:"article_ids,omitempty"`
	Quote      *orga.Quote `json:"quote,omitempty"`   // quote_refund의 NOT_ELIGIBLE·HANDOFF_REQUIRED
	Receipt    any         `json:"receipt,omitempty"` // DUPLICATE_REQUEST의 기존 접수 내역
}

func newError(code, msg string, articleIDs ...string) *apiError {
	return &apiError{Status: statusFor(code), Code: code, Message: msg, ArticleIDs: articleIDs}
}

func invalid(msg string) *apiError  { return newError(codeInvalidRequest, msg) }
func notFound(msg string) *apiError { return newError(codeNotFound, msg) }

// toolFunc는 도구 하나의 처리 함수다. Server.mu를 잡은 상태에서 불린다.
type toolFunc func(s *Server, sessionID string, body []byte) (any, *apiError)

// Server는 http.Handler다.
type Server struct {
	ds       *orga.Dataset
	log      *slog.Logger
	tools    map[string]toolFunc
	handler  http.Handler
	instance *InstanceInfo

	mu sync.Mutex
	st *state
}

// New는 데이터와 로거로 서버를 만든다. 로거에는 요청마다 JSON 한 줄을 남긴다.
func New(ds *orga.Dataset, logger *slog.Logger) *Server {
	s := &Server{ds: ds, log: logger, st: newState()}
	s.tools = map[string]toolFunc{
		"verify_identity":         (*Server).verifyIdentity,
		"get_customer":            (*Server).getCustomer,
		"list_subscriptions":      (*Server).listSubscriptions,
		"list_payments":           (*Server).listPayments,
		"get_usage":               (*Server).getUsage,
		"list_incidents":          (*Server).listIncidents,
		"quote_refund":            (*Server).quoteRefund,
		"create_cancellation":     (*Server).createCancellation,
		"create_refund_request":   (*Server).createRefundRequest,
		"register_refund_account": (*Server).registerRefundAccount,
		"create_handoff":          (*Server).createHandoff,
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/{tool}", s.handleTool)
	mux.HandleFunc("/_admin/faults", s.handleFaults)
	mux.HandleFunc("/_admin/reset", s.handleReset)
	mux.HandleFunc("GET /_admin/instance", s.handleInstance)
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		writeError(w, notFound("경로가 없다: "+r.URL.Path))
	})
	s.handler = s.withLog(withRecover(mux))
	return s
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) { s.handler.ServeHTTP(w, r) }

func (s *Server) handleTool(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("tool")
	fn, ok := s.tools[name]
	if !ok {
		writeError(w, notFound("알 수 없는 도구: "+name))
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
	resp, aerr := s.call(name, fn, sid, body)
	if aerr != nil {
		writeError(w, aerr)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// call은 상태 잠금 안에서 장애 주입을 먼저 보고 도구를 실행한다.
// 주입된 장애는 실행 전에 돌려주므로 상태를 바꾸지 않는다.
func (s *Server) call(name string, fn toolFunc, sid string, body []byte) (any, *apiError) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if aerr := s.st.takeFault(name); aerr != nil {
		return nil, aerr
	}
	return fn(s, sid, body)
}

type faultRequest struct {
	Tool  string `json:"tool"`
	Code  string `json:"code"`
	Count int    `json:"count"`
}

// handleFaults는 다음 count번의 tool 호출이 code(TIMEOUT·UNAVAILABLE)를 돌려주게 한다.
// 같은 도구에 다시 설정하면 덮어쓴다. count를 빼면 1이다.
func (s *Server) handleFaults(w http.ResponseWriter, r *http.Request) {
	if !requirePost(w, r) {
		return
	}
	var req faultRequest
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBodyBytes))
	if err != nil {
		writeError(w, invalid("요청 본문을 읽을 수 없다"))
		return
	}
	if aerr := decode(body, &req); aerr != nil {
		writeError(w, aerr)
		return
	}
	if _, ok := s.tools[req.Tool]; !ok {
		writeError(w, invalid("알 수 없는 도구: "+req.Tool))
		return
	}
	if req.Code != codeTimeout && req.Code != codeUnavailable {
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
	s.mu.Lock()
	s.st.faults[req.Tool] = &fault{code: req.Code, remaining: req.Count}
	s.mu.Unlock()
	writeJSON(w, http.StatusOK, map[string]any{"tool": req.Tool, "code": req.Code, "remaining": req.Count})
}

// handleReset은 세션, 견적, 접수 내역, 멱등 키, 장애 주입, 접수 번호를 모두 비운다. 고객 데이터는 그대로다.
func (s *Server) handleReset(w http.ResponseWriter, r *http.Request) {
	if !requirePost(w, r) {
		return
	}
	s.mu.Lock()
	s.st = newState()
	s.mu.Unlock()
	writeJSON(w, http.StatusOK, map[string]any{"reset": true})
}

func requirePost(w http.ResponseWriter, r *http.Request) bool {
	if r.Method == http.MethodPost {
		return true
	}
	w.Header().Set("Allow", http.MethodPost)
	writeError(w, &apiError{Status: http.StatusMethodNotAllowed, Code: codeInvalidRequest, Message: "POST만 허용한다"})
	return false
}

// decode는 JSON 본문을 엄격하게 읽는다. 모르는 필드가 있으면 거절해 엔진의 인자 오류를 드러낸다.
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
	writeJSON(w, e.Status, map[string]*apiError{"error": e})
}

// recorder는 로그에 남길 상태 코드와 오류 코드를 기록한다.
type recorder struct {
	http.ResponseWriter
	status int
	code   string
}

func (rec *recorder) WriteHeader(status int) {
	rec.status = status
	rec.ResponseWriter.WriteHeader(status)
}

// withLog는 요청마다 JSON 로그 한 줄을 남긴다: time, session, tool, status, code, latency_ms.
// 개인정보가 들어 있으므로 요청·응답 본문은 남기지 않는다.
func (s *Server) withLog(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &recorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		s.log.LogAttrs(r.Context(), slog.LevelInfo, "request",
			slog.String("session", truncate(r.Header.Get(sessionHeader), maxSessionIDLen)),
			slog.String("tool", toolName(r.URL.Path)),
			slog.String("method", r.Method),
			slog.Int("status", rec.status),
			slog.String("code", rec.code),
			slog.Float64("latency_ms", float64(time.Since(start).Microseconds())/1000),
		)
	})
}

func withRecover(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if v := recover(); v != nil {
				if v == http.ErrAbortHandler {
					panic(v)
				}
				writeError(w, newError(codeInternal, "서버 내부 오류"))
			}
		}()
		next.ServeHTTP(w, r)
	})
}

func toolName(path string) string {
	if name, ok := strings.CutPrefix(path, "/v1/"); ok {
		return truncate(name, 64)
	}
	return truncate(strings.TrimPrefix(path, "/"), 64)
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
