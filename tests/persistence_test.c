/* SPDX-License-Identifier: MIT */
/* Run from the project root on the isolated X server created by tests/run.py.
 * The executable is also its own raw-PTY fixture. Progress files provide a
 * parser barrier without adding bytes inside unfinished graphics sequences. */
#define _GNU_SOURCE
#include "window.h"
#include "remote.h"
#include "remote_internal.h"
#include "presentation.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <termios.h>
#include <unistd.h>

extern char **environ;
typedef struct {
    uint64_t bytes, step;
    unsigned queries;
    pid_t child, descendant;
    int command, failure;
} Progress;

static char root[PATH_MAX], self[PATH_MAX], helper[PATH_MAX], service[PATH_MAX];
static char progress_path[PATH_MAX], error[256];
static BtSession clients[4];
static BtWindow window;
static const char *owned_names[8];
static unsigned owned_count, assertions;
static bool window_open;
static Progress progress;
static int raw_observer=-1;

static void nap(void) {
    struct pollfd unused={.fd=-1};
    (void)poll(&unused,0,2);
}
static bool write_all(int fd,const void *data,size_t length) {
    const uint8_t *p=data;
    while(length) {
        ssize_t n=write(fd,p,length);
        if(n<0 && errno==EINTR) continue;
        if(n<=0) return false;
        p+=n; length-=(size_t)n;
    }
    return true;
}
static void cleanup(void) {
    if(raw_observer>=0) { close(raw_observer); raw_observer=-1; }
    if(window_open) { bt_window_close(&window); window_open=false; }
    for(unsigned i=0;i<4;i++) if(clients[i].remote) bt_session_close(&clients[i]);
    for(unsigned i=0;i<owned_count;i++) {
        char ignored[256];
        (void)bt_remote_terminate(root,owned_names[i],ignored,sizeof(ignored));
    }
    /* Only fixture files and empty directories directly below our mkdtemp
     * root are removed. A live/unknown session directory is left visible. */
    DIR *dir=*root?opendir(root):NULL;
    if(dir) {
        struct dirent *entry;
        while((entry=readdir(dir))) {
            if(!strcmp(entry->d_name,".") || !strcmp(entry->d_name,"..")) continue;
            struct stat st;
            if(fstatat(dirfd(dir),entry->d_name,&st,AT_SYMLINK_NOFOLLOW)) continue;
            if(S_ISSOCK(st.st_mode)) continue; /* Keep failed service cleanup visible. */
            (void)unlinkat(dirfd(dir),entry->d_name,S_ISDIR(st.st_mode)?AT_REMOVEDIR:0);
        }
        closedir(dir);
    }
    if(*root && rmdir(root) && errno!=ENOENT)
        fprintf(stderr,"persistence cleanup retained private root: %s\n",root);
    while(waitpid(-1,NULL,WNOHANG)>0) {}
}
static void require(bool ok,const char *message) {
    assertions++;
    if(ok) return;
    fprintf(stderr,"FAIL persistence: %s (errno=%d %s)\nremote: %s\nwindow: %s\n",
            message,errno,strerror(errno),error,window.error);
    for(unsigned i=0;i<4;i++) if(*clients[i].error)
        fprintf(stderr,"client %u: %s\n",i,clients[i].error);
    exit(1);
}
static void path(char *out,size_t size,const char *base,const char *name) {
    int n=snprintf(out,size,"%s/%s",base,name);
    require(n>0 && (size_t)n<size,"private fixture path fits");
}
static void endpoint_path(char *out,size_t size,const char *name) {
    int n=snprintf(out,size,"%s/%s.sock",root,name);
    require(n>0 && (size_t)n<size,"private socket path fits");
}

