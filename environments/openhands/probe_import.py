"""Import the four OpenHands distributions and construct an LLM pointed at a gateway."""

from __future__ import annotations

import importlib
import json

from pydantic import SecretStr

modules = [
    "openhands.sdk",
    "openhands.tools",
    "openhands.workspace",
    "openhands.agent_server",
]
found: dict[str, dict] = {}
for name in modules:
    module = importlib.import_module(name)
    found[name] = {"imported": True, "file": getattr(module, "__file__", None)}

from openhands.sdk import LLM

gateway = "http://127.0.0.1:9/v1"
llm = LLM(model="openai/aether-gateway", api_key=SecretStr("job-scoped-token"), base_url=gateway, usage_id="probe")
print(json.dumps({"modules": found, "llm_base_url": llm.base_url, "model": llm.model}))
