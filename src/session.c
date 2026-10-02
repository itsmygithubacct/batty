/* SPDX-License-Identifier: MIT */
#define _GNU_SOURCE
#include "session.h"
#include "remote.h"
#include "recovery.h"
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pty.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define BT_QUEUE_LIMIT (8u * 1024u * 1024u)
#define BT_READ_BUDGET (256u * 1024u)

uint64_t bt_millis(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000 + (uint64_t)ts.tv_nsec / 1000000;
}
static int fail(BtSession *s, const char *operation, int error) {
    snprintf(s->error, sizeof(s->error), "%s: %s", operation, strerror(error));
    errno=error;
    return -1;
}

int bt_session_send(BtSession *s, const void *bytes, size_t len) {
    if (s->remote) return bt_remote_send(s,bytes,len);
    if (s->eof) return fail(s, "PTY has closed", EPIPE);
    size_t remaining = s->pending_end - s->pending_start;
    if (len > BT_QUEUE_LIMIT - remaining) return fail(s, "PTY input queue limit", ENOBUFS);
    if (s->pending_start) memmove(s->pending, s->pending + s->pending_start, remaining);
    s->pending_start = 0;
    s->pending_end = remaining;
    if (remaining + len > s->pending_capacity) {
        size_t cap = s->pending_capacity ? s->pending_capacity : 4096;
        while (cap < remaining + len) cap *= 2;
        uint8_t *p = realloc(s->pending, cap);
        if (!p) return fail(s, "allocate input queue", ENOMEM);
        s->pending = p;
        s->pending_capacity = cap;
    }
    if (len) memcpy(s->pending + remaining, bytes, len);
    s->pending_end += len;
    return 0;
}
static void reply(GhosttyTerminal terminal, void *userdata, const uint8_t *bytes, size_t len) {
    (void)terminal;
    BtSession *s = userdata;
    if (!s->eof && !s->restoring) (void)bt_session_send(s, bytes, len);
}
static void graphics_reply(void *userdata, const uint8_t *bytes, size_t len) {
    reply(NULL,userdata,bytes,len);
}
static void title(GhosttyTerminal terminal, void *userdata) {
    (void)terminal;
    ((BtSession *)userdata)->title_changed = true;
}
void bt_session_clipboard_clear(BtSession *s) {
    free(s->clipboard); s->clipboard=NULL; s->clipboard_length=0;
}
int bt_session_clipboard_policy(BtSession *s, bool enabled) {
    bool changed=s->clipboard_enabled!=enabled;
    if(!enabled) { bt_session_clipboard_clear(s); s->clipboard_enabled=false; }
    if(!changed) return 0;
    if(s->remote) {
        int rc=bt_remote_clipboard_policy(s,enabled);
        /* Revocation applies locally before queued policy delivery or any
         * synchronous drain. It also remains effective on request failure. */
        if(!enabled) bt_session_clipboard_clear(s);
        if(rc) return -1;
    }
    s->clipboard_enabled=enabled; return 0;
}
static bool utf8_text(const unsigned char *text, size_t size) {
    for(size_t i=0;i<size;) {
        unsigned c=text[i++],need=0,min=0;
        if(c<128) { if(!c) return false; continue; }
        if(c>=0xc2 && c<=0xdf) { c&=31; need=1; min=0x80; }
        else if(c>=0xe0 && c<=0xef) { c&=15; need=2; min=0x800; }
        else if(c>=0xf0 && c<=0xf4) { c&=7; need=3; min=0x10000; }
        else return false;
        if(need>size-i) return false;
        while(need--) { unsigned next=text[i++]; if((next&0xc0)!=0x80) return false; c=(c<<6)|(next&63); }
        if(c<min || c>0x10ffff || (c>=0xd800 && c<=0xdfff)) return false;
    }
    return true;
}
static void clipboard_write(GhosttyTerminal terminal, void *userdata, const GhosttyClipboardWrite *write) {
    (void)terminal;
    BtSession *s=userdata;
    if(write->size<sizeof(*write)) return;
    GhosttyClipboardWriteReply result=GHOSTTY_INIT_SIZED(GhosttyClipboardWriteReply);
    result.result=GHOSTTY_CLIPBOARD_WRITE_RESULT_DENIED;
    if(!s->clipboard_enabled || s->restoring) goto answer;
    result.result=GHOSTTY_CLIPBOARD_WRITE_RESULT_UNSUPPORTED;
    if(write->location!=GHOSTTY_CLIPBOARD_LOCATION_STANDARD || write->contents_len>1) goto answer;
    const char *text=""; size_t size=0;
    if(write->contents_len) {
        const GhosttyClipboardContent *content=&write->contents[0];
        if(content->mime.len!=10 || memcmp(content->mime.ptr,"text/plain",10)) goto answer;
        text=(const char *)content->data.ptr; size=content->data.len;
    }
    result.result=GHOSTTY_CLIPBOARD_WRITE_RESULT_INVALID_DATA;
    if(size>BT_CLIPBOARD_LIMIT || !utf8_text((const unsigned char *)text,size)) goto answer;
    char *copy=malloc(size+1);
    result.result=GHOSTTY_CLIPBOARD_WRITE_RESULT_IO_ERROR;
    if(!copy) goto answer;
    if(size) memcpy(copy,text,size);
    copy[size]=0;
    bt_session_clipboard_clear(s); s->clipboard=copy; s->clipboard_length=size;
    result.result=GHOSTTY_CLIPBOARD_WRITE_RESULT_SUCCESS;
answer:
    write->reply(write,&result);
}
static bool size_query(GhosttyTerminal terminal, void *userdata, GhosttySizeReportSize *out) {
    (void)terminal;
    BtSession *s = userdata;
    out->rows = s->rows;
    out->columns = s->cols;
    out->cell_width = s->cell_width;
    out->cell_height = s->cell_height;
    return true;
}
static int read_status(BtSession *s) {
    for (;;) {
        ssize_t n = read(s->status, s->status_bytes + s->status_used, sizeof(BtMessage) - s->status_used);
        if (n > 0) {
            s->status_used += (size_t)n;
            if (s->status_used != sizeof(BtMessage)) continue;
            BtMessage msg;
            memcpy(&msg, s->status_bytes, sizeof(msg));
            s->status_used = 0;
            if (msg.type == BT_STARTED) s->child = msg.value;
            else if (msg.type == BT_READY) s->ready = true;
            else if (msg.type == BT_EXEC_ERROR) s->exec_error = msg.value;
            else if (msg.type == BT_EXIT) {
                s->exited = true;
                s->exit_status = msg.value;
                s->exit_time = bt_millis();
            }
        } else if (n == 0) {
            if (!s->exited && !s->exec_error) return fail(s, "session supervisor ended without status", EIO);
            return 0;
        } else if (errno == EINTR) continue;
        else if (errno == EAGAIN || errno == EWOULDBLOCK) return 0;
        else return fail(s, "read session status", errno);
    }
}
static int spawn_helper(BtSession *s, const char *helper, char **args, char *const env[], const int high[4]) {
    posix_spawn_file_actions_t actions;
    posix_spawnattr_t attrs;
    int error=posix_spawn_file_actions_init(&actions);
    if(error) return error;
    error=posix_spawnattr_init(&attrs);
    if(error) { posix_spawn_file_actions_destroy(&actions); return error; }
#define SPAWN_CHECK(call) do { error=(call); if(error) goto cleanup; } while(0)
    for(int i=0;i<3;++i) SPAWN_CHECK(posix_spawn_file_actions_addopen(&actions,i,"/dev/null",O_RDWR,0));
    for(int i=0;i<4;++i) SPAWN_CHECK(posix_spawn_file_actions_adddup2(&actions,high[i],i+3));
    SPAWN_CHECK(posix_spawn_file_actions_addclosefrom_np(&actions,7));
    sigset_t defaults,empty;
    sigfillset(&defaults); sigemptyset(&empty);
    SPAWN_CHECK(posix_spawnattr_setsigdefault(&attrs,&defaults));
    SPAWN_CHECK(posix_spawnattr_setsigmask(&attrs,&empty));
    SPAWN_CHECK(posix_spawnattr_setflags(&attrs,POSIX_SPAWN_SETSIGDEF|POSIX_SPAWN_SETSIGMASK));
    error=posix_spawn(&s->supervisor,helper,&actions,&attrs,args,env);
cleanup:
    posix_spawnattr_destroy(&attrs); posix_spawn_file_actions_destroy(&actions);
#undef SPAWN_CHECK
    return error;
}
int bt_session_resize(BtSession *s, unsigned cols, unsigned rows, unsigned cw, unsigned ch) {
    if (s->remote) return bt_remote_resize(s,cols,rows,cw,ch);
    if (!cols || !rows || cols > 1000 || rows > 1000 || !cw || !ch || cw > 512 || ch > 512)
        return fail(s, "invalid terminal dimensions", EINVAL);
    unsigned old_cols=s->cols, old_rows=s->rows, old_cw=s->cell_width, old_ch=s->cell_height;
    struct winsize previous={0};
    struct winsize ws = {.ws_col = cols, .ws_row = rows,
        .ws_xpixel = (unsigned short)(cols * cw > 65535 ? 65535 : cols * cw),
        .ws_ypixel = (unsigned short)(rows * ch > 65535 ? 65535 : rows * ch)};
    /* A failed PTY ioctl must not leave the terminal and graphics wrapper at
     * different sizes. No PTY input is parsed until this operation completes. */
    if (s->master>=0 && (ioctl(s->master,TIOCGWINSZ,&previous)<0 ||
                        ioctl(s->master,TIOCSWINSZ,&ws)<0)) return fail(s,"resize PTY",errno);
    s->cols=cols; s->rows=rows; s->cell_width=cw; s->cell_height=ch;
    if (ghostty_terminal_resize(s->terminal, cols, rows, cw, ch) != GHOSTTY_SUCCESS) {
        s->cols=old_cols; s->rows=old_rows; s->cell_width=old_cw; s->cell_height=old_ch;
        if (s->master>=0) (void)ioctl(s->master,TIOCSWINSZ,&previous);
        return fail(s, "resize terminal", ENOMEM);
    }
    bt_graphics_resize(s->graphics,cols,rows,cw,ch);
    return 0;
}
int bt_session_open(BtSession *s, const char *helper, char *const argv[], char *const env[],
                    unsigned cols, unsigned rows, unsigned cw, unsigned ch) {
    memset(s, 0, sizeof(*s));
    s->master = s->control = s->status = -1;
    s->exit_status = -1;
    if (!argv || !argv[0]) return fail(s, "missing command", EINVAL);
    if (ghostty_terminal_new(NULL, &s->terminal, cols, rows) != GHOSTTY_SUCCESS)
        return fail(s, "create terminal", ENOMEM);
    size_t history = 64u * 1024u * 1024u;
    ghostty_terminal_set(s->terminal, GHOSTTY_TERMINAL_OPT_SCROLLBACK_MAX_BYTES, &history);
    s->graphics=bt_graphics_new(s->terminal);
    if(!s->graphics) return fail(s,"create graphics decoder",ENOMEM);
    bt_graphics_set_reply(s->graphics,graphics_reply,s);
    GhosttyColorRgb bg = {19, 23, 30}, fg = {220, 226, 235};
    ghostty_terminal_set(s->terminal, GHOSTTY_TERMINAL_OPT_COLOR_BACKGROUND, &bg);
    ghostty_terminal_set(s->terminal, GHOSTTY_TERMINAL_OPT_COLOR_FOREGROUND, &fg);
    ghostty_terminal_set(s->terminal, GHOSTTY_TERMINAL_OPT_USERDATA, s);
    ghostty_terminal_set(s->terminal, GHOSTTY_TERMINAL_OPT_WRITE_PTY, (const void *)reply);
    ghostty_terminal_set(s->terminal, GHOSTTY_TERMINAL_OPT_TITLE_CHANGED, (const void *)title);
    ghostty_terminal_set(s->terminal, GHOSTTY_TERMINAL_OPT_CLIPBOARD_WRITE, (const void *)clipboard_write);
    size_t clipboard_limit=BT_CLIPBOARD_LIMIT;
    ghostty_terminal_set(s->terminal,GHOSTTY_TERMINAL_OPT_CLIPBOARD_WRITE_MAX_BYTES,&clipboard_limit);
    ghostty_terminal_set(s->terminal, GHOSTTY_TERMINAL_OPT_SIZE, (const void *)size_query);
    if (bt_session_resize(s, cols, rows, cw, ch)) return -1;
    for(unsigned i=0;env && env[i];++i) {
        const char prefix[]="BATTY_RECOVERY_FILE=";
        if(!strncmp(env[i],prefix,sizeof(prefix)-1) && env[i][sizeof(prefix)-1]) {
            if(bt_recovery_load(s,env[i]+sizeof(prefix)-1)) return fail(s,"restore saved output",errno);
            break;
        }
    }
    int slave = -1, status_pipe[2] = {-1,-1}, control_pipe[2] = {-1,-1};
    int high[4] = {-1,-1,-1,-1}, error = 0;
    struct winsize ws = {.ws_col=cols, .ws_row=rows,
        .ws_xpixel=cols*cw>65535?65535:cols*cw, .ws_ypixel=rows*ch>65535?65535:rows*ch};
    if (openpty(&s->master, &slave, NULL, NULL, &ws) < 0 ||
        pipe2(status_pipe, O_CLOEXEC) < 0 || pipe2(control_pipe, O_CLOEXEC) < 0) { error=errno; goto cleanup; }
    if(fcntl(s->master,F_SETFD,FD_CLOEXEC)<0) { error=errno; goto cleanup; }
    int originals[] = {slave, status_pipe[1], control_pipe[0], s->master};
    for (int i = 0; i < 4; ++i) {
        high[i] = fcntl(originals[i], F_DUPFD_CLOEXEC, 64);
        if (high[i] < 0) { error=errno; goto cleanup; }
    }
    size_t argc = 0;
    while (argv[argc]) ++argc;
    char **args = calloc(argc + 2, sizeof(*args));
    if (!args) { error=ENOMEM; goto cleanup; }
    args[0] = (char *)helper;
    for (size_t i=0; i<argc; ++i) args[i+1] = argv[i];
    error=spawn_helper(s,helper,args,env,high);
    free(args);
    if (error) goto cleanup;
    s->status = status_pipe[0]; status_pipe[0] = -1;
    s->control = control_pipe[1]; control_pipe[1] = -1;
    if(fcntl(s->status,F_SETFL,O_NONBLOCK)<0 || fcntl(s->master,F_SETFL,O_NONBLOCK)<0) error=errno;
cleanup:
    if (slave >= 0) close(slave);
    for (int i=0; i<4; ++i) if (high[i]>=0) close(high[i]);
    for (int i=0; i<2; ++i) {
        if (status_pipe[i]>=0) close(status_pipe[i]);
        if (control_pipe[i]>=0) close(control_pipe[i]);
    }
    if (error) return fail(s, "spawn session supervisor", error);
    uint64_t deadline = bt_millis() + 3000;
    while (!s->ready && !s->exec_error && bt_millis() < deadline) {
        struct pollfd fd = {s->status, POLLIN, 0};
        if (poll(&fd, 1, 20) < 0 && errno != EINTR) return fail(s, "wait for exec", errno);
        if (read_status(s)) return -1;
    }
    if (s->exec_error) return fail(s, argv[0], s->exec_error);
    if (!s->ready) return fail(s, "exec startup timeout", ETIMEDOUT);
    s->recorder=bt_recorder_open(helper,env,s->child,argv);
    if(!s->recorder) s->recorder_error=errno;
    return 0;
}