/* Child-only helpers deliberately avoid the terminal/parser API. */
static Progress child_progress;
static const char *child_path;
static void child_mark(int command,int failure) {
    child_progress.command=command;
    child_progress.failure=failure;
    child_progress.step++;
    char temporary[PATH_MAX];
    int n=snprintf(temporary,sizeof(temporary),"%s.tmp",child_path);
    if(n<=0 || (size_t)n>=sizeof(temporary)) _exit(90);
    int fd=open(temporary,O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC|O_NOFOLLOW,0600);
    if(fd<0 || !write_all(fd,&child_progress,sizeof(child_progress))) _exit(91);
    if(close(fd) || rename(temporary,child_path)) _exit(92);
}
static void emit(const char *format,...) {
    char bytes[1024];
    va_list ap;
    va_start(ap,format);
    int n=vsnprintf(bytes,sizeof(bytes),format,ap);
    va_end(ap);
    if(n<0 || (size_t)n>=sizeof(bytes) || !write_all(STDOUT_FILENO,bytes,(size_t)n)) _exit(93);
    child_progress.bytes+=(size_t)n;
}
static bool child_read_for(void *out,size_t length,unsigned timeout_ms) {
    uint8_t *bytes=out;
    uint64_t until=bt_millis()+timeout_ms;
    while(length && bt_millis()<until) {
        struct pollfd p={.fd=STDIN_FILENO,.events=POLLIN};
        int ready=poll(&p,1,50);
        if(ready<0 && errno==EINTR) continue;
        if(ready<=0) continue;
        ssize_t n=read(STDIN_FILENO,bytes,length);
        if(n<0 && errno==EINTR) continue;
        if(n<=0) return false;
        bytes+=n; length-=(size_t)n;
    }
    return !length;
}
static bool child_read(void *out,size_t length) { return child_read_for(out,length,5000); }
static bool query(void) {
    static const char expected[]="\033[2;4R";
    char reply[sizeof(expected)-1];
    emit("\033[2;4H\033[6n");
    if(!child_read(reply,sizeof(reply)) || memcmp(reply,expected,sizeof(reply))) return false;
    child_progress.queries++;
    return true;
}
static void screen(const char *label) {
    emit("\033c\033[?25l\033]2;%s\007\033[H%s\033[3;3H",label,label);
}
static void red(unsigned id,const char *action) {
    /* A 1x1 RGBA red image, scaled to four columns by two rows. */
    emit("\033_Ga=%s,f=32,s=1,v=1,i=%u,c=4,r=2,C=1,q=2;/wAA/w==\033\\",action,id);
}
static int child_main(const char *kind,const char *filename) {
    struct termios tty;
    if(tcgetattr(STDIN_FILENO,&tty)) return 80;
    cfmakeraw(&tty);
    if(tcsetattr(STDIN_FILENO,TCSANOW,&tty)) return 81;
    child_path=filename;
    child_progress.child=getpid();
    if(!strcmp(kind,"descendants") || !strcmp(kind,"orphan")) {
        int ready[2];
        if(pipe2(ready,O_CLOEXEC)) return 82;
        pid_t pid=fork();
        if(pid<0) return 83;
        if(!pid) {
            close(ready[0]);
            if(setsid()<0) _exit(84);
            signal(SIGHUP,SIG_IGN);
            signal(SIGTERM,SIG_IGN);
            if(!write_all(ready[1],"R",1)) _exit(85);
            close(ready[1]);
            if(!strcmp(kind,"orphan")) {
                child_progress.descendant=getpid();
                child_progress.step=1; /* Original writes the first ready record. */
                char command;
                if(!child_read_for(&command,1,30000) || command!='Q') {
                    child_mark('O',6); _exit(86);
                }
                emit("\033[HORPHAN_OUTPUT");
                if(!query()) { child_mark('O',7); _exit(87); }
                child_mark('O',0);
            }
            for(;;) pause();
        }
        close(ready[1]);
        char ready_byte;
        if(read(ready[0],&ready_byte,1)!=1) return 86;
        close(ready[0]);
        child_progress.descendant=pid;
        child_mark('R',0);
        if(!strcmp(kind,"orphan")) return 23;
        for(;;) pause();
    }
    /* No controller exists yet. The service must already own parsing/replies. */
    if(!query()) { child_mark('R',1); return 87; }
    emit("\033[HDETACHED_QUERY_OK\033[?25l");
    child_mark('R',0);
    for(;;) {
        char command;
        if(!child_read_for(&command,1,30000)) { child_mark('?',2); return 88; }
        switch(command) {
        case 'N': break; /* A replayed query reply would be an invalid command. */
        case 'U': screen("UPLOADED_UNPLACED"); red(401,"t"); break;
        case 'P':
            emit("\033[3;3H\033_Ga=p,i=401,p=1,c=4,r=2,C=1,q=2;\033\\");
            break;
        case 'D': screen("PRIMARY_SURVIVES"); red(301,"T"); break;
        case 'A':
            emit("\033[?1049h\033[2J\033[H\033[?25lALTERNATE_SURVIVES\033[3;3H"
                 "\033_Ga=T,f=32,s=1,v=1,i=301,c=4,r=2,C=1,q=2;AP8A/w==\033\\");
            break;
        case 'B': emit("\033[?1049l\033[?25l"); break;
        case 'K':
            screen("KITTY_PARTIAL");
            emit("\033_Ga=T,f=32,s=2,v=1,i=402,c=4,r=2,C=1,q=2,m=1;/wAA/w==\033\\");
            break;
        case 'L':
            emit("\033_Gm=0;AP8A/w==\033\\\033[HKITTY_FINISHED");
            break;
        case 'V':
            screen("SIXEL_PARTIAL");
            emit("\033[?1070l\033P0;1q\"1;1;8;6#1;2;100;0;0#1!8~\033\\"
                 "\033[6;3H\033P0;1q\"1;1;8;6#1!4~");
            break;
        case 'W': emit("!4~\033\\\033[HSIXEL_FINISHED"); break;
        case 'F': {
            char line[4096];
            memset(line,'x',sizeof(line));
            for(unsigned i=0;i<512;i++) {
                if(!write_all(STDOUT_FILENO,line,sizeof(line))) return 89;
                child_progress.bytes+=sizeof(line);
            }
            emit("\033[2J\033[HFLOOD_DONE");
            if(!query()) { child_mark(command,3); return 89; }
            break;
        }
        case 'J': emit("\033[?2004h"); break;
        case 'I': {
            static const char expected[]="\033[200~paste-test\033[201~";
            char reply[sizeof(expected)-1];
            if(!child_read(reply,sizeof(reply)) || memcmp(reply,expected,sizeof(reply))) {
                child_mark(command,4); return 89;
            }
            emit("\033[HPASTE_ACCEPTED");
            break;
        }
        case 'X': emit("\033[HEXIT_SEVEN"); child_mark(command,0); return 7;
        default: child_mark((unsigned char)command,5); return 89;
        }
        child_mark(command,0);
    }
}

