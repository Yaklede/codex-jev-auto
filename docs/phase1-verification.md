# 1차 라우팅 플러그인 검증 기록

검증일: 2026-09-25

## 완료된 검증

| 기준 | 결과 |
| --- | --- |
| 플러그인 설치 | `jev-router@personal` 활성화, 버전 `0.1.0+codex.20260925091813` |
| 로컬 점수화 | Gemma 3 4B 가중치 설치, Open Jev `/health` 정상 (`mlx`/`metal`), `/score` 응답 확인 |
| 후보 목록 | 현재 Codex 계정에서 4개 모델 × 5개 추론 강도 = 20개 조합 확인 |
| 백엔드 작업 | 실제 MCP `route_and_run` → `run_status` 호출로 `gpt-6-luna/medium` 선택 및 코드 수정 완료. `python3 -m unittest -q`: 2개 통과 |
| 프론트엔드 작업 | 실제 MCP 호출로 `gpt-6-luna/high` 선택 및 버튼 동작 수정 완료. `node --test test_ui.js`: 1개 통과 |
| 실행 기록 | 두 작업 모두 선택 근거, Open Jev 사용 여부, Codex 작업 ID, 사용량, 작업 전후 Git 상태와 diff 파일 기록 |
| Astra 차단 | 승인 기능이 없는 MCP 클라이언트와 명시적으로 거절하는 MCP 클라이언트에서 `run_astra` 호출 시 모두 `awaiting_confirmation` 유지, 실행 결과 없음. 승인 경로는 단위 테스트로 확인 |

백엔드 실행 ID: `c4e958a0-4928-4d26-838e-33c782190c2d`. 기록과 diff는 `~/.local/share/jev-router/verification/backend/runs/`에 있다.

프론트엔드 실행 ID: `bb1a8622-1678-4658-9521-a3bf9f25b4a7`. 기록과 diff는 `~/.local/share/jev-router/verification/frontend/runs/`에 있다.

테스트 저장소는 `~/.local/share/jev-router/verification/backend/repo/`와 `~/.local/share/jev-router/verification/frontend/repo/`에 보존했다. 두 저장소는 실제 사용자 프로젝트가 아닌 검증용 샘플이다.

## 남은 검증

- Codex Desktop이 실제 MCP 사용자 확인 요청을 표시하고 사용자의 수락을 전달하는지는 아직 확인하지 않았다. 이 대화는 플러그인을 설치하기 전에 시작되어 새 MCP 도구가 로드되지 않았으므로, 새 Codex 작업에서 확인해야 한다. 실제 Astra 실행에는 그 작업에서 사용자의 명시적 승인이 필요하다.
- 모델 선택 품질은 아직 보정되지 않았다. 간단한 오탈자 수정과 큰 설계 작업을 비교하는 시험에서 동일한 `gpt-6-luna/high`가 선택됐다. 1차 기능은 실행되지만, 난도에 맞는 선택이라는 제품 목표는 별도 평가가 필요하다.
- 백엔드·프론트엔드 품질 심사 루프는 각각 2차·3차 구현 범위다.

두 샘플 작업의 SDK 보고 총 토큰 수는 각각 66,059와 85,131이다. 캐시 입력 토큰을 포함한 수치이므로 실제 청구량과 같다고 해석하지 않는다. 작은 작업의 비용 효율은 후속 평가에서 확인한다.
