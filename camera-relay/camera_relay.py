#!/usr/bin/env python3
"""
camera_relay: RTSP in, WebSocket out, no decoding.

Pulls H.264 from one or more ONVIF/RTSP cameras over RTSP-interleaved TCP,
strips the RTP packaging (RFC 6184: single NAL, STAP-A, FU-A) and forwards
whole access units as Annex B byte strings over a binary WebSocket, one
message per access unit. A browser then decodes them (WASM) and draws.

Also serves a static directory over plain HTTP on the same port, so the test
page and its decoder can be loaded from the relay itself.

Standard library only; runs on Venus OS's Python 3.8.

Wire format of each WebSocket binary message (HEADER_LEN = 9):
    byte 0      flags: bit0 = contains IDR (key frame), bit1 = SPS/PPS only
    bytes 1-4   RTP timestamp / 90 (ms, big endian, wraps)
    bytes 5-8   relay wall clock when the access unit was complete, ms since
                epoch mod 2^32 (big endian). With GET /time a browser can
                estimate its offset to the relay clock and print the
                relay-to-screen latency of every frame.
    bytes 9..   Annex B access unit (00 00 00 01 NAL ...)
A freshly connected client first receives the SPS/PPS as one message with
flags bit1 set, then waits for the next key frame before it gets anything else.

Usage:
    camera_relay.py --port 8095 --static ./www --camera bow=rtsp://192.0.2.10/live/0/SUB

Also the store for the helm app's roaming preferences (pinned tanks, sensors,
switches, theme): GET /prefs.json returns the document, POST /prefs merges the
posted top-level keys into it (a null value deletes a key) and writes it to
prefs.json beside config.json, so every display on the boat sees the same picks.

config.json cameras may be a plain URL or an object:
    {"port": {"url": "rtsp://…", "rotate": 90, "mirror": false, "flip": false, "label": "Port"}}
`rotate` is clockwise degrees as shown on screen. The test page's orientation
buttons POST /cameras/<id> and the relay writes the change back to config.json,
so the orientation is set once, on the boat, and shared by every display.
Browsers POST their HUD numbers to /report; the last report per client is in
/stats.json, which is how the MFD's decode speed gets read from the dev box.
"""

import argparse
import base64
import hashlib
import json
import logging
import os
import socket
import struct
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

VERSION = "0.5.0"

log = logging.getLogger("camera_relay")

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
START_CODE = b"\x00\x00\x00\x01"
HEADER_LEN = 9


def now_ms32():
    return int(time.time() * 1000) & 0xFFFFFFFF
FLAG_KEY = 0x01
FLAG_PARAMS = 0x02

# ---------------------------------------------------------------------------
# RTSP client (TCP interleaved)
# ---------------------------------------------------------------------------


class RTSPError(Exception):
    pass