static Progress read_progress(const char *filename) {
    Progress value={0};
    int fd=open(filename,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);
    if(fd<0) return value;
    ssize_t n=read(fd,&value,sizeof(value));
    close(fd);
    if(n!=(ssize_t)sizeof(value)) memset(&value,0,sizeof(value));
    return value;
}
static Progress await_progress(const char *filename,uint64_t before,int command) {
    uint64_t until=bt_millis()+6000;
    Progress value={0};
    do {
        value=read_progress(filename);
        if(value.step>before) {
            if(value.failure || value.command!=command)
                fprintf(stderr,"fixture command=%d wanted=%d failure=%d step=%llu\n",
                        value.command,command,value.failure,(unsigned long long)value.step);
            require(!value.failure && value.command==command,"fixture completed the intended command without extra terminal replies");
            return value;
        }
        nap();
    } while(bt_millis()<until);
    require(false,"fixture progress deadline");
    return value;
}
static void parsed(BtSession *s,uint64_t bytes) {
    uint64_t until=bt_millis()+6000;
    do {
        int result=bt_session_pump(s,5);
        require(result>=0,"pump complete current presentation");
        if(s->bytes_read>=bytes) {
            require(s->presentation && s->presentation->epoch && s->presentation->revision,
                    "parsed output has an owned versioned presentation");
            return;
        }
    } while(bt_millis()<until);
    fprintf(stderr,"parser bytes=%llu wanted=%llu\n",
            (unsigned long long)s->bytes_read,(unsigned long long)bytes);
    require(false,"parser processed fixture output before disconnect");
}
static void attach(BtSession *s,bool observe) {
    require(!s->remote,"attach uses a fresh client");
    require(bt_remote_attach(s,root,"main",observe)==0,"attach named live service");
    require(s->presentation!=NULL,"attach immediately supplies complete presentation");
    require(bt_remote_observer(s)==observe,"attachment role matches request");
}
static void detach(BtSession *s) {
    bt_session_close(s);
    require(!s->remote && !s->presentation,"detach releases only frontend ownership");
}
static void command(BtSession *s,char command_byte) {
    uint64_t before=progress.step;
    require(bt_session_send(s,&command_byte,1)==0,"send fixture input through controller");
    progress=await_progress(progress_path,before,command_byte);
    parsed(s,progress.bytes);
}
static bool frame_contains(const BtPresentation *p,const char *expected) {
    if(!p) return false;
    for(unsigned row=0;row<p->rows;row++) {
        char text[4096];
        size_t used=0;
        for(unsigned col=0;col<p->cols;col++) {
            const BtPresentationCell *cell=&p->cells[(size_t)row*p->cols+col];
            if(!cell->length && used<sizeof(text)-1) text[used++]=' ';
            for(uint32_t i=0;i<cell->length && used<sizeof(text)-1;i++) {
                uint32_t cp=p->codepoints[cell->offset+i];
                text[used++]=cp>0 && cp<128?(char)cp:'?';
            }
        }
        text[used]=0;
        if(strstr(text,expected)) return true;
    }
    return false;
}
static bool contains(BtSession *s,const char *expected) {
    size_t length=0;
    char *text=bt_session_text(s,false,&length);
    bool found=text && strstr(text,expected) && frame_contains(s->presentation,expected);
    free(text);
    return found;
}
static const BtPresentationImage *find_image(const BtPresentation *p,uint32_t id) {
    for(size_t i=0;i<p->image_count;i++) if(p->images[i].id==id) return &p->images[i];
    return NULL;
}
static void image_color(const BtPresentation *p,uint32_t id,unsigned r,unsigned g,unsigned b) {
    const BtPresentationImage *img=find_image(p,id);
    require(img && img->width==1 && img->height==1 && img->channels==4 && img->length==4,
            "presentation includes placed image with exact dimensions and payload");
    require(img->pixels[0]==r && img->pixels[1]==g && img->pixels[2]==b && img->pixels[3]==255,
            "placed image pixels survive detach");
    bool placed=false;
    for(size_t i=0;i<p->placement_count;i++) if(p->placements[i].image_id==id) placed=true;
    require(placed,"image has a current placement in complete frame");
}
static void create(const char *name,const char *kind,const char *filename) {
    char *argv[]={self,"--child",(char *)kind,(char *)filename,NULL};
    require(owned_count<sizeof(owned_names)/sizeof(*owned_names),"bounded owned session registry");
    owned_names[owned_count++]=name; /* Cleanup also covers interrupted creation. */
    require(bt_remote_create(service,helper,root,name,argv,environ,80,24,8,16,error,sizeof(error))==0,
            "create persistent terminal service");
}
static void terminate(const char *name) {
    require(bt_remote_terminate(root,name,error,sizeof(error))==0,"terminate owned persistent session");
    for(unsigned i=0;i<owned_count;i++) if(!strcmp(owned_names[i],name)) {
        owned_names[i]=owned_names[--owned_count];
        break;
    }
    char endpoint[PATH_MAX];
    endpoint_path(endpoint,sizeof(endpoint),name);
    require(access(endpoint,F_OK)<0 && errno==ENOENT,"termination removes its private endpoint");
}
static void report(const char *name) { printf("PASS persistence: %s\n",name); fflush(stdout); }

