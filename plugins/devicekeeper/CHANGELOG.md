# Changelog

## 1.0.1 - 2026-10-31

- Fixed desktop client API requests resolving to `/D:/plugin/...` instead of
  `/plugin/...`.
- Documented the one-time desktop client restart needed after installing a
  plugin while the client is already running.

## 1.0.0 - 2026-10-30

- Initial read-only Xiaomi NAS device monitor.
- CPU, memory, load, uptime, temperature, fan, network, storage and Docker
  snapshots.
- Fixed systemd service whitelist.
- Local SQLite history and rule events.
- Responsive desktop and mobile UI.
- Docker healthcheck and host-probe timer.
- Fixed `devicekeeper.cgi` UI directory resolution when started through
  `plugin.cgi` `fexecve`.
- Added admin-only CGI permission check based on the logged-in user `MIUID`.
- Verified 18 unit tests and 72/72 deployment acceptance checks.
