/*
 * h264dec.c — the thinnest possible wrapper around libavcodec's H.264 decoder
 * for WebAssembly. Feed it Annex B access units, get YUV420 planes back.
 *
 * Exports (see build.sh):
 *   h264_create()                     -> ctx or 0
 *   h264_decode(ctx, data, len)       -> 1 if a picture is ready, 0 if not, <0 on error
 *   h264_width/height(ctx)            -> dimensions of the last picture
 *   h264_plane(ctx, i) / h264_stride(ctx, i) -> pointer + stride of plane i (0=Y,1=U,2=V)
 *   h264_destroy(ctx)
 *
 * Single-threaded, no pthreads: the browser runs one instance per Web Worker.
 */

#include <stdlib.h>
#include <string.h>
#include <libavcodec/avcodec.h>

typedef struct {
    const AVCodec *codec;
    AVCodecContext *cc;
    AVFrame *frame;
    AVPacket *pkt;
    int have_frame;
} h264_ctx;

h264_ctx *h264_create(void) {
    h264_ctx *c = (h264_ctx *)calloc(1, sizeof(h264_ctx));
    if (!c) return NULL;
    c->codec = avcodec_find_decoder(AV_CODEC_ID_H264);
    if (!c->codec) { free(c); return NULL; }
    c->cc = avcodec_alloc_context3(c->codec);
    if (!c->cc) { free(c); return NULL; }
    c->cc->thread_count = 1;
    c->cc->flags |= AV_CODEC_FLAG_LOW_DELAY;
    c->cc->flags2 |= AV_CODEC_FLAG2_FAST;
    if (avcodec_open2(c->cc, c->codec, NULL) < 0) {
        avcodec_free_context(&c->cc);
        free(c);
        return NULL;
    }
    c->frame = av_frame_alloc();
    c->pkt = av_packet_alloc();
    return c;
}

int h264_decode(h264_ctx *c, const uint8_t *data, int len) {
    int ret;
    c->have_frame = 0;
    c->pkt->data = (uint8_t *)data;
    c->pkt->size = len;
    ret = avcodec_send_packet(c->cc, c->pkt);
    if (ret < 0 && ret != AVERROR(EAGAIN)) return ret;
    ret = avcodec_receive_frame(c->cc, c->frame);
    if (ret == 0) { c->have_frame = 1; return 1; }
    if (ret == AVERROR(EAGAIN)) return 0;
    return ret;
}

int h264_width(h264_ctx *c) { return c->have_frame ? c->frame->width : 0; }
int h264_height(h264_ctx *c) { return c->have_frame ? c->frame->height : 0; }
uint8_t *h264_plane(h264_ctx *c, int i) { return c->have_frame ? c->frame->data[i] : NULL; }
int h264_stride(h264_ctx *c, int i) { return c->have_frame ? c->frame->linesize[i] : 0; }
int h264_format(h264_ctx *c) { return c->have_frame ? c->frame->format : -1; }

/*
 * YUV420 -> RGBA in C, for the 2D-canvas render path (putImageData wants RGBA).
 * BT.601 limited range, fixed point. `out` must hold width*height*4 bytes.
 */
static inline uint8_t clip8(int v) { return v < 0 ? 0 : v > 255 ? 255 : (uint8_t)v; }

void h264_to_rgba(h264_ctx *c, uint8_t *out) {
    if (!c->have_frame) return;
    int w = c->frame->width, h = c->frame->height;
    const uint8_t *yp = c->frame->data[0], *up = c->frame->data[1], *vp = c->frame->data[2];
    int ys = c->frame->linesize[0], us = c->frame->linesize[1], vs = c->frame->linesize[2];
    for (int row = 0; row < h; row++) {
        const uint8_t *yr = yp + row * ys;
        const uint8_t *ur = up + (row >> 1) * us;
        const uint8_t *vr = vp + (row >> 1) * vs;
        uint8_t *o = out + row * w * 4;
        for (int col = 0; col < w; col++) {
            int cy = 298 * (yr[col] - 16);
            int d = ur[col >> 1] - 128, e = vr[col >> 1] - 128;
            o[0] = clip8((cy + 409 * e + 128) >> 8);
            o[1] = clip8((cy - 100 * d - 208 * e + 128) >> 8);
            o[2] = clip8((cy + 516 * d + 128) >> 8);
            o[3] = 255;
            o += 4;
        }
    }
}

void h264_destroy(h264_ctx *c) {
    if (!c) return;
    av_frame_free(&c->frame);
    av_packet_free(&c->pkt);
    avcodec_free_context(&c->cc);
    free(c);
}
