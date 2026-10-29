#!/usr/bin/python3
"""Register devicekeeper in the Xiaomi NAS user plugin list."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path


PLUGIN = "devicekeeper"


def _replace_symlink(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        if Path(os.readlink(link)) == target:
            return
        link.unlink()
    elif link.exists():
        raise RuntimeError(f"refusing to replace non-symlink path: {link}")
    link.symlink_to(target)


def _backup(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{PLUGIN}-{stamp}")
    shutil.copy2(path, backup)
    return backup


def _write_json_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uid", default=os.environ.get("DEVICEKEEPER_UID", ""))
    parser.add_argument("--root", default="/data/plugins/devicekeeper")
    parser.add_argument("--plugin-data", default="/data/plugin")
    parser.add_argument("--home", default="/home")
    args = parser.parse_args()
    if not args.uid:
        parser.error("--uid or DEVICEKEEPER_UID is required")

    root = Path(args.root).resolve()
    uid = str(args.uid)
    user_dir = f"u{uid}"
    info_path = root / "INFO"
    config_path = root / "ui" / "config"
    icon_source = root / "ui" / "icon.png"
    list_path = Path(args.plugin_data) / f"{user_dir}.list"
    user_www = Path(args.plugin_data) / "www" / user_dir
    icon_dir = Path(args.plugin_data) / "www" / "icon"
    home_plugin = Path(args.home) / user_dir / "plugin" / PLUGIN

    info = json.loads(info_path.read_text(encoding="utf-8"))
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if info.get("plugin") != PLUGIN:
        raise RuntimeError("INFO.plugin must be devicekeeper")
    if not icon_source.is_file():
        raise RuntimeError(f"missing icon: {icon_source}")
    if not list_path.is_file():
        raise RuntimeError(f"plugin list not found: {list_path}")

    src_dir = root / "src"
    _replace_symlink(root / "ui", src_dir / "ui")
    _replace_symlink(root, home_plugin)
    _replace_symlink(root / "ui", user_www / PLUGIN)
    icon_dir.mkdir(parents=True, exist_ok=True)
    icon_target = icon_dir / f"{PLUGIN}.icon"
    shutil.copyfile(icon_source, icon_target)
    os.chmod(icon_target, 0o644)

    original = json.loads(list_path.read_text(encoding="utf-8"))
    if not isinstance(original, dict):
        raise RuntimeError("plugin list root must be an object")
    backup = _backup(list_path)
    now = int(time.time())
    original[PLUGIN] = {
        "resource": {
            "mpk": str(info_path),
            "icon": f"/icon/{PLUGIN}.icon",
            "preview": [],
        },
        "status": "running",
        "install": True,
        "upgrade": False,
        "enable": True,
        "changetime": now,
        "icon": f"/icon/{PLUGIN}.icon",
        "progress": "100",
        "info": info,
        "frontend": config,
    }
    _write_json_atomic(list_path, original)
    print(f"registered {PLUGIN}; backup={backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
