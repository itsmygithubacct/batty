/* SPDX-License-Identifier: MIT */
#ifndef BATTY_REMOTE_H
#define BATTY_REMOTE_H
#include "session.h"

/* Local semantic input intent. Keyboard/button enums use the pinned Ghostty
 * API. Mouse coordinates are pixels inside the grid, excluding window padding.
 * The service applies its current modes before encoding terminal input. */
enum { BT_INTENT_KEY=1, BT_INTENT_PASTE, BT_INTENT_POINTER,
       BT_INTENT_WHEEL, BT_INTENT_FOCUS, BT_INTENT_SCROLL };
typedef struct {
    uint32_t type, action, key, mods, consumed, codepoint, button, buttons;
    int32_t x, y, delta;
    uint32_t focused;
} BtIntent;

/* root must be absolute, user-owned and mode 0700 (created if absent).
 * Names are 1..48 ASCII letters/digits/underscore/dash/dot, no leading dot.
 * create: 0 started, 1 already exists, -1 error. Never replaces an endpoint. */
int bt_remote_create(const char *service, const char *helper,
                     const char *root, const char *name,
                     char *const argv[], char *const env[],
                     unsigned cols, unsigned rows, unsigned cw, unsigned ch,
                     char *error, size_t error_size);
int bt_remote_create_owned(const char *service, const char *helper,
                           const char *root, const char *name,
                           char *const argv[], char *const env[],
                           unsigned cols, unsigned rows, unsigned cw, unsigned ch,
                           char *error, size_t error_size, uint64_t *epoch);
/* Roll back only this creation, and only if no frontend has claimed it. */
int bt_remote_cancel(const char *root, const char *name, uint64_t epoch,
                     char *error, size_t error_size);
/* Attach receives a complete current frame. One controller and up to eight
 * observers. Observe handles cannot mutate terminal state. */
int bt_remote_attach(BtSession *, const char *root, const char *name, bool observe);
bool bt_remote_observer(const BtSession *);
int bt_remote_intent(BtSession *, const BtIntent *, const void *text, size_t length);
int bt_remote_list(const char *root, char **text, char *error, size_t error_size);
int bt_remote_terminate(const char *root, const char *name, char *error, size_t error_size);

/* BtSession dispatch implementation. */
int bt_remote_pump(BtSession *, int timeout_ms);
int bt_remote_send(BtSession *, const void *, size_t);
int bt_remote_resize(BtSession *, unsigned, unsigned, unsigned, unsigned);
char *bt_remote_text(BtSession *, bool selection, size_t *length);
void bt_remote_close(BtSession *);
#endif
