#!/bin/sh
set -eu

ROOT="${DEVICEKEEPER_ROOT:-/data/plugins/devicekeeper}"
DOCKER="${DOCKER_BIN:-/data/docker/docker}"
IMAGE="${DEVICEKEEPER_IMAGE:-local/devicekeeper:1.0.0}"
ALPINE_IMAGE="${DEVICEKEEPER_ALPINE_IMAGE:-docker.1ms.run/library/alpine:3.20}"
UID_VALUE="${DEVICEKEEPER_UID:-}"
STATE="$ROOT/state"

[ -n "$UID_VALUE" ] || {
    echo "DEVICEKEEPER_UID is required" >&2
    exit 1
}
[ "$(id -u)" = "0" ] || {
    echo "install-nas.sh must run as root" >&2
    exit 1
}
[ -f "$ROOT/INFO" ] || {
    echo "missing plugin files at $ROOT" >&2
    exit 1
}
[ -x "$DOCKER" ] || {
    echo "docker binary not found: $DOCKER" >&2
    exit 1
}

mkdir -p "$STATE" "$ROOT/src"
chown 10001:10001 "$STATE"
chmod 0770 "$STATE"
chmod 0755 "$ROOT/scripts/control" "$ROOT/deploy/host-probe.py" "$ROOT/deploy/register-plugin.py" "$ROOT/ui/devicekeeper.cgi"
chmod 0644 "$ROOT/INFO" "$ROOT/ui/index.html" "$ROOT/ui/styles.css" "$ROOT/ui/app.js" "$ROOT/ui/icon.png" "$ROOT/ui/config"

install -m 0644 "$ROOT/deploy/devicekeeper-host-probe.service" /etc/systemd/system/devicekeeper-host-probe.service
install -m 0644 "$ROOT/deploy/devicekeeper-host-probe.timer" /etc/systemd/system/devicekeeper-host-probe.timer
systemctl daemon-reload
systemctl enable --now devicekeeper-host-probe.timer
systemctl start devicekeeper-host-probe.service

/usr/bin/python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$STATE/host.json"

"$DOCKER" build \
    --network host \
    --build-arg "ALPINE_IMAGE=$ALPINE_IMAGE" \
    -t "$IMAGE" \
    "$ROOT"

if "$DOCKER" container inspect devicekeeper >/dev/null 2>&1; then
    "$DOCKER" rm -f devicekeeper >/dev/null
fi

"$DOCKER" run -d \
    --name devicekeeper \
    --restart unless-stopped \
    --read-only \
    --user 10001:10001 \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --pids-limit 64 \
    --memory 192m \
    --cpus 1.0 \
    -p 127.0.0.1:9365:9365 \
    -e HOST_ROOT=/host \
    -e STATE_DIR=/state \
    -e PORT=9365 \
    -e BIND_ADDRESS=0.0.0.0 \
    -e COLLECT_INTERVAL_SECONDS=5 \
    -v /proc:/host/proc:ro \
    -v /sys:/host/sys:ro \
    -v /nas/pool0:/host/nas/pool0:ro \
    -v "$STATE:/state" \
    --tmpfs /tmp:size=16m,mode=1777 \
    --log-driver json-file \
    --log-opt max-size=2m \
    --log-opt max-file=3 \
    "$IMAGE"

i=0
while [ "$i" -lt 30 ]; do
    if curl -fsS --max-time 2 http://127.0.0.1:9365/api/v1/health >/dev/null 2>&1; then
        break
    fi
    i=$((i + 1))
    sleep 1
done
curl -fsS --max-time 3 http://127.0.0.1:9365/api/v1/health
echo

/usr/bin/python3 "$ROOT/deploy/register-plugin.py" --root "$ROOT" --uid "$UID_VALUE"
echo "devicekeeper deployed"
