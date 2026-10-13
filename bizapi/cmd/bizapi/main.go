// bizapi는 가상 조직 A(별담)의 모의 업무 API 서버다. 상담 엔진은 금액과 자격을 직접 계산하지 않고 이 서버를 호출한다.
//
//	go run ./cmd/bizapi -addr 127.0.0.1:8081 -data ../domain/org_a/customers.json
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

	"github.com/jabdori/jikgam/bizapi/internal/orga"
	"github.com/jabdori/jikgam/bizapi/internal/server"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8081", "수신 주소")
	data := flag.String("data", "../domain/org_a/customers.json", "조직 A 고객 데이터(customers.json) 경로")
	flag.Parse()

	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	if err := run(*addr, *data, logger); err != nil {
		logger.Error("종료", "error", err.Error())
		os.Exit(1)
	}
}

func run(addr, dataPath string, logger *slog.Logger) error {
	ds, err := orga.Load(dataPath)
	if err != nil {
		return err
	}
	srv := &http.Server{
		Addr:              addr,
		Handler:           server.New(ds, logger),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      10 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	errc := make(chan error, 1)
	go func() { errc <- srv.ListenAndServe() }()
	logger.Info("시작", "addr", addr, "data", dataPath, "customers", len(ds.Customers), "reference_date", ds.ReferenceDate.String())

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
