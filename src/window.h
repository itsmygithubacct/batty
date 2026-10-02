/* SPDX-License-Identifier: MIT */
#ifndef BATTY_WINDOW_H
#define BATTY_WINDOW_H
#include "render.h"
#include "selection_drag.h"
#include "surface.h"
typedef struct {
    BtSession session;
    SDL_Window *window;
    BtRenderer *renderer;
    BtSurface *surface;
    /* Embedded views borrow a host surface and never poll, swap or destroy it. */
    bool embedded;
    SDL_Rect region;
    GhosttyKeyEncoder key_encoder;
    GhosttyKeyEvent key_event;
    GhosttyMouseEncoder mouse_encoder;
    GhosttyMouseEvent mouse_event;
    BtSelectionDrag selection;
    SDL_Keysym text_key;
    SDL_Keysym held_keys[SDL_NUM_SCANCODES];
    uint32_t held_buttons;
    int pointer_x, pointer_y;
    bool text_repeat, text_pending, composing, close_requested, force_draw, selecting;
    bool focused, clipboard_write;
    uint64_t copy_ticket;
    pid_t owner;
    uint32_t window_id;
    int drawable_width, drawable_height;
    char error[256];
} BtWindow;
int bt_window_open(BtWindow *, const char *helper, char *const argv[], char *const env[],
                   unsigned cols, unsigned rows, const char *font, int font_size, bool headless);
int bt_window_attach(BtWindow *, const char *root, const char *name,
                     const char *font, int font_size, bool headless, bool observe);
int bt_window_open_persistent(BtWindow *, const char *service, const char *helper,
                              const char *root, const char *name,
                              char *const argv[], char *const env[],
                              unsigned cols, unsigned rows, const char *font,
                              int font_size, bool headless);
int bt_window_open_view(BtWindow *, BtSurface *, SDL_Rect, const char *helper,
                        char *const argv[], char *const env[], const char *font, int font_size);
int bt_window_attach_view(BtWindow *, BtSurface *, SDL_Rect, const char *root,
                          const char *name, const char *font, int font_size, bool observe);
int bt_window_attach_view_epoch(BtWindow *, BtSurface *, SDL_Rect, const char *root,
                                const char *name, const char *font, int font_size,
                                bool observe, uint64_t epoch);
int bt_window_persistent_view(BtWindow *, BtSurface *, SDL_Rect, const char *service,
                              const char *helper, const char *root, const char *name,
                              char *const argv[], char *const env[], const char *font, int font_size);
int bt_window_set_region(BtWindow *, SDL_Rect);
int bt_window_draw_view(BtWindow *, bool focused);
int bt_window_focus(BtWindow *, bool focused);
int bt_window_release_keys(BtWindow *);
int bt_window_release_pointer(BtWindow *);
int bt_window_pump(BtWindow *, int timeout_ms);
void bt_window_close(BtWindow *);
/* Close graphical resources and move a persistent controller into an empty
 * output session. The caller owns draining/closing that session. Local PTYs
 * and observers close normally; their output session remains empty. */
void bt_window_close_deferred(BtWindow *, BtSession *deferred);
int bt_window_paste(BtWindow *, const char *, size_t);
int bt_window_event(BtWindow *, const SDL_Event *);
int bt_window_resize(BtWindow *);
#endif
