#!/usr/bin/python3
"""Read-only NAS host probe for devicekeeper.

Runs on the NAS host as root through devicekeeper-host-probe.timer.  It reads
the firmware pool through LuCI Lua, systemd through a fixed unit whitelist,
Docker through its Unix socket, and fan RPM through mifan.  It writes only
sanitized data to devicekeeper's state directory.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

PLUGIN_ROOT = Path(os.environ.get("DEVICEKEEPER_ROOT", "/data/plugins/devicekeeper"))
sys.path.insert(0, str(PLUGIN_ROOT))

from app.hostdata import (  # noqa: E402
    read_docker,
    read_fan,
    read_firmware,
    read_os_release,
    read_pool,
    read_systemd,
    utc_now,
    write_json_atomic,
)
from app.metrics import age_seconds, read_json  # noqa: E402


STATE_DIR = Path(os.environ.get("DEVICEKEEPER_STATE_DIR", str(PLUGIN_ROOT / "state")))
HOST_JSON = STATE_DIR / "host.json"
LUA_SCRIPT = Path(__file__).with_name("host-probe.lua")
POOL_INTERVAL_SECONDS = float(os.environ.get("DEVICEKEEPER_POOL_INTERVAL_SECONDS", "300"))
FAN_INTERVAL_SECONDS = float(os.environ.get("DEVICEKEEPER_FAN_INTERVAL_SECONDS", "60"))


def _is_fresh(value: dict | None, interval: float) -> bool:
    if not isinstance(value, dict):
        return False
    sampled = value.get("sampled_at")
    age = age_seconds(sampled)
    return age is not None and age < interval


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    previous = read_json(HOST_JSON) or {}
    errors: list[str] = []

    firmware = read_firmware()
    os_info = read_os_release()

    pool = previous.get("pool") if _is_fresh(previous.get("pool"), POOL_INTERVAL_SECONDS) else None
    if not isinstance(pool, dict):
        pool, error = read_pool(str(LUA_SCRIPT))
        if error:
            errors.append(f"pool: {error}")
            pool = previous.get("pool") if isinstance(previous.get("pool"), dict) else None

    fan = previous.get("fan") if _is_fresh(previous.get("fan"), FAN_INTERVAL_SECONDS) else None
    if not isinstance(fan, dict):
        fan = read_fan()
        if fan is None:
            errors.append("fan: mifan rpm unavailable")

    docker = read_docker()
    systemd = read_systemd()

    payload = {
        "generated_at": utc_now(),
        "hostname": socket.gethostname(),
        "firmware": firmware,
        "os": os_info,
        "pool": pool,
        "pool_sampled_at": (pool or {}).get("sampled_at") if isinstance(pool, dict) else previous.get("pool_sampled_at"),
        "fan": fan,
        "fan_sampled_at": (fan or {}).get("sampled_at") if isinstance(fan, dict) else previous.get("fan_sampled_at"),
        "docker": docker,
        "docker_sampled_at": docker.get("sampled_at"),
        "systemd": systemd,
        "systemd_sampled_at": systemd.get("sampled_at"),
        "probe_errors": errors,
    }
    write_json_atomic(HOST_JSON, payload, mode=0o644)

    firmware_version = (firmware or {}).get("version") or "unknown"
    pool_count = len(((pool or {}).get("internal_pool") or [])) if isinstance(pool, dict) else 0
    docker_state = "available" if docker.get("available") else "unavailable"
    print(
        f"host probe ok firmware={firmware_version} pools={pool_count} "
        f"docker={docker_state} errors={len(errors)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"host probe failed: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
