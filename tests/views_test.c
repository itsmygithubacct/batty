/* SPDX-License-Identifier: MIT */
/* Real PTYs and framebuffer isolation across shared views and OS windows. */
#define _GNU_SOURCE
#include "window.h"
#include "remote.h"
#include "presentation.h"
#include <GLES3/gl3.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <termios.h>
#include <unistd.h>

extern char **environ;
static BtSurface *surface;
static BtWindow views[5];
static void cleanup(void) {
    for(unsigned i=0;i<5;++i) bt_window_close(&views[i]);
    bt_surface_free(surface); surface=NULL;
}
static void require(bool ok, const char *message) {
    if(ok) return;
    fprintf(stderr,"FAIL views: %s\n",message);
    for(unsigned i=0;i<5;++i)
        if(views[i].error[0] || views[i].session.error[0])
            fprintf(stderr,"view %u: %s / %s\n",i,views[i].error,views[i].session.error);
    cleanup(); exit(1);
}
static int child(const char *name) {
    struct termios term;
    if(tcgetattr(0,&term)) return 30;
    cfmakeraw(&term);
    if(tcsetattr(0,TCSANOW,&term)) return 31;
    setvbuf(stdout,NULL,_IONBF,0);
    printf("\033]11;#%s\007",!strcmp(name,"LEFT")?"c8141e":"1428c8");
    printf("\033[48;2;%sm\033[2J\033[H\033[?25l%s_READY\r\n",
           !strcmp(name,"LEFT")?"200;20;30":"20;40;200",name);
    unsigned char bytes[512]; size_t used=0; unsigned clipboard_count=0;
    for(;;) {
        unsigned char c;
        if(read(0,&c,1)!=1) return 32;
        if(c=='q') return 0;
        if(c>=2 && c<=5) {
            if(c==5) usleep(100000);
            printf("\033]52;%s;%s\007CLIP:%u\r\n",c==4?"p":"c",c==3?"YQBi":"Y2Fmw6k=",++clipboard_count);
            continue;
        }
        if(c=='s') {
            struct winsize size;
            if(ioctl(0,TIOCGWINSZ,&size)) return 33;
            printf("SIZE:%u,%u\r\n",size.ws_col,size.ws_row);
        } else if(c=='g') {
            printf("\033[H\033_Ga=T,f=24,s=1,v=1,c=8,r=4,i=42,C=1,q=2;AP8A\033\\");
        } else if(c=='k') {
            printf("\033[>15uKBD_READY\r\n");
        } else if(c==1) {
            printf("\033[?1000h\033[?1006hMOUSE_READY\r\n");
        } else if(c=='!') {
            printf("RX:");
            for(size_t i=0;i<used;++i) printf("%02x",bytes[i]);
            printf("\r\n"); used=0;
        } else {
            if(used==sizeof(bytes)) return 34;
            bytes[used++]=c;
        }
    }
}
static bool contains(BtWindow *w, const char *needle) {
    size_t length=0; char *text=bt_session_text(&w->session,false,&length);
    bool found=text && strstr(text,needle); free(text); return found;
}
static void until(BtWindow *w, const char *needle) {
    uint64_t deadline=bt_millis()+5000;
    while(!contains(w,needle) && bt_millis()<deadline) require(!bt_window_pump(w,5),"pump view");
    require(contains(w,needle),needle);
}
static void send(BtWindow *w, const char *text) {
    require(!bt_session_send(&w->session,text,strlen(text)),"queue child input");
}
static void feed(BtWindow *w, const char *text) {
    bt_session_feed(&w->session,text,strlen(text));
}
static void key(BtWindow *w, char c, bool queued) {
    SDL_Event e={.type=SDL_KEYDOWN};
    e.key.windowID=w->window_id;
    e.key.keysym=(SDL_Keysym){.scancode=SDL_SCANCODE_A+(c-'a'),.sym=c};
    if(queued) require(SDL_PushEvent(&e)==1,"queue window key");
    else require(!bt_window_event(w,&e),"view key");
    e=(SDL_Event){.type=SDL_TEXTINPUT}; e.text.windowID=w->window_id;
    e.text.text[0]=c;
    if(queued) require(SDL_PushEvent(&e)==1,"queue window text");
    else require(!bt_window_event(w,&e),"view text");
}
static void pixel(int x, int y, unsigned red, unsigned green, unsigned blue) {
    int width,height; SDL_GL_GetDrawableSize(bt_surface_window(surface),&width,&height);
    unsigned char rgba[4]={0};
    glReadPixels(x,height-y-1,1,1,GL_RGBA,GL_UNSIGNED_BYTE,rgba);
    if(abs((int)rgba[0]-(int)red)>2 || abs((int)rgba[1]-(int)green)>2 || abs((int)rgba[2]-(int)blue)>2)
        fprintf(stderr,"pixel %d,%d: %u,%u,%u expected %u,%u,%u\n",x,y,rgba[0],rgba[1],rgba[2],red,green,blue);
    require(abs((int)rgba[0]-(int)red)<=2 && abs((int)rgba[1]-(int)green)<=2 &&
            abs((int)rgba[2]-(int)blue)<=2,"view framebuffer color and clipping");
}
static void draw(void) {
    require(!bt_surface_clear(surface,9,11,13),"clear host surface");
    require(!bt_window_draw_view(&views[0],true),"draw first terminal view");
    require(!bt_window_draw_view(&views[1],false),"draw second terminal view");
}
static void clipboard(const char *expected) {
    char *text=SDL_GetClipboardText();
    require(text && !strcmp(text,expected),"clipboard contents"); SDL_free(text);
}
int main(int argc, char **argv) {
    if(argc==3 && !strcmp(argv[1],"--child")) return child(argv[2]);
    char executable[PATH_MAX],helper[PATH_MAX],service[PATH_MAX],error[256];
    require(realpath(argv[0],executable)!=NULL,"test executable");
    require(realpath("build/batty-session",helper)!=NULL,"session helper");
    require(realpath("build/batty-state",service)!=NULL,"state service");
    char *left[]={executable,"--child","LEFT",NULL};
    char *right[]={executable,"--child","RIGHT",NULL};
    surface=bt_surface_new("Batty views",800,500,error,sizeof(error));
    require(surface!=NULL,"create host surface");
    SDL_Rect a={12,24,360,420}, b={400,24,360,420};
    require(!bt_window_open_view(&views[0],surface,a,helper,left,environ,"monospace",16),"open first view");
    require(!bt_window_open_view(&views[1],surface,b,helper,right,environ,"monospace",16),"open second view");
    until(&views[0],"LEFT_READY"); until(&views[1],"RIGHT_READY");
    draw(); pixel(100,250,200,20,30); pixel(500,250,20,40,200); pixel(390,250,9,11,13);

    key(&views[0],'l',false); send(&views[0],"!"); until(&views[0],"RX:6c");
    key(&views[1],'r',false); send(&views[1],"!"); until(&views[1],"RX:72");
    require(!contains(&views[0],"RX:72") && !contains(&views[1],"RX:6c"),"independent view input");
    feed(&views[1],"\033[?1000h\033[?1006h");
    int ww,wh,pw,ph,cw,ch;
    SDL_GetWindowSize(bt_surface_window(surface),&ww,&wh);
    SDL_GL_GetDrawableSize(bt_surface_window(surface),&pw,&ph);
    require(!bt_renderer_metrics(views[1].renderer,&cw,&ch),"view cell metrics");
    SDL_Event mouse={.type=SDL_MOUSEBUTTONDOWN};
    mouse.button.windowID=views[1].window_id; mouse.button.button=SDL_BUTTON_LEFT;
    mouse.button.x=(b.x+8+2*cw+2)*ww/pw; mouse.button.y=(b.y+8+ch+2)*wh/ph;
    require(!bt_window_event(&views[1],&mouse),"translate mouse to second view");
    send(&views[1],"!"); until(&views[1],"RX:1b5b3c303b333b324d");
    SDL_SetModState(KMOD_SHIFT);
    require(!bt_window_focus(&views[1],false),"focus loss releases captured mouse despite Shift");
    require(!bt_window_focus(&views[1],false),"repeated pointer cleanup is idempotent");
    mouse.type=SDL_MOUSEBUTTONUP;
    require(!bt_window_event(&views[1],&mouse),"late physical mouse release is suppressed");
    SDL_SetModState(KMOD_NONE);
    send(&views[1],"!"); until(&views[1],"RX:1b5b3c303b333b326d");
    mouse.type=SDL_MOUSEBUTTONDOWN; mouse.button.button=SDL_BUTTON_RIGHT;
    require(!bt_window_event(&views[1],&mouse),"application right-button press");
    send(&views[1],"!"); until(&views[1],"RX:1b5b3c323b333b324d");
    SDL_SetModState(KMOD_SHIFT); mouse.type=SDL_MOUSEBUTTONUP;
    require(!bt_window_event(&views[1],&mouse),"Shift cannot divert application button release into selection");
    SDL_SetModState(KMOD_NONE);
    send(&views[1],"!"); until(&views[1],"RX:1b5b3c323b333b326d");
    feed(&views[0],"\033[?1004h\033[?2004h");
    require(!bt_window_focus(&views[0],true) && !bt_window_focus(&views[0],false),"view focus reports");
    send(&views[0],"!"); until(&views[0],"RX:1b5b491b5b4f");
    require(!bt_window_paste(&views[0],"l",1),"view bracketed paste");
    send(&views[0],"!"); until(&views[0],"RX:1b5b3230307e6c1b5b3230317e");

    require(!bt_window_release_keys(&views[0]),"clear legacy held keys");
    feed(&views[0],"\033[?1004l\033[>15u");
    key(&views[0],'a',false);
    require(!bt_window_focus(&views[0],false),"focus loss releases application key");
    require(!bt_window_focus(&views[0],false),"repeated focus loss is idempotent");
    SDL_Event release={.type=SDL_KEYUP};
    release.key.keysym=(SDL_Keysym){.scancode=SDL_SCANCODE_A,.sym=SDLK_a};
    require(!bt_window_event(&views[0],&release),"late physical release is suppressed");
    require(!bt_window_event(&views[1],&release),"release cannot leak into another pane");
    send(&views[0],"!"); until(&views[0],"RX:1b5b3937751b5b39373b313a3375");
    feed(&views[0],"\033[<u");

    views[0].clipboard_write=false;
    require(!bt_window_focus(&views[0],true),"focused view can disable clipboard writes");
    SDL_SetClipboardText("sentinel");
    send(&views[0],"\2"); until(&views[0],"CLIP:1"); clipboard("sentinel");
    views[0].clipboard_write=true;
    require(!bt_window_focus(&views[0],true),"enable focused clipboard writes");
    send(&views[0],"\2"); until(&views[0],"CLIP:2"); clipboard("caf\303\251");
    SDL_SetClipboardText("new value");
    require(!bt_window_pump(&views[0],0),"clipboard consumed once"); clipboard("new value");
    feed(&views[0],"\033]52;c;wK8=\007");
    require(!bt_window_pump(&views[0],0),"reject invalid clipboard UTF-8"); clipboard("new value");
    size_t queued=views[0].session.pending_end-views[0].session.pending_start;
    feed(&views[0],"\033]52;c;?\007");
    require(views[0].session.pending_end-views[0].session.pending_start==queued,"clipboard read cannot send system contents to the child");
    size_t encoded=((BT_CLIPBOARD_LIMIT+3)/3)*4;
    char *oversized=malloc(encoded+9);
    require(oversized!=NULL,"allocate clipboard limit fixture");
    memcpy(oversized,"\033]52;c;",7);
    for(size_t i=0;i<encoded;i+=4) memcpy(oversized+7+i,"QUFB",4);
    oversized[7+encoded]=7; oversized[8+encoded]=0;
    feed(&views[0],oversized); free(oversized);
    require(views[0].session.clipboard==NULL,"oversized clipboard value rejected atomically");
    feed(&views[0],"\033]2;clipboard-limit-ok\007");
    require(!strcmp(bt_session_title(&views[0].session),"clipboard-limit-ok"),"parser recovers after oversized clipboard write");
    send(&views[0],"\3"); until(&views[0],"CLIP:3"); clipboard("new value");
    send(&views[0],"\4"); until(&views[0],"CLIP:4"); clipboard("new value");
    require(!bt_window_focus(&views[0],false),"revoke clipboard permission on focus loss");
    send(&views[0],"\2"); until(&views[0],"CLIP:5"); clipboard("new value");

    /* An image wider/taller than its pane cannot paint over the next pane or
     * the host chrome. The second view is deliberately drawn first. */
    feed(&views[0],"\033[H\033_Ga=T,f=24,s=1,v=1,c=200,r=200,i=41,C=1,q=2;/wAA\033\\");
    require(!bt_surface_clear(surface,9,11,13),"clear for image clipping");
    require(!bt_window_draw_view(&views[1],false) && !bt_window_draw_view(&views[0],true),"draw clipped image");
    pixel(100,250,255,0,0); pixel(500,250,20,40,200); pixel(390,250,9,11,13);
    feed(&views[0],"\033_Ga=d,d=A,q=2\033\\");
    a=(SDL_Rect){12,60,280,260};
    require(!bt_window_set_region(&views[0],a),"resize and move first view");
    send(&views[0],"s");
    char expected[64]; snprintf(expected,sizeof(expected),"SIZE:%u,%u",views[0].session.cols,views[0].session.rows);
    until(&views[0],expected);
    draw(); pixel(100,250,200,20,30); pixel(330,250,9,11,13);
    require(!bt_surface_capture(surface,"build/views-test.ppm"),"capture shared terminal surface");
    require(!bt_surface_present(surface),"present complete surface");

    /* Pumping one window must route, rather than steal, another's events. */
    require(!bt_window_open(&views[2],helper,right,environ,40,16,"monospace",16,false),"open first independent window");
    require(!bt_window_open(&views[3],helper,left,environ,40,16,"monospace",16,false),"open second independent window");
    until(&views[2],"RIGHT_READY"); until(&views[3],"LEFT_READY");
    key(&views[3],'z',true);
    require(!bt_window_pump(&views[2],0),"dispatch other window input");
    send(&views[3],"!"); until(&views[3],"RX:7a");
    require(!contains(&views[2],"RX:7a"),"window identity routes input");
    bt_window_close(&views[2]);
    key(&views[3],'x',true); require(!bt_window_pump(&views[3],0),"surviving window input");
    send(&views[3],"!"); until(&views[3],"RX:78");
    draw(); pixel(500,250,20,40,200);

    const char *root=getenv("BATTY_TEST_SESSION_DIR");
    require(root!=NULL,"private persistent test root");
    require(!bt_remote_create(service,helper,root,"view",right,environ,40,16,10,20,error,sizeof(error)),"create persistent view session");
    SDL_Rect saved={400,24,360,420};
    require(!bt_window_attach_view(&views[4],surface,saved,root,"view","monospace",16,false),"attach persistent view");
    until(&views[4],"RIGHT_READY");
    send(&views[4],"g");
    uint64_t deadline=bt_millis()+3000;
    do {
        require(!bt_window_pump(&views[4],5),"pump persistent image");
        require(!bt_window_draw_view(&views[4],true),"draw persistent image");
    } while(!views[4].session.presentation->image_count && bt_millis()<deadline);
    require(views[4].session.presentation->image_count==1,"persistent image reached the state owner");
    send(&views[4],"k"); until(&views[4],"KBD_READY");
    key(&views[4],'a',false);
    bt_window_close(&views[4]);
    require(!bt_window_attach_view(&views[4],surface,saved,root,"view","monospace",16,false),"reattach persistent view");
    require(!bt_window_draw_view(&views[4],true),"draw recovered image");
    pixel(saved.x+20,saved.y+20,0,255,0);
    send(&views[4],"!"); until(&views[4],"RX:1b5b3937751b5b39373b313a3375");
    send(&views[4],"\1"); until(&views[4],"MOUSE_READY");
    mouse.type=SDL_MOUSEBUTTONDOWN; mouse.button.button=SDL_BUTTON_LEFT;
    require(!bt_window_event(&views[4],&mouse),"persistent pointer press");
    send(&views[4],"!"); until(&views[4],"RX:1b5b3c303b333b324d");
    bt_window_close(&views[4]);
    require(!bt_window_attach_view(&views[4],surface,saved,root,"view","monospace",16,false),"reattach after captured pointer detach");
    send(&views[4],"!"); until(&views[4],"RX:1b5b3c303b333b326d");
    require(!bt_window_attach_view(&views[2],surface,saved,root,"view","monospace",16,true),"clipboard observer");
    views[2].clipboard_write=true;
    require(!bt_window_focus(&views[2],true),"observer cannot enable clipboard writes");
    SDL_SetClipboardText("persistent sentinel");
    send(&views[4],"\2"); until(&views[2],"CLIP:1"); clipboard("persistent sentinel");
    views[4].clipboard_write=true;
    require(!bt_window_focus(&views[4],true),"controller clipboard policy");
    send(&views[4],"\2"); until(&views[2],"CLIP:2"); clipboard("persistent sentinel");
    /* Explicit blocking pump establishes freshness before clipboard assertions. */
    require(!bt_window_pump(&views[4],1),"controller consumes pending clipboard write"); clipboard("caf\303\251");
    SDL_SetClipboardText("after copy");
    require(!bt_window_pump(&views[4],1) && !bt_window_pump(&views[2],1),"no clipboard replay"); clipboard("after copy");
    send(&views[4],"\2"); until(&views[2],"CLIP:3");
    bt_window_close(&views[4]);
    require(!bt_window_attach_view(&views[4],surface,saved,root,"view","monospace",16,false),"reattach discards old clipboard request");
    views[4].clipboard_write=true;
    require(!bt_window_focus(&views[4],true) && !bt_window_pump(&views[4],1),"new controller clipboard policy"); clipboard("after copy");
    send(&views[4],"\5"); bt_window_close(&views[4]);
    until(&views[2],"CLIP:4"); clipboard("after copy");
    require(!bt_window_attach_view(&views[4],surface,saved,root,"view","monospace",16,false),"reattach after detached clipboard output");
    views[4].clipboard_write=true;
    require(!bt_window_focus(&views[4],true) && !bt_window_pump(&views[4],1),"detached output cannot replay clipboard"); clipboard("after copy");
    bt_window_close(&views[2]);
    bt_window_close(&views[4]);
    require(!bt_remote_terminate(root,"view",error,sizeof(error)),"terminate persistent view session");
    cleanup();
    puts("PASS shared surfaces, isolated text/images, translated input, independent OS windows and persistent view reattachment");
    return 0;
}
