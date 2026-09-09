/* SPDX-License-Identifier: MIT */
#define _GNU_SOURCE
#include "window.h"
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <termios.h>
#include <unistd.h>
extern char **environ;
static BtWindow window;
static void require(bool ok,const char *message) {
    if(!ok) { fprintf(stderr,"FAIL: %s (%s)\n",message,window.error); bt_window_close(&window); exit(1); }
}
static void expect_input(const char *expected,size_t length) {
    char bytes[128]; size_t used=0;
    while(used<length) { ssize_t n=read(0,bytes+used,length-used); if(n<=0)_exit(20); used+=n; }
    if(memcmp(bytes,expected,length)) { printf("BAD_INPUT\r\n"); _exit(21); }
}
static int child(void) {
    struct termios term; tcgetattr(0,&term); cfmakeraw(&term); tcsetattr(0,TCSANOW,&term);
    setvbuf(stdout,NULL,_IONBF,0);
    printf("\033[2J\033[H\033[1;38;2;125;211;252mBatty\033[0m  terminal prototype\r\n\r\n");
    printf("  \033[32mgreen\033[0m  \033[31mred\033[0m  \033[34mblue\033[0m  \033[38;2;220;160;90mtrue color\033[0m\r\n");
    printf("  \033[1mbold\033[0m  \033[3mitalic\033[0m  \033[4munderline\033[0m  \033[7m inverse \033[0m\r\n\r\n");
    printf("  caf\303\251   e\314\201   Ελληνικά   日本語   →  λ\r\n");
    printf("  ligatures: -> => != ===   combining: A\314\212\r\n\r\n");
    printf("batty$ "); expect_input("hello\r",6); printf("hello\r\nINPUT_OK\r\n");
    printf("\033[?2004hPASTE_READY\r\n");
    expect_input("\033[200~pasted\033[201~",18); printf("\033[?2004lPASTE_OK\r\n");
    printf("\033[6n");
    char reply[64]; size_t n=0;
    while(n<sizeof(reply)) { if(read(0,reply+n,1)!=1)return 22; if(reply[n++]=='R')break; }
    if(n<4 || reply[0]!=27 || reply[1]!='[') return 23;
    printf("QUERY_OK\r\n\033[?1000h\033[?1006hMOUSE_READY\r\n");
    expect_input("\033[<0;3;4M",9);
    printf("\033[?1000l\033[?1006lMOUSE_OK\r\n\r\nReady.\r\n");
    expect_input("q",1); return 0;
}
static bool contains(const char *needle) {
    size_t len; char *text=bt_session_text(&window.session,false,&len);
    bool ok=text && strstr(text,needle); free(text); return ok;
}
static void until(const char *needle) {
    uint64_t end=bt_millis()+6000;
    while(!contains(needle) && !window.session.done && bt_millis()<end)
        require(!bt_window_pump(&window,5),"pump window");
    if(!contains(needle)) {
        size_t len; char *text=bt_session_text(&window.session,false,&len);
        if(text) { fprintf(stderr,"SCREEN: %s\n",text); free(text); }
    }
    require(contains(needle),needle);
}
static void push_key(SDL_Scancode sc,SDL_Keycode sym,SDL_Keymod mods,const char *text) {
    SDL_Event event={.type=SDL_KEYDOWN};
    event.key.windowID=window.window_id; event.key.keysym=(SDL_Keysym){.scancode=sc,.sym=sym,.mod=mods};
    require(SDL_PushEvent(&event)==1,"push key event");
    if(text) {
        SDL_Event input={.type=SDL_TEXTINPUT}; input.text.windowID=window.window_id;
        snprintf(input.text.text,sizeof(input.text.text),"%s",text);
        require(SDL_PushEvent(&input)==1,"push text event");
    }
    event.type=SDL_KEYUP; require(SDL_PushEvent(&event)==1,"push key release");
}
static int application(const char *program,bool editor) {
    char helper[PATH_MAX]; require(realpath("build/batty-session",helper)!=NULL,"helper");
    const char *path=editor?"build/editor-smoke.txt":"build/pager-smoke.txt";
    if(editor) remove(path);
    else {
        FILE *file=fopen(path,"w"); require(file!=NULL,"pager fixture");
        for(int i=1;i<=200;++i) fprintf(file,"LINE %03d: Batty pager integration\n",i);
        fclose(file);
    }
    char *vim_args[]={(char *)program,"-Nu","NONE","-i","NONE","-n","-N","--",(char *)path,NULL};
    char *pager_args[]={(char *)program,"-R","--",(char *)path,NULL};
    require(!bt_window_open(&window,helper,editor?vim_args:pager_args,environ,90,26,"monospace",16,false),"open application window");
    until(editor?"editor-smoke.txt":"LINE 001");
    const char *input=editor?"iBatty editor smoke\033":" ";
    require(!bt_session_send(&window.session,input,strlen(input)),"application input");
    until(editor?"Batty editor smoke":"LINE 040");
    SDL_SetWindowSize(window.window,900,600);
    for(int i=0;i<8;++i) require(!bt_window_pump(&window,5),"application resize");
    input=editor?":wq\r":"q";
    require(!bt_session_send(&window.session,input,strlen(input)),"exit application");
    uint64_t deadline=bt_millis()+6000;
    while(!window.session.done && bt_millis()<deadline) require(!bt_window_pump(&window,5),"application completion");
    require(window.session.done && window.session.exit_status==0,"application exit status");
    bt_window_close(&window);
    if(editor) {
        FILE *file=fopen(path,"r"); require(file!=NULL,"editor saved file");
        char text[128]; require(fgets(text,sizeof(text),file)!=NULL,"editor file contents"); fclose(file);
        require(!strcmp(text,"Batty editor smoke\n"),"saved editor text");
    }
    puts(editor?"PASS Vim insert, resize, save and exit in native window":"PASS less paging, resize and exit in native window");
    return 0;
}
int main(int argc,char **argv) {
    if(argc==2 && !strcmp(argv[1],"--child")) return child();
    if(argc==3 && !strcmp(argv[1],"--editor")) return application(argv[2],true);
    if(argc==3 && !strcmp(argv[1],"--pager")) return application(argv[2],false);
    char executable[PATH_MAX],helper[PATH_MAX];
    require(realpath(argv[0],executable)!=NULL,"test executable");
    require(realpath("build/batty-session",helper)!=NULL,"helper");
    char *args[]={executable,"--child",NULL};
    require(!bt_window_open(&window,helper,args,environ,90,26,"monospace",18,false),"open GPU terminal window");
    until("batty$");
    const char *input="hello";
    for(const char *p=input;*p;++p) { char text[2]={*p,0}; push_key(SDL_SCANCODE_A+(*p-'a'),*p,KMOD_NONE,text); }
    push_key(SDL_SCANCODE_RETURN,SDLK_RETURN,KMOD_NONE,NULL);
    until("PASTE_READY");
    SDL_SetClipboardText("pasted");
    push_key(SDL_SCANCODE_V,SDLK_v,KMOD_CTRL|KMOD_SHIFT,NULL);
    until("MOUSE_READY");
    int cw,ch; bt_renderer_metrics(window.renderer,&cw,&ch);
    SDL_Event mouse={.type=SDL_MOUSEBUTTONDOWN}; mouse.button.windowID=window.window_id;
    mouse.button.button=SDL_BUTTON_LEFT; mouse.button.x=8+cw*2+2; mouse.button.y=8+ch*3+2;
    require(SDL_PushEvent(&mouse)==1,"push mouse event");
    until("MOUSE_OK");
    SDL_Event start={.type=SDL_MOUSEBUTTONDOWN}; start.button.windowID=window.window_id;
    start.button.button=SDL_BUTTON_LEFT; start.button.x=9; start.button.y=9;
    SDL_Event finish=start; finish.type=SDL_MOUSEBUTTONUP; finish.button.x=8+cw+2;
    require(!bt_window_event(&window,&start) && !bt_window_event(&window,&finish),"select cells");
    push_key(SDL_SCANCODE_C,SDLK_c,KMOD_CTRL|KMOD_SHIFT,NULL);
    require(!bt_window_pump(&window,5),"copy selected text");
    char *clipboard=SDL_GetClipboardText(); require(clipboard && !strcmp(clipboard,"Ba"),"clipboard selection contents"); SDL_free(clipboard);
    SDL_SetWindowSize(window.window,1000,680);
    for(int i=0;i<8;++i) require(!bt_window_pump(&window,5),"resize window");
    require(window.session.cols==(1000-16)/cw && window.session.rows==(680-16)/ch,"window-to-PTY geometry");
    window.force_draw=true; require(!bt_window_pump(&window,0),"draw screenshot frame");
    require(!bt_renderer_capture(window.renderer,"build/window-test.ppm"),"capture rendered pixels");
    push_key(SDL_SCANCODE_Q,SDLK_q,KMOD_NONE,"q");
    uint64_t end=bt_millis()+3000;
    while(!window.session.done && bt_millis()<end) require(!bt_window_pump(&window,5),"wait for window child");
    require(window.session.done && window.session.exit_status==0,"window child status");
    bt_window_close(&window);
    puts("PASS GLES window, shaped text, keyboard, bracketed clipboard paste, reply relay, mouse, selection and resize");
    return 0;
}
