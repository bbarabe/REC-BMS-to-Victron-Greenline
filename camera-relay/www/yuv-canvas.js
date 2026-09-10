/* yuv-canvas.js — draws packed YUV420 frames with WebGL, with rotation and mirror.
 *
 * var view = new YuvCanvas(canvasElement)
 * view.setTransform(rotateDeg, mirror, flip)   // 0/90/180/270, booleans
 * view.draw(width, height, Uint8Array yuv)     // Y (w*h) then U then V ((w/2)*(h/2) each)
 *
 * Colour conversion runs in the fragment shader (BT.601 limited range), so the
 * main thread only uploads three textures per frame. Plain ES2015.
 */
"use strict"

function YuvCanvas(canvas) {
  this.canvas = canvas
  var gl = canvas.getContext("webgl", { preserveDrawingBuffer: false, antialias: false, alpha: false }) ||
    canvas.getContext("experimental-webgl")
  if (!gl) throw new Error("WebGL not available")
  this.gl = gl
  this.rotate = 0
  this.mirror = false
  this.flip = false
  this.frameWidth = 0
  this.frameHeight = 0
  this._setup()
}

YuvCanvas.prototype._setup = function () {
  var gl = this.gl
  var vs =
    "attribute vec2 aPos; attribute vec2 aTex; uniform mat3 uTex; varying vec2 vTex;" +
    "void main(){ vTex = (uTex * vec3(aTex, 1.0)).xy; gl_Position = vec4(aPos, 0.0, 1.0); }"
  // Lens correction runs in source space, after the rotation matrix: for each
  // output texel, find where the lens put it in the source and sample there.
  // Division model (Fitzgibbon): r_src = r / (1 + k1 r^2 + k2 r^4), which stays
  // monotonic for any positive k1 (barrel), unlike the polynomial that inverts
  // the picture at the corners. r = 1 at the source corners at zoom 1; x is
  // scaled by the aspect so the lens stays round. Texels that land outside the
  // source are painted black rather than clamped, so nothing is invented and
  // the true picture boundary is visible (uLens.z = zoom).
  var fs =
    "precision mediump float; varying vec2 vTex;" +
    "uniform sampler2D uY; uniform sampler2D uU; uniform sampler2D uV;" +
    "uniform vec3 uLens; uniform vec2 uCenter; uniform float uAspect;" +
    "void main(){" +
    "  float diag = sqrt(uAspect * uAspect + 1.0);" +
    "  vec2 q = (vTex - 0.5 - uCenter) * 2.0 / (uLens.z * diag);" +
    "  q.x *= uAspect;" +
    "  float r2 = dot(q, q);" +
    "  q /= max(0.15, 1.0 + uLens.x * r2 + uLens.y * r2 * r2);" +
    "  q.x /= uAspect;" +
    "  vec2 t = q * 0.5 * diag + 0.5 + uCenter;" +
    "  if (t.x < 0.0 || t.x > 1.0 || t.y < 0.0 || t.y > 1.0) { gl_FragColor = vec4(0.0, 0.0, 0.0, 1.0); return; }" +
    "  float y = 1.1643 * (texture2D(uY, t).r - 0.0625);" +
    "  float u = texture2D(uU, t).r - 0.5;" +
    "  float v = texture2D(uV, t).r - 0.5;" +
    "  gl_FragColor = vec4(y + 1.5958 * v, y - 0.39173 * u - 0.8129 * v, y + 2.017 * u, 1.0);" +
    "}"
  var prog = gl.createProgram()
  gl.attachShader(prog, this._shader(gl.VERTEX_SHADER, vs))
  gl.attachShader(prog, this._shader(gl.FRAGMENT_SHADER, fs))
  gl.linkProgram(prog)
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error("shader link: " + gl.getProgramInfoLog(prog))
  gl.useProgram(prog)
  this.prog = prog

  var quad = new Float32Array([-1, -1, 0, 1, 1, -1, 1, 1, -1, 1, 0, 0, 1, 1, 1, 0])
  var buf = gl.createBuffer()
  gl.bindBuffer(gl.ARRAY_BUFFER, buf)
  gl.bufferData(gl.ARRAY_BUFFER, quad, gl.STATIC_DRAW)
  var aPos = gl.getAttribLocation(prog, "aPos")
  var aTex = gl.getAttribLocation(prog, "aTex")
  gl.enableVertexAttribArray(aPos)
  gl.enableVertexAttribArray(aTex)
  gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 16, 0)
  gl.vertexAttribPointer(aTex, 2, gl.FLOAT, false, 16, 8)

  this.uTex = gl.getUniformLocation(prog, "uTex")
  this.uLens = gl.getUniformLocation(prog, "uLens")
  this.uCenter = gl.getUniformLocation(prog, "uCenter")
  this.uAspect = gl.getUniformLocation(prog, "uAspect")
  this.setLens(null)
  this.textures = []
  var names = ["uY", "uU", "uV"]
  for (var i = 0; i < 3; i++) {
    var tex = gl.createTexture()
    gl.activeTexture(gl.TEXTURE0 + i)
    gl.bindTexture(gl.TEXTURE_2D, tex)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE)
    gl.uniform1i(gl.getUniformLocation(prog, names[i]), i)
    this.textures.push(tex)
  }
  gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1)
  this._updateTransform()
}

