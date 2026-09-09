/* SPDX-License-Identifier: MIT */
#define _POSIX_C_SOURCE 200809L
#include "window.h"
#include "remote.h"
#include "presentation.h"
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define PAD 8
static unsigned desktop_windows;

static int fail(BtWindow *w, const char *message) {
    snprintf(w->error,sizeof(w->error),"%s",message);
    return -1;
}
static bool mode(BtWindow *w, GhosttyMode value) {
    if(!w->session.terminal) return false;
    GhosttyTerminalModeConfig config={.mode=value};
    return ghostty_terminal_get(w->session.terminal,GHOSTTY_TERMINAL_DATA_MODE,&config)==GHOSTTY_SUCCESS && config.value;
}
static GhosttyMods mods(SDL_Keymod m) {
    GhosttyMods out=0;
    if(m & KMOD_SHIFT) out|=GHOSTTY_MODS_SHIFT;
    if(m & KMOD_CTRL) out|=GHOSTTY_MODS_CTRL;
    if(m & KMOD_ALT) out|=GHOSTTY_MODS_ALT;
    if(m & KMOD_GUI) out|=GHOSTTY_MODS_SUPER;
    if(m & KMOD_CAPS) out|=GHOSTTY_MODS_CAPS_LOCK;
    if(m & KMOD_NUM) out|=GHOSTTY_MODS_NUM_LOCK;
    if(m & KMOD_RALT) out|=GHOSTTY_MODS_ALT_SIDE;
    return out;
}
static GhosttyKey key(SDL_Scancode sc) {
    if(sc>=SDL_SCANCODE_A && sc<=SDL_SCANCODE_Z) return GHOSTTY_KEY_A+(sc-SDL_SCANCODE_A);
    if(sc>=SDL_SCANCODE_1 && sc<=SDL_SCANCODE_9) return GHOSTTY_KEY_DIGIT_1+(sc-SDL_SCANCODE_1);
    if(sc>=SDL_SCANCODE_F1 && sc<=SDL_SCANCODE_F12) return GHOSTTY_KEY_F1+(sc-SDL_SCANCODE_F1);
    if(sc>=SDL_SCANCODE_F13 && sc<=SDL_SCANCODE_F24) return GHOSTTY_KEY_F13+(sc-SDL_SCANCODE_F13);
    if(sc>=SDL_SCANCODE_KP_1 && sc<=SDL_SCANCODE_KP_9) return GHOSTTY_KEY_NUMPAD_1+(sc-SDL_SCANCODE_KP_1);
#define K(s,g) case SDL_SCANCODE_##s: return GHOSTTY_KEY_##g
    switch(sc) {
        K(0,DIGIT_0); K(SPACE,SPACE); K(RETURN,ENTER); K(ESCAPE,ESCAPE);
        K(BACKSPACE,BACKSPACE); K(TAB,TAB); K(UP,ARROW_UP); K(DOWN,ARROW_DOWN);
        K(LEFT,ARROW_LEFT); K(RIGHT,ARROW_RIGHT); K(HOME,HOME); K(END,END);
        K(PAGEUP,PAGE_UP); K(PAGEDOWN,PAGE_DOWN); K(INSERT,INSERT); K(DELETE,DELETE);
        K(MINUS,MINUS); K(EQUALS,EQUAL); K(LEFTBRACKET,BRACKET_LEFT); K(RIGHTBRACKET,BRACKET_RIGHT);
        K(BACKSLASH,BACKSLASH); K(SEMICOLON,SEMICOLON); K(APOSTROPHE,QUOTE);
        K(GRAVE,BACKQUOTE); K(COMMA,COMMA); K(PERIOD,PERIOD); K(SLASH,SLASH);
        K(KP_0,NUMPAD_0); K(KP_PERIOD,NUMPAD_DECIMAL); K(KP_ENTER,NUMPAD_ENTER);
        K(KP_PLUS,NUMPAD_ADD); K(KP_MINUS,NUMPAD_SUBTRACT); K(KP_MULTIPLY,NUMPAD_MULTIPLY);
        K(KP_DIVIDE,NUMPAD_DIVIDE); K(KP_EQUALS,NUMPAD_EQUAL);
        K(LSHIFT,SHIFT_LEFT); K(RSHIFT,SHIFT_RIGHT); K(LCTRL,CONTROL_LEFT); K(RCTRL,CONTROL_RIGHT);
        K(LALT,ALT_LEFT); K(RALT,ALT_RIGHT); K(LGUI,META_LEFT); K(RGUI,META_RIGHT);
        K(CAPSLOCK,CAPS_LOCK); K(NUMLOCKCLEAR,NUM_LOCK); K(NONUSBACKSLASH,INTL_BACKSLASH);
        default: return GHOSTTY_KEY_UNIDENTIFIED;
    }
#undef K
}
static int encode_key(BtWindow *w, SDL_Keysym sym, GhosttyKeyAction action, const char *text, size_t len) {
    GhosttyMods consumed=0;
    if(len && sym.mod & KMOD_SHIFT) consumed|=GHOSTTY_MODS_SHIFT;
    if(len && sym.mod & KMOD_MODE) consumed|=GHOSTTY_MODS_ALT|GHOSTTY_MODS_CTRL;
    if(w->session.remote) {
        if(bt_remote_observer(&w->session)) return 0;
        BtIntent intent={.type=BT_INTENT_KEY,.action=action,.key=key(sym.scancode),
            .mods=mods(sym.mod),.consumed=consumed,
            .codepoint=sym.sym>=32 && sym.sym<0x110000?(uint32_t)sym.sym:0};
        return bt_remote_intent(&w->session,&intent,text,len)?fail(w,w->session.error):0;
    }
    ghostty_key_encoder_setopt_from_terminal(w->key_encoder,w->session.terminal);
    ghostty_key_event_set_action(w->key_event,action);
    ghostty_key_event_set_key(w->key_event,key(sym.scancode));
    ghostty_key_event_set_mods(w->key_event,mods(sym.mod));
    ghostty_key_event_set_consumed_mods(w->key_event,consumed);
    ghostty_key_event_set_composing(w->key_event,false);
    ghostty_key_event_set_unshifted_codepoint(w->key_event,
        sym.sym>=32 && sym.sym<0x110000 ? (uint32_t)sym.sym : 0);
    ghostty_key_event_set_utf8(w->key_event,text,len);
    char buffer[1024]; size_t written=0;
    if(ghostty_key_encoder_encode(w->key_encoder,w->key_event,buffer,sizeof(buffer),&written)!=GHOSTTY_SUCCESS)
        return fail(w,"Could not encode keyboard input");
    if(written) {
        GhosttyTerminalScrollViewport scroll={.tag=GHOSTTY_SCROLL_VIEWPORT_BOTTOM};
        ghostty_terminal_scroll_viewport(w->session.terminal,scroll);
        if(bt_session_send(&w->session,buffer,written)) return fail(w,w->session.error);
    }
    return 0;
}
int bt_window_paste(BtWindow *w, const char *data, size_t len) {
    if(len>4u*1024u*1024u) return fail(w,"Paste exceeds the 4 MiB limit");
    if(w->session.remote) {
        if(bt_remote_observer(&w->session)) return 0;
        BtIntent intent={.type=BT_INTENT_PASTE};
        return bt_remote_intent(&w->session,&intent,data,len)?fail(w,w->session.error):0;
    }
    char *copy=malloc(len+1), *encoded=malloc(len*2+64);
    if(!copy || !encoded) { free(copy); free(encoded); return fail(w,"Could not allocate paste"); }
    memcpy(copy,data,len); copy[len]=0;
    size_t written=0;
    GhosttyResult result=ghostty_paste_encode(copy,len,mode(w,GHOSTTY_MODE_BRACKETED_PASTE),encoded,len*2+64,&written);
    int rc=result==GHOSTTY_SUCCESS ? bt_session_send(&w->session,encoded,written) : -1;
    free(copy); free(encoded);
    if(rc) return fail(w,"Could not queue paste");
    GhosttyTerminalScrollViewport scroll={.tag=GHOSTTY_SCROLL_VIEWPORT_BOTTOM};
    ghostty_terminal_scroll_viewport(w->session.terminal,scroll);
    return 0;
}
static void mouse_position(BtWindow *w, int x, int y, float *px, float *py) {
    if(!w->window) { *px=(float)x; *py=(float)y; return; }
    int ww,wh,pw,ph;
    SDL_GetWindowSize(w->window,&ww,&wh); SDL_GL_GetDrawableSize(w->window,&pw,&ph);
    *px=(float)x*pw/(ww?ww:1); *py=(float)y*ph/(wh?wh:1);
}
static bool tracking(BtWindow *w) {
    if(!w->session.terminal) return false;
    GhosttyMouseTrackingMode value=GHOSTTY_MOUSE_TRACKING_NONE;
    ghostty_terminal_get(w->session.terminal,GHOSTTY_TERMINAL_DATA_MOUSE_TRACKING,&value);
    return value!=GHOSTTY_MOUSE_TRACKING_NONE;
}
static int remote_pointer(BtWindow *w, uint32_t type, GhosttyMouseAction action,
                          GhosttyMouseButton button, int x, int y,
                          uint32_t buttons, int delta) {
    float px,py;
    mouse_position(w,x,y,&px,&py);
    BtIntent intent={.type=type,.action=action,.button=button,.buttons=buttons,
        .mods=mods(SDL_GetModState()),.x=(int32_t)px-PAD,.y=(int32_t)py-PAD,.delta=delta};
    return bt_remote_intent(&w->session,&intent,NULL,0)?fail(w,w->session.error):0;
}
static int mouse_report(BtWindow *w, GhosttyMouseAction action, GhosttyMouseButton button, int x, int y, bool pressed) {
    ghostty_mouse_encoder_setopt_from_terminal(w->mouse_encoder,w->session.terminal);
    int width,height;
    SDL_GL_GetDrawableSize(w->window,&width,&height);
    GhosttyMouseEncoderSize size=GHOSTTY_INIT_SIZED(GhosttyMouseEncoderSize);
    size.screen_width=width; size.screen_height=height;
    size.cell_width=w->session.cell_width; size.cell_height=w->session.cell_height;
    size.padding_top=size.padding_bottom=size.padding_left=size.padding_right=PAD;
    ghostty_mouse_encoder_setopt(w->mouse_encoder,GHOSTTY_MOUSE_ENCODER_OPT_SIZE,&size);
    ghostty_mouse_encoder_setopt(w->mouse_encoder,GHOSTTY_MOUSE_ENCODER_OPT_ANY_BUTTON_PRESSED,&pressed);
    ghostty_mouse_event_set_action(w->mouse_event,action);
    ghostty_mouse_event_set_button(w->mouse_event,button);
    ghostty_mouse_event_set_mods(w->mouse_event,mods(SDL_GetModState()));
    GhosttyMousePosition position;
    mouse_position(w,x,y,&position.x,&position.y);
    ghostty_mouse_event_set_position(w->mouse_event,position);
    char buffer[128]; size_t written=0;
    if(ghostty_mouse_encoder_encode(w->mouse_encoder,w->mouse_event,buffer,sizeof(buffer),&written)!=GHOSTTY_SUCCESS)
        return fail(w,"Could not encode mouse input");
    return written ? bt_session_send(&w->session,buffer,written) : 0;
}
static void select_at(BtWindow *w, int x, int y, bool begin) {
    float px,py;
    mouse_position(w,x,y,&px,&py);
    int col=(int)(px-PAD)/w->session.cell_width, row=(int)(py-PAD)/w->session.cell_height;
    if(col<0) col=0;
    if(row<0) row=0;
    if(col>=w->session.cols) col=w->session.cols-1;
    if(row>=w->session.rows) row=w->session.rows-1;
    if(begin) { w->selection_x=col; w->selection_y=row; }
    GhosttySelection selection=GHOSTTY_INIT_SIZED(GhosttySelection);
    selection.start=(GhosttyGridRef)GHOSTTY_INIT_SIZED(GhosttyGridRef);
    selection.end=(GhosttyGridRef)GHOSTTY_INIT_SIZED(GhosttyGridRef);
    GhosttyPoint start={.tag=GHOSTTY_POINT_TAG_VIEWPORT,.value.coordinate={w->selection_x,w->selection_y}};
    GhosttyPoint end={.tag=GHOSTTY_POINT_TAG_VIEWPORT,.value.coordinate={col,row}};
    if(ghostty_terminal_grid_ref(w->session.terminal,start,&selection.start)==GHOSTTY_SUCCESS &&
       ghostty_terminal_grid_ref(w->session.terminal,end,&selection.end)==GHOSTTY_SUCCESS)
        ghostty_terminal_set(w->session.terminal,GHOSTTY_TERMINAL_OPT_SELECTION,&selection);
    w->force_draw=true;
}
int bt_window_resize(BtWindow *w) {
    int cw,ch,width,height;
    if(!w->window || !w->renderer) return 0;
    SDL_GL_GetDrawableSize(w->window,&width,&height);
    w->force_draw=true;
    if(w->session.remote && (bt_remote_observer(&w->session) ||
       (width==w->drawable_width && height==w->drawable_height))) return 0;
    w->drawable_width=width; w->drawable_height=height;
    if(bt_renderer_metrics(w->renderer,&cw,&ch)) return fail(w,bt_renderer_error(w->renderer));
    int cols=(width-2*PAD)/cw, rows=(height-2*PAD)/ch;
    if(cols<1) cols=1;
    if(rows<1) rows=1;
    if(cols>1000) cols=1000;
    if(rows>1000) rows=1000;
    if(bt_session_resize(&w->session,cols,rows,cw,ch)) return fail(w,w->session.error);
    w->force_draw=true;
    return 0;
}
int bt_window_event(BtWindow *w, const SDL_Event *event) {
    if(event->type==SDL_QUIT) { w->close_requested=true; return 0; }
    if(event->type==SDL_WINDOWEVENT) {
        switch(event->window.event) {
            case SDL_WINDOWEVENT_CLOSE: w->close_requested=true; break;
            case SDL_WINDOWEVENT_SIZE_CHANGED: case SDL_WINDOWEVENT_DISPLAY_CHANGED:
                return bt_window_resize(w);
            case SDL_WINDOWEVENT_EXPOSED: case SDL_WINDOWEVENT_RESTORED: w->force_draw=true; break;
            case SDL_WINDOWEVENT_FOCUS_GAINED: case SDL_WINDOWEVENT_FOCUS_LOST:
                w->force_draw=true;
                if(w->session.remote) {
                    if(bt_remote_observer(&w->session) || w->session.eof) break;
                    BtIntent intent={.type=BT_INTENT_FOCUS,
                        .focused=event->window.event==SDL_WINDOWEVENT_FOCUS_GAINED};
                    return bt_remote_intent(&w->session,&intent,NULL,0)?fail(w,w->session.error):0;
                }
                if(mode(w,GHOSTTY_MODE_FOCUS_EVENT) && !w->session.eof) {
                    char bytes[8]; size_t len=0;
                    GhosttyFocusEvent focus=event->window.event==SDL_WINDOWEVENT_FOCUS_GAINED?GHOSTTY_FOCUS_GAINED:GHOSTTY_FOCUS_LOST;
                    if(ghostty_focus_encode(focus,bytes,sizeof(bytes),&len)==GHOSTTY_SUCCESS)
                        return bt_session_send(&w->session,bytes,len);
                }
                break;
        }
        return 0;
    }
    if(w->session.eof) return 0;
    if(w->session.remote && bt_remote_observer(&w->session)) return 0;
    if(event->type==SDL_KEYDOWN || event->type==SDL_KEYUP) {
        SDL_Keysym sym=event->key.keysym;
        bool press=event->type==SDL_KEYDOWN;
        if((sym.mod & KMOD_CTRL) && (sym.mod & KMOD_SHIFT) && (sym.sym==SDLK_c || sym.sym==SDLK_v)) {
            if(!press || event->key.repeat) return 0;
            if(sym.sym==SDLK_c) {
                size_t len; char *text=bt_session_text(&w->session,true,&len);
                if(text) { SDL_SetClipboardText(text); free(text); }
                return 0;
            }
            char *text=SDL_GetClipboardText();
            int rc=text?bt_window_paste(w,text,strlen(text)):0;
            SDL_free(text); return rc;
        }
        if(press && (sym.mod & KMOD_SHIFT) && (sym.sym==SDLK_PAGEUP || sym.sym==SDLK_PAGEDOWN)) {
            if(w->session.remote) {
                BtIntent intent={.type=BT_INTENT_SCROLL,
                    .delta=(sym.sym==SDLK_PAGEUP?-1:1)*(int)w->session.rows};
                return bt_remote_intent(&w->session,&intent,NULL,0)?fail(w,w->session.error):0;
            }
            GhosttyTerminalScrollViewport scroll={.tag=GHOSTTY_SCROLL_VIEWPORT_DELTA,
                .value.delta=(sym.sym==SDLK_PAGEUP?-1:1)*(int)w->session.rows};
            ghostty_terminal_scroll_viewport(w->session.terminal,scroll); return 0;
        }
        bool text_key=sym.sym>=32 && sym.sym<0x110000;
        bool control=(sym.mod & (KMOD_CTRL|KMOD_GUI)) && !(sym.mod & KMOD_MODE);
        if(press && text_key && !control) {
            w->text_key=sym; w->text_repeat=event->key.repeat; w->text_pending=true;
            return 0; /* SDL_TEXTINPUT supplies the layout/IME-produced UTF-8. */
        }
        if(press) w->text_pending=false;
        char ascii=(sym.sym>=32 && sym.sym<127)?(char)sym.sym:0;
        return encode_key(w,sym,press?(event->key.repeat?GHOSTTY_KEY_ACTION_REPEAT:GHOSTTY_KEY_ACTION_PRESS):GHOSTTY_KEY_ACTION_RELEASE,
                          press && ascii?&ascii:NULL,press && ascii?1:0);
    }
    if(event->type==SDL_TEXTINPUT) {
        SDL_Keysym sym=w->text_pending?w->text_key:(SDL_Keysym){.scancode=SDL_SCANCODE_UNKNOWN};
        GhosttyKeyAction action=w->text_pending && w->text_repeat?GHOSTTY_KEY_ACTION_REPEAT:GHOSTTY_KEY_ACTION_PRESS;
        w->text_pending=false;
        return encode_key(w,sym,action,event->text.text,strlen(event->text.text));
    }
    if(event->type==SDL_MOUSEBUTTONDOWN || event->type==SDL_MOUSEBUTTONUP) {
        bool press=event->type==SDL_MOUSEBUTTONDOWN;
        if(w->session.remote) {
            GhosttyMouseButton button=event->button.button==SDL_BUTTON_LEFT?GHOSTTY_MOUSE_BUTTON_LEFT:
                event->button.button==SDL_BUTTON_RIGHT?GHOSTTY_MOUSE_BUTTON_RIGHT:
                event->button.button==SDL_BUTTON_MIDDLE?GHOSTTY_MOUSE_BUTTON_MIDDLE:GHOSTTY_MOUSE_BUTTON_UNKNOWN;
            return remote_pointer(w,BT_INTENT_POINTER,press?GHOSTTY_MOUSE_ACTION_PRESS:GHOSTTY_MOUSE_ACTION_RELEASE,
                button,event->button.x,event->button.y,press?SDL_BUTTON(event->button.button):0,0);
        }
        if(tracking(w) && !(SDL_GetModState() & KMOD_SHIFT)) {
            GhosttyMouseButton button=event->button.button==SDL_BUTTON_LEFT?GHOSTTY_MOUSE_BUTTON_LEFT:
                event->button.button==SDL_BUTTON_RIGHT?GHOSTTY_MOUSE_BUTTON_RIGHT:GHOSTTY_MOUSE_BUTTON_MIDDLE;
            return mouse_report(w,press?GHOSTTY_MOUSE_ACTION_PRESS:GHOSTTY_MOUSE_ACTION_RELEASE,button,event->button.x,event->button.y,press);
        }
        if(event->button.button==SDL_BUTTON_LEFT) {
            select_at(w,event->button.x,event->button.y,press); w->selecting=press;
        }
    } else if(event->type==SDL_MOUSEMOTION) {
        if(w->session.remote) {
            GhosttyMouseButton button=(event->motion.state & SDL_BUTTON_LMASK)?GHOSTTY_MOUSE_BUTTON_LEFT:
                (event->motion.state & SDL_BUTTON_RMASK)?GHOSTTY_MOUSE_BUTTON_RIGHT:
                (event->motion.state & SDL_BUTTON_MMASK)?GHOSTTY_MOUSE_BUTTON_MIDDLE:GHOSTTY_MOUSE_BUTTON_UNKNOWN;
            return remote_pointer(w,BT_INTENT_POINTER,GHOSTTY_MOUSE_ACTION_MOTION,button,
                event->motion.x,event->motion.y,event->motion.state,0);
        }
        if(w->selecting) select_at(w,event->motion.x,event->motion.y,false);
        else if(tracking(w)) {
            GhosttyMouseButton button=(event->motion.state & SDL_BUTTON_LMASK)?GHOSTTY_MOUSE_BUTTON_LEFT:
                (event->motion.state & SDL_BUTTON_RMASK)?GHOSTTY_MOUSE_BUTTON_RIGHT:
                (event->motion.state & SDL_BUTTON_MMASK)?GHOSTTY_MOUSE_BUTTON_MIDDLE:GHOSTTY_MOUSE_BUTTON_UNKNOWN;
            return mouse_report(w,GHOSTTY_MOUSE_ACTION_MOTION,button,event->motion.x,event->motion.y,event->motion.state!=0);
        }
    } else if(event->type==SDL_MOUSEWHEEL) {
        int delta=event->wheel.y;
        if(event->wheel.direction==SDL_MOUSEWHEEL_FLIPPED) delta=-delta;
        if(delta>20) delta=20;
        if(delta< -20) delta=-20;
        if(w->session.remote) {
            int x,y; uint32_t buttons=SDL_GetMouseState(&x,&y);
            return remote_pointer(w,BT_INTENT_WHEEL,GHOSTTY_MOUSE_ACTION_PRESS,GHOSTTY_MOUSE_BUTTON_UNKNOWN,
                                  x,y,buttons,delta);
        }
        if(tracking(w) && !(SDL_GetModState() & KMOD_SHIFT)) {
            int x,y; SDL_GetMouseState(&x,&y);
            for(int i=0;i<abs(delta);++i)
                if(mouse_report(w,GHOSTTY_MOUSE_ACTION_PRESS,delta>0?GHOSTTY_MOUSE_BUTTON_FOUR:GHOSTTY_MOUSE_BUTTON_FIVE,x,y,false)) return -1;
        } else if(mode(w,GHOSTTY_MODE_ALT_SCROLL) && (mode(w,GHOSTTY_MODE_ALT_SCREEN_SAVE)||mode(w,GHOSTTY_MODE_ALT_SCREEN))) {
            SDL_Keysym sym={.scancode=delta>0?SDL_SCANCODE_UP:SDL_SCANCODE_DOWN};
            for(int i=0;i<abs(delta)*3;++i) if(encode_key(w,sym,GHOSTTY_KEY_ACTION_PRESS,NULL,0)) return -1;
        } else {
            GhosttyTerminalScrollViewport scroll={.tag=GHOSTTY_SCROLL_VIEWPORT_DELTA,.value.delta=-delta*3};
            ghostty_terminal_scroll_viewport(w->session.terminal,scroll);
        }
    }
    return 0;
}
static void initialize_window(BtWindow *w) {
    memset(w,0,sizeof(*w)); w->owner=getpid();
    w->session.master=w->session.control=w->session.status=-1;
}
static void fit_surface(BtWindow *w, unsigned cols, unsigned rows, unsigned cw, unsigned ch) {
    if(!w->window) return;
    int ww,wh,pw,ph;
    SDL_GetWindowSize(w->window,&ww,&wh); SDL_GL_GetDrawableSize(w->window,&pw,&ph);
    if(pw>0 && ph>0) SDL_SetWindowSize(w->window,(cols*cw+2*PAD)*ww/pw,(rows*ch+2*PAD)*wh/ph);
    SDL_GL_GetDrawableSize(w->window,&w->drawable_width,&w->drawable_height);
    w->force_draw=true;
}
static int open_surface(BtWindow *w, unsigned cols, unsigned rows, const char *font,
                        int font_size, bool headless, int *cw, int *ch) {
    if(!headless && desktop_windows) return fail(w,"This prototype supports one desktop window per controller");
    *cw=8; *ch=16;
    if(headless) return 0;
    if(SDL_InitSubSystem(SDL_INIT_VIDEO)) return fail(w,SDL_GetError());
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_PROFILE_MASK,SDL_GL_CONTEXT_PROFILE_ES);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_MAJOR_VERSION,3); SDL_GL_SetAttribute(SDL_GL_CONTEXT_MINOR_VERSION,0);
    SDL_GL_SetAttribute(SDL_GL_DOUBLEBUFFER,1);
    w->window=SDL_CreateWindow("Batty",SDL_WINDOWPOS_CENTERED,SDL_WINDOWPOS_CENTERED,
        cols*font_size*3/5+2*PAD,rows*(font_size+4)+2*PAD,
        SDL_WINDOW_OPENGL|SDL_WINDOW_RESIZABLE|SDL_WINDOW_ALLOW_HIGHDPI);
    if(!w->window) { SDL_QuitSubSystem(SDL_INIT_VIDEO); return fail(w,SDL_GetError()); }
    ++desktop_windows;
    w->window_id=SDL_GetWindowID(w->window);
    w->renderer=bt_renderer_new(w->window,font,font_size,w->error,sizeof(w->error));
    if(!w->renderer) return -1;
    if(bt_renderer_metrics(w->renderer,cw,ch)) return fail(w,bt_renderer_error(w->renderer));
    fit_surface(w,cols,rows,(unsigned)*cw,(unsigned)*ch);
    SDL_SetWindowMinimumSize(w->window,120,80);
    SDL_StartTextInput();
    return 0;
}
int bt_window_open(BtWindow *w, const char *helper, char *const argv[], char *const env[],
                   unsigned cols, unsigned rows, const char *font, int font_size, bool headless) {
    initialize_window(w);
    int cw,ch;
    if(open_surface(w,cols,rows,font,font_size,headless,&cw,&ch)) return -1;
    if(bt_session_open(&w->session,helper,argv,env,cols,rows,cw,ch)) return fail(w,w->session.error);
    if(ghostty_key_encoder_new(NULL,&w->key_encoder)!=GHOSTTY_SUCCESS ||
       ghostty_key_event_new(NULL,&w->key_event)!=GHOSTTY_SUCCESS ||
       ghostty_mouse_encoder_new(NULL,&w->mouse_encoder)!=GHOSTTY_SUCCESS ||
       ghostty_mouse_event_new(NULL,&w->mouse_event)!=GHOSTTY_SUCCESS) return fail(w,"Could not allocate input encoders");
    w->force_draw=true;
    return 0;
}
int bt_window_attach(BtWindow *w, const char *root, const char *name,
                     const char *font, int font_size, bool headless, bool observe) {
    initialize_window(w);
    if(bt_remote_attach(&w->session,root,name,observe)) return fail(w,w->session.error);
    int cw,ch;
    if(open_surface(w,w->session.cols,w->session.rows,font,font_size,headless,&cw,&ch)) return -1;
    fit_surface(w,w->session.cols,w->session.rows,w->session.cell_width,w->session.cell_height);
    w->force_draw=true;
    return 0;
}
int bt_window_open_persistent(BtWindow *w, const char *service, const char *helper,
                              const char *root, const char *name,
                              char *const argv[], char *const env[],
                              unsigned cols, unsigned rows, const char *font,
                              int font_size, bool headless) {
    initialize_window(w);
    int cw,ch;
    if(open_surface(w,cols,rows,font,font_size,headless,&cw,&ch)) return -1;
    uint64_t epoch=0;
    int created=bt_remote_create_owned(service,helper,root,name,argv,env,cols,rows,
                                      (unsigned)cw,(unsigned)ch,w->error,sizeof(w->error),&epoch);
    if(created<0) return -1;
    if(bt_remote_attach(&w->session,root,name,false)) {
        int saved=errno;
        if(!created) {
            char cleanup_error[256];
            (void)bt_remote_cancel(root,name,epoch,cleanup_error,sizeof(cleanup_error));
        }
        errno=saved;
        return fail(w,w->session.error);
    }
    fit_surface(w,w->session.cols,w->session.rows,w->session.cell_width,w->session.cell_height);
    w->force_draw=true;
    return 0;
}

