# 한국어 판별 모델 학습

공개 LLM(Qwen3.5-0.8B, 2B)에 Unsloth의 decision model 학습 경로로 Clef 형식 판별 헤드와 LoRA를 붙여 학습하고, 이 PC(RTX 3090 Ti)에서 Jev 호환 API로 서빙해 val에서 Jev·Clef-flash와 비교한다. 결정과 근거는 ADR 0012, 수치는 `results/training/`와 `results/decisions/val/`에 있다.

학습에는 train 분할(`data/build/train.jsonl`), 보강 사례(`data/build/train_aug.jsonl`), typed-decisions의 train 분할만 쓴다. 선별·보정은 val만 쓴다. test 파일은 어떤 스크립트도 열지 않는다(`jikgam/train/data.py`는 train·train_aug가 아닌 행이나 val·test 그룹의 행을 만나면 멈춘다).

## 1. 환경

2026-10-14에 실제로 쓴 것:

| 항목 | 값 |
|---|---|
| GPU | NVIDIA RTX 3090 Ti 24 GB, 드라이버 591.86(CUDA 13.1). 화면 출력과 다른 프로그램이 약 5 GB를 쓰고 있었다 |
| OS | Windows 11 Pro 22631, WSL2 Ubuntu 24.04.2(커널 6.18.40.1-microsoft-standard-WSL2) |
| 학습·서빙 | Unsloth Studio 설치 스크립트(`curl -fsSL https://unsloth.ai/install.sh \| sh`)가 만든 venv `~/.unsloth/studio/unsloth_studio` |
| 패키지 | Python 3.13.14, unsloth 2026.10.5, unsloth_zoo 2026.10.5, torch 2.13.0+cu130(CUDA 13.0), transformers 5.5.0, peft 0.18.1, trl 0.23.1, datasets 4.3.0, bitsandbytes 0.50.2, accelerate 1.15.0 |
| 평가 실행기 | Windows 쪽 저장소 환경(`uv sync`, Python 3.12). 로컬 서버는 WSL2의 localhost 전달로 `http://127.0.0.1:<포트>`에 접속한다 |

WSL2를 쓴 이유: Unsloth 문서가 Linux/WSL 설치를 먼저 안내하고, 설치 스크립트가 한 번에 끝났다(PyTorch cu130, GPU 인식 확인). Windows 설치는 시도하지 않았다.

WSL2 가상 디스크는 C: 드라이브에 있다. 모델 파일은 `~/jikgam-train/runs/<실행 이름>/`(어댑터 약 0.1 GB, 병합 모델 0.8B 약 1.7 GB)에 두고 저장소에는 넣지 않는다.

## 2. 설치

