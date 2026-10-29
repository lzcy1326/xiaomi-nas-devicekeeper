"""Devicekeeper collector.

Runs inside the collector container.  It reads Linux counters from bind-mounted
/proc and /sys, and consumes the sanitized host probe JSON for firmware storage,
Docker, systemd and fan data.  It never receives the Docker socket.
"""

from __future__ import annotations

import math
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .hostdata import write_json_atomic
from .metrics import (
    Paths,
    age_seconds,
    cpu_percent,
    default_interface,
    docker_from_host,
    fan_from_host,
    host_meta,
    iso_from_epoch,
    metric,
    parse_loadavg,
    parse_meminfo,
    parse_proc_stat,
    read_boot_id,
    read_cpu_count,
    read_cpu_temperature,
    read_hostname,
    read_json,
    read_network_counters,
    read_text,
    read_uptime,
    storage_from_pool,
    storage_from_statvfs,
    system_from_host,
    utc_now,
)


INTERVAL_SECONDS = float(os.environ.get("COLLECT_INTERVAL_SECONDS", "5"))
HISTORY_RETENTION_DAYS = 7
EVENT_RETENTION_DAYS = 90
EVENT_RETENTION_ROWS = 2000


def _log(message: str) -> None:
    print(f"{utc_now()} {message}", flush=True)


class HistoryStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=3.0)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA busy_timeout=3000")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS metric_samples (
                series_id TEXT NOT NULL,
                ts_utc_ms INTEGER NOT NULL,
                boot_id TEXT NOT NULL,
                value REAL NOT NULL,
                source TEXT NOT NULL,
                PRIMARY KEY (series_id, ts_utc_ms)
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_samples_series_time ON metric_samples(series_id, ts_utc_ms)"
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                state TEXT NOT NULL,
                title TEXT NOT NULL,
                detail TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                resolved_at TEXT
            )
            """
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_recent ON events(last_seen_at DESC)"
        )
        self.connection.commit()

    def close(self) -> None:
        try:
            self.connection.close()
        except sqlite3.Error:
            pass

    def insert_sample(self, series_id: str, ts_ms: int, boot_id: str, value: float, source: str) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO metric_samples(series_id, ts_utc_ms, boot_id, value, source) VALUES (?, ?, ?, ?, ?)",
            (series_id, ts_ms, boot_id, float(value), source),
        )

    def insert_event(
        self,
        rule_id: str,
        subject_id: str,
        title: str,
        detail: str,
        opened_at: str | None = None,
    ) -> int:
        now = opened_at or utc_now()
        cursor = self.connection.execute(
            """
            INSERT INTO events(rule_id, subject_id, state, title, detail, opened_at, last_seen_at, resolved_at)
            VALUES (?, ?, 'open', ?, ?, ?, ?, NULL)
            """,
            (rule_id, subject_id, title, detail, now, now),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def resolve_event(self, rule_id: str, subject_id: str, detail: str, resolved_at: str | None = None) -> bool:
        now = resolved_at or utc_now()
        cursor = self.connection.execute(
            """
            UPDATE events
            SET state = 'resolved', detail = ?, resolved_at = ?, last_seen_at = ?
            WHERE id = (
                SELECT id FROM events
                WHERE rule_id = ? AND subject_id = ? AND state = 'open'
                ORDER BY id DESC LIMIT 1
            )
            """,
            (detail, now, now, rule_id, subject_id),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def resolve_all_open(self, detail: str, resolved_at: str | None = None) -> int:
        now = resolved_at or utc_now()
        cursor = self.connection.execute(
            """
            UPDATE events
            SET state = 'resolved', detail = ?, resolved_at = ?, last_seen_at = ?
            WHERE state = 'open'
            """,
            (detail, now, now),
        )
        self.connection.commit()
        return cursor.rowcount

    def query_events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT id, rule_id, subject_id, state, title, detail, opened_at, last_seen_at, resolved_at
            FROM events ORDER BY last_seen_at DESC, id DESC LIMIT ?
            """,
            (max(1, min(100, int(limit))),),
        ).fetchall()
        return [dict(row) for row in rows]

    def query_active_events(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT rule_id, subject_id FROM events WHERE state = 'open' ORDER BY id DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def query_history(self, series_id: str, start_ms: int, end_ms: int, step_ms: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT (ts_utc_ms / ?) * ? AS bucket, boot_id,
                   COUNT(*) AS count, AVG(value) AS avg, MIN(value) AS min, MAX(value) AS max
            FROM metric_samples
            WHERE series_id = ? AND ts_utc_ms >= ? AND ts_utc_ms <= ?
            GROUP BY bucket, boot_id
            ORDER BY bucket
            """,
            (step_ms, step_ms, series_id, start_ms, end_ms),
        ).fetchall()
        return [dict(row) for row in rows]

    def prune(self, now_ms: int) -> None:
        history_cutoff = now_ms - HISTORY_RETENTION_DAYS * 24 * 3600 * 1000
        event_cutoff = (
            datetime.now(timezone.utc)
            .timestamp()
            - EVENT_RETENTION_DAYS * 24 * 3600
        )
        event_cutoff_iso = iso_from_epoch(event_cutoff)
        self.connection.execute("DELETE FROM metric_samples WHERE ts_utc_ms < ?", (history_cutoff,))
        self.connection.execute(
            """
            DELETE FROM events
            WHERE opened_at < ?
               OR id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT ?)
            """,
            (event_cutoff_iso, EVENT_RETENTION_ROWS),
        )
        self.connection.commit()


class EventTracker:
    def __init__(self, store: HistoryStore) -> None:
        self.store = store
        self.active: set[str] = set()
        self.cpu_high = 0
        self.cpu_low = 0
        self.memory_high = 0
        self.memory_low = 0
        self.storage_high = 0
        self.storage_low = 0
        self.docker_bad = 0
        self.core_bad = 0

    def _open(self, key: str, rule_id: str, subject_id: str, title: str, detail: str) -> None:
        if key in self.active:
            return
        try:
            self.store.insert_event(rule_id, subject_id, title, detail)
            self.active.add(key)
        except sqlite3.Error as exc:
            _log(f"event open failed: {exc}")

    def _resolve(self, key: str, rule_id: str, subject_id: str, detail: str) -> None:
        if key not in self.active:
            return
        try:
            self.store.resolve_event(rule_id, subject_id, detail)
            self.active.discard(key)
        except sqlite3.Error as exc:
            _log(f"event resolve failed: {exc}")

    def evaluate(self, snapshot: dict[str, Any]) -> set[str]:
        metrics = snapshot.get("metrics", {})
        cpu = metrics.get("cpu.busy_pct", {})
        memory = metrics.get("memory.used_pct", {})
        storage = snapshot.get("storage") or {}
        docker = (snapshot.get("services") or {}).get("docker") or {}

        cpu_value = cpu.get("value")
        cpu_ok = cpu.get("quality") == "ok" and isinstance(cpu_value, (int, float))
        if cpu_ok and cpu_value >= 90:
            self.cpu_high += 1
            self.cpu_low = 0
        elif cpu_ok and cpu_value <= 80:
            self.cpu_low += 1
            self.cpu_high = 0
        elif not cpu_ok:
            self.cpu_high = 0
            self.cpu_low = 0
        key = "cpu.high:host"
        if cpu_ok and self.cpu_high >= 24:
            self._open(key, "cpu.high", "host", "处理器持续高负载", "CPU 使用率连续 2 分钟高于 90%。")
        if key in self.active and self.cpu_low >= 24:
            self._resolve(key, "cpu.high", "host", "CPU 使用率已连续 2 分钟低于 80%。")

        memory_value = memory.get("value")
        memory_ok = memory.get("quality") == "ok" and isinstance(memory_value, (int, float))
        if memory_ok and memory_value >= 90:
            self.memory_high += 1
            self.memory_low = 0
        elif memory_ok and memory_value <= 85:
            self.memory_low += 1
            self.memory_high = 0
        elif not memory_ok:
            self.memory_high = 0
            self.memory_low = 0
        key = "memory.high:host"
        if memory_ok and self.memory_high >= 60:
            self._open(key, "memory.high", "host", "内存持续高占用", "内存使用率连续 5 分钟高于 90%。")
        if key in self.active and self.memory_low >= 60:
            self._resolve(key, "memory.high", "host", "内存使用率已连续 5 分钟低于 85%。")

        storage_ok = storage.get("quality") == "ok" and isinstance(storage.get("used_pct"), (int, float))
        if storage_ok and storage["used_pct"] >= 85:
            self.storage_high += 1
            self.storage_low = 0
        elif storage_ok and storage["used_pct"] <= 80:
            self.storage_low += 1
            self.storage_high = 0
        elif not storage_ok:
            self.storage_high = 0
            self.storage_low = 0
        key = "storage.high:pool"
        if storage_ok and self.storage_high >= 120:
            self._open(key, "storage.high", "pool", "存储空间使用率偏高", "存储池使用率连续 10 分钟高于 85%。")
        if key in self.active and self.storage_low >= 120:
            self._resolve(key, "storage.high", "pool", "存储池使用率已连续 10 分钟低于 80%。")

        docker_ok = docker.get("available") is True and not docker.get("stale")
        if docker_ok:
            self.docker_bad = 0
            self._resolve("docker.unavailable:engine", "docker.unavailable", "engine", "Docker Engine 已恢复可用。")
        else:
            self.docker_bad += 1
            if self.docker_bad >= 3:
                self._open(
                    "docker.unavailable:engine",
                    "docker.unavailable",
                    "engine",
                    "Docker Engine 不可用",
                    "Docker Engine 连续 15 秒不可用，请检查 Docker 服务或 Socket 权限。",
                )

        core_bad = cpu.get("quality") in {"error", "permission_denied"} or memory.get("quality") in {"error", "permission_denied"}
        if core_bad:
            self.core_bad += 1
            if self.core_bad >= 3:
                self._open(
                    "collector.error:core",
                    "collector.error",
                    "core",
                    "核心指标采集中断",
                    "CPU 或内存采集连续 15 秒失败，请检查采集器权限与挂载。",
                )
        else:
            self.core_bad = 0
            self._resolve("collector.error:core", "collector.error", "core", "核心指标采集已恢复。")

        return set(self.active)


def _quality(value: Any, source: str, unit: str, quality: str = "ok", reason: str | None = None, sampled_at: str | None = None) -> dict[str, Any]:
    return metric(value, unit, source, quality=quality, reason=reason, sampled_at=sampled_at)


def _storage_quality(storage: dict[str, Any], host_age: float | None) -> dict[str, Any]:
    result = dict(storage)
    sampled_age = age_seconds(result.get("sampled_at"))
    if sampled_age is not None and sampled_age > 600:
        result["quality"] = "stale"
        result["reason"] = "storage_sample_is_older_than_10_minutes"
    return result


def _attention(state: dict[str, Any], active: set[str]) -> dict[str, str]:
    storage = state.get("storage") or {}
    collection = state.get("collection") or {}
    if any(key.startswith("collector.error:") for key in active):
        return {
            "level": "error",
            "title": "核心采集异常",
            "detail": "CPU 或内存采集失败，请检查采集器容器和宿主挂载。",
        }
    if any(key.startswith("storage.high:") for key in active):
        return {
            "level": "error",
            "title": "存储空间需要关注",
            "detail": "存储池使用率持续偏高，建议检查空间占用。",
        }
    if storage.get("status") not in (None, "normal", "ok"):
        return {
            "level": "error",
            "title": "存储池需要关注",
            "detail": "固件报告的存储池状态不是正常。",
        }
    if any(key.startswith(("cpu.high:", "memory.high:")) for key in active):
        return {
            "level": "warn",
            "title": "资源持续高负载",
            "detail": "处理器或内存使用率持续偏高，建议查看资源动态。",
        }
    if any(key.startswith("docker.unavailable:") for key in active):
        return {
            "level": "warn",
            "title": "Docker 暂不可用",
            "detail": "Docker Engine 数据暂时无法读取，其他设备指标不受影响。",
        }
    if storage.get("quality") == "stale" or collection.get("state") in {"partial", "stale", "warming_up"}:
        return {
            "level": "warn",
            "title": "部分数据暂未更新",
            "detail": "已保留最后一次可用读数，等待下一轮采集。",
        }
    return {
        "level": "ok",
        "title": "资源运行平稳",
        "detail": "CPU、内存与存储状态正常，暂无需要处理的事项。",
    }


class Collector:
    def __init__(self) -> None:
        self.paths = Paths.from_env()
        self.paths.state.mkdir(parents=True, exist_ok=True)
        self.store = HistoryStore(self.paths.state / "history.sqlite3")
        self.previous_cpu: dict[str, int] | None = None
        self.previous_network: dict[str, Any] | None = None
        self.loop_count = 0
        self.stop = False
        self.tracker = EventTracker(self.store)
        self.store.resolve_all_open("采集器重启，旧事件已归档。", utc_now())
        self.store.insert_event(
            "monitor.started",
            "collector",
            "开始本地监测",
            "设备管家采集器已启动，历史从此刻开始记录。",
        )

    def build_snapshot(self) -> tuple[dict[str, Any], dict[str, tuple[float, str]]]:
        now = time.time()
        now_iso = iso_from_epoch(now)
        host = read_json(self.paths.state / "host.json") or {}
        host_age = age_seconds(host.get("generated_at"))
        host_fresh = host_age is not None and host_age <= 90
        meta = host_meta(host)
        boot_id = read_boot_id(self.paths)
        hostname = host.get("hostname") or read_hostname(self.paths)
        uptime = read_uptime(self.paths)
        cpu_count = read_cpu_count(self.paths)
        warnings: list[str] = []

        current_cpu = parse_proc_stat(read_text(self.paths.proc / "stat"))
        cpu_pct = cpu_percent(self.previous_cpu, current_cpu)
        self.previous_cpu = current_cpu
        if current_cpu is None:
            cpu_metric = _quality(None, "linux.proc_stat", "percent", "error", "/proc/stat 不可读")
        elif cpu_pct is None:
            cpu_metric = _quality(None, "linux.proc_stat", "percent", "warming_up", "需要至少两次 CPU 采样")
        else:
            cpu_metric = _quality(cpu_pct, "linux.proc_stat", "percent")

        memory_values = parse_meminfo(read_text(self.paths.proc / "meminfo"))
        if not memory_values:
            memory_used = _quality(None, "linux.proc_meminfo", "percent", "error", "MemAvailable 不可读")
            memory_bytes = _quality(None, "linux.proc_meminfo", "bytes", "error", "内存信息不可读")
            memory_total = _quality(None, "linux.proc_meminfo", "bytes", "error", "内存信息不可读")
        else:
            used_pct = round(memory_values["used"] / memory_values["total"] * 100, 2)
            memory_used = _quality(used_pct, "linux.proc_meminfo", "percent")
            memory_bytes = _quality(memory_values["used"], "linux.proc_meminfo", "bytes")
            memory_total = _quality(memory_values["total"], "linux.proc_meminfo", "bytes")

        loads = parse_loadavg(read_text(self.paths.proc / "loadavg"))
        load_metrics = {
            "load.1m": _quality(loads[0], "linux.proc_loadavg", "load", "ok" if loads else "error", None if loads else "负载不可读"),
            "load.5m": _quality(loads[1], "linux.proc_loadavg", "load", "ok" if loads else "error", None if loads else "负载不可读"),
            "load.15m": _quality(loads[2], "linux.proc_loadavg", "load", "ok" if loads else "error", None if loads else "负载不可读"),
        } if loads else {
            "load.1m": _quality(None, "linux.proc_loadavg", "load", "error", "负载不可读"),
            "load.5m": _quality(None, "linux.proc_loadavg", "load", "error", "负载不可读"),
            "load.15m": _quality(None, "linux.proc_loadavg", "load", "error", "负载不可读"),
        }

        temperature, temperature_source = read_cpu_temperature(self.paths)
        if temperature is None:
            temp_metric = _quality(None, "linux.thermal", "celsius", "unsupported", "未发现 CPU 温度传感器")
        else:
            temp_metric = _quality(temperature, temperature_source or "linux.thermal", "celsius")

        fan = fan_from_host(host)
        if fan and fan.get("rpm") is not None:
            fan_age = age_seconds(fan.get("sampled_at") or host.get("generated_at"))
            fan_quality = "stale" if fan_age is not None and fan_age > 180 else "ok"
            fan_metric = _quality(fan.get("rpm"), fan.get("source", "firmware.mifan"), "rpm", fan_quality, "风扇读数已过期" if fan_quality == "stale" else None, fan.get("sampled_at"))
        else:
            fan_metric = _quality(None, "firmware.mifan", "rpm", "unsupported", "宿主探针未提供风扇转速")

        iface = default_interface(self.paths)
        network = read_network_counters(self.paths)
        network_quality = "warming_up" if iface else "unsupported"
        network_reason = "需要两次网络计数采样" if iface else "没有发现默认路由网卡"
        rx_metric = _quality(None, "linux.proc_net_dev", "bytes_per_second", network_quality, network_reason)
        tx_metric = _quality(None, "linux.proc_net_dev", "bytes_per_second", network_quality, network_reason)
        if (
            network
            and self.previous_network
            and network["iface"] == self.previous_network["iface"]
        ):
            dt = network["monotonic"] - self.previous_network["monotonic"]
            rx_delta = network["rx"] - self.previous_network["rx"]
            tx_delta = network["tx"] - self.previous_network["tx"]
            if dt > 0 and rx_delta >= 0 and tx_delta >= 0:
                rx_rate = rx_delta / dt
                tx_rate = tx_delta / dt
                source = f"linux.proc_net_dev.{network['iface']}"
                rx_metric = _quality(round(rx_rate, 2), source, "bytes_per_second")
                tx_metric = _quality(round(tx_rate, 2), source, "bytes_per_second")
        self.previous_network = network

        storage = storage_from_pool(host, self.paths)
        if storage is None:
            storage = storage_from_statvfs(self.paths)
            if storage:
                warnings.append("存储池来自 statvfs，不是固件池口径")
        if storage:
            storage = _storage_quality(storage, host_age)
        else:
            warnings.append("存储数据不可读")

        docker = docker_from_host(host)
        docker_age = age_seconds(docker.get("sampled_at"))
        if docker.get("available") and (docker_age is None or docker_age > 60):
            docker["stale"] = True
            warnings.append("Docker 数据已过期")
        else:
            docker["stale"] = False
        system = system_from_host(host)
        system_age = age_seconds(system.get("sampled_at"))
        if system.get("available") and (system_age is None or system_age > 60):
            system["stale"] = True
            warnings.append("系统服务数据已过期")
        else:
            system["stale"] = False

        collection_state = "ok"
        if cpu_metric["quality"] == "warming_up" or rx_metric["quality"] == "warming_up":
            collection_state = "warming_up"
        if any(
            item.get("quality") in {"stale", "error", "permission_denied", "unsupported"}
            for item in (cpu_metric, memory_used, temp_metric, fan_metric, rx_metric, tx_metric)
        ) or (storage and storage.get("quality") != "ok"):
            collection_state = "partial"
        if host_age is None:
            collection_state = "partial"
            warnings.append("宿主探针尚未提供数据")
        if storage is None:
            collection_state = "partial"
        if docker.get("stale") or system.get("stale"):
            collection_state = "partial"
        if host_age is not None and host_age > 90:
            collection_state = "stale"
            warnings.append("宿主探针数据已超过 90 秒")

        metrics_map = {
            "cpu.busy_pct": cpu_metric,
            "memory.used_pct": memory_used,
            "memory.used_bytes": memory_bytes,
            "memory.total_bytes": memory_total,
            "temperature.cpu_c": temp_metric,
            "fan.rpm": fan_metric,
            "network.rx_bytes_per_sec": rx_metric,
            "network.tx_bytes_per_sec": tx_metric,
            **load_metrics,
        }
        numeric_samples: dict[str, tuple[float, str]] = {}
        history_keys = (
            "cpu.busy_pct",
            "memory.used_pct",
            "temperature.cpu_c",
            "network.rx_bytes_per_sec",
            "network.tx_bytes_per_sec",
            "load.1m",
        )
        for key in history_keys:
            item = metrics_map.get(key) or {}
            value = item.get("value")
            if item.get("quality") == "ok" and isinstance(value, (int, float)) and math.isfinite(float(value)):
                numeric_samples[key] = (float(value), str(item.get("source") or "unknown"))

        state: dict[str, Any] = {
            "schema_version": 1,
            "request_id": os.urandom(8).hex(),
            "generated_at": now_iso,
            "device": {
                "hostname": hostname,
                "model": meta["model"],
                "firmware": meta["firmware"],
                "channel": meta["channel"],
                "build": meta["build"],
                "build_time": meta["build_time"],
                "os": meta["os"],
                "uptime_seconds": uptime,
                "boot_id": boot_id,
                "cpu_count": cpu_count,
            },
            "collection": {
                "state": collection_state,
                "last_success_at": now_iso,
                "host_sampled_at": host.get("generated_at"),
                "host_age_seconds": host_age,
                "warnings": warnings,
            },
            "metrics": metrics_map,
            "storage": storage,
            "services": {
                "docker": docker,
                "containers": docker.get("containers") or [],
                "system": system,
            },
            "capabilities": {
                "history": True,
                "docker": bool(docker.get("available") and not docker.get("stale")),
                "firmware_pool": bool(storage and storage.get("source") == "firmware.getPoolInfo"),
                "systemd": bool(system.get("available") and not system.get("stale")),
                "fan": fan_metric["quality"] == "ok",
                "storage_source": (storage or {}).get("source"),
            },
            "provenance": {
                "host_probe": "deploy/host-probe.py",
                "pool_sampled_at": (host.get("pool") or {}).get("sampled_at") if isinstance(host.get("pool"), dict) else host.get("pool_sampled_at"),
                "docker_sampled_at": docker.get("sampled_at"),
                "systemd_sampled_at": system.get("sampled_at"),
            },
        }
        return state, numeric_samples

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        _log(f"collector started interval={INTERVAL_SECONDS}s")
        while not self.stop:
            started = time.monotonic()
            try:
                snapshot, samples = self.build_snapshot()
                active = self.tracker.evaluate(snapshot)
                snapshot["attention"] = _attention(snapshot, active)
                boot_id = snapshot["device"]["boot_id"]
                ts_ms = int(time.time() * 1000)
                for series_id, (value, source) in samples.items():
                    self.store.insert_sample(series_id, ts_ms, boot_id, value, source)
                self.store.connection.commit()
                write_json_atomic(self.paths.state / "snapshot.json", snapshot)
                self.loop_count += 1
                if self.loop_count % 120 == 0:
                    self.store.prune(ts_ms)
            except Exception as exc:
                _log(f"collector iteration failed: {exc}")
            elapsed = time.monotonic() - started
            time.sleep(max(1.0, INTERVAL_SECONDS - elapsed))
        _log("collector stopped")
        self.store.close()

    def _stop(self, signum: int, frame: Any) -> None:
        _log(f"received signal {signum}")
        self.stop = True


def main() -> None:
    Collector().run()


if __name__ == "__main__":
    main()
