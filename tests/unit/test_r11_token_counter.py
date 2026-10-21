"""Input token reservation uses LiteLLM's token_counter and says whether the count is exact."""

from __future__ import annotations

import litellm
from aether_gateway import model


def _byte_estimate(prompt: str) -> int:
    return max(1, -(-len(prompt.encode("utf-8")) // model.INPUT_BYTES_PER_TOKEN))


def test_token_count_marks_exact_for_known_tokenizer() -> None:
    prompt = '{"messages":[{"content":"你好，世界 hello","role":"user"}],"model":"gpt-4o"}'
    count_input_tokens = model.count_input_tokens
    assert count_input_tokens(prompt, "gpt-4o") == (litellm.token_counter(model="gpt-4o", text=prompt), "exact")


def test_token_count_falls_back_to_estimate_and_marks_estimate() -> None:
    count_input_tokens = model.count_input_tokens
    prompt = "no tokenizer is mapped for this alias"
    assert count_input_tokens(prompt, "aether-sandbox-unmapped-alias") == (_byte_estimate(prompt), "estimate")
    assert count_input_tokens(prompt, None) == (_byte_estimate(prompt), "estimate")


def test_reservation_never_below_one_and_never_below_exact() -> None:
    estimate_input_tokens = model.estimate_input_tokens
    prompt = "x\n" * 10
    exact = litellm.token_counter(model="gpt-4o", text=prompt)
    assert exact > _byte_estimate(prompt)
    assert estimate_input_tokens(prompt, "gpt-4o") >= exact
    assert estimate_input_tokens(prompt, "gpt-4o") >= _byte_estimate(prompt)
    assert estimate_input_tokens("", "gpt-4o") >= 1
    assert estimate_input_tokens("") >= 1


def test_an_unavailable_hugging_face_tokenizer_falls_back_to_the_byte_estimate_at_once(monkeypatch) -> None:
    # A model whose LiteLLM tokenizer is a Hugging Face one that would be downloaded. With the download
    # unavailable (here: it takes 3 s and then fails, like a network timeout), counting must not wait on
    # it: the answer is the byte estimate, marked estimate, straight away; the hold is never below it.
    import time

    import litellm.utils

    calls: list[str] = []

    def unavailable(kind, backend):  # noqa: ANN001 - LiteLLM's loader signature
        calls.append(kind)
        time.sleep(3)
        raise OSError("hugging face hub unreachable")

    monkeypatch.setattr(litellm.utils, "_load_huggingface_tokenizer", unavailable)
    alias = "anyscale/meta-llama/Llama-2-7b-chat-hf"
    prompt = "llama prompt " * 20
    started = time.monotonic()
    assert model.count_input_tokens(prompt, alias) == (_byte_estimate(prompt), "estimate")
    assert model.estimate_input_tokens(prompt, alias) >= _byte_estimate(prompt)
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, (elapsed, calls)