static void startup_and_ownership(void) {
    char *argv[]={self,"--child","normal",progress_path,NULL};
    create("main","normal",progress_path);
    progress=await_progress(progress_path,0,'R');
    require(progress.queries==1,"query answered before the first frontend exists");
    attach(&clients[0],false);
    parsed(&clients[0],progress.bytes);
    require(contains(&clients[0],"DETACHED_QUERY_OK"),"first output survives first attachment");
    pid_t child=clients[0].child;
    require(child==progress.child,"metadata identifies the real application process");
    require(bt_remote_create(service,helper,root,"main",argv,environ,80,24,8,16,error,sizeof(error))==1,
            "duplicate create preserves existing session");
    BtSession other={0};
    require(bt_remote_attach(&other,root,"main",false)<0,"second controller is rejected");
    if(other.remote) bt_session_close(&other);
    BtWindow rejected={0};
    require(bt_window_open_persistent(&rejected,service,helper,root,"main",argv,environ,80,24,
                                      "monospace",16,true)<0,
            "existing session refuses a second persistent controller window");
    bt_window_close(&rejected);
    uint64_t epoch=clients[0].presentation->epoch;
    require(bt_remote_cancel(root,"main",epoch,error,sizeof(error))<0,
            "creation rollback cannot stop a session already claimed by a frontend");
    require(bt_remote_cancel(root,"main",epoch==1?2:1,error,sizeof(error))<0,
            "rollback cannot stop a different service epoch");
    command(&clients[0],'N');
    require(clients[0].child==child,"failed window attachment does not cancel an existing session");
    detach(&clients[0]);
    require(bt_remote_cancel(root,"main",epoch,error,sizeof(error))<0,
            "creation rollback still refuses an epoch after its frontend has detached");
    attach(&clients[0],false);
    command(&clients[0],'N');
    require(clients[0].child==child && progress.queries==1,"reattach keeps child identity without replaying DSR");
    char *listing=NULL;
    require(bt_remote_list(root,&listing,error,sizeof(error))==0 && listing && strstr(listing,"main"),
            "list discovers live named service");
    free(listing);
    struct stat st;
    require(!lstat(root,&st) && (st.st_mode&0777)==0700,"session root is private");
    char socket_path[PATH_MAX];
    endpoint_path(socket_path,sizeof(socket_path),"main");
    require(!lstat(socket_path,&st) && S_ISSOCK(st.st_mode) && st.st_uid==getuid() && (st.st_mode&0777)==0600,
            "session socket is private and user-owned");
    report("detached startup query, one controller, no replay, discovery and private endpoint");
}
static void graphics_state(void) {
    command(&clients[0],'U');
    detach(&clients[0]);
    attach(&clients[0],false);
    command(&clients[0],'P');
    image_color(clients[0].presentation,401,255,0,0);
    report("transmit-only Kitty image can be placed after all frontends disconnect");

    command(&clients[0],'D');
    image_color(clients[0].presentation,301,255,0,0);
    detach(&clients[0]);
    attach(&clients[0],false);
    command(&clients[0],'A');
    require(clients[0].presentation->screen==GHOSTTY_TERMINAL_SCREEN_ALTERNATE,
            "alternate screen identity is explicit");
    image_color(clients[0].presentation,301,0,255,0);
    detach(&clients[0]);
    attach(&clients[0],false);
    require(contains(&clients[0],"ALTERNATE_SURVIVES"),"alternate text survives reattachment");
    command(&clients[0],'B');
    require(clients[0].presentation->screen==GHOSTTY_TERMINAL_SCREEN_PRIMARY,
            "primary screen identity returns");
    require(contains(&clients[0],"PRIMARY_SURVIVES"),"primary text survives alternate-screen detachment");
    image_color(clients[0].presentation,301,255,0,0);
    report("primary and alternate text and separate image stores survive detach");

    command(&clients[0],'K');
    detach(&clients[0]);
    attach(&clients[0],false);
    command(&clients[0],'L');
    const BtPresentationImage *img=find_image(clients[0].presentation,402);
    static const uint8_t pixels[]={255,0,0,255,0,255,0,255};
    require(img && img->width==2 && img->height==1 && img->length==sizeof(pixels) &&
            !memcmp(img->pixels,pixels,sizeof(pixels)),"incomplete Kitty transfer resumes with exact pixel bytes");
    require(contains(&clients[0],"KITTY_FINISHED"),"Kitty continuation leaves parser in text state");
    report("unfinished Kitty transfer survives parser/client boundary");

    command(&clients[0],'V');
    detach(&clients[0]);
    attach(&clients[0],false);
    command(&clients[0],'W');
    const BtPresentation *p=clients[0].presentation;
    unsigned sixels=0;
    for(size_t i=0;i<p->image_count;i++) {
        img=&p->images[i];
        if(img->width!=8 || img->height!=6) continue;
        require(img->channels>=3 && img->length==8*6*img->channels,"Sixel image dimensions survive continuation");
        for(size_t j=0;j<img->length;j+=img->channels)
            require(img->pixels[j]==255 && !img->pixels[j+1] && !img->pixels[j+2],
                    "Sixel continuation retains the previously defined shared palette");
        sixels++;
    }
    require(sixels==2 && p->placement_count==2,"both complete and formerly partial Sixel images remain placed");
    require(contains(&clients[0],"SIXEL_FINISHED"),"Sixel continuation returns to text parsing");
    report("unfinished Sixel decoder and shared palette survive disconnect");
}
static int observer_request(BtPacket *packet) {
    uint64_t request=packet->request,deadline=bt_millis()+2000;
    uint32_t type=packet->type;
    while(bt_wire_send(raw_observer,packet,-1)<0) {
        if(errno==EINTR) continue;
        require((errno==EAGAIN || errno==EWOULDBLOCK) &&
                bt_wire_wait(raw_observer,POLLOUT,deadline)==0,"send bounded raw observer request");
    }
    int fd=-1;
    for(;;) {
        int result=bt_wire_receive(raw_observer,packet,&fd);
        if(result==1) break;
        if(result<0 && errno==EINTR) continue;
        require(result<0 && (errno==EAGAIN || errno==EWOULDBLOCK) &&
                bt_wire_wait(raw_observer,POLLIN,deadline)==0,"receive bounded raw observer reply");
    }
    bool matched=packet->request==request && packet->type==type;
    if(!matched && fd>=0) { close(fd); fd=-1; }
    require(matched,"raw observer reply matches request identity and operation");
    return fd;
}
static void observers_and_codec(void) {
    for(unsigned i=1;i<4;i++) attach(&clients[i],true);
    require(bt_session_send(&clients[1],"N",1)<0,"observer cannot send raw input");
    require(bt_session_resize(&clients[1],70,20,8,16)<0,"observer cannot resize authoritative terminal");
    BtIntent paste={.type=BT_INTENT_PASTE};
    require(bt_remote_intent(&clients[1],&paste,"observer",8)<0,"observer cannot send semantic input");
    /* Bypass client guards to verify the service enforces the declared role. */
    pid_t child=clients[0].child;
    unsigned cols=clients[0].cols,rows=clients[0].rows;
    raw_observer=bt_wire_connect(root,"main");
    require(raw_observer>=0,"connect an additional private raw observer");
    BtPacket packet;
    bt_wire_packet(&packet,BT_WIRE_HELLO); packet.request=1; packet.flags=2;
    int fd=observer_request(&packet);
    bool attached=!packet.error && fd>=0 && packet.blob_size>0;
    if(fd>=0) close(fd);
    require(attached,"raw observer HELLO receives a complete frame descriptor");
    const uint32_t operations[]={BT_WIRE_STOP,BT_WIRE_RESIZE};
    for(unsigned i=0;i<sizeof(operations)/sizeof(*operations);i++) {
        bt_wire_packet(&packet,operations[i]); packet.request=2+i;
        if(operations[i]==BT_WIRE_RESIZE) { packet.cols=70; packet.rows=20; packet.cw=8; packet.ch=16; }
        fd=observer_request(&packet);
        bool denied=packet.error==EPERM && fd<0 && !packet.blob_size;
        if(fd>=0) close(fd);
        require(denied,operations[i]==BT_WIRE_STOP?"service rejects STOP on an observer connection with EPERM":
                                                   "service rejects RESIZE on an observer connection with EPERM");
    }
    close(raw_observer); raw_observer=-1;
    command(&clients[0],'N');
    require(clients[0].child==child && clients[0].cols==cols && clients[0].rows==rows,
            "rejected raw observer mutations preserve the owning service and its geometry");
    uint64_t old_revision=clients[3].presentation->revision;
    uint64_t before=progress.step;
    require(bt_session_send(&clients[0],"F",1)==0,"start bounded output flood");
    uint64_t until=bt_millis()+6000;
    /* Two observers consume concurrently; the third receives nothing until
     * the fixture's query confirms parsing has reached the end of the flood. */
    do {
        require(bt_session_pump(&clients[1],0)>=0 && bt_session_pump(&clients[2],0)>=0,
                "active observers receive current frames during output pressure");
        Progress now=read_progress(progress_path);
        if(now.step>before) break;
        nap();
    } while(bt_millis()<until);
    progress=await_progress(progress_path,before,'F');
    require(progress.queries==2,"detached parser answered query after two MiB output despite idle observer");
    for(unsigned i=0;i<4;i++) {
        parsed(&clients[i],progress.bytes);
        require(contains(&clients[i],"FLOOD_DONE"),"every observer resynchronizes to the complete latest text frame");
    }
    require(clients[3].presentation->revision>old_revision,"idle observer skips obsolete frames and receives a newer revision");
    for(unsigned i=1;i<4;i++) detach(&clients[i]);
    report("two active observers and one idle observer cannot stop parsing or query replies");

    command(&clients[0],'J');
    before=progress.step;
    require(bt_session_send(&clients[0],"I",1)==0,"enter semantic paste fixture");
    require(bt_remote_intent(&clients[0],&paste,"paste-test",10)==0,
            "service encodes paste using authoritative terminal mode");
    progress=await_progress(progress_path,before,'I');
    parsed(&clients[0],progress.bytes);
    require(contains(&clients[0],"PASTE_ACCEPTED"),"owner applied bracketed-paste mode");
    report("semantic input uses modes retained by terminal owner");

    command(&clients[0],'D');
    uint8_t *packed=NULL;
    size_t length=0;
    BtPresentation *copy=NULL,*invalid=NULL;
    require(bt_presentation_pack(clients[0].presentation,&packed,&length)==0 && length>32,
            "pack complete presentation");
    require(bt_presentation_unpack(packed,length,&copy)==0 && copy,
            "unpack independent owned presentation");
    require(copy->epoch==clients[0].presentation->epoch && copy->revision==clients[0].presentation->revision &&
            copy->cell_count==clients[0].presentation->cell_count && copy->codepoint_count==clients[0].presentation->codepoint_count,
            "frame codec preserves epoch, revision, geometry and text extent");
    image_color(copy,301,255,0,0);
    size_t cuts[]={0,1,7,31,length-1};
    for(size_t i=0;i<sizeof(cuts)/sizeof(*cuts);i++) {
        invalid=(BtPresentation *)(uintptr_t)1;
        require(bt_presentation_unpack(packed,cuts[i],&invalid)<0 && !invalid,
                "codec rejects truncated frame without publishing partial ownership");
    }
    uint8_t *trailing=malloc(length+1);
    require(trailing!=NULL,"allocate bounded trailing-byte frame probe");
    memcpy(trailing,packed,length); trailing[length]=0;
    require(bt_presentation_unpack(trailing,length+1,&invalid)<0 && !invalid,
            "codec rejects data beyond the complete frame");
    free(trailing);
    packed[8]=2;
    require(bt_presentation_unpack(packed,length,&invalid)<0 && !invalid,
            "codec rejects an unsupported schema version");
    free(packed);
    BtPresentation malformed=*copy;
    malformed.cell_count--;
    packed=(uint8_t *)(uintptr_t)1; length=123;
    require(bt_presentation_pack(&malformed,&packed,&length)<0 && !packed && !length,
            "codec rejects cell counts inconsistent with geometry");
    malformed=*copy;
    require(copy->placement_count==1,"codec reference fixture has one placement");
    BtPresentationPlacement placement=copy->placements[0];
    placement.image_index=(uint32_t)copy->image_count;
    malformed.placements=&placement;
    require(bt_presentation_pack(&malformed,&packed,&length)<0 && !packed && !length,
            "codec rejects placements referring outside the owned image array");
    uint32_t first_codepoint=copy->codepoints[0];
    copy->codepoints[0]=0xd800;
    require(bt_presentation_pack(copy,&packed,&length)<0 && !packed && !length,
            "codec rejects surrogate values as Unicode scalar text");
    copy->codepoints[0]=first_codepoint;
    command(&clients[0],'A');
    image_color(copy,301,255,0,0);
    require(copy->screen==GHOSTTY_TERMINAL_SCREEN_PRIMARY,"decoded frame owns storage independent of later terminal mutation");
    bt_presentation_free(copy);
    command(&clients[0],'B');
    report("bounded frame codec roundtrip, truncation rejection and independent ownership");
}

