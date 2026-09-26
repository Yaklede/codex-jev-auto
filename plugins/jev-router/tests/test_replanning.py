from jev_router.replanning import needs_replan_review


def _message(role, text):
    kind = "input_text" if role == "user" else "output_text"
    return {"role": role, "content": [{"type": kind, "text": text}]}


def test_repeated_unresolved_attempts_trigger_review():
    latest = "이전 작업을 보완해서 목표를 달성해줘"
    payload = {"input": [
        _message("user", "검색 오류를 고쳐줘"),
        _message("assistant", "첫 번째 수정 후에도 검색 오류가 해결되지 않았습니다."),
        _message("user", "아직 안 되니 다른 접근으로 고쳐줘"),
        _message("assistant", "두 번째 수정도 검증에 실패했습니다."),
        _message("user", latest),
    ]}
    assert needs_replan_review(payload, latest)


def test_one_negative_experiment_is_not_model_failure():
    latest = "이전 연구의 결과를 보고서에 정리해줘"
    payload = {"input": [
        _message("assistant", "첫 번째 가설은 백테스트에서 손실이었습니다."),
        _message("user", latest),
    ]}
    assert not needs_replan_review(payload, latest)


def test_reporting_two_negative_experiments_is_not_a_retry():
    latest = "이전 연구 결과를 보고서에 정리해줘"
    payload = {"input": [
        _message("assistant", "첫 번째 가설은 손실이었습니다."),
        _message("assistant", "두 번째 가설도 손실이었습니다."),
        _message("user", latest),
    ]}
    assert not needs_replan_review(payload, latest)


def test_unrelated_new_request_does_not_inherit_old_failures():
    latest = "README 문구를 수정해줘"
    payload = {"input": [
        _message("assistant", "첫 번째 수정이 실패했습니다."),
        _message("assistant", "두 번째 수정도 실패했습니다."),
        _message("user", latest),
    ]}
    assert not needs_replan_review(payload, latest)


def test_explicit_repeated_failure_needs_no_assistant_history():
    latest = "여러 번 고쳤지만 여전히 동작하지 않아. 원인부터 다시 봐줘"
    assert needs_replan_review({"input": [_message("user", latest)]}, latest)
