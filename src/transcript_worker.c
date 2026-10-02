/* SPDX-License-Identifier: MIT */
#define _GNU_SOURCE
#include "transcript_filter.h"
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdbool.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

/* Internal worker protocol on stdin (Unix SOCK_SEQPACKET): D + <=65536 raw
 * PTY bytes, or E alone for orderly completion. EOF without E records an
 * incomplete stream. Reply is a four-byte little-endian errno, zero on success.
 * The owner must stop recording permanently if it cannot enqueue a packet;
 * silently dropping/resuming raw chunks could corrupt graphics scan state. */
typedef struct {
    int directory,fd;
    char name[64],temporary[80];
    size_t bytes,limit,used;
    unsigned char buffer[8192];
} Writer;
static int write_all(int fd, const void *bytes, size_t length) {
    const unsigned char *p=bytes;
    while(length) {
        ssize_t n=write(fd,p,length);
        if(n<0 && errno==EINTR) continue;
        if(n<=0) { if(!n) errno=EIO; return -1; }
        p+=n; length-=(size_t)n;
    }
    return 0;
}
static void quoted(FILE *out, const char *text, size_t limit) {
    fputc('"',out);
    for(size_t i=0;text[i] && i<limit;++i) {
        unsigned char c=(unsigned char)text[i];
        if(c=='"' || c=='\\') { fputc('\\',out); fputc(c,out); }
        else if(c<32 || c>=127) fprintf(out,"\\u%04x",c);
        else fputc(c,out);
    }
    fputc('"',out);
}
/* Metadata is a bounded two-record journal. Its inode stays locked for the
 * entire worker lifetime, independently of log rotation. Command/cwd strings
 * encode bytes as Latin-1 JSON codepoints so arbitrary argv bytes round-trip. */
