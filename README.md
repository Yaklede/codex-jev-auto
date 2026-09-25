# Jev Router

Codex 작업 요청을 로컬 Open Jev로 평가해 **GPT-6 Luna, GPT-5.6 Terra, GPT-6 Sol, GPT-6 Astra**와 `low`, `medium`, `high`, `xhigh`, `max`의 최대 20개 조합 중 하나를 선택하는 로컬 플러그인입니다. 현재 Codex 계정의 모델 목록에서 실제 지원 조합만 평가합니다. Astra가 선택되면 사용자 승인 후 읽기 전용 계획만 작성하고, Sol이 그 계획을 참고해 구현합니다.

1차 범위는 모델·추론 강도 선택, 실행, Astra 계획 전 직접 확인, 결과 기록입니다. 백엔드 코드 복잡도 관리와 프론트엔드 디자인 평가 루프는 [개발 계획](docs/plan.md)에 정리된 후속 단계입니다. Open Jev의 점수는 품질 성공 확률이 아니며, 이 버전의 라우팅 품질은 아직 실제 작업 데이터로 검증되지 않았습니다. 현재 보정 결과와 한계는 [라우팅 평가](docs/calibration.md)에 기록합니다.

Open Jev가 사용하는 Gemma 3 4B는 요청 문장과 간단한 저장소 프로필(작업 종류, 파일 수, 주요 언어)을 읽고 20개 설정의 설명 문구에 상대 점수를 매깁니다. 전체 코드를 직접 검토하거나 UI 이미지를 평가하지 않습니다. 또한 현재는 모델 선택에 맞춰 별도 학습된 Jev 가중치가 아니라 기본 Gemma 점수화를 사용하므로, 선택이 실제로 좋은지 대표 작업의 결과와 비교해야 합니다.

## 준비

- Python 3.11 이상, `uv`, Git, Codex 로그인 상태가 필요합니다.
- [Open Jev](https://github.com/daseinlabs/open-jev)를 별도로 설치하고 Gemma 3 4B 가중치를 준비합니다. Hugging Face에서 라이선스 수락과 로그인이 필요합니다. 기본 설치 위치는 `~/.local/share/jev-router/open-jev`, 기본 모델 위치는 그 안의 `models/gemma-3-4b-it`, 서버 주소는 `http://127.0.0.1:8000`입니다. 모델과 실행 파일이 준비되면 첫 라우팅 요청 때 서버가 자동으로 시작됩니다.
- Hugging Face의 `Acknowledge license` 흐름은 현재 `Model License Consent on Kaggle`의 이메일 주소 조회 권한을 요청할 수 있습니다. 권한과 Gemma 이용 조건을 확인한 뒤 동의 여부를 직접 결정하세요.
- 다른 설치 위치나 모델을 쓰면 MCP 서버 환경에 `OPENJEV_HOME`과 `OPENJEV_MODEL`을 설정합니다. `OPENJEV_URL`로 다른 주소를 지정할 수 있으며, `OPENJEV_AUTOSTART=0`이면 자동 실행을 끕니다. 서버가 없거나 응답이 불안정하면 작업 난도별 허용 범위 안에서 보수적인 설정으로 되돌아갑니다. Astra 계획 후보만 남는 매우 어려운 작업은 승인 대기 상태를 유지합니다.

## 개발과 설치

```sh
cd plugins/jev-router
uv sync
uv run pytest -q
```

이 저장소에는 `.agents/plugins/marketplace.json`이 포함됩니다. 로컬 Codex에 설치할 때:

```sh
codex plugin marketplace add /absolute/path/to/jev-harness
codex plugin add jev-router@personal
```

설치 후 **새 Codex 작업**에서 플러그인을 호출하고 개발 요청을 전달합니다. 플러그인이 `route_and_run`을 한 번 호출하고 `run_status`로 완료까지 추적합니다. Astra 계획이 추천되면 `run_astra`가 MCP 사용자 입력으로 계획 실행의 확인을 요청합니다. 승인되면 Astra는 읽기 전용으로 계획하고 Sol이 구현합니다. 클라이언트가 이 입력을 지원하지 않으면 Astra 계획도 구현도 시작하지 않습니다.

## 실행 기록

`PLUGIN_DATA` 또는 `JEV_ROUTER_DATA_DIR`을 설정하면 해당 폴더에 기록합니다. 기본값은 `~/.local/share/jev-router`입니다. 각 실행은 JSON 상태, 선택 설정, Codex 작업 ID, 응답, 사용량, 실행 전후 Git 상태와 각각의 `git diff HEAD`를 기록합니다. 최종 diff에는 시작 전에 존재하던 변경이 함께 들어갈 수 있고, 새 untracked 파일의 내용은 diff에 포함되지 않습니다. 두 diff와 Git 상태를 비교하세요.

서버 프로세스가 작업 중 종료되면 실행은 자동 재개되지 않습니다. 동일 저장소에서는 한 번에 한 작업만 실행합니다.

## 작업 후 피드백

플러그인은 작업 종료 시 선택한 모델·추론 강도, 실행 상태, 사용량(제공된 경우), Git 변경 상태를 자동으로 기록합니다. **결과에 대한 사용자 평가는 자동 추정하지 않습니다.** 작업이 끝난 뒤 같은 대화나 다른 Codex 대화에서 Jev Router에 대해 평소 말투로 의견을 남기면, 플러그인이 `recent_runs`로 해당 실행을 찾고 `record_feedback`으로 원문을 저장합니다. 예: “방금 Jev Router가 수정한 로그인 화면은 동작하지만 간격이 어색해.” 여러 실행 중 어느 것인지 불분명하면 실행을 골라야 합니다.

피드백은 같은 컴퓨터의 데이터 폴더 아래 `feedback/*.json`에 실행 ID와 함께 남습니다. 현재 버전은 이를 **수집만** 하며 다음 라우팅에 즉시 반영하거나 Gemma/Open Jev 가중치를 자동 학습하지 않습니다. 실제 작업의 검증 결과와 반복된 피드백을 모아 별도 평가 사례로 만든 후 정책을 보정할 예정입니다. 다른 컴퓨터와는 자동 동기화되지 않습니다.
