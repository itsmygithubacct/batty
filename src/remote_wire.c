/* SPDX-License-Identifier: MIT */
#define _GNU_SOURCE
#include "remote_internal.h"
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

int bt_wire_error(char *out, size_t capacity, const char *what, int error) {
    if (out && capacity) snprintf(out, capacity, "%s: %s", what, strerror(error));
    errno = error;
    return -1;
}
void bt_wire_packet(BtPacket *p, uint32_t type) {
    memset(p, 0, BT_WIRE_HEADER);
    p->magic=BT_WIRE_MAGIC; p->version=BT_WIRE_VERSION; p->type=type;
}
bool bt_wire_name(const char *name) {
    if (!name || !*name || *name=='.' || strlen(name)>48) return false;
    for (const unsigned char *p=(const unsigned char *)name; *p; ++p)
        if (!((*p>='a' && *p<='z') || (*p>='A' && *p<='Z') ||
              (*p>='0' && *p<='9') || *p=='_' || *p=='-' || *p=='.')) return false;
    return true;
}
int bt_wire_root(const char *path, bool create) {
    if (!path || path[0]!='/' || strlen(path)>=PATH_MAX || !path[1]) { errno=EINVAL; return -1; }
    char copy[PATH_MAX]; strcpy(copy,path);
    int fd=open("/",O_RDONLY|O_DIRECTORY|O_CLOEXEC);
    if (fd<0) return -1;
    char *save=NULL, *component=strtok_r(copy,"/",&save);
    while (component) {
        char *next=strtok_r(NULL,"/",&save);
        if (!strcmp(component,".") || !strcmp(component,"..")) { close(fd); errno=EINVAL; return -1; }
        if (!next && create && mkdirat(fd,component,0700)<0 && errno!=EEXIST) {
            int e=errno; close(fd); errno=e; return -1;
        }
        int child=openat(fd,component,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
        int e=errno; close(fd); fd=child; errno=e;
        if (fd<0) return -1;
        component=next;
    }
    struct stat st;
    if (fstat(fd,&st)<0 || st.st_uid!=geteuid() || (st.st_mode&0777)!=0700) {
        close(fd); errno=EPERM; return -1;
    }
    return fd;
}
int bt_wire_address(int root, const char *name, char *out, size_t capacity) {
    if (!bt_wire_name(name)) { errno=EINVAL; return -1; }
    int n=snprintf(out,capacity,"/proc/self/fd/%d/%s.sock",root,name);
    if (n<0 || (size_t)n>=capacity) { errno=ENAMETOOLONG; return -1; }
    return 0;
}
int bt_wire_peer(int fd) {
    struct ucred peer; socklen_t n=sizeof(peer);
    if (getsockopt(fd,SOL_SOCKET,SO_PEERCRED,&peer,&n)<0) return -1;
    if (n!=sizeof(peer) || peer.uid!=geteuid()) { errno=EPERM; return -1; }
    return 0;
}
int bt_wire_connect(const char *path, const char *name) {
    int root=bt_wire_root(path,false);
    if (root<0) return -1;
    struct sockaddr_un address={.sun_family=AF_UNIX};
    char leaf[64]; struct stat st;
    if (!bt_wire_name(name)) { close(root); errno=EINVAL; return -1; }
    snprintf(leaf,sizeof(leaf),"%s.sock",name);
    if (fstatat(root,leaf,&st,AT_SYMLINK_NOFOLLOW)<0) {
        int e=errno; close(root); errno=e; return -1;
    }
    if (!S_ISSOCK(st.st_mode) || st.st_uid!=geteuid() || (st.st_mode&0777)!=0600) {
        close(root); errno=EPERM; return -1;
    }
    if (bt_wire_address(root,name,address.sun_path,sizeof(address.sun_path))) {
        int e=errno; close(root); errno=e; return -1;
    }
    int fd=socket(AF_UNIX,SOCK_SEQPACKET|SOCK_CLOEXEC|SOCK_NONBLOCK,0);
    if (fd>=0 && connect(fd,(struct sockaddr *)&address,sizeof(address))<0) {
        int e=errno; close(fd); fd=-1; errno=e;
    }
    int e=errno; close(root); errno=e;
    if (fd>=0 && bt_wire_peer(fd)<0) { e=errno; close(fd); fd=-1; errno=e; }
    return fd;
}
int bt_wire_send(int fd, const BtPacket *p, int passed) {
    if (p->length>BT_WIRE_CHUNK) { errno=EMSGSIZE; return -1; }
    struct iovec iov={(void *)p,BT_WIRE_HEADER+p->length};
    union { struct cmsghdr align; char data[CMSG_SPACE(sizeof(int))]; } ancillary;
    struct msghdr message={.msg_iov=&iov,.msg_iovlen=1};
    if (passed>=0) {
        memset(&ancillary,0,sizeof(ancillary));
        message.msg_control=ancillary.data; message.msg_controllen=sizeof(ancillary.data);
        struct cmsghdr *c=CMSG_FIRSTHDR(&message);
        c->cmsg_level=SOL_SOCKET; c->cmsg_type=SCM_RIGHTS; c->cmsg_len=CMSG_LEN(sizeof(int));
        memcpy(CMSG_DATA(c),&passed,sizeof(passed));
    }
    ssize_t n=sendmsg(fd,&message,MSG_NOSIGNAL|MSG_DONTWAIT);
    if (n<0) return -1;
    if ((size_t)n!=iov.iov_len) { errno=EIO; return -1; }
    return 0;
}
int bt_wire_receive(int fd, BtPacket *p, int *passed) {
    *passed=-1;
    union { struct cmsghdr align; char data[CMSG_SPACE(8*sizeof(int))]; } ancillary;
    struct iovec iov={p,sizeof(*p)};
    struct msghdr message={.msg_iov=&iov,.msg_iovlen=1,
        .msg_control=ancillary.data,.msg_controllen=sizeof(ancillary.data)};
    ssize_t n=recvmsg(fd,&message,MSG_DONTWAIT|MSG_CMSG_CLOEXEC);
    if (n<0) return -1;
    bool bad=(message.msg_flags&(MSG_TRUNC|MSG_CTRUNC))!=0;
    for (struct cmsghdr *c=CMSG_FIRSTHDR(&message); c; c=CMSG_NXTHDR(&message,c)) {
        if (c->cmsg_level!=SOL_SOCKET || c->cmsg_type!=SCM_RIGHTS || c->cmsg_len<CMSG_LEN(0)) { bad=true; continue; }
        size_t count=(c->cmsg_len-CMSG_LEN(0))/sizeof(int);
        for (size_t i=0; i<count; ++i) {
            int value; memcpy(&value,CMSG_DATA(c)+i*sizeof(int),sizeof(value));
            if (*passed<0) *passed=value;
            else { close(value); bad=true; }
        }
    }
    if (n==0 && !bad && *passed<0) return 0;
    if (bad || n<(ssize_t)BT_WIRE_HEADER || p->magic!=BT_WIRE_MAGIC ||
        p->version!=BT_WIRE_VERSION || p->reserved || p->length>BT_WIRE_CHUNK ||
        (size_t)n!=BT_WIRE_HEADER+p->length) {
        if (*passed>=0) close(*passed);
        *passed=-1; errno=EPROTO; return -1;
    }
    return 1;
}
int bt_wire_wait(int fd, short events, uint64_t deadline) {
    for (;;) {
        uint64_t now=bt_millis();
        if (now>=deadline) { errno=ETIMEDOUT; return -1; }
        uint64_t left=deadline-now;
        struct pollfd p={fd,events,0};
        int rc=poll(&p,1,left>1000?1000:(int)left);
        if (rc<0 && errno==EINTR) continue;
        if (rc<0) return -1;
        if (rc && p.revents&(events|POLLHUP|POLLERR)) return 0;
    }
}
int bt_wire_blob(const void *data, size_t length) {
    int fd=memfd_create("batty-frame",MFD_CLOEXEC|MFD_ALLOW_SEALING);
    if (fd<0) return -1;
    size_t done=0;
    while (done<length) {
        ssize_t n=write(fd,(const uint8_t *)data+done,length-done);
        if (n<0 && errno==EINTR) continue;
        if (n<=0) { int e=n<0?errno:EIO; close(fd); errno=e; return -1; }
        done+=(size_t)n;
    }
    if (fcntl(fd,F_ADD_SEALS,F_SEAL_WRITE|F_SEAL_GROW|F_SEAL_SHRINK|F_SEAL_SEAL)<0) {
        int e=errno; close(fd); errno=e; return -1;
    }
    return fd;
}
void *bt_wire_map(int fd, size_t length, size_t maximum) {
    struct stat st;
    const int required=F_SEAL_WRITE|F_SEAL_GROW|F_SEAL_SHRINK|F_SEAL_SEAL;
    int seals=fcntl(fd,F_GET_SEALS);
    if (!length || length>maximum || fstat(fd,&st)<0 || !S_ISREG(st.st_mode) ||
        st.st_size<0 || (uint64_t)st.st_size!=length ||
        seals<0 || (seals&required)!=required) { errno=EINVAL; return MAP_FAILED; }
    return mmap(NULL,length,PROT_READ,MAP_PRIVATE,fd,0);
}
