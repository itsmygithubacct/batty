/* SPDX-License-Identifier: MIT */
#define _GNU_SOURCE
#include "remote_internal.h"
#include "presentation.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

enum { INPUT_COUNT_LIMIT=128, INPUT_BYTES_LIMIT=8u*1024u*1024u, TEXT_JOBS=16 };
typedef struct { unsigned cols,rows,cw,ch; } RemoteSize;
static bool same_size(RemoteSize a, RemoteSize b) {
    return a.cols==b.cols && a.rows==b.rows && a.cw==b.cw && a.ch==b.ch;
}
typedef struct TextResult {
    struct TextResult *next;
    uint64_t id;
    char *text;
    size_t limit,length;
    int error;
    bool pending,cancelled;
} TextResult;
typedef struct PendingInput {
    struct PendingInput *next;
    BtIntent intent;
    RemoteSize size;
    uint32_t type,flags;
    TextResult *text;
    size_t bytes;
    int fd;
    uint64_t request, deadline;
} PendingInput;
struct BtRemote {
    int fd;
    uint32_t poll_type;
    bool observe,clipboard_supported,delta_supported,poll_pending,clipboard_pending,queued_input;
    bool upstream_disconnected;
    BtRemoteFrameStats frame_stats;
    PendingInput *input_head,*input_tail;
    unsigned input_count,text_count;
    uint64_t text_id;
    TextResult *texts;
    size_t input_bytes;
    char failure[256];
    RemoteSize resize_target;
    uint64_t resize_request;
    uint64_t request, epoch, revision, poll_request, poll_deadline;
    uint64_t clipboard_generation,poll_clipboard_generation;
};
static int finish_next(BtSession *s, bool wait);
static int drain(BtSession *s, bool include_poll);
static int publish_clipboard(BtSession *s, const BtPacket *p, int fd, bool deliver);

