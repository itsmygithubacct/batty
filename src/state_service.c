/* SPDX-License-Identifier: MIT */
/* Native owner of one PTY, terminal and graphics decoder. Windows receive
 * complete owned presentations; they never parse/replay this PTY stream. */
#define _GNU_SOURCE
#include "remote_internal.h"
#include "presentation.h"
#include "recovery.h"
#include "selection_drag.h"
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

#define PEERS 24
#define OBSERVERS 8
#define USER_INPUT_LIMIT (7u * 1024u * 1024u)
typedef struct {
    int fd, role, output_fd;
    bool output, waiting, clipboard_policy;
    uint64_t last_request, deadline, activity;
    BtPacket packet;
} Peer;
typedef struct {
    BtSession session;
    char **argv;
    BtPresenter *presenter;
    GhosttyKeyEncoder keys;
    GhosttyKeyEvent key_event;
    GhosttyMouseEncoder mouse;
    GhosttyMouseEvent mouse_event;
    BtSelectionDrag selection;
    Peer peers[PEERS];
    int listener, root, frame_fd, delta_fd, stopping_peer, clipboard_owner;
    BtPresentation *current;
    uint64_t delta_base;
    size_t delta_size;
    uint64_t epoch, revision, frame_size;
    unsigned frame_cols, frame_rows, frame_cw, frame_ch;
    bool selecting, focused, focus_pending, capture_retry, claimed;
} Service;
static volatile sig_atomic_t stopped;
static void stop_signal(int number) { (void)number; stopped=1; }
static bool mode(Service *s, GhosttyMode value) {
    GhosttyTerminalModeConfig c={.mode=value};
    return ghostty_terminal_get(s->session.terminal,GHOSTTY_TERMINAL_DATA_MODE,&c)==GHOSTTY_SUCCESS && c.value;
}
static void scroll(Service *s, int delta, bool bottom) {
    GhosttyTerminalScrollViewport v={.tag=bottom?GHOSTTY_SCROLL_VIEWPORT_BOTTOM:GHOSTTY_SCROLL_VIEWPORT_DELTA,
                                     .value.delta=delta};
    ghostty_terminal_scroll_viewport(s->session.terminal,v);
}
static int focus(Service *s, bool focused) {
    if(!focused) {
        s->selecting=false;
        bt_selection_drag_reset(&s->selection);
    }
    if (s->focused==focused && !s->focus_pending) return 0;
    s->focused=focused;
    s->focus_pending=false;
    if (s->session.eof || !mode(s,GHOSTTY_MODE_FOCUS_EVENT)) return 0;
    char bytes[8]; size_t n=0;
    if (ghostty_focus_encode(focused?GHOSTTY_FOCUS_GAINED:GHOSTTY_FOCUS_LOST,
                            bytes,sizeof(bytes),&n)!=GHOSTTY_SUCCESS) { errno=EINVAL; return -1; }
    char previous[sizeof(s->session.error)]; memcpy(previous,s->session.error,sizeof(previous));
    if (bt_session_send(&s->session,bytes,n)) {
        s->focus_pending=true;
        memcpy(s->session.error,previous,sizeof(previous));
    }
    return 0;
}
static int input_send(Service *s, const void *bytes, size_t length) {
    size_t pending=s->session.pending_end-s->session.pending_start;
    /* Preserve one MiB of the native queue for terminal replies/control. */
    if (pending>USER_INPUT_LIMIT || length>USER_INPUT_LIMIT-pending || s->focus_pending) {
        errno=ENOBUFS; return -1;
    }
    return bt_session_send(&s->session,bytes,length);
}
static int clipboard_policy(Service *s, Peer *peer, bool enabled) {
    int index=(int)(peer-s->peers), previous=s->clipboard_owner;
    peer->clipboard_policy=enabled;
    if(enabled) s->clipboard_owner=index;
    else if(previous==index) {
        s->clipboard_owner=-1;
        for(unsigned i=0;i<PEERS;++i)
            if(s->peers[i].fd>=0 && s->peers[i].clipboard_policy && (s->peers[i].role==1 || s->peers[i].role==3)) {
                s->clipboard_owner=(int)i; break;
            }
    }
    if(previous!=s->clipboard_owner) bt_session_clipboard_clear(&s->session);
    return bt_session_clipboard_policy(&s->session,s->clipboard_owner>=0);
}
static void peer_close(Service *s, Peer *p) {
    if(p->clipboard_policy) (void)clipboard_policy(s,p,false);
    if (p->role==1) (void)focus(s,false);
    if (p->role==1 || p->role==3) {
        s->selecting=false;
        bt_selection_drag_reset(&s->selection);
    }
    if (p->fd>=0) close(p->fd);
    if (p->output_fd>=0) close(p->output_fd);
    memset(p,0,sizeof(*p)); p->fd=p->output_fd=-1;
}
static void counts(Service *s, unsigned *controllers, unsigned *observers) {
    *controllers=*observers=0;
    for (unsigned i=0;i<PEERS;++i) if (s->peers[i].fd>=0) {
        *controllers+=s->peers[i].role==1; *observers+=s->peers[i].role==2 || s->peers[i].role==3;
    }
}
static bool input_peer(Service *s) {
    for (unsigned i=0;i<PEERS;++i) if(s->peers[i].fd>=0 && s->peers[i].role==3) return true;
    return false;
}
static void metadata(Service *s, BtPacket *p) {
    BtSession *t=&s->session;
    p->epoch=s->epoch; p->revision=s->revision;
    p->cols=t->cols; p->rows=t->rows; p->cw=t->cell_width; p->ch=t->cell_height;
    p->child=t->child; p->exit_status=(uint32_t)t->exit_status;
    p->bytes_read=t->bytes_read; p->bytes_written=t->bytes_written;
    p->pending=t->pending_end-t->pending_start;
    p->state=(t->exited?BT_STATE_EXITED:0)|(t->eof?BT_STATE_EOF:0)|(t->done?BT_STATE_DONE:0)|
        BT_STATE_CLIPBOARD_SUPPORTED|BT_STATE_DELTA_SUPPORTED|(t->clipboard?BT_STATE_CLIPBOARD_PENDING:0);
    p->state|=bt_session_recording(t)<<BT_STATE_RECORD_SHIFT;
    counts(s,&p->controllers,&p->observers);
}
static void queue(Service *s, Peer *peer, const BtPacket *request, int error, int fd, uint64_t size) {
    uint32_t type=request->type; uint64_t id=request->request;
    bt_wire_packet(&peer->packet,type); peer->packet.request=id;
    metadata(s,&peer->packet);
    if(type==BT_WIRE_HELLO && s->session.recorder && s->session.recorder->name[0]) {
        peer->packet.length=32;
        memcpy(peer->packet.data,s->session.recorder->name,32);
        const char *directory=s->session.recorder->directory;
        if(directory) {
            size_t n=strlen(directory);
            if(n && n+34<=BT_WIRE_CHUNK) {
                peer->packet.data[32]=0;
                memcpy(peer->packet.data+33,directory,n+1);
                peer->packet.length=(uint32_t)(n+34);
            }
        }
    }
    if((int)(peer-s->peers)!=s->clipboard_owner &&
       !(peer->role==2 && s->clipboard_owner>=0 && s->peers[s->clipboard_owner].role==3))
        peer->packet.state&=~BT_STATE_CLIPBOARD_PENDING;
    peer->packet.error=error; peer->packet.blob_size=size;
    peer->output_fd=fd; peer->output=true; peer->waiting=false;
    peer->deadline=bt_millis()+3000;
}
static void flush(Service *s, Peer *p) {
    if (!p->output) return;
    if (bt_wire_send(p->fd,&p->packet,p->output_fd)==0) {
        if (p->output_fd>=0) close(p->output_fd);
        p->output_fd=-1; p->output=false; p->activity=bt_millis();
    } else if (errno!=EAGAIN && errno!=EWOULDBLOCK && errno!=EINTR) peer_close(s,p);
}
static int frame(Service *s, bool force) {
    BtPresentation *view=NULL;
    /* Retain one immutable current snapshot. Unchanged image generations share
     * the presenter's bounded pixel owners; no per-peer frame history exists. */
    int rc=bt_presenter_capture(s->presenter,s->epoch,s->revision+1,
        s->session.cell_width,s->session.cell_height,force || s->capture_retry || !s->current,&view);
    if(rc) {
        if(rc<0) s->capture_retry=true;
        return rc<0?-1:0;
    }
    int delta=-1; size_t delta_size=0;
    if(s->current && view->image_count) {
        delta=bt_presentation_pack_delta_fd(view,s->current,&delta_size);
        if(delta<0) {
            int error=errno; bt_presentation_free(view); s->capture_retry=true; errno=error; return -1;
        }
    }
    if(s->frame_fd>=0) close(s->frame_fd);
    if(s->delta_fd>=0) close(s->delta_fd);
    s->frame_fd=-1; s->frame_size=0;
    s->delta_fd=delta; s->delta_size=delta_size; s->delta_base=s->revision;
    bt_presentation_free(s->current); s->current=view;
    s->revision=view->revision;
    s->frame_cols=view->cols; s->frame_rows=view->rows;
    s->frame_cw=view->cell_width; s->frame_ch=view->cell_height;
    s->capture_retry=false;
    return 0;
}
static void frame_reply(Service *s, Peer *peer, const BtPacket *request) {
    int fd=-1, error=0; size_t size=0; bool delta=false;
    if (request->epoch!=s->epoch || request->revision!=s->revision) {
        delta=request->type==BT_WIRE_FRAME && (request->state&BT_STATE_DELTA_SUPPORTED) &&
              request->epoch==s->epoch && request->revision==s->delta_base && s->delta_fd>=0;
        int source=s->delta_fd;
        if(!delta) {
            /* Reconnecting, legacy and lagging clients receive a complete
             * snapshot. Materialize it once, only when actually requested. */
            if(s->frame_fd<0) {
                size_t length=0;
                s->frame_fd=bt_presentation_pack_fd(s->current,&length);
                s->frame_size=length;
            }
            source=s->frame_fd;
        }
        if(source<0) error=errno;
        else {
            fd=fcntl(source,F_DUPFD_CLOEXEC,4);
            if(fd<0) error=errno;
            else size=delta?s->delta_size:s->frame_size;
        }
    }
    queue(s,peer,request,error,fd,size);
    if(delta && fd>=0) peer->packet.state|=BT_STATE_DELTA_FRAME;
    /* Geometry always describes exactly the supplied/retained frame. */
    peer->packet.cols=s->frame_cols; peer->packet.rows=s->frame_rows;
    peer->packet.cw=s->frame_cw; peer->packet.ch=s->frame_ch;
}
static bool utf8(const uint8_t *text, size_t length) {
    size_t i=0;
    while (i<length) {
        uint32_t c=text[i++], minimum; unsigned more;
        if (c<0x80) continue;
        if (c>=0xc2 && c<=0xdf) { c&=31; more=1; minimum=0x80; }
        else if (c>=0xe0 && c<=0xef) { c&=15; more=2; minimum=0x800; }
        else if (c>=0xf0 && c<=0xf4) { c&=7; more=3; minimum=0x10000; }
        else return false;
        if (more>length-i) return false;
        while (more--) { if ((text[i]&0xc0)!=0x80) return false; c=(c<<6)|(text[i++]&63); }
        if (c<minimum || c>0x10ffff || (c>=0xd800 && c<=0xdfff)) return false;
    }
    return true;
}
static int key_input(Service *s, const BtIntent *in, const void *text, size_t length) {
    if (in->action>GHOSTTY_KEY_ACTION_REPEAT || in->key>GHOSTTY_KEY_PASTE ||
        in->mods>1023 || in->consumed>1023 || in->composing>1 || in->codepoint>0x10ffff ||
        (in->codepoint>=0xd800 && in->codepoint<=0xdfff) || length>1024 || !utf8(text,length)) {
        errno=EINVAL; return -1;
    }
    ghostty_key_encoder_setopt_from_terminal(s->keys,s->session.terminal);
    ghostty_key_event_set_action(s->key_event,in->action);
    ghostty_key_event_set_key(s->key_event,in->key);
    ghostty_key_event_set_mods(s->key_event,in->mods);
    ghostty_key_event_set_consumed_mods(s->key_event,in->consumed);
    ghostty_key_event_set_composing(s->key_event,in->composing!=0);
    ghostty_key_event_set_unshifted_codepoint(s->key_event,in->codepoint);
    ghostty_key_event_set_utf8(s->key_event,text,length);
    char bytes[8192]; size_t n=0;
    if (ghostty_key_encoder_encode(s->keys,s->key_event,bytes,sizeof(bytes),&n)!=GHOSTTY_SUCCESS) {
        errno=EINVAL; return -1;
    }
    if (n && input_send(s,bytes,n)) return -1;
    if (n) scroll(s,0,true);
    return 0;
}
static int mouse_input(Service *s, const BtIntent *in) {
    ghostty_mouse_encoder_setopt_from_terminal(s->mouse,s->session.terminal);
    GhosttyMouseEncoderSize size=GHOSTTY_INIT_SIZED(GhosttyMouseEncoderSize);
    size.screen_width=s->session.cols*s->session.cell_width;
    size.screen_height=s->session.rows*s->session.cell_height;
    size.cell_width=s->session.cell_width; size.cell_height=s->session.cell_height;
    bool pressed=in->buttons!=0;
    ghostty_mouse_encoder_setopt(s->mouse,GHOSTTY_MOUSE_ENCODER_OPT_SIZE,&size);
    ghostty_mouse_encoder_setopt(s->mouse,GHOSTTY_MOUSE_ENCODER_OPT_ANY_BUTTON_PRESSED,&pressed);
    ghostty_mouse_event_set_action(s->mouse_event,in->action);
    ghostty_mouse_event_set_button(s->mouse_event,in->button);
    ghostty_mouse_event_set_mods(s->mouse_event,in->mods);
    ghostty_mouse_event_set_position(s->mouse_event,(GhosttyMousePosition){in->x,in->y});
    char bytes[128]; size_t n=0;
    if (ghostty_mouse_encoder_encode(s->mouse,s->mouse_event,bytes,sizeof(bytes),&n)!=GHOSTTY_SUCCESS) {
        errno=EINVAL; return -1;
    }
    return n?input_send(s,bytes,n):0;
}
static int pointer_input(Service *s, const BtIntent *in) {
    if (in->action>GHOSTTY_MOUSE_ACTION_MOTION || in->button>GHOSTTY_MOUSE_BUTTON_ELEVEN ||
        in->mods>1023 || in->x < -1000000 || in->x>1000000 || in->y < -1000000 || in->y>1000000) {
        errno=EINVAL; return -1;
    }
    GhosttyMouseTrackingMode tracking=GHOSTTY_MOUSE_TRACKING_NONE;
    if (!s->session.eof)
        ghostty_terminal_get(s->session.terminal,GHOSTTY_TERMINAL_DATA_MOUSE_TRACKING,&tracking);
    if (in->type==BT_INTENT_WHEEL) {
        if (in->delta < -20 || in->delta>20) { errno=EINVAL; return -1; }
        if (tracking!=GHOSTTY_MOUSE_TRACKING_NONE && !(in->mods&GHOSTTY_MODS_SHIFT)) {
            BtIntent wheel=*in; wheel.action=GHOSTTY_MOUSE_ACTION_PRESS;
            wheel.button=in->delta>0?GHOSTTY_MOUSE_BUTTON_FOUR:GHOSTTY_MOUSE_BUTTON_FIVE;
            for (int i=0;i<abs(in->delta);++i) if (mouse_input(s,&wheel)) return -1;
        } else if (!s->session.eof && mode(s,GHOSTTY_MODE_ALT_SCROLL) &&
                   (mode(s,GHOSTTY_MODE_ALT_SCREEN_SAVE)||mode(s,GHOSTTY_MODE_ALT_SCREEN))) {
            BtIntent arrow={.action=GHOSTTY_KEY_ACTION_PRESS,
                .key=in->delta>0?GHOSTTY_KEY_ARROW_UP:GHOSTTY_KEY_ARROW_DOWN};
            for (int i=0;i<abs(in->delta)*3;++i) if (key_input(s,&arrow,NULL,0)) return -1;
        } else scroll(s,-in->delta*3,false);
        return 0;
    }
    if (!s->selecting && tracking!=GHOSTTY_MOUSE_TRACKING_NONE && !(in->mods&GHOSTTY_MODS_SHIFT))
        return mouse_input(s,in);
    bool begin=in->button==GHOSTTY_MOUSE_BUTTON_LEFT && in->action==GHOSTTY_MOUSE_ACTION_PRESS;
    bool end=in->button==GHOSTTY_MOUSE_BUTTON_LEFT && in->action==GHOSTTY_MOUSE_ACTION_RELEASE;
    if (!begin && !s->selecting) return 0;
    int changed=bt_selection_drag_event(&s->selection,s->session.terminal,
        s->session.cols,s->session.rows,s->session.cell_width,s->session.cell_height,0,
        in->x,in->y,begin,end,bt_millis());
    if(changed<0) { errno=ENOMEM; return -1; }
    s->selecting=!end && s->selection.active;
    return 0;
}
static int intent(Service *s, const BtIntent *in, const void *data, size_t length) {
    if (in->type!=BT_INTENT_KEY && in->type!=BT_INTENT_PASTE && length) { errno=EINVAL; return -1; }
    if (in->type==BT_INTENT_KEY) return key_input(s,in,data,length);
    if (in->type==BT_INTENT_PASTE) {
        if (length>4u*1024u*1024u) { errno=E2BIG; return -1; }
        char *encoded=malloc(length*2+64), *copy=malloc(length+1);
        if (!encoded || !copy) { free(encoded); free(copy); return -1; }
        if (length) memcpy(copy,data,length);
        copy[length]=0;
        size_t n=0;
        GhosttyResult result=ghostty_paste_encode(copy,length,mode(s,GHOSTTY_MODE_BRACKETED_PASTE),
                                                  encoded,length*2+64,&n);
        int rc;
        if (result!=GHOSTTY_SUCCESS) { errno=EINVAL; rc=-1; }
        else rc=input_send(s,encoded,n);
        free(encoded); free(copy); if (!rc) scroll(s,0,true); return rc;
    }
    if (in->type==BT_INTENT_POINTER || in->type==BT_INTENT_WHEEL) return pointer_input(s,in);
    if (in->type==BT_INTENT_FOCUS && in->focused<=1) return focus(s,in->focused);
    if (in->type==BT_INTENT_SCROLL && in->delta>=-1000 && in->delta<=1000) {
        scroll(s,in->delta,false); return 0;
    }
    errno=EINVAL; return -1;
}
static void request(Service *s, Peer *peer, const BtPacket *p, int received) {
    int error=0, output=-1; uint64_t output_size=0;
    bool recovery_failed=false;
    void *bytes=NULL;
    if (!p->request || p->request!=peer->last_request+1 || peer->waiting || peer->output) {
        if (received>=0) close(received);
        peer_close(s,peer); return;
    }
    peer->last_request=p->request; peer->activity=bt_millis();
    if (p->blob_size || received>=0) {
        if ((p->type!=BT_WIRE_SEND && p->type!=BT_WIRE_INTENT) || received<0 || !p->blob_size)
            error=EINVAL;
        else {
            bytes=bt_wire_map(received,p->blob_size,8u*1024u*1024u);
            if (bytes==MAP_FAILED) { bytes=NULL; error=errno; }
        }
    }
    if (received>=0) close(received);
    if (p->type!=BT_WIRE_INTENT && p->length) error=EINVAL;
    if (error) goto finish;
    if (p->type==BT_WIRE_HELLO) {
        unsigned control, observe; counts(s,&control,&observe);
        if (peer->role || (p->flags!=1 && p->flags!=2 && p->flags!=3)) error=EINVAL;
        else if (p->epoch && p->epoch!=s->epoch) error=ESTALE;
        else if ((p->flags==1 && control) ||
                 (p->flags!=1 && (observe>=OBSERVERS || (p->flags==3 && input_peer(s))))) error=EBUSY;
        else {
            peer->role=p->flags;
            if (frame(s,true)) { error=errno; peer->role=0; }
            else {
                frame_reply(s,peer,p);
                if (!peer->packet.error) s->claimed=true;
                return;
            }
        }
    } else if (p->type==BT_WIRE_STATUS) {
        /* Metadata is available without claiming the controller role. */
    } else if (p->type==BT_WIRE_STOP) {
        unsigned control, observe; counts(s,&control,&observe);
        if (peer->role==2 || peer->role==3) error=EPERM;
        else if (p->epoch && p->epoch!=s->epoch) error=ESTALE;
        else if (p->epoch && (s->claimed || control || observe)) error=EBUSY;
        else {
            if(!p->epoch && bt_recovery_forget(getenv("BATTY_KILIX_RECOVERY_DIR"),s->epoch)) {
                error=errno; recovery_failed=true; goto finish;
            }
            s->stopping_peer=(int)(peer-s->peers); peer->packet=*p; stopped=1; return;
        }
    } else if (!peer->role) error=EACCES;
    else if (p->type==BT_WIRE_FRAME) {
        if (p->flags>100) error=EINVAL;
        else {
            peer->packet=*p; peer->waiting=true; peer->deadline=bt_millis()+p->flags;
            return;
        }
    } else if (p->type==BT_WIRE_TEXT) {
        size_t n=0; char *text=bt_session_text(&s->session,p->flags!=0,&n);
        if (!text && p->flags) { text=strdup(""); n=0; }
        if (!text) error=errno?errno:ENOMEM;
        else {
            if (n>BT_PRESENTATION_MAX_BYTES) error=E2BIG;
            else if (n) { output=bt_wire_blob(text,n); if (output<0) error=errno; else output_size=n; }
            free(text);
        }
    } else if(p->type==BT_WIRE_RECOVERY) {
        uint8_t *archive=NULL; size_t length=0;
        if(p->flags || frame(s,false) || bt_recovery_capture(&s->session,s->current,s->argv,&archive,&length)) error=errno?errno:EINVAL;
        else { output=bt_wire_blob(archive,length); if(output<0) error=errno; else output_size=length; }
        free(archive);
    } else if (peer->role!=1 && peer->role!=3) error=EPERM;
    else if(p->type==BT_WIRE_CLIPBOARD) {
        if((int)(peer-s->peers)!=s->clipboard_owner) error=EPERM;
        else if(s->session.clipboard) {
            output_size=s->session.clipboard_length+1;
            output=bt_wire_blob(s->session.clipboard,output_size);
            if(output<0) error=errno;
            else bt_session_clipboard_clear(&s->session);
        }
    }
    else if (p->type==BT_WIRE_SEND) {
        if (input_send(s,bytes,p->blob_size)) error=errno;
    } else if (p->type==BT_WIRE_RESIZE) {
        if (bt_session_resize(&s->session,p->cols,p->rows,p->cw,p->ch)) error=errno;
    } else if (p->type==BT_WIRE_RESET) {
        if(bt_session_reset(&s->session)) error=errno;
    } else if (p->type==BT_WIRE_INTENT) {
        if (p->length!=sizeof(BtIntent)) error=EINVAL;
        else {
            BtIntent in; memcpy(&in,p->data,sizeof(in));
            if(peer->role==3 && in.type==BT_INTENT_FOCUS) error=EPERM;
            else if(in.type==BT_INTENT_CLIPBOARD_POLICY && in.focused<=1 && !p->blob_size) {
                if(clipboard_policy(s,peer,in.focused!=0)) error=errno;
            }
            else if (intent(s,&in,bytes,p->blob_size)) error=errno;
        }
    } else error=EINVAL;
finish:
    if (bytes) munmap(bytes,p->blob_size);
    /* Rejected client operations do not poison the authoritative terminal. */
    if (error) s->session.error[0]=0;
    queue(s,peer,p,error,output,output_size);
    if(recovery_failed) peer->packet.flags=BT_STOP_RECOVERY_FAILED;
}
static int bootstrap(Service *s) {
    BtPacket p; bt_wire_packet(&p,BT_WIRE_READY); metadata(s,&p);
    if (bt_wire_send(3,&p,-1)) return -1;
    uint64_t deadline=bt_millis()+3000;
    for (;;) {
        if (bt_session_pump(&s->session,0)) return -1;
        int fd=-1, rc=bt_wire_receive(3,&p,&fd);
        if (fd>=0) close(fd);
        if (rc==1) {
            if (fd>=0 || p.type!=BT_WIRE_READY || p.request!=1 || p.epoch!=s->epoch || p.length || p.blob_size) {
                errno=EPROTO; return -1;
            }
            bt_wire_packet(&p,BT_WIRE_READY); p.request=1; metadata(s,&p);
            return bt_wire_send(3,&p,-1);
        }
        if (!rc) { errno=ECANCELED; return -1; }
        if (errno!=EAGAIN && errno!=EWOULDBLOCK && errno!=EINTR) return -1;
        if (bt_millis()>=deadline || stopped) { errno=ETIMEDOUT; return -1; }
        struct pollfd wait={3,POLLIN,0}; (void)poll(&wait,1,5);
    }
}
static int run(Service *s) {
    while (!stopped) {
        struct pollfd fds[PEERS+3]={{s->listener,POLLIN,0},
            {s->session.eof?-1:s->session.master,POLLIN|(s->session.pending_end>s->session.pending_start?POLLOUT:0),0},
            {s->session.status,POLLIN,0}};
        int timeout=10;
        for (unsigned i=0;i<PEERS;++i) {
            Peer *p=&s->peers[i];
            fds[i+3]=(struct pollfd){p->fd,p->output?POLLOUT:POLLIN,0};
            if (p->waiting) {
                uint64_t now=bt_millis(); int left=p->deadline>now?(int)(p->deadline-now):0;
                if (left<timeout) timeout=left;
            }
        }
        int rc=poll(fds,PEERS+3,timeout);
        if (rc<0 && errno!=EINTR) return -1;
        if (bt_session_pump(&s->session,0)) return -1;
        if(s->selection.active) {
            int changed=bt_selection_drag_tick(&s->selection,s->session.terminal,
                s->session.cols,s->session.rows,s->session.cell_width,s->session.cell_height,0,bt_millis());
            if(changed<0) { errno=ENOMEM; return -1; }
            s->selecting=s->selection.active;
        }
        if (s->focus_pending) (void)focus(s,s->focused);
        for (unsigned i=0;i<PEERS;++i) {
            Peer *p=&s->peers[i];
            if (p->fd<0) continue;
            if (fds[i+3].revents&(POLLHUP|POLLERR|POLLNVAL)) { peer_close(s,p); continue; }
            if (p->output && fds[i+3].revents&POLLOUT) flush(s,p);
            if (p->fd>=0 && !p->output && !p->waiting && fds[i+3].revents&POLLIN) {
                BtPacket packet; int fd=-1;
                int n=bt_wire_receive(p->fd,&packet,&fd);
                if (n==1) request(s,p,&packet,fd);
                else if (!n || (errno!=EAGAIN && errno!=EWOULDBLOCK && errno!=EINTR)) peer_close(s,p);
            }
            if (stopped) break;
            if (p->fd>=0 && ((!p->role && !p->output && bt_millis()-p->activity>3000) ||
                             (p->output && bt_millis()>p->deadline))) peer_close(s,p);
        }
        if (stopped) break;
        bool waiting=false;
        for (unsigned i=0;i<PEERS;++i) if (s->peers[i].fd>=0 && s->peers[i].waiting) waiting=true;
        if (waiting) {
            int error=frame(s,false)?errno:0;
            for (unsigned i=0;i<PEERS;++i) {
                Peer *p=&s->peers[i];
                if (p->fd<0 || !p->waiting) continue;
                BtPacket req=p->packet;
                if (error) queue(s,p,&req,error,-1,0);
                else if (req.epoch!=s->epoch || req.revision!=s->revision || bt_millis()>=p->deadline ||
                         (p->role==1 && s->session.clipboard))
                    frame_reply(s,p,&req);
            }
        }
        if (fds[0].revents&POLLIN) {
            /* Accept a bounded number so a connection burst cannot starve PTY I/O. */
            for (unsigned n=0;n<4;++n) {
                int fd=accept4(s->listener,NULL,NULL,SOCK_CLOEXEC|SOCK_NONBLOCK);
                if (fd<0) break;
                unsigned i=0; while (i<PEERS && s->peers[i].fd>=0) ++i;
                if (i==PEERS || bt_wire_peer(fd)) { close(fd); continue; }
                Peer *p=&s->peers[i]; p->fd=fd; p->activity=bt_millis();
            }
        }
        for (unsigned i=0;i<PEERS;++i) if (s->peers[i].fd>=0) flush(s,&s->peers[i]);
    }
    return 0;
}
static bool number(const char *text, unsigned maximum, unsigned *out) {
    char *end; errno=0; unsigned long n=strtoul(text,&end,10);
    if (errno || !*text || *end || !n || n>maximum) return false;
    *out=(unsigned)n; return true;
}
int main(int argc, char **argv) {
    if (argc<10 || strcmp(argv[8],"--") || bt_wire_peer(3)) return 2;
    fcntl(3,F_SETFD,FD_CLOEXEC);
    struct sigaction action={.sa_handler=stop_signal}; sigemptyset(&action.sa_mask);
    sigaction(SIGTERM,&action,NULL); sigaction(SIGINT,&action,NULL); sigaction(SIGHUP,&action,NULL);
    signal(SIGPIPE,SIG_IGN);
    Service *s=calloc(1,sizeof(*s));
    if (!s) return 1;
    s->listener=s->root=s->frame_fd=s->delta_fd=s->stopping_peer=-1;
    s->clipboard_owner=-1;
    s->session.master=s->session.control=s->session.status=-1;
    for (unsigned i=0;i<PEERS;++i) s->peers[i].fd=s->peers[i].output_fd=-1;
    int error=0; bool owned=false, committed=false;
    struct stat identity={0}; char leaf[64]={0};
    unsigned cols,rows,cw,ch;
    if (!bt_wire_name(argv[2]) || !number(argv[4],1000,&cols) || !number(argv[5],1000,&rows) ||
        !number(argv[6],512,&cw) || !number(argv[7],512,&ch)) { error=EINVAL; goto done; }
    if (getrandom(&s->epoch,sizeof(s->epoch),0)!=(ssize_t)sizeof(s->epoch) || !s->epoch) { error=EIO; goto done; }
    s->root=bt_wire_root(argv[1],false);
    if (s->root<0) { error=errno; goto done; }
    snprintf(leaf,sizeof(leaf),"%s.sock",argv[2]);
    struct stat existing;
    if (fstatat(s->root,leaf,&existing,AT_SYMLINK_NOFOLLOW)==0) {
        error=S_ISSOCK(existing.st_mode) && existing.st_uid==geteuid() &&
              (existing.st_mode&0777)==0600?EEXIST:EPERM;
        goto done;
    }
    if (errno!=ENOENT) { error=errno; goto done; }
    struct sockaddr_un address={.sun_family=AF_UNIX};
    if (bt_wire_address(s->root,argv[2],address.sun_path,sizeof(address.sun_path))) { error=errno; goto done; }
    s->listener=socket(AF_UNIX,SOCK_SEQPACKET|SOCK_CLOEXEC|SOCK_NONBLOCK,0);
    if (s->listener<0) { error=errno; goto done; }
    mode_t mask=umask(0077);
    int bound=bind(s->listener,(struct sockaddr *)&address,sizeof(address));
    error=errno; umask(mask);
    if (bound<0) goto done;
    owned=true;
    if (fstatat(s->root,leaf,&identity,AT_SYMLINK_NOFOLLOW)<0 ||
        fchmodat(s->root,leaf,0600,0)<0 || listen(s->listener,16)<0) { error=errno; goto done; }
    extern char **environ;
    s->argv=argv+9;
    if (bt_session_open(&s->session,argv[3],argv+9,environ,cols,rows,cw,ch)) { error=errno; goto done; }
    s->presenter=bt_presenter_new(s->session.terminal);
    if (!s->presenter || ghostty_key_encoder_new(NULL,&s->keys)!=GHOSTTY_SUCCESS ||
        ghostty_key_event_new(NULL,&s->key_event)!=GHOSTTY_SUCCESS ||
        ghostty_mouse_encoder_new(NULL,&s->mouse)!=GHOSTTY_SUCCESS ||
        ghostty_mouse_event_new(NULL,&s->mouse_event)!=GHOSTTY_SUCCESS) { error=ENOMEM; goto done; }
    if (bootstrap(s)) { error=errno; goto done; }
    close(3); committed=true;
    error=run(s)?errno:0;
done:
    if (s->listener>=0) close(s->listener);
    /* Closing the lifetime pipe triggers the supervisor's descendant-aware
     * shutdown. Wait for that supervisor before acknowledging termination. */
    pid_t supervisor=s->session.supervisor;
    bt_presenter_free(s->presenter);
    bt_selection_drag_reset(&s->selection);
    bt_session_close(&s->session);
    if (supervisor>0) {
        for (;;) {
            pid_t waited=waitpid(supervisor,NULL,0);
            if (waited==supervisor || (waited<0 && errno==ECHILD)) break;
            if (waited<0 && errno==EINTR) continue;
            if (waited<0) { if(!error) error=errno; break; }
        }
    }
    if (owned) {
        struct stat current;
        if (fstatat(s->root,leaf,&current,AT_SYMLINK_NOFOLLOW)==0 &&
            current.st_dev==identity.st_dev && current.st_ino==identity.st_ino)
            if (unlinkat(s->root,leaf,0)<0 && !error) error=errno;
    }
    if (!committed) {
        BtPacket ready; bt_wire_packet(&ready,BT_WIRE_READY); ready.error=error?error:EIO;
        (void)bt_wire_send(3,&ready,-1); close(3);
    } else if (s->stopping_peer>=0) {
        Peer *p=&s->peers[s->stopping_peer]; BtPacket reply_packet;
        bt_wire_packet(&reply_packet,BT_WIRE_STOP); reply_packet.request=p->packet.request;
        reply_packet.epoch=s->epoch; reply_packet.error=error;
        (void)bt_wire_send(p->fd,&reply_packet,-1);
    }
    if (s->keys) ghostty_key_encoder_free(s->keys);
    if (s->key_event) ghostty_key_event_free(s->key_event);
    if (s->mouse) ghostty_mouse_encoder_free(s->mouse);
    if (s->mouse_event) ghostty_mouse_event_free(s->mouse_event);
    for (unsigned i=0;i<PEERS;++i) { s->peers[i].role=0; peer_close(s,&s->peers[i]); }
    if (s->frame_fd>=0) close(s->frame_fd);
    if (s->delta_fd>=0) close(s->delta_fd);
    bt_presentation_free(s->current);
    if (s->root>=0) close(s->root);
    free(s); return error?1:0;
}
