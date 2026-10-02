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
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/socket.h>
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
static pid_t stopped_service, pause_clipboard_pid, pause_frame_pid;
static bool frame_send_again, hold_input_sends;
static unsigned queued_inputs_sent;
static unsigned zero_frames_sent;
static bool trace_resizes;
static struct { uint32_t type,cols,rows,cw,ch; } resize_trace[16];
static unsigned resize_trace_count;
int __real_bt_wire_send(int fd, const BtPacket *packet, int passed_fd);
int __wrap_bt_wire_send(int fd, const BtPacket *packet, int passed_fd) {
    pid_t *pause_owner=packet->type==BT_WIRE_CLIPBOARD?&pause_clipboard_pid:
        packet->type==BT_WIRE_FRAME?&pause_frame_pid:NULL;
    if(pause_owner && *pause_owner>0) {
        stopped_service=*pause_owner; *pause_owner=0;
        if(kill(stopped_service,SIGSTOP)) return -1;
        char path[64]; snprintf(path,sizeof(path),"/proc/%ld/status",(long)stopped_service);
        bool stopped=false; uint64_t deadline=bt_millis()+500;
        while(!stopped && bt_millis()<deadline) {
            FILE *file=fopen(path,"r");
            if(file) { char text[1024]; size_t n=fread(text,1,sizeof(text)-1,file); text[n]=0;
                stopped=strstr(text,"\nState:\tT")!=NULL; fclose(file); }
            if(!stopped) (void)poll(NULL,0,1);
        }
        if(!stopped) { errno=ETIMEDOUT; return -1; }
    }
    if(hold_input_sends && (packet->type==BT_WIRE_SEND || packet->type==BT_WIRE_INTENT)) {
        errno=EAGAIN; return -1;
    }
    if(frame_send_again && packet->type==BT_WIRE_FRAME) {
        frame_send_again=false; errno=EAGAIN; return -1;
    }
    int result=__real_bt_wire_send(fd,packet,passed_fd);
    if(!result && packet->type==BT_WIRE_FRAME && !packet->flags) ++zero_frames_sent;
    if(!result && (packet->type==BT_WIRE_SEND || packet->type==BT_WIRE_INTENT)) ++queued_inputs_sent;
    if(!result && trace_resizes && (packet->type==BT_WIRE_RESIZE || packet->type==BT_WIRE_SEND)) {
        if(resize_trace_count<16) {
            resize_trace[resize_trace_count].type=packet->type;
            resize_trace[resize_trace_count].cols=packet->cols;
            resize_trace[resize_trace_count].rows=packet->rows;
            resize_trace[resize_trace_count].cw=packet->cw;
            resize_trace[resize_trace_count].ch=packet->ch;
        }
        ++resize_trace_count;
    }
    return result;
}

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
    if(stopped_service>0) { kill(stopped_service,SIGCONT); stopped_service=0; }
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
static void shared_pixels(const char *header, unsigned width, unsigned height, bool blue) {
    static unsigned serial;
    char name[96]; snprintf(name,sizeof(name),"/batty-persist-%ld-%u",(long)getpid(),++serial);
    int fd=shm_open(name,O_CREAT|O_EXCL|O_RDWR,0600);
    if(fd<0) _exit(94);
    size_t length=(size_t)width*height*4;
    uint8_t *pixels=malloc(length);
    if(!pixels) { close(fd); shm_unlink(name); _exit(95); }
    for(size_t i=0;i<length;i+=4) {
        pixels[i]=width>64?255:0; pixels[i+1]=width>64 || blue?0:255;
        pixels[i+2]=blue?255:0; pixels[i+3]=255;
    }
    bool ok=write_all(fd,pixels,length); free(pixels); close(fd);
    if(!ok) { shm_unlink(name); _exit(96); }
    static const char alphabet[]="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    char encoded[132]; size_t n=strlen(name),used=0;
    for(size_t i=0;i<n;i+=3) {
        unsigned word=(unsigned char)name[i]<<16;
        if(i+1<n) word|=(unsigned char)name[i+1]<<8;
        if(i+2<n) word|=(unsigned char)name[i+2];
        encoded[used++]=alphabet[(word>>18)&63]; encoded[used++]=alphabet[(word>>12)&63];
        encoded[used++]=i+1<n?alphabet[(word>>6)&63]:'=';
        encoded[used++]=i+2<n?alphabet[word&63]:'=';
    }
    encoded[used]=0;
    emit("\033_G%s,t=s,f=32,s=%u,v=%u,q=2;%s\033\\",header,width,height,encoded);
    /* The protocol owner consumes and unlinks the one-shot shared object. */
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
    if(!strcmp(kind,"record")) {
        char gate[PATH_MAX];
        if(snprintf(gate,sizeof(gate),"%s.go",filename)>=(int)sizeof(gate)) return 89;
        uint64_t deadline=bt_millis()+6000;
        while(access(gate,F_OK) && bt_millis()<deadline) usleep(1000);
        if(access(gate,F_OK)) return 89;
        emit("RECORDED_WHILE_DETACHED\r\n");
        if(!query()) return 90;
        child_mark('R',0);
        char command;
        if(!child_read_for(&command,1,6000) || command!='E') return 91;
        emit("RECORDED_AFTER_REATTACH\r\n");
        return 0;
    }
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
    unsigned large_edit=0;
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
        case 'g':
            screen("LIVE_DELTA");
            emit("\033_Ga=T,f=32,s=8,v=8,i=901,C=1,q=2;/wAA//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//wAA/w==\033\\");
            break;
        case 'h': emit("\033_Ga=f,i=901,r=1,x=2,y=3,s=1,v=1,f=32,N=1,q=2;AP8A/w==\033\\"); break;
        case 's':
            screen("LARGE_DELTA"); large_edit=0;
            shared_pixels("a=T,i=902,C=1,N=1",1920,1080,false); break;
        case 't': {
            char header[96];
            snprintf(header,sizeof(header),"a=f,i=902,r=1,x=%u,y=32,N=1",32+8*large_edit);
            shared_pixels(header,64,32,(large_edit++&1)!=0); break;
        }
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
        case 'C': emit("\033]52;c;YXN5bmM=\007"); break;
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

static void recording_lifetime(void) {
    char logs[PATH_MAX],record_progress[PATH_MAX],gate[PATH_MAX];
    path(logs,sizeof(logs),root,"transcripts");
    path(record_progress,sizeof(record_progress),root,"record.progress");
    path(gate,sizeof(gate),root,"record.progress.go");
    require(!mkdir(logs,0700) && !setenv("BATTY_TRANSCRIPT_DIR",logs,1),"private owner transcript root");
    create("record","record",record_progress);
    unsetenv("BATTY_TRANSCRIPT_DIR");
    BtSession *s=&clients[3];
    require(!bt_remote_attach(s,root,"record",false),"attach recorded owner");
    uint64_t epoch=s->presentation->epoch; pid_t pid=s->child;
    require(!s->recorder && bt_session_recording(s)==BT_RECORD_ACTIVE,"frontend reports owner recording without a second writer");
    const char *transcript=bt_session_transcript_id(s);
    require(transcript && strlen(transcript)==32,"owner supplies exact transcript identity");
    require(bt_session_transcript_directory(s) && !strcmp(bt_session_transcript_directory(s),logs),
            "owner supplies original transcript directory");
    char transcript_id[49]; snprintf(transcript_id,sizeof(transcript_id),"%s",transcript);
    detach(s);
    int fd=open(gate,O_CREAT|O_EXCL|O_WRONLY|O_CLOEXEC,0600);
    require(fd>=0,"release detached output gate"); close(fd);
    Progress recorded=await_progress(record_progress,0,'R');
    require(recorded.queries==1,"owner answers terminal query while detached and recording");
    require(!bt_remote_attach(s,root,"record",false),"reattach recorded owner");
    require(s->child==pid && s->presentation->epoch==epoch && bt_session_recording(s)==BT_RECORD_ACTIVE,
            "recording and process identity survive frontend replacement");
    require(bt_session_transcript_id(s) && !strcmp(bt_session_transcript_id(s),transcript_id),
            "transcript identity survives frontend replacement");
    require(bt_session_transcript_directory(s) && !strcmp(bt_session_transcript_directory(s),logs),
            "transcript directory survives frontend replacement");
    require(!bt_session_send(s,"E",1),"finish recorded child");
    uint64_t deadline=bt_millis()+6000;
    while((!s->done || bt_session_recording(s)!=BT_RECORD_COMPLETE) && bt_millis()<deadline)
        require(!bt_session_pump(s,5),"pump owner recording completion");
    require(s->done && s->exit_status==0 && bt_session_recording(s)==BT_RECORD_COMPLETE,"durable completion reaches replacement frontend");
    DIR *dir=opendir(logs); require(dir!=NULL,"open owner logs");
    unsigned count=0; struct dirent *entry;
    while((entry=readdir(dir))) {
        if(entry->d_name[0]=='.' || strstr(entry->d_name,".meta")) continue;
        char expected[sizeof(transcript_id)+4]; snprintf(expected,sizeof(expected),"%s.log",transcript_id);
        require(!strcmp(entry->d_name,expected),"owner identity names its transcript");
        ++count; char filename[PATH_MAX]; path(filename,sizeof(filename),logs,entry->d_name);
        FILE *file=fopen(filename,"rb"); require(file!=NULL,"read owner log");
        char text[2048]={0}; size_t n=fread(text,1,sizeof(text)-1,file); fclose(file);
        require(n && strstr(text,"RECORDED_WHILE_DETACHED") && strstr(text,"RECORDED_AFTER_REATTACH"),
                "one transcript spans detached and attached output");
        require(!unlink(filename),"remove owned log fixture");
        strcpy(filename+strlen(filename)-4,".meta"); require(!unlink(filename),"remove owned metadata fixture");
    }
    closedir(dir); require(count==1,"exactly one owner transcript across two frontends");
    detach(s); terminate("record");
    require(!rmdir(logs) && !unlink(gate) && !unlink(record_progress),"remove recording fixture state");
    report("transcript ownership, detached output and recording status across reattachment");
}

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
static unsigned descriptor_count(void) {
    DIR *directory=opendir("/proc/self/fd");
    require(directory!=NULL,"open process descriptor inventory");
    unsigned count=0; struct dirent *entry;
    while((entry=readdir(directory))) if(entry->d_name[0]!='.') ++count;
    closedir(directory); return count;
}

static void nonblocking_clipboard_focus(pid_t owner) {
    detach(&clients[0]);
    window_open=true;
    require(!bt_window_attach(&window,root,"main","monospace",16,true,false),"attach headless graphical controller for focus ordering");
    window.clipboard_write=true;
    for(unsigned refocus=0;refocus<2;++refocus) {
        require(!bt_window_focus(&window,true) && !bt_remote_input_flush(&window.session),"enable clipboard policy before fixture output");
        uint64_t before=progress.step;
        require(!bt_session_send(&window.session,"C",1),"request clipboard before focus change");
        progress=await_progress(progress_path,before,'C');
        pause_clipboard_pid=owner;
        uint64_t deadline=bt_millis()+1000;
        while(pause_clipboard_pid && bt_millis()<deadline) {
            require(!bt_session_pump(&window.session,0),"queue fetch for focus-change fixture"); nap();
        }
        require(!pause_clipboard_pid && stopped_service==owner,"pause owner with a clipboard reply outstanding");
        uint64_t started=bt_millis();
        require(!bt_window_focus(&window,false),"focus loss queues policy without waiting for stopped owner");
        require(!window.session.clipboard_enabled && !window.session.clipboard,"focus loss revokes local delivery immediately");
        if(refocus) require(!bt_window_focus(&window,true),"focus return queues behind revocation without waiting");
        require(bt_millis()-started<1000,"graphical focus transitions stay below the request timeout");
        require(!kill(stopped_service,SIGCONT),"resume owner after queued focus changes"); stopped_service=0;
        require(!bt_remote_input_flush(&window.session),"drain old fetch and ordered focus policies");
        require(!window.session.clipboard,"old fetch cannot publish after revocation or rapid refocus");
        require(!bt_session_pump(&window.session,1) && !window.session.clipboard,"old clipboard data is not fetched again");
        require(!bt_window_focus(&window,true),"restore clipboard permission for fresh output");
        command(&window.session,'C');
        require(window.session.clipboard && !strcmp(window.session.clipboard,"async"),"fresh clipboard output still works after focus returns");
        bt_session_clipboard_clear(&window.session);
    }
    require(!bt_window_focus(&window,false),"revoke focus fixture clipboard permission");
    bt_window_close(&window); window_open=false;
    attach(&clients[0],false);
    command(&clients[0],'N');
}

static void nonblocking_resizes(pid_t owner) {
    detach(&clients[0]); window_open=true;
    require(!bt_window_attach(&window,root,"main","monospace",16,true,false),"attach queued graphical resize controller");
    BtSession *s=&window.session;
    unsigned cols=s->cols,rows=s->rows,cw=s->cell_width,ch=s->cell_height;
    require(cols<990 && rows<990 && cw<500 && ch<500,"resize fixture dimensions leave room for bounded changes");
    require(!bt_session_pump(s,1),"finish earlier replies before resize probe");
    BtPresentation *retained=s->presentation;
    pause_frame_pid=owner;
    require(!bt_session_pump(s,0) && !pause_frame_pid && stopped_service==owner,"pause owner before an outstanding frame request");
    resize_trace_count=0; trace_resizes=true;
    uint64_t started=bt_millis();
    require(!bt_session_resize(s,cols+1,rows+1,cw+1,ch+1),"queue first resize without waiting for its owner");
    for(unsigned i=0;i<1000;++i)
        require(!bt_session_resize(s,cols+2+i%5,rows+2+i%3,cw+1+i%2,ch+1+i%2),"coalesce a resize burst within bounded queue storage");
    require(!bt_session_resize(s,cols,rows,cw,ch),"restore original published geometry while a different resize is pending");
    require(!bt_session_send(s,"N",1),"input separates resize coalescing groups");
    require(!bt_session_resize(s,cols+7,rows+4,cw+2,ch+2),"queue another resize after input");
    require(bt_session_resize(s,0,rows,cw,ch)<0 && errno==EINVAL,"invalid queued dimensions fail without altering the target");
    for(unsigned i=0;i<20;++i) require(!bt_session_pump(s,0),"resize and frame polling never wait for stopped owner");
    require(bt_millis()-started<1000 && resize_trace_count==1,"only one resize is sent before acknowledgment despite a large burst");
    require(bt_remote_resize_pending(s) && s->presentation==retained &&
            s->cols==cols && s->rows==rows && s->cell_width==cw && s->cell_height==ch,
            "last published geometry and frame remain consistent while resize is pending");
    require(!kill(stopped_service,SIGCONT),"resume resize owner"); stopped_service=0;
    require(!bt_remote_input_flush(s),"flush ordered resize groups and interleaved input");
    trace_resizes=false;
    require(resize_trace_count==4 && resize_trace[0].type==BT_WIRE_RESIZE && resize_trace[0].cols==cols+1 &&
            resize_trace[1].type==BT_WIRE_RESIZE && resize_trace[1].cols==cols && resize_trace[1].rows==rows &&
            resize_trace[2].type==BT_WIRE_SEND && resize_trace[3].type==BT_WIRE_RESIZE &&
            resize_trace[3].cols==cols+7 && resize_trace[3].rows==rows+4 && resize_trace[3].cw==cw+2 && resize_trace[3].ch==ch+2,
            "resize coalescing preserves reversal and never crosses an input request");
    progress=await_progress(progress_path,progress.step,'N');
    uint64_t deadline=bt_millis()+1000;
    while(bt_remote_resize_pending(s) && bt_millis()<deadline) {
        require(!bt_session_pump(s,0),"publish resized geometry asynchronously"); nap();
    }
    require(!bt_remote_resize_pending(s) && s->cols==cols+7 && s->rows==rows+4 &&
            s->cell_width==cw+2 && s->cell_height==ch+2 && s->presentation->cols==s->cols && s->presentation->rows==s->rows,
            "final owner frame settles the requested grid and cell metrics");
    attach(&clients[2],true);
    require(!bt_remote_resize_pending(&clients[2]),"observer has no queued geometry of its own");
    detach(&clients[2]);
    require(!bt_session_resize(s,cols,rows,cw,ch) && !bt_session_pump(s,1),"restore fixture dimensions with an explicit freshness barrier");
    require(!bt_remote_resize_pending(s),"blocking frame barrier observes the restored resize");
    bt_window_close(&window); window_open=false;
    attach(&clients[0],false);
}

static int take_text(BtSession *s, uint64_t ticket, char **text, size_t *length) {
    uint64_t deadline=bt_millis()+1000;
    int rc;
    while((rc=bt_remote_text_take(s,ticket,text,length))==1 && bt_millis()<deadline) {
        require(!bt_session_pump(s,0),"pump asynchronous text reply without waiting"); nap();
    }
    require(rc!=1,"asynchronous text response deadline"); return rc;
}
static void nonblocking_text(pid_t owner) {
    BtSession *s=&clients[0];
    require(!bt_session_pump(s,1),"finish previous frame before asynchronous text probe");
    unsigned descriptors=descriptor_count();
    pause_frame_pid=owner;
    require(!bt_session_pump(s,0) && !pause_frame_pid && stopped_service==owner,"pause text owner behind an outstanding frame");
    uint64_t tickets[16],extra=99,started=bt_millis();
    char *text=NULL; size_t length=0;
    for(unsigned i=0;i<16;++i) {
        require(!bt_remote_text_start(s,false,65536,&tickets[i]) && tickets[i],"queue bounded asynchronous text request");
        require(bt_remote_text_take(s,tickets[i],&text,&length)==1 && !text && !length,"pending text take never waits");
    }
    require(bt_remote_text_start(s,false,65536,&extra)<0 && errno==ENOBUFS && !extra,"seventeenth text request is rejected atomically");
    bt_remote_text_cancel(s,tickets[0]);
    require(bt_remote_text_take(s,tickets[0],&text,&length)<0 && errno==ENOENT,"cancelled text cannot be delivered");
    require(bt_remote_text_start(s,false,65536,&extra)<0 && errno==ENOBUFS,"cancelled in-flight result stays bounded until drained");
    require(bt_millis()-started<1000,"text request enqueue and take remain responsive with stopped owner");
    require(!kill(stopped_service,SIGCONT),"resume asynchronous text owner"); stopped_service=0;
    for(unsigned i=1;i<16;++i) {
        require(!take_text(s,tickets[i],&text,&length) && text && length && strstr(text,"PRIMARY_SURVIVES"),
                "asynchronous text preserves authoritative terminal contents");
        free(text); text=NULL;
    }
    require(descriptor_count()==descriptors,"completed and cancelled text replies leave no file descriptors");
    require(!bt_remote_text_start(s,false,1,&extra),"queue a deliberately small text limit");
    require(take_text(s,extra,&text,&length)<0 && errno==E2BIG && !text && !bt_remote_disconnected(s),
            "oversized text rejects only that result without disconnecting its owner");
    require(!bt_remote_text_start(s,true,65536,&extra),"queue empty selection request");
    require(!take_text(s,extra,&text,&length) && text && !length && !*text,"empty selection is a valid asynchronous result");
    free(text); text=NULL;
    require(bt_remote_text_start(s,false,BT_CLIPBOARD_LIMIT+1,&extra)<0 && errno==EINVAL && !extra,
            "reject unbounded asynchronous result capacity");
    require(!bt_remote_text_start(s,false,65536,&extra) && !bt_session_pump(s,1),"complete a result before cancellation");
    bt_remote_text_cancel(s,extra);
    require(bt_remote_text_take(s,extra,&text,&length)<0 && errno==ENOENT,"completed cancellation releases result ownership");
    attach(&clients[2],true);
    require(!bt_remote_text_start(&clients[2],false,65536,&extra),"observer can queue read-only text");
    require(!take_text(&clients[2],extra,&text,&length) && strstr(text,"PRIMARY_SURVIVES"),"observer receives asynchronous authoritative text");
    free(text); detach(&clients[2]);
    command(s,'N');
}

static void nonblocking_copy(pid_t owner) {
    require(!SDL_InitSubSystem(SDL_INIT_VIDEO),"initialize isolated clipboard client");
    detach(&clients[0]); window_open=true;
    require(!bt_window_attach(&window,root,"main","monospace",16,true,false),"attach graphical selection-copy controller");
    window.clipboard_write=false;
    require(!bt_window_focus(&window,true),"focus copy controller");
    BtSession *s=&window.session;
    uint64_t before_composing=s->bytes_written;
    BtIntent composing_key={.type=BT_INTENT_KEY,.action=GHOSTTY_KEY_ACTION_PRESS,
        .key=GHOSTTY_KEY_ARROW_LEFT,.composing=1};
    require(!bt_remote_intent(s,&composing_key,NULL,0) &&
            s->bytes_written==before_composing,
            "persistent owner suppresses IME navigation key input");
    command(s,'N'); /* The next fixture command must not inherit a key sequence. */
    BtIntent pointer={.type=BT_INTENT_POINTER,.action=GHOSTTY_MOUSE_ACTION_PRESS,
        .button=GHOSTTY_MOUSE_BUTTON_LEFT,.mods=GHOSTTY_MODS_SHIFT,.buttons=1};
    require(!bt_remote_intent(s,&pointer,NULL,0),"begin authoritative selection");
    pointer.action=GHOSTTY_MOUSE_ACTION_RELEASE; pointer.x=6*s->cell_width; pointer.buttons=0;
    require(!bt_remote_intent(s,&pointer,NULL,0) && !bt_session_pump(s,1),"finish authoritative selection");
    size_t length=0; char *selected=bt_session_text(s,true,&length);
    require(selected && !strcmp(selected,"PRIMARY"),"fixture selection contains the intended word"); free(selected);
    SDL_Event copy={.type=SDL_KEYDOWN};
    copy.key.keysym=(SDL_Keysym){.sym=SDLK_c,.scancode=SDL_SCANCODE_C,.mod=KMOD_CTRL|KMOD_SHIFT};
    for(unsigned cancel=0;cancel<2;++cancel) {
        require(!SDL_SetClipboardText("copy sentinel"),"set clipboard before paused copy");
        require(!bt_session_pump(s,1),"finish earlier copy frames");
        pause_frame_pid=owner;
        require(!bt_session_pump(s,0) && !pause_frame_pid && stopped_service==owner,"pause owner before selection copy");
        uint64_t started=bt_millis();
        require(!bt_window_event(&window,&copy) && window.copy_ticket,"copy shortcut queues text without waiting");
        uint64_t ticket=window.copy_ticket;
        require(!bt_window_event(&window,&copy) && window.copy_ticket==ticket,"repeated copy keeps one bounded request");
        for(unsigned i=0;i<20;++i) require(!bt_window_pump(&window,0),"graphical copy polling remains nonblocking");
        require(bt_millis()-started<1000,"copying from stopped owner cannot wait for the request deadline");
        if(cancel) {
            require(!bt_window_focus(&window,false) && !window.copy_ticket,"focus loss cancels copy delivery");
            require(!bt_window_focus(&window,true),"rapid refocus does not revive old copy");
        }
        require(!kill(stopped_service,SIGCONT),"resume selection owner"); stopped_service=0;
        require(!bt_window_pump(&window,1) && !window.copy_ticket,"complete or drain selection copy");
        char *clipboard=SDL_GetClipboardText();
        require(clipboard && !strcmp(clipboard,cancel?"copy sentinel":"PRIMARY"),"clipboard receives only a still-current explicit copy");
        SDL_free(clipboard);
    }
    require(!bt_window_event(&window,&copy) && !bt_window_pump(&window,1),"fresh copy works after cancellation");
    char *clipboard=SDL_GetClipboardText();
    require(clipboard && !strcmp(clipboard,"PRIMARY"),"fresh copy publishes selected text"); SDL_free(clipboard);
    bt_window_close(&window); window_open=false;
    attach(&clients[0],false);
    SDL_QuitSubSystem(SDL_INIT_VIDEO);
}

static void nonblocking_frames(void) {
    char other_progress[PATH_MAX]; path(other_progress,sizeof(other_progress),root,"poll-live.progress");
    create("poll-live","normal",other_progress);
    Progress other=await_progress(other_progress,0,'R');
    require(!bt_remote_attach(&clients[1],root,"poll-live",false),"attach independent polling service");
    require(!bt_session_pump(&clients[0],1),"finish previous frame before polling failure probe");
    int probe=bt_wire_connect(root,"main");
    require(probe>=0,"connect owned service for peer identity");
    struct ucred cred; socklen_t size=sizeof(cred);
    require(!getsockopt(probe,SOL_SOCKET,SO_PEERCRED,&cred,&size) && cred.uid==getuid() && cred.pid>0,
            "identify owned service through peer credentials");
    close(probe); stopped_service=cred.pid;
    require(!kill(stopped_service,SIGSTOP),"stop only the owned fixture service");
    char status_path[64]; snprintf(status_path,sizeof(status_path),"/proc/%ld/status",(long)stopped_service);
    bool stopped=false; uint64_t deadline=bt_millis()+1000;
    while(!stopped && bt_millis()<deadline) {
        FILE *status=fopen(status_path,"r");
        if(status) { char text[1024]; size_t n=fread(text,1,sizeof(text)-1,status); text[n]=0;
            stopped=strstr(text,"\nState:\tT")!=NULL; fclose(status); }
        if(!stopped) nap();
    }
    require(stopped,"verify fixture service is stopped before polling");
    frame_send_again=true;
    require(!bt_session_pump(&clients[0],0) && !frame_send_again,"retry frame send after bounded socket backpressure");
    uint64_t start=bt_millis();
    for(unsigned i=0;i<20;++i) require(!bt_session_pump(&clients[0],0),"zero-timeout polling never waits for stopped owner");
    require(bt_millis()-start<1000,"stopped service polls finish before RPC timeout");
    require(!bt_remote_input_queue(&clients[0],true),"enable bounded controller input queue");
    unsigned sent_before=queued_inputs_sent;
    uint64_t input_step=progress.step;
    char payload='U'; hold_input_sends=true;
    start=bt_millis();
    require(!bt_session_send(&clients[0],&payload,1),"queue input even when the socket cannot send");
    payload='!'; /* The caller's storage can be reused as soon as send returns. */
    hold_input_sends=false;
    BtIntent paste={.type=BT_INTENT_PASTE};
    require(!bt_remote_intent(&clients[0],&paste,"D",1),"queue semantic paste behind raw input and a pending frame");
    for(unsigned i=2;i<127;++i) require(!bt_session_send(&clients[0],NULL,0),"fill bounded input operation queue");
    require(!bt_session_send(&clients[0],"N",1),"queue final ordered command");
    require(bt_session_send(&clients[0],"N",1)<0 && errno==ENOBUFS,"reject operation 129 without dropping accepted input");
    require(bt_millis()-start<1000,"input enqueues do not wait for stopped owner acknowledgment");
    require(queued_inputs_sent>sent_before+1,"pipeline multiple inputs without waiting for frame or input replies");
    require(!bt_session_send(&clients[1],"N",1),"send input to independent live service");
    other=await_progress(other_progress,other.step,'N');
    deadline=bt_millis()+1000;
    while(clients[1].bytes_read<other.bytes && bt_millis()<deadline) {
        require(!bt_session_pump(&clients[0],0) && !bt_session_pump(&clients[1],0),"poll stopped and live owners together");
        nap();
    }
    require(clients[1].bytes_read>=other.bytes,"live owner progresses while another frame request remains pending");
    require(!kill(stopped_service,SIGCONT),"resume owned fixture service"); stopped_service=0;
    require(!bt_remote_input_flush(&clients[0]),"flush queued input and preceding poll in request order");
    progress=await_progress(progress_path,input_step+2,'N');
    require(progress.step==input_step+3,"accepted commands arrive once and overflow command is absent");
    parsed(&clients[0],progress.bytes);
    require(frame_contains(clients[0].presentation,"PRIMARY_SURVIVES"),"queued raw input and semantic paste preserve FIFO and payload ownership");
    require(!bt_remote_input_queue(&clients[0],false),"restore synchronous direct-session input");
    command(&clients[0],'N');
    require(!bt_session_clipboard_policy(&clients[0],true),"enable asynchronous clipboard fixture");
    for(unsigned revoke=0;revoke<2;++revoke) {
        uint64_t before=progress.step;
        require(!bt_session_send(&clients[0],"C",1),"request fixture clipboard write");
        progress=await_progress(progress_path,before,'C');
        pause_clipboard_pid=cred.pid;
        deadline=bt_millis()+1000;
        while(pause_clipboard_pid && bt_millis()<deadline) {
            require(!bt_session_pump(&clients[0],0),"queue clipboard fetch without waiting for its reply");
            nap();
        }
        require(!pause_clipboard_pid && stopped_service>0,"pause owner exactly when clipboard fetch is sent");
        start=bt_millis();
        for(unsigned i=0;i<20;++i) require(!bt_session_pump(&clients[0],0),"pending clipboard fetch stays nonblocking");
        require(bt_millis()-start<1000 && !clients[0].clipboard,"stopped clipboard owner cannot block or publish early");
        unsigned frames_before=zero_frames_sent;
        require(!kill(stopped_service,SIGCONT),"resume clipboard fixture owner"); stopped_service=0;
        if(revoke) {
            require(!bt_session_clipboard_policy(&clients[0],false),"revoke policy while fetched clipboard reply is outstanding");
            require(!clients[0].clipboard,"policy revocation discards data drained under the old policy");
        } else {
            deadline=bt_millis()+1000;
            while(!clients[0].clipboard && bt_millis()<deadline) {
                require(!bt_session_pump(&clients[0],0),"complete asynchronous clipboard fetch"); nap();
            }
            require(clients[0].clipboard && !strcmp(clients[0].clipboard,"async"),"asynchronous clipboard fetch preserves payload");
            require(zero_frames_sent==frames_before+1,"clipboard completion immediately queues the next frame request");
            bt_session_clipboard_clear(&clients[0]);
        }
        require(!bt_session_pump(&clients[0],1) && !clients[0].clipboard,"clipboard fetch is consumed once without replay");
    }
    nonblocking_clipboard_focus(cred.pid);
    nonblocking_resizes(cred.pid);
    nonblocking_text(cred.pid);
    nonblocking_copy(cred.pid);
    require(!bt_remote_input_queue(&clients[1],true),"enable independent payload capacity probe");
    require(!bt_session_clipboard_policy(&clients[1],true) && !bt_remote_input_flush(&clients[1]),"enable policy before queue exhaustion probe");
    char *large=calloc(1,8u*1024u*1024u);
    require(large!=NULL,"allocate bounded payload fixture");
    hold_input_sends=true;
    require(!bt_session_send(&clients[1],large,8u*1024u*1024u),"queue maximum payload while send is backpressured");
    free(large);
    require(bt_session_send(&clients[1],"N",1)<0 && errno==ENOBUFS,"reject byte overflow before staging more payload");
    for(unsigned i=1;i<128;++i) require(!bt_session_send(&clients[1],NULL,0),"fill operation queue before permission revocation");
    clients[1].clipboard=strdup("pending"); clients[1].clipboard_length=7;
    require(clients[1].clipboard!=NULL,"stage local clipboard value before failed revocation");
    require(bt_session_clipboard_policy(&clients[1],false)<0 && errno==ENOBUFS &&
            bt_remote_disconnected(&clients[1]) && !clients[1].clipboard_enabled && !clients[1].clipboard,
            "policy queue failure disconnects and clears clipboard permission without waiting");
    detach(&clients[1]); /* Disconnect discarded unsent bytes without feeding the child. */
    hold_input_sends=false;
    terminate("poll-live");
    attach(&clients[1],true);
    require(bt_remote_input_queue(&clients[1],true)<0 && errno==EPERM,"observer cannot enable input queue");
    detach(&clients[1]);
    BtPresentation *retained=clients[0].presentation;
    pid_t child=clients[0].child;
    require(!bt_remote_input_queue(&clients[0],true),"enable queued rejection probe");
    unsigned descriptors=descriptor_count();
    BtIntent invalid={.type=UINT32_MAX};
    require(!bt_remote_intent(&clients[0],&invalid,NULL,0),"input success reports local acceptance before service rejection");
    uint64_t failed_text=0;
    require(!bt_remote_text_start(&clients[0],false,65536,&failed_text),"queue text behind an operation that will fail");
    hold_input_sends=true;
    require(!bt_session_send(&clients[0],"N",1),"retain an unsent payload behind the rejected request");
    require(descriptor_count()==descriptors+1,"unsent payload owns exactly one staged descriptor");
    deadline=bt_millis()+1000;
    while(!bt_remote_disconnected(&clients[0]) && bt_millis()<deadline)
        (void)bt_session_pump(&clients[0],0);
    hold_input_sends=false;
    require(bt_remote_disconnected(&clients[0]) && *bt_remote_failure(&clients[0]),"asynchronous rejection exposes connection failure");
    require(descriptor_count()==descriptors-1,"disconnect immediately closes transport and staged payload descriptors");
    char *failed_body=NULL; size_t failed_length=0;
    require(bt_remote_text_take(&clients[0],failed_text,&failed_body,&failed_length)<0 && errno==EINVAL && !failed_body,
            "connection failure completes pending text with its original error");
    require(clients[0].presentation==retained && clients[0].child==child && !clients[0].done,
            "failed connection retains last frame without inventing child exit");
    char failure[256]; snprintf(failure,sizeof(failure),"%s",bt_remote_failure(&clients[0]));
    require(bt_session_send(&clients[0],"N",1)<0 && errno==ENOTCONN &&
            !strcmp(failure,bt_remote_failure(&clients[0])),"rejected later input preserves the original failure reason");
    detach(&clients[0]); attach(&clients[0],false);
    command(&clients[0],'N');
    report("nonblocking frame/input polling, bounded ordered input, retained failure state and independent owner progress");
}

static void capture_publication(void) {
    BtSession local={.master=-1,.control=-1,.status=-1,.cols=20,.rows=4,.cell_width=8,.cell_height=16};
    require(ghostty_terminal_new(NULL,&local.terminal,20,4)==GHOSTTY_SUCCESS,"create isolated capture owner");
    require(ghostty_terminal_resize(local.terminal,20,4,8,16)==GHOSTTY_SUCCESS,"set capture owner metrics");
    local.graphics=bt_graphics_new(local.terminal);
    require(local.graphics!=NULL,"create isolated capture graphics decoder");
    const char red[]="\033_Ga=T,f=32,s=1,v=1,i=301,q=2;/wAA/w==\033\\";
    bt_session_feed(&local,red,sizeof(red)-1);
    BtPresenter *presenter=bt_presenter_new(local.terminal);
    require(presenter!=NULL,"create exclusive capture presenter");
    BtPresentation *owned=NULL;
    require(!bt_presenter_capture(presenter,123,1,8,16,true,&owned),"capture independent image ownership");
    image_color(owned,301,255,0,0);
    const char label[]="cached image";
    bt_session_feed(&local,label,sizeof(label)-1);
    BtPresentation *shared=NULL,*new_epoch=NULL;
    require(!bt_presenter_capture(presenter,123,2,8,16,false,&shared),"capture text change with unchanged image");
    require(shared->images[0].pixels==owned->images[0].pixels && shared->images[0].pixel_owner,
            "text changes reuse immutable image storage without pixel copies");
    bt_presentation_free(owned); owned=shared;
    image_color(owned,301,255,0,0);
    require(!bt_presenter_capture(presenter,124,1,8,16,true,&new_epoch),"capture image in a new epoch");
    require(new_epoch->images[0].pixels!=owned->images[0].pixels,
            "image cache cannot reuse storage across epochs");
    bt_presentation_free(new_epoch);
    BtPresentationFile file;
    require(!bt_presenter_capture_file(presenter,123,2,8,16,true,&file),"capture directly from owner into sealed file");
    owned->revision=2;
    uint8_t *bytes=NULL; size_t length=0;
    require(!bt_presentation_pack(owned,&bytes,&length),"serialize copied capture for comparison");
    void *map=bt_wire_map(file.fd,file.length,BT_PRESENTATION_MAX_BYTES);
    require(map!=MAP_FAILED && file.length==length && !memcmp(map,bytes,length),
            "borrowed synchronous capture matches fully owned capture bytes");
    BtPresentation *retained=NULL;
    require(!bt_presentation_unpack_mapping(map,file.length,&retained),"retain published capture after owner mutation");
    close(file.fd); free(bytes);
    const char green[]="\033_Ga=T,f=32,s=1,v=1,i=301,q=2;AP8A/w==\033\\";
    /* Refill the local cache, then replace an image under the same ID. */
    BtPresentation *before_change=NULL,*after_change=NULL;
    require(!bt_presenter_capture(presenter,123,2,8,16,true,&before_change),"refill immutable image cache");
    bt_session_feed(&local,green,sizeof(green)-1);
    require(!bt_presenter_capture(presenter,123,3,8,16,false,&after_change),"capture changed image generation");
    require(after_change->images[0].pixels!=before_change->images[0].pixels,
            "changed image generation receives new immutable storage");
    image_color(before_change,301,255,0,0); image_color(after_change,301,0,255,0);
    bt_presentation_free(before_change); bt_presentation_free(after_change);
    image_color(owned,301,255,0,0); image_color(retained,301,255,0,0);
    struct rlimit original,limited;
    require(!getrlimit(RLIMIT_FSIZE,&original),"read file-size budget");
    limited=original; limited.rlim_cur=0;
    void (*handler)(int)=signal(SIGXFSZ,SIG_IGN);
    require(!setrlimit(RLIMIT_FSIZE,&limited),"force capture publication failure");
    int result=bt_presenter_capture_file(presenter,123,3,8,16,true,&file),saved_error=errno;
    require(!setrlimit(RLIMIT_FSIZE,&original),"restore publication budget");
    signal(SIGXFSZ,handler);
    require(result<0 && saved_error==EFBIG && file.fd==-1 && !file.length,
            "capture publication failure returns no borrowed frame or descriptor");
    require(!bt_presenter_capture_file(presenter,123,3,8,16,false,&file),
            "failed publication retries without additional terminal damage");
    map=bt_wire_map(file.fd,file.length,BT_PRESENTATION_MAX_BYTES);
    require(map!=MAP_FAILED,"map retried capture");
    BtPresentation *updated=NULL;
    require(!bt_presentation_unpack_mapping(map,file.length,&updated),"decode retried capture");
    close(file.fd); image_color(updated,301,0,255,0);
    require(bt_presenter_capture_file(presenter,123,4,8,16,false,&file)==1 && file.fd==-1,
            "unchanged capture does not publish another descriptor");
    bt_presentation_free(updated); bt_presentation_free(retained);
    bt_presenter_free(presenter); bt_graphics_free(local.graphics); ghostty_terminal_free(local.terminal);
    image_color(owned,301,255,0,0); bt_presentation_free(owned);
    report("synchronous borrowed-image capture, immutable publication and damage-free failure retry");
}

static BtPresentation *delta_roundtrip(const BtPresentation *next, const BtPresentation *base, size_t *length) {
    int fd=bt_presentation_pack_delta_fd(next,base,length);
    require(fd>=0 && (fcntl(fd,F_GETFD)&FD_CLOEXEC),"encode sealed incremental frame");
    void *map=bt_wire_map(fd,*length,BT_PRESENTATION_MAX_BYTES);
    require(map!=MAP_FAILED,"delta file has exact extent and required seals");
    close(fd);
    BtPresentation *decoded=NULL;
    require(!bt_presentation_unpack_delta_mapping(map,*length,base,&decoded),"reconstruct independent incremental frame");
    unsigned char resident;
    require(mincore(map,1,&resident)<0 && errno==ENOMEM,"delta decode releases its input mapping immediately");
    uint8_t *a=NULL,*b=NULL; size_t na=0,nb=0;
    require(!bt_presentation_pack(next,&a,&na) && !bt_presentation_pack(decoded,&b,&nb) &&
            na==nb && !memcmp(a,b,na),"delta reconstruction matches every byte of complete frame encoding");
    free(a); free(b); return decoded;
}
static void delta_codec(void) {
    uint8_t pixels[4][256], edits[4][256];
    BtPresentationImage images[4];
    for(unsigned i=0;i<4;++i) {
        for(unsigned j=0;j<sizeof(pixels[i]);++j) pixels[i][j]=(uint8_t)(j+i);
        images[i]=(BtPresentationImage){.id=100+i,.generation=10+i,.width=8,.height=8,
            .channels=i+1,.length=64*(i+1),.pixels=pixels[i]};
    }
    BtPresentationCell cell={0};
    BtPresentation original={.epoch=123,.revision=1,.cols=1,.rows=1,.cell_width=8,.cell_height=16,
        .title="",.cell_count=1,.cells=&cell,.image_count=4,.images=images};
    size_t full_length=0;
    int fd=bt_presentation_pack_fd(&original,&full_length);
    require(fd>=0,"publish full baseline for incremental images");
    void *map=bt_wire_map(fd,full_length,BT_PRESENTATION_MAX_BYTES); close(fd);
    require(map!=MAP_FAILED,"map baseline image pixels");
    BtPresentation *base=NULL;
    require(!bt_presentation_unpack_mapping(map,full_length,&base),"decode mapped baseline");
    BtPresentation next=original; next.revision=2;
    size_t length=0;
    BtPresentation *refs=delta_roundtrip(&next,base,&length);
    require(length==full_length-640+8+4,"unchanged images omit all pixel payloads");
    for(unsigned i=0;i<4;++i) require(refs->images[i].pixel_owner && refs->images[i].pixels!=base->images[i].pixels,
            "first reference copies mapped baseline into bounded individual image ownership");
    bt_presentation_free(base); base=NULL;
    next.revision=3;
    BtPresentation *shared=delta_roundtrip(&next,refs,&length);
    for(unsigned i=0;i<4;++i) require(shared->images[i].pixels==refs->images[i].pixels,
            "subsequent unchanged references share immutable pixels without retaining old frame metadata");
    bt_presentation_free(refs);
    for(unsigned i=0;i<4;++i) {
        memcpy(edits[i],pixels[i],sizeof(edits[i]));
        edits[i][(3*8+2)*(i+1)]=201;
        images[i].pixels=edits[i]; images[i].generation+=10;
    }
    next.revision=4;
    BtPresentation *changed=delta_roundtrip(&next,shared,&length);
    require(length==full_length-640+8+4+4*16+10,"one-pixel damage sends only rectangle headers and channel bytes");
    for(unsigned i=0;i<4;++i) {
        require(changed->images[i].pixels!=shared->images[i].pixels,"damaged image has independent ownership");
        require(!memcmp(shared->images[i].pixels,pixels[i],images[i].length),"delta does not mutate earlier image snapshots");
    }
    /* The exact base revision is mandatory, including for all-reference deltas. */
    int delta_fd=bt_presentation_pack_delta_fd(&next,shared,&length);
    require(delta_fd>=0,"create malformed-delta fixtures");
    uint8_t *encoded=malloc(length); require(encoded!=NULL,"allocate delta fixture bytes");
    require(pread(delta_fd,encoded,length,0)==(ssize_t)length,"read exact delta fixture"); close(delta_fd);
    BtPresentation wrong=*shared; wrong.revision++;
    for(unsigned variant=0;variant<7;++variant) {
        size_t n=length; uint8_t saved=0; size_t offset=0;
        if(variant==1) wrong.epoch++;
        if(variant==2) n--;
        if(variant==3) { offset=924; saved=encoded[offset]; encoded[offset]=3; } /* Invalid image mode. */
        if(variant==4) { offset=925; saved=encoded[offset]; encoded[offset]=255; } /* Out-of-bounds patch X. */
        if(variant==5) { offset=76; saved=encoded[offset]; encoded[offset]^=1; } /* Wrong wire base. */
        if(variant==6) { offset=12; saved=encoded[offset]; encoded[offset]^=1; } /* Wrong total size. */
        fd=bt_wire_blob(encoded,n); require(fd>=0,"seal malformed delta");
        map=bt_wire_map(fd,n,BT_PRESENTATION_MAX_BYTES); close(fd);
        require(map!=MAP_FAILED,"map malformed delta");
        BtPresentation *rejected=(void *)1;
        require(bt_presentation_unpack_delta_mapping(map,n,variant<2?&wrong:shared,&rejected)<0 && !rejected,
                "wrong base, truncation and malformed damage cannot publish a partial frame");
        unsigned char resident;
        require(mincore(map,1,&resident)<0 && errno==ENOMEM,"rejected delta releases mapping");
        if(offset) encoded[offset]=saved;
    }
    BtPresentation *rejected=NULL;
    require(bt_presentation_unpack(encoded,length,&rejected)<0 && !rejected,"complete-frame decoder rejects incremental version");
    free(encoded); bt_presentation_free(shared);
    /* Metadata is a replacement list: removed images do not linger. New IDs,
     * shape changes and complete-image damage take the full-image path. */
    images[0].width=4; images[0].length=32; images[0].generation+=10;
    images[1].id=999; images[1].generation+=10;
    memset(edits[2],0,sizeof(edits[2])); images[2].generation+=10;
    next.image_count=3; next.revision=5;
    BtPresentationImage tmp=images[0]; images[0]=images[2]; images[2]=tmp;
    BtPresentation *replacement=delta_roundtrip(&next,changed,&length);
    require(replacement->image_count==3,"delta removes omitted images and supports reordered image IDs");
    bt_presentation_free(changed);
    next.revision=6;
    for(unsigned i=0;i<3;++i) images[i].generation+=10;
    BtPresentation *identical=delta_roundtrip(&next,replacement,&length);
    for(unsigned i=0;i<3;++i) require(identical->images[i].pixels==replacement->images[i].pixels,
            "identical pixels with new generation share previous immutable storage");
    bt_presentation_free(replacement);
    require(!memcmp(identical->images[0].pixels,images[0].pixels,images[0].length),"latest frame survives disposal of every predecessor");
    bt_presentation_free(identical);
    next.epoch++;
    length=123;
    require(bt_presentation_pack_delta_fd(&next,&original,&length)<0 && !length,"encoder rejects cross-epoch delta");
    report("incremental image codec: references, all-channel damage, independent lifetime, replacement and malformed rejection");
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
static BtPresentation *observer_frame(uint64_t *serial, const BtPresentation *base, bool capable,
                                      bool expect_delta, size_t *length) {
    BtPacket p; bt_wire_packet(&p,BT_WIRE_FRAME); p.request=++*serial;
    if(base) { p.epoch=base->epoch; p.revision=base->revision; }
    if(capable) p.state=BT_STATE_DELTA_SUPPORTED;
    int fd=observer_request(&p);
    require(!p.error && fd>=0,"raw frame publication supplies a sealed descriptor");
    require(((p.state&BT_STATE_DELTA_FRAME)!=0)==expect_delta,"negotiated delta versus complete snapshot selection");
    *length=p.blob_size;
    void *map=bt_wire_map(fd,p.blob_size,BT_PRESENTATION_MAX_BYTES); close(fd);
    require(map!=MAP_FAILED,"live frame has immutable bounded backing");
    BtPresentation *out=NULL;
    int rc=expect_delta?bt_presentation_unpack_delta_mapping(map,p.blob_size,base,&out):
                        bt_presentation_unpack_mapping(map,p.blob_size,&out);
    require(!rc && out->revision==p.revision && out->epoch==p.epoch,"decode exact live publication revision");
    return out;
}
static void live_delta_publication(void) {
    command(&clients[0],'g');
    raw_observer=bt_wire_connect(root,"main"); require(raw_observer>=0,"connect delta observer");
    uint64_t serial=1;
    BtPacket p; bt_wire_packet(&p,BT_WIRE_HELLO); p.request=serial; p.flags=2;
    int fd=observer_request(&p);
    require(!p.error && fd>=0 && (p.state&BT_STATE_DELTA_SUPPORTED) && !(p.state&BT_STATE_DELTA_FRAME),
            "HELLO advertises delta capability and supplies independent full snapshot");
    size_t full_size=p.blob_size;
    void *map=bt_wire_map(fd,p.blob_size,BT_PRESENTATION_MAX_BYTES); close(fd);
    BtPresentation *base=NULL;
    require(map!=MAP_FAILED && !bt_presentation_unpack_mapping(map,p.blob_size,&base),"decode live delta baseline");
    uint64_t before=progress.step;
    require(!bt_session_send(&clients[0],"h",1),"send real root-frame edit");
    progress=await_progress(progress_path,before,'h');
    /* STATUS confirms parsing without publishing intervening frame revisions. */
    uint64_t deadline=bt_millis()+3000;
    do {
        bt_wire_packet(&p,BT_WIRE_STATUS); p.request=++serial;
        fd=observer_request(&p); if(fd>=0) close(fd);
        require(!p.error && fd<0,"status parser barrier has no frame payload");
        if(p.bytes_read>=progress.bytes) break;
        (void)poll(NULL,0,1);
    } while(bt_millis()<deadline);
    require(p.bytes_read>=progress.bytes,"owner parsed complete edit before delta request");
    size_t length=0;
    BtPresentation *edited=observer_frame(&serial,base,true,true,&length);
    require(length+128<full_size,"one-pixel live update omits most image payload bytes");
    const BtPresentationImage *im=find_image(edited,901);
    const uint8_t green[]={0,255,0,255};
    require(im && !memcmp(im->pixels+(3*8+2)*4,green,4),"live delta reconstructs changed image pixel");
    require(find_image(base,901)->pixels[(3*8+2)*4]==255,"live delta preserves old snapshot pixels");
    parsed(&clients[0],progress.bytes);
    require(clients[0].presentation->mapping!=NULL,"controller that missed the base receives a complete resynchronization");
    require(!memcmp(find_image(clients[0].presentation,901)->pixels,im->pixels,im->length),
            "ordinary client and raw delta observer agree on authoritative pixels");
    unsigned cols=clients[0].cols,rows=clients[0].rows,cw=clients[0].cell_width,ch=clients[0].cell_height;
    require(!bt_session_resize(&clients[0],cols+1,rows,cw,ch),"resize without changing image content");
    BtPresentation *resized=observer_frame(&serial,edited,true,true,&length);
    require(resized->cols==cols+1 && find_image(resized,901)->pixels==im->pixels,
            "geometry update references existing immutable pixels");
    BtPresentation *legacy=observer_frame(&serial,edited,false,false,&length);
    require(legacy->mapping && legacy->revision==resized->revision,"unnegotiated client receives a version-one full frame");
    BtPresentation *lagged=observer_frame(&serial,base,true,false,&length);
    require(lagged->revision==resized->revision,"skipped base revision falls back to latest complete snapshot");
    bt_wire_packet(&p,BT_WIRE_FRAME); p.request=++serial; p.epoch=resized->epoch;
    p.revision=resized->revision; p.state=BT_STATE_DELTA_SUPPORTED;
    fd=observer_request(&p);
    require(!p.error && fd<0 && !p.blob_size && !(p.state&BT_STATE_DELTA_FRAME),"unchanged poll sends no snapshot or delta");
    bt_presentation_free(legacy); bt_presentation_free(lagged); bt_presentation_free(base);
    bt_presentation_free(edited); bt_presentation_free(resized);
    close(raw_observer); raw_observer=-1;
    require(!bt_session_resize(&clients[0],cols,rows,cw,ch),"restore controller geometry after live delta test");
    command(&clients[0],'D');
    report("live negotiated image deltas, immutable pixel sharing, lagging/legacy full fallback and unchanged polls");
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
    require(clients[0].presentation->mapping || clients[0].presentation->images[0].pixel_owner,
            "remote frontend owns either sealed full-frame pixels or reconstructed incremental image storage");
    int mapped_fd=bt_wire_blob(packed,length);
    require(mapped_fd>=0,"create sealed presentation blob");
    void *mapped_bytes=bt_wire_map(mapped_fd,length,BT_PRESENTATION_MAX_BYTES);
    require(mapped_bytes!=MAP_FAILED,"map sealed presentation blob");
    close(mapped_fd);
    BtPresentation *mapped=NULL;
    require(!bt_presentation_unpack_mapping(mapped_bytes,length,&mapped) && mapped,
            "decode presentation while retaining its closed-descriptor mapping");
    size_t mapped_pixels=0;
    for(size_t i=0;i<mapped->image_count;++i) {
        uintptr_t start=(uintptr_t)mapped_bytes,pixels=(uintptr_t)mapped->images[i].pixels;
        require(pixels>=start && pixels-start<=length && mapped->images[i].length<=length-(pixels-start),
                "every decoded image points into the original sealed frame");
        mapped_pixels+=mapped->images[i].length;
    }
    require(mapped_pixels>0,"mapped fixture exercises image payloads");
    image_color(mapped,301,255,0,0);
    uint8_t *repacked=NULL; size_t repacked_length=0;
    require(!bt_presentation_pack(mapped,&repacked,&repacked_length) && repacked_length==length &&
            !memcmp(repacked,packed,length),"mapped and copied frames serialize identically");
    free(repacked);
    bt_presentation_free(mapped);
    unsigned char resident=0;
    errno=0;
    require(mincore(mapped_bytes,1,&resident)<0 && errno==ENOMEM,
            "presentation disposal releases the retained mapping");
    size_t direct_length=0;
    int direct_fd=bt_presentation_pack_fd(copy,&direct_length);
    require(direct_fd>=0 && direct_length==length,"direct frame encoding returns exact wire extent");
    void *direct_map=bt_wire_map(direct_fd,direct_length,BT_PRESENTATION_MAX_BYTES);
    require(direct_map!=MAP_FAILED && !memcmp(direct_map,packed,length),
            "direct memory-file encoding matches copied wire bytes and required seals");
    require((fcntl(direct_fd,F_GETFD)&FD_CLOEXEC)!=0,"direct frame descriptor is close-on-exec");
    require(pwrite(direct_fd,"x",1,0)<0 && errno==EPERM,"published frame rejects writes");
    require(ftruncate(direct_fd,0)<0 && errno==EPERM,"published frame rejects shrinking");
    require(ftruncate(direct_fd,(off_t)length+1)<0 && errno==EPERM,"published frame rejects growth");
    munmap(direct_map,direct_length); close(direct_fd);
    pid_t limited=fork();
    require(limited>=0,"fork bounded publication failure probe");
    if(!limited) {
        struct rlimit bound={1024,1024};
        signal(SIGXFSZ,SIG_IGN);
        if(setrlimit(RLIMIT_FSIZE,&bound)) _exit(2);
        size_t rejected_length=123;
        int rejected_fd=bt_presentation_pack_fd(copy,&rejected_length);
        _exit(rejected_fd<0 && errno==EFBIG && !rejected_length?0:3);
    }
    int limited_status=0;
    require(waitpid(limited,&limited_status,0)==limited && WIFEXITED(limited_status) && !WEXITSTATUS(limited_status),
            "partial file-write failure returns an error without publishing a frame or killing the owner");
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
    mapped_fd=bt_wire_blob(packed,length);
    require(mapped_fd>=0,"create malformed sealed presentation");
    mapped_bytes=bt_wire_map(mapped_fd,length,BT_PRESENTATION_MAX_BYTES);
    close(mapped_fd);
    require(mapped_bytes!=MAP_FAILED,"map malformed presentation");
    require(bt_presentation_unpack_mapping(mapped_bytes,length,&invalid)<0 && !invalid,
            "mapped decoder rejects malformed frame without publication");
    errno=0;
    require(mincore(mapped_bytes,1,&resident)<0 && errno==ENOMEM,
            "failed mapped decode releases its mapping");
    free(packed);
    BtPresentation malformed=*copy;
    malformed.cell_count--;
    direct_length=123;
    require(bt_presentation_pack_fd(&malformed,&direct_length)<0 && !direct_length,
            "direct encoder rejects malformed frames without publishing a descriptor");
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
static void persistent_damage_window(void) {
    command(&clients[0],'g');
    pid_t child=clients[0].child;
    detach(&clients[0]);
    memset(&window,0,sizeof(window));
    require(!bt_window_attach(&window,root,"main","monospace",18,false,false),"attach graphical image-delta controller");
    window_open=true;
    for(unsigned i=0;i<3;++i) require(bt_window_pump(&window,5)>=0,"settle graphical attach and resize");
    parsed(&window.session,progress.bytes);
    require(!bt_remote_resize_pending(&window.session),"delta framebuffer baseline has published geometry");
    require(!bt_renderer_draw(window.renderer,&window.session,true,false),"render persistent baseline image");
    BtRemoteFrameStats before=bt_remote_frame_stats(&window.session);
    BtImageStats gpu_before=bt_renderer_image_stats(window.renderer);
    require(before.full_frames>=1 && before.full_bytes>0,"transport counters include attach snapshot");
    uint64_t step=progress.step;
    require(!bt_session_send(&window.session,"h",1) && !bt_remote_input_flush(&window.session),"queue and acknowledge graphical image-edit input");
    progress=await_progress(progress_path,step,'h');
    parsed(&window.session,progress.bytes);
    require(!bt_renderer_draw(window.renderer,&window.session,true,false),"render decoded persistent image delta");
    BtRemoteFrameStats after=bt_remote_frame_stats(&window.session);
    BtImageStats gpu_after=bt_renderer_image_stats(window.renderer);
    require(after.delta_frames==before.delta_frames+1 && after.full_frames==before.full_frames,
            "ordinary graphical controller decodes the image edit as exactly one delta");
    uint8_t *full=NULL; size_t full_size=0;
    require(!bt_presentation_pack(window.session.presentation,&full,&full_size),"measure equivalent complete graphical frame");
    free(full);
    require(after.delta_bytes-before.delta_bytes+128<full_size,"graphical transport delta omits unchanged image pixels");
    require(gpu_after.full_uploads==gpu_before.full_uploads && gpu_after.region_uploads==gpu_before.region_uploads+1 &&
            gpu_after.uploaded_bytes==gpu_before.uploaded_bytes+4,"decoded delta reaches GPU as one four-byte texel update");
    const char *filename="build/persistence-delta.ppm";
    require(!bt_renderer_capture(window.renderer,filename),"capture persistent delta framebuffer");
    FILE *file=fopen(filename,"rb"); require(file!=NULL,"open persistent delta framebuffer");
    char magic[3]={0}; unsigned width,height,maximum;
    require(fscanf(file,"%2s%u%u%u",magic,&width,&height,&maximum)==4 && !strcmp(magic,"P6") &&
            maximum==255 && width && height && width<=8192 && height<=8192 && fgetc(file)=='\n',"validate persistent delta capture header");
    size_t size=(size_t)width*height*3; uint8_t *pixels=malloc(size);
    require(pixels && fread(pixels,1,size,file)==size && fgetc(file)==EOF,"read complete persistent delta framebuffer");
    fclose(file);
    unsigned x=8+2*window.session.cell_width+2,y=8+2*window.session.cell_height+3;
    require(x+1<width && y<height,"damage sample is within framebuffer");
    const uint8_t *sample=pixels+((size_t)y*width+x)*3;
    require(sample[0]<=2 && sample[1]>=253 && sample[2]<=2,"persistent delta renders changed green pixel");
    sample+=3;
    require(sample[0]>=253 && sample[1]<=2 && sample[2]<=2,"persistent delta leaves adjacent red pixel unchanged");
    free(pixels);
    require(bt_session_pump(&window.session,5)>=0,"poll idle persistent graphical session");
    BtRemoteFrameStats idle=bt_remote_frame_stats(&window.session);
    require(idle.unchanged_polls>after.unchanged_polls && idle.full_bytes==after.full_bytes && idle.delta_bytes==after.delta_bytes,
            "idle polling advances only no-payload counter");
    bt_window_close(&window); window_open=false;
    require(!bt_remote_frame_stats(&window.session).full_frames,"closed session releases transport counters");
    attach(&clients[0],false);
    require(clients[0].child==child && bt_remote_frame_stats(&clients[0]).full_frames==1 &&
            !bt_remote_frame_stats(&clients[0]).delta_frames,"reconnect starts fresh counters and complete snapshot without replacing child");
    report("persistent graphical delta: transport accounting, decoded pixels, four-byte GPU update, idle and reconnect");
}
static void desktop_sized_deltas(void) {
    enum { FRAMES=12, PIXELS=1920*1080*4, PATCH=64*32*4 };
    command(&clients[0],'s');
    detach(&clients[0]); memset(&window,0,sizeof(window));
    require(!bt_window_attach(&window,root,"main","monospace",18,false,false),"attach desktop-sized graphical fixture");
    window_open=true;
    for(unsigned i=0;i<3;++i) require(bt_window_pump(&window,5)>=0,"settle desktop-sized view geometry");
    attach(&clients[1],true); attach(&clients[2],true);
    parsed(&window.session,progress.bytes); parsed(&clients[1],progress.bytes);
    require(!bt_renderer_draw(window.renderer,&window.session,true,false),"upload desktop-sized initial texture");
    const BtPresentationImage *initial=find_image(window.session.presentation,902);
    require(initial && initial->length==PIXELS,"fixture retains full 1920x1080 RGBA image despite viewport clipping");
    BtRemoteFrameStats before=bt_remote_frame_stats(&window.session),observer_before=bt_remote_frame_stats(&clients[1]);
    BtImageStats gpu_before=bt_renderer_image_stats(window.renderer);
    uint64_t total=0,maximum=0;
    for(unsigned i=0;i<FRAMES;++i) {
        uint64_t start=bt_millis(),step=progress.step;
        require(!bt_session_send(&window.session,"t",1) && !bt_remote_input_flush(&window.session),"send desktop animation damage");
        progress=await_progress(progress_path,step,'t');
        parsed(&window.session,progress.bytes); parsed(&clients[1],progress.bytes);
        require(!bt_renderer_draw(window.renderer,&window.session,true,false),"render desktop-sized incremental image");
        uint64_t elapsed=bt_millis()-start; total+=elapsed; if(elapsed>maximum) maximum=elapsed;
        const BtPresentationImage *a=find_image(window.session.presentation,902),*b=find_image(clients[1].presentation,902);
        size_t offset=((size_t)32*1920+32+8*i)*4;
        require(a && b && a->pixels[offset]==0 && a->pixels[offset+1]==(i&1?0:255) &&
                a->pixels[offset+2]==(i&1?255:0) && !memcmp(a->pixels+offset,b->pixels+offset,4),
                "controller and active observer reconstruct each animation patch");
        require(a->pixels[a->length-4]==255 && !a->pixels[a->length-3] && !a->pixels[a->length-2],
                "large unchanged image region survives every delta");
    }
    BtRemoteFrameStats after=bt_remote_frame_stats(&window.session),observer_after=bt_remote_frame_stats(&clients[1]);
    BtImageStats gpu_after=bt_renderer_image_stats(window.renderer);
    require(after.delta_frames-before.delta_frames==FRAMES && after.full_frames==before.full_frames &&
            observer_after.delta_frames-observer_before.delta_frames==FRAMES && observer_after.full_frames==observer_before.full_frames,
            "active desktop consumers stay on incremental image publications");
    require(after.delta_bytes-before.delta_bytes<(uint64_t)FRAMES*PIXELS/20,
            "desktop animation payload is below five percent of raw full-image bytes");
    require(gpu_after.full_uploads==gpu_before.full_uploads && gpu_after.region_uploads-gpu_before.region_uploads==FRAMES &&
            gpu_after.uploaded_bytes-gpu_before.uploaded_bytes==(uint64_t)FRAMES*PATCH,
            "desktop animation uploads exactly the changed rectangles");
    require(gpu_after.texture_bytes==gpu_before.texture_bytes && gpu_after.shadow_bytes==gpu_before.shadow_bytes,
            "animated generations do not grow GPU or shadow caches");
    BtRemoteFrameStats lag_before=bt_remote_frame_stats(&clients[2]);
    parsed(&clients[2],progress.bytes);
    BtRemoteFrameStats lag_after=bt_remote_frame_stats(&clients[2]);
    require(lag_after.full_frames==lag_before.full_frames+1 && lag_after.delta_frames==lag_before.delta_frames,
            "idle desktop observer resynchronizes once without replaying animation history");
    require(!memcmp(find_image(clients[2].presentation,902)->pixels,find_image(window.session.presentation,902)->pixels,PIXELS),
            "lagging observer full snapshot matches every final animation pixel");
    for(unsigned i=0;i<5;++i) require(bt_session_pump(&window.session,5)>=0,"poll idle desktop-sized image");
    BtRemoteFrameStats idle=bt_remote_frame_stats(&window.session);
    require(idle.full_bytes==after.full_bytes && idle.delta_bytes==after.delta_bytes,"idle desktop image sends no additional payload");
    printf("PASS desktop image workload: %u frames, %llu ms total/%llu ms max, %llu payload bytes, %llu GPU bytes versus %llu raw full-image bytes\n",
            FRAMES,(unsigned long long)total,(unsigned long long)maximum,
            (unsigned long long)(after.delta_bytes-before.delta_bytes),(unsigned long long)(gpu_after.uploaded_bytes-gpu_before.uploaded_bytes),
            (unsigned long long)((uint64_t)FRAMES*PIXELS));
    detach(&clients[1]); detach(&clients[2]);
    bt_window_close(&window); window_open=false; attach(&clients[0],false);
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
static void reset_owner(void) {
    BtSession *controller=&clients[0];
    require(contains(controller,"PRIMARY_SURVIVES"),"owner has text before clear");
    pid_t child=controller->child;
    uint64_t revision=controller->presentation->revision;
    attach(&clients[1],true);
    require(bt_session_reset(&clients[1])<0 && errno==EPERM,"observer cannot clear authoritative terminal");
    detach(&clients[1]);
    require(!bt_session_reset(controller),"clear terminal at persistent PTY owner");
    uint64_t deadline=bt_millis()+3000;
    while(controller->presentation->revision==revision && bt_millis()<deadline)
        require(bt_session_pump(controller,5)>=0,"publish cleared owner presentation");
    require(controller->presentation->revision>revision && !contains(controller,"PRIMARY_SURVIVES") &&
            controller->child==child,"clear removes owner text without restarting its child");
    detach(controller);
    attach(controller,false);
    require(!contains(controller,"PRIMARY_SURVIVES") && controller->child==child,
            "cleared owner state survives frontend replacement");
    report("persistent terminal clear is owner-side, observer-denied and survives reattachment");
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
        const char *tmp=getenv("TMPDIR");
        if(!tmp || !*tmp) tmp="/tmp";
        require(snprintf(root,sizeof(root),"%s/bt-persist-XXXXXX",tmp)<(int)sizeof(root),
                "bounded private test root");
        require(mkdtemp(root)!=NULL,"create isolated private test root");
    }
    require(atexit(cleanup)==0,"register private session cleanup");
    path(progress_path,sizeof(progress_path),root,"main.progress");
    recording_lifetime();
    startup_and_ownership();
    nonblocking_frames();
    capture_publication();
    delta_codec();
    graphics_state();
    live_delta_publication();
    observers_and_codec();
    persistent_damage_window();
    desktop_sized_deltas();
    fresh_windows();
    reset_owner();
    exit_and_failures();
    printf("PASS persistence: %u assertions; no test sessions retained\n",assertions);
    return 0;
}
