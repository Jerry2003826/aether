"""Public-API proof for the SYS-01 follow-up. Each case reaches a terminal state through the worker."""

from __future__ import annotations

import asyncio
import io
import json
import sys
import threading
import time
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from aether_worker.startup_budget import wait_worker_ready
from aether_api.auth_service import issue_bootstrap_secret
from aether_storage.db import Database
from tests.integration.test_hybrid_retrieval import APPLE
from tests.integration.test_sys01_entry import _command_status, _poll, _wait_command
from tests.integration.test_worker_process import (
    ORIGIN_VALUE,
    PASSPHRASE,
    ROOT,
    _headers,
    _port,
    _service_env,
    _start,
    _stop,
    _temporal_address,
    _wait_http,
)

SECRET = "另一项目的机密句子不会进入这个项目的索引"


class _Feed(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        path = self.path.split("?", 1)[0]
        self.server.hits.append(path)  # type: ignore[attr-defined]
        if path == "/slow":
            time.sleep(8)
            self._body(b"slow")
            return
        if path == "/hop":
            self._redirect("/doc")
            return
        if path == "/evil-hop":
            self._redirect("http://evil.invalid/secret")
            return
        if path == "/doc":
            self._body(APPLE.encode())
            return
        self.send_error(404)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self, payload: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        return


class _Running:
    def __init__(self, role_urls: dict[str, str], tmp_path: Path, name: str, extra: dict[str, str] | None = None) -> None:
        self.role_urls = role_urls
        self.address = _temporal_address()
        self.api_port = _port()
        self.queue = f"aether-followup-{name}-{uuid.uuid4()}"
        self.artifact_root = tmp_path / "artifacts"
        self.index_root = tmp_path / "indexes"
        self.secret_path = ROOT / "secrets" / f"followup-{name}-{uuid.uuid4().hex}.json"
        secret = issue_bootstrap_secret(self.secret_path)
        self.env = _service_env(
            role_urls,
            address=self.address,
            api_port=self.api_port,
            queue=self.queue,
            artifact_root=self.artifact_root,
            secret_path=self.secret_path,
            index_root=self.index_root,
            extra=extra,
        )
        self.api_logs: list[str] = []
        self.worker_logs: list[str] = []
        self.api = _start([sys.executable, "-m", "aether_api.main"], self.env, self.api_logs)
        self.worker = _start([sys.executable, "-m", "aether_worker"], self.env, self.worker_logs)
        self.http = httpx.Client(base_url=f"http://127.0.0.1:{self.api_port}", timeout=10)
        self.secret = secret
        self.owner_id = ""
        self.project_id = ""
        self.csrf = ""

    def close(self) -> None:
        self.http.close()
        _stop(self.worker)
        _stop(self.api)
        self.secret_path.unlink(missing_ok=True)

    def ready(self) -> None:
        _wait_http(f"http://127.0.0.1:{self.api_port}/health/live")
        wait_worker_ready(self.worker_logs, self.worker)

    def bootstrap(self, key: str, name: str) -> None:
        created = self.http.post(
            "/v1/auth/bootstrap",
            headers={"Origin": ORIGIN_VALUE},
            json={"bootstrap_secret": self.secret, "display_name": "所有者", "passphrase": PASSPHRASE},
        )
        assert created.status_code == 201, created.text
        self.csrf = created.json()["csrf_token"]
        self.owner_id = created.json()["owner_id"]
        project = self.http.post("/v1/projects", headers=_headers(self.csrf, key), json={"name": name})
        assert project.status_code == 201, project.text
        self.project_id = project.json()["id"]

    def upload(self, key: str, payload: bytes) -> str:
        uploaded = self.http.post(
            f"/v1/projects/{self.project_id}/artifacts",
            headers=_headers(self.csrf, key),
            files={"file": ("note.txt", payload, "text/plain")},
        )
        assert uploaded.status_code == 201, uploaded.text
        return str(uploaded.json()["artifact_id"])

    def create_source(self, key: str, kind: str, locator: str, domains: list[str]) -> str:
        created = self.http.post(
            f"/v1/projects/{self.project_id}/sources",
            headers=_headers(self.csrf, key),
            json={"kind": kind, "locator": locator, "allowed_domains": domains},
        )
        assert created.status_code == 202, created.text
        source_id = str(created.json()["resource_id"])
        _poll(
            self.http,
            f"/v1/projects/{self.project_id}/sources/{source_id}",
            lambda body: body,
            self.worker,
            self.worker_logs,
        )
        return source_id

    def ingest(self, key: str, source_id: str, timeout: float = 90) -> dict[str, Any]:
        created = self.http.post(
            f"/v1/projects/{self.project_id}/ingestions",
            headers=_headers(self.csrf, key),
            json={"source_id": source_id},
        )
        assert created.status_code == 202, created.text

        def finished(body: dict[str, Any]) -> dict[str, Any] | None:
            item = next((row for row in body["items"] if row["source_id"] == source_id), None)
            if item is None or item["status"] not in {"SUCCEEDED", "FAILED"}:
                return None
            return item

        try:
            return _poll(
                self.http,
                f"/v1/projects/{self.project_id}/ingestions?limit=100",
                finished,
                self.worker,
                self.worker_logs,
                timeout,
            )
        except AssertionError as exc:
            raise AssertionError(f"{exc}\n{self._ingestion_state(source_id)}") from exc

    def _ingestion_state(self, source_id: str) -> str:
        """Database rows and child processes for a stuck ingestion. The assertion stays the same."""
        lines = [f"worker_pid={self.worker.pid} returncode={self.worker.poll()}", _descendant_cmds(self.worker.pid)]
        try:
            database = Database(self.role_urls["aether_worker"])
            with database.transaction(owner_id=self.owner_id, project_id=self.project_id) as conn:
                jobs = conn.execute(
                    text(
                        "SELECT id::text, status, error_code, source_version_id::text, command_id::text, "
                        "created_at::text, finished_at::text FROM ingestion_jobs "
                        "WHERE project_id = :project_id AND source_id = :source_id"
                    ),
                    {"project_id": self.project_id, "source_id": source_id},
                ).mappings().all()
                lines.append("ingestion_jobs=" + json.dumps([dict(row) for row in jobs], ensure_ascii=False))
                command_ids = [str(row["command_id"]) for row in jobs]
                if command_ids:
                    commands = conn.execute(
                        text(
                            "SELECT id::text, kind, status, attempts, delivered_at::text FROM command_outbox "
                            "WHERE project_id = :project_id AND id::text = ANY(:ids)"
                        ),
                        {"project_id": self.project_id, "ids": command_ids},
                    ).mappings().all()
                    lines.append("command_outbox=" + json.dumps([dict(row) for row in commands], ensure_ascii=False))
                version_ids = [str(row["source_version_id"]) for row in jobs if row["source_version_id"]]
                if version_ids:
                    chunks = conn.execute(
                        text(
                            "SELECT ordinal, embedding_revision, left(text_content, 80) AS text_content FROM chunks "
                            "WHERE project_id = :project_id AND source_version_id::text = ANY(:ids) ORDER BY ordinal"
                        ),
                        {"project_id": self.project_id, "ids": version_ids},
                    ).mappings().all()
                    lines.append("chunks=" + json.dumps([dict(row) for row in chunks], ensure_ascii=False))
                generations = conn.execute(
                    text(
                        "SELECT generation_key, state, created_at::text FROM index_generations "
                        "WHERE project_id = :project_id ORDER BY created_at"
                    ),
                    {"project_id": self.project_id},
                ).mappings().all()
                lines.append("index_generations=" + json.dumps([dict(row) for row in generations], ensure_ascii=False))
        except Exception as exc:
            lines.append(f"ingestion_state_failed={type(exc).__name__}: {exc}")
        return "\n".join(lines)

    def search(self, key: str, query: str) -> httpx.Response:
        return self.http.post(
            f"/v1/projects/{self.project_id}/search",
            headers=_headers(self.csrf, key),
            json={"project_id": self.project_id, "query": query, "mode": "hybrid"},
        )


def _descendant_cmds(pid: int) -> str:
    found: list[str] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
            command = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
        except OSError:
            continue
        end = stat.rfind(")")
        if end < 0:
            continue
        fields = stat[end + 2 :].split()
        if len(fields) < 2 or fields[1] != str(pid):
            continue
        child = int(entry.name)
        found.append(command.strip())
        nested = _descendant_cmds(child)
        if nested:
            found.append(nested)
    return "\n".join(found)


@contextmanager
def _services(role_urls: dict[str, str], tmp_path: Path, name: str, extra: dict[str, str] | None = None) -> Iterator[_Running]:
    running = _Running(role_urls, tmp_path, name, extra)
    try:
        running.ready()
        yield running
    finally:
        running.close()


@contextmanager
def _server() -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Feed)
    server.hits = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_url_allowlist_and_redirects(role_urls, clean_db, tmp_path: Path) -> None:
    with _server() as server, _services(
        role_urls,
        tmp_path,
        "allow",
        {"AETHER_INGEST_ALLOW_FIXTURES": "1", "AETHER_INGEST_FIXTURE_HOSTS": "127.0.0.1"},
    ) as running:
        running.bootstrap("allow-project", "域名")
        port = server.server_address[1]
        rejected = (
            ("url", "https://evil.invalid/page", ["example.test"]),
            ("url", "https://evil.example.test/page", ["example.test"]),
            ("url", "https://example.test.evil/page", ["example.test"]),
            ("rss", "https://evil.invalid/feed.xml", ["example.test"]),
            ("url", f"http://127.0.0.1:{port}/doc", []),
            ("github_api", "https://api.github.com/repos/octocat/Hello-World", ["example.test"]),
            ("github_api", "https://api.github.com/repos/octocat/Spoon-Knife", []),
            ("arxiv_api", "1706.03762", ["example.test"]),
            ("arxiv_api", "https://export.arxiv.org/api/query?id_list=1606.06565", []),
            ("github_api", f"http://127.0.0.1:{port}/repos/octocat/Hello-World", ["example.test"]),
            ("arxiv_api", f"http://127.0.0.1:{port}/api/query?id_list=1706.03762", []),
        )
        for ordinal, (kind, locator, domains) in enumerate(rejected):
            source_id = running.create_source(f"allow-source-{ordinal}", kind, locator, domains)
            job = running.ingest(f"allow-ingest-{ordinal}", source_id)
            assert job["status"] == "FAILED", job
            assert job["error_code"] == "domain_rejected", job
        server.hits.clear()  # type: ignore[attr-defined]
        evil_id = running.create_source(
            "allow-evil",
            "url",
            f"http://127.0.0.1:{port}/evil-hop",
            ["127.0.0.1"],
        )
        evil = running.ingest("allow-evil-ingest", evil_id)
        assert server.hits == ["/evil-hop"]  # type: ignore[attr-defined]
        assert evil["status"] == "FAILED", evil
        assert evil["error_code"] == "domain_rejected", evil
        server.hits.clear()  # type: ignore[attr-defined]
        good_id = running.create_source(
            "allow-good",
            "url",
            f"http://127.0.0.1:{port}/hop",
            ["127.0.0.1"],
        )
        good = running.ingest("allow-good-ingest", good_id)
        assert good["status"] == "SUCCEEDED", good
        assert "/hop" in server.hits and "/doc" in server.hits  # type: ignore[attr-defined]
        found = running.search("allow-search", "苹果派")
        assert found.status_code == 200, found.text + "".join(running.worker_logs[-40:])
        assert found.json()["hits"][0]["text"] == APPLE


