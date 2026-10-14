# 한국어 판별 모델 학습

> 이 문서는 한국어 판별 모델 학습의 환경, 데이터 구성, 학습·선별·서빙 절차와 val 측정 결과를 다룬다. 학습을 재현하거나 학습한 모델을 서빙하려는 사람이 읽으면 된다.
> 기반 모델과 데이터 구성을 고른 이유는 [결정 기록 0012](adr/0012-한국어-판별-모델-기반과-데이터-구성.md)에 있다.

공개 LLM(Qwen3.5-0.8B, 2B)에 Unsloth의 decision model 학습 경로로 Clef 형식 판별 헤드와 LoRA를 붙여 학습하고, 이 PC(RTX 3090 Ti)에서 Jev 호환 API로 서빙해 val에서 Jev·Clef-flash와 비교한다. 결정과 근거는 ADR 0012, 수치는 `results/training/`와 `results/decisions/val/`에 있다.

학습에는 train 분할(`data/build/train.jsonl`), 보강 사례(`data/build/train_aug.jsonl`), typed-decisions의 train 분할만 쓴다. 선별·보정은 val만 쓴다. test 파일은 어떤 스크립트도 열지 않는다(`jikgam/train/data.py`는 train·train_aug가 아닌 행이나 val·test 그룹의 행을 만나면 멈춘다).

## 1. 환경

2026-10-14에 실제로 쓴 것:

| 항목 | 값 |
|---|---|
| GPU | NVIDIA RTX 3090 Ti 24 GB, 드라이버 591.86(CUDA 13.1). 화면 출력과 다른 프로그램이 약 5 GB를 쓰고 있었다. 07:22에 Windows Update가 드라이버를 610.88로 바꿨다(아래) |
| OS | Windows 11 Pro 22631, WSL2 Ubuntu 24.04.2(커널 6.18.40.1-microsoft-standard-WSL2) |
| 학습·서빙 | Unsloth Studio 설치 스크립트(`curl -fsSL https://unsloth.ai/install.sh \| sh`)가 만든 venv `~/.unsloth/studio/unsloth_studio` |
| 패키지 | Python 3.13.14, unsloth 2026.10.5, unsloth_zoo 2026.10.5, torch 2.13.0+cu130(CUDA 13.0), transformers 5.5.0, peft 0.18.1, trl 0.23.1, datasets 4.3.0, bitsandbytes 0.50.2, accelerate 1.15.0 |
| 평가 실행기 | Windows 쪽 저장소 환경(`uv sync`, Python 3.12). 로컬 서버는 WSL2의 localhost 전달로 `http://127.0.0.1:<포트>`에 접속한다 |

드라이버 변경: 2026-10-14 07:21~07:22 Windows Update가 "NVIDIA Display Driver Update (32.0.16.1088)"를 설치해 드라이버가 591.86에서 610.88로 바뀌었다. 그 시각 학습 중이던 2B (d) 시드 3408이 `CUDA error: unknown error`로 끝났다.

610.88에서 시작한 2B 학습 4번 중 2번이 GPU를 쓰지 않은 채 멈췄고(07:32:20, 08:24:50), 두 번 모두 같은 시각에 시스템 로그에 `nvlddmkm` 이벤트 153이 남았다. 이날 0시 이후 시스템 로그의 `nvlddmkm` 이벤트는 이 두 개뿐이고, 591.86에서 끝까지 돈 학습 13번 동안에는 없었다. 첫 번째는 `wsl --shutdown`(이 PC의 모든 WSL 배포판을 멈춘다) 뒤에, 두 번째는 해당 프로세스만 끝낸 뒤 다시 돌렸고 둘 다 끝까지 돌았다.

§6.1·§8·§9의 모든 수치와 §10의 처음 여섯 실행은 591.86에서, 2B 시드 3408·3409와 §10의 `20261014T090432`는 610.88에서 쟀다. 0.8B 시드 3408·3409는 06:43~07:17에 591.86에서 돌았다.

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

저장소 모듈은 `jikgam/train/run_wsl.sh <모듈> [인자]`로 실행한다. 이 스크립트는 venv를 켜고 `PYTHONPATH`를 저장소로 두며, Unsloth가 현재 폴더에 만드는 `unsloth_compiled_cache/`가 저장소에 생기지 않도록 실행 폴더(`$JIKGAM_RUNS`, 기본 `~/jikgam-train/runs`)에서 돌린다.

