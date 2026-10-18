package server

import (
	"log/slog"
	"maps"
	"net/http"
	"slices"

	"github.com/jabdori/jikgam/bizapi/internal/orga"
)

// InstanceInfo는 현재 기동의 식별자와 실제 실행 파일·적재 데이터 해시다. 고객 원문은 넣지 않는다.
type InstanceInfo struct {
	Nonce        string            `json:"nonce"`
	BinarySHA256 string            `json:"binary_sha256"`
	DataFiles    map[string]string `json:"data_files"`
	DataPaths    []string          `json:"data_paths"`
}

// NewWithInstance는 업무 동작을 그대로 두고 현재 기동을 확인할 상태 응답만 덧붙인다.
func NewWithInstance(ds *orga.Dataset, logger *slog.Logger, instance InstanceInfo) *Server {
	s := New(ds, logger)
	instance.DataFiles = maps.Clone(instance.DataFiles)
	instance.DataPaths = slices.Clone(instance.DataPaths)
	s.instance = &instance
	return s
}

func (s *Server) handleInstance(w http.ResponseWriter, r *http.Request) {
	if s.instance == nil || s.instance.Nonce == "" {
		writeError(w, newError(codeUnavailable, "현재 기동 정보가 없다"))
		return
	}
	writeJSON(w, http.StatusOK, s.instance)
}