```bash
wsl -d Ubuntu-24.04
curl -fsSL https://unsloth.ai/install.sh -o /tmp/unsloth_install.sh   # 내용 확인 후 실행
UNSLOTH_SKIP_AUTOSTART=1 sh /tmp/unsloth_install.sh
source ~/.unsloth/studio/unsloth_studio/bin/activate
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

저장소 모듈은 `jikgam/train/run_wsl.sh <모듈> [인자]`로 실행한다. 이 스크립트는 venv를 켜고 `PYTHONPATH`를 저장소로 두며, Unsloth가 현재 폴더에 만드는 `unsloth_compiled_cache/`가 저장소에 생기지 않도록 실행 폴더(`$JIKGAM_RUNS`, 기본 `~/jikgam-train/runs`)에서 돌린다. WSL의 git은 Windows 쪽 worktree를 읽지 못해서, 학습 기록의 커밋은 `JIKGAM_GIT_COMMIT` 환경 변수로 넘긴다.

## 3. 데이터

### 3.1 빌드 파일 → Unsloth 행 (`jikgam/train/data.py`)

`FastDecisionModel.build_dataset`은 `data/build/train.jsonl`을 그대로 받는다(판단 461개 중 버린 것 0, 잘린 것 0, `results/training/dataset_check.json`). `gold`의 `{type, label}`과 noul의 `"true"`/`"false"`도 그대로 읽는다. 그래도 그대로 쓰지 않고 두 가지를 바꾼다.

- **질문 키:** Unsloth는 질문 ID를 프롬프트에 그대로 넣는다(`ID: applies:A-08`). 평가 실행기는 Workers AI 제약 때문에 모든 Jev 형식 백엔드에 `applies__A-08`로 보낸다(ADR 0007). 빌드 파일 그대로 학습하면 학습 입력과 서빙 입력이 달라진다. train 64건은 모두 applies 질문이 있어 64건 모두 입력 토큰이 달랐다. 그래서 학습 데이터의 키도 `jikgam.decision.types.wire_id`로 바꾼다. Unsloth 자체는 키의 `:`를 거부하지 않는다.
- **질문 문구:** 평가 실행기와 같은 함수(`jikgam.eval.questions.render`)로 질문 세트에서 다시 만든다. 기본은 엔진 질문 세트 `q-2026.10.14-mix`(ADR 0011)다. 처음 학습한 `q35-0.8b-a-v1` 하나만 빌드 파일 문구(`q-2026.10.12`)로 학습했다.

state는 JSON 문자열 그대로 둔다. Unsloth는 학습(`_parsed` 후 `render`)과 서빙(요청의 객체를 `render`) 모두 키 정렬, 공백 없음, `ensure_ascii=False`로 다시 직렬화하므로 한국어가 `\uXXXX`로 바뀌지 않고 두 경로의 입력이 같다.

### 3.2 typed-decisions

- `LocalLLaMA/typed-decisions`, 구성 `all`, 리비전 `c806f6c69d734f29436f25536138547031a673d6`(2026-10-13 수정, 2026-10-14 확인).
- 라이선스: 데이터셋 카드의 `license: apache-2.0`. 영어. train 1,200건(판단 6,000개, 업무 4종 × 300), test 400건(판단 2,000개). 저장 용량 약 3.8 MB.
- 정답은 약 4B급 교사 모델의 세 번 표본 평균(부드러운 분포)이다. 카드는 점수가 교사와의 일치도라고 밝힌다. score 질문이 있지만 이 저장소의 질문에는 없다.
- 학습에는 train만 쓴다. test는 학습 경로 점검(§6.3)에서 평가에만 썼다.

### 3.3 구성

| 구성 | 내용 | 한국어 반복 | 행 | 판단 |
|---|---|---|---|---|
| (a) | train 64건 | 1 | 64 | 461 |
| (b) | typed-decisions train | - | 1,200 | 6,000 |
| (c) | typed-decisions + train | 4 | 1,456 | 7,844 |
| (d) | typed-decisions + train + train_aug | 4 | 1,872 | 10,952 |
| (e) | train + train_aug (진단용) | 1 | 168 | 1,238 |

- 반복 4회는 (c)를 학습하기 전에 정했다. typed-decisions 1,200건 옆에서 한국어 64건이 묻히지 않게 하되(행 17.6%, 판단 23.5%. 입력 길이 중앙값이 한국어 2,402토큰, typed-decisions 876토큰이라 토큰으로는 중앙값 기준 추정 약 37%), 2 에폭 동안 한 사례를 8번 보는 정도로 제한했다. (d)는 사례당 노출을 (c)와 같게 같은 4회를 썼다(행 35.9%, 판단 45.2%). 반복 횟수는 val로 고르지 않았다.
- train_aug(104건, 대화 그룹 a001~a038, 판단 777개)는 다른 작성자가 val 실패 유형을 기준으로 val 사례를 보지 않고 새로 썼다(`scripts/build_dataset.py --aug`). 어떤 유형을 보강할지는 val 결과에서 나왔으므로, (d)·(e)의 val 수치에는 그만큼 val 정보가 섞여 있다.
- 섞는 순서는 시드 3407. 실제로 넣은 행은 `~/jikgam-train/runs/<실행>/train_rows.jsonl`에 남기고 sha256 앞 16자리를 학습 기록(`results/training/runs/<실행>.json`)에 적는다. 입력 파일 해시: `data/build/train.jsonl` `3a57f58bb3f6d705`, `data/build/train_aug.jsonl`은 학습 기록 참조.

## 4. 토큰화 확인 (`jikgam/train/tokens.py`, `results/training/tokenizer.json`)

학습 전에 Qwen3.5 토크나이저(0.8B·2B 같은 어휘, 248,077개)로 확인했다.

- train 앞 5건의 고객·상담사 발화(1,639자, 한글 음절 1,084자): 920토큰. 한글 음절당 0.85토큰, 전체 글자당 0.56토큰.
- 바이트 조각: 토큰 하나만 디코드했을 때 완성된 글자가 안 되는 토큰이 920개 중 27개(2.9%), 여러 토큰으로 쪼개진 한글 음절이 1,084자 중 12자(1.1%). 대부분의 음절은 한 토큰 안에 들어 있고, 드문 음절만 UTF-8 바이트 조각으로 나뉜다.
- 직렬화한 state 5건: 글자당 0.50토큰, `\uXXXX` 이스케이프 0건.
- Unsloth가 실제로 만드는 입력(시스템 프롬프트 + state + 질문·선택지) 길이, mix 문구: train 최대 3,324토큰(중앙값 2,402), val 최대 2,775, typed-decisions train 최대 1,222. q-2026.10.12 문구로는 train 최대 3,112, val 2,537.
- 그래서 `max_seq_length = 4096`(Unsloth Clef 기본값)으로 학습했다. 잘린 입력은 없다. 서빙과 선별은 Studio와 같게 16,384토큰까지 읽는다.

## 5. 학습 (`jikgam/train/train.py`)

```bash
bash jikgam/train/run_wsl.sh train --base unsloth/Qwen3.5-0.8B --composition c      # 이름: q35-0.8b-c-mix
bash jikgam/train/run_wsl.sh train --base unsloth/Qwen3.5-2B   --composition d      # 이름: q35-2b-d-mix
bash jikgam/train/run_wsl.sh train --base unsloth/Qwen3.5-0.8B --composition b --name q35-0.8b-b
bash jikgam/train/run_wsl.sh train --base unsloth/Qwen3.5-0.8B --composition a --questions v1 --name q35-0.8b-a-v1
```

기반 모델은 리비전을 고정한다: `unsloth/Qwen3.5-0.8B@23c69c53358a07516b5827588b3fdb12ae78fd65`, `unsloth/Qwen3.5-2B@fffbc0d8711cf87df6d1f97938c5e50ef94aadc7`(둘 다 Apache-2.0). 일반 LLM이라 Unsloth가 Clef 형식 판별 헤드(폭 512, 라우팅 2층 + 디코더 4층)를 새로 붙인다.

하이퍼파라미터는 Unsloth 문서의 "Recommended settings"를 따랐고 구성마다 바꾸지 않았다.

| 항목 | 값 |
|---|---|
| 양자화 | 4비트 LoRA(QLoRA, nf4). 병합 저장 때 4비트 기반을 풀어 16비트로 합친다 |
| LoRA | r 16, alpha 16, dropout 0, 언어 모델 선형층(비전 탑 제외, Unsloth 기본) |
| 학습률 | 2e-4, 헤드 1e-4(Unsloth 기본), cosine, warmup = 전체 단계의 4%(문서 예시 10/240과 같은 비율), weight decay 0.01 |
| 배치 | 장치당 2 × 누적 4 = 8 |
| 에폭 | (b)·(c)·(d) 2, (a)·(e) 4(문서: 데이터가 적으면 3~4) |
| 정밀도 | bf16, 옵티마이저 adamw_torch_fused |
| 시드 | 3407(섞기, LoRA 초기화, 헤드 초기화, Trainer) |

문서의 코드 예시는 학습률 2e-5, `max_steps = 240`이고 같은 문서의 권장값 절은 2e-4다. 권장값 절을 따랐다. 문서 첫 표의 벤치마크는 LoRA r=64, 1 에폭이라고 적혀 있어 권장값(r 16, 2 에폭)과도 다르다.

평가 데이터 없이 학습한다(`eval_strategy="no"`). 체크포인트 선택은 하지 않고 마지막 상태를 저장한다. 어댑터(`adapter/`)와 16비트 병합 모델(`merged/`)을 저장한다.

## 6. val 선별과 보정 (`jikgam/train/screen.py`)

```bash
bash jikgam/train/run_wsl.sh screen --run q35-0.8b-c-mix                 # 평가 문구 mix
bash jikgam/train/run_wsl.sh screen --run q35-0.8b-a-v1 --questions v1
uv run python -m jikgam.train.screening_table                            # results/training/val-screening/README.md
```

- 병합 모델을 Studio Decision API와 같은 방식으로 올린다(`FastDecisionModel.from_pretrained(max_seq_length=16384)`, 메모리가 충분하면 16비트). 답은 Studio가 쓰는 함수와 같은 `FastDecisionModel.predict`(`_clef_decide`)로 낸다. 같은 사례를 서버에 HTTP로 보낸 답과 확률이 소수 넷째 자리까지 같았다.
- 지표는 저장소 정의(`jikgam.eval.metrics`, `label_and_prob`)로 계산한다.
- 보정: 보정 전 정확도·ECE가 주 지표다. 보정은 `FastDecisionModel.calibrate`(온도 조정, 예측 라벨은 바뀌지 않음)를 val에 맞춘 것이라 표본 내 값이다. 두 값을 적는다. (1) val 전체로 맞춘 온도로 다시 예측해 저장소 정의로 계산한 ECE, (2) Unsloth가 돌려주는 값(val 사례를 반씩 나눠 서로의 절반으로 맞춘 온도, Unsloth의 ECE 정의). 보정한 모델은 저장하지 않고, 서빙하는 병합 모델은 보정 전이다.
- val을 쓴 모든 선별 구성을 `results/training/val-screening/README.md`에 빠짐없이 적는다.

<!-- 이하 결과에 따라 채운다 -->
