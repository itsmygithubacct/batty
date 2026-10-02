/* SPDX-License-Identifier: MIT */
#ifndef BATTY_RENDER_H
#define BATTY_RENDER_H
#include "session.h"
#include "image_renderer.h"
#include <SDL.h>
typedef struct BtRenderer BtRenderer;
BtRenderer *bt_renderer_new(SDL_Window *, const char *, int, char *, size_t);
/* The supplied context belongs to the host surface and outlives the renderer. */
BtRenderer *bt_renderer_new_shared(SDL_Window *, SDL_GLContext, const char *, int, char *, size_t);
void bt_renderer_free(BtRenderer *);
int bt_renderer_metrics(BtRenderer *, int *, int *);
/* Draws an owned remote presentation when supplied by the session; otherwise
 * retains the local terminal's sole presenter. Does not parse remote output. */
int bt_renderer_draw(BtRenderer *, BtSession *, bool force, bool focused);
/* Frontend-only IME composition, drawn over the terminal at its cursor. */
void bt_renderer_preedit(BtRenderer *, const char *utf8, int start, int length);
/* Compose into a top-left-origin rectangle in drawable pixels. Clears only
 * this rectangle, never swaps the window. The host redraws all visible views
 * before swapping; back-buffer preservation across swaps is not assumed. */
int bt_renderer_draw_region(BtRenderer *, BtSession *, SDL_Rect, bool focused);
/* Scale the presentation's own pixel grid into a clipped drawable rectangle.
 * Rendering only: this never changes the terminal's PTY or input geometry. */
int bt_renderer_draw_region_scaled(BtRenderer *, BtSession *, SDL_Rect, bool focused);
int bt_renderer_capture(BtRenderer *, const char *path);
int bt_renderer_graphics(BtRenderer *, char *, size_t);
const char *bt_renderer_error(BtRenderer *);
uint64_t bt_renderer_frames(BtRenderer *);
BtImageStats bt_renderer_image_stats(BtRenderer *);
/* Drops cached image textures and pixel shadows; the next draw recreates them
 * from the retained presentation. The renderer owns its GL context binding. */
int bt_renderer_drop_image_cache(BtRenderer *);
bool bt_renderer_due(BtRenderer *);
#endif
