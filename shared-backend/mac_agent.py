from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


QUARTZ_PYTHON: str | None = None


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


def get_quartz_python() -> str:
    global QUARTZ_PYTHON
    if QUARTZ_PYTHON:
        return QUARTZ_PYTHON

    candidates = [
        sys.executable,
        "/opt/anaconda3/bin/python3",
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
    ]

    probe = "import Quartz"
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        result = subprocess.run(
            [candidate, "-c", probe],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            QUARTZ_PYTHON = candidate
            return candidate

    raise RuntimeError("No Python interpreter with Quartz support is available.")


def get_app_window_bounds(app_name: str) -> tuple[float, float, float, float]:
    helper = r"""
import json
import sys
import Quartz

app_name = sys.argv[1]
windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
candidates = []

for window in windows:
    if window.get("kCGWindowOwnerName") != app_name:
        continue
    if window.get("kCGWindowLayer", 1) != 0:
        continue
    bounds = window.get("kCGWindowBounds", {})
    width = int(bounds.get("Width", 0))
    height = int(bounds.get("Height", 0))
    if width <= 0 or height <= 0:
        continue
    candidates.append((width * height, bounds))

if not candidates:
    raise SystemExit(2)

_, bounds = max(candidates, key=lambda item: item[0])
plain_bounds = {key: float(value) for key, value in dict(bounds).items()}
print(json.dumps(plain_bounds))
"""
    result = subprocess.run(
        [get_quartz_python(), "-c", helper, app_name],
        text=True,
        capture_output=True,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Could not find an on-screen window for {app_name}.")

    bounds = json.loads(result.stdout)
    return (float(bounds["X"]), float(bounds["Y"]), float(bounds["Width"]), float(bounds["Height"]))


def click_point(x: float, y: float) -> None:
    helper = r"""
import sys
import time
import Quartz

x = float(sys.argv[1])
y = float(sys.argv[2])
source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateCombinedSessionState)
move = Quartz.CGEventCreateMouseEvent(source, Quartz.kCGEventMouseMoved, (x, y), Quartz.kCGMouseButtonLeft)
down = Quartz.CGEventCreateMouseEvent(source, Quartz.kCGEventLeftMouseDown, (x, y), Quartz.kCGMouseButtonLeft)
up = Quartz.CGEventCreateMouseEvent(source, Quartz.kCGEventLeftMouseUp, (x, y), Quartz.kCGMouseButtonLeft)
Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
time.sleep(0.04)
Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
time.sleep(0.02)
Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
"""
    result = subprocess.run(
        [get_quartz_python(), "-c", helper, str(x), str(y)],
        text=True,
        capture_output=True,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError("Unable to click Codex composer region.")


def focus_codex_composer(app_name: str) -> None:
    left, top, width, height = get_app_window_bounds(app_name)
    # Codex exposes a custom UI tree, so target the lower-center input region directly.
    target_x = left + (width * 0.5)
    target_y = top + (height * 0.9)
    click_point(target_x, target_y)
    time.sleep(0.2)


def paste_into_codex(text: str, submit: bool, app_name: str) -> str:
    subprocess.run(["pbcopy"], input=text, text=True, check=True)
    activate_codex(app_name)
    time.sleep(0.2)
    focus_codex_composer(app_name)

    paste_script = f'''
        tell application "System Events"
            keystroke "a" using command down
            delay 0.1
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


def activate_codex(app_name: str) -> str:
    subprocess.run(["osascript", "-e", f'tell application "{app_name}" to activate'], check=True)
    return f"Brought {app_name} to the front."


def interrupt_codex(app_name: str) -> str:
    interrupt_script = f'''
        tell application "{app_name}" to activate
        delay 0.15
        tell application "System Events"
            key code 53
        end tell
    '''
    subprocess.run(["osascript", "-e", interrupt_script], check=True)
    return f"Sent Escape to {app_name}."


def process_command(command: dict, dry_run: bool, app_name: str) -> tuple[bool, str]:
    payload = command["payload"]
    if dry_run:
        if command["kind"] == "prompt_to_codex":
            return True, f"Dry run: would paste {len(payload['text'])} characters into {app_name}."
        return True, f"Dry run: would run {command['kind']} on {app_name}."

    try:
        if command["kind"] == "prompt_to_codex":
            text = payload["text"]
            submit = bool(payload.get("submit", True))
            detail = paste_into_codex(text, submit, app_name)
        elif command["kind"] == "focus_codex":
            detail = activate_codex(app_name)
        elif command["kind"] == "interrupt_codex":
            detail = interrupt_codex(app_name)
        else:
            return False, f"Unsupported command kind: {command['kind']}"
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
    parser = argparse.ArgumentParser(description="Pocket Mac agent")
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
