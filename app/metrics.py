"""Linux and host-probe metric readers for devicekeeper."""

from __future__ import annotations

import json
import os
import re
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def iso_from_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def age_seconds(value: str | None) -> float | None:
    parsed = parse_iso(value)
    if parsed is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


@dataclass(frozen=True)
class Paths:
    host: Path
    state: Path
    pool: Path

    @property
    def proc(self) -> Path:
        return self.host / "proc"

    @property
    def sys(self) -> Path:
        return self.host / "sys"

    @classmethod
    def from_env(cls) -> "Paths":
        host = Path(os.environ.get("HOST_ROOT", "/host"))
        state = Path(os.environ.get("STATE_DIR", "/state"))
        pool = Path(os.environ.get("POOL_PATH", str(host / "nas" / "pool0")))
        return cls(host=host, state=state, pool=pool)


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return None


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def parse_proc_stat(text: str | None) -> dict[str, int] | None:
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("cpu "):
            parts = line.split()
            try:
                fields = [int(part) for part in parts[1:9]]
            except ValueError:
                return None
            if len(fields) < 5:
                return None
            # guest/guest_nice are already included in user/nice on Linux.
            return {
                "total": sum(fields),
                "idle": fields[3],
                "iowait": fields[4],
            }
    return None


def cpu_percent(previous: dict[str, int] | None, current: dict[str, int] | None) -> float | None:
    if not previous or not current:
        return None
    delta_total = current["total"] - previous["total"]
    delta_idle = current["idle"] - previous["idle"]
    delta_iowait = current["iowait"] - previous["iowait"]
    if delta_total <= 0:
        return None
    busy = 100.0 * (delta_total - delta_idle - delta_iowait) / delta_total
    return round(max(0.0, min(100.0, busy)), 2)


def parse_meminfo(text: str | None) -> dict[str, int] | None:
    if not text:
        return None
    values: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        parts = raw.strip().split()
        if not parts:
            continue
        try:
            values[key] = int(parts[0]) * 1024
        except ValueError:
            continue
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if not total or available is None:
        return None
    return {
        "total": total,
        "available": available,
        "used": max(0, total - available),
    }


def parse_loadavg(text: str | None) -> tuple[float, float, float] | None:
    if not text:
        return None
    parts = text.split()
    if len(parts) < 3:
        return None
    try:
        return float(parts[0]), float(parts[1]), float(parts[2])
    except ValueError:
        return None


def read_uptime(paths: Paths) -> float | None:
    text = read_text(paths.proc / "uptime")
    if not text:
        return None
    try:
        return float(text.split()[0])
    except (ValueError, IndexError):
        return None


def read_boot_id(paths: Paths) -> str:
    text = read_text(paths.proc / "sys/kernel/random/boot_id")
    return (text or "").strip() or "unknown"


def read_hostname(paths: Paths) -> str:
    text = read_text(paths.proc / "sys/kernel/hostname")
    return (text or "").strip() or socket.gethostname()


def read_cpu_count(paths: Paths) -> int | None:
    text = read_text(paths.proc / "stat")
    if text:
        count = sum(1 for line in text.splitlines() if re.match(r"^cpu\d+\s", line))
        if count:
            return count
    online = read_text(paths.sys / "devices/system/cpu/online")
    if online and online.strip():
        count = 0
        for part in online.strip().split(","):
            if "-" in part:
                try:
                    start, end = part.split("-", 1)
                    count += int(end) - int(start) + 1
                except ValueError:
                    continue
            elif part.isdigit():
                count += 1
        return count or None
    return None


def _float_file(path: Path) -> float | None:
    text = read_text(path)
    if not text:
        return None
    try:
        return float(text.strip())
    except ValueError:
        return None


def read_cpu_temperature(paths: Paths) -> tuple[float | None, str | None]:
    thermal_root = paths.sys / "class/thermal"
    candidates: list[tuple[int, Path, str]] = []
    if thermal_root.exists():
        for zone in thermal_root.glob("thermal_zone*"):
            kind = (read_text(zone / "type") or "").strip().lower()
            if not kind:
                continue
            priority = 0 if kind == "cpu-thermal" else 1 if "cpu" in kind or "soc" in kind else 2
            candidates.append((priority, zone, f"linux.thermal.{kind}"))
    if candidates:
        candidates.sort(key=lambda item: item[0])
        for _, zone, source in candidates:
            value = _float_file(zone / "temp")
            if value is not None:
                if value > 1000:
                    value /= 1000.0
                return round(value, 2), source

    hwmon_root = paths.sys / "class/hwmon"
    if hwmon_root.exists():
        for hwmon in hwmon_root.glob("hwmon*"):
            name = (read_text(hwmon / "name") or "").strip().lower()
            if "cpu" not in name and "soc" not in name:
                continue
            for temp in sorted(hwmon.glob("temp*_input")):
                value = _float_file(temp)
                if value is not None:
                    if value > 1000:
                        value /= 1000.0
                    return round(value, 2), f"linux.hwmon.{name}.{temp.name}"
    return None, None


