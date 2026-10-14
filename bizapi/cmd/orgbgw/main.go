// orgbgw는 조직 B 업무 API 어댑터다. 상담 엔진의 공통 계약(POST /v1/<도구>, X-Session-ID)을 받아 조직 B 업무 시스템
// (cmd/orgb)의 계약으로 바꿔 부른다. 엔진의 조직 B 설정(config/orgs/org_b.toml)의 bizapi_url이 이 주소다.
//
//	go run ./cmd/orgbgw -addr 127.0.0.1:8098 -backend http://127.0.0.1:8097
package main

import (
	"context"
	"errors"
	"flag"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/jabdori/jikgam/bizapi/internal/orgbgw"
)

const defaultAPIKey = "orgb-mock-key" // cmd/orgb의 기본값과 같은 모의 값

func main() {
	addr := flag.String("addr", "127.0.0.1:8098", "수신 주소(엔진이 부르는 공통 계약)")
	backend := flag.String("backend", "http://127.0.0.1:8097", "조직 B 업무 시스템 주소")
	key := flag.String("api-key", envOr("ORGB_API_KEY", defaultAPIKey), "조직 B 시스템 키(X-Api-Key)")
	timeout := flag.Duration("timeout", 3*time.Second, "조직 B 호출 하나의 제한 시간(넘으면 TIMEOUT)")
	flag.Parse()
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	gw := orgbgw.New(orgbgw.Config{BackendURL: *backend, APIKey: *key, Timeout: *timeout}, logger)
	srv := &http.Server{Addr: *addr, Handler: gw, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second,
		WriteTimeout: 10 * time.Second, IdleTimeout: 60 * time.Second}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	errc := make(chan error, 1)
	go func() { errc <- srv.ListenAndServe() }()
	logger.Info("시작", "addr", *addr, "backend", *backend)
	select {
	case err := <-errc:
		if !errors.Is(err, http.ErrServerClosed) {
			logger.Error("종료", "error", err.Error())
			os.Exit(1)
		}
		return
	case <-ctx.Done():
	}
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = srv.Shutdown(shutdownCtx)
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
