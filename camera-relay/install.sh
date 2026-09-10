#!/bin/sh
# camera-relay installer — run ON the Cerbo, after copying this folder to
# /data/camera-relay. Creates the daemontools service and makes it survive
# Venus OS firmware updates via /data/rc.local. Needs /data/camera-relay/config.json
# (copy config.json.example and put the real camera URLs in; it is not in git).
set -e

DIR=/data/camera-relay

if [ ! -f "$DIR/camera_relay.py" ]; then
    echo "ERROR: copy this folder to $DIR first (found no $DIR/camera_relay.py)"
    exit 1
fi
if [ ! -f "$DIR/config.json" ]; then
    echo "ERROR: $DIR/config.json missing — copy config.json.example and fill in the cameras"
    exit 1
fi

chmod 755 "$DIR/camera_relay.py" "$DIR/service/run" "$DIR/service/log/run" "$DIR/install.sh" "$DIR/uninstall.sh" 2>/dev/null || true

# Start now: svscan picks up the /service symlink within ~5s
ln -sfn "$DIR/service" /service/camera-relay

# Survive firmware updates (rootfs is replaced; /data persists)
RCLOCAL=/data/rc.local
touch "$RCLOCAL"
chmod 755 "$RCLOCAL"
if ! grep -q "camera-relay" "$RCLOCAL"; then
    echo "ln -sfn $DIR/service /service/camera-relay" >> "$RCLOCAL"
fi

echo "Installed. The service starts within ~5 seconds."
echo "  status:  svstat /service/camera-relay"
echo "  logs:    tail -f /var/log/camera-relay/current | tai64nlocal"
echo "  restart: svc -t /service/camera-relay"
echo "  check:   wget -qO- http://127.0.0.1:8095/stats.json"
