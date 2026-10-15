"""One bounded OpenHands SDK completion. The provider key is not in this process."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

from openhands.sdk.llm import LLM, Message, TextContent
from pydantic import SecretStr


def _text(message: Message) -> str:
    parts: list[str] = []
    for item in message.content:
        value = getattr(item, "text", None)
        if isinstance(value, str):
            parts.append(value)
    return "\n".join(parts)


def _billing_source(content: str) -> str | None:
    start = content.find("{")
    end = content.rfind("}")
    if start >= 0 and end > start:
        try:
            payload = json.loads(content[start : end + 1])
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("billing_py"), str):
            source = payload["billing_py"]
            if len(source) <= 2000 and source.startswith("def add"):
                return source
    fenced = content.find("```")
    if fenced >= 0:
        rest = content[fenced + 3 :]
        if rest.startswith("python"):
            rest = rest[6:]
        close = rest.find("```")
        if close > 0 and rest[:close].lstrip().startswith("def add") and close <= 2000:
            return rest[:close].strip() + "\n"
    stripped = content.strip()
    if stripped.startswith("def add") and len(stripped) <= 2000:
        return stripped + ("\n" if not stripped.endswith("\n") else "")
    return None


def _write_billing(content: str) -> bool:
    raw_dir = os.environ.get("AETHER_ATTEMPT_DIR")
    if not raw_dir:
        return False
    root = Path(raw_dir).resolve()
    allowed = Path(tempfile.gettempdir()).resolve()
    target = (root / "billing.py").resolve()
    if not root.is_relative_to(allowed) or target.parent != root:
        raise RuntimeError("patch_path_rejected")
    source = _billing_source(content)
    if source is None:
        return False
    token = os.environ.get("AETHER_JOB_TOKEN", "")
    if token and token in source:
        raise RuntimeError("secret_in_patch")
    target.write_text(source, encoding="utf-8")
    return True


def main() -> int:
    prompt = sys.stdin.read()
    llm = LLM(
        model="openai/deepseek-chat",
        api_key=SecretStr(os.environ["AETHER_JOB_TOKEN"]),
        base_url=os.environ["AETHER_GATEWAY_URL"],
        usage_id="f26",
    )
    response = llm.completion(
        messages=[Message(role="user", content=[TextContent(text=prompt)])],
        max_tokens=int(os.environ.get("AETHER_MAX_OUTPUT_TOKENS", "512")),
    )
    content = _text(response.message)
    wrote = _write_billing(content)
    sys.stdout.write(
        json.dumps(
            {
                "sdk": getattr(llm, "model", "openai/deepseek-chat"),
                "release": version("openhands-sdk"),
                "content": content,
                "wrote_billing": wrote,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        sys.stdout.write(json.dumps({"error": type(exc).__name__}))
        raise SystemExit(1) from None
