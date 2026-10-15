"""Goal completion is stricter than a short citation overlap."""

from aether_worker.research import estimate_tokens, fact_satisfied, goal_completion_state, required_facts


def test_tokenizer_counts_a_long_prompt_without_a_provider_report() -> None:
    counted = estimate_tokens("token " * 2000)
    assert counted >= 2000


def test_short_quote_does_not_complete_the_goal() -> None:
    fact = "接口每分钟最多允许的请求数量明确规定为六十次。"
    goal = "根据资料，接口每分钟最多允许多少次请求？只回答请求限额。"
    state = goal_completion_state(answer=fact[:12], goal=goal, source_text=fact)
    assert state != "pass"


def test_same_number_in_a_paraphrase_can_pass() -> None:
    fact = "接口请求上限为每分钟六十次。"
    goal = "根据资料回答接口每分钟的请求限额。"
    assert fact_satisfied("每分钟六十次。", fact, goal)


def test_a_different_number_is_not_the_same_fact() -> None:
    fact = "接口请求上限为每分钟六十次。"
    goal = "根据资料回答接口每分钟的请求限额。"
    answer = f"引用：{fact}\n最终答案：接口每分钟允许六百次请求。"
    assert goal_completion_state(answer=answer, goal=goal, source_text=fact) != "pass"


def test_two_overlapping_lines_are_both_required() -> None:
    source = "接口请求上限为每分钟六十次。\n实验设备每九十天完成一次保养。"
    goal = "请根据资料分别回答接口请求上限和实验设备保养周期。"
    facts = required_facts(source_text=source, goal=goal)
    assert len(facts) == 2
    assert goal_completion_state(answer=facts[0], goal=goal, source_text=source) != "pass"
    assert goal_completion_state(answer=source, goal=goal, source_text=source) == "pass"
