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

struct BtRemote { int fd; bool observe; uint64_t request, epoch, revision; };

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
    p->request=++r->request;
    int result=exchange(r->fd,p,input,output,bt_millis()+wait+3000);
    if (result<0) { int e=errno; close(r->fd); r->fd=-1; errno=e; }
    if (result)
        return bt_wire_error(s->error,sizeof(s->error),"state service request",errno);
    return 0;
}
static int publish(BtSession *s, const BtPacket *p, int fd) {
    BtPresentation *frame=NULL;
    int error=0;
    if (!p->epoch || !p->cols || p->cols>1000 || !p->rows || p->rows>1000 ||
        !p->cw || p->cw>512 || !p->ch || p->ch>512 || p->child>INT32_MAX ||
        p->pending>8u*1024u*1024u) error=EPROTO;
    if (!error && fd>=0) {
        void *map=bt_wire_map(fd,p->blob_size,BT_PRESENTATION_MAX_BYTES);
        if (map==MAP_FAILED) error=errno;
        else {
            if (bt_presentation_unpack(map,p->blob_size,&frame)) error=errno;
            munmap(map,p->blob_size);
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
        bt_presentation_free(frame);
        if (s->remote->fd>=0) { close(s->remote->fd); s->remote->fd=-1; }
        return bt_wire_error(s->error,sizeof(s->error),"invalid presentation",error);
    }
    if (frame) {
        bt_presentation_free(s->presentation); s->presentation=frame;
        size_t n=frame->title_length<sizeof(s->title_cache)-1?frame->title_length:sizeof(s->title_cache)-1;
        if (strlen(s->title_cache)!=n || memcmp(s->title_cache,frame->title,n)) s->title_changed=true;
        memcpy(s->title_cache,frame->title,n); s->title_cache[n]=0;
    }
    s->remote->epoch=p->epoch; s->remote->revision=p->revision;
    s->cols=p->cols; s->rows=p->rows; s->cell_width=p->cw; s->cell_height=p->ch;
    s->child=(pid_t)p->child; s->ready=true;
    s->bytes_read=p->bytes_read; s->bytes_written=p->bytes_written;
    s->pending_start=0; s->pending_end=p->pending;
    s->exited=(p->state&BT_STATE_EXITED)!=0; s->eof=(p->state&BT_STATE_EOF)!=0;
    s->done=(p->state&BT_STATE_DONE)!=0; s->exit_status=(int32_t)p->exit_status;
    return 0;
}
int bt_remote_attach(BtSession *s, const char *root, const char *name, bool observe) {
    memset(s,0,sizeof(*s)); s->master=s->control=s->status=-1; s->exit_status=-1;
    int fd=bt_wire_connect(root,name);
    if (fd<0) return bt_wire_error(s->error,sizeof(s->error),"connect state service",errno);
    s->remote=calloc(1,sizeof(*s->remote));
    if (!s->remote) { close(fd); return bt_wire_error(s->error,sizeof(s->error),"allocate connection",ENOMEM); }
    s->remote->fd=fd; s->remote->observe=observe;
    BtPacket p; bt_wire_packet(&p,BT_WIRE_HELLO); p.flags=observe?2:1;
    int received=-1;
    if (rpc(s,&p,-1,&received,0) || publish(s,&p,received)) {
        bt_remote_close(s); return -1;
    }
    return 0;
}
bool bt_remote_observer(const BtSession *s) { return s->remote && s->remote->observe; }
int bt_remote_pump(BtSession *s, int timeout) {
    if (timeout<0 || timeout>100) return bt_wire_error(s->error,sizeof(s->error),"invalid pump timeout",EINVAL);
    BtPacket p; bt_wire_packet(&p,BT_WIRE_FRAME);
    p.revision=s->remote->revision; p.epoch=s->remote->epoch; p.flags=(unsigned)timeout;
    int fd=-1;
    if (rpc(s,&p,-1,&fd,(unsigned)timeout)) return -1;
    return publish(s,&p,fd);
}
static int action(BtSession *s, BtPacket *p, const void *data, size_t length) {
    if (bt_remote_observer(s)) return bt_wire_error(s->error,sizeof(s->error),"observer is read-only",EPERM);
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
    BtPacket p; bt_wire_packet(&p,BT_WIRE_RESIZE);
    p.cols=cols; p.rows=rows; p.cw=cw; p.ch=ch;
    if (action(s,&p,NULL,0)) return -1;
    return bt_remote_pump(s,0);
}
char *bt_remote_text(BtSession *s, bool selection, size_t *length) {
    *length=0;
    BtPacket p; bt_wire_packet(&p,BT_WIRE_TEXT); p.flags=selection;
    int fd=-1;
    if (rpc(s,&p,-1,&fd,0)) return NULL;
    if (p.blob_size>BT_PRESENTATION_MAX_BYTES || (p.blob_size && fd<0) || (!p.blob_size && fd>=0)) {
        if (fd>=0) close(fd);
        bt_wire_error(s->error,sizeof(s->error),"invalid text response",EPROTO); return NULL;
    }
    char *text=malloc((size_t)p.blob_size+1);
    if (!text) { if(fd>=0) close(fd); return NULL; }
    if (p.blob_size) {
        void *map=bt_wire_map(fd,p.blob_size,BT_PRESENTATION_MAX_BYTES);
        close(fd);
        if (map==MAP_FAILED) { free(text); return NULL; }
        memcpy(text,map,p.blob_size); munmap(map,p.blob_size);
    }
    text[p.blob_size]=0; *length=p.blob_size;
    return text;
}
void bt_remote_close(BtSession *s) {
    if (s->remote) { if (s->remote->fd>=0) close(s->remote->fd); free(s->remote); s->remote=NULL; }
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
