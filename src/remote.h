/* SPDX-License-Identifier: MIT */
#ifndef BATTY_REMOTE_H
#define BATTY_REMOTE_H
#include "session.h"

/* Local semantic input intent. Keyboard/button enums use the pinned Ghostty
 * API. Mouse coordinates are pixels inside the grid, excluding window padding.
 * The service applies its current modes before encoding terminal input. */
enum { BT_INTENT_KEY=1, BT_INTENT_PASTE, BT_INTENT_POINTER,
       BT_INTENT_WHEEL, BT_INTENT_FOCUS, BT_INTENT_SCROLL, BT_INTENT_CLIPBOARD_POLICY };
typedef struct {
    uint32_t type, action, key, mods, consumed, codepoint, button, buttons;
    int32_t x, y, delta;
    /* Focus intents use focused; key intents use composing. Keep the wire size stable. */
    union { uint32_t focused, composing; };
} BtIntent;
_Static_assert(sizeof(BtIntent)==48,"input intent wire layout");

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
/* A nonzero epoch pins attachment to the saved owner, rejecting a reused name
 * before publishing a view or sending input/resize requests. Zero is unpinned. */
int bt_remote_attach_epoch(BtSession *, const char *root, const char *name, bool observe, uint64_t epoch);
uint64_t bt_remote_epoch(const BtSession *);
bool bt_remote_observer(const BtSession *);
/* Successfully accepted publications for this connection, including HELLO.
 * Payload bytes measure sealed frame contents, not socket/header traffic.
 * Retained after disconnect; zero for local or closed sessions. */
typedef struct {
    uint64_t full_frames, delta_frames, unchanged_polls;
    uint64_t full_bytes, delta_bytes;
} BtRemoteFrameStats;
BtRemoteFrameStats bt_remote_frame_stats(const BtSession *);
/* Transport failure preserves the last presentation and child status. The
 * original reason stays available until close, even after rejected operations. */
bool bt_remote_disconnected(const BtSession *);
const char *bt_remote_failure(const BtSession *);
int bt_remote_intent(BtSession *, const BtIntent *, const void *text, size_t length);
/* Opt-in ordered input queue: 128 unacknowledged operations, 8 MiB payload.
 * SEND, INTENT and RESIZE success means queued; pump reports later failures.
 * Overflow returns ENOBUFS without accepting that operation. Payload is copied.
 * Default direct-session behavior stays synchronous. Disabling drains inputs.
 * flush waits for accepted input acknowledgments (not child consumption), up
 * to the existing three-second request deadline; close alone discards pending
 * input. Other explicit RPCs drain inputs before running. */
int bt_remote_input_queue(BtSession *, bool enabled);
int bt_remote_input_flush(BtSession *);
/* Drain accepted operations without waiting or starting another frame poll:
 * 0 drained, 1 pending, -1 failed. Used when detaching an off-screen view. */
int bt_remote_input_drain(BtSession *);
/* Called by bt_session_clipboard_policy to synchronize its local permission.
 * Queued policies invalidate older clipboard fetches immediately. A policy
 * request failure disconnects rather than retaining the previous permission. */
int bt_remote_clipboard_policy(BtSession *, bool enabled);
int bt_remote_list(const char *root, char **text, char *error, size_t error_size);
int bt_remote_terminate(const char *root, const char *name, char *error, size_t error_size);

/* BtSession dispatch implementation. */
/* timeout=0 sends queued input and polls a bounded batch of acknowledgments
 * plus one frame/clipboard reply without waiting. It queues the next poll
 * after completion. Positive timeouts and other explicit RPCs are synchronous. */
int bt_remote_pump(BtSession *, int timeout_ms);
int bt_remote_send(BtSession *, const void *, size_t);
/* Queued resize retains the last published geometry until a new frame arrives.
 * Adjacent unsent resizes coalesce; input between them preserves ordering. */
int bt_remote_resize(BtSession *, unsigned, unsigned, unsigned, unsigned);
int bt_remote_reset(BtSession *);
bool bt_remote_resize_pending(const BtSession *);
/* Nonblocking text jobs share request ordering with input and frames. Up to
 * 16 pending/unclaimed results, each capped by the caller at at most 1 MiB.
 * start returns a ticket; take returns 1 pending, 0 owned text, or -1 error.
 * Pump the session to make progress. take consumes completed results/errors.
 * cancel discards delivery; an in-flight slot stays reserved until its reply
 * is drained or the connection fails. Close releases every ticket/result. */
int bt_remote_text_start(BtSession *, bool selection, size_t limit, uint64_t *ticket);
int bt_remote_text_take(BtSession *, uint64_t ticket, char **text, size_t *length);
void bt_remote_text_cancel(BtSession *, uint64_t ticket);
char *bt_remote_text(BtSession *, bool selection, size_t *length);
void bt_remote_close(BtSession *);
#endif
