# Jev Auto

Jev Auto는 로컬 Open Jev로 Codex 작업에 사용할 모델과 추론 강도를 고른다. 후보는 GPT-6 Luna, GPT-5.6 Terra, GPT-6 Sol, GPT-6 Astra와 `low`·`medium`·`high`·`xhigh`·`max` 중 실제 지원 조합이다. 사용자의 기존 메인 작업을 유지하며, `jev-auto` 스킬은 선택 결과를 **Codex 기본 서브에이전트**에 적용한다. 별도 사용자 작업을 만들거나 MCP 작업 실행기를 호출하지 않는다.

```text
기존 메인 작업 (오케스트레이션)
  ├─ Jev Auto CLI → Open Jev 점수화 → 모델/추론 강도
  ├─ 기본 서브에이전트로 구현 → 결과 수집 → 메인 작업에서 검증
  └─ Astra 추천 시: 사용자 확인 → Astra 계획만 → Sol 구현

선택 사항: 모델 선택기에서 Jev Auto 선택
  └─ 로컬 Responses 프록시가 현재 작업의 각 턴을 실제 모델로 전달
```

**두 진입 방식의 차이:** 메인 작업의 모델을 그대로 두고 `$jev-auto`를 호출하면 메인은 조정자이고 서브에이전트가 선택된 모델을 사용한다. 모델 선택기에서 `Jev Auto`를 직접 선택하면 현재 작업의 턴 자체가 자동 라우팅된다. 이 경우 서브에이전트가 자동으로 생기지는 않는다. Astra 추천은 프록시에서 실행 전에 차단되며, 스킬을 통한 확인·계획 흐름이 필요하다.

## 설치

Python 3.11+, `uv`, Codex 로그인이 필요하다. Open Jev 점수화를 쓰려면 로컬 Open Jev와 Gemma 3 4B 가중치를 준비한다. 기존 설치 기본 경로는 `~/.local/share/jev-router/open-jev`이고 Open Jev 주소는 `http://127.0.0.1:8000`이다. 준비되지 않으면 정책에 따른 비 Astra 기본 설정을 사용한다.

```sh
cd plugins/jev-router
uv sync
uv run pytest -q
uv run jev-auto install
codex plugin add jev-router@personal
```

`install`은 설치된 Codex 모델 목록을 보존하면서 `Jev Auto` 항목을 더한 카탈로그를 만든다. 로컬 서버를 macOS LaunchAgent로 시작하고 `~/.codex/config.toml`에 `model_catalog_json`과 `openai_base_url`을 추가한다. 기존 설정 백업은 `~/.local/share/jev-router`에 보관한다. Codex Desktop은 카탈로그 갱신을 반영하려면 다시 열어야 할 수 있으며, 이미 열려 있던 작업의 새 플러그인 스킬 로딩은 보장되지 않는다. 모델 선택기의 사용자 인터페이스 지원 여부는 설치된 Desktop 빌드에서 확인해야 한다.

```sh
# 상태 확인
curl http://127.0.0.1:18084/healthz
codex debug models

# 메인 작업에서 사용할 서브에이전트 모델 사전 선택
~/.local/share/jev-router/bin/jev-auto route '요청 내용' --workspace /absolute/repo/path

# 카탈로그 갱신 / 설정 복원
uv run jev-auto sync-catalog
uv run jev-auto restore
```

Codex 0.154의 `codex_exec` 클라이언트에서는 GPT-6 Sol/Luna가 목록에 있어도 ChatGPT 백엔드가 직접 호출을 거절하는 것을 관찰했다. 이 클라이언트의 자동 라우팅은 Terra/Astra로 제한한다. Desktop은 동일한 제한을 확인하지 않았으므로 이 CLI 제한을 적용하지 않는다. 다른 구체 모델을 선택한 요청은 모델을 바꾸지 않고 프록시를 통과한다.

## 동작과 범위

프록시는 `127.0.0.1:18084`에서 Responses HTTP/WebSocket 요청을 받아, `jev-auto`일 때만 모델·추론 강도를 바꾼다. 기존 Codex 로그인 헤더를 상위 서버로 전달한다. 요청 본문과 인증 토큰은 라우팅 로그에 저장하지 않는다. 선택 결과는 `~/.local/share/jev-router/auto-decisions.jsonl`에 모델·강도·정책 근거로 남는다. Open Jev 점수는 상대적인 옵션 순위이며 실제 성공률이 아니다.

1차 범위는 라우팅과 실행 연결이다. 쉬운 백엔드 작업은 단순하게, 어려운 작업에는 필요한 구조를 충분히 적용하는 코드 품질 루프와 정돈된 프론트엔드 코드·더 나은 화면 디자인 평가 루프는 [개발 계획](docs/plan.md)의 다음 단계다. 과거 MCP 시제품의 검증은 [1차 검증 기록](docs/phase1-verification.md)에 남겨 두었다.