def _virtual_interface(name: str) -> bool:
    prefixes = ("lo", "docker", "veth", "br-", "virbr", "tun", "tap", "tailscale")
    return name.startswith(prefixes)


def default_interface(paths: Paths) -> str | None:
    # In a container, /proc/net reflects the container network namespace.
    # The host probe mounts host /proc, so PID 1's net directory is the host view.
    for route_path in (paths.proc / "1/net/route", paths.proc / "net/route"):
        text = read_text(route_path)
        if not text:
            continue
        for line in text.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 8:
                continue
            if fields[1] == "00000000" and fields[7] == "00000000" and not _virtual_interface(fields[0]):
                return fields[0]
    net_root = paths.sys / "class/net"
    best: tuple[int, str] | None = None
    if net_root.exists():
        for iface in net_root.iterdir():
            if _virtual_interface(iface.name):
                continue
            if (read_text(iface / "operstate") or "").strip() != "up":
                continue
            rx = _float_file(iface / "statistics/rx_bytes") or 0
            if best is None or rx > best[0]:
                best = (int(rx), iface.name)
    return best[1] if best else None


def read_network_counters(paths: Paths) -> dict[str, Any] | None:
    iface = default_interface(paths)
    if not iface:
        return None
    root = paths.sys / "class/net" / iface
    rx = _float_file(root / "statistics/rx_bytes")
    tx = _float_file(root / "statistics/tx_bytes")
    if rx is None or tx is None:
        return None
    return {
        "iface": iface,
        "rx": rx,
        "tx": tx,
        "monotonic": time.monotonic(),
    }


def read_statvfs(paths: Paths) -> dict[str, Any] | None:
    try:
        stats = os.statvfs(paths.pool)
    except OSError:
        return None
    total = stats.f_blocks * stats.f_frsize
    used = (stats.f_blocks - stats.f_bfree) * stats.f_frsize
    available = stats.f_bavail * stats.f_frsize
    if total <= 0:
        return None
    return {
        "total": total,
        "used": used,
        "available": available,
        "used_pct": round(used / total * 100, 2),
    }


def _disk_block_names(paths: Paths) -> list[str]:
    root = paths.sys / "block"
    if not root.exists():
        return []
    names = []
    for item in root.iterdir():
        if re.fullmatch(r"sd[a-z]+|nvme\d+n\d+", item.name):
            names.append(item.name)
    return sorted(names)


def _disk_temperature(paths: Paths) -> dict[str, float]:
    result: dict[str, float] = {}
    hwmon_root = paths.sys / "class/hwmon"
    if not hwmon_root.exists():
        return result
    for hwmon in hwmon_root.glob("hwmon*"):
        name = (read_text(hwmon / "name") or "").strip().lower()
        if "drivetemp" not in name and "drive" not in name:
            continue
        device = hwmon / "device"
        try:
            real = Path(os.path.realpath(device))
        except OSError:
            continue
        match = re.search(r"/block/([^/]+)$", str(real)) or re.search(r"/(sd[a-z]+|nvme\d+n\d+)$", str(real))
        if not match:
            continue
        block = match.group(1)
        value = _float_file(hwmon / "temp1_input")
        if value is None:
            continue
        result[block] = round(value / 1000.0, 1) if value > 1000 else round(value, 1)
    return result


def discover_disks(paths: Paths) -> list[dict[str, Any]]:
    temperatures = _disk_temperature(paths)
    disks = []
    for name in _disk_block_names(paths):
        root = paths.sys / "block" / name
        size_sectors = _float_file(root / "size")
        if size_sectors is None:
            continue
        rota = (read_text(root / "queue/rotational") or "").strip()
        model = (read_text(root / "device/model") or "").strip()
        disks.append(
            {
                "dev": name,
                "slot": None,
                "type": "HDD" if rota == "1" else "SSD",
                "model": model,
                "size_bytes": int(size_sectors * 512),
                "used_bytes": None,
                "temperature_c": temperatures.get(name),
                "health": "unknown",
                "health_source": "not_provided",
                "state": "detected",
            }
        )
    return disks


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(str(value).rstrip("℃°C%"))
    except (TypeError, ValueError):
        return None


