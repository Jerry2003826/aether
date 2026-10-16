"""P2-01 · 证据和账目标签说的不是真话。

不变量 INV-TRUTH-05：验收报告如实记录运行模式；
不变量 INV-MONEY-04：费用来源如实标注（本地价目表估算不是供应商回执），缺失用量记成 unknown 而不是"超额"。
"""

from __future__ import annotations

import dataclasses
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aether_storage.db import Database
from aether_worker.goal_effect import materialize_goal_effect
from audit_helpers import rows
from tests.integration.test_audit_round4 import setup_case


def test_live_run_report_does_not_claim_scripted_offline(client, role_urls, monkeypatch) -> None:
    owner, project_id, _db, store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch)
    result = materialize_goal_effect(endpoints, payload)
    assert state["calls"] == 1 and result["status"] == "SUCCEEDED", result
    report_id = rows(
        Database(role_urls["aether_verifier"]),
        owner,
        project_id,
        "SELECT report_artifact_id::text AS id FROM verification_results WHERE task_id = :t",
        t=payload["task_id"],
    )[0]["id"]
    data, _media = store.get(owner_id=owner, project_id=project_id, artifact_id=report_id)
    report = json.loads(data)
    detail = f"真实调用了 {state['calls']} 次 live 传输，报告写的是 model_mode={report['model_mode']} offline={report['offline']}"
    assert report["model_mode"] != "scripted", detail
    assert report["offline"] is False, detail


def test_missing_usage_is_unknown_not_an_overrun(client, role_urls, monkeypatch) -> None:
    owner, project_id, database, _store, _refs, endpoints, payload, state = setup_case(client, role_urls, monkeypatch)
    state["tokens"] = None
    state["completion_tokens"] = None
    result = materialize_goal_effect(endpoints, payload)
    row = rows(database, owner, project_id, "SELECT usage_status, usage_detail->>'book_result' AS book FROM model_requests WHERE task_id = :t", t=payload["task_id"])[0]
    detail = f"供应商没有回报用量：usage_status={row['usage_status']} book_result={row['book']} 结果={result}"
    assert row["usage_status"] == "unknown", detail
    assert result.get("stop_reason") != "budget_unreconciled", detail


def _priced_upstream(reported_model: str):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length", "0")))
            body = json.dumps(
                {
                    "id": "chatcmpl-audit",
                    "object": "chat.completion",
                    "created": 1,
                    "model": reported_model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "资料记载：接口请求上限为每分钟六十次。"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
                }
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_local_price_table_is_not_labelled_as_a_provider_receipt(client, role_urls, monkeypatch) -> None:
    """The upstream returns no cost at all. LiteLLM prices the self-reported model name locally."""
    owner, project_id, database, _store, _refs, endpoints, payload, _state = setup_case(client, role_urls, monkeypatch)
    monkeypatch.undo()
    server = _priced_upstream("gpt-4o")
    try:
        live = dataclasses.replace(endpoints, live_model_base=f"http://127.0.0.1:{server.server_address[1]}/v1")
        materialize_goal_effect(live, payload)
    finally:
        server.shutdown()
    row = rows(
        database,
        owner,
        project_id,
        "SELECT usage_status, actual_micro_usd, usage_detail->>'cost_source' AS source FROM model_requests WHERE task_id = :t",
        t=payload["task_id"],
    )[0]
    detail = f"上游响应里没有任何费用字段；账本记 usage_status={row['usage_status']} actual={row['actual_micro_usd']} cost_source={row['source']}"
    assert row["source"] != "provider_response", detail
