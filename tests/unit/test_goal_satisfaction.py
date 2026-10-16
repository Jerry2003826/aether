"""Goal completion is stricter than a short citation overlap."""

from aether_worker.research import (
    estimate_provider_request_tokens,
    estimate_tokens,
    fact_satisfied,
    goal_completion_detail,
    goal_completion_state,
    required_facts,
)


def test_tokenizer_counts_a_long_prompt_without_a_provider_report() -> None:
    counted = estimate_tokens("token " * 2000)
    assert counted >= 2000


def test_provider_request_reserves_more_than_the_raw_prompt() -> None:
    prompt = "只根据下列资料回答目标。\n目标：沸点\n\n[资料1]\n水在一百摄氏度沸腾"
    assert estimate_provider_request_tokens(prompt) > estimate_tokens(prompt)


def test_short_quote_does_not_complete_the_goal() -> None:
    fact = "接口每分钟最多允许的请求数量明确规定为六十次。"
    goal = "根据资料，接口每分钟最多允许多少次请求？只回答请求限额。"
    state = goal_completion_state(answer=fact[:12], goal=goal, source_text=fact)
    assert state != "pass"


def test_same_number_in_a_paraphrase_can_pass() -> None:
    fact = "接口请求上限为每分钟六十次。"
    goal = "根据资料回答接口每分钟的请求限额。"
    answer = "每分钟六十次。"
    assert fact_satisfied(answer, fact, goal)
    assert goal_completion_state(answer=answer, goal=goal, source_text=fact) == "pass"


def test_a_different_number_is_not_the_same_fact() -> None:
    fact = "接口请求上限为每分钟六十次。"
    goal = "根据资料回答接口每分钟的请求限额。"
    answer = f"引用：{fact}\n最终答案：接口每分钟允许六百次请求。"
    assert goal_completion_state(answer=answer, goal=goal, source_text=fact) != "pass"


BOILING_GOAL = "请根据已上传资料回答沸点。回答时请引用资料原句。"
BOILING_FACT = "事实：水在标准大气压下于一百摄氏度沸腾"
BOILING_ANSWER = "根据资料1原句：“水在标准大气压下于一百摄氏度沸腾”。因此，沸点为一百摄氏度。"


def test_citation_label_is_not_an_unsupported_number() -> None:
    assert goal_completion_state(answer=BOILING_ANSWER, goal=BOILING_GOAL, source_text=BOILING_FACT) == "pass"
    assert goal_completion_detail(answer=BOILING_ANSWER, goal=BOILING_GOAL, source_text=BOILING_FACT) == ""
    for label in ("资料1", "来源2", "source 3", "[4]", "【5】"):
        answer = f"根据{label}原句：“水在标准大气压下于一百摄氏度沸腾”。因此，沸点为一百摄氏度。"
        assert goal_completion_state(answer=answer, goal=BOILING_GOAL, source_text=BOILING_FACT) == "pass", label


def test_invented_boiling_point_is_still_rejected() -> None:
    for answer in (
        "根据资料1原句：“水在标准大气压下于一百摄氏度沸腾”。因此，沸点为九十摄氏度。",
        "沸点为90度。",
        "根据资料，沸点为一百二十摄氏度。",
    ):
        detail = goal_completion_detail(answer=answer, goal=BOILING_GOAL, source_text=BOILING_FACT)
        assert goal_completion_state(answer=answer, goal=BOILING_GOAL, source_text=BOILING_FACT) != "pass"
        assert detail.startswith("unsupported_number:")
        assert "一百" not in detail or "九十" in detail or "90" in detail or "一百二十" in detail


def test_swapped_values_and_unit_changes_do_not_cover_the_fact() -> None:
    source = "接口请求上限为每分钟六十次。\n实验设备每九十天完成一次保养。"
    goal = "请根据资料分别回答接口请求上限和实验设备保养周期。"
    swapped = "接口请求上限为每分钟九十次。\n实验设备每六十天完成一次保养。"
    assert goal_completion_detail(answer=swapped, goal=goal, source_text=source) == "missing_fact"
    assert goal_completion_state(answer=swapped, goal=goal, source_text=source) != "pass"
    fact = "室温记录为六十摄氏度。"
    assert goal_completion_state(answer="室温记录为六十华氏度。", goal="根据资料回答室温。", source_text=fact) != "pass"
    assert goal_completion_detail(answer="室温记录为六十华氏度。", goal="根据资料回答室温。", source_text=fact) == "missing_fact"


def test_subjectless_numbers_do_not_cover_two_objects() -> None:
    source = "甲接口每分钟最多接受60次请求。\n乙接口每分钟最多接受90次请求。"
    goal = "根据资料，分别说明甲接口和乙接口每分钟最多接受多少次请求。"
    answer = "每分钟最多60次，每分钟最多90次"
    assert goal_completion_state(answer=answer, goal=goal, source_text=source) != "pass"
    assert goal_completion_detail(answer=answer, goal=goal, source_text=source) == "missing_fact"


def test_shared_generic_word_does_not_support_a_second_object() -> None:
    fact = "客户端请求限额为每分钟60次。"
    goal = "分别说明客户端限额和服务端限额"
    answer = f"资料记载：{fact}"
    assert goal_completion_state(answer=answer, goal=goal, source_text=fact) == "unverifiable"
    assert goal_completion_detail(answer=answer, goal=goal, source_text=fact) == "unsupported_goal_part"


def test_two_overlapping_lines_are_both_required() -> None:
    source = "接口请求上限为每分钟六十次。\n实验设备每九十天完成一次保养。"
    goal = "请根据资料分别回答接口请求上限和实验设备保养周期。"
    facts = required_facts(source_text=source, goal=goal)
    assert len(facts) == 2
    assert goal_completion_state(answer=facts[0], goal=goal, source_text=source) != "pass"
    assert goal_completion_state(answer=source, goal=goal, source_text=source) == "pass"
