"""Offline excerpt selection. These cases do not call a provider."""

from aether_worker.research import offline_fact, scripted_answer

MAINTENANCE = "实验设备每九十天完成一次保养。"
LIMIT = "接口请求上限为每分钟六十次。"
BOILING = "事实：水在标准大气压下于一百摄氏度沸腾"


def test_excerpt_picks_the_line_that_overlaps_the_goal() -> None:
    source = f"{MAINTENANCE}\n{LIMIT}\n"
    fact = offline_fact(source_text=source, goal="根据资料，接口每分钟最多允许多少次请求？只回答请求限额。")
    assert fact == LIMIT
    assert "六十" in scripted_answer(source_text=source, goal="根据资料，接口每分钟最多允许多少次请求？只回答请求限额。")


def test_single_uploaded_fact_can_answer_an_excerpt_request() -> None:
    fact = offline_fact(source_text=BOILING + "\n", goal="请根据已上传资料回答沸点")
    assert fact == BOILING


def test_arithmetic_and_unrelated_first_line_are_not_answered() -> None:
    source = f"{MAINTENANCE}\n{LIMIT}\n"
    assert offline_fact(source_text=source, goal="计算 17 × 23，只返回整数。") == ""
    assert scripted_answer(source_text=MAINTENANCE, goal="计算 17 × 23，只返回整数。") == ""
    assert offline_fact(source_text=source, goal="请根据已上传资料回答沸点") == ""


def test_maintenance_excerpt_still_selects_the_maintenance_line() -> None:
    assert offline_fact(source_text=MAINTENANCE, goal="请摘录设备保养周期。") == MAINTENANCE