unsigned bt_session_recording(const BtSession *s) {
    if(s->remote) return s->remote_recording;
    if(s->recorder_error || (s->recorder && s->recorder->error)) return BT_RECORD_FAILED;
    if(!s->recorder) return BT_RECORD_DISABLED;
    if(s->recorder->complete) return BT_RECORD_COMPLETE;
    return s->recorder->ending?BT_RECORD_FINISHING:BT_RECORD_ACTIVE;
}
const char *bt_session_transcript_id(const BtSession *s) {
    if(s->remote) return s->remote_transcript_id[0]?s->remote_transcript_id:NULL;
    return s->recorder && s->recorder->name[0]?s->recorder->name:NULL;
}
const char *bt_session_transcript_directory(const BtSession *s) {
    if(s->remote) return s->remote_transcript_directory;
    return s->recorder && s->recorder->name[0]?s->recorder->directory:NULL;
}
void bt_session_feed(BtSession *s, const void *bytes, size_t length) {
    if(length) s->graphics_next_tick=0;
    bt_recorder_write(s->recorder,bytes,length);
    bt_graphics_feed(s->graphics,bytes,length);
}
int bt_session_reset(BtSession *s) {
    if(s->remote) return bt_remote_reset(s);
    if(!s->terminal) return fail(s,"reset terminal",ENOTCONN);
    BtGraphics *graphics=bt_graphics_new(s->terminal);
    if(!graphics) return fail(s,"reset graphics decoder",ENOMEM);
    bt_graphics_set_reply(graphics,graphics_reply,s);
    bt_graphics_resize(graphics,s->cols,s->rows,s->cell_width,s->cell_height);
    bt_graphics_free(s->graphics); s->graphics=graphics;
    ghostty_terminal_reset(s->terminal);
    s->graphics_next_tick=0; ++s->graphics_revision; s->title_changed=true;
    return 0;
}
static int tick_graphics(BtSession *s) {
    GhosttyKittyGraphics storage=NULL;
    uint64_t before=0, after=0, delay=UINT64_MAX;
    uint64_t now=bt_millis();
    if(now<s->graphics_next_tick) return 0;
    if(ghostty_terminal_get(s->terminal,GHOSTTY_TERMINAL_DATA_KITTY_GRAPHICS,&storage)!=GHOSTTY_SUCCESS ||
       ghostty_kitty_graphics_get(storage,GHOSTTY_KITTY_GRAPHICS_DATA_GENERATION,&before)!=GHOSTTY_SUCCESS ||
       ghostty_kitty_graphics_animation_tick(s->terminal,now,&delay)!=GHOSTTY_SUCCESS ||
       ghostty_terminal_get(s->terminal,GHOSTTY_TERMINAL_DATA_KITTY_GRAPHICS,&storage)!=GHOSTTY_SUCCESS ||
       ghostty_kitty_graphics_get(storage,GHOSTTY_KITTY_GRAPHICS_DATA_GENERATION,&after)!=GHOSTTY_SUCCESS)
        return fail(s,"advance Kitty animation",EIO);
    if(before!=after) ++s->graphics_revision;
    s->graphics_next_tick=delay==UINT64_MAX || delay>UINT64_MAX-now?UINT64_MAX:now+delay;
    return 0;
}
int bt_session_pump(BtSession *s, int timeout_ms) {
    bt_recorder_pump(NULL,false);
    if (s->remote) return bt_remote_pump(s,timeout_ms);
    if (s->error[0]) return -1;
    struct pollfd fds[] = {
        {s->eof ? -1 : s->master, POLLIN | (s->pending_end > s->pending_start ? POLLOUT : 0), 0},
        {s->status, POLLIN, 0}};
    int rc = poll(fds, 2, timeout_ms);
    if (rc < 0 && errno != EINTR) return fail(s, "poll PTY", errno);
    if (read_status(s)) return -1;
    if (!s->eof && fds[0].revents & POLLOUT) {
        size_t budget = BT_READ_BUDGET;
        while (s->pending_start < s->pending_end && budget) {
            size_t len = s->pending_end - s->pending_start;
            if (len > budget) len = budget;
            ssize_t n = write(s->master, s->pending + s->pending_start, len);
            if (n > 0) { s->pending_start += n; s->bytes_written += n; budget -= n; }
            else if (n < 0 && errno == EINTR) continue;
            else if (n == 0 || errno == EAGAIN || errno == EWOULDBLOCK) break;
            else if (errno == EIO) { s->eof = true; break; }
            else return fail(s, "write PTY", errno);
        }
        if (s->pending_start == s->pending_end) s->pending_start = s->pending_end = 0;
    }
    size_t total = 0;
    uint64_t start = bt_millis();
    while (!s->eof && total < BT_READ_BUDGET && bt_millis() - start < 4) {
        uint8_t bytes[16384];
        size_t remaining=BT_READ_BUDGET-total;
        ssize_t n = read(s->master, bytes, remaining<sizeof(bytes)?remaining:sizeof(bytes));
        if (n > 0) {
            bt_session_feed(s,bytes,(size_t)n);
            total += n; s->bytes_read += n;
            if (s->error[0]) return -1;
        } else if (n == 0 || (n < 0 && errno == EIO)) { s->eof = true; }
        else if (errno == EINTR) continue;
        else if (errno == EAGAIN || errno == EWOULDBLOCK) break;
        else return fail(s, "read PTY", errno);
    }
    if(tick_graphics(s)) return -1;
    bt_recorder_pump(s->recorder,s->eof);
    if (s->exited && s->eof) s->done = true;
    return 0;
}

