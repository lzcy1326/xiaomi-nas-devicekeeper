from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.metrics import (
    Paths,
    cpu_percent,
    default_interface,
    parse_meminfo,
    parse_proc_stat,
    storage_from_pool,
    storage_from_statvfs,
)


class MetricsTest(unittest.TestCase):
    def test_proc_stat_does_not_double_count_guest(self) -> None:
        text = "cpu  100 2 50 1000 10 0 5 3 999 999\ncpu0 1 2 3 4\n"
        parsed = parse_proc_stat(text)
        self.assertEqual(
            parsed,
            {"total": 1170, "idle": 1000, "iowait": 10},
        )
        self.assertEqual(
            cpu_percent({"total": 0, "idle": 0, "iowait": 0}, parsed),
            13.68,
        )

    def test_meminfo_requires_mem_available(self) -> None:
        self.assertIsNone(parse_meminfo("MemTotal: 1000 kB\nMemFree: 500 kB\n"))
        self.assertEqual(
            parse_meminfo("MemTotal: 1000 kB\nMemAvailable: 250 kB\n"),
            {"total": 1024000, "available": 256000, "used": 768000},
        )

    def test_default_interface_skips_virtual_and_uses_largest_physical(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = Paths(host=root, state=root / "state", pool=root)
            net = root / "sys/class/net"
            for name, rx in (("eth0", "200"), ("eth1", "500"), ("docker0", "9999"), ("tailscale0", "99999")):
                iface = net / name
                (iface / "statistics").mkdir(parents=True)
                (iface / "operstate").write_text("up\n", encoding="utf-8")
                (iface / "statistics/rx_bytes").write_text(rx + "\n", encoding="utf-8")
            self.assertEqual(default_interface(paths), "eth1")

    def test_default_interface_prefers_host_namespace_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = Paths(host=root, state=root / "state", pool=root)
            for relative, iface in (("proc/net/route", "eth0"), ("proc/1/net/route", "enu1u3")):
                route = root / relative
                route.parent.mkdir(parents=True, exist_ok=True)
                route.write_text(
                    "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
                    f"{iface}\t00000000\t01001FA8\t0003\t0\t0\t100\t00000000\t0\t0\t0\n",
                    encoding="utf-8",
                )
            self.assertEqual(default_interface(paths), "enu1u3")

    def test_pool_source_remains_firmware_specific(self) -> None:
        host = {
            "pool": {
                "sampled_at": "2026-10-29T12:00:00Z",
                "internal_pool": [
                    {
                        "total_size": "8000",
                        "used_size": "2000",
                        "status": "normal",
                        "pool_type": "cfs",
                        "disk_num": 1,
                        "disks": [
                            {
                                "dev": "sda",
                                "slot": 1,
                                "type": "HDD",
                                "total_size": "8000",
                                "temperature": "40℃",
                                "health": "normal",
                                "state": "inuse",
                            }
                        ],
                    }
                ],
            }
        }
        storage = storage_from_pool(host, Paths(Path("/nonexistent"), Path("/nonexistent"), Path("/nonexistent")))
        self.assertIsNotNone(storage)
        assert storage is not None
        self.assertEqual(storage["source"], "firmware.getPoolInfo")
        self.assertEqual(storage["available_bytes"], 6000)
        self.assertEqual(storage["used_pct"], 25.0)
        self.assertEqual(storage["disks"][0]["temperature_c"], 40.0)

    @unittest.skipUnless(hasattr(os, "statvfs"), "statvfs is POSIX-only")
    def test_statvfs_fallback_is_a_separate_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = Paths(host=root, state=root / "state", pool=root)
            storage = storage_from_statvfs(paths)
        self.assertIsNotNone(storage)
        assert storage is not None
        self.assertEqual(storage["source"], "statvfs")
        self.assertEqual(storage["status"], "ok")
        self.assertGreater(storage["total_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
