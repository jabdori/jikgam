package orgb

import (
	"encoding/json"
	"fmt"
	"time"
)

const dateLayout = "2006-01-02"

// Date는 시각이 없는 KST 달력 날짜다. 일 수 계산에 시차가 끼지 않게 UTC 자정으로 둔다.
// 조직 A 패키지(orga)와 일부러 공유하지 않는다. 조직별 업무 시스템은 서로 독립이다.
type Date struct{ t time.Time }

func NewDate(year int, month time.Month, day int) Date {
	return Date{time.Date(year, month, day, 0, 0, 0, 0, time.UTC)}
}

func ParseDate(s string) (Date, error) {
	t, err := time.ParseInLocation(dateLayout, s, time.UTC)
	if err != nil {
		return Date{}, fmt.Errorf("날짜는 YYYY-MM-DD 형식이어야 한다: %q", s)
	}
	return Date{t}, nil
}

func dateOf(t time.Time) Date { return NewDate(t.Year(), t.Month(), t.Day()) }

func (d Date) IsZero() bool { return d.t.IsZero() }

func (d Date) String() string {
	if d.IsZero() {
		return ""
	}
	return d.t.Format(dateLayout)
}

func (d Date) Before(o Date) bool { return d.t.Before(o.t) }
func (d Date) After(o Date) bool  { return d.t.After(o.t) }
func (d Date) AddDays(n int) Date { return Date{d.t.AddDate(0, 0, n)} }
func (d Date) SameMonth(o Date) bool {
	return d.t.Year() == o.t.Year() && d.t.Month() == o.t.Month()
}

// AddMonths는 n개월 뒤의 같은 날이다. 그 달에 같은 날이 없으면 말일로 맞춘다.
func (d Date) AddMonths(n int) Date {
	y, m, day := d.t.Date()
	first := time.Date(y, m+time.Month(n), 1, 0, 0, 0, 0, time.UTC)
	last := first.AddDate(0, 1, -1).Day()
	return Date{time.Date(first.Year(), first.Month(), min(day, last), 0, 0, 0, 0, time.UTC)}
}

// DaysBetween은 from 다음 날을 1로 세어 to까지의 일 수다.
func DaysBetween(from, to Date) int { return int((to.t.Unix() - from.t.Unix()) / 86400) }

func (d Date) MarshalJSON() ([]byte, error) {
	if d.IsZero() {
		return []byte("null"), nil
	}
	return json.Marshal(d.String())
}