bool bt_remote_disconnected(const BtSession *s) {
    return s->remote && (s->remote->fd<0 || s->remote->upstream_disconnected);
}
const char *bt_remote_failure(const BtSession *s) { return s->remote?s->remote->failure:""; }
BtRemoteFrameStats bt_remote_frame_stats(const BtSession *s) {
    return s && s->remote?s->remote->frame_stats:(BtRemoteFrameStats){0};
}
bool bt_remote_resize_pending(const BtSession *s) {
    BtRemote *r=s->remote;
    if(!r || r->fd<0 || r->observe) return false;
    if(!same_size(r->resize_target,(RemoteSize){s->cols,s->rows,s->cell_width,s->cell_height})) return true;
    for(PendingInput *input=r->input_head;input;input=input->next)
        if(input->type==BT_WIRE_RESIZE) return true;
    return false;
}
static void free_text(BtRemote *r, TextResult *result) {
    TextResult **link=&r->texts;
    while(*link && *link!=result) link=&(*link)->next;
    if(*link) { *link=result->next; --r->text_count; }
    free(result->text); free(result);
}
static TextResult *find_text(BtRemote *r, uint64_t id) {
    for(TextResult *result=r?r->texts:NULL;result;result=result->next)
        if(result->id==id) return result;
    return NULL;
}
static void discard_inputs(BtRemote *r, int error) {
    PendingInput *input=r->input_head;
    while(input) {
        PendingInput *next=input->next;
        if(input->fd>=0) close(input->fd);
        if(input->text) {
            input->text->pending=false; input->text->error=error;
            if(input->text->cancelled) free_text(r,input->text);
        }
        free(input); input=next;
    }
    r->input_head=r->input_tail=NULL; r->input_count=0; r->input_bytes=0; r->resize_request=0;
}
static int disconnect(BtSession *s, const char *operation, int error) {
    BtRemote *r=s->remote;
    if(r->fd>=0) close(r->fd);
    r->fd=-1; r->poll_pending=false;
    discard_inputs(r,error);
    bt_session_clipboard_clear(s); s->clipboard_enabled=false;
    bt_wire_error(s->error,sizeof(s->error),operation,error);
    if(!r->failure[0]) snprintf(r->failure,sizeof(r->failure),"%s",s->error);
    return -1;
}
static int exchange(int fd, BtPacket *p, int input, int *output, uint64_t deadline) {
    *output=-1;
    uint64_t request=p->request; uint32_t type=p->type;
    while (bt_wire_send(fd,p,input)<0) {
        if (errno==EINTR) continue;
        if (errno!=EAGAIN && errno!=EWOULDBLOCK) return -1;
        if (bt_wire_wait(fd,POLLOUT,deadline)) return -1;
    }
    for (;;) {
        int rc=bt_wire_receive(fd,p,output);
        if (rc==1) break;
        if (!rc) { errno=ECONNRESET; return -1; }
        if (errno==EINTR) continue;
        if (errno!=EAGAIN && errno!=EWOULDBLOCK) return -1;
        if (bt_wire_wait(fd,POLLIN,deadline)) return -1;
    }
    if (p->request!=request || p->type!=type) {
        if (*output>=0) close(*output);
        *output=-1; errno=EPROTO; return -1;
    }
    if (p->error) {
        if (*output>=0) close(*output);
        *output=-1; errno=p->error<=4095?(int)p->error:EPROTO; return 1;
    }
    return 0;
}
static int rpc(BtSession *s, BtPacket *p, int input, int *output, unsigned wait) {
    BtRemote *r=s->remote;
    if (!r || r->fd<0) return bt_wire_error(s->error,sizeof(s->error),"session connection closed",ENOTCONN);
    s->error[0]=0;
    *output=-1;
    if(drain(s,true)) return -1;
    if(p->type==BT_WIRE_FRAME) {
        /* Draining an earlier asynchronous poll may replace the base frame. */
        p->epoch=r->epoch; p->revision=r->revision;
        p->state=r->delta_supported?BT_STATE_DELTA_SUPPORTED:0;
    }
    p->request=++r->request;
    int result=exchange(r->fd,p,input,output,bt_millis()+wait+3000);
    if (result<0) return disconnect(s,"state service request",errno);
    if (result)
        return bt_wire_error(s->error,sizeof(s->error),"state service request",errno);
    return 0;
}
static int publish(BtSession *s, const BtPacket *p, int fd) {
    BtPresentation *frame=NULL;
    char *directory=NULL;
    int error=0;
    if(p->type==BT_WIRE_HELLO) {
        if(p->length && p->length<32) error=EPROTO;
        else for(unsigned i=0;i<32 && i<p->length;++i)
            if((p->data[i]<'0' || p->data[i]>'9') && (p->data[i]<'a' || p->data[i]>'f')) error=EPROTO;
        if(!error && p->length>32) {
            if(p->length<35 || p->data[32] || p->data[33]!='/' ||
               p->data[p->length-1] || memchr(p->data+33,0,p->length-34)) error=EPROTO;
            else {
                directory=strdup((const char *)p->data+33);
                if(!directory) error=ENOMEM;
            }
        }
    } else if(p->length) error=EPROTO;
    if (!p->epoch || !p->cols || p->cols>1000 || !p->rows || p->rows>1000 ||
        !p->cw || p->cw>512 || !p->ch || p->ch>512 || p->child>INT32_MAX ||
        p->pending>8u*1024u*1024u) error=EPROTO;
    bool delta=(p->state&BT_STATE_DELTA_FRAME)!=0;
    if(delta && (!s->remote->delta_supported || p->type!=BT_WIRE_FRAME || fd<0)) error=EPROTO;
    if (!error && fd>=0) {
        void *map=bt_wire_map(fd,p->blob_size,BT_PRESENTATION_MAX_BYTES);
        if (map==MAP_FAILED) error=errno;
        else {
            int rc=delta?bt_presentation_unpack_delta_mapping(map,p->blob_size,s->presentation,&frame):
                         bt_presentation_unpack_mapping(map,p->blob_size,&frame);
            if(rc) error=errno;
        }
        if (!error && (frame->epoch!=p->epoch || frame->revision!=p->revision ||
                       frame->cols!=p->cols || frame->rows!=p->rows ||
                       frame->cell_width!=p->cw || frame->cell_height!=p->ch)) error=EPROTO;
    } else if (!error && (p->blob_size || !s->presentation ||
               s->presentation->epoch!=p->epoch || s->presentation->revision!=p->revision ||
               s->presentation->cols!=p->cols || s->presentation->rows!=p->rows ||
               s->presentation->cell_width!=p->cw || s->presentation->cell_height!=p->ch)) error=EPROTO;
    if (fd>=0) close(fd);
    if (error) {
        free(directory);
        bt_presentation_free(frame);
        return disconnect(s,"invalid presentation",error);
    }
    if(frame) {
        if(delta) { ++s->remote->frame_stats.delta_frames; s->remote->frame_stats.delta_bytes+=p->blob_size; }
        else { ++s->remote->frame_stats.full_frames; s->remote->frame_stats.full_bytes+=p->blob_size; }
    } else if(p->type==BT_WIRE_FRAME) ++s->remote->frame_stats.unchanged_polls;
    if (frame) {
        bt_presentation_free(s->presentation); s->presentation=frame;
        size_t n=frame->title_length<sizeof(s->title_cache)-1?frame->title_length:sizeof(s->title_cache)-1;
        if (strlen(s->title_cache)!=n || memcmp(s->title_cache,frame->title,n)) s->title_changed=true;
        memcpy(s->title_cache,frame->title,n); s->title_cache[n]=0;
    }
    s->remote->delta_supported=(p->state&BT_STATE_DELTA_SUPPORTED)!=0;
    s->remote->clipboard_supported=(p->state&BT_STATE_CLIPBOARD_SUPPORTED)!=0;
    s->remote->upstream_disconnected=(p->state&BT_STATE_UPSTREAM_DISCONNECTED)!=0;
    s->remote->epoch=p->epoch; s->remote->revision=p->revision;
    s->cols=p->cols; s->rows=p->rows; s->cell_width=p->cw; s->cell_height=p->ch;
    s->child=(pid_t)p->child; s->ready=true;
    s->bytes_read=p->bytes_read; s->bytes_written=p->bytes_written;
    s->pending_start=0; s->pending_end=p->pending;
    s->exited=(p->state&BT_STATE_EXITED)!=0; s->eof=(p->state&BT_STATE_EOF)!=0;
    s->done=(p->state&BT_STATE_DONE)!=0; s->exit_status=(int32_t)p->exit_status;
    unsigned recording=(p->state&BT_STATE_RECORD_MASK)>>BT_STATE_RECORD_SHIFT;
    s->remote_recording=recording<=BT_RECORD_FAILED?recording:BT_RECORD_UNKNOWN;
    if(p->type==BT_WIRE_HELLO && p->length>=32) {
        memcpy(s->remote_transcript_id,p->data,32);
        s->remote_transcript_id[32]=0;
        s->remote_transcript_directory=directory;
    }
    return 0;
}
static int poll_failure(BtSession *s, int error) {
    return disconnect(s,"state service poll",error);
}
/* Consumes the descriptor on every path. Limit failures belong to the text
 * request, not the connection; malformed descriptors remain protocol errors. */
