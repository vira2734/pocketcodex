from __future__ import annotations

import io
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import qrcode
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from qrcode.image.svg import SvgImage


def resolve_base_dir() -> Path:
    override = os.getenv("POCKETCODEX_BASE_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


BASE_DIR = resolve_base_dir()
DB_PATH = Path(os.getenv("POCKETCODEX_DB_PATH", str(BASE_DIR / "control.db"))).expanduser()
WEB_DIR = Path(os.getenv("POCKETCODEX_WEB_DIR", str(BASE_DIR / "web"))).expanduser()
AGENT_LOG_DIR = DB_PATH.parent / "logs"
CLAIM_STALE_AFTER_SECONDS = 30
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
DEFAULT_ICE_SERVERS = [{"urls": "stun:stun.l.google.com:19302"}]
CONTROL_LEASE_SECONDS = int(os.getenv("CONTROL_LEASE_SECONDS", "45"))
REMOTE_TRIAL_START_ATTEMPTS = max(1, int(os.getenv("REMOTE_TRIAL_START_ATTEMPTS", "2")))

try:
    ICE_SERVERS = json.loads(os.getenv("ICE_SERVERS_JSON", json.dumps(DEFAULT_ICE_SERVERS)))
except json.JSONDecodeError:
    ICE_SERVERS = DEFAULT_ICE_SERVERS


class SessionCreate(BaseModel):
    session_id: str | None = Field(default=None, min_length=4, max_length=32)


class SessionHeartbeat(BaseModel):
    role: str = Field(..., pattern="^(host|viewer|agent)$")


class CommandCreate(BaseModel):
    kind: str = Field(..., pattern="^(prompt_to_codex|focus_codex|interrupt_codex)$")
    text: str = Field(default="", max_length=8000)
    submit: bool = True

    @model_validator(mode="after")
    def validate_payload(self) -> "CommandCreate":
        if self.kind == "prompt_to_codex" and not self.text.strip():
            raise ValueError("Prompt text is required for prompt_to_codex commands.")
        return self


class CommandClaim(BaseModel):
    agent_name: str = Field(..., min_length=1, max_length=100)


class CommandComplete(BaseModel):
    ok: bool
    detail: str = Field(..., min_length=1, max_length=12000)


class ControlAcquire(BaseModel):
    viewer_id: str = Field(..., min_length=8, max_length=128)
    label: str = Field(default="Phone", min_length=1, max_length=100)


class ControlRelease(BaseModel):
    viewer_id: str = Field(..., min_length=8, max_length=128)


app = FastAPI(title="Pocket Mac", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

active_sockets: dict[str, list[WebSocket]] = defaultdict(list)


def utc_timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


class RemoteTrialManager:
    TUNNEL_HOST_PATTERN = re.compile(
        r"https://[A-Za-z0-9.-]+\.(?:trycloudflare\.com|[A-Za-z0-9-]+\.ts\.net|loca\.lt)(?:/[^\s|]*)?"
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._process: subprocess.Popen[str] | None = None
        self._public_url: str | None = None
        self._provider: str | None = None
        self._started_at: float | None = None
        self._last_error: str | None = None
        self._log_lines: list[str] = []

    def _append_log(self, line: str) -> None:
        clean = line.strip()
        if not clean:
            return
        self._log_lines.append(clean)
        self._log_lines = self._log_lines[-20:]

    def _snapshot_locked(self, include_logs: bool = False) -> dict[str, Any]:
        active = self._process is not None and self._process.poll() is None and self._public_url is not None
        starting = self._process is not None and self._process.poll() is None and self._public_url is None
        snapshot = {
            "active": active,
            "starting": starting,
            "provider": self._provider,
            "public_url": self._public_url,
            "started_at": utc_timestamp(self._started_at),
            "last_error": self._last_error,
        }
        if include_logs:
            snapshot["logs"] = list(self._log_lines)
        return snapshot

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._reap_locked()
            return self._snapshot_locked()

    def public_url(self) -> str | None:
        with self._lock:
            self._reap_locked()
            return self._public_url

    def _reap_locked(self) -> None:
        if self._process is None:
            return
        if self._process.poll() is None:
            return
        if self._public_url is None and self._last_error is None:
            self._last_error = "Remote trial tunnel exited before publishing a URL."
        self._clear_process_locked(keep_url=False)

    def _clear_process_locked(self, keep_url: bool) -> None:
        self._process = None
        if not keep_url:
            self._public_url = None
            self._provider = None
            self._started_at = None
        self._condition.notify_all()

    def _terminate_locked(self) -> None:
        process = self._process
        self._clear_process_locked(keep_url=False)
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        except OSError:
            return

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._terminate_locked()
            self._last_error = None
            return self._snapshot_locked()

    def _command_candidates(self, target_url: str) -> list[tuple[list[str], str]]:
        candidates: list[tuple[list[str], str]] = []
        parsed_target = urlparse(target_url)
        target_port = str(parsed_target.port or (443 if parsed_target.scheme == "https" else 80))
        custom = os.getenv("POCKETCODEX_TUNNEL_COMMAND", "").strip()
        if custom:
            command = [part.format(target=target_url) for part in shlex.split(custom)]
            return [(command, "custom")]

        bundled_cloudflared = os.getenv("POCKETMAC_CLOUDFLARED", "").strip()
        if bundled_cloudflared and Path(bundled_cloudflared).exists():
            candidates.append(([bundled_cloudflared, "tunnel", "--url", target_url], "cloudflared"))

        bundled_node = os.getenv("POCKETMAC_NODE", "").strip()
        bundled_localtunnel = os.getenv("POCKETMAC_LOCALTUNNEL_ENTRY", "").strip()
        if bundled_node and bundled_localtunnel and Path(bundled_node).exists() and Path(bundled_localtunnel).exists():
            candidates.append(([bundled_node, bundled_localtunnel, "--port", target_port], "localtunnel"))

        cloudflared = shutil.which("cloudflared")
        if cloudflared:
            candidates.append(([cloudflared, "tunnel", "--url", target_url], "cloudflared"))

        wrangler = shutil.which("wrangler")
        if wrangler:
            candidates.append(([wrangler, "tunnel", "quick-start", target_url], "wrangler"))

        npx = shutil.which("npx")
        if npx:
            candidates.append(([npx, "--yes", "wrangler@latest", "tunnel", "quick-start", target_url], "wrangler"))
            candidates.append(([npx, "--yes", "localtunnel", "--port", target_port], "localtunnel"))

        if not candidates:
            raise RuntimeError("No tunnel launcher found. Install cloudflared or use npx.")
        return candidates

    def _watch_process(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            with self._lock:
                if self._process is not process:
                    return
                self._append_log(line)
                if self._public_url is None:
                    match = self.TUNNEL_HOST_PATTERN.search(line)
                    if match:
                        self._public_url = match.group(0).rstrip("/").rstrip("|")
                        self._condition.notify_all()

        return_code = process.wait()
        with self._lock:
            if self._process is not process:
                return
            if return_code != 0 and self._last_error is None:
                self._last_error = f"Remote trial tunnel exited with status {return_code}."
            self._clear_process_locked(keep_url=False)

    def start(self, target_url: str) -> dict[str, Any]:
        with self._lock:
            self._reap_locked()
            if self._process is not None and self._process.poll() is None:
                return self._snapshot_locked()

            attempts: list[str] = []
            for command, provider in self._command_candidates(target_url):
                self._last_error = None
                self._log_lines = []
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
                self._process = process
                self._provider = provider
                self._started_at = time.time()
                self._public_url = None
                threading.Thread(target=self._watch_process, args=(process,), daemon=True).start()

                deadline = time.time() + 25
                while self._public_url is None and self._last_error is None and time.time() < deadline:
                    remaining = deadline - time.time()
                    self._condition.wait(timeout=min(remaining, 1))
                    self._reap_locked()

                if self._public_url is not None:
                    return self._snapshot_locked()

                error_message = self._last_error or "Timed out waiting for the remote trial tunnel URL."
                attempts.append(f"{provider}: {error_message}")
                self._terminate_locked()

            self._last_error = " | ".join(attempts) if attempts else "Timed out waiting for the remote trial tunnel URL."
            raise RuntimeError(self._last_error)


remote_trial_manager = RemoteTrialManager()


class LocalAgentManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen[str]] = {}

    def _reap_locked(self) -> None:
        stale = [session_id for session_id, process in self._processes.items() if process.poll() is not None]
        for session_id in stale:
            self._processes.pop(session_id, None)

    def start(self, session_id: str, token: str, base_url: str, app_name: str = "Codex") -> dict[str, Any]:
        with self._lock:
            self._reap_locked()
            existing = self._processes.get(session_id)
            if existing is not None and existing.poll() is None:
                return {"running": True, "started": False, "pid": existing.pid}

            runtime_executable = os.getenv("POCKETCODEX_RUNTIME_EXECUTABLE", "").strip()
            runtime_script = os.getenv("POCKETCODEX_RUNTIME_SCRIPT", "").strip()
            if runtime_executable:
                command = [runtime_executable]
                if runtime_script:
                    command.append(runtime_script)
                command.extend(
                    [
                        "--agent-mode",
                        "--session",
                        session_id,
                        "--token",
                        token,
                        "--base-url",
                        base_url,
                        "--poll-seconds",
                        "0.5",
                        "--app-name",
                        app_name,
                    ]
                )
            else:
                command = [
                    sys.executable,
                    str(BASE_DIR / "mac_agent.py"),
                    "--session",
                    session_id,
                    "--token",
                    token,
                    "--base-url",
                    base_url,
                    "--poll-seconds",
                    "0.5",
                    "--app-name",
                    app_name,
                ]
            AGENT_LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_path = AGENT_LOG_DIR / f"agent-{session_id}.log"
            log_handle = open(log_path, "a", encoding="utf-8")
            log_handle.write(
                f"\n=== starting agent session={session_id} pid-parent={os.getpid()} at {datetime.now(timezone.utc).isoformat()} ===\n"
            )
            log_handle.flush()
            agent_env = os.environ.copy()
            agent_env["PYTHONUNBUFFERED"] = "1"

            process = subprocess.Popen(
                command,
                cwd=BASE_DIR,
                env=agent_env,
                stdout=log_handle,
                stderr=log_handle,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            log_handle.close()
            self._processes[session_id] = process
            return {"running": True, "started": True, "pid": process.pid}

    def stop_all(self) -> None:
        with self._lock:
            processes = list(self._processes.values())
            self._processes.clear()

        for process in processes:
            try:
                process.terminate()
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            except OSError:
                continue


local_agent_manager = LocalAgentManager()


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def generate_access_token() -> str:
    return secrets.token_urlsafe(24)


def get_session_columns(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("PRAGMA table_info(sessions)").fetchall()
    return {row["name"] for row in rows}


def ensure_session_schema(conn: sqlite3.Connection) -> None:
    columns = get_session_columns(conn)
    if "access_token" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN access_token TEXT")
    if "controller_viewer_id" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN controller_viewer_id TEXT")
    if "controller_label" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN controller_label TEXT")
    if "controller_acquired_at" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN controller_acquired_at TEXT")
    if "controller_last_seen" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN controller_last_seen TEXT")

    rows = conn.execute("SELECT id, access_token FROM sessions").fetchall()
    for row in rows:
        if not row["access_token"]:
            conn.execute(
                "UPDATE sessions SET access_token = ? WHERE id = ?",
                (generate_access_token(), row["id"]),
            )


def init_db() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                access_token TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_host_seen TEXT,
                last_viewer_seen TEXT,
                last_agent_seen TEXT,
                controller_viewer_id TEXT,
                controller_label TEXT,
                controller_acquired_at TEXT,
                controller_last_seen TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                claimed_by TEXT,
                result_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(session_id) REFERENCES sessions(id)
            )
            """
        )
        ensure_session_schema(conn)


def serialize_command(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload_json"])
    result = json.loads(row["result_json"]) if row["result_json"] else None
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "kind": row["kind"],
        "payload": payload,
        "status": row["status"],
        "claimed_by": row["claimed_by"],
        "result": result,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def parse_db_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def get_controller_state(session_row: sqlite3.Row | dict[str, Any], viewer_id: str | None = None) -> dict[str, Any]:
    controller_viewer_id = session_row["controller_viewer_id"]
    controller_last_seen = parse_db_timestamp(session_row["controller_last_seen"])
    active = False
    if controller_viewer_id and controller_last_seen is not None:
        active = (datetime.now(timezone.utc) - controller_last_seen).total_seconds() <= CONTROL_LEASE_SECONDS

    return {
        "active": active,
        "viewer_id": controller_viewer_id if active else None,
        "label": session_row["controller_label"] if active else None,
        "acquired_at": session_row["controller_acquired_at"] if active else None,
        "last_seen": session_row["controller_last_seen"] if active else None,
        "lease_seconds": CONTROL_LEASE_SECONDS,
        "is_current_viewer": bool(active and viewer_id and controller_viewer_id == viewer_id),
    }


def clear_controller_if_stale(conn: sqlite3.Connection, session_row: sqlite3.Row) -> sqlite3.Row:
    controller_state = get_controller_state(session_row)
    if controller_state["active"] or not session_row["controller_viewer_id"]:
        return session_row

    conn.execute(
        """
        UPDATE sessions
        SET controller_viewer_id = NULL,
            controller_label = NULL,
            controller_acquired_at = NULL,
            controller_last_seen = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (session_row["id"],),
    )
    return conn.execute("SELECT * FROM sessions WHERE id = ?", (session_row["id"],)).fetchone()


def require_session_control(session_row: sqlite3.Row, viewer_id: str | None) -> None:
    if not viewer_id:
        raise HTTPException(status_code=409, detail="Take control from the viewer before sending commands.")
    controller_state = get_controller_state(session_row, viewer_id)
    if not controller_state["active"]:
        raise HTTPException(status_code=409, detail="No active controller. Take control from the viewer first.")
    if not controller_state["is_current_viewer"]:
        raise HTTPException(status_code=409, detail="Another viewer currently controls this session.")


def detect_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def resolve_request_scheme(request: Request | None) -> str:
    if request is None:
        return "http"
    return request.url.scheme


def resolve_request_port(request: Request | None) -> int:
    if request is None:
        return 8000
    if request.url.port is not None:
        return request.url.port
    return 443 if request.url.scheme == "https" else 80


def with_port(scheme: str, host: str, port: int) -> str:
    default_port = 443 if scheme == "https" else 80
    if port == default_port:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def resolve_lan_base_url(request: Request | None) -> str:
    scheme = resolve_request_scheme(request)
    port = resolve_request_port(request)
    host = detect_lan_ip()
    return with_port(scheme, host, port)


def resolve_public_base_url(request: Request | None) -> str:
    remote_trial_url = remote_trial_manager.public_url()
    if remote_trial_url:
        return remote_trial_url

    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL

    return resolve_lan_base_url(request)


def resolve_local_host_base_url(request: Request | None) -> str:
    scheme = resolve_request_scheme(request)
    port = resolve_request_port(request)
    return with_port(scheme, "127.0.0.1", port)


def build_session_urls(session_id: str, access_token: str, request: Request | None) -> dict[str, str]:
    local_host_base = resolve_local_host_base_url(request)
    lan_base = resolve_lan_base_url(request)
    public_base = resolve_public_base_url(request)
    host_url = f"{local_host_base}/host.html?session={session_id}&token={access_token}"
    host_public_url = f"{public_base}/host.html?session={session_id}&token={access_token}"
    viewer_url = f"{public_base}/viewer.html?session={session_id}&token={access_token}"
    viewer_lan_url = f"{lan_base}/viewer.html?session={session_id}&token={access_token}"
    return {
        "host_url": host_url,
        "host_local_url": host_url,
        "host_public_url": host_public_url,
        "viewer_url": viewer_url,
        "viewer_public_url": viewer_url,
        "viewer_lan_url": viewer_lan_url,
        "host_qr_url": f"{public_base}/api/sessions/{session_id}/qr.svg?kind=host&token={access_token}",
        "viewer_qr_url": f"{public_base}/api/sessions/{session_id}/qr.svg?kind=viewer&token={access_token}",
        "viewer_lan_qr_url": f"{lan_base}/api/sessions/{session_id}/qr.svg?kind=viewer_lan&token={access_token}",
    }


def get_authorized_session(session_id: str, token: str | None) -> sqlite3.Row:
    if not token:
        raise HTTPException(status_code=401, detail="Missing session token")

    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ? AND access_token = ?",
            (session_id, token),
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=401, detail="Invalid session token")

    return row


def touch_session(session_id: str, role: str) -> None:
    column = {
        "host": "last_host_seen",
        "viewer": "last_viewer_seen",
        "agent": "last_agent_seen",
    }[role]

    with get_connection() as conn:
        conn.execute(
            f"""
            UPDATE sessions
            SET {column} = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (session_id,),
        )


def touch_controller(session_id: str, viewer_id: str) -> None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            return
        row = clear_controller_if_stale(conn, row)
        if row["controller_viewer_id"] != viewer_id:
            return
        conn.execute(
            """
            UPDATE sessions
            SET controller_last_seen = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (session_id,),
        )


def make_qr_svg(data: str) -> str:
    image = qrcode.make(data, image_factory=SvgImage, box_size=8, border=2)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode("utf-8")


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.on_event("shutdown")
def on_shutdown() -> None:
    remote_trial_manager.stop()
    local_agent_manager.stop_all()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/runtime-config")
def runtime_config(request: Request) -> dict[str, Any]:
    return {
        "public_base_url": resolve_public_base_url(request),
        "lan_base_url": resolve_lan_base_url(request),
        "local_host_base_url": resolve_local_host_base_url(request),
        "ice_servers": ICE_SERVERS,
        "remote_trial": remote_trial_manager.status(),
    }


@app.post("/api/sessions")
def create_session(payload: SessionCreate, request: Request) -> dict[str, Any]:
    session_id = payload.session_id or secrets.token_urlsafe(6).replace("-", "").replace("_", "")
    session_id = session_id[:12]
    access_token = generate_access_token()

    with get_connection() as conn:
        existing = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail="Session id already exists. Reuse the original tokenized host/viewer links.",
            )

        conn.execute(
            """
            INSERT INTO sessions (id, access_token)
            VALUES (?, ?)
            """,
            (session_id, access_token),
        )

    return {
        "session_id": session_id,
        "access_token": access_token,
        **build_session_urls(session_id, access_token, request),
    }


@app.get("/api/sessions/{session_id}")
def get_session(
    session_id: str,
    request: Request,
    x_session_token: str | None = Header(default=None),
    token: str | None = Query(default=None),
    viewer_id: str | None = Query(default=None),
) -> dict[str, Any]:
    row = get_authorized_session(session_id, x_session_token or token)

    with get_connection() as conn:
        row = clear_controller_if_stale(conn, row)
        commands = conn.execute(
            """
            SELECT *
            FROM commands
            WHERE session_id = ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (session_id,),
        ).fetchall()

    session = dict(row)
    session.pop("access_token", None)
    return {
        "session": session,
        "controller": get_controller_state(row, viewer_id),
        "recent_commands": [serialize_command(command) for command in commands],
        "links": build_session_urls(session_id, row["access_token"], request),
    }


@app.get("/api/sessions/{session_id}/qr.svg")
def session_qr(
    session_id: str,
    kind: str = Query(pattern="^(host|viewer|viewer_lan)$"),
    token: str | None = Query(default=None),
    request: Request = None,
) -> Response:
    row = get_authorized_session(session_id, token)
    urls = build_session_urls(session_id, row["access_token"], request)
    if kind == "host":
        target = urls["host_url"]
    elif kind == "viewer_lan":
        target = urls["viewer_lan_url"]
    else:
        target = urls["viewer_url"]
    return Response(content=make_qr_svg(target), media_type="image/svg+xml")


@app.get("/api/remote-trial")
def remote_trial_status() -> dict[str, Any]:
    return remote_trial_manager.status()


@app.post("/api/remote-trial/start")
def start_remote_trial(request: Request) -> dict[str, Any]:
    target_url = resolve_local_host_base_url(request)
    try:
        remote_trial = remote_trial_manager.start(target_url)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return remote_trial


@app.post("/api/remote-trial/stop")
def stop_remote_trial() -> dict[str, Any]:
    return remote_trial_manager.stop()


@app.post("/api/sessions/{session_id}/host/prepare")
def prepare_host(
    session_id: str,
    request: Request,
    x_session_token: str | None = Header(default=None),
) -> dict[str, Any]:
    row = get_authorized_session(session_id, x_session_token)
    remote_trial: dict[str, Any] | None = None
    for _ in range(REMOTE_TRIAL_START_ATTEMPTS):
        try:
            remote_trial = remote_trial_manager.start(resolve_local_host_base_url(request))
        except RuntimeError:
            remote_trial = remote_trial_manager.status()
        if remote_trial and remote_trial.get("active"):
            break
    agent = local_agent_manager.start(
        session_id=session_id,
        token=row["access_token"],
        base_url=resolve_local_host_base_url(request),
    )
    return {
        "session_id": session_id,
        "agent": agent,
        "remote_trial": remote_trial,
        "links": build_session_urls(session_id, row["access_token"], request),
    }


@app.post("/api/sessions/{session_id}/heartbeat")
def heartbeat(
    session_id: str,
    payload: SessionHeartbeat,
    x_session_token: str | None = Header(default=None),
    x_viewer_id: str | None = Header(default=None),
) -> dict[str, str]:
    get_authorized_session(session_id, x_session_token)
    touch_session(session_id, payload.role)
    if payload.role == "viewer" and x_viewer_id:
        touch_controller(session_id, x_viewer_id)
    return {"status": "ok"}


@app.post("/api/sessions/{session_id}/control/acquire")
def acquire_control(
    session_id: str,
    payload: ControlAcquire,
    x_session_token: str | None = Header(default=None),
) -> dict[str, Any]:
    get_authorized_session(session_id, x_session_token)

    with get_connection() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Session not found")
        row = clear_controller_if_stale(conn, row)
        controller_state = get_controller_state(row, payload.viewer_id)
        if controller_state["active"] and not controller_state["is_current_viewer"]:
            raise HTTPException(
                status_code=409,
                detail=f"{controller_state['label'] or 'Another viewer'} currently controls this session.",
            )

        acquired_at = row["controller_acquired_at"]
        if row["controller_viewer_id"] != payload.viewer_id or not acquired_at:
            acquired_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

        conn.execute(
            """
            UPDATE sessions
            SET controller_viewer_id = ?,
                controller_label = ?,
                controller_acquired_at = ?,
                controller_last_seen = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (payload.viewer_id, payload.label, acquired_at, session_id),
        )
        updated = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()

    return {"controller": get_controller_state(updated, payload.viewer_id)}


@app.post("/api/sessions/{session_id}/control/release")
def release_control(
    session_id: str,
    payload: ControlRelease,
    x_session_token: str | None = Header(default=None),
) -> dict[str, Any]:
    get_authorized_session(session_id, x_session_token)

    with get_connection() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Session not found")
        row = clear_controller_if_stale(conn, row)
        if row["controller_viewer_id"] and row["controller_viewer_id"] != payload.viewer_id:
            raise HTTPException(status_code=409, detail="Only the active controller can release control.")

        conn.execute(
            """
            UPDATE sessions
            SET controller_viewer_id = NULL,
                controller_label = NULL,
                controller_acquired_at = NULL,
                controller_last_seen = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (session_id,),
        )
        updated = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()

    return {"controller": get_controller_state(updated, payload.viewer_id)}


@app.post("/api/sessions/{session_id}/commands")
def create_command(
    session_id: str,
    payload: CommandCreate,
    x_session_token: str | None = Header(default=None),
    x_viewer_id: str | None = Header(default=None),
) -> dict[str, Any]:
    row = get_authorized_session(session_id, x_session_token)
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Session not found")
        row = clear_controller_if_stale(conn, row)
    require_session_control(row, x_viewer_id)
    serialized = json.dumps(payload.model_dump())

    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO commands (session_id, kind, payload_json)
            VALUES (?, ?, ?)
            """,
            (session_id, payload.kind, serialized),
        )
        row = conn.execute("SELECT * FROM commands WHERE id = ?", (cursor.lastrowid,)).fetchone()

    return serialize_command(row)


@app.post("/api/sessions/{session_id}/commands/claim-next")
def claim_next_command(
    session_id: str,
    payload: CommandClaim,
    x_session_token: str | None = Header(default=None),
) -> dict[str, Any] | None:
    get_authorized_session(session_id, x_session_token)
    touch_session(session_id, "agent")

    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM commands
            WHERE session_id = ?
              AND (
                status = 'queued'
                OR (status = 'claimed' AND updated_at <= datetime('now', ?))
              )
            ORDER BY id ASC
            LIMIT 1
            """,
            (session_id, f"-{CLAIM_STALE_AFTER_SECONDS} seconds"),
        ).fetchone()

        if row is None:
            return None

        conn.execute(
            """
            UPDATE commands
            SET status = 'claimed',
                claimed_by = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (payload.agent_name, row["id"]),
        )
        updated = conn.execute("SELECT * FROM commands WHERE id = ?", (row["id"],)).fetchone()

    return serialize_command(updated)


