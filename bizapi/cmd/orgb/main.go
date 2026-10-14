// orgb는 가상 조직 B(책솔) 업무 시스템의 모의 서버다. 상담 엔진은 이 서버를 직접 부르지 않고 어댑터(cmd/orgbgw)를 거친다.
//
//	go run ./cmd/orgb -addr 127.0.0.1:8097 -data ../domain/org_b/customers.json
//
// 시스템 키는 -api-key 또는 환경 변수 ORGB_API_KEY로 정한다(모의 값이며 실제 비밀이 아니다).
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

	"github.com/jabdori/jikgam/bizapi/internal/orgb"
	"github.com/jabdori/jikgam/bizapi/internal/orgbsrv"
)

// DefaultAPIKey는 모의 조직 B 시스템 키다. 어댑터의 기본값과 같다.
const DefaultAPIKey = "orgb-mock-key"

func main() {
	addr := flag.String("addr", "127.0.0.1:8097", "수신 주소")
	data := flag.String("data", "../domain/org_b/customers.json", "조직 B 회원 데이터 경로")
	key := flag.String("api-key", envOr("ORGB_API_KEY", DefaultAPIKey), "호출 시스템이 X-Api-Key로 보내야 하는 값")
	flag.Parse()
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	if err := run(*addr, *data, *key, logger); err != nil {
		logger.Error("종료", "error", err.Error())
		os.Exit(1)
	}
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func run(addr, dataPath, apiKey string, logger *slog.Logger) error {
	ds, err := orgb.Load(dataPath)
	if err != nil {
		return err
	}
	srv := &http.Server{Addr: addr, Handler: orgbsrv.New(ds, apiKey, logger), ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second, IdleTimeout: 60 * time.Second}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	errc := make(chan error, 1)
	go func() { errc <- srv.ListenAndServe() }()
	logger.Info("시작", "addr", addr, "data", dataPath, "members", len(ds.Members), "as_of", ds.AsOf.String())
	select {
	case err := <-errc:
		if !errors.Is(err, http.ErrServerClosed) {
			return err
		}
		return nil
	case <-ctx.Done():
	}
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return srv.Shutdown(shutdownCtx)
}
