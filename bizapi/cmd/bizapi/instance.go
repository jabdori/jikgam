package main

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"

	"github.com/jabdori/jikgam/bizapi/internal/server"
)

func fileSHA256(path string) (string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:]), nil
}

// instanceInfo는 전달된 기동 식별자와 현재 바이너리·데이터의 실제 바이트를 묶는다.
func instanceInfo(dataPaths []string, nonce string) (server.InstanceInfo, error) {
	if nonce == "" {
		nonce = rand.Text()
	}
	info := server.InstanceInfo{Nonce: nonce, DataFiles: make(map[string]string)}
	binary, err := os.Executable()
	if err != nil {
		return info, err
	}
	info.BinarySHA256, err = fileSHA256(binary)
	if err != nil {
		return info, err
	}
	for _, path := range dataPaths {
		absolute, err := filepath.Abs(path)
		if err != nil {
			return info, err
		}
		absolute, err = filepath.EvalSymlinks(absolute)
		if err != nil {
			return info, err
		}
		hash, err := fileSHA256(absolute)
		if err != nil {
			return info, err
		}
		info.DataFiles[absolute] = hash
		info.DataPaths = append(info.DataPaths, absolute)
	}
	return info, nil
}
