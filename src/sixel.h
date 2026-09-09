/* SPDX-License-Identifier: MIT */
#ifndef BATTY_SIXEL_H
#define BATTY_SIXEL_H

#include <stddef.h>
#include <stdint.h>

enum {
    BT_SIXEL_MAX_DIMENSION = 4096,
    BT_SIXEL_PALETTE_SIZE = 256,
    BT_SIXEL_MAX_INPUT = 16 * 1024 * 1024,
    BT_SIXEL_MAX_EXPANSION = 64 * 1024 * 1024
};

typedef struct BtSixel BtSixel;
typedef struct { uint8_t rgb[BT_SIXEL_PALETTE_SIZE][3]; } BtSixelPalette;
typedef struct {
    unsigned width, height;
    /* Compact, top-to-bottom, straight RGBA. The caller frees this pointer. */
    uint8_t *pixels;
} BtSixelImage;

/* p1/p2 are the first two DCS parameters. Only p2 == 1 is transparent.
 * Supply the terminal's current RGB background; NULL means black.
 * The decoder starts with a private VT340-style palette. */
BtSixel *bt_sixel_new(unsigned p1, unsigned p2, const uint8_t background[3]);

/* Feed only the body after DCS q, without DCS/ST or cancellation controls.
 * Calls may split at any byte. Errors are sticky and return -1; success is 0.
 * Width/height are bounded independently; repeat expansion is also bounded. */
int bt_sixel_feed(BtSixel *, const void *, size_t);

/* Finish transfers ownership of a compact image to the caller on success.
 * An empty payload succeeds with width == height == 0 and pixels == NULL.
 * Each decoder can finish only once. */
int bt_sixel_finish(BtSixel *, BtSixelImage *);
const char *bt_sixel_error(const BtSixel *);
void bt_sixel_free(BtSixel *);

/* Optional shared-register support for DEC private mode 1070. Import before
 * feeding any bytes; export before freeing, normally after successful finish. */
int bt_sixel_set_palette(BtSixel *, const BtSixelPalette *);
void bt_sixel_get_palette(const BtSixel *, BtSixelPalette *);

#endif