def test_local_document_indexes_the_uploaded_artifact(role_urls, clean_db, tmp_path: Path) -> None:
    with _services(role_urls, tmp_path, "artifact") as running:
        running.bootstrap("artifact-project", "产物")
        _assert_missing_jobs_are_not_queued(running)
        artifact_id = running.upload("artifact-upload", APPLE.encode())
        source_id = running.create_source("artifact-source", "local_document", artifact_id, ["example.test"])
        job = running.ingest("artifact-ingest", source_id)
        assert job["status"] == "SUCCEEDED", job
        found = running.search("artifact-search", "苹果派")
        assert found.status_code == 200, found.text
        assert found.json()["hits"][0]["text"] == APPLE
        assert artifact_id not in found.json()["hits"][0]["text"]
        missing_id = running.create_source("artifact-missing", "local_document", str(uuid.uuid4()), [])
        missing = running.ingest("artifact-missing-ingest", missing_id)
        assert missing["status"] == "FAILED", missing
        assert missing["error_code"] == "not_found", missing
        invalid_id = running.create_source("artifact-invalid", "local_document", "notes/local.txt", [])
        invalid = running.ingest("artifact-invalid-ingest", invalid_id)
        assert invalid["status"] == "FAILED", invalid
        assert invalid["error_code"] == "artifact_missing", invalid
        other = running.http.post("/v1/projects", headers=_headers(running.csrf, "artifact-other"), json={"name": "另一个"})
        assert other.status_code == 201, other.text
        uploaded = running.http.post(
            f"/v1/projects/{other.json()['id']}/artifacts",
            headers=_headers(running.csrf, "artifact-secret"),
            files={"file": ("secret.txt", SECRET.encode(), "text/plain")},
        )
        assert uploaded.status_code == 201, uploaded.text
        foreign_id = running.create_source("artifact-foreign", "local_document", str(uploaded.json()["artifact_id"]), [])
        foreign = running.ingest("artifact-foreign-ingest", foreign_id)
        assert foreign["status"] == "FAILED", foreign
        assert foreign["error_code"] == "not_found", foreign
        leaked = running.search("artifact-leak", "苹果派")
        assert leaked.status_code == 200, leaked.text
        assert all(SECRET not in hit["text"] for hit in leaked.json()["hits"])


