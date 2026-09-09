/* SPDX-License-Identifier: MIT */
#ifndef BATTY_WINDOW_H
#define BATTY_WINDOW_H
#include "render.h"
typedef struct {
    BtSession session;
    SDL_Window *window;
    BtRenderer *renderer;
    GhosttyKeyEncoder key_encoder;
    GhosttyKeyEvent key_event;
    GhosttyMouseEncoder mouse_encoder;
    GhosttyMouseEvent mouse_event;
    SDL_Keysym text_key;
    bool text_repeat, text_pending, close_requested, force_draw, selecting;
    uint16_t selection_x, selection_y;
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
int bt_window_pump(BtWindow *, int timeout_ms);
void bt_window_close(BtWindow *);
int bt_window_paste(BtWindow *, const char *, size_t);
int bt_window_event(BtWindow *, const SDL_Event *);
int bt_window_resize(BtWindow *);
#endif
