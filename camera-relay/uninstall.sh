#!/bin/sh
# Stops and removes the camera-relay service. Leaves /data/camera-relay in place.
svc -d /service/camera-relay 2>/dev/null || true
svc -d /service/camera-relay/log 2>/dev/null || true
rm -f /service/camera-relay
sed -i '/camera-relay/d' /data/rc.local 2>/dev/null || true
echo "camera-relay service removed (files kept in /data/camera-relay)"
