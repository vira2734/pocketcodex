from __future__ import annotations

import argparse
import json
import socket
import subprocess
import time
import urllib.error
import urllib.request


def request_json(method: str, url: str, payload: dict | None = None, token: str | None = None) -> dict | None:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Session-Token"] = token
    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: {exc.code} {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc.reason}") from exc

    if not raw:
        return None
    return json.loads(raw)


def paste_into_codex(text: str, submit: bool, app_name: str) -> str:
    subprocess.run(["pbcopy"], input=text, text=True, check=True)

    paste_script = f'''
        tell application "{app_name}" to activate
        delay 0.35
        tell application "System Events"
            keystroke "a" using command down
            delay 0.08
            keystroke "v" using command down
    '''
    if submit:
        paste_script += """
            delay 0.1
            key code 36
        """
    paste_script += """
        end tell
    """

    subprocess.run(["osascript", "-e", paste_script], check=True)
    return f"Replaced prompt draft in {app_name}" + (" and pressed Return." if submit else ".")


def process_command(command: dict, dry_run: bool, app_name: str) -> tuple[bool, str]:
    payload = command["payload"]
    if command["kind"] != "prompt_to_codex":
        return False, f"Unsupported command kind: {command['kind']}"

    text = payload["text"]
    submit = bool(payload.get("submit", True))

    if dry_run:
        return True, f"Dry run: would paste {len(text)} characters into {app_name}."

    try:
        detail = paste_into_codex(text, submit, app_name)
    except subprocess.CalledProcessError as exc:
        return False, f"AppleScript failed: {exc}"

    return True, detail


def run_loop(
    base_url: str,
    session_id: str,
    token: str,
    poll_seconds: float,
    dry_run: bool,
    app_name: str,
) -> None:
    agent_name = socket.gethostname()
    claim_url = f"{base_url}/api/sessions/{session_id}/commands/claim-next"
    heartbeat_url = f"{base_url}/api/sessions/{session_id}/heartbeat"

    print(f"Mac agent watching session {session_id}")
    print(f"Target app: {app_name}")
    if dry_run:
        print("Dry-run mode enabled")

    while True:
        try:
            request_json("POST", heartbeat_url, {"role": "agent"}, token=token)
            command = request_json("POST", claim_url, {"agent_name": agent_name}, token=token)
            if command:
                ok, detail = process_command(command, dry_run=dry_run, app_name=app_name)
                complete_url = f"{base_url}/api/sessions/{session_id}/commands/{command['id']}/complete"
                request_json("POST", complete_url, {"ok": ok, "detail": detail}, token=token)
                print(f"Completed command {command['id']}: {detail}")
            else:
                time.sleep(poll_seconds)
        except KeyboardInterrupt:
            print("Mac agent stopped.")
            break
        except Exception as exc:
            print(f"Mac agent error: {exc}")
            time.sleep(poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="PocketCodex Mac agent")
    parser.add_argument("--session", required=True, help="Session id to watch")
    parser.add_argument("--token", required=True, help="Session access token")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="FastAPI base URL")
    parser.add_argument("--poll-seconds", type=float, default=2.0, help="Polling interval")
    parser.add_argument("--app-name", default="Codex", help="Application name to activate")
    parser.add_argument("--dry-run", action="store_true", help="Do not send real keystrokes")
    args = parser.parse_args()

    run_loop(
        base_url=args.base_url.rstrip("/"),
        session_id=args.session,
        token=args.token,
        poll_seconds=args.poll_seconds,
        dry_run=args.dry_run,
        app_name=args.app_name,
    )


if __name__ == "__main__":
    main()
