/* SPDX-License-Identifier: MIT */
/* Real local and persistent PTYs: SDL input through all Kitty keyboard flags. */
#define _GNU_SOURCE
#include "window.h"
#include "remote.h"
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <termios.h>
#include <unistd.h>

extern char **environ;
static BtWindow window;
static char helper[PATH_MAX],service[PATH_MAX],self[PATH_MAX];
static const char *root;
static bool persistent;
static unsigned captures;
static void require(bool ok, const char *message) {
    if(ok) return;
    fprintf(stderr,"FAIL keyboard (%s): %s (%s; %s)\n",persistent?"persistent":"local",message,window.error,window.session.error);
    bt_window_close(&window);
    if(persistent) { char error[256]; (void)bt_remote_terminate(root,"keyboard",error,sizeof(error)); }
    exit(1);
}
static int child(void) {
    struct termios term;
    if(tcgetattr(0,&term)) return 2;
    cfmakeraw(&term);
    if(tcsetattr(0,TCSANOW,&term)) return 2;
    setvbuf(stdout,NULL,_IONBF,0);
    printf("READY\r\n");
    uint8_t bytes[4096],c; size_t used=0; unsigned queries=0;
    while(read(0,&c,1)==1) {
        if(c==254) { used=0; printf("\033[>31uFLAGS31\r\n"); continue; }
        if(c==253) { used=0; printf("\033[<u\033[>4;0mLEGACY\r\n"); continue; }
        if(c==252) { used=0; printf("\033[>4;2mMODIFY2\r\n"); continue; }
        if(c==251) { used=0; printf("\033[?1049h\033[?1007h\033[>31uALT_SCROLL\r\n"); continue; }
        if(c==250) { used=0; printf("\033[?1049lMAIN_SCREEN\r\n"); continue; }
        if(c==255) {
            char report[9000];
            int at=snprintf(report,sizeof(report),"\033[2J\033[3J\033[HKEYS%u:",++queries);
            for(size_t i=0;i<used;++i) at+=snprintf(report+at,sizeof(report)-(size_t)at,"%02x",bytes[i]);
            snprintf(report+at,sizeof(report)-(size_t)at,"|END%u|\r\n",queries);
            printf("%s",report); used=0; continue;
        }
        if(used==sizeof(bytes)) return 3;
        bytes[used++]=c;
    }
    return 0;
}
static void until(const char *needle) {
    uint64_t deadline=bt_millis()+5000;
    for(;;) {
        size_t length=0; char *text=bt_session_text(&window.session,false,&length);
        bool found=text && strstr(text,needle); free(text);
        if(found) return;
        if(bt_millis()>=deadline) {
            text=bt_session_text(&window.session,false,&length);
            fprintf(stderr,"Captured terminal: %s\n",text?text:"(unavailable)"); free(text);
            require(false,needle);
        }
        require(!bt_session_pump(&window.session,5),"pump keyboard fixture");
    }
}
static void command(uint8_t c, const char *ready) {
    require(!bt_session_send(&window.session,&c,1),"send fixture mode"); until(ready);
}
static void expect(const char *sequence) {
    char expected[8192],prefix[32],complete[32];
    snprintf(prefix,sizeof(prefix),"KEYS%u:",++captures);
    snprintf(complete,sizeof(complete),"|END%u|",captures);
    size_t at=(size_t)snprintf(expected,sizeof(expected),"%s",prefix);
    for(const unsigned char *p=(const unsigned char *)sequence;*p;++p) {
        require(at+2<sizeof(expected),"expected sequence bound");
        snprintf(expected+at,sizeof(expected)-at,"%02x",*p); at+=2;
    }
    /* Clear previous reports so an identical expected sequence cannot hide a failure. */
    require(!bt_session_send(&window.session,"\xff",1),"request captured input");
    until(complete);
    size_t length=0; char *text=bt_session_text(&window.session,false,&length);
    char *line=text?strstr(text,prefix):NULL;
    if(!line || strncmp(line,expected,at) || strncmp(line+at,complete,strlen(complete))) {
        fprintf(stderr,"Expected: %s%s\nActual: %s\n",expected,complete,line?line:"(missing)");
        require(false,"exact keyboard bytes");
    }
    free(text);
}
static void key(SDL_Scancode sc, SDL_Keycode code, SDL_Keymod mods, bool press, bool repeat) {
    SDL_Event event={.type=press?SDL_KEYDOWN:SDL_KEYUP};
    event.key.keysym=(SDL_Keysym){.scancode=sc,.sym=code,.mod=mods};
    event.key.repeat=repeat;
    require(!bt_window_event(&window,&event),"deliver SDL key");
}
static void text(const char *utf8) {
    SDL_Event event={.type=SDL_TEXTINPUT};
    snprintf(event.text.text,sizeof(event.text.text),"%s",utf8);
    require(!bt_window_event(&window,&event),"deliver SDL text");
}
static void tests(void) {
    until("READY"); command(254,"FLAGS31");
    key(SDL_SCANCODE_A,SDLK_a,KMOD_SHIFT,true,false); text("A");
    key(SDL_SCANCODE_A,SDLK_a,KMOD_SHIFT,true,true); text("A");
    key(SDL_SCANCODE_A,SDLK_a,KMOD_SHIFT,false,false);
    expect("\033[97:65;2;65u\033[97:65;2:2;65u\033[97;2:3u");

    key(SDL_SCANCODE_Q,233,KMOD_NONE,true,false); text("é");
    key(SDL_SCANCODE_Q,233,KMOD_NONE,false,false);
    expect("\033[233::113;;233u\033[233::113;1:3u");

    key(SDL_SCANCODE_A,SDLK_a,KMOD_NONE,true,false); text("á");
    key(SDL_SCANCODE_A,SDLK_a,KMOD_NONE,false,false);
    expect("\033[97;;97:769u\033[97;1:3u");

    SDL_Keymod altgr=KMOD_RALT|KMOD_LCTRL|KMOD_MODE;
    key(SDL_SCANCODE_Q,SDLK_q,altgr,true,false); text("@");
    key(SDL_SCANCODE_Q,SDLK_q,altgr,false,false);
    expect("\033[113;;64u\033[113;1:3u");

    key(SDL_SCANCODE_C,SDLK_c,altgr|KMOD_LSHIFT,true,false); text("Ç");
    key(SDL_SCANCODE_C,SDLK_c,altgr|KMOD_LSHIFT,false,false);
    expect("\033[99:199;2;199u\033[99;2:3u");

    key(SDL_SCANCODE_KP_ENTER,SDLK_KP_ENTER,KMOD_NONE,true,false);
    key(SDL_SCANCODE_KP_ENTER,SDLK_KP_ENTER,KMOD_NONE,false,false);
    expect("\033[57414u\033[57414;1:3u");

    key(SDL_SCANCODE_KP_1,SDLK_KP_1,KMOD_NUM,true,false);
    text("1");
    key(SDL_SCANCODE_KP_1,SDLK_KP_1,KMOD_NUM,false,false);
    expect("\033[57400;129;49u\033[57400::49;129:3u");

    key(SDL_SCANCODE_KP_1,SDLK_KP_1,KMOD_NONE,true,false);
    key(SDL_SCANCODE_KP_1,SDLK_KP_1,KMOD_NONE,false,false);
    expect("\033[57400::49u\033[57400::49;1:3u");

    key(SDL_SCANCODE_KP_PLUS,SDLK_KP_PLUS,KMOD_NONE,true,false); text("+");
    key(SDL_SCANCODE_KP_PLUS,SDLK_KP_PLUS,KMOD_NONE,false,false);
    expect("\033[57413;;43u\033[57413::43;1:3u");

    key(SDL_SCANCODE_A,SDLK_a,KMOD_CAPS,true,false); text("A");
    key(SDL_SCANCODE_A,SDLK_a,KMOD_CAPS,false,false);
    expect("\033[97;65;65u\033[97;65:3u");

    key(SDL_SCANCODE_RCTRL,SDLK_RCTRL,KMOD_RCTRL,true,false);
    key(SDL_SCANCODE_RCTRL,SDLK_RCTRL,KMOD_NONE,false,false);
    expect("\033[57448;5u\033[57448;1:3u");

    /* No text event may arrive for an Alt shortcut. Preserve the complete
     * physical lifecycle, including repeated presses, before the release. */
    key(SDL_SCANCODE_SPACE,SDLK_SPACE,KMOD_ALT,true,false);
    key(SDL_SCANCODE_SPACE,SDLK_SPACE,KMOD_ALT,true,true);
    key(SDL_SCANCODE_SPACE,SDLK_SPACE,KMOD_ALT,false,false);
    expect("\033[32;3u\033[32;3:2u\033[32;3:3u");

    key(SDL_SCANCODE_SPACE,SDLK_SPACE,KMOD_ALT,true,false);
    require(!bt_window_pump(&window,0),"dispatch a no-text Alt press before key release");
    expect("\033[32;3u");
    key(SDL_SCANCODE_SPACE,SDLK_SPACE,KMOD_ALT,false,false);
    expect("\033[32;3:3u");

    key(SDL_SCANCODE_F24,SDLK_F24,KMOD_NONE,true,false);
    key(SDL_SCANCODE_F24,SDLK_F24,KMOD_NONE,false,false);
    expect("\033[57387u\033[57387;1:3u");

    key(SDL_SCANCODE_A,SDLK_a,KMOD_NONE,true,false); text("a");
    require(!bt_window_release_keys(&window),"release held key on focus loss");
    key(SDL_SCANCODE_A,SDLK_a,KMOD_NONE,false,false);
    expect("\033[97;;97u\033[97;1:3u");

    SDL_Event event={.type=SDL_TEXTEDITING};
    snprintf(event.edit.text,sizeof(event.edit.text),"λ");
    require(!bt_window_event(&window,&event),"start composition with all flags");
    key(SDL_SCANCODE_LEFT,SDLK_LEFT,KMOD_NONE,true,false);
    key(SDL_SCANCODE_LEFT,SDLK_LEFT,KMOD_NONE,false,false);
    text("λ界");
    expect("λ界");

    command(251,"ALT_SCROLL");
    SDL_Event wheel={.type=SDL_MOUSEWHEEL}; wheel.wheel.y=1;
    require(!bt_window_event(&window,&wheel),"scroll alternate screen upward");
    expect("\033[1;1:1A\033[1;1:1A\033[1;1:1A");
    require(!bt_window_release_keys(&window),"release focus after alternate scroll");
    expect("");

    key(SDL_SCANCODE_UP,SDLK_UP,KMOD_CTRL,true,false);
    expect("\033[1;5:1A");
    require(!bt_window_event(&window,&wheel),"scroll while the Up key is held");
    expect("\033[1;1:1A\033[1;1:1A\033[1;1:1A");
    require(!bt_window_release_keys(&window),"release the physical Up key after scrolling");
    expect("\033[1;5:3A");
    key(SDL_SCANCODE_UP,SDLK_UP,KMOD_NONE,false,false);
    expect("");
    command(250,"MAIN_SCREEN");

    command(253,"LEGACY");
    key(SDL_SCANCODE_SPACE,SDLK_SPACE,KMOD_ALT,true,false);
    key(SDL_SCANCODE_SPACE,SDLK_SPACE,KMOD_ALT,false,false);
    expect("\033 ");
    key(SDL_SCANCODE_A,SDLK_a,KMOD_NONE,true,false); text("a");
    key(SDL_SCANCODE_A,SDLK_a,KMOD_NONE,false,false);
    expect("a");
    key(SDL_SCANCODE_KP_1,SDLK_KP_1,KMOD_NUM,true,false); text("1");
    key(SDL_SCANCODE_KP_1,SDLK_KP_1,KMOD_NUM,false,false);
    expect("1");
    key(SDL_SCANCODE_Q,SDLK_q,altgr,true,false); text("@");
    key(SDL_SCANCODE_Q,SDLK_q,altgr,false,false);
    expect("@");
    key(SDL_SCANCODE_UP,SDLK_UP,KMOD_NONE,true,false);
    key(SDL_SCANCODE_UP,SDLK_UP,KMOD_NONE,false,false);
    expect("\033[A");

    command(252,"MODIFY2");
    key(SDL_SCANCODE_A,SDLK_a,KMOD_CTRL,true,false);
    key(SDL_SCANCODE_A,SDLK_a,KMOD_CTRL,false,false);
    expect("\033[27;5;97~");
}
int main(int argc,char **argv) {
    if(argc==2 && !strcmp(argv[1],"--child")) return child();
    require(realpath("build/batty-session",helper)!=NULL && realpath("build/batty-state",service)!=NULL,"resolve native helpers");
    ssize_t n=readlink("/proc/self/exe",self,sizeof(self)-1); require(n>0,"resolve fixture executable"); self[n]=0;
    root=getenv("BATTY_TEST_SESSION_DIR"); require(root && *root,"isolated persistent root");
    char *args[]={self,"--child",NULL};
    for(unsigned i=0;i<2;++i) {
        persistent=i!=0;
        captures=0;
        require(!(persistent?bt_window_open_persistent(&window,service,helper,root,"keyboard",args,environ,200,24,"monospace",16,true):
                             bt_window_open(&window,helper,args,environ,200,24,"monospace",16,true)),"open keyboard fixture");
        tests(); bt_window_close(&window);
        if(persistent) { char error[256]; require(!bt_remote_terminate(root,"keyboard",error,sizeof(error)),"terminate persistent fixture"); }
    }
    puts("PASS keyboard: all Kitty flags, associated Unicode, alternate keys, repeat/release, keypad, lock/side modifiers, no-text keys, focus, IME, legacy and modifyOtherKeys through local and persistent PTYs");
    return 0;
}
