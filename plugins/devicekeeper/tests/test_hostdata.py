from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from app import hostdata


class HostdataTest(unittest.TestCase):
    def test_missing_docker_socket_is_explicit(self) -> None:
        result = hostdata.read_docker("/definitely/missing/docker.sock")
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "docker_socket_not_found")
        self.assertEqual(result["containers"], [])

    def test_docker_container_projection_removes_sensitive_fields(self) -> None:
        version = {"Version": "20.10.17", "ApiVersion": "1.41"}
        containers = [
            {
                "Id": "a" * 64,
                "Names": ["/miot_central"],
                "Image": "example/image:1",
                "State": "running",
                "Status": "Up 2 days (healthy)",
                "Env": ["SECRET=value"],
                "Mounts": [{"Source": "/private"}],
                "Labels": {"secret": "yes"},
            }
        ]
        result = hostdata.sanitize_docker(version, containers)
        item = result["containers"][0]
        self.assertEqual(item["id"], "a" * 12)
        self.assertEqual(item["health"], "healthy")
        self.assertNotIn("Env", item)
        self.assertNotIn("Mounts", item)
        self.assertNotIn("Labels", item)
        self.assertEqual(result["containers_running"], 1)

    def test_systemd_active_exited_is_not_rewritten_as_failure(self) -> None:
        output = (
            "LoadState=loaded\n"
            "ActiveState=active\n"
            "SubState=exited\n"
            "UnitFileState=enabled\n"
        )
        completed = SimpleNamespace(stdout=output)
        with mock.patch.object(hostdata.shutil, "which", return_value="/usr/bin/systemctl"):
            with mock.patch.object(hostdata.subprocess, "run", return_value=completed):
                result = hostdata.read_systemd((("minas.plugin_center.service", "插件中心"),))
        service = result["services"][0]
        self.assertTrue(result["available"])
        self.assertEqual(service["active_state"], "active")
        self.assertEqual(service["sub_state"], "exited")

    def test_fan_uses_first_numeric_field(self) -> None:
        completed = SimpleNamespace(stdout="Fan1: 1240 RPM\n", returncode=0)
        with mock.patch.object(hostdata.shutil, "which", return_value="/usr/sbin/mifan"):
            with mock.patch.object(hostdata.Path, "exists", return_value=True):
                with mock.patch.object(hostdata.subprocess, "run", return_value=completed):
                    fan = hostdata.read_fan()
        self.assertEqual(fan["rpm"], 1240)

    def test_pool_projection_drops_serial_and_unknown_fields(self) -> None:
        payload = {
            "internal_pool": [
                {
                    "total_size": 100,
                    "used_size": 25,
                    "serial": "private",
                    "disks": [{"dev": "sda", "serial": "disk-private", "health": "normal"}],
                }
            ]
        }
        result = hostdata._sanitize_pool(payload)
        self.assertNotIn("serial", result["internal_pool"][0])
        self.assertNotIn("serial", result["internal_pool"][0]["disks"][0])


if __name__ == "__main__":
    unittest.main()
