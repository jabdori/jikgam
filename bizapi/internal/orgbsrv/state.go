package orgbsrv

import (
	"fmt"

	"github.com/jabdori/jikgam/bizapi/internal/orgb"
)

// state는 서버의 바뀌는 상태다. Server.mu를 잡은 상태에서만 접근하고 /_mock/reset으로 비운다.
type state struct {
	failures  map[string]int            // X-Contact-Ref → 본인 확인 실패 수
	tokens    map[string]string         // 회원 토큰 → 회원 번호
	estimates map[string]*orgb.Estimate // 견적 번호 → 견적
	refunds   map[string]*Refund        // 결제 번호 → 환불 접수
	cancels   map[string]*Cancel        // 이용권 번호 → 해지 접수
	accounts  map[string]*Account       // 회원 번호 → 환불 계좌
	idem      map[string]idemEntry      // 엔드포인트 + 멱등 키 → 처음 결과
	faults    map[string]*fault         // 엔드포인트 → 주입한 장애
	seq       map[string]int
}

func newState() *state {
	return &state{
		failures: map[string]int{}, tokens: map[string]string{}, estimates: map[string]*orgb.Estimate{},
		refunds: map[string]*Refund{}, cancels: map[string]*Cancel{}, accounts: map[string]*Account{},
		idem: map[string]idemEntry{}, faults: map[string]*fault{}, seq: map[string]int{},
	}
}

func (st *state) nextID(prefix string) string {
	st.seq[prefix]++
	return fmt.Sprintf("%s-%06d", prefix, st.seq[prefix])
}

type idemEntry struct {
	fingerprint string
	resp        any
}

type fault struct {
	code      string
	remaining int
}

func (st *state) takeFault(endpoint string) *bErr {
	f, ok := st.faults[endpoint]
	if !ok || f.remaining <= 0 {
		return nil
	}
	f.remaining--
	if f.remaining == 0 {
		delete(st.faults, endpoint)
	}
	return newErr(f.code, "주입된 장애: "+endpoint)
}