YuvCanvas.prototype._shader = function (type, src) {
  var gl = this.gl
  var s = gl.createShader(type)
  gl.shaderSource(s, src)
  gl.compileShader(s)
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error("shader: " + gl.getShaderInfoLog(s))
  return s
}

// lens = { k1, k2, zoom, cx, cy } or null for none. Positive k1 straightens a barrel.
YuvCanvas.prototype.setLens = function (lens) {
  var l = lens || {}
  var k1 = +l.k1 || 0, k2 = +l.k2 || 0, zoom = +l.zoom || 1, cx = +l.cx || 0, cy = +l.cy || 0
  if (!(zoom > 0.05)) zoom = 1
  this.gl.uniform3f(this.uLens, k1, k2, zoom)
  this.gl.uniform2f(this.uCenter, cx, cy)
}

YuvCanvas.prototype.setTransform = function (rotate, mirror, flip) {
  this.rotate = ((rotate % 360) + 360) % 360
  this.mirror = !!mirror
  this.flip = !!flip
  this._updateTransform()
}

// Texture-coordinate matrix: translate to centre, mirror/flip, rotate, translate back.
// `rotate` is clockwise degrees as seen on screen; the texture lookup turns the
// other way, hence the negated angle.
YuvCanvas.prototype._updateTransform = function () {
  var r = (-this.rotate * Math.PI) / 180
  var c = Math.cos(r)
  var s = Math.sin(r)
  var mx = this.mirror ? -1 : 1
  var my = this.flip ? -1 : 1
  // column-major mat3: [a b 0, c d 0, tx ty 1]
  var a = c * mx
  var b = s * mx
  var cc = -s * my
  var d = c * my
  var tx = 0.5 - 0.5 * a - 0.5 * cc
  var ty = 0.5 - 0.5 * b - 0.5 * d
  this.gl.uniformMatrix3fv(this.uTex, false, new Float32Array([a, b, 0, cc, d, 0, tx, ty, 1]))
}

// Size the canvas backing store to the displayed size in device pixels (a tablet
// at devicePixelRatio 1.75 would otherwise show 1080p through a 494px-wide
// buffer) and keep the frame's aspect (after rotation) inside it with letterboxing.
// An OffscreenCanvas has no clientWidth; the page sizes it through "resize".
YuvCanvas.prototype._fit = function (w, h) {
  var gl = this.gl
  var canvas = this.canvas
  var dpr = typeof window !== "undefined" && window.devicePixelRatio > 0 ? window.devicePixelRatio : 1
  var cw = canvas.clientWidth ? Math.round(canvas.clientWidth * dpr) : canvas.width
  var ch = canvas.clientHeight ? Math.round(canvas.clientHeight * dpr) : canvas.height
  if (canvas.width !== cw || canvas.height !== ch) {
    canvas.width = cw
    canvas.height = ch
  }
  var fw = this.rotate % 180 === 0 ? w : h
  var fh = this.rotate % 180 === 0 ? h : w
  var scale = Math.min(cw / fw, ch / fh)
  var vw = Math.round(fw * scale)
  var vh = Math.round(fh * scale)
  gl.viewport((cw - vw) >> 1, (ch - vh) >> 1, vw, vh)
}

YuvCanvas.prototype.draw = function (w, h, yuv) {
  var gl = this.gl
  var cw = (w + 1) >> 1
  var ch = (h + 1) >> 1
  var planes = [
    [w, h, yuv.subarray(0, w * h)],
    [cw, ch, yuv.subarray(w * h, w * h + cw * ch)],
    [cw, ch, yuv.subarray(w * h + cw * ch, w * h + 2 * cw * ch)],
  ]
  var resized = w !== this.frameWidth || h !== this.frameHeight
  if (resized) gl.uniform1f(this.uAspect, w / h)
  this.frameWidth = w
  this.frameHeight = h
  this._fit(w, h)
  gl.clearColor(0, 0, 0, 1)
  gl.clear(gl.COLOR_BUFFER_BIT)
  for (var i = 0; i < 3; i++) {
    gl.activeTexture(gl.TEXTURE0 + i)
    gl.bindTexture(gl.TEXTURE_2D, this.textures[i])
    if (resized) {
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.LUMINANCE, planes[i][0], planes[i][1], 0, gl.LUMINANCE, gl.UNSIGNED_BYTE, planes[i][2])
    } else {
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, planes[i][0], planes[i][1], gl.LUMINANCE, gl.UNSIGNED_BYTE, planes[i][2])
    }
  }
  gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4)
}
