"""Container supervisor for the read-only devicekeeper API and collector."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


def _terminate(children: list[subprocess.Popen[bytes]]) -> None:
    for child in children:
        if child.poll() is None:
            child.terminate()
    deadline = time.monotonic() + 8
    for child in children:
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
    for child in children:
        if child.poll() is None:
            child.kill()


def main() -> int:
    children = [
        subprocess.Popen([sys.executable, "-m", "app.collector"]),
        subprocess.Popen([sys.executable, "-m", "app.api"]),
    ]
    stopping = False

    def stop(signum: int, frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        while not stopping:
            for child in children:
                code = child.poll()
                if code is not None:
                    print(
                        f"devicekeeper child pid={child.pid} exited code={code}; stopping container",
                        file=sys.stderr,
                        flush=True,
                    )
                    return 1 if code == 0 else code
            time.sleep(0.5)
        return 0
    finally:
        _terminate(children)


if __name__ == "__main__":
    raise SystemExit(main())
