import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from playwright.sync_api import expect, sync_playwright

from aether_api.app import create_app
from aether_api.auth_service import issue_bootstrap_secret
from aether_api.settings import Settings
from aether_storage.db import Database
from aether_worker.console_commands import apply_console_commands

PASSPHRASE = "correct horse battery"
HTML = "<img src=x onerror=alert(1)>"


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait(url: str) -> None:
    deadline = time.time() + 90
    last = ""
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=2)
            if response.status_code < 500:
                return
            last = str(response.status_code)
        except httpx.HTTPError as exc:
            last = str(exc)
        time.sleep(0.4)
    raise AssertionError(f"服务没有起来: {url} {last}")


def test_console_shows_real_states_and_does_not_execute_html(role_urls, clean_db, tmp_path: Path) -> None:
    api_port = _port()
    web_port = _port()
    web_origin = f"http://127.0.0.1:{web_port}"
    secret_path = Path("secrets/console-bootstrap.json")
    secret = issue_bootstrap_secret(secret_path)
    app = create_app(
        Settings(
            database_url=role_urls["aether_api"],
            bootstrap_path=secret_path,
            allow_testclient=False,
            origins=frozenset({web_origin, "http://127.0.0.1:3000"}),
            cookie_secure=False,
            artifact_root=tmp_path / "artifacts",
        )
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait(f"http://127.0.0.1:{api_port}/health/live")
    if not Path("apps/web/node_modules/next/package.json").is_file():
        subprocess.run(["corepack", "pnpm@10.18.2", "install", "--frozen-lockfile"], cwd="apps/web", check=True)
    web = subprocess.Popen(
        ["node_modules/.bin/next", "dev", "--hostname", "127.0.0.1", "--port", str(web_port)],
        cwd="apps/web",
        env={**os.environ, "AETHER_API_BASE": f"http://127.0.0.1:{api_port}"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait(f"{web_origin}/console")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            desktop = browser.new_context(viewport={"width": 1280, "height": 800})
            page = desktop.new_page()
            page.goto(f"{web_origin}/console", wait_until="networkidle")
            expect(page.get_by_text("未登录。没有任务、审批或费用数据。")).to_be_visible()
            expect(page.get_by_test_id("long-text")).to_contain_text("控制台需要完整显示中文长文本")
            assert len(page.get_by_test_id("long-text").inner_text()) > 200
            bootstrap = httpx.post(
                f"http://127.0.0.1:{api_port}/v1/auth/bootstrap",
                headers={"Origin": web_origin},
                json={"bootstrap_secret": secret, "display_name": "所有者", "passphrase": PASSPHRASE},
                timeout=10,
            )
            assert bootstrap.status_code == 201, bootstrap.text
            page.get_by_label("口令").fill(PASSPHRASE)
            page.get_by_role("button", name="登录").click()
            expect(page.get_by_text("还没有项目。可以新建一个。")).to_be_visible()
            page.get_by_label("项目名称").fill("控制台项目")
            page.get_by_role("button", name="新建项目").click()
            expect(page.get_by_text("还没有任务。可以新建。")).to_be_visible()
            page.get_by_role("button", name="刷新").focus()
            page.keyboard.press("Shift+Tab")
            expect(page.get_by_role("button", name="设置")).to_be_focused()
            session = page.request.get(f"{web_origin}/v1/auth/session")
            assert session.ok
            csrf = session.json()["csrf_token"]
            projects = page.request.get(f"{web_origin}/v1/projects")
            project_id = projects.json()["items"][0]["id"]
            created = page.request.post(
                f"{web_origin}/v1/projects/{project_id}/sources",
                headers={
                    "X-CSRF-Token": csrf,
                    "Idempotency-Key": "browser-source",
                    "Origin": web_origin,
                    "Content-Type": "application/json",
                },
                data=json.dumps({"kind": "url", "locator": HTML, "allowed_domains": ["example.test"]}),
            )
            assert created.status == 202, created.text()
            apply_console_commands(Database(role_urls["aether_worker"]), owner_id=session.json()["owner_id"], project_id=project_id)
            page.get_by_role("button", name="刷新").click()
            page.get_by_role("button", name="知识").click()
            expect(page.get_by_text(HTML)).to_be_visible()
            assert page.locator("img").count() == 0
            page.get_by_role("button", name="技能").click()
            expect(page.get_by_text("评测由独立评分器写入终态。受理不是通过，证据不足不会晋升。")).to_be_visible()
            code = desktop.new_page()
            code.goto(f"{web_origin}/code", wait_until="networkidle")
            expect(code.get_by_text("提交只表示已受理。沙箱断网、只读、限资源。终态是 FINISHED 或 FAILED，失败后可以用同一作业键重试。")).to_be_visible()
            refreshed = code.request.get(f"{web_origin}/v1/auth/session")
            assert refreshed.ok, refreshed.text()
            csrf = refreshed.json()["csrf_token"]
            task = code.request.post(
                f"{web_origin}/v1/projects/{project_id}/tasks",
                headers={
                    "X-CSRF-Token": csrf,
                    "Idempotency-Key": "browser-code-task",
                    "Origin": web_origin,
                    "Content-Type": "application/json",
                },
                data=json.dumps({"goal": "改一处"}),
            )
            assert task.ok, task.text()
            uploaded = code.request.post(
                f"{web_origin}/v1/projects/{project_id}/artifacts",
                headers={
                    "X-CSRF-Token": csrf,
                    "Idempotency-Key": "browser-code-file",
                    "Origin": web_origin,
                },
                multipart={
                    "file": {
                        "name": "base.json",
                        "mimeType": "application/json",
                        "buffer": json.dumps(
                            {
                                "kind": "aether.coding_workspace",
                                "base_files": {"app.py": "def value():\n    return 0\n"},
                                "files": {"app.py": "def value():\n    return 1\n"},
                                "tests": {"test_app.py": "import app\n\ndef test_value():\n    assert app.value() == 1\n"},
                            }
                        ).encode(),
                    }
                },
            )
            assert uploaded.ok, uploaded.text()
            code.get_by_label("项目编号").fill(project_id)
            code.get_by_label("任务编号").fill(task.json()["resource_id"])
            code.get_by_label("基线产物").fill(uploaded.json()["artifact_id"])
            code.get_by_label("作业键").fill("edit")
            code.get_by_label("目标").fill("改一处")
            code.get_by_role("button", name="提交编码作业").click()
            expect(code.get_by_role("status")).to_have_text("作业已排队，还没有通过结果。")
            mobile = browser.new_context(viewport={"width": 390, "height": 800})
            narrow = mobile.new_page()
            narrow.goto(f"{web_origin}/console", wait_until="networkidle")
            expect(narrow.get_by_role("button", name="总览")).to_be_visible()
            narrow.goto(f"{web_origin}/code", wait_until="networkidle")
            expect(narrow.get_by_text("提交只表示已受理。沙箱断网、只读、限资源。终态是 FINISHED 或 FAILED，失败后可以用同一作业键重试。")).to_be_visible()
            narrow.get_by_role("button", name="提交编码作业").click()
            expect(narrow.get_by_role("status")).to_have_text("还没有登录。")
            page.context.set_offline(True)
            page.get_by_role("button", name="刷新").click()
            expect(page.get_by_text("API 未连接。进程恢复前这里保持空状态。")).to_be_visible()
            browser.close()
    finally:
        web.terminate()
        web.wait(timeout=10)
        server.should_exit = True
        thread.join(timeout=10)
        secret_path.unlink(missing_ok=True)