def test_text_pdf_is_indexed_as_extracted_text(role_urls, clean_db, tmp_path: Path) -> None:
    shown = (ROOT / "tests" / "fixtures" / "r8a" / "bake.pdf").read_bytes()
    with _services(role_urls, tmp_path, "pdf") as running:
        running.bootstrap("pdf-project", "文本")
        uploaded = running.http.post(
            f"/v1/projects/{running.project_id}/artifacts",
            headers=_headers(running.csrf, "pdf-upload"),
            files={"file": ("note.pdf", shown, "application/pdf")},
        )
        assert uploaded.status_code == 201, uploaded.text
        assert uploaded.json()["media_type"] == "application/pdf"
        source_id = running.create_source("pdf-source", "local_document", str(uploaded.json()["artifact_id"]), ["example.test"])
        job = running.ingest("pdf-ingest", source_id)
        assert job["status"] == "SUCCEEDED", job
        found = running.http.post(
            f"/v1/projects/{running.project_id}/search",
            headers=_headers(running.csrf, "pdf-search"),
            json={"project_id": running.project_id, "query": "苹果派", "mode": "lexical"},
        )
        assert found.status_code == 200, found.text
        text = found.json()["hits"][0]["text"]
        assert "苹果派烘烤说明" in text
        assert "%PDF" not in text
        assert "endstream" not in text
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("secret.txt", SECRET)
        zipped = running.http.post(
            f"/v1/projects/{running.project_id}/artifacts",
            headers=_headers(running.csrf, "zip-upload"),
            files={"file": ("notes.zip", archive.getvalue(), "application/zip")},
        )
        assert zipped.status_code == 201, zipped.text
        zip_source = running.create_source("zip-source", "local_document", str(zipped.json()["artifact_id"]), [])
        rejected = running.ingest("zip-ingest", zip_source)
        assert rejected["status"] == "FAILED", rejected
        assert rejected["error_code"] == "parse_rejected", rejected
        leaked = running.http.post(
            f"/v1/projects/{running.project_id}/search",
            headers=_headers(running.csrf, "zip-search"),
            json={"project_id": running.project_id, "query": "机密句子", "mode": "lexical"},
        )
        assert leaked.status_code == 200, leaked.text
        assert all(SECRET not in hit["text"] for hit in leaked.json()["hits"])


