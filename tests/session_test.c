/* SPDX-License-Identifier: MIT */
#define _GNU_SOURCE
#include "session.h"
#include "recovery.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <termios.h>
#include <unistd.h>

extern char **environ;
static char executable[PATH_MAX], helper[PATH_MAX];
static volatile sig_atomic_t signaled;
static void signal_seen(int sig) { (void)sig; signaled=1; }
static void require(bool good, const char *message) {
    if(!good) { fprintf(stderr,"FAIL: %s\n",message); exit(1); }
}
static void raw(void) { struct termios t; tcgetattr(0,&t); cfmakeraw(&t); tcsetattr(0,TCSANOW,&t); }
static bool reply_is(const char *expected) {
    for(size_t i=0;expected[i];++i) {
        char byte; ssize_t n;
        do { n=read(0,&byte,1); } while(n<0 && errno==EINTR);
        if(n!=1 || byte!=expected[i]) return false;
    }
    return true;
}
static int child(const char *mode) {
    setvbuf(stdout,NULL,_IONBF,0);
    if(!strcmp(mode,"exit3")) return 3;
    if(!strcmp(mode,"exit4")) return 4;
    if(!strcmp(mode,"record")) {
        printf("BEFORE\033_Ga=t,f=24,s=1,v=1;AAAA\033\\AFTER\r\n");
        return 0;
    }
    if(!strcmp(mode,"query")) {
        raw(); printf("\033[2;4H\033[6n");
        char answer[6]; size_t pos=0;
        while(pos<sizeof(answer)) { ssize_t n=read(0,answer+pos,sizeof(answer)-pos); if(n<=0)return 10; pos+=n; }
        if(memcmp(answer,"\033[2;4R",6)) return 11;
        printf("REPLY_OK\r\n"); return 7;
    }
    if(!strcmp(mode,"graphics-query")) {
        raw();
        printf("\033[c\033[?80$p\033[?8452$p\033[?1070$p");
        if(!reply_is("\033[?62;4;22c\033[?80;2$y\033[?8452;2$y\033[?1070;2$y")) return 20;
        /* C0 controls execute without becoming mode parameter bytes. */
        printf("\033[?8\0070;8452;1070h\033[?80$p\033[?8452$p\033[?1070$p");
        if(!reply_is("\033[?80;1$y\033[?8452;1$y\033[?1070;1$y")) return 21;
        printf("\033[?80;8452;1070l\033[?80$p\033[?8452$p\033[?1070$p");
        if(!reply_is("\033[?80;2$y\033[?8452;2$y\033[?1070;2$y")) return 22;
        printf("\033[?80;8452;1070h\033c\033[?80$p\033[?8452$p\033[?1070$p\033[6n");
        if(!reply_is("\033[?80;2$y\033[?8452;2$y\033[?1070;2$y\033[1;1R")) return 23;
        /* Intercepted queries must still end the string that their ESC
         * interrupted. Otherwise later text becomes part of the old OSC/APC. */
        printf("\033c\033]0;query-title\033[?80$pOSC_OK\033[6n");
        if(!reply_is("\033[?80;2$y\033[1;7R")) return 24;
        printf("\033c\033_ignored\033[?8452$pAPC_OK\033[6n");
        if(!reply_is("\033[?8452;2$y\033[1;7R")) return 25;
        /* An empty Sixel emits no internal image commands that could mask an
         * underlying parser left inside the interrupted OSC. */
        printf("\033c\033]0;sixel-title\033Pq\033\\SIXEL_OK\033[6n");
        if(!reply_is("\033[1;9R")) return 26;
        printf("GRAPHICS_QUERY_OK\r\n"); return 0;
    }
    if(!strcmp(mode,"paste")) {
        raw(); printf("PASTE_READY\r\n"); usleep(250000);
        size_t total=0;
        while(total<2u*1024u*1024u) {
            uint8_t bytes[8192]; ssize_t n=read(0,bytes,sizeof(bytes)); if(n<=0)return 12;
            for(ssize_t i=0;i<n;++i) if(bytes[i]!=(uint8_t)((total+(size_t)i)*31)) return 13;
            total+=(size_t)n;
        }
        printf("PASTE_OK\r\n"); return 0;
    }
    if(!strcmp(mode,"resize")) {
        raw();
        signal(SIGWINCH,signal_seen);
        struct winsize ws; ioctl(0,TIOCGWINSZ,&ws);
        printf("\033[?2048h");
        if(!reply_is("\033[48;24;80;384;640t")) return 16;
        printf("INITIAL=%ux%u\r\n",ws.ws_col,ws.ws_row);
        while(!signaled) usleep(10000);
        if(!reply_is("\033[48;10;42;160;336t")) return 17;
        printf("\033[14t\033[16t\033[18t");
        if(!reply_is("\033[4;160;336t\033[6;16;8t\033[8;10;42t")) return 18;
        ioctl(0,TIOCGWINSZ,&ws);
        printf("RESIZED=%ux%u\r\n",ws.ws_col,ws.ws_row);
        return ws.ws_col==42 && ws.ws_row==10?0:14;
    }
    if(!strcmp(mode,"signal")) {
        signal(SIGINT,signal_seen);
        printf("SIGNAL_READY\r\n");
        while(!signaled) usleep(10000);
        printf("SIGNAL_OK\r\n"); return 0;
    }
    if(!strcmp(mode,"reflow")) { raw(); printf("abcdefghij"); for(;;) pause(); }
    if(!strcmp(mode,"stubborn")) {
        signal(SIGHUP,SIG_IGN); signal(SIGTERM,SIG_IGN);
        printf("STUBBORN_READY\r\n"); for(;;) pause();
    }
    if(!strcmp(mode,"flood")) {
        printf("FLOOD_READY\r\n");
        char bytes[4096]; memset(bytes,'x',sizeof(bytes));
        for(;;) if(write(1,bytes,sizeof(bytes))<0) return 0;
    }
    return 15;
}
static void open_session(BtSession *s,const char *mode,unsigned cols,unsigned rows) {
    char *args[]={executable,"--child",(char *)mode,NULL};
    if(bt_session_open(s,helper,args,environ,cols,rows,8,16)) {
        fprintf(stderr,"open: %s\n",s->error); bt_session_close(s); exit(1);
    }
}
static bool contains(BtSession *s,const char *needle) {
    size_t len; char *text=bt_session_text(s,false,&len);
    bool result=text && strstr(text,needle); free(text); return result;
}
static void until(BtSession *s,const char *needle) {
    uint64_t end=bt_millis()+5000;
    while(bt_millis()<end && !contains(s,needle)) require(!bt_session_pump(s,10),s->error);
    require(contains(s,needle),needle);
}
static void done(BtSession *s,int status) {
    uint64_t end=bt_millis()+5000;
    while(!s->done && bt_millis()<end) require(!bt_session_pump(s,10),s->error);
    require(s->done,"child completion deadline"); require(s->exit_status==status,"child exit status");
}
static int fd_count(void) {
    DIR *d=opendir("/proc/self/fd"); require(d!=NULL,"open FD directory");
    int count=0; while(readdir(d)) ++count; closedir(d); return count;
}
static void recording_tests(void) {
    char root[PATH_MAX-128],path[PATH_MAX];
    const char *tmp=getenv("TMPDIR");
    if(!tmp || !*tmp) tmp="/tmp";
    require(snprintf(root,sizeof(root),"%s/batty-recording-test-XXXXXX",tmp)<(int)sizeof(root),
            "bounded transcript test directory");
    require(mkdtemp(root)!=NULL,"private transcript directory");
    require(!setenv("BATTY_TRANSCRIPT_DIR",root,1),"recording environment");
    BtSession s;
    open_session(&s,"record",80,24); done(&s,0);
    require(s.recorder && !s.recorder_error,"recorder started");
    uint64_t deadline=bt_millis()+3000;
    while(!s.recorder->complete && !s.recorder->error && bt_millis()<deadline)
        require(!bt_session_pump(&s,5),s.error);
    require(s.recorder->complete && !s.recorder->error,"worker acknowledges durable completion");
    snprintf(path,sizeof(path),"%s/%s.log",root,s.recorder->name);
    FILE *file=fopen(path,"rb"); require(file!=NULL,"recorded PTY output exists");
    char data[1024]={0}; size_t length=fread(data,1,sizeof(data)-1,file);
    require(!ferror(file) && length>0,"read transcript"); fclose(file);
    require(strstr(data,"BEFORE") && strstr(data,"AFTER") && strstr(data,"elided") && !strstr(data,"AAAA"),
            "live transcript preserves text and elides graphics payload");
    require(contains(&s,"BEFOREAFTER"),"recording leaves terminal parser output unchanged");
    bt_session_close(&s);
    require(!unlink(path),"remove completed transcript");
    strcpy(path+strlen(path)-4,".meta"); require(!unlink(path),"remove completed metadata");

    require(!chmod(root,0755),"make invalid transcript destination");
    open_session(&s,"query",80,24); done(&s,7);
    deadline=bt_millis()+3000;
    while(!s.recorder->error && bt_millis()<deadline) require(!bt_session_pump(&s,5),s.error);
    require(s.recorder->error && !s.error[0] && contains(&s,"REPLY_OK"),"disk worker failure preserves PTY query and exit status");
    bt_session_close(&s); require(!chmod(root,0700),"restore private directory");

    open_session(&s,"flood",80,24);
    require(s.recorder && !s.recorder->error,"flood recorder started");
    pid_t worker=s.recorder->worker;
    require(!kill(worker,SIGSTOP),"stop owned transcript worker");
    int status;
    require(waitpid(worker,&status,WUNTRACED)==worker && WIFSTOPPED(status),"worker confirmed stopped");
    deadline=bt_millis()+3000;
    while(!s.recorder->error && bt_millis()<deadline) require(!bt_session_pump(&s,1),s.error);
    require(s.recorder->error==EAGAIN || s.recorder->error==EWOULDBLOCK,"bounded recording channel reports overflow");
    uint64_t accepted=s.recorder->bytes;
    require(accepted>0 && accepted<2u*1024u*1024u,"bounded accepted recording bytes");
    require(!bt_session_send(&s,"\003",1),"input accepted while disk worker stopped");
    done(&s,130);
    require(s.recorder->bytes==accepted && !s.error[0],"failed recording never resumes or poisons terminal");
    snprintf(path,sizeof(path),"%s/%s.log",root,s.recorder->name);
    require(!kill(worker,SIGCONT),"resume owned stopped writer");
    bt_session_close(&s);
    /* Drive ordinary maintenance until the detached writer exits. */
    deadline=bt_millis()+3000;
    for(;;) {
        bt_recorder_pump(NULL,false);
        pid_t result=waitpid(worker,&status,WNOHANG);
        if(result==worker || (result<0 && errno==ECHILD)) break;
        require(bt_millis()<deadline,"detached transcript worker exits"); usleep(1000);
    }
    file=fopen(path,"rb"); require(file!=NULL,"overflow transcript exists");
    require(!fseek(file,-128,SEEK_END),"read interrupted transcript tail");
    memset(data,0,sizeof(data)); length=fread(data,1,sizeof(data)-1,file); fclose(file);
    require(length && strstr(data,"transcript interrupted"),"overflow records interruption instead of a false complete log");
    require(!unlink(path),"remove interrupted transcript");
    strcpy(path+strlen(path)-4,".meta"); require(!unlink(path) && !rmdir(root),"remove recording fixtures");
    unsetenv("BATTY_TRANSCRIPT_DIR");
    for(unsigned i=0;i<128;++i) bt_recorder_pump(NULL,false);
    puts("PASS live recording, graphics elision, disk failure isolation and stopped-writer backpressure");
}
static BtPresentation *recovery_frame(BtSession *s) {
    BtPresenter *presenter=bt_presenter_new(s->terminal);
    BtPresentation *frame=NULL;
    require(presenter && !bt_presenter_capture(presenter,1,1,s->cell_width,s->cell_height,true,&frame),
            "capture recovery presentation");
    bt_presenter_free(presenter); return frame;
}
static void recovery_tests(void) {
    const char *contents[]={
        "\033]10;rgb:ff/00/00\033\\\033]11;rgb:00/00/ff\033\\HELLO",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "HARD_ONE\r\nHARD_TWO",
        "HELLO\033[3;1H\033_Ga=T,f=32,s=1,v=1,i=5,c=1,r=2,C=1,q=2;/wAA/w==\033\\",
        ""
    };
    char path[]="/tmp/bt-recovery-output-XXXXXX";
    int fd=mkstemp(path); require(fd>=0,"create private recovery fixture"); close(fd);
    char setting[PATH_MAX+32]; snprintf(setting,sizeof(setting),"BATTY_RECOVERY_FILE=%s",path);
    char *args[]={"/bin/cat",NULL},*env[]={"BATTY_TRANSCRIPT_ENABLED=0",NULL};
    char *restoring[]={setting,"BATTY_TRANSCRIPT_ENABLED=0",NULL};
    for(unsigned i=0;i<sizeof(contents)/sizeof(*contents);++i) {
        BtSession source,recovered;
        require(!bt_session_open(&source,helper,args,env,20,6,10,20),"open recovery source");
        bt_session_feed(&source,contents[i],strlen(contents[i]));
        BtPresentation *before=recovery_frame(&source);
        uint8_t *data=NULL; size_t length=0;
        require(!bt_recovery_capture(&source,before,args,&data,&length),"serialize recovery output");
        fd=open(path,O_WRONLY|O_TRUNC|O_CLOEXEC);
        require(fd>=0 && write(fd,data,length)==(ssize_t)length,"write recovery fixture"); close(fd); free(data);
        require(!bt_session_open(&recovered,helper,args,restoring,i==1?40:20,6,10,20),"open recovered terminal");
        BtPresentation *after=recovery_frame(&recovered);
        if(i==0) {
            require(after->colors.foreground.r==255 && !after->colors.foreground.g && !after->colors.foreground.b &&
                    !after->colors.background.r && !after->colors.background.g && after->colors.background.b==255,
                    "preserve OSC default text and background colors");
            require(after->cells[0].length && after->codepoints[after->cells[0].offset]=='H' &&
                    after->cursor.viewport_has_value && after->cursor.viewport_y==1,
                    "sparse output remains visible above the fresh prompt");
        } else if(i==1 || i==2) {
            size_t used=0; char *text=bt_session_text(&recovered,false,&used);
            require(text && strstr(text,i==1?contents[i]:"HARD_ONE\nHARD_TWO"),
                    i==1?"soft-wrapped history reflows after widening":"hard line breaks survive recovery");
            free(text);
        } else if(i==3) {
            require(after->placement_count==1 && after->placements[0].geometry.viewport_row==2 &&
                    after->cursor.viewport_has_value && after->cursor.viewport_y==4,
                    "static graphics retain their anchor and the prompt follows their bottom edge");
            require(after->cells[0].length && after->codepoints[after->cells[0].offset]=='H',
                    "graphics placement does not displace sparse text");
        } else {
            require(after->cursor.viewport_has_value && !after->cursor.viewport_y,
                    "empty recovery starts at the first row");
        }
        bt_presentation_free(before); bt_presentation_free(after);
        bt_session_close(&source); bt_session_close(&recovered);
    }
    unlink(path);
    puts("PASS recovery colors, visible sparse output, soft-wrap reflow, hard newlines, static graphics and empty output");
}
int main(int argc,char **argv) {
    if(argc==3 && !strcmp(argv[1],"--child")) return child(argv[2]);
    require(realpath(argv[0],executable)!=NULL,"test executable path");
    require(realpath("build/batty-session",helper)!=NULL,"session helper path");
    recovery_tests();
    require(!unsetenv("BATTY_KITTY_LOCAL_FILES"),"isolate default file-medium policy");
    int baseline=fd_count();
    BtSession s,a,b;
    open_session(&s,"query",80,24); done(&s,7); require(contains(&s,"REPLY_OK"),"reply reached waiting child"); bt_session_close(&s);
    puts("PASS query reply relay and child status");
    open_session(&s,"graphics-query",80,24); done(&s,0);
    bool local_files=true;
    GhosttyString temporary_dir={0};
    require(ghostty_terminal_get(s.terminal,GHOSTTY_TERMINAL_DATA_KITTY_IMAGE_MEDIUM_FILE,&local_files)==GHOSTTY_SUCCESS &&
            ghostty_terminal_get(s.terminal,GHOSTTY_TERMINAL_DATA_KITTY_IMAGE_MEDIUM_TEMP_FILE,&temporary_dir)==GHOSTTY_SUCCESS &&
            !local_files && !temporary_dir.len,"standalone sessions disable local image files by default");
    require(contains(&s,"GRAPHICS_QUERY_OK"),"Sixel capabilities and mode queries reached waiting child");
    GhosttyString graphics_title={0};
    require(ghostty_terminal_get(s.terminal,GHOSTTY_TERMINAL_DATA_TITLE,&graphics_title)==GHOSTTY_SUCCESS &&
            graphics_title.len==strlen("sixel-title") && !memcmp(graphics_title.ptr,"sixel-title",graphics_title.len),
            "interrupted OSC title excludes subsequent terminal text");
    bt_session_close(&s);
    puts("PASS Sixel capability replies, mode set/reset, embedded controls and interrupted string framing");
    open_session(&s,"paste",80,24); until(&s,"PASTE_READY");
    size_t length=2u*1024u*1024u; uint8_t *bytes=malloc(length); require(bytes!=NULL,"paste allocation");
    for(size_t i=0;i<length;++i) bytes[i]=(uint8_t)(i*31);
    require(!bt_session_send(&s,bytes,length),"queue binary input"); free(bytes);
    require(!bt_session_pump(&s,0),s.error);
    require(s.pending_end>s.pending_start,"retain writes when child is not reading");
    done(&s,0); require(contains(&s,"PASTE_OK") && s.bytes_written==length,"lossless binary paste"); bt_session_close(&s);
    puts("PASS 2 MiB binary input under PTY backpressure");
    open_session(&s,"resize",80,24); until(&s,"INITIAL=80x24");
    require(!bt_session_resize(&s,42,10,8,16),"resize PTY"); done(&s,0); require(contains(&s,"RESIZED=42x10"),"SIGWINCH geometry"); bt_session_close(&s);
    puts("PASS initial geometry, resize signal and terminal size reports");
    open_session(&s,"signal",80,24); until(&s,"SIGNAL_READY");
    require(!bt_session_send(&s,"\003",1),"queue Ctrl-C"); done(&s,0); require(contains(&s,"SIGNAL_OK"),"PTY ISIG"); bt_session_close(&s);
    puts("PASS Ctrl-C delivery to foreground process");
    const char *bash=getenv("BATTY_BASH");
    require(bash!=NULL,"BATTY_BASH for interactive shell test");
    setenv("PS1","batty-test$ ",1);
    char *shell_args[]={(char *)bash,"--noprofile","--norc","-i",NULL};
    require(!bt_session_open(&s,helper,shell_args,environ,80,24,8,16),"interactive Bash");
    until(&s,"batty-test$");
    const char *shell_command="printf 'SHELL_%s\\n' OK\r";
    require(!bt_session_send(&s,shell_command,strlen(shell_command)),"send shell command"); until(&s,"SHELL_OK");
    /* bash-os has a sleep builtin; an external command is needed for job control. */
    const char *job="/bin/sleep 30\r";
    require(!bt_session_send(&s,job,strlen(job)),"start foreground job");
    uint64_t job_deadline=bt_millis()+3000;
    while(tcgetpgrp(s.master)==s.child && bt_millis()<job_deadline) require(!bt_session_pump(&s,5),s.error);
    require(tcgetpgrp(s.master)!=s.child,"foreground job process group");
    require(!bt_session_send(&s,"\032",1),"send Ctrl-Z"); until(&s,"Stopped");
    require(!bt_session_send(&s,"fg\r",3),"resume stopped job");
    job_deadline=bt_millis()+3000;
    while(tcgetpgrp(s.master)==s.child && bt_millis()<job_deadline) require(!bt_session_pump(&s,5),s.error);
    require(tcgetpgrp(s.master)!=s.child,"resumed foreground group");
    require(!bt_session_send(&s,"\003",1),"interrupt resumed job");
    job_deadline=bt_millis()+3000;
    while(tcgetpgrp(s.master)!=s.child && bt_millis()<job_deadline) require(!bt_session_pump(&s,5),s.error);
    require(tcgetpgrp(s.master)==s.child,"shell regained foreground group");
    shell_command="printf 'JOB_%s\\n' OK\rexit 6\r";
    require(!bt_session_send(&s,shell_command,strlen(shell_command)),"continue shell after job control");
    done(&s,6); require(contains(&s,"JOB_OK"),"interactive shell survived job control"); bt_session_close(&s);
    puts("PASS Bash readline, Ctrl-Z, fg and Ctrl-C job control");
    open_session(&s,"reflow",6,2); until(&s,"ghij");
    require(!bt_session_resize(&s,4,3,8,16),"resize core"); require(contains(&s,"abcd\nefgh\nij"),"active reflow"); bt_session_close(&s);
    puts("PASS active-screen reflow through session API");
    open_session(&a,"exit3",80,24); open_session(&b,"exit4",80,24);
    done(&a,3); done(&b,4); bt_session_close(&a); bt_session_close(&b);
    puts("PASS independent session statuses");
    char *bad[]={"/nonexistent-batty-test-command",NULL};
    require(bt_session_open(&s,helper,bad,environ,80,24,8,16)<0 && s.exec_error==ENOENT,"exec failure handshake"); bt_session_close(&s);
    puts("PASS failed exec cleanup");
    open_session(&s,"stubborn",80,24); until(&s,"STUBBORN_READY");
    pid_t pid=s.child; uint64_t start=bt_millis(); bt_session_close(&s);
    require(bt_millis()-start<1500,"bounded close"); require(kill(pid,0)<0 && errno==ESRCH,"stubborn child reaped");
    puts("PASS bounded shutdown of HUP/TERM-ignoring child");
    open_session(&s,"flood",80,24);
    for(int i=0;i<30;++i) {
        uint64_t before=s.bytes_read;
        require(!bt_session_pump(&s,1),s.error);
        require(s.bytes_read-before<=256u*1024u,"read budget");
    }
    require(!bt_session_send(&s,"\003",1),"interrupt output flood"); done(&s,130); bt_session_close(&s);
    puts("PASS bounded output reads and interrupt during flood");
    recording_tests();
    for(int i=0;i<5;++i) { open_session(&s,"exit3",80,24); done(&s,3); bt_session_close(&s); }
    require(fd_count()==baseline,"file descriptor leak");
    puts("PASS repeated session cleanup without FD leaks");
    return 0;
}
