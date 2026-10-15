"""Single-owner sessions. The database stores hashes, and the plaintext token stays in the cookie."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from aether_api.errors import ApiError
from aether_domain.security import hashes_match, sha256_text
from aether_storage.db import Database

HASHER = PasswordHasher()


def confine_bootstrap_path(path: Path) -> Path:
    """Bootstrap files may only live under secrets/, regardless of AETHER_BOOTSTRAP_PATH."""
    root = Path("secrets").resolve()
    root.mkdir(parents=True, exist_ok=True)
    candidate = path if path.is_absolute() else Path.cwd() / path
    candidate = candidate.resolve()
    if candidate != root and root not in candidate.parents:
        raise ApiError(403, "bootstrap_path_rejected", "引导文件必须位于 secrets/")
    return candidate


def issue_bootstrap_secret(path: Path) -> str:
    path = confine_bootstrap_path(path)
    secret = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"secret_sha256": sha256_text(secret), "consumed": False}
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return secret


def _load_bootstrap(path: Path) -> dict[str, Any]:
    path = confine_bootstrap_path(path)
    if not path.is_file():
        raise ApiError(403, "bootstrap_unavailable", "引导密钥不存在")
    return json.loads(path.read_text(encoding="utf-8"))


def _save_bootstrap(path: Path, payload: dict[str, Any]) -> None:
    path = confine_bootstrap_path(path)
    path.write_text(json.dumps(payload), encoding="utf-8")


class AuthService:
    def __init__(self, database: Database, *, session_hours: int, idle_minutes: int) -> None:
        self.database = database
        self.session_hours = session_hours
        self.idle_minutes = idle_minutes

    def bootstrap(self, path: Path, secret: str, display_name: str, passphrase: str) -> dict[str, Any]:
        if len(passphrase) < 12:
            raise ApiError(422, "invalid_passphrase", "口令至少 12 个字符")
        if not display_name.strip():
            raise ApiError(422, "invalid_name", "显示名不能为空")
        record = _load_bootstrap(path)
        if record.get("consumed") is True:
            raise ApiError(409, "bootstrap_consumed", "引导密钥已使用")
        if not hashes_match(str(record.get("secret_sha256")), secret):
            raise ApiError(401, "bootstrap_rejected", "引导密钥不正确")
        try:
            with self.database.transaction() as conn:
                existing = conn.execute(text("SELECT id FROM owners")).first()
                if existing is not None:
                    raise ApiError(409, "owner_exists", "已经有所有者")
                owner = conn.execute(
                    text(
                        "INSERT INTO owners (display_name, password_hash) VALUES (:name, :password_hash) "
                        "RETURNING id::text AS id, display_name"
                    ),
                    {"name": display_name.strip(), "password_hash": HASHER.hash(passphrase)},
                ).mappings().one()
                session = self._insert_session(conn, owner["id"])
        except IntegrityError as exc:
            raise ApiError(409, "owner_exists", "已经有所有者") from exc
        record["consumed"] = True
        _save_bootstrap(path, record)
        return self._public_session(owner["id"], owner["display_name"], session)

    def login(self, passphrase: str) -> dict[str, Any]:
        with self.database.transaction() as conn:
            owner = conn.execute(text("SELECT id::text AS id, display_name, password_hash FROM owners")).mappings().first()
            if owner is None:
                raise ApiError(401, "login_failed", "口令不正确")
            try:
                HASHER.verify(owner["password_hash"], passphrase)
            except VerifyMismatchError as exc:
                raise ApiError(401, "login_failed", "口令不正确") from exc
            session = self._insert_session(conn, owner["id"])
        return self._public_session(owner["id"], owner["display_name"], session)

    def authenticate(self, token: str) -> dict[str, str]:
        """Validate a session for a read-only route. Does not rotate the CSRF token."""
        with self.database.transaction() as conn:
            row = self._locked_session(conn, token)
            conn.execute(text("UPDATE auth_sessions SET last_seen_at = now() WHERE id = :id"), {"id": row["id"]})
            return {"owner_id": row["owner_id"], "display_name": row["display_name"]}

    def current(self, token: str) -> dict[str, Any]:
        """Return the session and rotate CSRF. Only GET /v1/auth/session should call this."""
        with self.database.transaction() as conn:
            row = self._locked_session(conn, token)
            csrf = secrets.token_urlsafe(32)
            conn.execute(
                text("UPDATE auth_sessions SET last_seen_at = now(), csrf_secret_hash = :csrf WHERE id = :id"),
                {"csrf": sha256_text(csrf), "id": row["id"]},
            )
            expires = row["expires_at"]
        return {
            "owner_id": row["owner_id"],
            "display_name": row["display_name"],
            "csrf_token": csrf,
            "expires_at": expires,
            "token": token,
        }

    def logout(self, token: str, csrf_token: str) -> None:
        with self.database.transaction() as conn:
            row = self._locked_session(conn, token)
            if not hashes_match(row["csrf_secret_hash"], csrf_token):
                raise ApiError(403, "csrf_rejected", "CSRF 校验失败")
            conn.execute(text("UPDATE auth_sessions SET revoked_at = now() WHERE id = :id"), {"id": row["id"]})

    def require_csrf(self, token: str, csrf_token: str | None) -> dict[str, str]:
        if not csrf_token:
            raise ApiError(403, "csrf_required", "缺少 CSRF 令牌")
        with self.database.transaction() as conn:
            row = self._locked_session(conn, token)
            if not hashes_match(row["csrf_secret_hash"], csrf_token):
                raise ApiError(403, "csrf_rejected", "CSRF 校验失败")
            return {"owner_id": row["owner_id"], "display_name": row["display_name"]}

    def _insert_session(self, conn: Any, owner_id: str) -> dict[str, Any]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        expires = datetime.now(UTC) + timedelta(hours=self.session_hours)
        row = conn.execute(
            text(
                "INSERT INTO auth_sessions (owner_id, token_hash, csrf_secret_hash, expires_at) "
                "VALUES (:owner_id, :token_hash, :csrf_hash, :expires_at) "
                "RETURNING expires_at"
            ),
            {
                "owner_id": owner_id,
                "token_hash": sha256_text(token),
                "csrf_hash": sha256_text(csrf),
                "expires_at": expires,
            },
        ).mappings().one()
        return {"token": token, "csrf_token": csrf, "expires_at": row["expires_at"]}

    def _locked_session(self, conn: Any, token: str) -> dict[str, Any]:
        row = conn.execute(
            text(
                "SELECT s.id::text AS id, s.owner_id::text AS owner_id, s.csrf_secret_hash, s.expires_at, "
                "s.last_seen_at, s.revoked_at, o.display_name "
                "FROM auth_sessions s JOIN owners o ON o.id = s.owner_id WHERE s.token_hash = :token_hash"
            ),
            {"token_hash": sha256_text(token)},
        ).mappings().first()
        if row is None or row["revoked_at"] is not None:
            raise ApiError(401, "unauthenticated", "未登录")
        now = datetime.now(UTC)
        if row["expires_at"] <= now or now - row["last_seen_at"] > timedelta(minutes=self.idle_minutes):
            self._revoke_expired(row["id"])
            raise ApiError(401, "session_expired", "会话已过期")
        return dict(row)

    def _revoke_expired(self, session_id: str) -> None:
        with self.database.transaction() as conn:
            conn.execute(
                text("UPDATE auth_sessions SET revoked_at = now() WHERE id = :id AND revoked_at IS NULL"),
                {"id": session_id},
            )

    @staticmethod
    def _public_session(owner_id: str, display_name: str, session: dict[str, Any]) -> dict[str, Any]:
        return {
            "owner_id": owner_id,
            "display_name": display_name,
            "csrf_token": session["csrf_token"],
            "expires_at": session["expires_at"],
            "token": session["token"],
        }