WSL의 git은 Windows 쪽 worktree를 읽지 못해서, 학습 기록의 커밋은 `JIKGAM_GIT_COMMIT` 환경 변수로 넘긴다.

## 3. 데이터

### 3.1 빌드 파일 → Unsloth 행 (`jikgam/train/data.py`)

`FastDecisionModel.build_dataset`은 `data/build/train.jsonl`을 그대로 받는다(판단 461개 중 버린 것 0, 잘린 것 0, `results/training/dataset_check.json`). `gold`의 `{type, label}`과 noul의 `"true"`/`"false"`도 그대로 읽는다. 그래도 그대로 쓰지 않고 두 가지를 바꾼다.

- **질문 키:** Unsloth는 질문 ID를 프롬프트에 그대로 넣는다(`ID: applies:A-08`). 평가 실행기는 Workers AI 제약 때문에 모든 Jev 형식 백엔드에 `applies__A-08`로 보낸다(ADR 0007).

  빌드 파일 그대로 학습하면 학습 입력과 서빙 입력이 달라진다. train 64건은 모두 applies 질문이 있어 64건 모두 입력 토큰이 달랐다. 그래서 학습 데이터의 키도 `jikgam.decision.types.wire_id`로 바꾼다. Unsloth 자체는 키의 `:`를 거부하지 않는다.
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
- 섞는 순서는 시드 3407. 실제로 넣은 행은 `~/jikgam-train/runs/<실행>/train_rows.jsonl`에 남기고 sha256 앞 16자리를 학습 기록(`results/training/runs/<실행>.json`)에 적는다.

  입력 파일 해시: `data/build/train.jsonl` `3a57f58bb3f6d705`, `data/build/train_aug.jsonl` `f64e713e361676ef`. 구성별로 실제로 넣은 행의 해시: (a) mix `07d064d9ecf5a7a6`, (a) q-2026.10.12 `c891fc460653bcaf`, (b) `db80b2cd3bba5763`, (c) `9d5aeb8548e42b01`, (d) `b4147322c6fb2190`, (e) `d89f0afa924850e6`(0.8B와 2B가 같은 행을 썼다).

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
- 보정: 보정 전 정확도·ECE가 주 지표다. 보정은 `FastDecisionModel.calibrate`(온도 조정. 대부분 예측 라벨이 그대로지만 0.8B (b)에서는 정답 수가 81개에서 80개로 바뀌었다)를 val에 맞춘 것이라 표본 내 값이다.

  두 값을 적는다. (1) val 전체로 맞춘 온도로 다시 예측해 저장소 정의로 계산한 ECE, (2) Unsloth가 돌려주는 값(val 사례를 반씩 나눠 서로의 절반으로 맞춘 온도, Unsloth의 ECE 정의). 보정한 모델은 저장하지 않고, 서빙하는 병합 모델은 보정 전이다.
- val을 쓴 모든 선별 구성을 `results/training/val-screening/README.md`에 빠짐없이 적는다.

### 6.1 선별 결과(2026-10-14, val 판단 152개)

이 표의 val 평가 구성은 11개다(보정 변형은 같은 모델의 열로 적고 따로 세지 않았다). §11의 시드 실행 4개를 더하면 val로 평가한 구성은 모두 15개다. 하이퍼파라미터·반복 횟수·에폭은 val을 보고 바꾸지 않았다. 구성 (d)·(e)는 val 실패 유형을 기준으로 쓴 train_aug를 쓰므로 val 정보가 간접적으로 섞여 있다.

