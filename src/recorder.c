/* SPDX-License-Identifier: MIT */
#define _GNU_SOURCE
#include "recorder.h"
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

/* Reap detached writers without waiting for disk I/O or signalling numeric
 * PIDs. This bounded table also limits outstanding writer processes. */
static int workers[128]; /* pidfd+1; zero free, -1 reserved */
static pid_t fallback[128]; /* Only used if pidfd acquisition fails after spawn. */
static unsigned cursor;
static void reap(void) {
    for(unsigned step=0;step<4;++step) {
        unsigned i=cursor++%128;
        if(fallback[i]) {
            int status;
            pid_t pid=waitpid(fallback[i],&status,WNOHANG);
            if(pid>0 || (pid<0 && errno==ECHILD)) { fallback[i]=0; workers[i]=0; }
            continue;
        }
        if(workers[i]<=0) continue;
        siginfo_t info={0};
        int rc=waitid(P_PIDFD,(id_t)(workers[i]-1),&info,WEXITED|WNOHANG);
        if((rc<0 && errno==ECHILD) || (!rc && info.si_pid)) { close(workers[i]-1); workers[i]=0; }
    }
}
static const char *setting(char *const env[], const char *key) {
    size_t n=strlen(key);
    if(env) for(unsigned i=0;env[i];++i) if(!strncmp(env[i],key,n) && env[i][n]=='=') return env[i]+n+1;
    return NULL;
}
static void failed(BtRecorder *r, int error) {
    if(!r->error) r->error=error?error:EIO;
    if(r->fd>=0) { close(r->fd); r->fd=-1; }
}
BtRecorder *bt_recorder_open(const char *helper, char *const env[], pid_t pid, char *const argv[]) {
    reap();
    const char *root=setting(env,"BATTY_TRANSCRIPT_DIR");
    const char *enabled=setting(env,"BATTY_TRANSCRIPT_ENABLED");
    if(!root || !*root || (enabled && !strcmp(enabled,"0"))) { errno=0; return NULL; }
    BtRecorder *r=calloc(1,sizeof(*r)); if(!r) return NULL;
    r->fd=-1;
    r->directory=strdup(root);
    if(!r->directory) { r->error=ENOMEM; return r; }
    const char *limit=setting(env,"BATTY_TRANSCRIPT_LIMIT"),*policy=setting(env,"BATTY_TRANSCRIPT_GRAPHICS");
    if(!limit || !*limit) limit="8388608";
    if(!policy || !*policy) policy="elide";
    unsigned slot=0; while(slot<128 && workers[slot]) ++slot;
    if(slot==128) { r->error=ENOSPC; return r; }
    char program[PATH_MAX]; const char *slash=helper?strrchr(helper,'/'):NULL;
    if(!slash || (size_t)(slash-helper)+sizeof("/batty-transcript")>sizeof(program)) { r->error=EINVAL; return r; }
    memcpy(program,helper,(size_t)(slash-helper)); strcpy(program+(slash-helper),"/batty-transcript");
    uint64_t random[2];
    if(getrandom(random,sizeof(random),0)!=(ssize_t)sizeof(random)) { r->error=errno?errno:EIO; return r; }
    snprintf(r->name,sizeof(r->name),"%016llx%016llx",(unsigned long long)random[0],(unsigned long long)random[1]);
    int pair[2];
    if(socketpair(AF_UNIX,SOCK_SEQPACKET|SOCK_CLOEXEC,0,pair)) { r->error=errno; return r; }
    r->fd=pair[0];
    int size=256*1024;
    if(setsockopt(r->fd,SOL_SOCKET,SO_SNDBUF,&size,sizeof(size)) || fcntl(r->fd,F_SETFL,O_NONBLOCK)<0) {
        int error=errno; close(pair[1]); failed(r,error); return r;
    }
    int child=fcntl(pair[1],F_DUPFD_CLOEXEC,64); close(pair[1]);
    if(child<0) { failed(r,errno); return r; }
    /* Reserve a descriptor before spawn, and mask Bash's SIGCHLD handler until
     * the child's pidfd is acquired. The child restores an empty signal mask. */
    int reserve=(int)syscall(SYS_pidfd_open,getpid(),0);
    if(reserve<0) { close(child); failed(r,errno); return r; }
    sigset_t blocked,previous,empty,defaults;
    sigemptyset(&blocked); sigaddset(&blocked,SIGCHLD); sigemptyset(&empty); sigfillset(&defaults);
    sigprocmask(SIG_BLOCK,&blocked,&previous);
    posix_spawn_file_actions_t actions; posix_spawnattr_t attrs;
    bool actions_ok=false,attrs_ok=false; int error=posix_spawn_file_actions_init(&actions);
    if(error) goto done;
    actions_ok=true; error=posix_spawnattr_init(&attrs); if(error) goto done; attrs_ok=true;
#define CHECK(call) do { error=(call); if(error) goto done; } while(0)
    CHECK(posix_spawn_file_actions_adddup2(&actions,child,0));
    CHECK(posix_spawn_file_actions_addopen(&actions,1,"/dev/null",O_RDWR,0));
    CHECK(posix_spawn_file_actions_addopen(&actions,2,"/dev/null",O_RDWR,0));
    CHECK(posix_spawn_file_actions_addclosefrom_np(&actions,3));
    CHECK(posix_spawnattr_setsigdefault(&attrs,&defaults));
    CHECK(posix_spawnattr_setsigmask(&attrs,&empty));
    CHECK(posix_spawnattr_setflags(&attrs,POSIX_SPAWN_SETSIGDEF|POSIX_SPAWN_SETSIGMASK));
    char process[32]; snprintf(process,sizeof(process),"%ld",(long)pid);
    char *args[40]={program,(char *)root,r->name,(char *)limit,(char *)policy,process,"0"};
    unsigned count=0;
    while(argv && argv[count] && count<32) { args[7+count]=argv[count]; ++count; }
    if(argv && argv[count]) args[6]="1";
    workers[slot]=-1;
    error=posix_spawn(&r->worker,program,&actions,&attrs,args,env);
    close(reserve); reserve=-1;
    if(!error) {
        int pidfd=(int)syscall(SYS_pidfd_open,r->worker,0);
        if(pidfd>=0) workers[slot]=pidfd+1;
        else { error=errno; fallback[slot]=r->worker; }
    } else workers[slot]=0;
#undef CHECK
 done:
    if(attrs_ok) posix_spawnattr_destroy(&attrs);
    if(actions_ok) posix_spawn_file_actions_destroy(&actions);
    if(reserve>=0) close(reserve);
    close(child);
    sigprocmask(SIG_SETMASK,&previous,NULL);
    if(error) failed(r,error);
    return r;
}
void bt_recorder_pump(BtRecorder *r, bool eof) {
    reap(); if(!r || r->fd<0) return;
    unsigned char status[5];
    ssize_t n=recv(r->fd,status,sizeof(status),MSG_DONTWAIT);
    if(n>=0) {
        if(n!=4) { failed(r,EPROTO); return; }
        unsigned error=0; for(unsigned i=0;i<4;++i) error|=(unsigned)status[i]<<(8*i);
        if(error || !r->ending) { failed(r,error && error<=4095?(int)error:EPROTO); return; }
        r->complete=true; close(r->fd); r->fd=-1; return;
    }
    if(errno!=EAGAIN && errno!=EWOULDBLOCK && errno!=EINTR) { failed(r,errno); return; }
    if(eof && !r->ending) {
        if(send(r->fd,"E",1,MSG_DONTWAIT|MSG_NOSIGNAL)!=1) { failed(r,errno); return; }
        r->ending=true;
    }
}
void bt_recorder_write(BtRecorder *r, const void *data, size_t length) {
    if(!r || r->fd<0 || r->ending) return;
    bt_recorder_pump(r,false);
    const unsigned char *bytes=data;
    while(length && r->fd>=0) {
        size_t n=length>16384?16384:length;
        struct iovec parts[2]={{.iov_base="D",.iov_len=1},{.iov_base=(void *)bytes,.iov_len=n}};
        struct msghdr message={.msg_iov=parts,.msg_iovlen=2};
        ssize_t sent=sendmsg(r->fd,&message,MSG_DONTWAIT|MSG_NOSIGNAL);
        if(sent!=(ssize_t)n+1) { failed(r,sent<0?errno:EIO); return; }
        r->bytes+=n; bytes+=n; length-=n;
    }
}
void bt_recorder_close(BtRecorder *r) {
    if(!r) { reap(); return; }
    /* Session closure before PTY EOF is an interrupted stream, not a complete
     * transcript. Already queued data drains after this endpoint closes. */
    bt_recorder_pump(r,false);
    if(r->fd>=0) close(r->fd);
    free(r->directory);
    free(r); reap();
}