class RTSPClient(object):
    """Minimal RTSP client that plays one video track over interleaved TCP."""

    def __init__(self, url, timeout=10.0):
        self.url = url
        self.timeout = timeout
        parsed = urlparse(url)
        self.host = parsed.hostname
        self.port = parsed.port or 554
        self.auth = None
        if parsed.username:
            userinfo = "%s:%s" % (parsed.username, parsed.password or "")
            self.auth = "Basic " + base64.b64encode(userinfo.encode()).decode()
            # strip credentials from the URL we send on the wire
            netloc = parsed.hostname + (":%d" % parsed.port if parsed.port else "")
            self.url = parsed._replace(netloc=netloc).geturl()
        self.sock = None
        self.cseq = 0
        self.session = None
        self.sps = None
        self.pps = None
        self.buf = b""

    def _send(self, method, url, extra=None):
        self.cseq += 1
        lines = ["%s %s RTSP/1.0" % (method, url), "CSeq: %d" % self.cseq, "User-Agent: camera_relay/%s" % VERSION]
        if self.session:
            lines.append("Session: %s" % self.session)
        if self.auth:
            lines.append("Authorization: %s" % self.auth)
        if extra:
            lines.extend(extra)
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        return self._read_response()

    def _read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RTSPError("connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _read_response(self):
        # Interleaved data may arrive between responses; skip those frames.
        while True:
            while len(self.buf) < 4:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise RTSPError("connection closed")
                self.buf += chunk
            if self.buf[0:1] == b"$":
                length = struct.unpack(">H", self.buf[2:4])[0]
                self._read_exact(4 + length)
                continue
            end = self.buf.find(b"\r\n\r\n")
            while end < 0:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise RTSPError("connection closed")
                self.buf += chunk
                end = self.buf.find(b"\r\n\r\n")
            head = self.buf[:end].decode(errors="replace")
            self.buf = self.buf[end + 4 :]
            headers = {}
            lines = head.split("\r\n")
            status = lines[0]
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            body = b""
            if "content-length" in headers:
                body = self._read_exact(int(headers["content-length"]))
            if not status.startswith("RTSP/1.0 200"):
                raise RTSPError("%s -> %s" % (status, headers))
            return headers, body

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        self.buf = b""
        self._send("OPTIONS", self.url)
        headers, sdp = self._send("DESCRIBE", self.url, ["Accept: application/sdp"])
        base = headers.get("content-base") or headers.get("content-location") or self.url
        control = None
        in_video = False
        for line in sdp.decode(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("m="):
                in_video = line.startswith("m=video")
            elif in_video and line.startswith("a=control:"):
                control = line[len("a=control:") :]
            elif in_video and "sprop-parameter-sets=" in line:
                sets = line.split("sprop-parameter-sets=")[1].split(";")[0].split(",")
                try:
                    self.sps = base64.b64decode(sets[0])
                    if len(sets) > 1:
                        self.pps = base64.b64decode(sets[1])
                except Exception:
                    pass
        if control is None:
            raise RTSPError("no video track in SDP")
        if control.startswith("rtsp://"):
            setup_url = control
        elif control == "*":
            setup_url = base
        else:
            setup_url = base.rstrip("/") + "/" + control
        headers, _ = self._send("SETUP", setup_url, ["Transport: RTP/AVP/TCP;unicast;interleaved=0-1"])
        self.session = headers.get("session", "").split(";")[0]
        if not self.session:
            raise RTSPError("no session id")
        self._send("PLAY", self.url, ["Range: npt=0.000-"])
        log.info("%s: playing (sps=%s pps=%s)", self.url, bool(self.sps), bool(self.pps))

    def keepalive(self):
        try:
            self._send("GET_PARAMETER", self.url)
        except RTSPError:
            self._send("OPTIONS", self.url)

    def read_interleaved(self):
        """Yield (channel, payload) for every interleaved frame."""
        while True:
            head = self._read_exact(4)
            if head[0:1] != b"$":
                # An unsolicited RTSP message (e.g. ANNOUNCE). Discard it.
                self.buf = head + self.buf
                self._read_response()
                continue
            channel = head[1]
            length = struct.unpack(">H", head[2:4])[0]
            yield channel, self._read_exact(length)

    def teardown(self):
        try:
            if self.sock and self.session:
                self._send("TEARDOWN", self.url)
        except Exception:
            pass
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None


# ---------------------------------------------------------------------------
# RTP H.264 depacketizer -> access units
# ---------------------------------------------------------------------------


class H264Depacketizer(object):
    """Turns RTP packets into Annex B access units, one per RTP timestamp."""

    def __init__(self):
        self.fu_buf = None
        self.fu_type = 0
        self.nals = []
        self.timestamp = None
        self.has_key = False
        self.sps = None
        self.pps = None

    def _add_nal(self, nal):
        if not nal:
            return
        nal_type = nal[0] & 0x1F
        if nal_type == 7:
            self.sps = nal
        elif nal_type == 8:
            self.pps = nal
        elif nal_type == 5:
            self.has_key = True
        self.nals.append(nal)

    def push(self, packet):
        """Feed one RTP packet. Returns a finished access unit or None."""
        if len(packet) < 12:
            return None
        b0, b1 = packet[0], packet[1]
        if (b0 >> 6) != 2:
            return None
        cc = b0 & 0x0F
        has_ext = b0 & 0x10
        marker = b1 & 0x80
        timestamp = struct.unpack(">I", packet[4:8])[0]
        offset = 12 + 4 * cc
        if has_ext:
            if len(packet) < offset + 4:
                return None
            ext_len = struct.unpack(">H", packet[offset + 2 : offset + 4])[0]
            offset += 4 + 4 * ext_len
        if b0 & 0x20:  # padding
            packet = packet[: -packet[-1]] if packet[-1] else packet
        payload = packet[offset:]
        if not payload:
            return None

        finished = None
        if self.timestamp is not None and timestamp != self.timestamp and self.nals:
            finished = self._flush()
        self.timestamp = timestamp

        nal_type = payload[0] & 0x1F
        if 1 <= nal_type <= 23:
            self._add_nal(payload)
        elif nal_type == 24:  # STAP-A
            pos = 1
            while pos + 2 <= len(payload):
                size = struct.unpack(">H", payload[pos : pos + 2])[0]
                pos += 2
                self._add_nal(payload[pos : pos + size])
                pos += size
        elif nal_type == 28:  # FU-A
            if len(payload) < 2:
                return finished
            fu_header = payload[1]
            start = fu_header & 0x80
            end = fu_header & 0x40
            if start:
                self.fu_type = (payload[0] & 0xE0) | (fu_header & 0x1F)
                self.fu_buf = bytearray([self.fu_type])
                self.fu_buf += payload[2:]
            elif self.fu_buf is not None:
                self.fu_buf += payload[2:]
            if end and self.fu_buf is not None:
                self._add_nal(bytes(self.fu_buf))
                self.fu_buf = None
        # other types (STAP-B, MTAP, FU-B) are not used by this camera

        if marker and self.nals:
            finished = self._flush() if finished is None else finished + self._flush()
        return finished

    def _flush(self):
        """Package the pending NALs as one Annex B access unit with a header."""
        au = b"".join(START_CODE + nal for nal in self.nals)
        flags = FLAG_KEY if self.has_key else 0
        ts_ms = (self.timestamp // 90) & 0xFFFFFFFF if self.timestamp is not None else 0
        self.nals = []
        self.has_key = False
        return struct.pack(">BII", flags, ts_ms, now_ms32()) + au


# ---------------------------------------------------------------------------
# Camera feed thread
# ---------------------------------------------------------------------------


ON_DEMAND_GRACE_S = 10.0  # keep an on-demand stream up this long after its last viewer leaves


class CameraFeed(threading.Thread):
    """One RTSP session, fanned out to any number of websocket clients.

    The substream feed runs for the life of the relay. A main-stream feed is
    on demand: created when the first viewer asks for it, torn down (RTSP
    TEARDOWN, thread ends) once nobody has watched it for ON_DEMAND_GRACE_S,
    so the camera's 4 Mbit/s never crosses the Cerbo unless a PC or tablet is
    actually looking. A finished thread cannot restart; the next viewer gets a
    fresh CameraFeed from get_feed().
    """

    def __init__(self, name, url, stream="sub", on_demand=False):
        threading.Thread.__init__(self, name="feed-%s-%s" % (name, stream), daemon=True)
        self.camera = name
        self.stream = stream
        self.url = url
        self.on_demand = on_demand
        self.clients = set()
        self.lock = threading.Lock()
        self.sps = None
        self.pps = None
        self.stats = {"state": "starting", "frames": 0, "keyframes": 0, "bytes": 0, "reconnects": 0, "fps": 0.0}
        self._fps_window = deque(maxlen=60)
        self._last_viewer = time.time()

    def _idle(self):
        with self.lock:
            if self.clients:
                self._last_viewer = time.time()
                return False
        return time.time() - self._last_viewer > ON_DEMAND_GRACE_S

    def params_message(self):
        if not (self.sps and self.pps):
            return None
        return struct.pack(">BII", FLAG_PARAMS, 0, now_ms32()) + START_CODE + self.sps + START_CODE + self.pps

    def add_client(self, client):
        with self.lock:
            self.clients.add(client)
        params = self.params_message()
        if params:
            client.enqueue(params)

    def remove_client(self, client):
        with self.lock:
            self.clients.discard(client)

    def broadcast(self, message):
        with self.lock:
            clients = list(self.clients)
        for client in clients:
            client.enqueue(message)

    def run(self):
        backoff = 1.0
        while True:
            if self.on_demand and self._idle():
                self.stats["state"] = "idle"
                log.info("%s/%s: no viewers, stream released", self.camera, self.stream)
                return
            rtsp = RTSPClient(self.url)
            try:
                self.stats["state"] = "connecting"
                rtsp.connect()
                self.sps = rtsp.sps or self.sps
                self.pps = rtsp.pps or self.pps
                self.stats["state"] = "playing"
                backoff = 1.0
                depack = H264Depacketizer()
                last_keepalive = time.time()
                for channel, payload in rtsp.read_interleaved():
                    if channel != 0:
                        continue
                    au = depack.push(payload)
                    if depack.sps and depack.sps != self.sps:
                        self.sps = depack.sps
                    if depack.pps and depack.pps != self.pps:
                        self.pps = depack.pps
                    if au:
                        self._account(au)
                        self.broadcast(au)
                        if self.on_demand and self._idle():
                            break
                    now = time.time()
                    if now - last_keepalive > 25:
                        last_keepalive = now
                        rtsp.keepalive()
            except (RTSPError, socket.error, OSError) as e:
                self.stats["state"] = "error: %s" % e
                self.stats["reconnects"] += 1
                log.warning("%s/%s: %s; retrying in %.0fs", self.camera, self.stream, e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                rtsp.teardown()

    def _account(self, au):
        self.stats["frames"] += 1
        self.stats["bytes"] += len(au)
        if au[0] & FLAG_KEY:
            self.stats["keyframes"] += 1
        now = time.time()
        self._fps_window.append(now)
        if len(self._fps_window) > 1:
            span = now - self._fps_window[0]
            if span >= 1.0:
                self.stats["fps"] = round((len(self._fps_window) - 1) / span, 1)


# ---------------------------------------------------------------------------
# WebSocket client
# ---------------------------------------------------------------------------


class WSClient(object):
    """One browser. Frames are queued; if the queue backs up we drop until the next key frame."""

    MAX_QUEUE = 30

    def __init__(self, sock, feed):
        self.sock = sock
        self.feed = feed
        self.queue = deque()
        self.cond = threading.Condition()
        self.closed = False
        self.waiting_for_key = True
        self.dropped = 0
        self.sent = 0

    def enqueue(self, message):
        with self.cond:
            if self.closed:
                return
            flags = message[0]
            if flags & FLAG_PARAMS:
                self.queue.append(message)
            else:
                if self.waiting_for_key:
                    if not (flags & FLAG_KEY):
                        self.dropped += 1
                        return
                    self.waiting_for_key = False
                if len(self.queue) >= self.MAX_QUEUE:
                    # Too slow: drop everything pending and resync on the next key frame.
                    self.dropped += len(self.queue)
                    self.queue.clear()
                    self.waiting_for_key = True
                    if not (flags & FLAG_KEY):
                        return
                    self.waiting_for_key = False
                self.queue.append(message)
            self.cond.notify()

    def sender(self):
        try:
            while True:
                with self.cond:
                    while not self.queue and not self.closed:
                        self.cond.wait(1.0)
                    if self.closed:
                        return
                    message = self.queue.popleft()
                self._send_frame(0x2, message)
                self.sent += 1
        except (socket.error, OSError):
            pass
        finally:
            self.close()

    def _send_frame(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header += struct.pack(">H", n)
        else:
            header.append(127)
            header += struct.pack(">Q", n)
        self.sock.sendall(bytes(header) + payload)

    def reader(self):
        """Consume client frames: answer pings, honour close. Runs in the handler thread."""
        try:
            while not self.closed:
                head = self._recv_exact(2)
                if head is None:
                    return
                opcode = head[0] & 0x0F
                masked = head[1] & 0x80
                n = head[1] & 0x7F
                if n == 126:
                    n = struct.unpack(">H", self._recv_exact(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._recv_exact(8))[0]
                mask = self._recv_exact(4) if masked else None
                payload = self._recv_exact(n) if n else b""
                if payload is None:
                    return
                if mask:
                    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                if opcode == 0x8:  # close
                    return
                if opcode == 0x9:  # ping
                    with self.cond:
                        self._send_frame(0xA, payload)
        except (socket.error, OSError):
            pass
        finally:
            self.close()

    def _recv_exact(self, n):
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def close(self):
        with self.cond:
            if self.closed:
                return
            self.closed = True
            self.cond.notify_all()
        self.feed.remove_client(self)
        try:
            self.sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# HTTP + WebSocket server
# ---------------------------------------------------------------------------

FEEDS = {}       # camera -> always-on substream feed
MAIN_FEEDS = {}  # camera -> on-demand main-stream feed (may have finished)
FEEDS_LOCK = threading.Lock()
STATIC_DIR = None
START_TIME = time.time()
CONFIG_PATH = None
CONFIG = {}
CONFIG_LOCK = threading.Lock()
REPORTS = {}
ORIENTATION_KEYS = ("rotate", "mirror", "flip", "label", "k1", "k2", "zoom", "cx", "cy")
LENS_KEYS = {"k1": 0.0, "k2": 0.0, "zoom": 1.0, "cx": 0.0, "cy": 0.0}  # radial lens correction, see yuv-canvas.js


def camera_entry(name):
    """The config object for a camera, normalised (a bare URL becomes {"url": …})."""
    entry = CONFIG.get("cameras", {}).get(name)
    if isinstance(entry, str):
        entry = {"url": entry}
    return entry or {}


def get_feed(camera, stream):
    """The feed for a websocket request; starts the main stream if it is not running."""
    if stream != "main":
        return FEEDS.get(camera)
    url = camera_entry(camera).get("main")
    if not url:
        return None
    with FEEDS_LOCK:
        feed = MAIN_FEEDS.get(camera)
        if feed is None or not feed.is_alive():
            feed = CameraFeed(camera, url, stream="main", on_demand=True)
            MAIN_FEEDS[camera] = feed
            feed.start()
            log.info("%s/main: stream requested, connecting", camera)
        return feed


def camera_public(name, feed):
    e = camera_entry(name)
    return {
        "id": name,
        "label": e.get("label", name),
        "state": feed.stats["state"],
        "main": bool(e.get("main")),
        "rotate": int(e.get("rotate", 0) or 0),
        "mirror": bool(e.get("mirror", False)),
        "flip": bool(e.get("flip", False)),
        "lens": {k: lens_value(e.get(k), d) for k, d in LENS_KEYS.items()},
    }


def lens_value(v, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # NaN guard


def save_config():
    if not CONFIG_PATH:
        return False
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(CONFIG, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)
    return True

# Roaming helm preferences: an opaque JSON object owned by the app, persisted
# beside config.json. The relay only merges top-level keys and never looks
# inside the values, so the app can grow the document without a relay change.
PREFS_PATH = None
PREFS = {}
PREFS_LOCK = threading.Lock()
PREFS_MAX_BYTES = 64 * 1024


def load_prefs():
    global PREFS
    if not PREFS_PATH or not os.path.isfile(PREFS_PATH):
        return
    try:
        with open(PREFS_PATH) as f:
            doc = json.load(f)
        if isinstance(doc, dict):
            PREFS = doc
    except (ValueError, OSError) as e:
        log.warning("prefs: could not read %s: %s", PREFS_PATH, e)


def save_prefs():
    if not PREFS_PATH:
        return False
    tmp = PREFS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(PREFS, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, PREFS_PATH)
    return True


CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript",
    ".wasm": "application/wasm",
    ".css": "text/css",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "camera_relay/" + VERSION

    def log_message(self, fmt, *args):
        log.debug("%s " + fmt, self.client_address[0], *args)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/ws/"):
            stream = parse_qs(parsed.query).get("stream", ["sub"])[0]
            return self._websocket(path[4:], stream)
        if path == "/stats.json":
            return self._json(self._stats())
        if path == "/time":
            return self._json({"ms": int(time.time() * 1000)})
        if path == "/cameras.json":
            return self._json([camera_public(name, feed) for name, feed in FEEDS.items()])
        if path == "/prefs.json":
            with PREFS_LOCK:
                return self._json(dict(PREFS))
        return self._static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        if length > PREFS_MAX_BYTES:
            self.send_error(413)
            return
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode() or "{}")
        except ValueError:
            self.send_error(400, "bad json")
            return
        if path == "/report":
            REPORTS[self.client_address[0]] = dict(body, at=int(time.time()))
            return self._json({"ok": True})
        if path == "/prefs":
            if not isinstance(body, dict):
                self.send_error(400, "object expected")
                return
            with PREFS_LOCK:
                for k, v in body.items():
                    if v is None:
                        PREFS.pop(k, None)
                    else:
                        PREFS[k] = v
                try:
                    saved = save_prefs()
                except OSError as e:
                    log.warning("prefs: could not write %s: %s", PREFS_PATH, e)
                    saved = False
                doc = dict(PREFS)
            log.info("prefs: %s from %s (saved=%s)", ", ".join(sorted(body)), self.client_address[0], saved)
            return self._json({"ok": True, "saved": saved, "prefs": doc})
        if path.startswith("/cameras/"):
            name = path[len("/cameras/") :]
            feed = FEEDS.get(name)
            if feed is None:
                self.send_error(404)
                return
            with CONFIG_LOCK:
                cams = CONFIG.setdefault("cameras", {})
                entry = camera_entry(name)
                for k in ORIENTATION_KEYS:
                    if k in body:
                        entry[k] = lens_value(body[k], LENS_KEYS[k]) if k in LENS_KEYS else body[k]
                entry["rotate"] = int(entry.get("rotate", 0) or 0) % 360
                cams[name] = entry
                saved = save_config()
            log.info("%s: orientation %s (saved=%s)", name, {k: entry.get(k) for k in ORIENTATION_KEYS}, saved)
            return self._json(dict(camera_public(name, feed), saved=saved))
        self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stats(self):
        out = {"version": VERSION, "uptime_s": int(time.time() - START_TIME), "cameras": {}, "reports": REPORTS}
        for name, feed in FEEDS.items():
            stats = self._feed_stats(feed)
            main = MAIN_FEEDS.get(name)
            if main is not None:
                stats["main"] = self._feed_stats(main)
            out["cameras"][name] = stats
        return out

    @staticmethod
    def _feed_stats(feed):
        stats = dict(feed.stats)
        with feed.lock:
            stats["clients"] = [{"sent": c.sent, "dropped": c.dropped, "queued": len(c.queue)} for c in feed.clients]
        return stats

    def _static(self, path):
        if STATIC_DIR is None:
            self.send_error(404)
            return
        if path == "/":
            path = "/index.html"
        full = os.path.normpath(os.path.join(STATIC_DIR, path.lstrip("/")))
        if not full.startswith(os.path.abspath(STATIC_DIR)) or not os.path.isfile(full):
            self.send_error(404)
            return
        ext = os.path.splitext(full)[1]
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _websocket(self, camera, stream="sub"):
        feed = get_feed(camera, stream)
        key = self.headers.get("Sec-WebSocket-Key")
        if feed is None or key is None or "websocket" not in self.headers.get("Upgrade", "").lower():
            self.send_error(404 if feed is None else 400)
            return
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        sock = self.connection
        sock.settimeout(None)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        client = WSClient(sock, feed)
        log.info("%s/%s: client %s connected", camera, stream, self.client_address[0])
        feed.add_client(client)
        sender = threading.Thread(target=client.sender, name="ws-send", daemon=True)
        sender.start()
        client.reader()
        sender.join(2.0)
        log.info("%s/%s: client %s left (sent %d, dropped %d)", camera, stream, self.client_address[0], client.sent, client.dropped)


def main(argv=None):
    global STATIC_DIR
    parser = argparse.ArgumentParser(description="RTSP to WebSocket H.264 relay")
    parser.add_argument("--port", type=int, default=8095)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--static", help="directory served at / (test page, decoder)")
    parser.add_argument("--camera", action="append", default=[], help="name=rtsp://... (repeatable)")
    parser.add_argument("--config", help="JSON file: {\"port\":…, \"static\":…, \"cameras\":{name:url}}")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    global CONFIG_PATH, PREFS_PATH
    port = args.port
    static = args.static
    if args.config:
        CONFIG_PATH = os.path.abspath(args.config)
        with open(args.config) as f:
            CONFIG.update(json.load(f))
        port = CONFIG.get("port", port)
        static = CONFIG.get("static", static)
        PREFS_PATH = os.path.join(os.path.dirname(CONFIG_PATH), "prefs.json")
        load_prefs()
    cams = CONFIG.setdefault("cameras", {})
    for spec in args.camera:
        name, _, url = spec.partition("=")
        if not url:
            parser.error("--camera expects name=rtsp://...")
        cams[name] = url
    if not cams:
        parser.error("no cameras configured")
    if static:
        STATIC_DIR = os.path.abspath(static)

    for name in list(cams):
        url = camera_entry(name).get("url")
        if not url:
            parser.error("camera %s has no url" % name)
        feed = CameraFeed(name, url)
        FEEDS[name] = feed
        feed.start()

    server = ThreadingHTTPServer((args.bind, port), Handler)
    server.daemon_threads = True
    log.info("camera_relay %s listening on %s:%d, cameras: %s", VERSION, args.bind, port, ", ".join(cams))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