static int decode_text(const BtPacket *p, int fd, size_t limit, bool discard, char **out, size_t *length) {
    *out=NULL; *length=0;
    int error=0;
    if(p->blob_size>BT_PRESENTATION_MAX_BYTES || (p->blob_size && fd<0) || (!p->blob_size && fd>=0)) error=EPROTO;
    else if(p->blob_size>limit) error=E2BIG;
    void *map=MAP_FAILED;
    if(!error && p->blob_size) {
        map=bt_wire_map(fd,p->blob_size,BT_PRESENTATION_MAX_BYTES);
        if(map==MAP_FAILED) error=EPROTO;
    }
    if(fd>=0) close(fd);
    if(!error && !discard) {
        *out=malloc((size_t)p->blob_size+1);
        if(!*out) error=ENOMEM;
        else {
            if(p->blob_size) memcpy(*out,map,p->blob_size);
            (*out)[p->blob_size]=0; *length=p->blob_size;
        }
    }
    if(map!=MAP_FAILED) munmap(map,p->blob_size);
    if(error) { errno=error; return -1; }
    return 0;
}
/* Inputs may follow an outstanding poll on the socket. IDs order both kinds
 * of reply; a slow frame does not hold up sending the next key or paste. */
static int finish_next(BtSession *s, bool wait) {
    BtRemote *r=s->remote;
    PendingInput *input=r->input_head;
    bool is_input=input && input->request &&
        (!r->poll_pending || input->request<r->poll_request);
    if(!is_input && !r->poll_pending) return 0;
    uint64_t request=is_input?input->request:r->poll_request;
    uint64_t deadline=is_input?input->deadline:r->poll_deadline;
    uint32_t type=is_input?input->type:r->poll_type;
    BtPacket p; int fd=-1;
    for(;;) {
        int rc=bt_wire_receive(r->fd,&p,&fd);
        if(rc==1) break;
        if(!rc) return poll_failure(s,ECONNRESET);
        if(errno!=EINTR && errno!=EAGAIN && errno!=EWOULDBLOCK) return poll_failure(s,errno);
        if(bt_millis()>=deadline) return poll_failure(s,ETIMEDOUT);
        if(!wait) return 1;
        if(bt_wire_wait(r->fd,POLLIN,deadline)) return poll_failure(s,errno);
    }
    bool text_reply=is_input && input->text;
    if(p.request!=request || p.type!=type || p.error>4095 || (p.error && !text_reply)) {
        if(fd>=0) close(fd);
        return poll_failure(s,p.request==request && p.type==type && p.error && p.error<=4095?(int)p.error:EPROTO);
    }
    if(is_input) {
        if(text_reply) {
            TextResult *result=input->text;
            if(p.error) {
                if(fd>=0) close(fd);
                if(fd>=0 || p.blob_size) return poll_failure(s,EPROTO);
                result->error=(int)p.error;
            } else if(decode_text(&p,fd,result->limit,result->cancelled,&result->text,&result->length)) {
                if(errno==EPROTO) return poll_failure(s,errno);
                result->error=errno;
            }
            result->pending=false;
            if(result->cancelled) free_text(r,result);
        } else if(fd>=0 || p.blob_size) {
            if(fd>=0) close(fd);
            return poll_failure(s,EPROTO);
        }
        r->input_head=input->next;
        if(!r->input_head) r->input_tail=NULL;
        --r->input_count; r->input_bytes-=input->bytes;
        if(input->type==BT_WIRE_RESIZE) r->resize_request=0;
        free(input);
        return 0;
    }
    r->poll_pending=false;
    if(type==BT_WIRE_CLIPBOARD) {
        r->clipboard_pending=false;
        return publish_clipboard(s,&p,fd,r->poll_clipboard_generation==r->clipboard_generation);
    }
    if(publish(s,&p,fd)) return -1;
    r->clipboard_pending=(p.state&BT_STATE_CLIPBOARD_PENDING)!=0;
    return 0;
}
/* Nonblocking sends only. In particular, never wait for socket space while
 * unread acknowledgments might be keeping the service's output blocked. */
