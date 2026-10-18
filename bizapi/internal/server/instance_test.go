package server

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/jabdori/jikgam/bizapi/internal/orga"
)

func TestInstanceStatusWithoutSession(t *testing.T) {
	logger := slog.New(slog.NewJSONHandler(io.Discard, nil))
	info := InstanceInfo{Nonce: "첫기동", BinarySHA256: "실행파일해시", DataFiles: map[string]string{"합성.json": "데이터해시"}}
	s := NewWithInstance(&orga.Dataset{}, logger, info)
	info.DataFiles["합성.json"] = "외부수정"
	w := httptest.NewRecorder()
	s.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/_admin/instance", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("상태 응답: %d", w.Code)
	}
	var got InstanceInfo
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got.Nonce != "첫기동" || got.BinarySHA256 != "실행파일해시" || got.DataFiles["합성.json"] != "데이터해시" {
		t.Fatalf("기동 정보가 바뀜: %+v", got)
	}
	second := NewWithInstance(&orga.Dataset{}, logger, InstanceInfo{Nonce: "다음기동"})
	w = httptest.NewRecorder()
	second.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/_admin/instance", nil))
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil || got.Nonce == "첫기동" {
		t.Fatal("다른 기동은 다른 식별자를 돌려줘야 한다")
	}
}

func TestInstanceStatusRequiresManagedInfoAndGet(t *testing.T) {
	s := New(&orga.Dataset{}, slog.New(slog.NewJSONHandler(io.Discard, nil)))
	w := httptest.NewRecorder()
	s.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/_admin/instance", nil))
	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("기동 정보 없는 서버: %d", w.Code)
	}
	w = httptest.NewRecorder()
	s.ServeHTTP(w, httptest.NewRequest(http.MethodPost, "/_admin/instance", nil))
	if w.Code == http.StatusOK {
		t.Fatal("POST로 상태를 바꿀 수 없어야 한다")
	}
}
