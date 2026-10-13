// bizapi는 가상 조직 A(별담)의 모의 업무 API 서버다. 상담 엔진은 금액과 자격을 직접 계산하지 않고 이 서버를 호출한다.
//
//	go run ./cmd/bizapi -addr 127.0.0.1:8081 -data ../domain/org_a/customers.json
//
// -data는 여러 번 주거나 쉼표로 나눠 여러 파일을 줄 수 있다. 고객과 장애를 합치며, ID가 겹치면 시작하지 않는다.
package main

import (
	"context"
	"errors"
	"flag"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/jabdori/jikgam/bizapi/internal/orga"
	"github.com/jabdori/jikgam/bizapi/internal/server"
)

const defaultData = "../domain/org_a/customers.json"

// dataPaths는 -data 플래그 값이다. 처음 지정하면 기본값을 버리고, 이후 지정할 때마다 덧붙인다.
type dataPaths struct {
	paths []string
	set   bool
}

func (p *dataPaths) String() string { return strings.Join(p.paths, ",") }

func (p *dataPaths) Set(v string) error {
	if !p.set {
		p.paths, p.set = nil, true
	}
	for _, s := range strings.Split(v, ",") {
		if s = strings.TrimSpace(s); s != "" {
			p.paths = append(p.paths, s)
		}
	}
	if len(p.paths) == 0 {
		return errors.New("경로가 비었다")
	}
	return nil
}

func main() {
	addr := flag.String("addr", "127.0.0.1:8081", "수신 주소")
	data := &dataPaths{paths: []string{defaultData}}
	flag.Var(data, "data", "조직 A 고객 데이터(customers.json) 경로. 여러 번 주거나 쉼표로 나누면 합쳐 읽는다")
	flag.Parse()

	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	if err := run(*addr, data.paths, logger); err != nil {
		logger.Error("종료", "error", err.Error())
		os.Exit(1)
	}
}

func run(addr string, dataPaths []string, logger *slog.Logger) error {
	ds, err := orga.LoadAll(dataPaths...)
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
	logger.Info("시작", "addr", addr, "data", strings.Join(dataPaths, ","), "customers", len(ds.Customers), "reference_date", ds.ReferenceDate.String())

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
