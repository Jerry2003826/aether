from types import SimpleNamespace

from aether_gateway.model import _completion_text


def test_length_finish_is_not_a_complete_answer() -> None:
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="被截断的半句"), finish_reason="length")]
    )
    text, reason = _completion_text(response)
    assert reason == "length"
    assert text is None


def test_stop_finish_keeps_the_answer() -> None:
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="完整回答"), finish_reason="stop")]
    )
    text, reason = _completion_text(response)
    assert reason == "stop"
    assert text == "完整回答"


def test_missing_finish_reason_keeps_the_answer() -> None:
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="完整回答"))])
    text, reason = _completion_text(response)
    assert reason is None
    assert text == "完整回答"
