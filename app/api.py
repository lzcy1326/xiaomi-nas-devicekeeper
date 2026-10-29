"""Small bounded HTTP API and static server for devicekeeper."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


STATE_DIR = Path(os.environ.get("STATE_DIR", "/state"))
SNAPSHOT_PATH = STATE_DIR / "snapshot.json"
DB_PATH = STATE_DIR / "history.sqlite3"
STATIC_DIR = Path(
    os.environ.get("STATIC_DIR", str(Path(__file__).resolve().parent.parent / "ui"))
)

HISTORY_METRICS = {
    "cpu.busy_pct",
    "memory.used_pct",
    "temperature.cpu_c",
    "network.rx_bytes_per_sec",
    "network.tx_bytes_per_sec",
    "load.1m",
}
RANGES = {
    "1h": {"seconds": 3600, "step": 30},
    "24h": {"seconds": 86400, "step": 300},
    "7d": {"seconds": 604800, "step": 1800},
}
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/icon.png": ("icon.png", "image/png"),
    "/favicon.ico": ("icon.png", "image/png"),
}

_cache_lock = threading.Lock()
_cache_mtime_ns = -1
_cache_size = -1
_cache_snapshot: dict | None = None
_request_slots = threading.BoundedSemaphore(8)


def _iso(epoch_ms: int | float | None) -> str | None:
    if epoch_ms is None:
        return None
    return datetime.fromtimestamp(float(epoch_ms) / 1000.0, timezone.utc).isoformat().replace("+00:00", "Z")


def _now_ms() -> int:
    return int(time.time() * 1000)


def read_snapshot() -> tuple[dict | None, float | None]:
    global _cache_mtime_ns, _cache_size, _cache_snapshot
    try:
        stat = SNAPSHOT_PATH.stat()
    except OSError:
        return None, None
    with _cache_lock:
        if stat.st_mtime_ns == _cache_mtime_ns and stat.st_size == _cache_size and _cache_snapshot is not None:
            age = max(0.0, time.time() - stat.st_mtime)
            return _cache_snapshot, age
    try:
        payload = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, max(0.0, time.time() - stat.st_mtime)
    if not isinstance(payload, dict):
        return None, max(0.0, time.time() - stat.st_mtime)
    with _cache_lock:
        _cache_mtime_ns = stat.st_mtime_ns
        _cache_size = stat.st_size
        _cache_snapshot = payload
    return payload, max(0.0, time.time() - stat.st_mtime)


def _read_only_connection() -> sqlite3.Connection | None:
    if not DB_PATH.exists():
        return None
    try:
        connection = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=3.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=3000")
        return connection
    except sqlite3.Error:
        return None


def query_history(metric: str, range_key: str) -> dict:
    config = RANGES[range_key]
    to_ms = _now_ms()
    from_ms = to_ms - config["seconds"] * 1000
    step_ms = config["step"] * 1000
    connection = _read_only_connection()
    buckets: list[dict] = []
    available_from = None
    available_to = None
    if connection is not None:
        try:
            rows = connection.execute(
                """
                SELECT (ts_utc_ms / ?) * ? AS bucket, boot_id,
                       COUNT(*) AS count, AVG(value) AS avg, MIN(value) AS min, MAX(value) AS max
                FROM metric_samples
                WHERE series_id = ? AND ts_utc_ms >= ? AND ts_utc_ms <= ?
                GROUP BY bucket, boot_id
                ORDER BY bucket
                """,
                (step_ms, step_ms, metric, from_ms, to_ms),
            ).fetchall()
            for row in rows:
                start_ms = int(row["bucket"])
                buckets.append(
                    {
                        "start": _iso(start_ms),
                        "end": _iso(start_ms + step_ms),
                        "count": int(row["count"]),
                        "avg": round(float(row["avg"]), 4),
                        "min": round(float(row["min"]), 4),
                        "max": round(float(row["max"]), 4),
                        "boot_id": row["boot_id"],
                    }
                )
            bounds = connection.execute(
                "SELECT MIN(ts_utc_ms) AS first, MAX(ts_utc_ms) AS last FROM metric_samples WHERE series_id = ?",
                (metric,),
            ).fetchone()
            if bounds and bounds["first"] is not None:
                available_from = _iso(bounds["first"])
                available_to = _iso(bounds["last"])
        except sqlite3.Error:
            buckets = []
        finally:
            connection.close()
    expected = max(1, config["seconds"] // config["step"])
    segments = []
    current = None
    for bucket in buckets:
        if current is None or current["boot_id"] != bucket["boot_id"]:
            if current is not None:
                segments.append(current)
            current = {
                "boot_id": bucket["boot_id"],
                "from": bucket["start"],
                "to": bucket["end"],
                "count": 1,
            }
        else:
            current["to"] = bucket["end"]
            current["count"] += 1
    if current is not None:
        segments.append(current)
    return {
        "metric": metric,
        "range": range_key,
        "requested_from": _iso(from_ms),
        "requested_to": _iso(to_ms),
        "available_from": available_from,
        "available_to": available_to,
        "resolution_seconds": config["step"],
        "coverage": round(min(1.0, len(buckets) / expected), 4),
        "segments": segments,
        "buckets": buckets,
    }


def query_events(limit: int, before_id: int | None = None) -> tuple[list[dict], str | None]:
    connection = _read_only_connection()
    if connection is None:
        return [], None
    try:
        bounded_limit = max(1, min(100, limit))
        if before_id is None:
            rows = connection.execute(
                """
                SELECT id, rule_id, subject_id, state, title, detail, opened_at, last_seen_at, resolved_at
                FROM events ORDER BY last_seen_at DESC, id DESC LIMIT ?
                """,
                (bounded_limit + 1,),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT id, rule_id, subject_id, state, title, detail, opened_at, last_seen_at, resolved_at
                FROM events WHERE id < ? ORDER BY last_seen_at DESC, id DESC LIMIT ?
                """,
                (before_id, bounded_limit + 1),
            ).fetchall()
        has_more = len(rows) > bounded_limit
        page = [dict(row) for row in rows[:bounded_limit]]
        next_cursor = str(page[-1]["id"]) if has_more and page else None
        return page, next_cursor
    except sqlite3.Error:
        return [], None
    finally:
        connection.close()


class DevicekeeperHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server_version = "devicekeeper/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:
        return

    def _security_headers(self, cache: str = "no-store") -> None:
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; base-uri 'self'; form-action 'self'; "
            "frame-ancestors 'self'",
        )

    def _json(self, status: int, payload: dict, cache: str = "no-store") -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._security_headers(cache)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(status, {"ok": False, "code": code, "message": message})

    def _empty(self, status: int) -> None:
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _static(self, path: str, head: bool = False) -> None:
        entry = STATIC_FILES.get(path)
        if entry is None:
            self._error(HTTPStatus.NOT_FOUND, "not_found", "资源不存在")
            return
        filename, content_type = entry
        file_path = STATIC_DIR / filename
        try:
            body = file_path.read_bytes()
            stat = file_path.stat()
        except OSError:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "static_unavailable", "静态资源不可用")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Last-Modified", self.date_time_string(stat.st_mtime))
        cache = "no-store" if filename == "index.html" else "public, max-age=3600"
        self._security_headers(cache)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def do_HEAD(self) -> None:
        self.do_GET(head=True)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, HEAD, POST, OPTIONS")
        self._security_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self, head: bool = False) -> None:
        if not _request_slots.acquire(blocking=False):
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "busy", "请求过多，请稍后重试")
            return
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path in STATIC_FILES:
                self._static(path, head=head)
                return
            if path == "/api/v1/health":
                snapshot, age = read_snapshot()
                healthy = snapshot is not None and age is not None and age < 30
                self._json(
                    HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "ok": healthy,
                        "snapshot_age_seconds": round(age, 2) if age is not None else None,
                        "collector_state": (snapshot or {}).get("collection", {}).get("state"),
                        "generated_at": (snapshot or {}).get("generated_at"),
                        "version": "1.0.0",
                    },
                )
                return
            if path == "/api/v1/capabilities":
                snapshot, _ = read_snapshot()
                if snapshot is None:
                    self._error(HTTPStatus.SERVICE_UNAVAILABLE, "collector_unavailable", "采集器尚未生成快照")
                    return
                self._json(HTTPStatus.OK, {"capabilities": snapshot.get("capabilities", {})})
                return
            if path == "/api/v1/snapshot":
                snapshot, age = read_snapshot()
                if snapshot is None:
                    self._error(HTTPStatus.SERVICE_UNAVAILABLE, "collector_unavailable", "采集器尚未生成快照")
                    return
                payload = dict(snapshot)
                payload["snapshot_age_seconds"] = round(age, 2) if age is not None else None
                self._json(HTTPStatus.OK, payload)
                return
            if path == "/api/v1/storage":
                snapshot, _ = read_snapshot()
                if snapshot is None:
                    self._error(HTTPStatus.SERVICE_UNAVAILABLE, "collector_unavailable", "采集器尚未生成快照")
                    return
                self._json(HTTPStatus.OK, {"storage": snapshot.get("storage"), "device": snapshot.get("device")})
                return
            if path == "/api/v1/services":
                snapshot, _ = read_snapshot()
                if snapshot is None:
                    self._error(HTTPStatus.SERVICE_UNAVAILABLE, "collector_unavailable", "采集器尚未生成快照")
                    return
                self._json(HTTPStatus.OK, snapshot.get("services") or {})
                return
            if path == "/api/v1/events":
                query = parse_qs(parsed.query)
                try:
                    limit = int((query.get("limit") or ["50"])[0])
                except ValueError:
                    self._error(HTTPStatus.BAD_REQUEST, "invalid_limit", "limit 必须是整数")
                    return
                if limit < 1 or limit > 100:
                    self._error(HTTPStatus.BAD_REQUEST, "invalid_limit", "limit 必须在 1 到 100 之间")
                    return
                before_id_raw = (query.get("cursor") or [None])[0]
                try:
                    before_id = int(before_id_raw) if before_id_raw is not None else None
                except ValueError:
                    self._error(HTTPStatus.BAD_REQUEST, "invalid_cursor", "cursor 必须是正整数")
                    return
                if before_id is not None and before_id <= 0:
                    self._error(HTTPStatus.BAD_REQUEST, "invalid_cursor", "cursor 必须是正整数")
                    return
                events, next_cursor = query_events(limit, before_id)
                self._json(HTTPStatus.OK, {"events": events, "next_cursor": next_cursor})
                return
            if path == "/api/v1/history":
                query = parse_qs(parsed.query)
                metric = (query.get("metric") or [""])[0]
                range_key = (query.get("range") or ["24h"])[0]
                if metric not in HISTORY_METRICS:
                    self._error(HTTPStatus.BAD_REQUEST, "invalid_metric", "metric 不在允许范围内")
                    return
                if range_key not in RANGES:
                    self._error(HTTPStatus.BAD_REQUEST, "invalid_range", "range 必须是 1h、24h 或 7d")
                    return
                self._json(HTTPStatus.OK, query_history(metric, range_key))
                return
            if path.startswith("/api/"):
                self._error(HTTPStatus.NOT_FOUND, "not_found", "接口不存在")
                return
            self._error(HTTPStatus.NOT_FOUND, "not_found", "资源不存在")
        finally:
            _request_slots.release()

    def do_POST(self) -> None:
        if not _request_slots.acquire(blocking=False):
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "busy", "请求过多，请稍后重试")
            return
        try:
            path = urlparse(self.path).path
            if path != "/api/v1/refresh":
                self._error(HTTPStatus.NOT_FOUND, "not_found", "接口不存在")
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._error(HTTPStatus.BAD_REQUEST, "invalid_content_length", "Content-Length 无效")
                return
            if content_length > 4096:
                self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body_too_large", "请求体过大")
                return
            if content_length:
                self.rfile.read(content_length)
            snapshot, age = read_snapshot()
            if snapshot is None:
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, "collector_unavailable", "采集器尚未生成快照")
                return
            self._json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "generated_at": snapshot.get("generated_at"),
                    "snapshot_age_seconds": round(age, 2) if age is not None else None,
                    "message": "已读取最新快照",
                },
            )
        finally:
            _request_slots.release()


def main() -> None:
    port = int(os.environ.get("PORT", "9365"))
    bind = os.environ.get("BIND_ADDRESS", "0.0.0.0")
    server = DevicekeeperHTTPServer((bind, port), Handler)
    print(f"devicekeeper api listening on {bind}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