| 선별 | 학습 문구 | 정확도 | ECE | 보정 후 ECE(표본 내) | Unsloth 교차 적합 ECE | 학습 분 | 최대 GPU GB |
|---|---|---|---|---|---|---|---|
| 0.8B (a) | q-2026.10.12 | 69.7% (106/152) | 0.061 | 0.045 | 0.048 | 2.6 | 2.42 |
| 0.8B (a) | mix | 64.5% (98/152) | 0.082 | 0.080 | 0.056 | 2.2 | 2.69 |
| 0.8B (b) | (한국어 없음) | 53.3% (81/152) | 0.136 | 0.020 | 0.075 | 8.4* | 2.03 |
| 0.8B (c) | mix | 73.7% (112/152) | 0.160 | 0.069 | 0.087 | 10.4 | 2.54 |
| 0.8B (d) | mix | **83.6% (127/152)** | 0.090 | 0.049 | 0.038 | 13.3 | 2.54 |
| 0.8B (e) | mix | 67.8% (103/152) | 0.103 | 0.077 | 0.074 | 2.9 | 2.56 |
| 2B (a) | mix | 71.7% (109/152) | 0.044 | 0.040 | 0.050 | 2.1 | 4.41 |
| 2B (b) | (한국어 없음) | 55.3% (84/152) | 0.091 | 0.068 | 0.091 | 8.7 | 3.77 |
| 2B (c) | mix | 83.6% (127/152) | 0.121 | 0.068 | 0.049 | 12.3 | 4.35 |
| 2B (d) | mix | **88.2% (134/152)** | 0.077 | 0.015 | 0.051 | 18.2 | 4.37 |
| 2B (e) | mix | 80.3% (122/152) | 0.046 | 0.082 | 0.106 | 5.0 | 4.32 |

평가 문구는 학습 문구와 같다(0.8B (a) q-2026.10.12 행만 q-2026.10.12로 평가). \* 0.8B (b) 학습 중 약 3분 동안 서빙 점검이 같은 GPU를 썼다. 학습 시간은 모두 같은 GPU에서 다른 프로그램(약 5 GB)과 함께 쟀다. 학습 GPU 시간 합계 86.1분.

- typed-decisions만(b)은 두 크기 모두 q-2026.10.12 기준 다수 라벨 기준선(60.5%)보다 낮다. applies에서 true를 0.8B 9.0%, 2B 20.9%만 예측했다(정답 true 56.7%).
- train 64건만(a)은 두 크기 모두 sufficient를 모두 true, condition_changed를 모두 false로 냈다(균형 정확도 50%).
- 둘을 섞으면(c) 둘 다 혼자일 때보다 높고, 보강 사례를 더하면(d) 더 높다. typed-decisions를 빼면(e) (d)보다 0.8B 24개, 2B 12개 낮다.
- 질문 세트 변경 전 문구로 학습·평가한 0.8B (a)가 mix보다 8개 높았다. 학습 사례 64건에서의 차이라 문구 효과인지 학습 변동인지 가리지 않았다.

### 6.2 크기별 최종 후보

각 크기에서 val 정확도가 가장 높은 (d)를 골랐다. 고른 기준은 보정 전 val 정확도 하나다. 후보는 시드 3407로 학습한 모델이다. 다른 시드(§11)는 선택 뒤에 변동을 보려고 돌렸고, 그 결과로 후보를 바꾸지 않았다.

### 6.3 학습 경로 점검(typed-decisions 공개 test)

`jikgam/train/td_eval.py`로 (b) 모델을 typed-decisions의 공개 test(400건, 판단 2,000개)에 평가했다. 이 저장소의 test 분할과 무관하며 선택에 쓰지 않았다.

Unsloth `evaluate` 정의로 0.8B 77.0%, 2B 78.8%였다(`results/training/typed-decisions-test/`). Unsloth 문서의 typed-decisions 수치(0.8B 73%, 2B 78%, 다른 데이터 12종을 더한 학습)와 같은 범위라 학습 경로가 정상으로 돈다고 봤다.

## 7. 서빙 (`jikgam/train/serve.py`)

```bash
bash jikgam/train/run_wsl.sh serve --run q35-0.8b-d-mix --port 8888
bash jikgam/train/run_wsl.sh serve --run q35-2b-d-mix   --port 8889
bash jikgam/train/run_wsl.sh serve --stop --port 8888
```

