from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from pathlib import Path

from websockets.sync.client import connect as ws_connect


BASE_DIR = Path(__file__).resolve().parent.parent


def fetch_json(url: str, method: str = "GET", payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read().decode("utf-8")


def wait_for_server(url: str, attempts: int = 30) -> None:
    for _ in range(attempts):
        try:
            data = fetch_json(url)
            if data.get("status") == "ok":
                return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("Server did not start in time")


def main() -> None:
    process = subprocess.Popen(
        ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8011"],
        cwd=BASE_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        wait_for_server("http://127.0.0.1:8011/api/health")

        session = fetch_json(
            "http://127.0.0.1:8011/api/sessions",
            method="POST",
            payload={"session_id": "smoke123"},
        )
        assert session["session_id"] == "smoke123"

        session_state = fetch_json("http://127.0.0.1:8011/api/sessions/smoke123")
        assert session_state["session"]["id"] == "smoke123"

        heartbeat = fetch_json(
            "http://127.0.0.1:8011/api/sessions/smoke123/heartbeat",
            method="POST",
            payload={"role": "viewer"},
        )
        assert heartbeat["status"] == "ok"

        command = fetch_json(
            "http://127.0.0.1:8011/api/sessions/smoke123/commands",
            method="POST",
            payload={
                "kind": "prompt_to_codex",
                "text": "Hello from smoke test",
                "submit": True,
            },
        )
        assert command["status"] == "queued"

        claimed = fetch_json(
            "http://127.0.0.1:8011/api/sessions/smoke123/commands/claim-next",
            method="POST",
            payload={"agent_name": "smoke-agent"},
        )
        assert claimed["status"] == "claimed"
        assert claimed["payload"]["text"] == "Hello from smoke test"

        completed = fetch_json(
            f"http://127.0.0.1:8011/api/sessions/smoke123/commands/{claimed['id']}/complete",
            method="POST",
            payload={"ok": True, "detail": "Smoke test complete"},
        )
        assert completed["status"] == "completed"
        assert completed["result"]["detail"] == "Smoke test complete"

        refreshed = fetch_json("http://127.0.0.1:8011/api/sessions/smoke123")
        assert refreshed["session"]["last_viewer_seen"] is not None

        with ws_connect("ws://127.0.0.1:8011/ws/session/smoke123/viewer") as viewer_ws:
            ready = json.loads(viewer_ws.recv())
            assert ready["type"] == "socket-ready"

            with ws_connect("ws://127.0.0.1:8011/ws/session/smoke123/host") as host_ws:
                host_ready = json.loads(host_ws.recv())
                assert host_ready["type"] == "socket-ready"

                viewer_ws.send(json.dumps({"type": "viewer-ready"}))
                relayed_to_host = json.loads(host_ws.recv())
                assert relayed_to_host["type"] == "viewer-ready"
                assert relayed_to_host["role"] == "viewer"

                host_ws.send(json.dumps({"type": "offer", "sdp": {"type": "offer", "sdp": "fake"}}))
                offer = json.loads(viewer_ws.recv())
                assert offer["type"] == "offer"
                assert offer["role"] == "host"

        index_html = fetch_text("http://127.0.0.1:8011/")
        host_html = fetch_text("http://127.0.0.1:8011/host.html")
        viewer_html = fetch_text("http://127.0.0.1:8011/viewer.html")
        assert "Create Session" in index_html
        assert "Start Sharing" in host_html
        assert "Send Prompt" in viewer_html

        print("smoke test passed")
    finally:
        process.terminate()
        process.wait(timeout=10)


if __name__ == "__main__":
    main()
