#!/usr/bin/env bash
# Builds www/h264dec.js + www/h264dec.wasm: libavcodec's H.264 decoder only,
# wrapped by h264dec.c, for the Simrad's Qt WebEngine 5.12 (Chromium 69):
# WebAssembly MVP, no bulk memory, no BigInt, no threads.
#
# Needs emscripten 3.1.x: 4.x refuses MIN_CHROME_VERSION below 85, and that
# flag is what makes emcc lower sign-ext/bulk-memory for the old engine.
# Tested with emsdk 3.1.74.
#
# Usage: EMSDK=/path/to/emsdk FFMPEG_SRC=/path/to/ffmpeg ./build.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/../www"
: "${EMSDK:?set EMSDK to the emsdk checkout}"
: "${FFMPEG_SRC:?set FFMPEG_SRC to an FFmpeg source tree (6.1 tested)}"
JOBS="${JOBS:-$(nproc)}"

# shellcheck disable=SC1091
source "$EMSDK/emsdk_env.sh" >/dev/null

if [ ! -f "$FFMPEG_SRC/libavcodec/libavcodec.a" ] || [ "${REBUILD_FFMPEG:-0}" = 1 ]; then
  echo ">> configuring ffmpeg (h264 decoder only)"
  (
    cd "$FFMPEG_SRC"
    [ -f ffbuild/config.mak ] && make distclean >/dev/null 2>&1 || true
    emconfigure ./configure \
      --cc=emcc --cxx=em++ --ar=emar --ranlib=emranlib --nm=llvm-nm \
      --target-os=none --arch=x86_32 --enable-cross-compile \
      --disable-x86asm --disable-inline-asm --disable-asm \
      --disable-stripping --disable-programs --disable-doc \
      --disable-everything \
      --enable-decoder=h264 --enable-parser=h264 \
      --disable-avformat --disable-avfilter --disable-avdevice \
      --disable-swresample --disable-swscale --disable-postproc \
      --disable-network --disable-pthreads --disable-w32threads --disable-os2threads \
      --disable-autodetect --disable-runtime-cpudetect --disable-debug \
      --disable-iconv --disable-zlib --disable-bzlib --disable-lzma \
      --extra-cflags="-O3 -fno-exceptions" \
      --extra-cxxflags="-O3 -fno-exceptions"
    emmake make -j"$JOBS" libavcodec/libavcodec.a libavutil/libavutil.a
  )
fi

echo ">> compiling wrapper"
mkdir -p "$OUT"
emcc -O3 "$HERE/h264dec.c" \
  -I"$FFMPEG_SRC" \
  "$FFMPEG_SRC/libavcodec/libavcodec.a" "$FFMPEG_SRC/libavutil/libavutil.a" \
  -s WASM=1 \
  -s MODULARIZE=1 -s EXPORT_NAME=H264Module \
  -s ENVIRONMENT=web,worker \
  -s MIN_CHROME_VERSION=69 -s WASM_BIGINT=0 \
  -s ALLOW_MEMORY_GROWTH=1 -s INITIAL_MEMORY=33554432 \
  -s FILESYSTEM=0 \
  -s EXPORTED_FUNCTIONS=_h264_create,_h264_decode,_h264_width,_h264_height,_h264_plane,_h264_stride,_h264_format,_h264_to_rgba,_h264_destroy,_malloc,_free \
  -s EXPORTED_RUNTIME_METHODS=HEAPU8 \
  -o "$OUT/h264dec.js"

# emscripten's glue uses optional chaining once; Chromium 69 has none.
sed -i 's/document\.currentScript?\.src/(document.currentScript \&\& document.currentScript.src)/' "$OUT/h264dec.js"
if grep -q '?\.' "$OUT/h264dec.js"; then echo "!! optional chaining still present in h264dec.js"; exit 1; fi

ls -la "$OUT/h264dec.js" "$OUT/h264dec.wasm"
echo ">> done"
