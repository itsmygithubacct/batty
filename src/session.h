/* SPDX-License-Identifier: MIT */
#ifndef BATTY_SESSION_H
#define BATTY_SESSION_H
#include <ghostty/vt.h>
#include <stdbool.h>
#include <stdint.h>
#include <sys/types.h>
#include "graphics.h"
#include "recorder.h"

typedef struct BtPresentation BtPresentation;
typedef struct BtRemote BtRemote;
#define BT_CLIPBOARD_LIMIT (1024u * 1024u)

enum { BT_STARTED = 1, BT_READY, BT_EXEC_ERROR, BT_EXIT };
enum { BT_RECORD_UNKNOWN, BT_RECORD_DISABLED, BT_RECORD_ACTIVE,
       BT_RECORD_FINISHING, BT_RECORD_COMPLETE, BT_RECORD_FAILED };
typedef struct { int32_t type, value; } BtMessage;

typedef struct {
    GhosttyTerminal terminal;
    BtGraphics *graphics;
    BtRemote *remote;
    BtRecorder *recorder;
    char *recovery_arguments;
    char **recovery_argv;
    int recorder_error;
    unsigned remote_recording;
    char remote_transcript_id[49];
    char *remote_transcript_directory;
    BtPresentation *presentation;
    int master, control, status;
    pid_t supervisor, child;
    uint16_t cols, rows, cell_width, cell_height;
    uint8_t *pending;
    size_t pending_start, pending_end, pending_capacity;
    uint8_t status_bytes[sizeof(BtMessage)];
    size_t status_used;
    bool ready, exited, eof, done, title_changed, restoring;
    int exit_status, exec_error;
    uint64_t exit_time, bytes_read, bytes_written, graphics_revision, graphics_next_tick;
    char error[256];
    char title_cache[1024];
    char *clipboard;
    size_t clipboard_length;
    bool clipboard_enabled;
} BtSession;

uint64_t bt_millis(void);
unsigned bt_session_recording(const BtSession *);
const char *bt_session_transcript_id(const BtSession *);
const char *bt_session_transcript_directory(const BtSession *);
int bt_session_open(BtSession *, const char *helper, char *const argv[],
                    char *const env[], unsigned cols, unsigned rows,
                    unsigned cell_width, unsigned cell_height);
int bt_session_pump(BtSession *, int timeout_ms);
int bt_session_send(BtSession *, const void *, size_t);
void bt_session_feed(BtSession *, const void *, size_t);
int bt_session_resize(BtSession *, unsigned, unsigned, unsigned, unsigned);
/* Reset the authoritative terminal parser and its graphics decoder, retaining the PTY. */
int bt_session_reset(BtSession *);
void bt_session_close(BtSession *);
char *bt_session_text(BtSession *, bool selection, size_t *length);
const char *bt_session_title(BtSession *);
int bt_session_clipboard_policy(BtSession *, bool enabled);
void bt_session_clipboard_clear(BtSession *);
#endif
