from __future__ import annotations

import io
import json
import os
import secrets
import socket
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

import qrcode
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from qrcode.image.svg import SvgImage


BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "control.db"
WEB_DIR = BASE_DIR / "web"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
DEFAULT_ICE_SERVERS = [{"urls": "stun:stun.l.google.com:19302"}]

try:
    ICE_SERVERS = json.loads(os.getenv("ICE_SERVERS_JSON", json.dumps(DEFAULT_ICE_SERVERS)))
except json.JSONDecodeError:
    ICE_SERVERS = DEFAULT_ICE_SERVERS


class SessionCreate(BaseModel):
    session_id: str | None = Field(default=None, min_length=4, max_length=32)


class SessionHeartbeat(BaseModel):
    role: str = Field(..., pattern="^(host|viewer|agent)$")


class CommandCreate(BaseModel):
    kind: str = Field(..., pattern="^(prompt_to_codex)$")
    text: str = Field(..., min_length=1, max_length=8000)
    submit: bool = True


class CommandClaim(BaseModel):
    agent_name: str = Field(..., min_length=1, max_length=100)


class CommandComplete(BaseModel):
    ok: bool
    detail: str = Field(..., min_length=1, max_length=12000)


app = FastAPI(title="PocketCodex", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

active_sockets: dict[str, list[WebSocket]] = defaultdict(list)


def get_connection() -> sqlite3.Connection:
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
                last_agent_seen TEXT
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


def resolve_public_base_url(request: Request | None) -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL

    scheme = resolve_request_scheme(request)
    port = resolve_request_port(request)
    host = detect_lan_ip()
    return with_port(scheme, host, port)


def resolve_local_host_base_url(request: Request | None) -> str:
    scheme = resolve_request_scheme(request)
    port = resolve_request_port(request)
    return with_port(scheme, "127.0.0.1", port)


def build_session_urls(session_id: str, access_token: str, request: Request | None) -> dict[str, str]:
    local_host_base = resolve_local_host_base_url(request)
    public_base = resolve_public_base_url(request)
    host_url = f"{local_host_base}/host.html?session={session_id}&token={access_token}"
    host_public_url = f"{public_base}/host.html?session={session_id}&token={access_token}"
    viewer_url = f"{public_base}/viewer.html?session={session_id}&token={access_token}"
    return {
        "host_url": host_url,
        "host_local_url": host_url,
        "host_public_url": host_public_url,
        "viewer_url": viewer_url,
        "host_qr_url": f"{public_base}/api/sessions/{session_id}/qr.svg?kind=host&token={access_token}",
        "viewer_qr_url": f"{public_base}/api/sessions/{session_id}/qr.svg?kind=viewer&token={access_token}",
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


def make_qr_svg(data: str) -> str:
    image = qrcode.make(data, image_factory=SvgImage, box_size=8, border=2)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode("utf-8")


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/runtime-config")
def runtime_config(request: Request) -> dict[str, Any]:
    return {
        "public_base_url": resolve_public_base_url(request),
        "local_host_base_url": resolve_local_host_base_url(request),
        "ice_servers": ICE_SERVERS,
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
) -> dict[str, Any]:
    row = get_authorized_session(session_id, x_session_token or token)

    with get_connection() as conn:
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
        "recent_commands": [serialize_command(command) for command in commands],
        "links": build_session_urls(session_id, row["access_token"], request),
    }


@app.get("/api/sessions/{session_id}/qr.svg")
def session_qr(
    session_id: str,
    kind: str = Query(pattern="^(host|viewer)$"),
    token: str | None = Query(default=None),
    request: Request = None,
) -> Response:
    row = get_authorized_session(session_id, token)
    urls = build_session_urls(session_id, row["access_token"], request)
    target = urls["host_url"] if kind == "host" else urls["viewer_url"]
    return Response(content=make_qr_svg(target), media_type="image/svg+xml")


@app.post("/api/sessions/{session_id}/heartbeat")
def heartbeat(
    session_id: str,
    payload: SessionHeartbeat,
    x_session_token: str | None = Header(default=None),
) -> dict[str, str]:
    get_authorized_session(session_id, x_session_token)
    touch_session(session_id, payload.role)
    return {"status": "ok"}


@app.post("/api/sessions/{session_id}/commands")
def create_command(
    session_id: str,
    payload: CommandCreate,
    x_session_token: str | None = Header(default=None),
) -> dict[str, Any]:
    get_authorized_session(session_id, x_session_token)
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
            WHERE session_id = ? AND status = 'queued'
            ORDER BY id ASC
            LIMIT 1
            """,
            (session_id,),
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