def storage_from_pool(host: dict[str, Any], paths: Paths) -> dict[str, Any] | None:
    pool_payload = host.get("pool")
    if not isinstance(pool_payload, dict):
        return None
    pools = pool_payload.get("internal_pool")
    if not isinstance(pools, list) or not pools:
        return None
    pool = next((item for item in pools if isinstance(item, dict)), None)
    if not pool:
        return None
    total = _as_int(pool.get("total_size"))
    used = _as_int(pool.get("used_size"))
    if not total or used is None:
        return None
    available = max(0, total - used)
    disks = []
    for disk in pool.get("disks") or []:
        if not isinstance(disk, dict):
            continue
        disks.append(
            {
                "dev": disk.get("dev"),
                "slot": _as_int(disk.get("slot")),
                "type": disk.get("type") or "unknown",
                "model": disk.get("model") or "",
                "size_bytes": _as_int(disk.get("total_size")),
                "used_bytes": _as_int(disk.get("used_size")),
                "temperature_c": _as_float(disk.get("temperature")),
                "health": disk.get("health") or "unknown",
                "health_source": "firmware",
                "state": disk.get("state") or "unknown",
            }
        )
    status = str(pool.get("status") or "unknown")
    return {
        "source": "firmware.getPoolInfo",
        "status": status,
        "status_label": "存储池正常" if status == "normal" else "需要关注",
        "pool_type": pool.get("pool_type") or "",
        "disk_count": _as_int(pool.get("disk_num")) or len(disks),
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": available,
        "used_pct": round(used / total * 100, 2),
        "disks": disks,
        "sampled_at": pool_payload.get("sampled_at") or host.get("pool_sampled_at"),
        "quality": "ok",
    }


def storage_from_statvfs(paths: Paths) -> dict[str, Any] | None:
    values = read_statvfs(paths)
    if not values:
        return None
    disks = discover_disks(paths)
    return {
        "source": "statvfs",
        "status": "ok",
        "status_label": "容量可读",
        "pool_type": "filesystem",
        "disk_count": len(disks),
        "total_bytes": values["total"],
        "used_bytes": values["used"],
        "available_bytes": values["available"],
        "used_pct": values["used_pct"],
        "disks": disks,
        "sampled_at": utc_now(),
        "quality": "ok",
    }


def metric(
    value: Any,
    unit: str,
    source: str,
    quality: str = "ok",
    reason: str | None = None,
    sampled_at: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "value": value,
        "unit": unit,
        "quality": quality,
        "source": source,
        "sampled_at": sampled_at or utc_now(),
    }
    if reason:
        result["reason"] = reason
    return result


def docker_from_host(host: dict[str, Any]) -> dict[str, Any]:
    docker = host.get("docker")
    if isinstance(docker, dict):
        result = dict(docker)
        result.setdefault("sampled_at", host.get("docker_sampled_at"))
        result.setdefault("containers", [])
        result.setdefault("containers_running", 0)
        result.setdefault("containers_total", len(result["containers"]))
        return result
    return {
        "available": False,
        "reason": "host_probe_unavailable",
        "containers": [],
        "containers_running": 0,
        "containers_total": 0,
        "sampled_at": None,
    }


def system_from_host(host: dict[str, Any]) -> dict[str, Any]:
    systemd = host.get("systemd")
    if isinstance(systemd, dict):
        return systemd
    return {
        "available": False,
        "reason": "host_probe_unavailable",
        "services": [],
    }


def fan_from_host(host: dict[str, Any]) -> dict[str, Any] | None:
    fan = host.get("fan")
    return fan if isinstance(fan, dict) else None


def host_meta(host: dict[str, Any]) -> dict[str, Any]:
    firmware = host.get("firmware") if isinstance(host.get("firmware"), dict) else {}
    os_info = host.get("os") if isinstance(host.get("os"), dict) else {}
    return {
        "model": firmware.get("model") or "",
        "firmware": firmware.get("version") or "",
        "channel": firmware.get("channel") or "",
        "build": firmware.get("build") or "",
        "build_time": firmware.get("time") or "",
        "os": os_info.get("pretty_name") or "",
    }
