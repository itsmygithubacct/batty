/* SPDX-License-Identifier: MIT */
#ifndef BATTY_SURFACE_H
#define BATTY_SURFACE_H
#include <SDL.h>
#include <stddef.h>

/* An OS window and its GL context. Terminal views borrow both. All views must
 * be destroyed before the surface. Coordinates exposed by rendering APIs are
 * drawable pixels, with the origin at the top left. SDL events retain their
 * logical window coordinates until a view translates them. */
typedef struct BtSurface BtSurface;
typedef void (*BtSurfaceEvent)(void *, const SDL_Event *);
BtSurface *bt_surface_new(const char *title, int width, int height, char *, size_t);
void bt_surface_free(BtSurface *);
SDL_Window *bt_surface_window(BtSurface *);
SDL_GLContext bt_surface_context(BtSurface *);
void bt_surface_handler(BtSurface *, BtSurfaceEvent, void *);
/* Dispatch to the owner of each event, including other OS windows. Handlers
 * may request closure but must not destroy surfaces during dispatch. */
void bt_surface_poll(void);
int bt_surface_clear(BtSurface *, unsigned char, unsigned char, unsigned char);
int bt_surface_present(BtSurface *);
/* Capture the freshly drawn back buffer, before present. */
int bt_surface_capture(BtSurface *, const char *);
const char *bt_surface_error(BtSurface *);
#endif
