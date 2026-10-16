"""P2-01: evidence and ledger labels say what actually happened.

INV-TRUTH-05: the verification report records the run mode that produced the answer.
INV-MONEY-04: a local price-table estimate is not a provider receipt; missing usage is unknown, not an overrun.
Ported from reports/audits/system_round1/repro/test_p2_01_evidence_labels.py.
"""

from __future__ import annotations

import dataclasses
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aether_storage.db import Database
from aether_worker.goal_effect import materialize_goal_effect
from tests.integration.sysfix_b_helpers import endpoints, key, project, rows, store_for
from tests.integration.test_audit_round4 import setup_case

REPORT_SQL = "SELECT report_artifact_id::text AS id FROM verification_results WHERE task_id = :t"


def _report(store, role_urls, owner: str, project_id: str, task_id: str) -> dict:
    report_id = rows(Database(role_urls["aether_verifier"]), owner, project_id, REPORT_SQL, t=task_id)[0]["id"]
    data, _media = store.get(owner_id=owner, project_id=project_id, artifact_id=report_id)
    return json.loads(data)


def test_live_run_report_does_not_claim_scripted_offline(client, role_urls, monkeypatch) -> None:
    owner, project_id, _db, store, _refs, live, payload, state = setup_case(client, role_urls, monkeypatch)
    result = materialize_goal_effect(live, payload)
    assert state["calls"] == 1 and result["status"] == "SUCCEEDED", result
    report = _report(store, role_urls, owner, project_id, payload["task_id"])
    detail = f"真实调用了 {state['calls']} 次 live 传输，报告写的是 model_mode={report['model_mode']} offline={report['offline']}"
    assert report["model_mode"] != "scripted", detail
    assert report["offline"] is False, detail


def test_scripted_run_report_still_says_scripted_offline(client, role_urls) -> None:
    owner, project_id, headers = project(client)
    uploaded = client.post(
        f"/v1/projects/{project_id}/artifacts",
        headers={**headers, "Idempotency-Key": key()},
        files={"file": ("fact.txt", "事实：水在标准大气压下于一百摄氏度沸腾。\n".encode(), "text/plain")},
    )
    assert uploaded.status_code == 201, uploaded.text
    created = client.post(f"/v1/projects/{project_id}/tasks", headers={**headers, "Idempotency-Key": key()}, json={"goal": "请根据已上传资料回答沸点"})
    payload = {"project_id": project_id, "task_id": created.json()["resource_id"], "command_id": created.json()["command_id"]}
    store = store_for(client, role_urls)
    assert materialize_goal_effect(endpoints(role_urls, store.root, provider="scripted"), payload)["status"] == "SUCCEEDED"
    report = _report(store, role_urls, owner, project_id, payload["task_id"])
    assert (report["model_mode"], report["offline"]) == ("scripted", True), report


def test_missing_usage_is_unknown_not_an_overrun(client, role_urls, monkeypatch) -> None:
    owner, project_id, database, _store, _refs, live, payload, state = setup_case(client, role_urls, monkeypatch)
    state["tokens"] = None
    state["completion_tokens"] = None
    result = materialize_goal_effect(live, payload)
    row = rows(database, owner, project_id, "SELECT usage_status, usage_detail->>'book_result' AS book FROM model_requests WHERE task_id = :t", t=payload["task_id"])[0]
    account = rows(database, owner, project_id, "SELECT held_micro_usd, tokens_held FROM budget_accounts WHERE task_id = :t", t=payload["task_id"])[0]
    detail = f"供应商没有回报用量：usage_status={row['usage_status']} book_result={row['book']} 结果={result} 账户={account}"
    assert row["usage_status"] == "unknown", detail
    assert result.get("stop_reason") != "budget_unreconciled", detail
    assert account["held_micro_usd"] > 0 and account["tokens_held"] > 0, detail


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
    owner, project_id, database, _store, _refs, live, payload, _state = setup_case(client, role_urls, monkeypatch)
    monkeypatch.undo()
    server = _priced_upstream("gpt-4o")
    try:
        upstream = dataclasses.replace(live, live_model_base=f"http://127.0.0.1:{server.server_address[1]}/v1")
        materialize_goal_effect(upstream, payload)
    finally:
        server.shutdown()
    row = rows(
        database,
        owner,
        project_id,
        "SELECT usage_status, actual_micro_usd, pricing_version, usage_detail->>'cost_source' AS source FROM model_requests WHERE task_id = :t",
        t=payload["task_id"],
    )[0]
    events = rows(database, owner, project_id, "SELECT payload FROM task_events WHERE task_id = :t AND event_type = 'research.call'", t=payload["task_id"])
    detail = f"上游响应里没有任何费用字段；账本记 usage_status={row['usage_status']} actual={row['actual_micro_usd']} cost_source={row['source']} 事件={events}"
    assert row["source"] != "provider_response", detail
    assert row["source"] == "local_price_estimate", detail
    assert row["pricing_version"], detail
    assert all(event["payload"]["cost_status"] != "known" for event in events), detail
