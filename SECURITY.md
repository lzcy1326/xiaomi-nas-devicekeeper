# Security Policy

## Scope

Devicekeeper is a read-only monitor. It does not restart the NAS, start or stop
containers, change fan speed, or delete files. The only host-level privileged
component is `devicekeeper-host-probe.service`, which reads Docker metadata and
fixed firmware helpers as root and writes a sanitized JSON snapshot.

## Reporting

Please do not open a public issue for a vulnerability that could expose a NAS
to unauthorized access. Contact the repository owner through the GitHub profile
linked in the repository metadata, or use GitHub's private vulnerability
reporting if it is enabled.

Include the firmware version, the affected endpoint or file, a minimal
reproduction, and the expected impact. Do not include tokens, private keys,
serial numbers, or real device identifiers.

## Deployment notes

- Keep `127.0.0.1:9365` on loopback. Do not publish it through a public reverse
  proxy or a firewall rule.
- Access the UI through the existing Mi Home `plugin.cgi` chain.
- Do not mount `/var/run/docker.sock` into the web container.
- Review the install script before running it as root.
- Keep the `.list` backup created by the installer.
