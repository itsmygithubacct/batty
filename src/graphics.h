/* SPDX-License-Identifier: MIT */
#ifndef BATTY_GRAPHICS_H
#define BATTY_GRAPHICS_H
#include <ghostty/vt.h>
#include <stddef.h>
#include <stdint.h>
typedef struct BtGraphics BtGraphics;
typedef void (*BtGraphicsReply)(void *, const uint8_t *, size_t);
BtGraphics *bt_graphics_new(GhosttyTerminal);
void bt_graphics_free(BtGraphics *);
void bt_graphics_set_reply(BtGraphics *, BtGraphicsReply, void *);
void bt_graphics_resize(BtGraphics *, unsigned, unsigned, unsigned, unsigned);
void bt_graphics_feed(BtGraphics *, const void *, size_t);
#endif
