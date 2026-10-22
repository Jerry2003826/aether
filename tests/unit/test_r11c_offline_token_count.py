"""LiteLLM must never download a Hugging Face tokenizer: offline, counting falls back to the byte estimate."""

from __future__ import annotations

import litellm
import pytest

from aether_gateway import model


def test_hf_tokenizer_download_is_disabled_at_import() -> None:
    assert litellm.disable_hf_tokenizer_download is True


@pytest.mark.parametrize("name", ["groq/llama-3.1-8b-instant", "together_ai/meta-llama/Llama-3-8b-chat-hf"])
def test_a_hf_tokenizer_model_is_counted_by_byte_estimate_without_a_download(monkeypatch, name: str) -> None:
    calls: list[str] = []

    def _download(m: str):
        calls.append(m)
        raise OSError("offline: huggingface.co unreachable")

    monkeypatch.setattr(litellm.utils, "_return_huggingface_tokenizer", _download)
    prompt = "离线计数" * 50
    assert model.count_input_tokens(prompt, name) == (model._byte_estimate(prompt), "estimate")
    assert model.estimate_input_tokens(prompt, name) >= model._byte_estimate(prompt)
    assert calls == []


def test_openai_models_still_count_exactly() -> None:
    counted, kind = model.count_input_tokens("hello world", "gpt-4o")
    assert kind == "exact" and counted > 0
