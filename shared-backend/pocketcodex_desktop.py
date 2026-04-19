from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import uvicorn

import mac_agent


def resource_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            return Path(meipass).resolve()
    return Path(__file__).resolve().parent


def app_support_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "PocketMac"


def find_open_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_server(url: str, timeout_seconds: float = 20.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except URLError:
            time.sleep(0.25)
    raise RuntimeError(f"Pocket Mac did not become healthy at {url} in time.")


def build_runtime_env(base_dir: Path, data_dir: Path, runtime_executable: str, runtime_script: str | None) -> None:
    os.environ["POCKETCODEX_BASE_DIR"] = str(base_dir)
    os.environ["POCKETCODEX_WEB_DIR"] = str(base_dir / "web")
    os.environ["POCKETCODEX_DB_PATH"] = str(data_dir / "control.db")
    os.environ["POCKETCODEX_RUNTIME_EXECUTABLE"] = runtime_executable
    if runtime_script:
        os.environ["POCKETCODEX_RUNTIME_SCRIPT"] = runtime_script
    else:
        os.environ.pop("POCKETCODEX_RUNTIME_SCRIPT", None)


def run_desktop(open_browser: bool, port: int | None) -> None:
    base_dir = resource_base_dir()
    data_dir = app_support_dir()
    data_dir.mkdir(parents=True, exist_ok=True)

    runtime_script = None if getattr(sys, "frozen", False) else str(Path(__file__).resolve())
    build_runtime_env(
        base_dir=base_dir,
        data_dir=data_dir,
        runtime_executable=sys.executable,
        runtime_script=runtime_script,
    )

    actual_port = port or find_open_port()
    health_url = f"http://127.0.0.1:{actual_port}/api/health"
    launch_url = f"http://127.0.0.1:{actual_port}/"

    config = uvicorn.Config(
        "app.main:app",
        host="127.0.0.1",
        port=actual_port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    wait_for_server(health_url)
    if open_browser:
        webbrowser.open(launch_url)

    shutdown_requested = threading.Event()

    def request_shutdown(signum: int, frame: object) -> None:
        del signum, frame
        server.should_exit = True
        shutdown_requested.set()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)

    try:
        while server_thread.is_alive() and not shutdown_requested.is_set():
            time.sleep(0.5)
    finally:
        server.should_exit = True
        server_thread.join(timeout=10)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pocket Mac desktop launcher")
    parser.add_argument("--agent-mode", action="store_true", help="Run the local Mac agent worker")
    parser.add_argument("--session", help="Session id to watch")
    parser.add_argument("--token", help="Session access token")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="FastAPI base URL")
    parser.add_argument("--poll-seconds", type=float, default=2.0, help="Polling interval")
    parser.add_argument("--app-name", default="Codex", help="Application name to activate")
    parser.add_argument("--dry-run", action="store_true", help="Do not send real keystrokes")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the web UI automatically")
    parser.add_argument("--port", type=int, default=None, help="Preferred local port")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.agent_mode:
        if not args.session or not args.token:
            parser.error("--agent-mode requires --session and --token")
        mac_agent.run_loop(
            base_url=args.base_url.rstrip("/"),
            session_id=args.session,
            token=args.token,
            poll_seconds=args.poll_seconds,
            dry_run=args.dry_run,
            app_name=args.app_name,
        )
        return

    run_desktop(open_browser=not args.no_browser, port=args.port)


if __name__ == "__main__":
    main()