static int flush_inputs(BtSession *s) {
    BtRemote *r=s->remote;
    for(PendingInput *input=r->input_head;input;input=input->next) {
        if(input->request) continue;
        /* One resize may be in flight. Keep its successor unsent so adjacent
         * layout changes can coalesce; never pass it with later input. */
        if(input->type==BT_WIRE_RESIZE && r->resize_request) {
            if(bt_millis()>=input->deadline) return poll_failure(s,ETIMEDOUT);
            return 1;
        }
        BtPacket p; bt_wire_packet(&p,input->type); p.flags=input->flags;
        if(input->type==BT_WIRE_INTENT) {
            p.length=sizeof(input->intent); memcpy(p.data,&input->intent,p.length);
        }
        p.cols=input->size.cols; p.rows=input->size.rows; p.cw=input->size.cw; p.ch=input->size.ch;
        p.blob_size=input->bytes; p.request=r->request+1;
        if(bt_wire_send(r->fd,&p,input->fd)<0) {
            if(errno!=EINTR && errno!=EAGAIN && errno!=EWOULDBLOCK) return poll_failure(s,errno);
            if(bt_millis()>=input->deadline) return poll_failure(s,ETIMEDOUT);
            return 1;
        }
        input->request=p.request; r->request=p.request;
        if(input->type==BT_WIRE_RESIZE) r->resize_request=p.request;
        if(input->fd>=0) { close(input->fd); input->fd=-1; }
    }
    return 0;
}
static int drain(BtSession *s, bool include_poll) {
    BtRemote *r=s->remote;
    while(r->input_head || (include_poll && r->poll_pending)) {
        if(flush_inputs(s)<0) return -1;
        if(r->poll_pending || (r->input_head && r->input_head->request)) {
            if(finish_next(s,true)) return -1;
        } else if(bt_wire_wait(r->fd,POLLOUT,r->input_head->deadline)) return poll_failure(s,errno);
    }
    return 0;
}
int bt_remote_input_drain(BtSession *s) {
    BtRemote *r=s->remote;
    if(!r || r->fd<0) return bt_wire_error(s->error,sizeof(s->error),"session connection closed",ENOTCONN);
    for(unsigned i=0;i<32 && r->input_head;++i) {
        if(flush_inputs(s)<0) return -1;
        if(!r->poll_pending && !r->input_head->request) return 1;
        int rc=finish_next(s,false);
        if(rc) return rc;
    }
    return r->input_head?1:0;
}
int bt_remote_input_flush(BtSession *s) {
    if(!s->remote || s->remote->fd<0)
        return bt_wire_error(s->error,sizeof(s->error),"session connection closed",ENOTCONN);
    s->error[0]=0;
    return drain(s,false);
}
int bt_remote_input_queue(BtSession *s, bool enabled) {
    if(!s->remote || s->remote->fd<0)
        return bt_wire_error(s->error,sizeof(s->error),"session connection closed",ENOTCONN);
    if(s->remote->observe) return bt_wire_error(s->error,sizeof(s->error),"observer is read-only",EPERM);
    if(!enabled && bt_remote_input_flush(s)) return -1;
    s->remote->queued_input=enabled;
    return 0;
}
int bt_remote_attach(BtSession *s, const char *root, const char *name, bool observe) {
    return bt_remote_attach_epoch(s,root,name,observe,0);
}
int bt_remote_attach_epoch(BtSession *s, const char *root, const char *name, bool observe, uint64_t epoch) {
    memset(s,0,sizeof(*s)); s->master=s->control=s->status=-1; s->exit_status=-1;
    int fd=bt_wire_connect(root,name);
    if (fd<0) return bt_wire_error(s->error,sizeof(s->error),"connect state service",errno);
    s->remote=calloc(1,sizeof(*s->remote));
    if (!s->remote) { close(fd); return bt_wire_error(s->error,sizeof(s->error),"allocate connection",ENOMEM); }
    s->remote->fd=fd; s->remote->observe=observe;
    BtPacket p; bt_wire_packet(&p,BT_WIRE_HELLO); p.flags=observe?2:1; p.epoch=epoch;
    int received=-1;
    if (rpc(s,&p,-1,&received,0)) {
        bt_remote_close(s); return -1;
    }
    /* Also check the reply from an older owner that ignores HELLO.epoch. */
    if(epoch && p.epoch!=epoch) {
        if(received>=0) close(received);
        bt_remote_close(s);
        return bt_wire_error(s->error,sizeof(s->error),"saved session owner has changed",ESTALE);
    }
    if(publish(s,&p,received)) { bt_remote_close(s); return -1; }
    s->remote->resize_target=(RemoteSize){s->cols,s->rows,s->cell_width,s->cell_height};
    return 0;
}
uint64_t bt_remote_epoch(const BtSession *s) { return s->remote?s->remote->epoch:0; }
bool bt_remote_observer(const BtSession *s) { return s->remote && s->remote->observe; }
int bt_remote_clipboard_policy(BtSession *s, bool enabled) {
    if(bt_remote_observer(s)) return bt_wire_error(s->error,sizeof(s->error),"observer is read-only",EPERM);
    /* Older running services retain their disabled clipboard policy. */
    if(!s->remote->clipboard_supported) return 0;
    /* Invalidate an earlier fetch even if permission is revoked and restored
     * before its reply arrives. Queue failure must not leave the old policy
     * active on this connection. */
    ++s->remote->clipboard_generation;
    s->remote->clipboard_pending=false;
    BtIntent intent={.type=BT_INTENT_CLIPBOARD_POLICY,.focused=enabled};
    if(bt_remote_intent(s,&intent,NULL,0)) return disconnect(s,"clipboard policy",errno);
    return 0;
}
static int publish_clipboard(BtSession *s, const BtPacket *p, int fd, bool deliver) {
    if(!p->blob_size && fd<0) return 0;
    int error=0; void *map=MAP_FAILED;
    if(fd<0 || !p->blob_size || p->blob_size>BT_CLIPBOARD_LIMIT+1) error=EPROTO;
    else {
        map=bt_wire_map(fd,p->blob_size,BT_CLIPBOARD_LIMIT+1);
        if(map==MAP_FAILED) error=errno;
        else if(((char *)map)[p->blob_size-1] || memchr(map,0,p->blob_size-1)) error=EPROTO;
        else if(deliver && s->clipboard_enabled && !bt_remote_observer(s)) {
            char *text=malloc(p->blob_size);
            if(!text) error=ENOMEM;
            else {
                memcpy(text,map,p->blob_size); bt_session_clipboard_clear(s);
                s->clipboard=text; s->clipboard_length=p->blob_size-1;
            }
        }
    }
    if(map!=MAP_FAILED) munmap(map,p->blob_size);
    if(fd>=0) close(fd);
    return error?bt_wire_error(s->error,sizeof(s->error),"invalid clipboard response",error):0;
}
static int clipboard_fetch(BtSession *s) {
    BtPacket p; bt_wire_packet(&p,BT_WIRE_CLIPBOARD);
    int fd=-1;
    if(rpc(s,&p,-1,&fd,0)) return -1;
    s->remote->clipboard_pending=false;
    return publish_clipboard(s,&p,fd,true);
}
static int start_poll(BtSession *s) {
    BtRemote *r=s->remote;
    uint32_t type=(!r->observe && s->clipboard_enabled && r->clipboard_pending)?BT_WIRE_CLIPBOARD:BT_WIRE_FRAME;
    BtPacket request; bt_wire_packet(&request,type);
    if(type==BT_WIRE_FRAME) { request.revision=r->revision; request.epoch=r->epoch;
        if(r->delta_supported) request.state=BT_STATE_DELTA_SUPPORTED; }
    request.request=r->request+1;
    if(bt_wire_send(r->fd,&request,-1)<0) {
        if(errno==EINTR || errno==EAGAIN || errno==EWOULDBLOCK) return 1;
        return poll_failure(s,errno);
    }
    r->request=request.request; r->poll_type=type;
    r->poll_clipboard_generation=r->clipboard_generation;
    r->poll_pending=true; r->poll_request=request.request; r->poll_deadline=bt_millis()+3000;
    return 0;
}
int bt_remote_pump(BtSession *s, int timeout) {
    if (timeout<0 || timeout>100) return bt_wire_error(s->error,sizeof(s->error),"invalid pump timeout",EINVAL);
    BtRemote *r=s->remote;
    if(!r || r->fd<0) return bt_wire_error(s->error,sizeof(s->error),"session connection closed",ENOTCONN);
    if(!timeout) {
        s->error[0]=0;
        int sent=flush_inputs(s);
        if(sent<0) return -1;
        if(!r->poll_pending && !sent && start_poll(s)<0) return -1;
        /* Drain a bounded batch of acknowledgments and at most one poll. */
        for(unsigned i=0;i<32;++i) {
            bool poll=r->poll_pending;
            if(!poll && (!r->input_head || !r->input_head->request)) break;
            int rc=finish_next(s,false);
            if(rc<0) return -1;
            if(rc || (poll && !r->poll_pending)) break;
        }
        sent=flush_inputs(s);
        if(sent<0) return -1;
        if(!r->poll_pending && !sent && start_poll(s)<0) return -1;
        return 0;
    }
    BtPacket p; bt_wire_packet(&p,BT_WIRE_FRAME);
    p.revision=s->remote->revision; p.epoch=s->remote->epoch; p.flags=(unsigned)timeout;
    if(s->remote->delta_supported) p.state=BT_STATE_DELTA_SUPPORTED;
    int fd=-1;
    if (rpc(s,&p,-1,&fd,(unsigned)timeout)) return -1;
    if(publish(s,&p,fd)) return -1;
    if(!bt_remote_observer(s) && s->clipboard_enabled && (p.state&BT_STATE_CLIPBOARD_PENDING))
        return clipboard_fetch(s);
    return 0;
}
static int enqueue(BtSession *s, const BtPacket *p, const void *data, size_t length, TextResult *text) {
    BtRemote *r=s->remote;
    if(r->fd<0) return bt_wire_error(s->error,sizeof(s->error),"session connection closed",ENOTCONN);
    RemoteSize size={p->cols,p->rows,p->cw,p->ch};
    if(p->type==BT_WIRE_RESIZE && r->input_tail &&
       r->input_tail->type==BT_WIRE_RESIZE && !r->input_tail->request) {
        r->input_tail->size=size; s->error[0]=0;
        return flush_inputs(s)<0?-1:0;
    }
    if(r->input_count>=INPUT_COUNT_LIMIT || length>INPUT_BYTES_LIMIT-r->input_bytes)
        return bt_wire_error(s->error,sizeof(s->error),"input queue full",ENOBUFS);
    PendingInput *input=calloc(1,sizeof(*input));
    if(!input) return bt_wire_error(s->error,sizeof(s->error),"allocate input",ENOMEM);
    input->fd=-1; input->type=p->type; input->bytes=length; input->deadline=bt_millis()+3000;
    input->size=size; input->flags=p->flags; input->text=text;
    if(p->type==BT_WIRE_INTENT) memcpy(&input->intent,p->data,sizeof(input->intent));
    if(length) {
        input->fd=bt_wire_blob(data,length);
        if(input->fd<0) { int e=errno; free(input); return bt_wire_error(s->error,sizeof(s->error),"stage input",e); }
    }
    if(r->input_tail) r->input_tail->next=input;
    else r->input_head=input;
    r->input_tail=input; ++r->input_count; r->input_bytes+=length;
    s->error[0]=0;
    return flush_inputs(s)<0?-1:0;
}
static int action(BtSession *s, BtPacket *p, const void *data, size_t length) {
    if (bt_remote_observer(s)) return bt_wire_error(s->error,sizeof(s->error),"observer is read-only",EPERM);
    if(s->remote->queued_input && (p->type==BT_WIRE_SEND || p->type==BT_WIRE_INTENT || p->type==BT_WIRE_RESIZE))
        return enqueue(s,p,data,length,NULL);
    int blob=-1, received=-1;
    if (length) {
        blob=bt_wire_blob(data,length);
        if (blob<0) return bt_wire_error(s->error,sizeof(s->error),"stage input",errno);
        p->blob_size=length;
    }
    int rc=rpc(s,p,blob,&received,0), error=errno;
    if (blob>=0) close(blob);
    if (received>=0) { close(received); rc=-1; error=EPROTO; }
    errno=error;
    return rc;
}
int bt_remote_send(BtSession *s, const void *data, size_t length) {
    if (length>8u*1024u*1024u || (!data && length))
        return bt_wire_error(s->error,sizeof(s->error),"input exceeds limit",EINVAL);
    BtPacket p; bt_wire_packet(&p,BT_WIRE_SEND);
    return action(s,&p,data,length);
}
int bt_remote_intent(BtSession *s, const BtIntent *intent, const void *text, size_t length) {
    if (!intent || length>4u*1024u*1024u || (!text && length))
        return bt_wire_error(s->error,sizeof(s->error),"invalid input intent",EINVAL);
    BtPacket p; bt_wire_packet(&p,BT_WIRE_INTENT); p.length=sizeof(*intent);
    memcpy(p.data,intent,sizeof(*intent));
    return action(s,&p,text,length);
}
int bt_remote_resize(BtSession *s, unsigned cols, unsigned rows, unsigned cw, unsigned ch) {
    BtRemote *r=s->remote;
    if(!r || r->fd<0) return bt_wire_error(s->error,sizeof(s->error),"session connection closed",ENOTCONN);
    if(!cols || cols>1000 || !rows || rows>1000 || !cw || cw>512 || !ch || ch>512)
        return bt_wire_error(s->error,sizeof(s->error),"invalid terminal dimensions",EINVAL);
    RemoteSize size={cols,rows,cw,ch};
    if(r->queued_input && same_size(r->resize_target,size)) { s->error[0]=0; return 0; }
    BtPacket p; bt_wire_packet(&p,BT_WIRE_RESIZE);
    p.cols=cols; p.rows=rows; p.cw=cw; p.ch=ch;
    if (action(s,&p,NULL,0)) return -1;
    r->resize_target=size;
    return r->queued_input?0:bt_remote_pump(s,1);
}
int bt_remote_reset(BtSession *s) {
    BtPacket p; bt_wire_packet(&p,BT_WIRE_RESET);
    return action(s,&p,NULL,0);
}
int bt_remote_text_start(BtSession *s, bool selection, size_t limit, uint64_t *ticket) {
    *ticket=0;
    BtRemote *r=s->remote;
    if(!r || r->fd<0) return bt_wire_error(s->error,sizeof(s->error),"session connection closed",ENOTCONN);
    if(limit>BT_CLIPBOARD_LIMIT) return bt_wire_error(s->error,sizeof(s->error),"text request limit",EINVAL);
    if(r->text_count==TEXT_JOBS) return bt_wire_error(s->error,sizeof(s->error),"text request queue full",ENOBUFS);
    TextResult *result=calloc(1,sizeof(*result));
    if(!result) return bt_wire_error(s->error,sizeof(s->error),"allocate text request",ENOMEM);
    result->id=++r->text_id; if(!result->id) result->id=++r->text_id;
    result->limit=limit; result->pending=true; result->next=r->texts; r->texts=result; ++r->text_count;
    BtPacket p; bt_wire_packet(&p,BT_WIRE_TEXT); p.flags=selection;
    if(enqueue(s,&p,NULL,0,result)) { free_text(r,result); return -1; }
    *ticket=result->id; return 0;
}
int bt_remote_text_take(BtSession *s, uint64_t ticket, char **text, size_t *length) {
    *text=NULL; *length=0;
    TextResult *result=find_text(s->remote,ticket);
    if(!result || result->cancelled) return bt_wire_error(s->error,sizeof(s->error),"text request does not exist",ENOENT);
    if(result->pending) return 1;
    int error=result->error;
    *text=result->text; *length=result->length; result->text=NULL;
    free_text(s->remote,result);
    if(error) return bt_wire_error(s->error,sizeof(s->error),"state service text",error);
    return 0;
}
void bt_remote_text_cancel(BtSession *s, uint64_t ticket) {
    TextResult *result=find_text(s->remote,ticket);
    if(!result) return;
    if(result->pending) result->cancelled=true;
    else free_text(s->remote,result);
}
char *bt_remote_text(BtSession *s, bool selection, size_t *length) {
    *length=0;
    BtPacket p; bt_wire_packet(&p,BT_WIRE_TEXT); p.flags=selection;
    int fd=-1;
    if (rpc(s,&p,-1,&fd,0)) return NULL;
    char *text=NULL;
    if(decode_text(&p,fd,BT_PRESENTATION_MAX_BYTES,false,&text,length)) {
        bt_wire_error(s->error,sizeof(s->error),"invalid text response",errno); return NULL;
    }
    return text;
}
void bt_remote_close(BtSession *s) {
    bt_session_clipboard_clear(s); s->clipboard_enabled=false;
    free(s->remote_transcript_directory); s->remote_transcript_directory=NULL;
    if (s->remote) {
        if (s->remote->fd>=0) close(s->remote->fd);
        discard_inputs(s->remote,ECANCELED);
        while(s->remote->texts) free_text(s->remote,s->remote->texts);
        free(s->remote); s->remote=NULL;
    }
    bt_presentation_free(s->presentation); s->presentation=NULL;
    s->pending_start=s->pending_end=0;
}
int bt_remote_create_owned(const char *service, const char *helper, const char *root, const char *name,
                     char *const argv[], char *const env[], unsigned cols, unsigned rows,
                     unsigned cw, unsigned ch, char *error, size_t error_size, uint64_t *epoch) {
    if (epoch) *epoch=0;
    if (!service || !helper || !argv || !argv[0] || !bt_wire_name(name) ||
        !cols || cols>1000 || !rows || rows>1000 || !cw || cw>512 || !ch || ch>512)
        return bt_wire_error(error,error_size,"invalid service options",EINVAL);
    int directory=bt_wire_root(root,true);
    if (directory<0) return bt_wire_error(error,error_size,"private session directory",errno);
    close(directory);
    size_t count=0; while (argv[count]) ++count;
    char **args=calloc(count+10,sizeof(*args));
    if (!args) return bt_wire_error(error,error_size,"allocate service arguments",ENOMEM);
    char dimensions[4][16]; unsigned values[]={cols,rows,cw,ch};
    args[0]=(char *)service; args[1]=(char *)root; args[2]=(char *)name; args[3]=(char *)helper;
    for (int i=0;i<4;++i) { snprintf(dimensions[i],sizeof(dimensions[i]),"%u",values[i]); args[4+i]=dimensions[i]; }
    args[8]="--";
    for (size_t i=0;i<count;++i) args[9+i]=argv[i];
    int pair[2]={-1,-1}, high=-1, result=0;
    pid_t child=-1;
    if (socketpair(AF_UNIX,SOCK_SEQPACKET|SOCK_CLOEXEC,0,pair)<0) { result=errno; goto done; }
    high=fcntl(pair[1],F_DUPFD_CLOEXEC,64);
    if (high<0) { result=errno; goto done; }
    posix_spawn_file_actions_t actions; posix_spawnattr_t attributes;
    result=posix_spawn_file_actions_init(&actions);
    if (result) goto done;
    result=posix_spawnattr_init(&attributes);
    if (result) { posix_spawn_file_actions_destroy(&actions); goto done; }
#define CHECK(call) do { result=(call); if(result) goto spawned; } while(0)
    for (int i=0;i<3;++i) CHECK(posix_spawn_file_actions_addopen(&actions,i,"/dev/null",O_RDWR,0));
    CHECK(posix_spawn_file_actions_adddup2(&actions,high,3));
    CHECK(posix_spawn_file_actions_addclosefrom_np(&actions,4));
    sigset_t empty, defaults; sigemptyset(&empty); sigfillset(&defaults);
    CHECK(posix_spawnattr_setsigmask(&attributes,&empty));
    CHECK(posix_spawnattr_setsigdefault(&attributes,&defaults));
    CHECK(posix_spawnattr_setflags(&attributes,POSIX_SPAWN_SETSID|POSIX_SPAWN_SETSIGMASK|POSIX_SPAWN_SETSIGDEF));
    result=posix_spawn(&child,service,&actions,&attributes,args,env);
spawned:
    posix_spawnattr_destroy(&attributes); posix_spawn_file_actions_destroy(&actions);
#undef CHECK
    close(pair[1]); pair[1]=-1;
    close(high); high=-1;
    if (result) goto done;
    BtPacket p; int passed=-1;
    uint64_t deadline=bt_millis()+6000;
    for (;;) {
        int rc=bt_wire_receive(pair[0],&p,&passed);
        if (rc==1) {
            if (passed>=0) close(passed);
            result=passed>=0 || p.type!=BT_WIRE_READY || p.request?EPROTO:(int)p.error;
            break;
        }
        if (!rc) { result=ECONNRESET; break; }
        if (errno!=EINTR && errno!=EAGAIN && errno!=EWOULDBLOCK) { result=errno; break; }
        if (bt_wire_wait(pair[0],POLLIN,deadline)) { result=errno; break; }
    }
    if (!result) {
        uint64_t identity=p.epoch;
        if (!identity) result=EPROTO;
        else {
            bt_wire_packet(&p,BT_WIRE_READY); p.request=1; p.epoch=identity;
            if (exchange(pair[0],&p,-1,&passed,deadline) || passed>=0 || p.epoch!=identity) {
                result=errno?errno:EPROTO;
                if (passed>=0) close(passed);
            } else if (epoch) *epoch=identity;
        }
    }
done:
    if (high>=0) close(high);
    for (int i=0;i<2;++i) if (pair[i]>=0) close(pair[i]);
    free(args);
    if (child>0 && result) (void)waitpid(child,NULL,WNOHANG);
    if (result==EADDRINUSE || result==EEXIST) return 1;
    if (result) return bt_wire_error(error,error_size,"create state service",result);
    return 0;
}
int bt_remote_create(const char *service, const char *helper, const char *root, const char *name,
                     char *const argv[], char *const env[], unsigned cols, unsigned rows,
                     unsigned cw, unsigned ch, char *error, size_t capacity) {
    return bt_remote_create_owned(service,helper,root,name,argv,env,cols,rows,cw,ch,error,capacity,NULL);
}
static int stop(const char *root, const char *name, uint64_t epoch, char *error, size_t capacity) {
    int fd=bt_wire_connect(root,name);
    if (fd<0) return bt_wire_error(error,capacity,"connect state service",errno);
    BtPacket p; bt_wire_packet(&p,BT_WIRE_STOP); p.request=1;
    p.epoch=epoch;
    int passed=-1, rc=exchange(fd,&p,-1,&passed,bt_millis()+8000), e=errno;
    if (passed>=0) { close(passed); rc=-1; e=EPROTO; }
    close(fd);
    return rc?bt_wire_error(error,capacity,"terminate state service",e):0;
}
int bt_remote_terminate(const char *root, const char *name, char *error, size_t capacity) {
    return stop(root,name,0,error,capacity);
}
int bt_remote_cancel(const char *root, const char *name, uint64_t epoch, char *error, size_t capacity) {
    if (!epoch) return bt_wire_error(error,capacity,"invalid creation identity",EINVAL);
    return stop(root,name,epoch,error,capacity);
}
int bt_remote_list(const char *root, char **text, char *error, size_t capacity) {
    *text=NULL;
    int directory=bt_wire_root(root,false);
    if (directory<0) {
        if (errno==ENOENT) { *text=strdup(""); return *text?0:-1; }
        return bt_wire_error(error,capacity,"private session directory",errno);
    }
    DIR *entries=fdopendir(directory);
    if (!entries) { int e=errno; close(directory); return bt_wire_error(error,capacity,"list sessions",e); }
    size_t used=0, size=256; char *out=malloc(size);
    if (!out) { closedir(entries); return -1; }
    out[0]=0;
    struct dirent *entry; unsigned count=0;
    while ((entry=readdir(entries))) {
        size_t n=strlen(entry->d_name);
        if (n<6 || n>53 || strcmp(entry->d_name+n-5,".sock")) continue;
        char name[49]; memcpy(name,entry->d_name,n-5); name[n-5]=0;
        if (!bt_wire_name(name)) continue;
        if (++count>1024) { free(out); closedir(entries); return bt_wire_error(error,capacity,"session list limit",E2BIG); }
        int fd=bt_wire_connect(root,name), passed=-1;
        BtPacket p; bt_wire_packet(&p,BT_WIRE_STATUS); p.request=1;
        bool live=fd>=0 && exchange(fd,&p,-1,&passed,bt_millis()+250)==0;
        if (passed>=0) close(passed);
        if (fd>=0) close(fd);
        char line[256];
        if (live) snprintf(line,sizeof(line),"%s pid=%u cols=%u rows=%u status=%s exit=%d controllers=%u observers=%u\n",
            name,p.child,p.cols,p.rows,p.state&BT_STATE_EXITED?"exited":"running",
            (int32_t)p.exit_status,p.controllers,p.observers);
        else snprintf(line,sizeof(line),"%s unavailable\n",name);
        n=strlen(line);
        if (used+n+1>size) {
            size*=2; char *bigger=realloc(out,size);
            if (!bigger) { free(out); closedir(entries); return -1; }
            out=bigger;
        }
        memcpy(out+used,line,n+1); used+=n;
    }
    closedir(entries); *text=out; return 0;
}