char *bt_session_text(BtSession *s, bool selection, size_t *length) {
    if (s->remote) return bt_remote_text(s,selection,length);
    GhosttyFormatter f = NULL;
    GhosttyFormatterTerminalOptions options = GHOSTTY_INIT_SIZED(GhosttyFormatterTerminalOptions);
    options.emit = GHOSTTY_FORMATTER_FORMAT_PLAIN;
    options.trim = true; options.unwrap = selection;
    GhosttySelection sel = GHOSTTY_INIT_SIZED(GhosttySelection);
    if (selection) {
        if (ghostty_terminal_get(s->terminal, GHOSTTY_TERMINAL_DATA_SELECTION, &sel) != GHOSTTY_SUCCESS) return NULL;
        options.selection = &sel;
    }
    if (ghostty_formatter_terminal_new(NULL, &f, s->terminal, options) != GHOSTTY_SUCCESS) return NULL;
    size_t len = 0;
    GhosttyResult result = ghostty_formatter_format_buf(f, NULL, 0, &len);
    char *out = NULL;
    if ((result == GHOSTTY_OUT_OF_SPACE || result == GHOSTTY_SUCCESS) && len < 128u*1024u*1024u) {
        out = malloc(len+1);
        if (out && ghostty_formatter_format_buf(f, (uint8_t *)out, len, &len) != GHOSTTY_SUCCESS) { free(out); out=NULL; }
        if (out) { out[len]=0; *length=len; }
    }
    ghostty_formatter_free(f);
    return out;
}
void bt_session_close(BtSession *s) {
    free(s->recovery_arguments); s->recovery_arguments=NULL;
    free(s->recovery_argv); s->recovery_argv=NULL;
    bt_session_clipboard_clear(s);
    bt_recorder_close(s->recorder); s->recorder=NULL;
    if (s->remote) { bt_remote_close(s); return; }
    if (s->control >= 0) { close(s->control); s->control = -1; }
    if (s->master >= 0) { close(s->master); s->master = -1; }
    if (s->status >= 0) {
        uint64_t deadline = bt_millis()+1200;
        while (bt_millis() < deadline) {
            uint8_t bytes[128];
            ssize_t n = read(s->status, bytes, sizeof(bytes));
            if (n == 0) break;
            if (n < 0 && errno != EAGAIN && errno != EINTR) break;
            struct pollfd fd = {s->status, POLLIN|POLLHUP, 0};
            poll(&fd, 1, 20);
        }
        close(s->status); s->status = -1;
    }
    if (s->supervisor > 0) {
        uint64_t deadline=bt_millis()+200;
        while(waitpid(s->supervisor,NULL,WNOHANG)==0 && bt_millis()<deadline) {
            struct timespec delay={0,1000000}; nanosleep(&delay,NULL);
        }
        s->supervisor=0;
    }
    bt_graphics_free(s->graphics); s->graphics=NULL;
    if (s->terminal) { ghostty_terminal_free(s->terminal); s->terminal = NULL; }
    free(s->pending); s->pending=NULL;
}
const char *bt_session_title(BtSession *s) {
    if (s->remote) return s->title_cache;
    GhosttyString value;
    if (s->terminal && ghostty_terminal_get(s->terminal,GHOSTTY_TERMINAL_DATA_TITLE,&value)==GHOSTTY_SUCCESS) {
        size_t n=value.len<sizeof(s->title_cache)-1?value.len:sizeof(s->title_cache)-1;
        if (n) memcpy(s->title_cache,value.ptr,n);
        s->title_cache[n]=0;
    }
    return s->title_cache;
}
