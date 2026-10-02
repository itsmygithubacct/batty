/* SPDX-License-Identifier: MIT */
/* Cursor shape, glyph contrast and presentation transport framebuffer checks. */
#define _GNU_SOURCE
#include "window.h"
#include "presentation.h"
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

extern char **environ;
static BtWindow window;
static unsigned cw,ch,width,height;
static size_t frame_bytes;
static unsigned char *pixels;
enum { PAD=8 };
static const char reset_sequence[]=
    "\033c\033]10;#ffffff\033\\\033]11;#000000\033\\\033]12;#ffffff\033\\"
    "\033[?25l\033[3;3H\033[2 q";

static void require(bool ok,const char *message) {
    if(ok) return;
    fprintf(stderr,"FAIL cursor: %s (%s; %s)\n",message,window.error,
            window.renderer?bt_renderer_error(window.renderer):"no renderer");
    free(pixels); bt_window_close(&window); exit(1);
}
static void feed(const char *text) { bt_session_feed(&window.session,text,strlen(text)); }
static void capture(BtSession *session,bool focused) {
    require(!bt_renderer_draw(window.renderer,session,true,focused),"draw cursor fixture");
    require(!bt_renderer_capture(window.renderer,"build/cursor-test.ppm"),"capture cursor fixture");
    FILE *file=fopen("build/cursor-test.ppm","rb"); require(file!=NULL,"open cursor capture");
    char magic[3]; unsigned maximum;
    require(fscanf(file,"%2s%u%u%u",magic,&width,&height,&maximum)==4 &&
            !strcmp(magic,"P6") && maximum==255 && fgetc(file)=='\n',"read cursor capture header");
    frame_bytes=(size_t)width*height*3;
    unsigned char *next=realloc(pixels,frame_bytes); require(next!=NULL,"allocate cursor pixels"); pixels=next;
    require(fread(pixels,1,frame_bytes,file)==frame_bytes && fgetc(file)==EOF && !fclose(file),
            "read cursor framebuffer");
}
static void pixel(unsigned x,unsigned y,unsigned r,unsigned g,unsigned b,const char *message) {
    require(x<width && y<height,"cursor sample inside framebuffer");
    const unsigned char *p=pixels+((size_t)y*width+x)*3;
    if(abs((int)p[0]-(int)r)>1 || abs((int)p[1]-(int)g)>1 || abs((int)p[2]-(int)b)>1) {
        fprintf(stderr,"pixel %u,%u: expected %u,%u,%u; got %u,%u,%u\n",x,y,r,g,b,p[0],p[1],p[2]);
        require(false,message);
    }
}
static void reset(void) {
    feed(reset_sequence);
}
static void test_shapes(void) {
    const struct { const char *mode; unsigned style; } shapes[]={
        {"\033[2 q",0},{"\033[4 q",1},{"\033[6 q",2}
    };
    unsigned x=PAD+2*cw,y=PAD+2*ch;
    for(unsigned i=0;i<sizeof(shapes)/sizeof(shapes[0]);++i) {
        reset(); feed(shapes[i].mode); feed("\033[?25h"); capture(&window.session,true);
        pixel(x+cw/2,y+ch/2,i==0?255:0,i==0?255:0,i==0?255:0,"focused cursor interior");
        pixel(x,y,i==1?0:255,i==1?0:255,i==1?0:255,"cursor top left");
        pixel(x+cw/2,y+ch-1,i==2?0:255,i==2?0:255,i==2?0:255,"cursor lower edge");
        pixel(x+cw,y+ch/2,0,0,0,"cursor stays in its cell");
        capture(&window.session,false);
        pixel(x+cw/2,y+ch/2,0,0,0,"unfocused cursor is hollow");
        pixel(x,y,255,255,255,"unfocused cursor outline");
        feed("\033[?25l"); capture(&window.session,true);
        pixel(x,y,0,0,0,"hidden cursor has no outline");
    }
    reset(); feed("\033]12;#ff0000\033\\\033[?25h"); capture(&window.session,true);
    pixel(x+cw/2,y+ch/2,255,0,0,"OSC 12 cursor color");
    reset(); feed("\033]12;#000000\033\\M\033[3;3H\033[?25h"); capture(&window.session,true);
    unsigned ink=0;
    for(unsigned yy=y;yy<y+ch;++yy) for(unsigned xx=x;xx<x+cw;++xx)
        if(pixels[((size_t)yy*width+xx)*3]>32) ++ink;
    require(ink>0,"cursor matching the background keeps its text readable");
    puts("PASS cursor block, underline, bar, focus, visibility and OSC 12 color");
}
static void test_text(const char *text,unsigned span,bool tail) {
    reset(); feed(text); feed(tail?"\033[3;4H":"\033[3;3H");
    capture(&window.session,true);
    unsigned char *original=malloc(frame_bytes); require(original!=NULL,"save glyph framebuffer");
    memcpy(original,pixels,frame_bytes);
    feed("\033[?25h"); capture(&window.session,true);
    unsigned x0=PAD+2*cw,y0=PAD+2*ch,ink=0;
    for(unsigned y=0;y<height;++y) for(unsigned x=0;x<width;++x) {
        bool inside=x>=x0 && x<x0+span*cw && y>=y0 && y<y0+ch;
        size_t at=((size_t)y*width+x)*3;
        for(unsigned channel=0;channel<3;++channel) {
            int expected=inside?255-original[at+channel]:original[at+channel];
            require(abs((int)pixels[at+channel]-expected)<=2,
                    "block inverts shaped glyphs only inside the cursor");
        }
        if(inside && original[at]>32) ++ink;
    }
    require(ink>0,"cursor fixture contains visible glyph strokes");
    free(original);
}
static void test_transport(void) {
    const char fixture[]="\xe7\x95\x8c\033[3;4H\033[?25h";
    reset(); feed(fixture);
    capture(&window.session,true);
    /* Each terminal has one presentation consumer, including in the fixture. */
    GhosttyTerminal terminal=NULL;
    require(ghostty_terminal_new(NULL,&terminal,window.session.cols,window.session.rows)==GHOSTTY_SUCCESS &&
            ghostty_terminal_resize(terminal,window.session.cols,window.session.rows,cw,ch)==GHOSTTY_SUCCESS,
            "create snapshot terminal");
    ghostty_terminal_vt_write(terminal,(const uint8_t *)reset_sequence,strlen(reset_sequence));
    ghostty_terminal_vt_write(terminal,(const uint8_t *)fixture,strlen(fixture));
    BtPresenter *presenter=bt_presenter_new(terminal);
    require(presenter!=NULL,"create cursor snapshot presenter");
    BtPresentation *frame=NULL,*decoded=NULL;
    require(!bt_presenter_capture(presenter,1,1,cw,ch,true,&frame),"capture wide cursor snapshot");
    bt_presenter_free(presenter);
    ghostty_terminal_free(terminal);
    uint8_t *wire=NULL; size_t length=0;
    require(!bt_presentation_pack(frame,&wire,&length) &&
            !bt_presentation_unpack(wire,length,&decoded),"round-trip cursor snapshot");
    free(wire); bt_presentation_free(frame);
    require(decoded->cursor.wide_tail,"snapshot preserves wide cursor tail");
    capture(&window.session,true);
    unsigned char *original=malloc(frame_bytes); require(original!=NULL,"save local cursor frame");
    memcpy(original,pixels,frame_bytes);
    BtSession remote={.presentation=decoded}; capture(&remote,true);
    for(size_t i=0,shown=0;i<frame_bytes && shown<8;++i) if(original[i]!=pixels[i]) {
        fprintf(stderr,"transport pixel %zu,%zu channel %zu: local %u remote %u\n",
                i/3%width,i/3/width,i%3,original[i],pixels[i]); ++shown;
    }
    require(!memcmp(original,pixels,frame_bytes),"transported cursor matches local framebuffer");
    decoded->cursor.visual_style=GHOSTTY_RENDER_STATE_CURSOR_VISUAL_STYLE_BLOCK_HOLLOW;
    capture(&remote,true);
    pixel(PAD+2*cw+cw/2,PAD+2*ch+1,0,0,0,"explicit hollow cursor retains its interior");
    pixel(PAD+4*cw-1,PAD+2*ch,255,255,255,"wide hollow cursor covers both cells");
    decoded->cursor.visual_style=GHOSTTY_RENDER_STATE_CURSOR_VISUAL_STYLE_UNDERLINE;
    capture(&remote,true);
    pixel(PAD+4*cw-2,PAD+3*ch-1,255,255,255,"wide underline cursor covers both cells");
    decoded->cursor.visual_style=GHOSTTY_RENDER_STATE_CURSOR_VISUAL_STYLE_BLOCK;
    decoded->cursor.blinking=true;
    /* Sample safely inside each blink phase, leaving time for the capture. */
    for(unsigned phase=0;phase<2;++phase) {
        uint64_t deadline=bt_millis()+1800;
        while((bt_millis()/600%2!=phase || bt_millis()%600>400) && bt_millis()<deadline) SDL_Delay(10);
        require(bt_millis()<deadline,"reach cursor blink phase");
        capture(&remote,true);
        pixel(PAD+2*cw+cw/2,PAD+2*ch+1,phase?0:255,phase?0:255,phase?0:255,
              "focused cursor alternates blink phases without new terminal output");
    }
    capture(&remote,false);
    pixel(PAD+2*cw,PAD+2*ch,255,255,255,"unfocused blinking cursor stays visible");
    decoded->cursor.viewport_has_value=false;
    capture(&remote,true);
    pixel(PAD+2*cw,PAD+2*ch,0,0,0,"cursor outside the viewport is omitted");
    capture(&window.session,true); /* Release the borrowed remote session before leaving. */
    free(original); bt_presentation_free(decoded);
    puts("PASS cursor wide head/tail and hollow shape survive presentation transport");
}
int main(void) {
    char helper[PATH_MAX]; require(realpath("build/batty-session",helper)!=NULL,"session helper");
    char *args[]={"/bin/cat",NULL};
    require(!bt_window_open(&window,helper,args,environ,24,8,"monospace",16,false),"open cursor test window");
    cw=window.session.cell_width; ch=window.session.cell_height;
    require(cw>=4 && ch>=8,"cursor font metrics");
    test_shapes();
    test_text("M next",1,false);
    test_text("ffi ->",1,false);
    test_text("e\xcc\x81",1,false);
    test_text("\033[1;3;4;9mM next\033[0m",1,false);
    test_text("\xe7\x95\x8c next",2,false);
    test_text("\xe7\x95\x8c next",2,true);
    puts("PASS cursor readable glyphs, shaping, combining marks, decorations and wide characters");
    test_transport();
    free(pixels); pixels=NULL; bt_window_close(&window); return 0;
}
