from __future__ import annotations

import signal
import sys
import time


running = True


def handle_signal(signum, frame) -> None:  # type: ignore[override]
    del signum, frame
    global running
    running = False


def main() -> None:
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    target = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    print(f"Starting fake tunnel to {target}...", flush=True)
    print("Your tunnel URL: https://pocketmac-demo.trycloudflare.com", flush=True)
    while running:
        time.sleep(0.2)


if __name__ == "__main__":
    main()