static void verify_capture(const char *filename,unsigned cw,unsigned ch) {
    FILE *file=fopen(filename,"rb");
    require(file!=NULL,"open fresh window framebuffer capture");
    unsigned width,height,maximum;
    char magic[3]={0};
    require(fscanf(file,"%2s %u %u %u",magic,&width,&height,&maximum)==4 &&
            !strcmp(magic,"P6") && maximum==255 && width>0 && height>0 && width<=8192 && height<=8192,
            "capture is a bounded PPM framebuffer");
    require(fgetc(file)=='\n',"PPM header ends before pixels");
    size_t size=(size_t)width*height*3;
    uint8_t *pixels=malloc(size);
    require(pixels && fread(pixels,1,size,file)==size,"read complete captured framebuffer");
    fclose(file);
    unsigned x=8+4*cw,y=8+3*ch;
    require(x<width && y<height,"image sample lies within framebuffer");
    const uint8_t *sample=pixels+((size_t)y*width+x)*3;
    require(sample[0]>=253 && sample[1]<=2 && sample[2]<=2,"fresh renderer uploaded retained red image into a new GPU texture");
    unsigned ink=0;
    for(y=8;y<8+ch && y<height;y++) for(x=8;x<8+16*cw && x<width;x++) {
        sample=pixels+((size_t)y*width+x)*3;
        if(sample[0]>80 || sample[1]>80 || sample[2]>80) ink++;
    }
    free(pixels);
    require(ink>40,"fresh renderer drew retained text alongside graphics");
}
static void fresh_windows(void) {
    command(&clients[0],'D');
    pid_t child=clients[0].child;
    detach(&clients[0]);
    for(unsigned pass=0;pass<2;pass++) {
        memset(&window,0,sizeof(window));
        require(bt_window_attach(&window,root,"main","monospace",18,false,false)==0,
                "attach a newly constructed GPU window to existing service");
        window_open=true;
        parsed(&window.session,progress.bytes);
        require(window.session.child==child && contains(&window.session,"PRIMARY_SURVIVES"),
                "new window receives current child and retained text");
        require(!strcmp(bt_session_title(&window.session),"PRIMARY_SURVIVES"),
                "new window receives the retained terminal title");
        for(unsigned i=0;i<3;i++) require(bt_window_pump(&window,5)>=0,"pump newly attached GPU window");
        require(bt_renderer_draw(window.renderer,&window.session,true,false)==0,"draw complete retained presentation");
        int cw,ch;
        require(bt_renderer_metrics(window.renderer,&cw,&ch)==0 && cw>0 && ch>0,
                "new renderer retains usable local font metrics");
        const char *filename=pass?"build/persistence-second.ppm":"build/persistence-first.ppm";
        require(bt_renderer_capture(window.renderer,filename)==0,"capture freshly reconstructed GPU state");
        verify_capture(filename,window.session.cell_width,window.session.cell_height);
        bt_window_close(&window); window_open=false;
    }
    attach(&clients[0],false);
    command(&clients[0],'N');
    require(clients[0].child==child,"closing both GPU windows leaves application alive");
    report("two independent GPU windows rebuild retained text and image textures");
}
static void exit_and_failures(void) {
    command(&clients[0],'X');
    uint64_t until=bt_millis()+6000;
    while(!clients[0].done && bt_millis()<until)
        require(bt_session_pump(&clients[0],5)>=0,"pump natural child completion");
    require(clients[0].done && clients[0].exited && clients[0].eof && clients[0].exit_status==7,
            "real child exit status includes final PTY drain");
    detach(&clients[0]);
    attach(&clients[0],false);
    require(clients[0].done && clients[0].exit_status==7 && contains(&clients[0],"EXIT_SEVEN"),
            "completed session retains final text and exit status for later attach");
    detach(&clients[0]);
    terminate("main");
    report("final output and real exit status remain available until explicit termination");

    char descendants[PATH_MAX];
    path(descendants,sizeof(descendants),root,"descendants.progress");
    create("tree","descendants",descendants);
    Progress p=await_progress(descendants,0,'R');
    require(p.descendant>0 && kill(p.descendant,0)==0,"term-resistant detached descendant is alive");
    terminate("tree");
    until=bt_millis()+2000;
    while(bt_millis()<until) {
        while(waitpid(-1,NULL,WNOHANG)>0) {}
        if(kill(p.child,0)<0 && errno==ESRCH && kill(p.descendant,0)<0 && errno==ESRCH) break;
        nap();
    }
    require(kill(p.child,0)<0 && errno==ESRCH,"termination reaps real application");
    require(kill(p.descendant,0)<0 && errno==ESRCH,"termination cleans setsid descendant that ignores HUP and TERM");
    report("explicit termination waits for owned term-resistant descendants");

    path(descendants,sizeof(descendants),root,"orphan.progress");
    create("orphan","orphan",descendants);
    p=await_progress(descendants,0,'R');
    require(bt_remote_attach(&clients[0],root,"orphan",false)==0,"attach after original child exits with a live descendant");
    until=bt_millis()+4000;
    while(!clients[0].exited && bt_millis()<until)
        require(bt_session_pump(&clients[0],5)>=0,"observe original child exit independent of descendant lifetime");
    require(clients[0].exited && clients[0].exit_status==23,"original child status survives adopted descendant cleanup");
    require(!clients[0].done && !clients[0].eof,"live descendant keeps PTY usable after original exit");
    require(bt_session_send(&clients[0],"Q",1)==0,"send input to descendant after original child exit");
    Progress later=await_progress(descendants,p.step,'O');
    parsed(&clients[0],later.bytes);
    require(later.queries==1 && contains(&clients[0],"ORPHAN_OUTPUT"),
            "surviving descendant produces later output and receives a terminal query reply");
    require(clients[0].exited && clients[0].exit_status==23 && !clients[0].done,
            "continuing PTY preserves original exit status until final EOF");
    detach(&clients[0]);
    terminate("orphan");
    until=bt_millis()+2000;
    while(bt_millis()<until) {
        while(waitpid(-1,NULL,WNOHANG)>0) {}
        if(kill(p.descendant,0)<0 && errno==ESRCH) break;
        nap();
    }
    require(kill(p.descendant,0)<0 && errno==ESRCH,"termination also cleans descendants after the original child has exited");
    report("later descendant input, output and terminal replies preserve original exit status until cleanup");

    char cancel_path[PATH_MAX];
    path(cancel_path,sizeof(cancel_path),root,"cancel-first.progress");
    char *cancel_argv[]={self,"--child","normal",cancel_path,NULL};
    owned_names[owned_count++]="cancel";
    uint64_t first_epoch=0,next_epoch=0;
    require(bt_remote_create_owned(service,helper,root,"cancel",cancel_argv,environ,80,24,8,16,
                                    error,sizeof(error),&first_epoch)==0 && first_epoch,
            "unclaimed creation returns a rollback identity");
    (void)await_progress(cancel_path,0,'R');
    require(bt_remote_cancel(root,"cancel",first_epoch,error,sizeof(error))==0,
            "matching unclaimed creation can be rolled back");
    path(cancel_path,sizeof(cancel_path),root,"cancel-second.progress");
    require(bt_remote_create_owned(service,helper,root,"cancel",cancel_argv,environ,80,24,8,16,
                                    error,sizeof(error),&next_epoch)==0 && next_epoch && next_epoch!=first_epoch,
            "replacement service gets a fresh creation identity");
    (void)await_progress(cancel_path,0,'R');
    require(bt_remote_cancel(root,"cancel",first_epoch,error,sizeof(error))<0,
            "stale rollback cannot cancel a replacement session");
    require(bt_remote_cancel(root,"cancel",next_epoch,error,sizeof(error))==0,
            "replacement remains available for its own matching rollback");
    char cancelled[PATH_MAX];
    endpoint_path(cancelled,sizeof(cancelled),"cancel");
    require(access(cancelled,F_OK)<0 && errno==ENOENT,"rollback completes endpoint cleanup before returning");
    --owned_count;
    report("creation rollback is scoped to unclaimed matching epochs and preserves replacements");

    char *missing[]={"/nonexistent-batty-persistence-fixture",NULL};
    require(bt_remote_create(service,helper,root,"badexec",missing,environ,80,24,8,16,error,sizeof(error))<0,
            "startup reports failed child exec");
    char endpoint[PATH_MAX];
    endpoint_path(endpoint,sizeof(endpoint),"badexec");
    require(access(endpoint,F_OK)<0 && errno==ENOENT,"failed startup leaves no endpoint behind");
    char *argv[]={self,"--child","normal",progress_path,NULL};
    require(bt_remote_create(service,helper,root,"../escape",argv,environ,80,24,8,16,error,sizeof(error))<0,
            "session names cannot escape private root");
    char weak[PATH_MAX],link[PATH_MAX],target[PATH_MAX];
    path(weak,sizeof(weak),root,"weak-root");
    require(mkdir(weak,0755)==0 && chmod(weak,0755)==0,"prepare nonprivate root fixture");
    require(bt_remote_create(service,helper,weak,"reject",argv,environ,80,24,8,16,error,sizeof(error))<0,
            "nonprivate root is rejected");
    path(target,sizeof(target),root,"untouched");
    int fd=open(target,O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC,0600);
    require(fd>=0 && write_all(fd,"sentinel",8) && close(fd)==0,"prepare endpoint symlink target");
    endpoint_path(link,sizeof(link),"symlink");
    require(symlink(target,link)==0,"prepare session symlink fixture");
    require(bt_remote_create(service,helper,root,"symlink",argv,environ,80,24,8,16,error,sizeof(error))<0,
            "symlink endpoint is rejected");
    char sentinel[8];
    fd=open(target,O_RDONLY|O_CLOEXEC);
    require(fd>=0 && read(fd,sentinel,sizeof(sentinel))==sizeof(sentinel) && !memcmp(sentinel,"sentinel",8),
            "rejected endpoint does not alter its symlink target");
    close(fd);
    report("failed exec, invalid names, permissive roots and symlink endpoints leave no owned residue");
}

