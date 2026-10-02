/* SPDX-License-Identifier: MIT */
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
    expect_input("\033[<0;3;4m",9);
    printf("\033[?1000l\033[?1006lMOUSE_OK\r\n\r\nReady.\r\n");
    expect_input("q",1); return 0;
}
static bool contains(const char *needle) {
    size_t len; char *text=bt_session_text(&window.session,false,&len);
    bool ok=text && strstr(text,needle); free(text); return ok;
}
static uint32_t ppm_pixel(const char *path, int x, int y) {
    FILE *file=fopen(path,"rb"); require(file!=NULL,"open preedit capture");
    char magic[3]={0}; int width=0,height=0,maximum=0;
    require(fscanf(file,"%2s %d %d %d%*c",magic,&width,&height,&maximum)==4 &&
            !strcmp(magic,"P6") && maximum==255 && x>=0 && x<width && y>=0 && y<height,
            "read preedit capture header");
    long pixels=ftell(file);
    require(pixels>=0 && !fseek(file,pixels+((long)y*width+x)*3,SEEK_SET),"seek preedit pixel");
    unsigned char rgb[3]; require(fread(rgb,1,3,file)==3 && !fclose(file),"read preedit pixel");
    return (uint32_t)rgb[0]<<16 | (uint32_t)rgb[1]<<8 | rgb[2];
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
static void completed_copy(int cw, char *result, size_t capacity) {
    SDL_Event event={.type=SDL_MOUSEBUTTONDOWN};
    event.button.button=SDL_BUTTON_LEFT; event.button.x=9; event.button.y=9;
    require(!bt_window_event(&window,&event),"completed pane selection start");
    event.type=SDL_MOUSEBUTTONUP; event.button.x=8+cw*2+2;
    require(!bt_window_event(&window,&event),"completed pane selection end");
    SDL_SetClipboardText("sentinel");
    push_key(SDL_SCANCODE_C,SDLK_c,KMOD_CTRL|KMOD_SHIFT,NULL);
    uint64_t deadline=bt_millis()+3000;
    do {
        require(!bt_window_pump(&window,5),"completed pane copy pump");
        char *text=SDL_GetClipboardText();
        snprintf(result,capacity,"%s",text?text:""); SDL_free(text);
    } while(!strcmp(result,"sentinel") && bt_millis()<deadline);
    require(strcmp(result,"sentinel")!=0,"completed pane copies selected text");
}
static void completed_autoscroll(int cw) {
    SDL_Event event={.type=SDL_MOUSEBUTTONDOWN};
    event.button.button=SDL_BUTTON_LEFT;
    event.button.x=8+cw*2+2; event.button.y=9;
    require(!bt_window_event(&window,&event),"begin selection at a scrollback viewport cell");
    event=(SDL_Event){.type=SDL_MOUSEMOTION};
    event.motion.x=9; event.motion.y=-20; event.motion.state=SDL_BUTTON_LMASK;
    require(!bt_window_event(&window,&event),"drag selection above the terminal viewport");
    for(int i=0;i<4;++i) {
        SDL_Delay(60);
        require(!bt_window_pump(&window,0),"tick selection autoscroll");
    }
    event=(SDL_Event){.type=SDL_MOUSEBUTTONUP};
    event.button.button=SDL_BUTTON_LEFT; event.button.x=9; event.button.y=-20;
    require(!bt_window_event(&window,&event),"finish selection after autoscroll");
    size_t length=0; char *text=bt_session_text(&window.session,true,&length);
    if(!text || !strstr(text,"024") || !strstr(text,"025"))
        fprintf(stderr,"selection after edge drag: %s\n",text?text:"(none)");
    require(text && strstr(text,"024") && strstr(text,"025"),
            "scrolling selection keeps its original cell and reaches earlier history");
    free(text);
}
static void completed_panes(const char *executable, const char *helper) {
    char service[PATH_MAX],root[PATH_MAX];
    const char *tmp=getenv("TMPDIR");
    if(!tmp || !*tmp) tmp="/tmp";
    require(snprintf(root,sizeof(root),"%s/bt-completed-XXXXXX",tmp)<(int)sizeof(root),
            "bounded completed session root");
    require(realpath("build/batty-state",service)!=NULL,"state service executable");
    require(mkdtemp(root)!=NULL,"completed session root");
    for(int persistent=0;persistent<2;++persistent) for(int alternate=0;alternate<2;++alternate) {
        char *args[]={(char *)executable,alternate?"--completed-alt":"--completed",NULL};
        int rc=persistent?bt_window_open_persistent(&window,service,helper,root,"completed",args,environ,40,6,"monospace",16,false):
            bt_window_open(&window,helper,args,environ,40,6,"monospace",16,false);
        require(!rc,"open completed pane fixture");
        uint64_t deadline=bt_millis()+5000;
        while((!window.session.eof || !window.session.done) && bt_millis()<deadline)
            require(!bt_window_pump(&window,5),"wait for completed pane");
        require(window.session.eof && window.session.done && window.session.exit_status==7,"completed fixture status");
        int cw,ch; require(!bt_renderer_metrics(window.renderer,&cw,&ch),"completed pane metrics");
        uint64_t written=window.session.bytes_written;
        char before[32],after[32]; completed_copy(cw,before,sizeof(before));
        require(alternate?!strcmp(before,"ALT"):!strcmp(before,"034"),"completed pane initial selection");
        SDL_Event wheel={.type=SDL_MOUSEWHEEL}; wheel.wheel.y=1;
        require(!bt_window_event(&window,&wheel),"completed pane wheel");
        completed_copy(cw,after,sizeof(after));
        require(alternate?!strcmp(after,"ALT"):!strcmp(after,"031"),"completed pane wheel scroll without mouse reports");
        push_key(SDL_SCANCODE_PAGEUP,SDLK_PAGEUP,KMOD_SHIFT,NULL);
        require(!bt_window_pump(&window,5),"completed pane page scroll");
        completed_copy(cw,after,sizeof(after));
        require(alternate?!strcmp(after,"ALT"):!strcmp(after,"025"),"completed pane page scroll");
        if(!alternate) completed_autoscroll(cw);
        push_key(SDL_SCANCODE_A,SDLK_a,KMOD_NONE,"a");
        push_key(SDL_SCANCODE_RETURN,SDLK_RETURN,KMOD_NONE,NULL);
        push_key(SDL_SCANCODE_V,SDLK_v,KMOD_CTRL|KMOD_SHIFT,NULL);
        require(!bt_window_paste(&window,"ignored",7),"completed pane direct paste ignored");
        require(!bt_window_pump(&window,5),"completed pane rejects process input");
        if(persistent) require(!bt_remote_input_flush(&window.session),"completed inspection acknowledged");
        require(window.session.bytes_written==written,"completed inspection sends no PTY bytes");
        window.force_draw=true;
        require(!bt_window_pump(&window,0),"render completed pane");
        bt_window_close(&window);
        if(persistent) {
            char error[256];
            require(!bt_remote_terminate(root,"completed",error,sizeof(error)),"terminate completed owner");
        }
    }
    require(!rmdir(root),"remove completed session root");
}
int main(int argc,char **argv) {
    if(argc==2 && !strcmp(argv[1],"--child")) return child();
    if(argc==2 && (!strcmp(argv[1],"--completed") || !strcmp(argv[1],"--completed-alt"))) {
        for(int i=0;i<40;++i) printf("%s%03d diagnostic",i?"\r\n":"",i);
        if(!strcmp(argv[1],"--completed-alt")) printf("\033[?1049h\033[HALT FAILURE");
        printf("\033[?1000h\033[?1006h\033[?1007h");
        return 7;
    }
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
    mouse.type=SDL_MOUSEBUTTONUP;
    require(SDL_PushEvent(&mouse)==1,"push mouse release");
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
    uint16_t cursor_x=0,cursor_y=0;
    require(ghostty_terminal_get(window.session.terminal,GHOSTTY_TERMINAL_DATA_CURSOR_X,&cursor_x)==GHOSTTY_SUCCESS &&
            ghostty_terminal_get(window.session.terminal,GHOSTTY_TERMINAL_DATA_CURSOR_Y,&cursor_y)==GHOSTTY_SUCCESS,
            "locate IME preedit cursor");
    int sample_x=8+cursor_x*cw+4,sample_y=8+cursor_y*ch+4;
    require(!bt_renderer_draw(window.renderer,&window.session,true,true),"draw focused preedit baseline");
    require(!bt_renderer_capture(window.renderer,"build/preedit-before.ppm"),"capture preedit baseline");
    uint32_t before=ppm_pixel("build/preedit-before.ppm",sample_x,sample_y);
    uint64_t written=window.session.bytes_written;
    SDL_Event editing={.type=SDL_TEXTEDITING}; editing.edit.windowID=window.window_id;
    snprintf(editing.edit.text,sizeof(editing.edit.text),"IM λ");
    editing.edit.start=3; editing.edit.length=1;
    require(!bt_window_event(&window,&editing),"accept local UTF-8 preedit");
    require(!bt_renderer_draw(window.renderer,&window.session,true,true),"draw local IME preedit");
    require(!bt_renderer_capture(window.renderer,"build/preedit-short.ppm"),"capture local IME preedit");
    require(ppm_pixel("build/preedit-short.ppm",sample_x,sample_y)!=before &&
            window.session.bytes_written==written && !contains("IM λ"),
            "IME preedit is visible but never sent to the PTY");
#if SDL_VERSION_ATLEAST(2,0,22)
    editing=(SDL_Event){.type=SDL_TEXTEDITING_EXT}; editing.editExt.windowID=window.window_id;
    editing.editExt.text=strdup("Extended composition text longer than SDL's inline event buffer λ");
    require(editing.editExt.text!=NULL,"allocate extended preedit fixture");
    editing.editExt.start=12; editing.editExt.length=3;
    require(!bt_window_event(&window,&editing),"accept extended IME preedit");
    free(editing.editExt.text);
    require(!bt_renderer_draw(window.renderer,&window.session,true,true),"draw retained extended IME preedit");
    require(!bt_renderer_capture(window.renderer,"build/preedit-extended.ppm"),"capture extended IME preedit");
    require(ppm_pixel("build/preedit-extended.ppm",sample_x,sample_y)!=before &&
            window.session.bytes_written==written,"extended IME event is copied and stays frontend-only");
#endif
    editing=(SDL_Event){.type=SDL_TEXTEDITING}; editing.edit.windowID=window.window_id;
    require(!bt_window_event(&window,&editing),"clear IME preedit");
    require(!bt_renderer_draw(window.renderer,&window.session,true,true),"redraw after IME cancellation");
    require(!bt_renderer_capture(window.renderer,"build/preedit-cleared.ppm"),"capture cancelled IME preedit");
    require(ppm_pixel("build/preedit-cleared.ppm",sample_x,sample_y)==before &&
            window.session.bytes_written==written,"cancelled IME preedit restores terminal pixels without PTY input");
    push_key(SDL_SCANCODE_Q,SDLK_q,KMOD_NONE,"q");
    uint64_t end=bt_millis()+3000;
    while(!window.session.done && bt_millis()<end) require(!bt_window_pump(&window,5),"wait for window child");
    require(window.session.done && window.session.exit_status==0,"window child status");
    bt_window_close(&window);
    completed_panes(executable,helper);
    puts("PASS completed pane inspection; GLES window, shaped text, keyboard, bracketed clipboard paste, reply relay, mouse, selection and resize");
    return 0;
}