def test_fetch_failures_reach_a_terminal_state(role_urls, clean_db, tmp_path: Path) -> None:
    with _server() as server, _services(role_urls, tmp_path, "fetch", {"AETHER_INGEST_ALLOW_FIXTURES": "1"}) as running:
        running.bootstrap("fetch-project", "抓取")
        port = server.server_address[1]
        cases = (
            ("refused", f"http://127.0.0.1:1/refused"),
            ("tls", f"https://127.0.0.1:{port}/doc"),
            ("timeout", f"http://127.0.0.1:{port}/slow"),
        )
        for key, locator in cases:
            source_id = running.create_source(f"fetch-{key}", "url", locator, ["127.0.0.1"])
            job = running.ingest(f"fetch-{key}-ingest", source_id, timeout=45)
            assert job["status"] == "FAILED", job
            assert job["error_code"] == "fetch_failed", job
            time.sleep(0.8)
            again = running.http.get(f"/v1/projects/{running.project_id}/ingestions?limit=100")
            assert again.status_code == 200, again.text
            current = next(item for item in again.json()["items"] if item["source_id"] == source_id)
            assert current["status"] == "FAILED", current
            assert current["error_code"] == "fetch_failed", current


def test_activated_watch_records_the_approver_and_can_pause(role_urls, clean_db, tmp_path: Path) -> None:
    with _services(role_urls, tmp_path, "pause") as running:
        running.bootstrap("pause-project", "暂停")
        source_id = running.create_source("pause-source", "local_document", "draft-locator", ["example.test"])
        created = running.http.post(
            f"/v1/projects/{running.project_id}/watches",
            headers=_headers(running.csrf, "pause-watch"),
            json={
                "purpose": "跟踪",
                "source_ids": [source_id],
                "schedule": {"interval_seconds": 3600, "timezone": "Australia/Melbourne"},
                "budget": {"max_tokens": 100},
            },
        )
        assert created.status_code == 202, created.text
        watch_id = str(created.json()["resource_id"])
        draft = _poll(
            running.http,
            f"/v1/projects/{running.project_id}/watches",
            lambda body: next((item for item in body["items"] if item["id"] == watch_id and item["status"] == "DRAFT"), None),
            running.worker,
            running.worker_logs,
        )
        database = Database(role_urls["aether_worker"])
        approved = running.http.patch(
            f"/v1/projects/{running.project_id}/watches/{watch_id}",
            headers={**_headers(running.csrf, "pause-activate"), "If-Match": str(draft["version"])},
            json={"status": "ACTIVE", "expected_source_ids": [source_id]},
        )
        assert approved.status_code == 202, approved.text
        active = _poll(
            running.http,
            f"/v1/projects/{running.project_id}/watches",
            lambda body: next((item for item in body["items"] if item["id"] == watch_id and item["last_run_at"]), None),
            running.worker,
            running.worker_logs,
        )
        assert active["status"] == "ACTIVE"
        approved_by = _approved_by(database, running.owner_id, running.project_id, watch_id)
        interval = running.http.patch(
            f"/v1/projects/{running.project_id}/watches/{watch_id}",
            headers={**_headers(running.csrf, "pause-interval"), "If-Match": str(active["version"])},
            json={"interval_seconds": 7200},
        )
        assert interval.status_code == 202, interval.text
        _wait_command(database, running.owner_id, running.project_id, interval.json()["command_id"], "FAILED", running.worker, running.worker_logs)
        listed = running.http.get(f"/v1/projects/{running.project_id}/watches")
        assert listed.status_code == 200, listed.text
        current = next(item for item in listed.json()["items"] if item["id"] == watch_id)
        assert current["status"] == "ACTIVE"
        paused = running.http.patch(
            f"/v1/projects/{running.project_id}/watches/{watch_id}",
            headers={**_headers(running.csrf, "pause-stop"), "If-Match": str(current["version"])},
            json={"status": "PAUSED"},
        )
        assert paused.status_code == 202, paused.text
        pause_state = _command_terminal(
            database,
            running.owner_id,
            running.project_id,
            paused.json()["command_id"],
            running.worker,
            running.worker_logs,
        )
        listed = running.http.get(f"/v1/projects/{running.project_id}/watches")
        stopped = next(item for item in listed.json()["items"] if item["id"] == watch_id)
        schedule_id = _schedule_id(database, running.owner_id, running.project_id, watch_id)
        schedule_missing = _schedule_is_missing(schedule_id) if pause_state == "DELIVERED" else False
        approved_after = _approved_by(database, running.owner_id, running.project_id, watch_id)
        problems = [
            item
            for item, failed in (
                (f"approved_by={approved_by}", approved_by != running.owner_id),
                (f"pause={pause_state}", pause_state != "DELIVERED"),
                (f"status={stopped['status']}", stopped["status"] != "PAUSED"),
                (f"approved_after={approved_after}", approved_after != running.owner_id),
                (f"schedule={schedule_id}", not schedule_missing),
            )
            if failed
        ]
        assert not problems, problems


