from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from websockets.sync.client import connect as ws_connect


BASE_DIR = Path(__file__).resolve().parent.parent


def fetch_json(
    url: str,
    method: str = "GET",
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {"Content-Type": "application/json"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        url,
        data=body,
        headers=request_headers,
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
    env = os.environ.copy()
    env["POCKETCODEX_TUNNEL_COMMAND"] = (
        f"{sys.executable} {BASE_DIR / 'scripts' / 'fake_tunnel.py'} {{target}}"
    )
    process = subprocess.Popen(
        ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8011"],
        cwd=BASE_DIR,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        wait_for_server("http://127.0.0.1:8011/api/health")
        session_id = f"smk{secrets.token_hex(4)}"[:12]
        viewer_id = f"viewer-{secrets.token_hex(8)}"

        session = fetch_json(
            "http://127.0.0.1:8011/api/sessions",
            method="POST",
            payload={"session_id": session_id},
        )
        assert session["session_id"] == session_id
        token = session["access_token"]
        auth = {"X-Session-Token": token}
        runtime = fetch_json("http://127.0.0.1:8011/api/runtime-config")
        assert runtime["local_host_base_url"] == "http://127.0.0.1:8011"
        assert runtime["lan_base_url"].startswith("http://")
        assert runtime["public_base_url"].startswith("http://")
        assert runtime["remote_trial"]["active"] is False
        assert session["host_url"].startswith("http://127.0.0.1:8011/host.html")
        assert session["host_local_url"] == session["host_url"]
        assert session["host_public_url"].endswith(f"session={session_id}&token={token}")
        assert session["viewer_lan_url"].endswith(f"session={session_id}&token={token}")

        session_state = fetch_json(f"http://127.0.0.1:8011/api/sessions/{session_id}", headers=auth)
        assert session_state["session"]["id"] == session_id
        assert session_state["controller"]["active"] is False
        assert session_state["links"]["host_url"].startswith("http://127.0.0.1:8011/host.html")
        assert session_state["links"]["viewer_url"].endswith(f"session={session_id}&token={token}")
        assert session_state["links"]["viewer_lan_url"].endswith(f"session={session_id}&token={token}")

        heartbeat = fetch_json(
            f"http://127.0.0.1:8011/api/sessions/{session_id}/heartbeat",
            method="POST",
            payload={"role": "viewer"},
            headers=auth,
        )
        assert heartbeat["status"] == "ok"

        control = fetch_json(
            f"http://127.0.0.1:8011/api/sessions/{session_id}/control/acquire",
            method="POST",
            payload={"viewer_id": viewer_id, "label": "Smoke Phone"},
            headers=auth,
        )
        assert control["controller"]["active"] is True
        assert control["controller"]["is_current_viewer"] is True

        command = fetch_json(
            f"http://127.0.0.1:8011/api/sessions/{session_id}/commands",
            method="POST",
            payload={
                "kind": "prompt_to_codex",
                "text": "Hello from smoke test",
                "submit": True,
            },
            headers={**auth, "X-Viewer-Id": viewer_id},
        )
        assert command["status"] == "queued"

        focus_command = fetch_json(
            f"http://127.0.0.1:8011/api/sessions/{session_id}/commands",
            method="POST",
            payload={"kind": "focus_codex", "text": "", "submit": True},
            headers={**auth, "X-Viewer-Id": viewer_id},
        )
        assert focus_command["status"] == "queued"

        claimed = fetch_json(
            f"http://127.0.0.1:8011/api/sessions/{session_id}/commands/claim-next",
            method="POST",
            payload={"agent_name": "smoke-agent"},
            headers=auth,
        )
        assert claimed["status"] == "claimed"
        assert claimed["payload"]["text"] == "Hello from smoke test"

        claimed_focus = fetch_json(
            f"http://127.0.0.1:8011/api/sessions/{session_id}/commands/claim-next",
            method="POST",
            payload={"agent_name": "smoke-agent"},
            headers=auth,
        )
        assert claimed_focus["status"] == "claimed"
        assert claimed_focus["kind"] == "focus_codex"

        completed = fetch_json(
            f"http://127.0.0.1:8011/api/sessions/{session_id}/commands/{claimed['id']}/complete",
            method="POST",
            payload={"ok": True, "detail": "Smoke test complete"},
            headers=auth,
        )
        assert completed["status"] == "completed"
        assert completed["result"]["detail"] == "Smoke test complete"

        completed_focus = fetch_json(
            f"http://127.0.0.1:8011/api/sessions/{session_id}/commands/{claimed_focus['id']}/complete",
            method="POST",
            payload={"ok": True, "detail": "Focused Codex"},
            headers=auth,
        )
        assert completed_focus["status"] == "completed"

        refreshed = fetch_json(
            f"http://127.0.0.1:8011/api/sessions/{session_id}?viewer_id={viewer_id}",
            headers=auth,
        )
        assert refreshed["session"]["last_viewer_seen"] is not None
        assert refreshed["controller"]["is_current_viewer"] is True

        with ws_connect(f"ws://127.0.0.1:8011/ws/session/{session_id}/viewer?token={token}") as viewer_ws:
            ready = json.loads(viewer_ws.recv())
            assert ready["type"] == "socket-ready"

            with ws_connect(f"ws://127.0.0.1:8011/ws/session/{session_id}/host?token={token}") as host_ws:
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

        qr_svg = fetch_text(f"http://127.0.0.1:8011/api/sessions/{session_id}/qr.svg?kind=viewer&token={token}")
        assert "<svg" in qr_svg
        qr_lan_svg = fetch_text(
            f"http://127.0.0.1:8011/api/sessions/{session_id}/qr.svg?kind=viewer_lan&token={token}"
        )
        assert "<svg" in qr_lan_svg

        remote_trial = fetch_json(
            "http://127.0.0.1:8011/api/remote-trial/start",
            method="POST",
            payload={},
        )
        assert remote_trial["active"] is True
        assert remote_trial["public_url"] == "https://pocketcodex-demo.trycloudflare.com"

        runtime_with_trial = fetch_json("http://127.0.0.1:8011/api/runtime-config")
        assert runtime_with_trial["public_base_url"] == "https://pocketcodex-demo.trycloudflare.com"
        assert runtime_with_trial["remote_trial"]["active"] is True

        session_with_trial = fetch_json(f"http://127.0.0.1:8011/api/sessions/{session_id}", headers=auth)
        assert session_with_trial["links"]["viewer_url"].startswith("https://pocketcodex-demo.trycloudflare.com")
        assert session_with_trial["links"]["viewer_lan_url"].startswith("http://")

        stopped_trial = fetch_json(
            "http://127.0.0.1:8011/api/remote-trial/stop",
            method="POST",
            payload={},
        )
        assert stopped_trial["active"] is False

        duplicate_failed = False
        try:
            fetch_json(
                "http://127.0.0.1:8011/api/sessions",
                method="POST",
                payload={"session_id": session_id},
            )
        except Exception:
            duplicate_failed = True
        assert duplicate_failed, "Duplicate session creation should fail"

        index_html = fetch_text("http://127.0.0.1:8011/")
        host_html = fetch_text("http://127.0.0.1:8011/host.html")
        viewer_html = fetch_text("http://127.0.0.1:8011/viewer.html")
        assert "Host URL (open on Mac)" in index_html
        assert "Adaptive Viewer QR" in index_html
        assert "Start Remote Trial" in index_html
        assert "localhost or HTTPS" in host_html
        assert "Share Window" in host_html
        assert "Share Entire Screen" in host_html
        assert "Take Control" in viewer_html
        assert "Composer" in viewer_html

        print("smoke test passed")
    finally:
        process.terminate()
        process.wait(timeout=10)


if __name__ == "__main__":
    main()
