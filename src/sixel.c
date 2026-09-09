/* SPDX-License-Identifier: MIT */
#include "sixel.h"

#include <stdbool.h>
#include <stdlib.h>
#include <string.h>

struct BtSixel {
    uint8_t *pixels;
    unsigned stride, allocated_height, width, height;
    unsigned x, y, scale, declared_width, declared_height;
    uint8_t background[4];
    BtSixelPalette palette;
    unsigned color;
    unsigned command, param_index;
    uint32_t params[5];
    size_t input_bytes, expanded_pixels;
    bool painted, finished;
    const char *error;
};

static int fail(BtSixel *s, const char *error) {
    if (!s->error) s->error = error;
    return -1;
}

static uint8_t intensity(uint32_t value) {
    if (value > 100) value = 100;
    return (uint8_t)((value * 255 + 50) / 100);
}

/* DEC's HLS wheel starts at blue: its hue 120 is conventional HSL red. */
static void hls(uint32_t hue, uint32_t light, uint32_t sat, uint8_t rgb[3]) {
    if (hue > 360) hue = 360;
    if (light > 100) light = 100;
    if (sat > 100) sat = 100;
    unsigned angle = (hue + 240) % 360;
    double l = light / 100.0, saturation = sat / 100.0;
    double triangle = l < 0.5 ? 2.0 * l : 2.0 * (1.0 - l);
    double chroma = triangle * saturation;
    double fraction = (angle % 120) / 60.0;
    double second = chroma * (fraction <= 1.0 ? fraction : 2.0 - fraction);
    double components[3] = { 0, 0, 0 };
    switch (angle / 60) {
    case 0: components[0] = chroma; components[1] = second; break;
    case 1: components[0] = second; components[1] = chroma; break;
    case 2: components[1] = chroma; components[2] = second; break;
    case 3: components[1] = second; components[2] = chroma; break;
    case 4: components[0] = second; components[2] = chroma; break;
    default: components[0] = chroma; components[2] = second; break;
    }
    double offset = l - chroma / 2.0;
    for (unsigned i = 0; i < 3; ++i) {
        double value = (components[i] + offset) * 255.0;
        if (value < 0) value = 0;
        if (value > 255) value = 255;
        rgb[i] = (uint8_t)(value + 0.5);
    }
}

static void fill(uint8_t *pixels, size_t count, const uint8_t color[4]) {
    for (size_t i = 0; i < count; ++i) memcpy(pixels + i * 4, color, 4);
}

/* Power-of-two storage makes adversarial one-column growth amortized linear.
 * Stored pixels use <= 64 MiB; old+new buffers during growth use <= 96 MiB. */
static int reserve(BtSixel *s, unsigned width, unsigned height) {
    if (width > BT_SIXEL_MAX_DIMENSION || height > BT_SIXEL_MAX_DIMENSION)
        return fail(s, "Sixel dimensions exceed 4096 pixels");
    if (!width || !height) return 0;
    if (width <= s->stride && height <= s->allocated_height) return 0;
    unsigned stride = s->stride ? s->stride : 1;
    unsigned rows = s->allocated_height ? s->allocated_height : 1;
    while (stride < width) stride *= 2;
    while (rows < height) rows *= 2;
    size_t count = (size_t)stride * rows;
    uint8_t *pixels = malloc(count * 4);
    if (!pixels) return fail(s, "Cannot allocate Sixel image");
    fill(pixels, count, s->background);
    for (unsigned row = 0; row < s->allocated_height; ++row)
        memcpy(pixels + (size_t)row * stride * 4,
               s->pixels + (size_t)row * s->stride * 4, (size_t)s->stride * 4);
    free(s->pixels);
    s->pixels = pixels;
    s->stride = stride;
    s->allocated_height = rows;
    return 0;
}