def test_runner_refuses_an_active_watch_without_an_approver(role_urls, clean_db, tmp_path: Path) -> None:
    with _services(role_urls, tmp_path, "unapproved") as running:
        running.bootstrap("unapproved-project", "未批准")
        source_id = running.create_source("unapproved-source", "local_document", "unapproved-locator", ["example.test"])
        created = running.http.post(
            f"/v1/projects/{running.project_id}/watches",
            headers=_headers(running.csrf, "unapproved-watch"),
            json={
                "purpose": "跟踪",
                "source_ids": [source_id],
                "schedule": {"interval_seconds": 3600, "timezone": "Australia/Melbourne"},
                "budget": {"max_tokens": 100},
            },
        )
        assert created.status_code == 202, created.text
        watch_id = str(created.json()["resource_id"])
        _poll(
            running.http,
            f"/v1/projects/{running.project_id}/watches",
            lambda body: next((item for item in body["items"] if item["id"] == watch_id and item["status"] == "DRAFT"), None),
            running.worker,
            running.worker_logs,
        )
        database = Database(role_urls["aether_worker"])
        with database.transaction(owner_id=running.owner_id, project_id=running.project_id) as conn:
            updated = conn.execute(
                text(
                    "UPDATE watches SET status = 'ACTIVE' "
                    "WHERE project_id = :project_id AND id = :id AND status = 'DRAFT' AND approved_by IS NULL"
                ),
                {"project_id": running.project_id, "id": watch_id},
            ).rowcount
        assert updated == 1
        schedule_id = _schedule_id(database, running.owner_id, running.project_id, watch_id)
        _wait_schedule_fired(schedule_id)
        listed = running.http.get(f"/v1/projects/{running.project_id}/watches")
        assert listed.status_code == 200, listed.text
        current = next(item for item in listed.json()["items"] if item["id"] == watch_id)
        assert current["status"] == "ACTIVE"
        assert current["last_run_at"] is None, current
        with database.transaction(owner_id=running.owner_id, project_id=running.project_id) as conn:
            notes = conn.execute(
                text("SELECT count(*) FROM notifications WHERE project_id = :project_id AND watch_id = :watch_id"),
                {"project_id": running.project_id, "watch_id": watch_id},
            ).scalar_one()
        assert int(notes) == 0