int bt_window_pump(BtWindow *w, int timeout_ms) {
    if(w->error[0]) return -1;
    if(w->window) {
        SDL_Event event;
        while(SDL_PollEvent(&event)) if(bt_window_event(w,&event)) return -1;
    }
    if(w->close_requested) return 0;
    if(bt_session_pump(&w->session,timeout_ms)) return fail(w,w->session.error);
    if(w->session.title_changed && w->window) {
        const char *title=bt_session_title(&w->session);
        char text[1024]; size_t len=title?strlen(title):0;
        if(len>=sizeof(text)) len=sizeof(text)-1;
        for(size_t i=0;i<len;++i) text[i]=(unsigned char)title[i]>=32?title[i]:' ';
        text[len]=0; SDL_SetWindowTitle(w->window,len?text:"Batty");
    }
    if(w->renderer) {
        bool focused=(SDL_GetWindowFlags(w->window)&SDL_WINDOW_INPUT_FOCUS)!=0;
        if(bt_renderer_draw(w->renderer,&w->session,w->force_draw,focused)) return fail(w,bt_renderer_error(w->renderer));
        w->force_draw=false;
        uint16_t x=0,y=0;
        if(w->session.presentation) {
            GhosttyRenderStateCursor cursor=w->session.presentation->cursor;
            if(cursor.viewport_has_value) { x=cursor.viewport_x; y=cursor.viewport_y; }
        } else if(w->session.terminal) {
            ghostty_terminal_get(w->session.terminal,GHOSTTY_TERMINAL_DATA_CURSOR_X,&x);
            ghostty_terminal_get(w->session.terminal,GHOSTTY_TERMINAL_DATA_CURSOR_Y,&y);
        }
        int ww,wh,pw,ph;
        SDL_GetWindowSize(w->window,&ww,&wh); SDL_GL_GetDrawableSize(w->window,&pw,&ph);
        if(pw<1 || ph<1) return 0;
        SDL_Rect ime={(PAD+x*w->session.cell_width)*ww/pw,(PAD+y*w->session.cell_height)*wh/ph,
            w->session.cell_width*ww/pw,w->session.cell_height*wh/ph};
        SDL_SetTextInputRect(&ime);
    }
    return 0;
}
void bt_window_close(BtWindow *w) {
    if(w->owner!=getpid()) return;
    bt_session_close(&w->session);
    if(w->key_event) ghostty_key_event_free(w->key_event);
    if(w->key_encoder) ghostty_key_encoder_free(w->key_encoder);
    if(w->mouse_event) ghostty_mouse_event_free(w->mouse_event);
    if(w->mouse_encoder) ghostty_mouse_encoder_free(w->mouse_encoder);
    bt_renderer_free(w->renderer);
    if(w->window) {
        SDL_StopTextInput(); SDL_DestroyWindow(w->window);
        if(desktop_windows) --desktop_windows;
        SDL_QuitSubSystem(SDL_INIT_VIDEO);
    }
    w->key_event=NULL; w->key_encoder=NULL; w->mouse_event=NULL; w->mouse_encoder=NULL;
    w->renderer=NULL; w->window=NULL;
}