static int raster(BtSixel *s) {
    /* Raster attributes belong before pixel data. Ignoring later attributes
     * avoids changing the scale of only half an already painted image. */
    if (s->painted) return 0;
    if (s->param_index < 1 || s->param_index > 3) return 0;
    uint32_t pan = s->params[0] ? s->params[0] : 1;
    uint32_t pad = s->params[1] ? s->params[1] : 1;
    uint32_t scale = pan / pad;
    if ((uint64_t)(pan % pad) * 2 >= pad) ++scale;
    if (!scale) scale = 1;
    if (scale > BT_SIXEL_MAX_DIMENSION)
        return fail(s, "Sixel aspect ratio exceeds image limit");
    unsigned width = s->param_index >= 2 ? s->params[2] : 0;
    uint64_t height = s->param_index >= 3 ? (uint64_t)s->params[3] * scale : 0;
    if (width > BT_SIXEL_MAX_DIMENSION || height > BT_SIXEL_MAX_DIMENSION)
        return fail(s, "Sixel raster exceeds 4096 pixels");
    s->scale = scale;
    s->declared_width = width;
    s->declared_height = (unsigned)height;
    s->width = width;
    s->height = (unsigned)height;
    return reserve(s, s->width, s->height);
}

static int finish_command(BtSixel *s) {
    int result = 0;
    if (s->command == '"') result = raster(s);
    else if (s->command == '#') {
        unsigned color = s->params[0];
        if (color >= BT_SIXEL_PALETTE_SIZE)
            return fail(s, "Sixel color register exceeds 255");
        if (s->param_index == 0) s->color = color;
        else if (s->param_index == 4) {
            if (s->params[1] == 2) {
                for (unsigned i = 0; i < 3; ++i)
                    s->palette.rgb[color][i] = intensity(s->params[i + 2]);
                s->color = color;
            } else if (s->params[1] == 1) {
                hls(s->params[2], s->params[3], s->params[4], s->palette.rgb[color]);
                s->color = color;
            }
        }
    }
    s->command = 0;
    s->param_index = 0;
    memset(s->params, 0, sizeof(s->params));
    return result;
}

static int emit(BtSixel *s, unsigned bits, unsigned count) {
    if (!count) count = 1;
    if (count > BT_SIXEL_MAX_DIMENSION - s->x)
        return fail(s, "Sixel row exceeds 4096 pixels");
    size_t expanded = (size_t)count * 6 * s->scale;
    if (expanded > BT_SIXEL_MAX_EXPANSION - s->expanded_pixels)
        return fail(s, "Sixel repeat expansion exceeds limit");
    s->expanded_pixels += expanded;
    unsigned rows = 6;
    if (s->declared_height) {
        rows = 0;
        for (unsigned value = bits; value; value >>= 1) ++rows;
    }
    uint64_t bottom = (uint64_t)s->y + (uint64_t)rows * s->scale;
    if (bottom > BT_SIXEL_MAX_DIMENSION)
        return fail(s, "Sixel column exceeds 4096 pixels");
    unsigned width = s->x + count;
    unsigned height = (unsigned)bottom;
    if (width < s->width) width = s->width;
    if (height < s->height) height = s->height;
    if (reserve(s, width, height) < 0) return -1;
    uint8_t color[4] = { 0, 0, 0, 255 };
    memcpy(color, s->palette.rgb[s->color], 3);
    for (unsigned bit = 0; bit < 6; ++bit) {
        if (!(bits & (1u << bit))) continue;
        for (unsigned scale_row = 0; scale_row < s->scale; ++scale_row) {
            unsigned row = s->y + bit * s->scale + scale_row;
            fill(s->pixels + ((size_t)row * s->stride + s->x) * 4, count, color);
        }
    }
    s->width = width;
    s->height = height;
    s->x += count;
    s->painted = true;
    return 0;
}

BtSixel *bt_sixel_new(unsigned p1, unsigned p2, const uint8_t background[3]) {
    BtSixel *s = calloc(1, sizeof(*s));
    if (!s) return NULL;
    if (background && p2 != 1) memcpy(s->background, background, 3);
    s->background[3] = p2 == 1 ? 0 : 255;
    s->scale = p1 == 2 ? 5 : p1 == 3 || p1 == 4 ? 3 : p1 >= 7 && p1 <= 9 ? 1 : 2;
    /* The VT340's sixteen initial color registers, expressed in HLS. Higher
     * registers start black; applications normally define them explicitly. */
    static const unsigned defaults[16][3] = {
        { 0, 0, 0 }, { 0, 50, 60 }, { 120, 46, 72 }, { 240, 50, 60 },
        { 60, 50, 60 }, { 300, 50, 60 }, { 180, 50, 60 }, { 0, 53, 0 },
        { 0, 26, 0 }, { 0, 46, 29 }, { 120, 43, 39 }, { 240, 46, 29 },
        { 60, 46, 29 }, { 300, 46, 29 }, { 180, 46, 29 }, { 0, 80, 0 }
    };
    for (unsigned i = 0; i < 16; ++i)
        hls(defaults[i][0], defaults[i][1], defaults[i][2], s->palette.rgb[i]);
    s->color = 3;
    return s;
}

