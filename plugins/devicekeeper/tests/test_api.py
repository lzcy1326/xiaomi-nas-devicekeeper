from __future__ import annotations

import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app import api


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
        api.SNAPSHOT_PATH = root / "snapshot.json"
        api.DB_PATH = root / "history.sqlite3"
        api.SNAPSHOT_PATH.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": "2026-10-29T12:00:00Z",
                    "collection": {"state": "ok", "warnings": []},
                    "device": {"hostname": "nas-demo"},
                    "metrics": {},
                    "storage": None,
                    "services": {},
                    "capabilities": {},
                }
            ),
            encoding="utf-8",
        )
        cls.server = api.DevicekeeperHTTPServer(("127.0.0.1", 0), api.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.temporary.cleanup()

    def request(self, method: str, path: str, body: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(method, path, body=body)
        response = connection.getresponse()
        payload = response.read()
        headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        return response.status, headers, payload

    def test_static_index_and_security_headers(self) -> None:
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html>", body.lower())
        self.assertIn("content-security-policy", headers)
        self.assertEqual(headers["x-content-type-options"], "nosniff")

    def test_unknown_static_path_is_not_an_arbitrary_file_reader(self) -> None:
        status, _, _ = self.request("GET", "/../INFO")
        self.assertEqual(status, 404)

    def test_history_parameter_validation(self) -> None:
        status, _, body = self.request("GET", "/api/v1/history?metric=nope&range=24h")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["code"], "invalid_metric")
        status, _, _ = self.request("GET", "/api/v1/history?metric=cpu.busy_pct&range=bad")
        self.assertEqual(status, 400)

    def test_empty_history_is_successful(self) -> None:
        status, _, body = self.request("GET", "/api/v1/history?metric=cpu.busy_pct&range=24h")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["buckets"], [])
        self.assertEqual(payload["coverage"], 0)

    def test_events_parameter_validation(self) -> None:
        status, _, _ = self.request("GET", "/api/v1/events?cursor=nope")
        self.assertEqual(status, 400)
        status, _, _ = self.request("GET", "/api/v1/events?limit=101")
        self.assertEqual(status, 400)

    def test_post_body_limit(self) -> None:
        status, _, _ = self.request("POST", "/api/v1/refresh", b"x" * 4097)
        self.assertEqual(status, 413)

    def test_event_cursor_pagination(self) -> None:
        connection = sqlite3.connect(api.DB_PATH)
        connection.execute(
            """
            CREATE TABLE events (
                id INTEGER PRIMARY KEY,
                rule_id TEXT,
                subject_id TEXT,
                state TEXT,
                title TEXT,
                detail TEXT,
                opened_at TEXT,
                last_seen_at TEXT,
                resolved_at TEXT
            )
            """
        )
        for event_id in range(1, 6):
            connection.execute(
                "INSERT INTO events VALUES (?, 'r', 's', 'open', ?, 'd', ?, ?, NULL)",
                (event_id, f"event-{event_id}", f"2026-10-29T12:00:0{event_id}Z", f"2026-10-29T12:00:0{event_id}Z"),
            )
        connection.commit()
        connection.close()
        first_page, cursor = api.query_events(2)
        second_page, next_cursor = api.query_events(2, int(cursor))
        self.assertEqual([item["id"] for item in first_page], [5, 4])
        self.assertEqual([item["id"] for item in second_page], [3, 2])
        self.assertIsNotNone(next_cursor)


if __name__ == "__main__":
    unittest.main()
