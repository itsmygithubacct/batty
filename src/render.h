/* SPDX-License-Identifier: MIT */
#ifndef BATTY_RENDER_H
#define BATTY_RENDER_H
#include "session.h"
#include <SDL.h>
typedef struct BtRenderer BtRenderer;
BtRenderer *bt_renderer_new(SDL_Window *, const char *, int, char *, size_t);
void bt_renderer_free(BtRenderer *);
int bt_renderer_metrics(BtRenderer *, int *, int *);
/* Draws an owned remote presentation when supplied by the session; otherwise
 * retains the local terminal's sole presenter. Does not parse remote output. */
int bt_renderer_draw(BtRenderer *, BtSession *, bool force, bool focused);
int bt_renderer_capture(BtRenderer *, const char *path);
int bt_renderer_graphics(BtRenderer *, char *, size_t);
const char *bt_renderer_error(BtRenderer *);
uint64_t bt_renderer_frames(BtRenderer *);
#endif