def _assert_missing_jobs_are_not_queued(running: _Running) -> None:
    coding = running.http.post(
        f"/v1/projects/{running.project_id}/coding-jobs",
        headers={"Origin": ORIGIN_VALUE, "X-CSRF-Token": running.csrf, "Content-Type": "application/json"},
        json={"task_id": str(uuid.uuid4()), "base_artifact_id": str(uuid.uuid4()), "job_key": "edit", "objective": "改一处"},
    )
    assert coding.status_code == 404, coding.text
    assert coding.json()["code"] == "not_found"
    skill = running.http.post(
        f"/v1/projects/{running.project_id}/skills/{uuid.uuid4()}/evaluate",
        headers={"Origin": ORIGIN_VALUE, "X-CSRF-Token": running.csrf, "Content-Type": "application/json"},
        json={"candidate_version_id": str(uuid.uuid4()), "dataset_manifest_id": str(uuid.uuid4()), "budget": {"max_tokens": 10}},
    )
    assert skill.status_code == 404, skill.text
    assert skill.json()["code"] == "not_found"
    evaluations = running.http.get(f"/v1/projects/{running.project_id}/evaluations")
    assert evaluations.status_code == 200, evaluations.text
    assert evaluations.json()["items"] == []
    worker = Database(running.role_urls["aether_worker"])
    with worker.transaction(owner_id=running.owner_id, project_id=running.project_id) as conn:
        jobs = conn.execute(
            text("SELECT count(*) FROM coding_jobs WHERE project_id = :project_id"),
            {"project_id": running.project_id},
        ).scalar_one()
        runs = conn.execute(
            text("SELECT count(*) FROM evaluation_runs WHERE project_id = :project_id"),
            {"project_id": running.project_id},
        ).scalar_one()
    assert int(jobs) == 0
    assert int(runs) == 0


