/* decoder-worker.js — one camera per worker: WebSocket in, H.264 decode, and
 * (when the browser allows it) the WebGL draw too, so the page's main thread
 * never touches a frame.
 *
 * Messages in:
 *   { type: "init", base, id, wsUrl, rotate, mirror, flip, canvas? }
 *       base   = URL prefix for h264dec.js / h264dec.wasm / yuv-canvas.js
 *       canvas = OffscreenCanvas (transferred) when the worker should draw
 *   { type: "clock", offset }          relay clock minus local Date.now()
 *   { type: "stream", wsUrl }          drop the socket, reset the decoder, connect elsewhere
 *   { type: "transform", rotate, mirror, flip }
 *   { type: "resize", width, height }  backing-store size for the offscreen canvas
 * Messages out:
 *   { type: "ready" }  { type: "ws", state }  { type: "error", message }
 *   { type: "drawn", ...timing }       worker drew the frame itself
 *   { type: "frame", width, height, buffer, ...timing }   page must draw it
 *   { type: "nopic", decodeMs }
 *
 * Timing fields (ms): relayMs (relay stamp), netMs (relay -> here), decodeMs,
 * drawMs, totalMs (relay -> drawn, worker path only), key (bool), tDone (Date.now()
 * when the message was posted, so the page can time the hop).
 *
 * Plain ES2015: Chromium 69 has no optional chaining or nullish coalescing.
 */
"use strict"

var HEADER_LEN = 9 // flags(1) rtp ms(4) relay ms(4) — see camera_relay.py
var Module = null
var ctx = 0
var inPtr = 0
var inCap = 0
var gl = null // YuvCanvas on the OffscreenCanvas, when drawing here
var canvas = null
var clockOffset = null
var ws = null
var wsUrl = ""
var camId = ""
var skipping = false // dropped to the next key frame after falling too far behind
var SKIP_AFTER_MS = 700
var nal = { recv: 0, skipped: 0 }

function copyPlane(heap, ptr, stride, width, height, out, offset) {
  if (stride === width) {
    out.set(heap.subarray(ptr, ptr + width * height), offset)
    return offset + width * height
  }
  for (var row = 0; row < height; row++) {
    out.set(heap.subarray(ptr + row * stride, ptr + row * stride + width), offset)
    offset += width
  }
  return offset
}

function nalTypes(bytes) {
  var types = []
  for (var i = 0; i + 4 < bytes.length; i++) {
    if (bytes[i] === 0 && bytes[i + 1] === 0 && bytes[i + 2] === 0 && bytes[i + 3] === 1) {
      types.push(bytes[i + 4] & 0x1f)
      i += 3
    }
  }
  return types
}

function relayNow() {
  return (Date.now() + clockOffset) % 4294967296
}
function sinceRelay(relayMs) {
  if (clockOffset === null) return -1
  var d = relayNow() - relayMs
  if (d < -2147483648) d += 4294967296
  return d
}

