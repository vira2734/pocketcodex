from __future__ import annotations

import json
import secrets
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "control.db"
WEB_DIR = BASE_DIR / "web"


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


app = FastAPI(title="PocketCodex", version="0.1.0")
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


def init_db() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
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


def ensure_session(session_id: str) -> None:
    with get_connection() as conn:
        row = conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Session not found")


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


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/sessions")
def create_session(payload: SessionCreate) -> dict[str, str]:
    session_id = payload.session_id or secrets.token_urlsafe(6).replace("-", "").replace("_", "")
    session_id = session_id[:12]

    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO sessions (id)
            VALUES (?)
            """,
            (session_id,),
        )

    return {"session_id": session_id}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    ensure_session(session_id)

    with get_connection() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
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

    return {
        "session": dict(row),
        "recent_commands": [serialize_command(command) for command in commands],
    }


@app.post("/api/sessions/{session_id}/heartbeat")
def heartbeat(session_id: str, payload: SessionHeartbeat) -> dict[str, str]:
    ensure_session(session_id)
    touch_session(session_id, payload.role)
    return {"status": "ok"}


@app.post("/api/sessions/{session_id}/commands")
def create_command(session_id: str, payload: CommandCreate) -> dict[str, Any]:
    ensure_session(session_id)
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
def claim_next_command(session_id: str, payload: CommandClaim) -> dict[str, Any] | None:
    ensure_session(session_id)
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
) -> dict[str, Any]:
    ensure_session(session_id)
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

    await websocket.accept()
    active_sockets[session_id].append(websocket)

    with get_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO sessions (id) VALUES (?)", (session_id,))

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