def _approved_by(database: Database, owner_id: str, project_id: str, watch_id: str) -> str | None:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        found = conn.execute(
            text("SELECT approved_by::text FROM watches WHERE project_id = :project_id AND id = :id"),
            {"project_id": project_id, "id": watch_id},
        ).scalar()
    return None if found is None else str(found)


def _schedule_id(database: Database, owner_id: str, project_id: str, watch_id: str) -> str:
    with database.transaction(owner_id=owner_id, project_id=project_id) as conn:
        return str(
            conn.execute(
                text("SELECT schedule_id FROM watches WHERE project_id = :project_id AND id = :id"),
                {"project_id": project_id, "id": watch_id},
            ).scalar_one()
        )


def _command_terminal(database: Database, owner_id: str, project_id: str, command_id: str, proc, logs: list[str]) -> str:
    deadline = time.monotonic() + 30
    last = ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"worker 退出 {proc.returncode}: {''.join(logs[-40:])}")
        last = _command_status(database, owner_id, project_id, command_id) or ""
        if last in {"DELIVERED", "FAILED"}:
            return last
        time.sleep(0.2)
    raise AssertionError(f"命令停在 {last}: {''.join(logs[-40:])}")


def _schedule_is_missing(schedule_id: str) -> bool:
    async def look() -> bool:
        client = await Client.connect(_temporal_address())
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                await client.get_schedule_handle(schedule_id).describe()
            except RPCError as exc:
                if exc.status == RPCStatusCode.NOT_FOUND:
                    return True
                raise
            await asyncio.sleep(0.3)
        return False

    return asyncio.run(look())


def _wait_schedule_fired(schedule_id: str) -> None:
    async def look() -> None:
        client = await Client.connect(_temporal_address())
        deadline = time.monotonic() + 40
        last = None
        while time.monotonic() < deadline:
            try:
                described = await client.get_schedule_handle(schedule_id).describe()
            except RPCError as exc:
                if exc.status != RPCStatusCode.NOT_FOUND:
                    raise
                await asyncio.sleep(0.3)
                continue
            last = described.info
            if described.info.num_actions >= 1 and not list(described.info.running_actions):
                return
            await asyncio.sleep(0.3)
        raise AssertionError(f"schedule 没有跑完: {last}")

    asyncio.run(look())