int main(int argc,char **argv) {
    if(argc==4 && !strcmp(argv[1],"--child")) return child_main(argv[2],argv[3]);
    require(argc==1,"test executable arguments");
    require(prctl(PR_SET_CHILD_SUBREAPER,1)==0,"test process reaps only its orphaned fixture descendants");
    require(realpath(argv[0],self)!=NULL && realpath("build/batty-session",helper)!=NULL &&
            realpath("build/batty-state",service)!=NULL,"resolve built service, helper and fixture");
    const char *supplied_root=getenv("BATTY_TEST_SESSION_DIR");
    if(supplied_root) {
        require(*supplied_root=='/' && strlen(supplied_root)<sizeof(root),"isolated runner root is absolute and bounded");
        strcpy(root,supplied_root);
        struct stat st;
        require(!lstat(root,&st) && S_ISDIR(st.st_mode) && st.st_uid==getuid() && (st.st_mode&0777)==0700,
                "isolated runner supplies its own private directory");
    } else {
        strcpy(root,"/tmp/bt-persist-XXXXXX");
        require(mkdtemp(root)!=NULL,"create isolated private test root");
    }
    require(atexit(cleanup)==0,"register private session cleanup");
    path(progress_path,sizeof(progress_path),root,"main.progress");
    startup_and_ownership();
    graphics_state();
    observers_and_codec();
    fresh_windows();
    exit_and_failures();
    printf("PASS persistence: %u assertions; no test sessions retained\n",assertions);
    return 0;
}
