/* SPDX-License-Identifier: MIT */
#define _GNU_SOURCE
#include "session.h"
#include <dirent.h>
#include <errno.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
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
int main(int argc,char **argv) {
    if(argc==3 && !strcmp(argv[1],"--child")) return child(argv[2]);
    require(realpath(argv[0],executable)!=NULL,"test executable path");
    require(realpath("build/batty-session",helper)!=NULL,"session helper path");
    int baseline=fd_count();
    BtSession s,a,b;
    open_session(&s,"query",80,24); done(&s,7); require(contains(&s,"REPLY_OK"),"reply reached waiting child"); bt_session_close(&s);
    puts("PASS query reply relay and child status");
    open_session(&s,"graphics-query",80,24); done(&s,0);
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
    for(int i=0;i<5;++i) { open_session(&s,"exit3",80,24); done(&s,3); bt_session_close(&s); }
    require(fd_count()==baseline,"file descriptor leak");
    puts("PASS repeated session cleanup without FD leaks");
    return 0;
}
