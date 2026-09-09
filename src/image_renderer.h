/* SPDX-License-Identifier: MIT */
#ifndef BATTY_IMAGE_RENDERER_H
#define BATTY_IMAGE_RENDERER_H
#include "presentation.h"
typedef struct BtImageRenderer BtImageRenderer;
BtImageRenderer *bt_images_new(char *, size_t);
void bt_images_free(BtImageRenderer *);
void bt_images_reset(BtImageRenderer *);
/* Borrows an immutable owned frame only until the following draw calls. */
int bt_images_prepare(BtImageRenderer *, const BtPresentation *);
int bt_images_draw(BtImageRenderer *, int layer, int width, int height, unsigned cols, unsigned rows);
const char *bt_images_error(BtImageRenderer *);
#endif