function onAu(buf) {
  var dv = new DataView(buf)
  var flags = dv.getUint8(0)
  var relayMs = dv.getUint32(5)
  var key = (flags & 1) === 1
  var params = (flags & 2) === 2
  var netMs = sinceRelay(relayMs)
  nal.recv++

  // Safety valve: if this frame is already far behind, skip to the next key frame
  // rather than decode a backlog nobody will look at. Parameter sets always pass.
  if (!params) {
    if (skipping && key) skipping = false
    else if (!skipping && !key && netMs > SKIP_AFTER_MS) skipping = true
    if (skipping) {
      nal.skipped++
      postMessage({ type: "skipped", netMs: netMs, skipped: nal.skipped })
      return
    }
  }

  var bytes = new Uint8Array(buf, HEADER_LEN)
  // libavcodec wants AV_INPUT_BUFFER_PADDING_SIZE (64) zero bytes after the data
  var need = bytes.length + 64
  if (need > inCap) {
    if (inPtr) Module._free(inPtr)
    inCap = Math.max(need, 256 * 1024)
    inPtr = Module._malloc(inCap)
  }
  Module.HEAPU8.set(bytes, inPtr)
  Module.HEAPU8.fill(0, inPtr + bytes.length, inPtr + bytes.length + 64)

  var t0 = performance.now()
  var rc = Module._h264_decode(ctx, inPtr, bytes.length)
  var decodeMs = performance.now() - t0
  if (rc < 0) {
    // a bare SPS/PPS message yields no picture and libavcodec calls that invalid data; it is not
    if (params) postMessage({ type: "nopic", decodeMs: decodeMs, bytes: buf.byteLength })
    else postMessage({ type: "error", message: "decode rc=" + rc + " nals=" + nalTypes(bytes).join(",") + " len=" + bytes.length })
    return
  }
  if (rc === 0) {
    postMessage({ type: "nopic", decodeMs: decodeMs, bytes: buf.byteLength })
    return
  }
  var w = Module._h264_width(ctx)
  var h = Module._h264_height(ctx)
  var cw = (w + 1) >> 1
  var ch = (h + 1) >> 1
  var out = new Uint8Array(w * h + 2 * cw * ch)
  var heap = Module.HEAPU8
  var off = copyPlane(heap, Module._h264_plane(ctx, 0), Module._h264_stride(ctx, 0), w, h, out, 0)
  off = copyPlane(heap, Module._h264_plane(ctx, 1), Module._h264_stride(ctx, 1), cw, ch, out, off)
  copyPlane(heap, Module._h264_plane(ctx, 2), Module._h264_stride(ctx, 2), cw, ch, out, off)

  if (gl) {
    var t1 = performance.now()
    try {
      gl.draw(w, h, out)
      // Chromium 69's OffscreenCanvas still wants an explicit commit() to push the
      // frame to the placeholder canvas; later versions dropped it (frames push themselves)
      if (typeof gl.gl.commit === "function") gl.gl.commit()
    } catch (err) {
      postMessage({ type: "error", message: "draw failed: " + err })
      return
    }
    var drawMs = performance.now() - t1
    postMessage({ type: "drawn", width: w, height: h, key: key, bytes: buf.byteLength, relayMs: relayMs, netMs: netMs,
      decodeMs: decodeMs, drawMs: drawMs, totalMs: sinceRelay(relayMs), tDone: Date.now() })
  } else {
    postMessage({ type: "frame", width: w, height: h, key: key, bytes: buf.byteLength, relayMs: relayMs, netMs: netMs,
      decodeMs: decodeMs, buffer: out.buffer, tDone: Date.now() }, [out.buffer])
  }
}

function connect() {
  try {
    ws = new WebSocket(wsUrl)
  } catch (err) {
    postMessage({ type: "error", message: "websocket: " + err })
    setTimeout(connect, 2000)
    return
  }
  ws.binaryType = "arraybuffer"
  ws.onopen = function () { postMessage({ type: "ws", state: "open" }) }
  ws.onclose = function (e) {
    if (e.target !== ws) return // an abandoned socket closing after a stream switch
    ws = null
    postMessage({ type: "ws", state: "closed" })
    setTimeout(connect, 2000)
  }
  ws.onerror = function () { postMessage({ type: "ws", state: "error" }) }
  ws.onmessage = function (e) {
    if (e.target !== ws) return // a socket we already abandoned
    try {
      onAu(e.data)
    } catch (err) {
      postMessage({ type: "error", message: "decode threw: " + err })
    }
  }
}

onmessage = function (e) {
  var msg = e.data
  if (msg.type === "init") {
    camId = msg.id
    wsUrl = msg.wsUrl
    try {
      importScripts(msg.base + "h264dec.js")
      if (msg.canvas) {
        importScripts(msg.base + "yuv-canvas.js")
        canvas = msg.canvas
        gl = new YuvCanvas(canvas)
        gl.setTransform(msg.rotate || 0, msg.mirror, msg.flip)
        gl.setLens(msg.lens)
      }
    } catch (err) {
      postMessage({ type: "error", message: "worker setup failed: " + err })
      gl = null
      if (!Module && typeof H264Module !== "function") return
    }
    H264Module({
      locateFile: function (file) {
        return msg.base + file
      },
    }).then(
      function (m) {
        Module = m
        ctx = Module._h264_create()
        if (!ctx) {
          postMessage({ type: "error", message: "h264_create failed" })
          return
        }
        postMessage({ type: "ready", draws: !!gl })
        connect()
      },
      function (err) {
        postMessage({ type: "error", message: "wasm init failed: " + err })
      },
    )
  } else if (msg.type === "stream") {
    wsUrl = msg.wsUrl
    skipping = false
    if (Module && ctx) { Module._h264_destroy(ctx); ctx = Module._h264_create() }
    if (ws) { var old = ws; ws = null; try { old.close() } catch (err) { /* ignore */ } }
    connect()
  } else if (msg.type === "clock") {
    clockOffset = msg.offset
  } else if (msg.type === "transform") {
    if (gl) { gl.setTransform(msg.rotate, msg.mirror, msg.flip); gl.setLens(msg.lens) }
  } else if (msg.type === "resize") {
    if (canvas) {
      canvas.width = msg.width
      canvas.height = msg.height
    }
  }
}