- `UNSLOTH_SYSTEMONE_MODEL=<병합 폴더> UNSLOTH_SYSTEMONE_DEVICE=gpu unsloth studio -H 127.0.0.1 -p <포트>`로 띄운다. 엔드포인트는 `POST /v1/systemone`(Jev 형식).
- Decision API는 환경 변수만으로 켜지지 않는다. 관리자 설정 `systemone_enabled`가 필요해서, 스크립트가 관리자 로그인(`/api/auth/login`) 뒤 `PUT /api/settings/systemone {"enabled": true, "backend": "pytorch"}`를 보내고 API 키를 발급한다(`POST /api/auth/api-keys`).

  첫 실행의 관리자 비밀번호는 `UNSLOTH_STUDIO_PASSWORD`로 한 번만 정할 수 있고, 다시 주면 Studio가 시작을 거부한다. 비밀번호와 키는 WSL의 `~/jikgam-train/secrets/`(권한 600)에 두고, 평가 실행기는 저장소 `.env`(gitignore)의 `UNSLOTH_API_KEY`로 읽는다.
- 병합 폴더에 GGUF가 없어 PyTorch로 서빙된다(응답 헤더 `x-unsloth-decision-backend: pytorch`). 메모리가 충분하면 16비트로 올린다. 모델을 처음 올리는 데 31~50초 걸렸다.
- 요청 모델 이름은 `laya-local`을 쓴다. 별칭(`laya`, `default`, `jev-latest`)은 설정 화면에서 고른 모델로 바뀔 수 있지만 `laya-local`은 환경 변수로 띄운 폴더에만 연결된다.

  응답의 `model`도 항상 `laya-local`이라, 설정(`config/decision_models.toml`)의 `served`와 `weights_sha256_16`으로 가중치를 식별한다: 0.8B `94f8e23735f64400`, 2B `3f9dcfe6df110822`.
- 한 GPU에 두 Studio를 포트만 달리해 띄웠다(같은 `~/.unsloth/studio`). 같은 val 사례에 대한 서버 응답 확률은 선별 때 같은 프로세스에서 낸 확률과 소수 넷째 자리까지 같았다.
- Windows의 실행기는 WSL2 localhost 전달로 접속한다. WSL 세션이 모두 끝나면 서버가 같이 내려갈 수 있어, 측정하는 동안 WSL 세션 하나를 열어 두었다.
- 2B는 지연 때문에 나중에 Studio 대신 같은 프로세스 서버(`jikgam/train/serve_direct.py`, 설정 `q35-2b-ko-direct`)로 서빙하기로 했다(§10). 가중치와 답은 같다.

## 8. 실행기 비교 (val, 질문 세트 q-2026.10.14-mix)

```bash
uv run python -m jikgam.eval.run_decisions --split val --questions data/question_sets/q-2026.10.14-mix.json \
    --backends q35-0.8b-ko,q35-2b-ko,jev,clef-flash
uv run python -m jikgam.eval.report results/decisions/val/20261014T060432
uv run python -m jikgam.train.tag_compare results/decisions/val/20261014T060432 --models q35-0.8b-ko,q35-2b-ko
```

실행 `20261014T060432`(코드 `b206f6a2e95b`, 5회 반복, 동시 실행 1, 서울, Cloudflare ICN):

| 백엔드 | 서빙 위치 | 정확도 | ECE | p50 ms | p95 ms | 요청당 USD | 회차 간 라벨 일치 |
|---|---|---|---|---|---|---|---|
| q35-0.8b-ko | 이 PC, WSL2 localhost | 83.6% (127/152) | 0.090 | 128 | 197 | 참고 0.0000193 | 100.0% |
| q35-2b-ko | 이 PC, WSL2 localhost | 88.2% (134/152) | 0.077 | 220 | 448 | 참고 0.0000358 | 100.0% |
| jev | TypeSafe API | 92.4% (140~141/152) | 0.147 | 182 | 242 | 0.000120 | 92.8% |
| clef-flash | Workers AI | 73.7% (112/152) | 0.080 | 686 | 981 | 0.000089 | 100.0% |