@app.post("/api/sessions/{session_id}/commands/{command_id}/complete")
def complete_command(
    session_id: str,
    command_id: int,
    payload: CommandComplete,
    x_session_token: str | None = Header(default=None),
) -> dict[str, Any]:
    get_authorized_session(session_id, x_session_token)
    touch_session(session_id, "agent")
    result_json = json.dumps(payload.model_dump())

    with get_connection() as conn:
        existing = conn.execute(
            "SELECT * FROM commands WHERE id = ? AND session_id = ?",
            (command_id, session_id),
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Command not found")

        conn.execute(
            """
            UPDATE commands
            SET status = 'completed',
                result_json = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (result_json, command_id),
        )
        row = conn.execute("SELECT * FROM commands WHERE id = ?", (command_id,)).fetchone()

    return serialize_command(row)


@app.websocket("/ws/session/{session_id}/{role}")
async def session_socket(websocket: WebSocket, session_id: str, role: str) -> None:
    if role not in {"host", "viewer"}:
        await websocket.close(code=1008)
        return

    token = websocket.query_params.get("token")
    try:
        get_authorized_session(session_id, token)
    except HTTPException:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    active_sockets[session_id].append(websocket)
    touch_session(session_id, role)

    try:
        await websocket.send_json({"type": "socket-ready", "role": role, "session_id": session_id})
        while True:
            message = await websocket.receive_json()
            message["role"] = role
            for peer in list(active_sockets[session_id]):
                if peer is not websocket:
                    await peer.send_json(message)
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in active_sockets[session_id]:
            active_sockets[session_id].remove(websocket)
        if not active_sockets[session_id]:
            active_sockets.pop(session_id, None)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
