package main

import (
	"flag"
	"io"
	"slices"
	"testing"
)

func parseData(t *testing.T, args ...string) []string {
	t.Helper()
	fs := flag.NewFlagSet("bizapi", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	data := &dataPaths{paths: []string{defaultData}}
	fs.Var(data, "data", "")
	if err := fs.Parse(args); err != nil {
		t.Fatalf("parse %v: %v", args, err)
	}
	return data.paths
}

func TestDataFlag(t *testing.T) {
	for _, tc := range []struct {
		args []string
		want []string
	}{
		{nil, []string{defaultData}},
		{[]string{"-data", "a.json"}, []string{"a.json"}},
		{[]string{"-data", "a.json", "-data", "b.json"}, []string{"a.json", "b.json"}},
		{[]string{"-data", "a.json, b.json"}, []string{"a.json", "b.json"}},
	} {
		if got := parseData(t, tc.args...); !slices.Equal(got, tc.want) {
			t.Errorf("%v → %v, want %v", tc.args, got, tc.want)
		}
	}
	fs := flag.NewFlagSet("bizapi", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	fs.Var(&dataPaths{}, "data", "")
	if err := fs.Parse([]string{"-data", " , "}); err == nil {
		t.Error("빈 경로는 거절해야 한다")
	}
}