- 로컬 모델의 정확도는 선별 결과와 같았다(127, 134).
- 질문 유형별 2B 대 Jev: applies 58 대 61~64, sufficient 18 대 17~18, next_info 8 대 8~10, action 16 대 16, intent 15 대 15~16, condition_changed 16 대 16~17(판단 수 67, 20, 10, 18, 17, 17).
- 지연 점검 실행 `20261014T060738`(로컬 두 모델만, 원격 호출 없이 이어서 호출): 0.8B p50 128 / p95 180ms, 2B p50 204 / p95 348ms. 원격 백엔드와 번갈아 부를 때 2B의 꼬리가 더 길었다(p95 448ms). 같은 프로세스에서 잰 predict 시간은 0.8B p95 101ms, 2B p95 179ms라 HTTP·Studio 작업 프로세스 경유에 수십 ms가 더 든다. GPU는 화면 출력·다른 프로그램과 함께 썼다.

### 8.1 §7.2 기준에 비춘 val 결과(판정은 test에서만 한다)

- 정확도(Jev보다 5%p 넘게 낮지 않음): 2B는 4.2%p 낮아 범위 안, 0.8B는 8.8%p 낮아 범위 밖.
- 지연(이 PC p95 ≤ 200ms): 0.8B 197ms(점검 실행 180ms)로 범위 안, 2B 448ms(점검 실행 348ms)로 범위 밖.
- 두 조건을 함께 만족하는 모델은 val에서 없다. 이 결과는 val로 고른 모델의 val 수치이고, (d)의 보강 사례는 val 실패 유형을 보고 썼으므로 test에서는 더 낮을 수 있다.
- 갱신(2026-10-14, §10·§11): 2B 후보를 같은 프로세스 서버로 서빙하면 로컬 실행 세 번의 p95가 175, 173, 181ms였고 답은 위 실행과 같다(152개 판단 × 5회 모두 같은 라벨, 확률 차 0).

  그래서 2B 후보(시드 3407)는 val에서 두 조건을 함께 만족한다: 정확도 Jev보다 4.2%p 낮음(`20261014T060432`), p95 200ms 이하(`20261014T064204`, `20261014T064235`, `20261014T090432`).

  단, 지연은 로컬 백엔드만 부른 조건에서 쟀고, 같은 구성을 다른 시드로 학습한 두 모델(128, 126개)은 정확도 조건(5%p, 이 실행 기준 133개 이상)을 만족하지 못했다. 판정은 §8 절차의 test 평가로 한다.

### 8.2 실패 유형 태그별 비교

판정 규칙: 회차별 정답 수 범위가 겹치면 같음. 판단 수가 7~77개라 1~2개 차이도 나아짐·나빠짐으로 나온다.

| 태그 | 0.8B | 2B | clef-flash | jev | 0.8B 대 clef-flash | 0.8B 대 jev | 2B 대 clef-flash | 2B 대 jev |
|---|---|---|---|---|---|---|---|---|
| colloquial (7건) | 41/49 | 43/49 | 38/49 | 44~46/49 | 나아짐 | 나빠짐 | 나아짐 | 나빠짐 |
| correction (4건) | 25/31 | 27/31 | 26/31 | 28~31/31 | 나빠짐 | 나빠짐 | 나아짐 | 나빠짐 |
| ellipsis (1건) | 7/7 | 4/7 | 3/7 | 4~5/7 | 나아짐 | 나아짐 | 나아짐 | 같음 |
| exception (10건) | 61/72 | 65/72 | 52/72 | 68~69/72 | 나아짐 | 나빠짐 | 나아짐 | 나빠짐 |
| handoff (2건) | 10/14 | 12/14 | 8/14 | 13~14/14 | 나아짐 | 나빠짐 | 나아짐 | 나빠짐 |
| insufficient_info (3건) | 14/18 | 12/18 | 9/18 | 14~15/18 | 나아짐 | 같음 | 나아짐 | 나빠짐 |
| missing_evidence (1건) | 4/7 | 5/7 | 5/7 | 6~7/7 | 나빠짐 | 나빠짐 | 같음 | 나빠짐 |
| multi_evidence (10건) | 68/77 | 72/77 | 61/77 | 73~74/77 | 나아짐 | 나빠짐 | 나아짐 | 나빠짐 |
| negation (3건) | 18/24 | 23/24 | 20/24 | 21~23/24 | 나빠짐 | 나빠짐 | 나아짐 | 같음 |
| number_date (5건) | 30/38 | 35/38 | 32/38 | 35~37/38 | 나빠짐 | 나빠짐 | 나아짐 | 같음 |
| partial_stt (1건) | 6/6 | 5/6 | 4/6 | 6/6 | 나아짐 | 같음 | 나아짐 | 나빠짐 |
| similar_article (5건) | 32/37 | 32/37 | 26/37 | 30~32/37 | 나아짐 | 같음 | 나아짐 | 같음 |
| stale_suggestion (3건) | 21/24 | 23/24 | 21/24 | 23~24/24 | 같음 | 나빠짐 | 나아짐 | 같음 |
| stt_error (2건) | 11/12 | 10/12 | 7/12 | 12/12 | 나아짐 | 나빠짐 | 나아짐 | 나빠짐 |

