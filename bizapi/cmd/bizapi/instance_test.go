package main

import (
	"os"
	"path/filepath"
	"slices"
	"testing"
)

func TestInstanceUsesFreshNonceAndActualFileHashes(t *testing.T) {
	path := filepath.Join(t.TempDir(), "합성.json")
	if err := os.WriteFile(path, []byte("abc"), 0o600); err != nil {
		t.Fatal(err)
	}
	first, err := instanceInfo([]string{path}, "")
	if err != nil {
		t.Fatal(err)
	}
	second, err := instanceInfo([]string{path}, "")
	if err != nil {
		t.Fatal(err)
	}
	canonical, err := filepath.EvalSymlinks(path)
	if err != nil {
		t.Fatal(err)
	}
	if first.Nonce == "" || first.Nonce == second.Nonce {
		t.Fatal("기동마다 새 식별자가 필요하다")
	}
	if len(first.BinarySHA256) != 64 || first.BinarySHA256 != second.BinarySHA256 {
		t.Fatal("같은 실행 파일의 SHA-256이 필요하다")
	}
	if first.DataFiles[canonical] != "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad" {
		t.Fatal("실제 적재 파일 바이트의 SHA-256과 다르다")
	}
	if err := os.WriteFile(path, []byte("수정"), 0o600); err != nil {
		t.Fatal(err)
	}
	changed, err := instanceInfo([]string{path}, "전달한기동식별자")
	if err != nil {
		t.Fatal(err)
	}
	if changed.Nonce != "전달한기동식별자" || changed.DataFiles[canonical] == first.DataFiles[canonical] {
		t.Fatal("전달한 식별자와 수정된 데이터 해시를 돌려줘야 한다")
	}
}

func TestInstanceRejectsMissingDataFile(t *testing.T) {
	if _, err := instanceInfo([]string{filepath.Join(t.TempDir(), "없음.json")}, "기동"); err == nil {
		t.Fatal("없는 데이터는 거절해야 한다")
	}
}

func TestInstanceDataPathsPreserveMergeOrder(t *testing.T) {
	paths := []string{filepath.Join(t.TempDir(), "셋.json"), filepath.Join(t.TempDir(), "하나.json"), filepath.Join(t.TempDir(), "둘.json")}
	for _, path := range paths {
		if err := os.WriteFile(path, []byte("abc"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	got, err := instanceInfo(paths, "병합기동")
	if err != nil {
		t.Fatal(err)
	}
	for i, path := range paths {
		paths[i], err = filepath.EvalSymlinks(path)
		if err != nil {
			t.Fatal(err)
		}
	}
	if !slices.Equal(got.DataPaths, paths) || len(got.DataFiles) != 3 {
		t.Fatal("순서 있는 세 파일과 각 해시를 기록해야 한다")
	}
}
