/* SPDX-License-Identifier: MIT */
#ifndef BATTY_IMAGE_RENDERER_H
#define BATTY_IMAGE_RENDERER_H
#include "presentation.h"
typedef struct BtImageRenderer BtImageRenderer;
/* Lifetime upload counters survive epoch resets; cache bytes are current. */
typedef struct {
    uint64_t full_uploads, region_uploads, unchanged_updates, uploaded_bytes;
    size_t texture_bytes, shadow_bytes;
} BtImageStats;
BtImageStats bt_images_stats(const BtImageRenderer *);
BtImageRenderer *bt_images_new(char *, size_t);
void bt_images_free(BtImageRenderer *);
void bt_images_reset(BtImageRenderer *);
/* Borrows an immutable owned frame only until the following draw calls. */
int bt_images_prepare(BtImageRenderer *, const BtPresentation *);
int bt_images_draw(BtImageRenderer *, int layer, int width, int height, unsigned cols, unsigned rows,
                  int origin_x, int origin_y, int viewport_width, int viewport_height);
const char *bt_images_error(BtImageRenderer *);
#endif