int bt_sixel_feed(BtSixel *s, const void *data, size_t length) {
    if (!s) return -1;
    if (s->error) return -1;
    if (s->finished) return fail(s, "Sixel decoder already finished");
    if (length && !data) return fail(s, "Missing Sixel input");
    if (length > BT_SIXEL_MAX_INPUT - s->input_bytes)
        return fail(s, "Sixel encoded input exceeds 16 MiB");
    s->input_bytes += length;
    const uint8_t *bytes = data;
    for (size_t i = 0; i < length; ++i) {
        unsigned ch = bytes[i];
        if (ch < 0x20 || ch == 0x7f) continue;
        if (s->command && ch >= '0' && ch <= '9') {
            uint32_t *value = &s->params[s->param_index];
            if (*value > (UINT32_MAX - (ch - '0')) / 10)
                return fail(s, "Sixel numeric parameter overflows");
            *value = *value * 10 + ch - '0';
            continue;
        }
        if (s->command && ch == ';') {
            if (s->command == '!' || s->param_index >= 4)
                return fail(s, "Too many Sixel parameters");
            ++s->param_index;
            continue;
        }
        if (ch >= '?' && ch <= '~') {
            unsigned count = s->command == '!' ? s->params[0] : 1;
            if (finish_command(s) < 0 || emit(s, ch - '?', count) < 0) return -1;
            continue;
        }
        if (finish_command(s) < 0) return -1;
        if (ch == '#' || ch == '!' || ch == '"') s->command = ch;
        else if (ch == '$') s->x = 0;
        else if (ch == '-') {
            uint64_t row = (uint64_t)s->y + 6 * s->scale;
            /* One final newline may step past the canvas without painting. */
            if (row > BT_SIXEL_MAX_DIMENSION + 6u * s->scale)
                return fail(s, "Sixel cursor exceeds image limit");
            s->x = 0;
            s->y = (unsigned)row;
        }
    }
    return 0;
}

int bt_sixel_finish(BtSixel *s, BtSixelImage *image) {
    if (image) memset(image, 0, sizeof(*image));
    if (!s || !image) return -1;
    if (s->error) return -1;
    if (s->finished) return fail(s, "Sixel decoder already finished");
    if (finish_command(s) < 0) return -1;
    s->finished = true;
    if (!s->width || !s->height) return 0;
    /* Compact in place; target rows never overlap the next source row. */
    for (unsigned row = 0; row < s->height; ++row)
        memmove(s->pixels + (size_t)row * s->width * 4,
                s->pixels + (size_t)row * s->stride * 4, (size_t)s->width * 4);
    size_t bytes = (size_t)s->width * s->height * 4;
    uint8_t *compact = realloc(s->pixels, bytes);
    if (compact) s->pixels = compact;
    image->pixels = s->pixels;
    image->width = s->width;
    image->height = s->height;
    s->pixels = NULL;
    return 0;
}

const char *bt_sixel_error(const BtSixel *s) {
    return !s ? "Cannot allocate Sixel decoder" : s->error ? s->error : "";
}

void bt_sixel_free(BtSixel *s) {
    if (!s) return;
    free(s->pixels);
    free(s);
}

int bt_sixel_set_palette(BtSixel *s, const BtSixelPalette *palette) {
    if (!s || !palette) return -1;
    if (s->error) return -1;
    if (s->input_bytes || s->finished) return fail(s, "Palette import must precede Sixel data");
    s->palette = *palette;
    return 0;
}

void bt_sixel_get_palette(const BtSixel *s, BtSixelPalette *palette) {
    if (s && palette) *palette = s->palette;
}
