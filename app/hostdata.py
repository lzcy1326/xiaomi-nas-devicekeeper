"""Read-only host adapters used by the small host probe.

The web and collector containers do not receive the Docker socket.  Only this
module runs on the NAS host through the devicekeeper host probe timer.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SYSTEM_UNITS = (
    ("nginx.service", "网页服务"),
    ("minas.plugin_center.service", "插件中心"),
)


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 3.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _docker_json(socket_path: str, path: str, timeout: float = 3.0) -> tuple[int, Any]:
    conn = UnixHTTPConnection(socket_path, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Host": "localhost"})
        response = conn.getresponse()
        body = response.read(2_000_000)
        if response.status >= 400:
            return response.status, None
        return response.status, json.loads(body.decode("utf-8"))
    finally:
        conn.close()


def _container_health(status: str) -> str:
    lower = (status or "").lower()
    if "(healthy)" in lower:
        return "healthy"
    if "(unhealthy)" in lower:
        return "unhealthy"
    if "health: starting" in lower:
        return "starting"
    return "not_provided"


def sanitize_docker(version: dict[str, Any], containers: list[dict[str, Any]]) -> dict[str, Any]:
    sanitized = []
    for item in containers[:50]:
        names = item.get("Names") or []
        name = str(names[0] if names else "").lstrip("/")
        status = str(item.get("Status") or "")
        sanitized.append(
            {
                "id": str(item.get("Id") or "")[:12],
                "name": name or "unnamed",
                "image": str(item.get("Image") or ""),
                "state": str(item.get("State") or "unknown"),
                "status": status,
                "health": _container_health(status),
            }
        )
    running = sum(1 for item in sanitized if item["state"] == "running")
    return {
        "available": True,
        "version": str(version.get("Version") or ""),
        "api_version": str(version.get("ApiVersion") or ""),
        "containers_running": running,
        "containers_total": len(sanitized),
        "containers": sanitized,
        "sampled_at": utc_now(),
    }


def read_docker(socket_path: str = "/var/run/docker.sock", timeout: float = 3.0) -> dict[str, Any]:
    if not Path(socket_path).exists():
        return {
            "available": False,
            "reason": "docker_socket_not_found",
            "containers": [],
            "containers_running": 0,
            "containers_total": 0,
            "sampled_at": utc_now(),
        }
    try:
        version_status, version = _docker_json(socket_path, "/version", timeout=timeout)
        containers_status, containers = _docker_json(
            socket_path, "/containers/json?all=1", timeout=timeout
        )
        if version_status != 200 or containers_status != 200:
            raise RuntimeError(f"docker_http_{version_status}_{containers_status}")
        return sanitize_docker(version or {}, containers or [])
    except Exception as exc:  # socket permissions, daemon errors, malformed JSON
        return {
            "available": False,
            "reason": str(exc)[:160],
            "containers": [],
            "containers_running": 0,
            "containers_total": 0,
            "sampled_at": utc_now(),
        }


def read_systemd(units: tuple[tuple[str, str], ...] = SYSTEM_UNITS) -> dict[str, Any]:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return {"available": False, "reason": "systemctl_not_found", "services": []}
    services = []
    for unit, label in units:
        try:
            result = subprocess.run(
                [
                    systemctl,
                    "show",
                    "--no-pager",
                    "-p",
                    "LoadState",
                    "-p",
                    "ActiveState",
                    "-p",
                    "SubState",
                    "-p",
                    "UnitFileState",
                    unit,
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            values: dict[str, str] = {}
            for line in result.stdout.splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    values[key] = value
            services.append(
                {
                    "unit": unit,
                    "label": label,
                    "load_state": values.get("LoadState", "unknown"),
                    "active_state": values.get("ActiveState", "unknown"),
                    "sub_state": values.get("SubState", "unknown"),
                    "file_state": values.get("UnitFileState", "unknown"),
                }
            )
        except Exception as exc:
            services.append(
                {
                    "unit": unit,
                    "label": label,
                    "load_state": "error",
                    "active_state": "unknown",
                    "sub_state": "unknown",
                    "file_state": "unknown",
                    "error": str(exc)[:160],
                }
            )
    return {"available": True, "services": services, "sampled_at": utc_now()}


def read_firmware(path: str = "/etc/minas.version") -> dict[str, Any] | None:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    allowed = ("version", "time", "commit", "branch", "compiler", "model", "channel", "build")
    return {key: raw.get(key) for key in allowed if raw.get(key) is not None}


def read_os_release(path: str = "/etc/os-release") -> dict[str, Any]:
    result: dict[str, str] = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            result[key] = value.strip().strip('"').strip("'")
    except Exception:
        return {}
    return {
        "id": result.get("ID", ""),
        "name": result.get("NAME", ""),
        "version": result.get("VERSION", ""),
        "pretty_name": result.get("PRETTY_NAME", ""),
    }


def read_fan() -> dict[str, Any] | None:
    mifan = shutil.which("mifan") or "/usr/sbin/mifan"
    if not Path(mifan).exists():
        return None
    try:
        result = subprocess.run(
            [mifan, "rpm"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        numbers = re.findall(r"\d+", result.stdout or "")
        if not numbers:
            return None
        return {"rpm": int(numbers[-1]), "source": "firmware.mifan", "sampled_at": utc_now()}
    except Exception:
        return None


def _sanitize_pool(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    pools = payload.get("internal_pool")
    if not isinstance(pools, list):
        data = payload.get("data")
        pools = data.get("internal_pool") if isinstance(data, dict) else None
    if not isinstance(pools, list) or not pools:
        return None
    clean_pools = []
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        clean = {
            key: pool.get(key)
            for key in ("pool_id", "pool_name", "personal_used", "other_used", "pool_type", "status", "total_size", "used_size", "disk_num")
            if pool.get(key) is not None
        }
        disks = []
        for disk in pool.get("disks") or []:
            if not isinstance(disk, dict):
                continue
            disks.append(
                {
                    key: disk.get(key)
                    for key in ("dev", "type", "slot", "run_time", "state", "temperature", "total_size", "model", "pool_type", "health", "used_size")
                    if disk.get(key) is not None
                }
            )
        clean["disks"] = disks
        clean_pools.append(clean)
    return {"internal_pool": clean_pools, "sampled_at": utc_now()}


def read_pool(lua_script: str, lua_bin: str = "/usr/bin/lua", timeout: float = 8.0) -> tuple[dict[str, Any] | None, str | None]:
    if not Path(lua_bin).exists():
        return None, "lua_not_found"
    if not Path(lua_script).exists():
        return None, "lua_script_not_found"
    try:
        result = subprocess.run(
            [lua_bin, lua_script],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            return None, (result.stderr or result.stdout or "lua_failed")[:200]
        return _sanitize_pool(json.loads(result.stdout)), None
    except Exception as exc:
        return None, str(exc)[:200]


def write_json_atomic(path: Path, payload: dict[str, Any], mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, mode)
    os.replace(temporary, path)
