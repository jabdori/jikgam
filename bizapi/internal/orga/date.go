package orga

import (
	"encoding/json"
	"fmt"
	"time"
)

const dateLayout = "2006-01-02"

// Date는 시각이 없는 달력 날짜다. 데이터의 날짜는 모두 KST 달력 날짜이고,
// 내부에서는 UTC 자정으로 두어 일 수 계산에 시차가 끼지 않게 한다.
type Date struct{ t time.Time }

// NewDate는 연·월·일로 날짜를 만든다.
func NewDate(year int, month time.Month, day int) Date {
	return Date{time.Date(year, month, day, 0, 0, 0, 0, time.UTC)}
}

// ParseDate는 YYYY-MM-DD 문자열을 읽는다.
func ParseDate(s string) (Date, error) {
	t, err := time.ParseInLocation(dateLayout, s, time.UTC)
	if err != nil {
		return Date{}, fmt.Errorf("날짜는 YYYY-MM-DD 형식이어야 한다: %q", s)
	}
	return Date{t}, nil
}

func (d Date) IsZero() bool { return d.t.IsZero() }

func (d Date) String() string {
	if d.IsZero() {
		return ""
	}
	return d.t.Format(dateLayout)
}

func (d Date) Compare(o Date) int { return d.t.Compare(o.t) }
func (d Date) Before(o Date) bool { return d.t.Before(o.t) }
func (d Date) After(o Date) bool  { return d.t.After(o.t) }
func (d Date) AddDays(n int) Date { return Date{d.t.AddDate(0, 0, n)} }
func (d Date) SameMonth(o Date) bool {
	return d.t.Year() == o.t.Year() && d.t.Month() == o.t.Month()
}

// AddMonths는 n개월 뒤의 같은 날을 돌려준다. 그 달에 같은 날이 없으면 말일로 맞춘다.
// 예: 1월 31일 + 1개월 = 2월 28일(평년). 항상 원래 날짜에서 계산하므로 오차가 쌓이지 않는다.
func (d Date) AddMonths(n int) Date {
	y, m, day := d.t.Date()
	first := time.Date(y, m+time.Month(n), 1, 0, 0, 0, 0, time.UTC)
	last := first.AddDate(0, 1, -1).Day()
	return Date{time.Date(first.Year(), first.Month(), min(day, last), 0, 0, 0, 0, time.UTC)}
}

// DaysBetween은 from에서 to까지의 일 수다. from 다음 날이 1이다.
func DaysBetween(from, to Date) int {
	return int((to.t.Unix() - from.t.Unix()) / 86400)
}

// AgeOn은 on 날짜 기준 만 나이다.
func AgeOn(birth, on Date) int {
	age := on.t.Year() - birth.t.Year()
	if on.t.Month() < birth.t.Month() || (on.t.Month() == birth.t.Month() && on.t.Day() < birth.t.Day()) {
		age--
	}
	return age
}

func (d Date) MarshalJSON() ([]byte, error) {
	if d.IsZero() {
		return []byte("null"), nil
	}
	return json.Marshal(d.String())
}

func (d *Date) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err != nil {
		return err
	}
	parsed, err := ParseDate(s)
	if err != nil {
		return err
	}
	*d = parsed
	return nil
}
