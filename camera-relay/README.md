# camera-relay

RTSP in, WebSocket out, no decoding. Lets a browser that has no H.264 support
(the Simrad NSS evo3S webview: Qt WebEngine 5.12 / Chromium 69, built without
proprietary codecs) show ONVIF cameras by decoding H.264 itself in
WebAssembly.

```
IP camera  --RTSP/TCP-->  camera_relay.py (Cerbo, /data/camera-relay)  --WebSocket-->  browser
   H.264                  strips RTP, forwards access units                 wasm decode -> WebGL
```

The Cerbo never touches pixels. One camera substream at 800x448 / 20 fps /
768 kbit/s costs it a few percent of one core.

## Files

| Path | What |
|---|---|
| `camera_relay.py` | the relay: RTSP client (interleaved TCP), RFC 6184 depacketizer, WebSocket server, static file server, `/stats.json`, `/cameras.json`, `/prefs.json` |
| `config.json.example` | `{"port", "static", "cameras": {name: {"url": substream, "main": main stream, …}}}`; the real `config.json` holds the boat's camera URLs and is **not in git**. `url` (the substream) is relayed for the life of the process; `main` is optional and opened **on demand** — only while a browser asks for `/ws/<name>?stream=main`, released 10 s after the last viewer leaves, so the 1080p stream never crosses the GX unless a PC or tablet is watching. The page picks it automatically once the substream decodes fast enough (page "Stream" button: auto / main / sub). |
| `prefs.json` | the helm app's roaming preferences (pinned tanks, sensors, switches, theme); written by the relay beside `config.json`, **not in git** |
| `service/` | daemontools service (`/service/camera-relay`), log to `/var/log/camera-relay` |
| `install.sh` / `uninstall.sh` | symlink the service, persist through `/data/rc.local` |
| `www/index.html` | test page: pick a camera, add views, rotate 0/90/180/270, mirror, flip; HUD with fps, decode ms, draw ms |
| `www/decoder-worker.js` | one Web Worker per view, runs the WASM decoder, hands back packed YUV420 |
| `www/yuv-canvas.js` | WebGL YUV→RGB with the rotate/mirror in the texture matrix |
| `www/h264dec.js` `www/h264dec.wasm` | libavcodec's H.264 decoder (FFmpeg 6.1.2), built by `wasm/build.sh` |
| `wasm/h264dec.c` | 60-line C wrapper around `avcodec_send_packet` / `avcodec_receive_frame` |

## Wire format

Each WebSocket binary message is one access unit:

```
byte 0     flags   bit0 = contains an IDR (key frame), bit1 = SPS/PPS only
bytes 1-4  RTP timestamp / 90 (ms, big endian, wraps)
bytes 5..  Annex B (00 00 00 01 NAL ...)
```

A new client gets the SPS/PPS first, then nothing until the next key frame.
If a client's queue backs up past 30 messages it is emptied and the client
waits for the next key frame again, so a slow browser lags by at most a GOP
and never decodes a P-frame whose reference it dropped.

## Helm preferences (roaming)

The Victron HTML5 app's Home page keeps its pinned readings, quick switches
and theme on the GX rather than in the browser, because the MFD's webview
forgets its web storage on every reboot and the owner wants the same picks on
every display. The relay is the store, since it is already the one HTTP
service on the box that writes files:

```
GET  /prefs.json          -> the document ({} until something is saved)
POST /prefs  {"key": …}   -> merge the posted top-level keys, null deletes one; returns {"ok", "saved", "prefs"}
```

Bodies are capped at 64 KB and the relay never looks inside the values; the
app validates what it reads back. The file is `prefs.json` beside
`config.json`, written atomically.

## Building the decoder

Needs emscripten **3.1.x** (4.x refuses `MIN_CHROME_VERSION=69`, and that flag
is what lowers sign-ext / bulk-memory / BigInt away for the 2018 engine):

```sh
git clone https://github.com/emscripten-core/emsdk && (cd emsdk && ./emsdk install 3.1.74 && ./emsdk activate 3.1.74)
git clone --depth 1 --branch n6.1.2 https://github.com/FFmpeg/FFmpeg ffmpeg
EMSDK=./emsdk FFMPEG_SRC=./ffmpeg wasm/build.sh
```

`build.sh` configures FFmpeg with everything disabled except the h264 decoder
and parser, no threads, no asm, then links the wrapper. Output is 1.3 MB of
wasm plus 15 KB of glue. Check what came out before trusting it on the MFD:

```sh
emsdk/upstream/bin/wasm-dis www/h264dec.wasm | grep -cE 'extend(8|16|32)_s|memory\.(copy|fill)|trunc_sat'   # must be 0
npx es-check es2018 www/h264dec.js www/decoder-worker.js www/yuv-canvas.js                                   # Chromium 69 is ES2018
```

## Deploy

```sh
./cerbo put config.json /data/camera-relay/config.json     # the boat's camera URLs
python deploy_cerbo.py camerarelay --install               # first time; later just `camerarelay`
```

Then on the MFD: Victron app → logo → Diagnostics → page 2 → **Camera test
page**. It navigates the webview to `http://<gx>:8095/`; the page's `← App`
button comes back.

## Camera side

The IRIS-S460-18 exposes ONVIF Profile S without a password. Its substream
is H.264 **Main** profile with CABAC (the camera offers no Baseline), so
Broadway/tinyh264-class decoders are out; libavcodec handles it. Useful
endpoints: `rtsp://<cam>/live/0/SUB` (800x448), `rtsp://<cam>/live/0/MAIN`
(1080p), `http://<cam>/SnapShot_Ch0.jpg` (640x360 JPEG, ~28 KB, ~25 ms).