static int metadata(int fd, int argc, char **argv) {
    char cwd[PATH_MAX]; if(!getcwd(cwd,sizeof(cwd))) cwd[0]=0;
    char *bytes=NULL; size_t size=0; FILE *out=open_memstream(&bytes,&size);
    if(!out) return -1;
    long pid=argc>=7?strtol(argv[5],NULL,10):0;
    bool truncated=argc>=7 && strcmp(argv[6],"0");
    fprintf(out,"{\"version\":1,\"id\":\"%s\",\"started\":%lld,\"pid\":%ld,\"cwd_bytes\":",argv[2],(long long)time(NULL),pid);
    quoted(out,cwd,sizeof(cwd)); fputs(",\"argv_bytes\":[",out);
    for(int i=7;i<argc && i<39;++i) {
        if(i>7) fputc(',',out);
        if(strlen(argv[i])>256) truncated=true;
        quoted(out,argv[i],256);
    }
    fprintf(out,"],\"truncated\":%s}\n",truncated?"true":"false");
    int rc=fclose(out);
    if(!rc) rc=write_all(fd,bytes,size);
    free(bytes); return rc;
}
static int root(const char *path) {
    if(!path || *path!='/' || strlen(path)>=PATH_MAX || !path[1]) { errno=EINVAL; return -1; }
    char copy[PATH_MAX]; strcpy(copy,path);
    char *save=NULL;
    for(char *part=strtok_r(copy,"/",&save);part;part=strtok_r(NULL,"/",&save))
        if(!strcmp(part,".") || !strcmp(part,"..")) { errno=EINVAL; return -1; }
    strcpy(copy,path); save=NULL;
    const char *option=getenv("BATTY_TRANSCRIPT_CREATE_ROOT");
    bool create=option && !strcmp(option,"1");
    int fd=open("/",O_RDONLY|O_DIRECTORY|O_CLOEXEC);
    if(fd<0) return -1;
    for(char *part=strtok_r(copy,"/",&save);part;part=strtok_r(NULL,"/",&save)) {
        int next=openat(fd,part,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
        if(next<0 && errno==ENOENT && create) {
            if(mkdirat(fd,part,0700)<0 && errno!=EEXIST) { int error=errno; close(fd); errno=error; return -1; }
            if(fsync(fd)) { int error=errno; close(fd); errno=error; return -1; }
            next=openat(fd,part,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
        }
        int error=errno; close(fd); fd=next; errno=error;
        if(fd<0) return -1;
    }
    struct stat st;
    if(fstat(fd,&st) || st.st_uid!=geteuid() || (st.st_mode&07777)!=0700) { close(fd); errno=EPERM; return -1; }
    return fd;
}
static int owned(Writer *w) {
    struct stat a,b;
    if(fstat(w->fd,&a) || fstatat(w->directory,w->name,&b,AT_SYMLINK_NOFOLLOW)) return -1;
    if(a.st_dev!=b.st_dev || a.st_ino!=b.st_ino) { errno=ESTALE; return -1; }
    return 0;
}
static int flush(Writer *w) {
    if(!w->used) return 0;
    if(owned(w)) return -1;
    size_t n=w->used; const unsigned char *bytes=w->buffer;
    if(n>w->limit) { bytes+=n-w->limit; n=w->limit; }
    if(w->bytes+n>w->limit) {
        size_t budget=w->limit-w->limit/4;
        size_t keep=budget>n?budget-n:0;
        if(keep>w->bytes) keep=w->bytes;
        int temporary=openat(w->directory,w->temporary,O_RDWR|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);
        if(temporary<0) return -1;
        int rc=fchmod(temporary,0600);
        unsigned char copy[8192]; size_t offset=w->bytes-keep,remaining=keep;
        while(!rc && remaining) {
            size_t wanted=remaining<sizeof(copy)?remaining:sizeof(copy);
            ssize_t got=pread(w->fd,copy,wanted,(off_t)offset);
            if(got<0 && errno==EINTR) continue;
            if(got<=0) { if(!got) errno=EIO; rc=-1; break; }
            rc=write_all(temporary,copy,(size_t)got); offset+=(size_t)got; remaining-=(size_t)got;
        }
        if(!rc) rc=write_all(temporary,bytes,n);
        if(!rc) rc=fsync(temporary);
        if(!rc) rc=owned(w);
        if(!rc) rc=renameat(w->directory,w->temporary,w->directory,w->name);
        int error=errno;
        if(rc) { close(temporary); unlinkat(w->directory,w->temporary,0); errno=error; return -1; }
        close(w->fd);
        w->fd=temporary;
        if(fsync(w->directory)) return -1;
        w->bytes=keep+n;
    } else {
        if(write_all(w->fd,bytes,n)) return -1;
        w->bytes+=n;
    }
    w->used=0; return 0;
}
static int sink(void *data, const void *bytes, size_t length) {
    Writer *w=data; const unsigned char *p=bytes;
    while(length) {
        size_t n=sizeof(w->buffer)-w->used; if(n>length) n=length;
        memcpy(w->buffer+w->used,p,n); w->used+=n; p+=n; length-=n;
        if(w->used==sizeof(w->buffer) && flush(w)) return -1;
    }
    return 0;
}
int main(int argc, char **argv) {
    signal(SIGXFSZ,SIG_IGN);
    umask(0077);
    Writer w={.directory=-1,.fd=-1}; int error=EINVAL,meta=-1;
    bool initialized=false;
    if((argc!=5 && (argc<7 || argc>39)) || !*argv[2] || strlen(argv[2])>48 || argv[2][0]=='.' ||
       (strcmp(argv[4],"keep") && strcmp(argv[4],"elide"))) goto done;
    for(const char *p=argv[2];*p;++p)
        if(!((*p>='a' && *p<='z') || (*p>='A' && *p<='Z') || (*p>='0' && *p<='9') || *p=='_' || *p=='-' || *p=='.')) goto done;
    if(!*argv[3]) goto done;
    for(const char *p=argv[3];*p;++p) if(*p<'0' || *p>'9') goto done;
    errno=0; unsigned long limit=strtoul(argv[3],NULL,10);
    if(errno || limit<4096 || limit>128u*1024u*1024u) goto done;
    int type=0; socklen_t type_size=sizeof(type);
    if(getsockopt(0,SOL_SOCKET,SO_TYPE,&type,&type_size) || type!=SOCK_SEQPACKET) goto done;
    w.limit=(size_t)limit;
    w.directory=root(argv[1]); if(w.directory<0) { error=errno; goto done; }
    snprintf(w.name,sizeof(w.name),"%s.log",argv[2]);
    snprintf(w.temporary,sizeof(w.temporary),".%s.rotate-%ld",argv[2],(long)getpid());
    w.fd=openat(w.directory,w.name,O_RDWR|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);
    if(w.fd<0) { error=errno; goto done; }
    if(fchmod(w.fd,0600)) { error=errno; goto done; }
    char meta_name[64]; snprintf(meta_name,sizeof(meta_name),"%s.meta",argv[2]);
    meta=openat(w.directory,meta_name,O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);
    if(meta<0 || fchmod(meta,0600) || flock(meta,LOCK_EX|LOCK_NB) || metadata(meta,argc,argv)) { error=errno; goto done; }
    initialized=true;
    BtTranscriptFilter filter; bt_transcript_filter_init(&filter,!strcmp(argv[4],"keep"),sink,&w);
    bool complete=false; error=0;
    for(;;) {
        unsigned char packet[65537]; struct iovec iov={packet,sizeof(packet)};
        struct msghdr message={.msg_iov=&iov,.msg_iovlen=1};
        ssize_t n=recvmsg(0,&message,0);
        if(n<0 && errno==EINTR) continue;
        if(n<0) { error=errno; break; }
        if(!n) { error=ECANCELED; break; }
        if(message.msg_flags&(MSG_TRUNC|MSG_CTRUNC)) { error=EMSGSIZE; break; }
        if(n==1 && packet[0]=='E') { complete=true; break; }
        if(packet[0]!='D' || n==1) { error=EPROTO; break; }
        if(bt_transcript_filter_write(&filter,packet+1,(size_t)n-1) || flush(&w)) { error=errno?errno:EIO; break; }
    }
    if(!filter.failed) {
        if(bt_transcript_filter_finish(&filter)) error=errno?errno:EIO;
        if(!complete) {
            const char marker[]="\r\n[batty: transcript interrupted]\r\n";
            if(sink(&w,marker,sizeof(marker)-1)) error=errno?errno:EIO;
        }
        if(flush(&w) || owned(&w) || fsync(w.fd) || fsync(w.directory)) error=errno?errno:EIO;
    }
done:
    /* A failed metadata setup must not pair our new log with unrelated existing
     * metadata. Remove only the log inode this worker exclusively created. */
    if(!initialized && w.fd>=0 && !owned(&w)) (void)unlinkat(w.directory,w.name,0);
    if(initialized) {
        char final[128]; int n=snprintf(final,sizeof(final),"{\"ended\":%lld,\"error\":%d}\n",(long long)time(NULL),error);
        if(write_all(meta,final,(size_t)n) || fsync(meta)) error=errno?errno:EIO;
    }
    if(meta>=0) close(meta);
    if(w.fd>=0) close(w.fd);
    if(w.directory>=0) close(w.directory);
    unsigned char status[4];
    for(unsigned i=0;i<4;++i) status[i]=(unsigned char)((unsigned)error>>(8*i));
    (void)send(0,status,sizeof(status),MSG_NOSIGNAL);
    return error?1:0;
}