- 2B는 clef-flash보다 14개 태그 중 13개에서 나아졌고 missing_evidence는 같았다.

  Jev보다 나아진 태그는 없다. 같음 5개(ellipsis, negation, number_date, similar_article, stale_suggestion), 나빠짐 9개(colloquial, correction, exception, handoff, insufficient_info, missing_evidence, multi_evidence, partial_stt, stt_error).
- 0.8B는 clef-flash보다 나아짐 9개, 나빠짐 4개(correction, missing_evidence, negation, number_date), 같음 1개(stale_suggestion). Jev보다는 ellipsis 하나(판단 7개, 사례 1건)만 나아졌고 같음 3개(insufficient_info, partial_stt, similar_article), 나빠짐 10개.

## 9. 참고 비용 (§2.6)

`jikgam/train/gpu_cost.py`, `results/training/gpu-cost/`. 단가는 RunPod RTX 3090(3090 Ti는 목록에 없음) Secure Cloud $0.50/시간, Community Cloud $0.22/시간(https://www.runpod.io/pricing, 페이지 수정일 2026-10-08, 2026-10-14 확인).

| 실행 | 백엔드 | 점유 시간 기준(지연 합) | 요청당 USD, Secure | 요청당 USD, Community | 측정 구간 기준 상한, Secure |
|---|---|---|---|---|---|
| 20261014T060432 | q35-0.8b-ko | 14.6초 / 105요청 | 0.0000193 | 0.0000085 | 0.000188 |
| 20261014T060432 | q35-2b-ko | 27.1초 / 105요청 | 0.0000358 | 0.0000158 | 0.000188 |

측정 구간 기준은 실행 전체 142초를 각 백엔드에 다 붙인 값이라(두 모델이 한 GPU를 같이 썼고, 원격 호출을 기다리는 동안에도 모델을 올려 두었다) 상한이다. 전기료와 장비비는 넣지 않았다.

두 해석의 결론은 갈린다. 처리 시간 합 기준으로는 Jev(요청당 0.000120달러)와 Clef-flash(0.000089달러)보다 싸고, 측정 구간 기준으로는 두 모델 모두 0.000188달러로 더 비싸다. 그래서 둘 다 적는다(`docs/evaluation.md` §2.6 보완). 주장 3의 성공 수치에는 비용 조건이 없다.

## 10. 2B 서빙 방식과 지연

2B (d)의 가중치는 그대로 두고 서빙 방식만 바꿔 쟀다. 모든 측정은 로컬 백엔드 하나만 부르는 실행기 실행(원격 호출 없음, 5회 반복, 동시 실행 1, 예열 후, Windows 클라이언트 → WSL2 localhost)이고, 한 번에 서버 하나만 GPU에 올렸다.

서빙한 답은 선별 예측(`results/training/val-screening/q35-2b-d-mix__q-mix/predictions.jsonl`)과 판단마다 비교했다(`jikgam/train/compare_served.py`, 표는 `jikgam/train/latency_table.py`).

| 방식 | 실행 | p50 ms | p95 ms | 최대 ms | 정답(회차 평균)/152 | 선별 예측과 라벨 일치 | 확률 차 중앙값 / 최대 |
|---|---|---|---|---|---|---|---|
| Studio PyTorch 16비트 | `20261014T063542` | 188 | 217 | 276 | 134.0 | 760/760 | 0 / 0 |
| Studio llama.cpp GGUF BF16 | `20261014T063644` | 257 | 507 | 3,472 | 135.0 | 755/760 | 0.0001 / 0.1148 |
| Studio llama.cpp GGUF Q8_0 | `20261014T063752` | 208 | 485 | 2,747 | 135.0 | 745/760 | 0.0002 / 0.2679 |
| Studio llama.cpp GGUF Q4_K_M | `20261014T063846` | 212 | 486 | 2,591 | 132.0 | 695/760 | 0.0023 / 0.8597 |
| 같은 프로세스 서버(`serve_direct.py`) | `20261014T064204` | 146 | 175 | 215 | 134.0 | 760/760 | 0 / 0 |
| 같은 프로세스 서버, 반복 | `20261014T064235` | 145 | 173 | 196 | 134.0 | 760/760 | 0 / 0 |
| 같은 프로세스 서버, 드라이버 610.88 | `20261014T090432` | 146 | 181 | 395 | 134.0 | 760/760 | 0 / 0 |

참고로 앞선 조건의 Studio PyTorch: 0.8B 서버와 함께 올리고 원격 백엔드와 번갈아 부른 실행 `20261014T060432` p50 220 / p95 448ms, 0.8B와 번갈아 부른 실행 `20261014T060738` p50 204 / p95 348ms. 한 GPU에 모델 하나만 올리고 그것만 부르면 꼬리가 줄었다.

- **llama.cpp(GGUF):** Studio의 Clef GGUF 경로(`jikgam/train/export_gguf.py`로 내보내고 `serve.py --backend llama.cpp`로 서빙, unslothai/llama.cpp b11541). 세 양자화 모두 회차 0에서 5개 호출이 1.7~3.5초 걸렸다(1초 넘는 호출은 모두 회차 0).

  회차 1~4의 중앙값도 BF16 244~270ms, Q8_0 196~216ms로 Studio PyTorch의 회차별 중앙값(187~197ms)보다 빠르지 않았다.

  라벨이 선별 예측과 다른 판단은 BF16 1개(g033-1 intent, 확률 0.4979 → 0.5059로 정답 쪽으로 바뀌어 5회 모두 다름), Q8_0 3개(회차 합 15개), Q4_K_M 13개(회차 합 65개)였다.
- **GPU 클럭 고정:** `nvidia-smi -lgc`가 "The current user does not have permission to change clocks"로 거부됐다(관리자 권한 필요). 측정하지 않았다. 유휴 상태는 P8, 210MHz였다.
- **요청 경로:** Studio 로그의 서버 처리 시간(`process_time_ms`, 실행기 예열 요청 1개 포함 106~107건)은 PyTorch p50 172 / p95 205ms였다. 클라이언트 지연(188/217)과의 차이인 약 15ms가 Windows → WSL2 전달 몫이다. 선별 때 같은 프로세스에서 잰 `predict`는 p50 132 / p95 179ms라, Studio의 요청 경로(설정·키 확인, 스레드 풀, 별도 작업 프로세스와의 파이프)에 약 40ms가 든다.
- **같은 프로세스 서버:** `jikgam/train/serve_direct.py`는 Studio와 같은 방식으로 병합 모델을 올리고(16비트, 최대 16,384토큰) Studio의 작업 프로세스가 부르는 함수(`unsloth.models.decision._clef_decide`)를 서버 프로세스에서 직접 부른다. 응답 형식도 Studio와 같다. 답은 선별 예측과 760개 모두 같았고 확률 차는 0이었다.

선택: 2B는 같은 프로세스 서버로 서빙한다(설정 `q35-2b-ko-direct`). 로컬 실행 세 번에서 p95 175ms, 173ms(드라이버 591.86), 181ms(610.88)였다. 610.88에서는 서버를 띄운 뒤 첫 요청이 11.0초 걸렸다(591.86에서는 4.1초).

```bash
bash jikgam/train/run_wsl.sh serve_direct --run q35-2b-d-mix --port 8893   # 앞에서 실행. 종료는 Ctrl+C
uv run python -m jikgam.eval.run_decisions --split val --questions data/question_sets/q-2026.10.14-mix.json \
    --backends q35-2b-ko-direct
```

한계:

- 이 측정은 로컬 백엔드만 이어서 부른 조건이다. 원격 백엔드와 번갈아 부를 때는 Studio PyTorch에서 꼬리가 길어졌다(위 참고). 같은 프로세스 서버를 그 조건에서는 재지 않았다.
- 서버를 띄운 뒤 첫 요청은 4.1초 걸렸다(점검 요청에서 잼). 실행기의 예열 요청이 이를 흡수한다. 엔진도 세션 전에 예열해야 한다.
- Studio의 관리 기능(설정, 키 관리, 모델 교체, 유휴 시 내리기)은 쓰지 않는다. 키는 Studio가 발급한 키 파일을 그대로 확인한다.

## 11. 시드 변동

(d) 구성을 같은 하이퍼파라미터로 시드만 3408, 3409로 바꿔 크기마다 두 번 더 학습하고 val로 선별했다(시드는 학습 전에 정함). 시드는 섞는 순서, LoRA·헤드 초기화, Trainer에 모두 쓰인다. 이 4개도 val 평가 구성으로 센다(누적 15개).

```bash
bash jikgam/train/run_wsl.sh train --base unsloth/Qwen3.5-2B --composition d --seed 3408 --name q35-2b-d-mix-s3408
bash jikgam/train/run_wsl.sh screen --run q35-2b-d-mix-s3408
```

| 기반 모델 | 시드 3407(후보) | 시드 3408 | 시드 3409 | 범위 | 평균 |
|---|---|---|---|---|---|
| 0.8B | 127/152 (83.6%), ECE 0.090 | 124 (81.6%), ECE 0.106 | 123 (80.9%), ECE 0.125 | 123~127 | 82.0% |
| 2B | 134/152 (88.2%), ECE 0.077 | 128 (84.2%), ECE 0.137 | 126 (82.9%), ECE 0.128 | 126~134 | 85.1% |

- 후보로 고른 시드 3407이 두 크기 모두 세 시드 중 가장 높았다. 2B는 시드에 따라 8개(5.3%p) 차이가 났다.
- 모든 시드가 Clef-flash(112개)보다 높고 Jev(140~141개)보다 낮다.
- 질문 유형별로 2B 세 시드의 차이가 가장 큰 곳은 applies(55~58/67)와 action(13~16/18)이다(`results/training/val-screening/README.md`).
- 2B 시드 3408·3409는 드라이버 610.88에서 학습했다(§1). 학습 시간 18.7분·18.2분으로 591.86에서의 시드 3407(18.2분)과 비슷했다.

## 12. 남은 문제

- (d)의 이득에는 val 실패 유형을 보고 쓴 보강 사례의 선택 효과가 섞여 있다. 고정 절차의 test 평가 전에는 일반화 정도를 알 수 없다.
- 시드 변동이 크다(§11). 2B 후보(134개)는 세 시드 중 가장 높았고, 다른 두 시드(128, 126개)는 Jev보다 5%p 넘게 낮다. val 15개 구성 중 최고를 고른 값이라 test에서는 더 낮을 수 있다.
- 지연 200ms 이하는 로컬 백엔드만 이어서 부른 조건에서만 쟀다. 원격 백엔드와 번갈아 부를 때(엔진에서 Jev와 함께 쓰는 경우) 같은 프로세스 서버의 꼬리는 재지 않았다. GPU 클럭 고정은 관리자 권한이 없어 시도하지 못했다.
- 드라이버 610.88에서 2B 학습이 두 번 GPU 오류(`nvlddmkm` 153)로 멈췄다(§1). 드라이버 업데이트 뒤 재부팅은 하지 않았다.
- 디스크: 최종 후보가 아닌 9개 실행의 병합 모델(26.1 GB), GGUF 서빙 폴더 3개(8.1 GB), 시드 실행 4개의 병합 모델(12.2 GB)을 지웠다. 어댑터는 모두 남겨 다시 병합할 수 있다(`model.save_pretrained_merged`). 남은 것: `~/jikgam-train` 8.0 GB(병합 모델 2개 6.1 GB, 어댑터 15개), Unsloth 설치 11 GB, Hugging Face 캐시 6 GB.
- `q35-0.8b-a-v1`은 학습 스크립트를 처음 커밋하기 전에 돌려 학습 기록의 커밋이 비어 있다(학습 후 같은 코드를 `32efcf6`으로 커밋, 데이터와 하이퍼파라미터는 기록에 있다).
