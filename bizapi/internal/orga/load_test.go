package orga

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const holdoutDataPath = "../../../domain/org_a/customers_holdout.json"

func writeData(t *testing.T, name, body string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), name)
	if err := os.WriteFile(p, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

// TestLoadAllSingleFile은 파일 하나면 Load와 결과가 같은지 본다(기존 동작 유지).
func TestLoadAllSingleFile(t *testing.T) {
	want, err := Load(dataPath)
	if err != nil {
		t.Fatal(err)
	}
	got, err := LoadAll(dataPath)
	if err != nil {
		t.Fatal(err)
	}
	wb, _ := json.Marshal(want)
	gb, _ := json.Marshal(got)
	if string(wb) != string(gb) || len(got.Customers) != len(want.Customers) || len(got.Incidents) != len(want.Incidents) {
		t.Errorf("LoadAll(한 파일) 결과가 Load와 다르다")
	}
	if _, err := LoadAll(); err == nil {
		t.Error("파일이 없으면 오류여야 한다")
	}
}

// TestLoadAllMerge는 개발용 고객 데이터와 보류용 고객 데이터를 합쳐 읽는다.
func TestLoadAllMerge(t *testing.T) {
	dev, err := Load(dataPath)
	if err != nil {
		t.Fatal(err)
	}
	hold, err := Load(holdoutDataPath)
	if err != nil {
		t.Fatal(err)
	}
	ds, err := LoadAll(dataPath, holdoutDataPath)
	if err != nil {
		t.Fatal(err)
	}
	if len(ds.Customers) != len(dev.Customers)+len(hold.Customers) || len(ds.Incidents) != len(dev.Incidents)+len(hold.Incidents) {
		t.Errorf("병합 수: 고객 %d, 장애 %d", len(ds.Customers), len(ds.Incidents))
	}
	for _, c := range append(dev.Customers, hold.Customers...) {
		if got, ok := ds.Customer(c.ID); !ok || got.Name != c.Name {
			t.Errorf("병합 데이터에 고객 %s가 없다", c.ID)
		}
		if got, ok := ds.CustomerByPhone(c.Phone); !ok || got.ID != c.ID {
			t.Errorf("병합 데이터에서 번호 %s로 고객 %s를 찾지 못했다", c.Phone, c.ID)
		}
	}
	if ds.ReferenceDate.Compare(dev.ReferenceDate) != 0 {
		t.Errorf("reference_date = %s", ds.ReferenceDate)
	}
}

func TestLoadAllConflicts(t *testing.T) {
	const customer = `{"id": "%[1]s", "name": "가상", "phone": "%[2]s", "birth": "1990-01-01", "status": "active",
		"subscriptions": [{"id": "S%[1]s", "plan": "monthly", "price": 9900, "start": "2026-10-01", "next_billing": "2026-11-01"}],
		"payments": [], "usage": []}`
	file := func(ref, incidentID, id, phone string) string {
		c := fmt.Sprintf(customer, id, phone)
		inc := ""
		if incidentID != "" {
			inc = `{"id": "` + incidentID + `", "start": "2026-10-01T10:00", "end": "2026-10-01T11:00", "hours": 1, "cause": "company"}`
		}
		return `{"reference_date": "` + ref + `", "incidents": [` + inc + `], "customers": [` + c + `]}`
	}
	a := writeData(t, "a.json", file("2026-10-12", "I-1", "X001", "010-1111-0001"))
	for _, tc := range []struct {
		name, body, want string
	}{
		{"고객 ID 중복", file("2026-10-12", "", "X001", "010-1111-0002"), "고객 ID"},
		{"휴대폰 번호 중복", file("2026-10-12", "", "X002", "010-1111-0001"), "휴대폰"},
		{"장애 ID 중복", file("2026-10-12", "I-1", "X002", "010-1111-0002"), "장애 ID"},
		{"기준일 다름", file("2026-10-13", "", "X002", "010-1111-0002"), "reference_date"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			b := writeData(t, "b.json", tc.body)
			_, err := LoadAll(a, b)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Errorf("err = %v, want %q 포함", err, tc.want)
			}
		})
	}
	ok := writeData(t, "c.json", file("2026-10-12", "I-2", "X002", "010-1111-0002"))
	ds, err := LoadAll(a, ok)
	if err != nil || len(ds.Customers) != 2 || len(ds.Incidents) != 2 {
		t.Errorf("겹치지 않는 두 파일 병합 실패: %v", err)
	}
	if _, err := LoadAll(dataPath, dataPath); err == nil {
		t.Error("같은 파일을 두 번 주면 ID 중복 오류여야 한다")
	}
}
