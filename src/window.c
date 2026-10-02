/* SPDX-License-Identifier: MIT */
#define _POSIX_C_SOURCE 200809L
#include "window.h"
#include "remote.h"
#include "presentation.h"
#include <ghostty/vt/unicode.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define PAD 8

static int fail(BtWindow *w, const char *message) {
    snprintf(w->error,sizeof(w->error),"%s",message);
    return -1;
}
static void cancel_copy(BtWindow *w) {
    if(w->copy_ticket) bt_remote_text_cancel(&w->session,w->copy_ticket);
    w->copy_ticket=0;
}
static void finish_copy(BtWindow *w) {
    if(!w->copy_ticket) return;
    char *text=NULL; size_t length=0;
    int rc=bt_remote_text_take(&w->session,w->copy_ticket,&text,&length);
    if(rc==1) return;
    w->copy_ticket=0;
    if(!rc && length && w->focused) (void)SDL_SetClipboardText(text);
    free(text);
}
static bool mode(BtWindow *w, GhosttyMode value) {
    if(w->session.eof || !w->session.terminal) return false;
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
    if(m & KMOD_RSHIFT) out|=GHOSTTY_MODS_SHIFT_SIDE;
    if(m & KMOD_RCTRL) out|=GHOSTTY_MODS_CTRL_SIDE;
    if(m & KMOD_RALT) out|=GHOSTTY_MODS_ALT_SIDE;
    if(m & KMOD_RGUI) out|=GHOSTTY_MODS_SUPER_SIDE;
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
        K(KP_0,NUMPAD_0); K(KP_PERIOD,NUMPAD_DECIMAL); K(KP_DECIMAL,NUMPAD_DECIMAL); K(KP_ENTER,NUMPAD_ENTER);
        K(KP_PLUS,NUMPAD_ADD); K(KP_MINUS,NUMPAD_SUBTRACT); K(KP_MULTIPLY,NUMPAD_MULTIPLY);
        K(KP_DIVIDE,NUMPAD_DIVIDE); K(KP_EQUALS,NUMPAD_EQUAL);
        K(LSHIFT,SHIFT_LEFT); K(RSHIFT,SHIFT_RIGHT); K(LCTRL,CONTROL_LEFT); K(RCTRL,CONTROL_RIGHT);
        K(LALT,ALT_LEFT); K(RALT,ALT_RIGHT); K(LGUI,META_LEFT); K(RGUI,META_RIGHT);
        K(CAPSLOCK,CAPS_LOCK); K(NUMLOCKCLEAR,NUM_LOCK); K(NONUSBACKSLASH,INTL_BACKSLASH);
        K(INTERNATIONAL1,INTL_RO); K(INTERNATIONAL3,INTL_YEN);
        K(INTERNATIONAL2,KANA_MODE); K(INTERNATIONAL4,CONVERT); K(INTERNATIONAL5,NON_CONVERT);
        K(APPLICATION,CONTEXT_MENU); K(MENU,CONTEXT_MENU); K(HELP,HELP);
        K(PRINTSCREEN,PRINT_SCREEN); K(SCROLLLOCK,SCROLL_LOCK); K(PAUSE,PAUSE);
        K(KP_COMMA,NUMPAD_SEPARATOR); K(KP_EQUALSAS400,NUMPAD_EQUAL);
        K(KP_BACKSPACE,NUMPAD_BACKSPACE); K(KP_CLEAR,NUMPAD_CLEAR);
        K(KP_CLEARENTRY,NUMPAD_CLEAR_ENTRY);
        K(KP_LEFTPAREN,NUMPAD_PAREN_LEFT); K(KP_RIGHTPAREN,NUMPAD_PAREN_RIGHT);
        K(KP_MEMSTORE,NUMPAD_MEMORY_STORE); K(KP_MEMRECALL,NUMPAD_MEMORY_RECALL);
        K(KP_MEMCLEAR,NUMPAD_MEMORY_CLEAR); K(KP_MEMADD,NUMPAD_MEMORY_ADD);
        K(KP_MEMSUBTRACT,NUMPAD_MEMORY_SUBTRACT);
        K(CUT,CUT); K(COPY,COPY); K(PASTE,PASTE);
        K(AUDIONEXT,MEDIA_TRACK_NEXT); K(AUDIOPREV,MEDIA_TRACK_PREVIOUS);
        K(AUDIOSTOP,MEDIA_STOP); K(AUDIOPLAY,MEDIA_PLAY_PAUSE);
        K(AUDIOMUTE,AUDIO_VOLUME_MUTE); K(MUTE,AUDIO_VOLUME_MUTE);
        K(VOLUMEUP,AUDIO_VOLUME_UP); K(VOLUMEDOWN,AUDIO_VOLUME_DOWN);
        K(MEDIASELECT,MEDIA_SELECT); K(MAIL,LAUNCH_MAIL);
        K(CALCULATOR,LAUNCH_APP_1); K(APP1,LAUNCH_APP_1); K(APP2,LAUNCH_APP_2);
        K(AC_BACK,BROWSER_BACK); K(AC_FORWARD,BROWSER_FORWARD);
        K(AC_HOME,BROWSER_HOME); K(AC_SEARCH,BROWSER_SEARCH);
        K(AC_STOP,BROWSER_STOP); K(AC_REFRESH,BROWSER_REFRESH);
        K(AC_BOOKMARKS,BROWSER_FAVORITES);
        K(EJECT,EJECT); K(POWER,POWER); K(SLEEP,SLEEP);
        default: return GHOSTTY_KEY_UNIDENTIFIED;
    }
#undef K
}
static bool keypad_text(SDL_Keysym sym) {
    SDL_Scancode sc=sym.scancode;
    if((sc>=SDL_SCANCODE_KP_1 && sc<=SDL_SCANCODE_KP_9) || sc==SDL_SCANCODE_KP_0 ||
       sc==SDL_SCANCODE_KP_PERIOD || sc==SDL_SCANCODE_KP_DECIMAL) return (sym.mod&KMOD_NUM)!=0;
    return sc==SDL_SCANCODE_KP_PLUS || sc==SDL_SCANCODE_KP_MINUS || sc==SDL_SCANCODE_KP_MULTIPLY ||
           sc==SDL_SCANCODE_KP_DIVIDE || sc==SDL_SCANCODE_KP_EQUALS || sc==SDL_SCANCODE_KP_EQUALSAS400 ||
           sc==SDL_SCANCODE_KP_COMMA || sc==SDL_SCANCODE_KP_LEFTPAREN || sc==SDL_SCANCODE_KP_RIGHTPAREN;
}
static bool text_matches_key(SDL_Keysym key, const char *text) {
    const unsigned char *p=(const unsigned char *)text;
    size_t length=strlen(text);
    if(!length) return true;
    uint32_t points[SDL_TEXTINPUTEVENT_TEXT_SIZE]; size_t count=0;
    for(size_t i=0;i<length;) {
        unsigned codepoint=p[i++],more=0,minimum=0;
        if(codepoint<128) {}
        else if(codepoint>=0xc2 && codepoint<=0xdf) { codepoint&=31; more=1; minimum=0x80; }
        else if(codepoint>=0xe0 && codepoint<=0xef) { codepoint&=15; more=2; minimum=0x800; }
        else if(codepoint>=0xf0 && codepoint<=0xf4) { codepoint&=7; more=3; minimum=0x10000; }
        else return false;
        if(more>length-i || count==SDL_TEXTINPUTEVENT_TEXT_SIZE) return false;
        while(more--) { if((p[i]&0xc0)!=0x80) return false; codepoint=codepoint<<6|(p[i++]&63); }
        if(codepoint<minimum || codepoint>0x10ffff || (codepoint>=0xd800 && codepoint<=0xdfff)) return false;
        points[count++]=codepoint;
    }
    /* A layout can produce one grapheme with several codepoints. Preserve
     * its associated text; multi-grapheme IME commits remain direct text. */
    if(count>1 && ghostty_unicode_grapheme_width(points,count,NULL)!=count) return false;
    if(key.sym<32 || key.sym>=SDLK_SCANCODE_MASK ||
       (key.mod&(KMOD_SHIFT|KMOD_CAPS|KMOD_MODE|KMOD_CTRL|KMOD_ALT|KMOD_GUI))) return true;
    return points[0]==(uint32_t)key.sym;
}
static int encode_key_raw(BtWindow *w, SDL_Keysym sym, GhosttyKeyAction action, const char *text, size_t len) {
    GhosttyMods consumed=0;
    GhosttyMods encoded_mods=mods(sym.mod);
    if(len && sym.mod & KMOD_SHIFT) consumed|=GHOSTTY_MODS_SHIFT;
    if(len && sym.mod & KMOD_MODE) consumed|=GHOSTTY_MODS_ALT|GHOSTTY_MODS_CTRL;
    /* AltGr translates text; its synthetic Ctrl/Alt bits must not turn the
     * result into a shortcut or suppress Kitty's associated-text field.
     * Apply the same modifier identity to the corresponding key release. */
    if((sym.mod&KMOD_MODE) && ((sym.sym>=32 && sym.sym<0x110000) || keypad_text(sym)))
        encoded_mods&=~(GHOSTTY_MODS_ALT|GHOSTTY_MODS_CTRL|GHOSTTY_MODS_ALT_SIDE|GHOSTTY_MODS_CTRL_SIDE);
    if(w->session.remote) {
        if(bt_remote_observer(&w->session)) return 0;
        BtIntent intent={.type=BT_INTENT_KEY,.action=action,.key=key(sym.scancode),
            .mods=encoded_mods,.consumed=consumed,
            .codepoint=sym.sym>=32 && sym.sym<0x110000?(uint32_t)sym.sym:0,
            .composing=w->composing};
        return bt_remote_intent(&w->session,&intent,text,len)?fail(w,w->session.error):0;
    }
    ghostty_key_encoder_setopt_from_terminal(w->key_encoder,w->session.terminal);
    ghostty_key_event_set_action(w->key_event,action);
    ghostty_key_event_set_key(w->key_event,key(sym.scancode));
    ghostty_key_event_set_mods(w->key_event,encoded_mods);
    ghostty_key_event_set_consumed_mods(w->key_event,consumed);
    ghostty_key_event_set_composing(w->key_event,w->composing);
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
static int encode_key(BtWindow *w, SDL_Keysym sym, GhosttyKeyAction action, const char *text, size_t len) {
    bool physical=sym.scancode>SDL_SCANCODE_UNKNOWN && sym.scancode<SDL_NUM_SCANCODES;
    if(physical && action==GHOSTTY_KEY_ACTION_RELEASE && !w->held_keys[sym.scancode].scancode) return 0;
    int rc=encode_key_raw(w,sym,action,text,len);
    if(!rc && physical) w->held_keys[sym.scancode]=action==GHOSTTY_KEY_ACTION_RELEASE?(SDL_Keysym){0}:sym;
    return rc;
}
static int flush_text_key(BtWindow *w) {
    if(!w->text_pending) return 0;
    SDL_Keysym sym=w->text_key;
    GhosttyKeyAction action=w->text_repeat?GHOSTTY_KEY_ACTION_REPEAT:GHOSTTY_KEY_ACTION_PRESS;
    w->text_pending=false;
    if(w->session.eof || w->composing) return 0;
    /* Some physical keys have no SDL_TEXTINPUT (for example Alt+Space).
     * Keep their press/repeat before later keys and release events. The
     * authoritative encoder chooses the legacy or extended representation. */
    return encode_key(w,sym,action,NULL,0);
}
int bt_window_release_keys(BtWindow *w) {
    w->text_pending=false;
    w->composing=false;
    if(w->renderer) bt_renderer_preedit(w->renderer,NULL,0,0);
    w->force_draw=true;
    for(unsigned i=1;i<SDL_NUM_SCANCODES;++i) {
        SDL_Keysym sym=w->held_keys[i];
        w->held_keys[i]=(SDL_Keysym){0};
        if(sym.scancode && !w->session.eof && !bt_remote_disconnected(&w->session) &&
           encode_key_raw(w,sym,GHOSTTY_KEY_ACTION_RELEASE,NULL,0)) return -1;
    }
    return 0;
}
static int preedit(BtWindow *w, const char *text, int start, int length) {
    bool active=!w->session.eof && text && *text;
    if(active && !w->composing && bt_window_release_keys(w)) return -1;
    w->composing=active;
    if(w->renderer) bt_renderer_preedit(w->renderer,active?text:NULL,start,length);
    w->force_draw=true;
    return 0;
}
int bt_window_paste(BtWindow *w, const char *data, size_t len) {
    if(w->session.eof) return 0;
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
    if(w->embedded) { *px-=w->region.x; *py-=w->region.y; }
}
static bool tracking(BtWindow *w) {
    if(w->session.eof || !w->session.terminal) return false;
    GhosttyMouseTrackingMode value=GHOSTTY_MOUSE_TRACKING_NONE;
    ghostty_terminal_get(w->session.terminal,GHOSTTY_TERMINAL_DATA_MOUSE_TRACKING,&value);
    return value!=GHOSTTY_MOUSE_TRACKING_NONE;
}
static int remote_pointer(BtWindow *w, uint32_t type, GhosttyMouseAction action,
                          GhosttyMouseButton button, int x, int y,
                          uint32_t buttons, int delta, GhosttyMods modifiers) {
    float px,py;
    mouse_position(w,x,y,&px,&py);
    BtIntent intent={.type=type,.action=action,.button=button,.buttons=buttons,
        .mods=modifiers,.x=(int32_t)px-PAD,.y=(int32_t)py-PAD,.delta=delta};
    return bt_remote_intent(&w->session,&intent,NULL,0)?fail(w,w->session.error):0;
}
static int mouse_report(BtWindow *w, GhosttyMouseAction action, GhosttyMouseButton button, int x, int y, bool pressed, GhosttyMods modifiers) {
    ghostty_mouse_encoder_setopt_from_terminal(w->mouse_encoder,w->session.terminal);
    int width,height;
    SDL_GL_GetDrawableSize(w->window,&width,&height);
    if(w->embedded) { width=w->region.w; height=w->region.h; }
    GhosttyMouseEncoderSize size=GHOSTTY_INIT_SIZED(GhosttyMouseEncoderSize);
    size.screen_width=width; size.screen_height=height;
    size.cell_width=w->session.cell_width; size.cell_height=w->session.cell_height;
    size.padding_top=size.padding_bottom=size.padding_left=size.padding_right=PAD;
    ghostty_mouse_encoder_setopt(w->mouse_encoder,GHOSTTY_MOUSE_ENCODER_OPT_SIZE,&size);
    ghostty_mouse_encoder_setopt(w->mouse_encoder,GHOSTTY_MOUSE_ENCODER_OPT_ANY_BUTTON_PRESSED,&pressed);
    ghostty_mouse_event_set_action(w->mouse_event,action);
    ghostty_mouse_event_set_button(w->mouse_event,button);
    ghostty_mouse_event_set_mods(w->mouse_event,modifiers);
    GhosttyMousePosition position;
    mouse_position(w,x,y,&position.x,&position.y);
    ghostty_mouse_event_set_position(w->mouse_event,position);
    char buffer[128]; size_t written=0;
    if(ghostty_mouse_encoder_encode(w->mouse_encoder,w->mouse_event,buffer,sizeof(buffer),&written)!=GHOSTTY_SUCCESS)
        return fail(w,"Could not encode mouse input");
    return written ? bt_session_send(&w->session,buffer,written) : 0;
}
static GhosttyMouseButton mouse_button(unsigned button) {
    switch(button) {
        case SDL_BUTTON_LEFT: return GHOSTTY_MOUSE_BUTTON_LEFT;
        case SDL_BUTTON_MIDDLE: return GHOSTTY_MOUSE_BUTTON_MIDDLE;
        case SDL_BUTTON_RIGHT: return GHOSTTY_MOUSE_BUTTON_RIGHT;
        default: return GHOSTTY_MOUSE_BUTTON_UNKNOWN;
    }
}
int bt_window_release_pointer(BtWindow *w) {
    uint32_t held=w->held_buttons;
    w->held_buttons=0; w->selecting=false;
    bt_selection_drag_reset(&w->selection);
    if(w->session.eof || bt_remote_disconnected(&w->session)) return 0;
    for(unsigned i=SDL_BUTTON_LEFT;i<=SDL_BUTTON_RIGHT;++i) if(held&SDL_BUTTON(i)) {
        int rc=w->session.remote?
            remote_pointer(w,BT_INTENT_POINTER,GHOSTTY_MOUSE_ACTION_RELEASE,mouse_button(i),w->pointer_x,w->pointer_y,0,0,0):
            mouse_report(w,GHOSTTY_MOUSE_ACTION_RELEASE,mouse_button(i),w->pointer_x,w->pointer_y,false,0);
        if(rc) return rc;
    }
    return 0;
}
static int select_at(BtWindow *w, int x, int y, bool begin, bool end) {
    float px,py;
    mouse_position(w,x,y,&px,&py);
    int result=bt_selection_drag_event(&w->selection,w->session.terminal,w->session.cols,w->session.rows,
                                        w->session.cell_width,w->session.cell_height,PAD,
                                        (int)px,(int)py,begin,end,bt_millis());
    if(result<0) return fail(w,"Could not update text selection");
    w->force_draw=true;
    return 0;
}
int bt_window_resize(BtWindow *w) {
    int cw,ch,width,height;
    if(!w->window || !w->renderer) return 0;
    if(w->embedded) return bt_window_set_region(w,w->region);
    SDL_GL_GetDrawableSize(w->window,&width,&height);
    w->force_draw=true;
    if(w->session.remote && (bt_remote_disconnected(&w->session) || bt_remote_observer(&w->session) ||
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
                return w->embedded?0:bt_window_resize(w);
            case SDL_WINDOWEVENT_EXPOSED: case SDL_WINDOWEVENT_RESTORED: w->force_draw=true; break;
            case SDL_WINDOWEVENT_FOCUS_GAINED: case SDL_WINDOWEVENT_FOCUS_LOST:
                w->force_draw=true;
                w->focused=event->window.event==SDL_WINDOWEVENT_FOCUS_GAINED;
                if(!w->focused) cancel_copy(w);
                if(bt_remote_disconnected(&w->session)) {
                    (void)bt_window_release_keys(w); (void)bt_window_release_pointer(w);
                    break;
                }
                if(bt_session_clipboard_policy(&w->session,w->clipboard_write && w->focused &&
                    !bt_remote_observer(&w->session))) return fail(w,w->session.error);
                if(event->window.event==SDL_WINDOWEVENT_FOCUS_LOST) {
                    w->text_pending=false; w->selecting=false;
                    if(bt_window_release_keys(w)) return -1;
                    if(bt_window_release_pointer(w)) return -1;
                }
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
    if(bt_remote_disconnected(&w->session)) return 0;
    if(w->session.remote && bt_remote_observer(&w->session)) return 0;
    if(event->type==SDL_KEYDOWN || event->type==SDL_KEYUP) {
        if(flush_text_key(w)) return -1;
        SDL_Keysym sym=event->key.keysym;
        bool press=event->type==SDL_KEYDOWN;
        if(!(sym.mod & KMOD_MODE) && (sym.mod & KMOD_CTRL) && (sym.mod & KMOD_SHIFT) && (sym.sym==SDLK_c || sym.sym==SDLK_v)) {
            if(!press || event->key.repeat) return 0;
            if(sym.sym==SDLK_c) {
                if(w->session.remote) {
                    if(!w->copy_ticket)
                        (void)bt_remote_text_start(&w->session,true,BT_CLIPBOARD_LIMIT,&w->copy_ticket);
                    return 0;
                }
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
        if(w->session.eof) { w->text_pending=false; return 0; }
        bool text_key=(sym.sym>=32 && sym.sym<0x110000) || keypad_text(sym);
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
        if(w->renderer) bt_renderer_preedit(w->renderer,NULL,0,0);
        w->force_draw=true;
        bool composed=w->composing || (w->text_pending && !text_matches_key(w->text_key,event->text.text));
        w->composing=false;
        if(w->session.eof) { w->text_pending=false; return 0; }
        SDL_Keysym sym=w->text_pending && !composed?w->text_key:(SDL_Keysym){.scancode=SDL_SCANCODE_UNKNOWN};
        GhosttyKeyAction action=w->text_pending && !composed && w->text_repeat?GHOSTTY_KEY_ACTION_REPEAT:GHOSTTY_KEY_ACTION_PRESS;
        w->text_pending=false;
        return encode_key(w,sym,action,event->text.text,strlen(event->text.text));
    }
    if(event->type==SDL_TEXTEDITING) {
        return preedit(w,event->edit.text,event->edit.start,event->edit.length);
    }
#if SDL_VERSION_ATLEAST(2,0,22)
    if(event->type==SDL_TEXTEDITING_EXT) {
        return preedit(w,event->editExt.text,event->editExt.start,event->editExt.length);
    }
#endif
    if(event->type==SDL_MOUSEBUTTONDOWN || event->type==SDL_MOUSEBUTTONUP) {
        bool press=event->type==SDL_MOUSEBUTTONDOWN;
        GhosttyMouseButton button=mouse_button(event->button.button);
        if(button==GHOSTTY_MOUSE_BUTTON_UNKNOWN) return 0;
        uint32_t bit=SDL_BUTTON(event->button.button);
        w->pointer_x=event->button.x; w->pointer_y=event->button.y;
        if(w->session.remote || (!w->session.eof && (press?tracking(w) && !(SDL_GetModState() & KMOD_SHIFT):(w->held_buttons&bit)!=0))) {
            if(!press && !(w->held_buttons&bit)) return 0;
            uint32_t buttons=press?w->held_buttons|bit:w->held_buttons&~bit;
            GhosttyMods modifiers=mods(SDL_GetModState());
            /* A release belongs to the application that received the press,
             * even if Shift was pressed in the meantime. */
            if(!press) modifiers&=~GHOSTTY_MODS_SHIFT;
            int rc=w->session.remote?
                remote_pointer(w,BT_INTENT_POINTER,press?GHOSTTY_MOUSE_ACTION_PRESS:GHOSTTY_MOUSE_ACTION_RELEASE,
                    button,event->button.x,event->button.y,buttons,0,modifiers):
                mouse_report(w,press?GHOSTTY_MOUSE_ACTION_PRESS:GHOSTTY_MOUSE_ACTION_RELEASE,
                    button,event->button.x,event->button.y,buttons!=0,modifiers);
            if(!rc) w->held_buttons=buttons;
            return rc;
        }
        if(event->button.button==SDL_BUTTON_LEFT && (press || w->selecting)) {
            if(select_at(w,event->button.x,event->button.y,press,!press)) return -1;
            w->selecting=press && w->selection.active;
        }
    } else if(event->type==SDL_MOUSEMOTION) {
        w->pointer_x=event->motion.x; w->pointer_y=event->motion.y;
        uint32_t buttons=event->motion.state & w->held_buttons;
        GhosttyMouseButton button=(buttons & SDL_BUTTON_LMASK)?GHOSTTY_MOUSE_BUTTON_LEFT:
            (buttons & SDL_BUTTON_RMASK)?GHOSTTY_MOUSE_BUTTON_RIGHT:
            (buttons & SDL_BUTTON_MMASK)?GHOSTTY_MOUSE_BUTTON_MIDDLE:GHOSTTY_MOUSE_BUTTON_UNKNOWN;
        if(w->session.remote)
            return remote_pointer(w,BT_INTENT_POINTER,GHOSTTY_MOUSE_ACTION_MOTION,button,
                event->motion.x,event->motion.y,buttons,0,mods(SDL_GetModState()));
        if(w->selecting) {
            if(select_at(w,event->motion.x,event->motion.y,false,false)) return -1;
            w->selecting=w->selection.active;
        }
        else if(tracking(w))
            return mouse_report(w,GHOSTTY_MOUSE_ACTION_MOTION,button,event->motion.x,event->motion.y,
                                buttons!=0,mods(SDL_GetModState()));
    } else if(event->type==SDL_MOUSEWHEEL) {
        int delta=event->wheel.y;
        if(event->wheel.direction==SDL_MOUSEWHEEL_FLIPPED) delta=-delta;
        if(delta>20) delta=20;
        if(delta< -20) delta=-20;
        if(w->session.remote) {
            int x,y; uint32_t buttons=SDL_GetMouseState(&x,&y);
            return remote_pointer(w,BT_INTENT_WHEEL,GHOSTTY_MOUSE_ACTION_PRESS,GHOSTTY_MOUSE_BUTTON_UNKNOWN,
                                  x,y,buttons,delta,mods(SDL_GetModState()));
        }
        if(tracking(w) && !(SDL_GetModState() & KMOD_SHIFT)) {
            int x,y; SDL_GetMouseState(&x,&y);
            for(int i=0;i<abs(delta);++i)
                if(mouse_report(w,GHOSTTY_MOUSE_ACTION_PRESS,delta>0?GHOSTTY_MOUSE_BUTTON_FOUR:GHOSTTY_MOUSE_BUTTON_FIVE,x,y,false,mods(SDL_GetModState()))) return -1;
        } else if(!w->session.eof && mode(w,GHOSTTY_MODE_ALT_SCROLL) && (mode(w,GHOSTTY_MODE_ALT_SCREEN_SAVE)||mode(w,GHOSTTY_MODE_ALT_SCREEN))) {
            SDL_Keysym sym={.scancode=delta>0?SDL_SCANCODE_UP:SDL_SCANCODE_DOWN};
            /* Scroll-generated arrows have no physical key lifecycle. They
             * must not create held keys or replace an actual arrow's mods. */
            for(int i=0;i<abs(delta)*3;++i) if(encode_key_raw(w,sym,GHOSTTY_KEY_ACTION_PRESS,NULL,0)) return -1;
        } else {
            GhosttyTerminalScrollViewport scroll={.tag=GHOSTTY_SCROLL_VIEWPORT_DELTA,.value.delta=-delta*3};
            ghostty_terminal_scroll_viewport(w->session.terminal,scroll);
        }
    }
    return 0;
}
static void initialize_window(BtWindow *w) {
    memset(w,0,sizeof(*w)); w->owner=getpid();
    const char *clipboard=getenv("BATTY_CLIPBOARD");
    w->clipboard_write=clipboard && !strcmp(clipboard,"write");
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
static void surface_event(void *userdata, const SDL_Event *event) {
    BtWindow *w=userdata;
    if(bt_window_event(w,event) && !w->error[0])
        fail(w,w->session.error[0]?w->session.error:"Could not handle window input");
}
static int open_surface(BtWindow *w, unsigned cols, unsigned rows, const char *font,
                        int font_size, bool headless, int *cw, int *ch) {
    *cw=8; *ch=16;
    if(headless) return 0;
    w->surface=bt_surface_new("Batty",cols*font_size*3/5+2*PAD,rows*(font_size+4)+2*PAD,
                              w->error,sizeof(w->error));
    if(!w->surface) return -1;
    w->window=bt_surface_window(w->surface);
    w->window_id=SDL_GetWindowID(w->window);
    bt_surface_handler(w->surface,surface_event,w);
    w->renderer=bt_renderer_new_shared(w->window,bt_surface_context(w->surface),font,font_size,w->error,sizeof(w->error));
    if(!w->renderer) return -1;
    if(bt_renderer_metrics(w->renderer,cw,ch)) return fail(w,bt_renderer_error(w->renderer));
    fit_surface(w,cols,rows,(unsigned)*cw,(unsigned)*ch);
    return 0;
}
static int input_encoders(BtWindow *w) {
    if(ghostty_key_encoder_new(NULL,&w->key_encoder)!=GHOSTTY_SUCCESS ||
       ghostty_key_event_new(NULL,&w->key_event)!=GHOSTTY_SUCCESS ||
       ghostty_mouse_encoder_new(NULL,&w->mouse_encoder)!=GHOSTTY_SUCCESS ||
       ghostty_mouse_event_new(NULL,&w->mouse_event)!=GHOSTTY_SUCCESS)
        return fail(w,"Could not allocate input encoders");
    return 0;
}
int bt_window_open(BtWindow *w, const char *helper, char *const argv[], char *const env[],
                   unsigned cols, unsigned rows, const char *font, int font_size, bool headless) {
    initialize_window(w);
    int cw,ch;
    if(open_surface(w,cols,rows,font,font_size,headless,&cw,&ch)) return -1;
    if(bt_session_open(&w->session,helper,argv,env,cols,rows,cw,ch)) return fail(w,w->session.error);
    if(input_encoders(w)) return -1;
    w->force_draw=true;
    return 0;
}
int bt_window_attach(BtWindow *w, const char *root, const char *name,
                     const char *font, int font_size, bool headless, bool observe) {
    initialize_window(w);
    if(bt_remote_attach(&w->session,root,name,observe)) return fail(w,w->session.error);
    int cw,ch;
    if(open_surface(w,w->session.cols,w->session.rows,font,font_size,headless,&cw,&ch)) return -1;
    if(!bt_remote_observer(&w->session) && bt_remote_input_queue(&w->session,true)) return fail(w,w->session.error);
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
    if(!bt_remote_observer(&w->session) && bt_remote_input_queue(&w->session,true)) return fail(w,w->session.error);
    fit_surface(w,w->session.cols,w->session.rows,w->session.cell_width,w->session.cell_height);
    w->force_draw=true;
    return 0;
}

static int view_surface(BtWindow *w, BtSurface *surface, SDL_Rect region, const char *font, int font_size) {
    initialize_window(w);
    w->embedded=true; w->surface=surface; w->region=region;
    w->window=bt_surface_window(surface); w->window_id=SDL_GetWindowID(w->window);
    w->renderer=bt_renderer_new_shared(w->window,bt_surface_context(surface),font,font_size,w->error,sizeof(w->error));
    return w->renderer?0:-1;
}
static int view_geometry(BtWindow *w, SDL_Rect region, unsigned *cols, unsigned *rows, int *cw, int *ch) {
    int width,height; SDL_GL_GetDrawableSize(w->window,&width,&height);
    if(region.x<0 || region.y<0 || region.w<1 || region.h<1 ||
       region.w>width || region.h>height || region.x>width-region.w || region.y>height-region.h)
        return fail(w,"Terminal view is outside its window surface");
    if(bt_renderer_metrics(w->renderer,cw,ch)) return fail(w,bt_renderer_error(w->renderer));
    int c=(region.w-2*PAD)/ *cw, r=(region.h-2*PAD)/ *ch;
    *cols=c<1?1:c>1000?1000:(unsigned)c;
    *rows=r<1?1:r>1000?1000:(unsigned)r;
    return 0;
}
int bt_window_open_view(BtWindow *w, BtSurface *surface, SDL_Rect region, const char *helper,
                        char *const argv[], char *const env[], const char *font, int font_size) {
    if(view_surface(w,surface,region,font,font_size)) return -1;
    unsigned cols,rows; int cw,ch;
    if(view_geometry(w,region,&cols,&rows,&cw,&ch)) return -1;
    if(bt_session_open(&w->session,helper,argv,env,cols,rows,cw,ch)) return fail(w,w->session.error);
    if(input_encoders(w)) return -1;
    w->force_draw=true; return 0;
}
int bt_window_attach_view(BtWindow *w, BtSurface *surface, SDL_Rect region, const char *root,
                          const char *name, const char *font, int font_size, bool observe) {
    return bt_window_attach_view_epoch(w,surface,region,root,name,font,font_size,observe,0);
}
int bt_window_attach_view_epoch(BtWindow *w, BtSurface *surface, SDL_Rect region, const char *root,
                                const char *name, const char *font, int font_size, bool observe, uint64_t epoch) {
    if(view_surface(w,surface,region,font,font_size)) return -1;
    if(bt_remote_attach_epoch(&w->session,root,name,observe,epoch)) return fail(w,w->session.error);
    if(!bt_remote_observer(&w->session) && bt_remote_input_queue(&w->session,true)) return fail(w,w->session.error);
    return bt_window_set_region(w,region);
}
int bt_window_persistent_view(BtWindow *w, BtSurface *surface, SDL_Rect region, const char *service,
                              const char *helper, const char *root, const char *name,
                              char *const argv[], char *const env[], const char *font, int font_size) {
    if(view_surface(w,surface,region,font,font_size)) return -1;
    unsigned cols,rows; int cw,ch;
    if(view_geometry(w,region,&cols,&rows,&cw,&ch)) return -1;
    uint64_t epoch=0;
    int created=bt_remote_create_owned(service,helper,root,name,argv,env,cols,rows,cw,ch,
                                       w->error,sizeof(w->error),&epoch);
    if(created<0) return -1;
    if(bt_remote_attach(&w->session,root,name,false)) {
        if(!created) {
            char ignored[256];
            (void)bt_remote_cancel(root,name,epoch,ignored,sizeof(ignored));
        }
        return fail(w,w->session.error);
    }
    if(!bt_remote_observer(&w->session) && bt_remote_input_queue(&w->session,true)) return fail(w,w->session.error);
    return bt_window_set_region(w,region);
}
int bt_window_set_region(BtWindow *w, SDL_Rect region) {
    if(!w->embedded || !w->renderer) return fail(w,"A view region requires an embedded terminal");
    unsigned cols,rows; int cw,ch;
    if(view_geometry(w,region,&cols,&rows,&cw,&ch)) return -1;
    if(!bt_remote_observer(&w->session) && !bt_remote_disconnected(&w->session) &&
       (w->session.remote || cols!=w->session.cols || rows!=w->session.rows || cw!=w->session.cell_width || ch!=w->session.cell_height) &&
       bt_session_resize(&w->session,cols,rows,cw,ch) && !bt_remote_disconnected(&w->session)) return fail(w,w->session.error);
    w->region=region; w->force_draw=true;
    return 0;
}
static void ime_position(BtWindow *w) {
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
    if(pw<1 || ph<1) return;
    int ox=w->embedded?w->region.x:0, oy=w->embedded?w->region.y:0;
    SDL_Rect ime={(ox+PAD+x*w->session.cell_width)*ww/pw,(oy+PAD+y*w->session.cell_height)*wh/ph,
        w->session.cell_width*ww/pw,w->session.cell_height*wh/ph};
    SDL_SetTextInputRect(&ime);
}
int bt_window_draw_view(BtWindow *w, bool focused) {
    if(!w->embedded || !w->renderer) return fail(w,"Drawing a view requires an embedded terminal");
    if(bt_renderer_draw_region(w->renderer,&w->session,w->region,focused))
        return fail(w,bt_renderer_error(w->renderer));
    w->force_draw=false;
    if(focused) ime_position(w);
    return 0;
}
int bt_window_focus(BtWindow *w, bool focused) {
    SDL_Event e={.type=SDL_WINDOWEVENT};
    e.window.windowID=w->window_id;
    e.window.event=focused?SDL_WINDOWEVENT_FOCUS_GAINED:SDL_WINDOWEVENT_FOCUS_LOST;
    return bt_window_event(w,&e);
}

int bt_window_pump(BtWindow *w, int timeout_ms) {
    if(w->error[0]) return -1;
    if(w->window && !w->embedded) bt_surface_poll();
    if(w->error[0]) return -1;
    if(w->close_requested) return 0;
    /* SDL queues ordinary text with its key event. Once that batch is
     * drained, dispatch an Alt key with no text promptly rather than waiting
     * for its release. AltGr and active composition continue waiting for text. */
    if(w->text_pending && (w->text_key.mod&KMOD_ALT) && !(w->text_key.mod&KMOD_MODE) &&
       (!w->window || !SDL_HasEvent(SDL_TEXTINPUT)) && flush_text_key(w)) return -1;
    if(!w->embedded && w->window) w->focused=(SDL_GetWindowFlags(w->window)&SDL_WINDOW_INPUT_FOCUS)!=0;
    if(bt_session_clipboard_policy(&w->session,w->clipboard_write && w->focused &&
        !bt_remote_observer(&w->session))) return fail(w,w->session.error);
    if(bt_session_pump(&w->session,timeout_ms)) { cancel_copy(w); return fail(w,w->session.error); }
    if(!w->session.remote && w->selection.active) {
        int changed=bt_selection_drag_tick(&w->selection,w->session.terminal,
            w->session.cols,w->session.rows,w->session.cell_width,w->session.cell_height,PAD,bt_millis());
        if(changed<0) return fail(w,"Could not scroll text selection");
        if(changed) w->force_draw=true;
        w->selecting=w->selection.active;
    }
    if(w->session.clipboard) {
        (void)SDL_SetClipboardText(w->session.clipboard);
        bt_session_clipboard_clear(&w->session);
    }
    finish_copy(w);
    if(w->session.title_changed && w->window && !w->embedded) {
        const char *title=bt_session_title(&w->session);
        char text[1024]; size_t len=title?strlen(title):0;
        if(len>=sizeof(text)) len=sizeof(text)-1;
        for(size_t i=0;i<len;++i) text[i]=(unsigned char)title[i]>=32?title[i]:' ';
        text[len]=0; SDL_SetWindowTitle(w->window,len?text:"Batty");
    }
    if(w->renderer && !w->embedded) {
        bool focused=(SDL_GetWindowFlags(w->window)&SDL_WINDOW_INPUT_FOCUS)!=0;
        if(bt_renderer_draw(w->renderer,&w->session,w->force_draw,focused)) return fail(w,bt_renderer_error(w->renderer));
        w->force_draw=false;
        if(focused) ime_position(w);
    }
    return 0;
}
static void close_window(BtWindow *w, BtSession *deferred) {
    if(w->owner!=getpid()) return;
    cancel_copy(w);
    (void)bt_window_release_keys(w);
    (void)bt_window_release_pointer(w);
    if(deferred && w->session.remote && !bt_remote_observer(&w->session)) {
        bt_session_clipboard_clear(&w->session); w->session.clipboard_enabled=false;
        *deferred=w->session;
        w->session=(BtSession){.master=-1,.control=-1,.status=-1};
    } else {
        if(w->session.remote && !bt_remote_observer(&w->session)) (void)bt_remote_input_flush(&w->session);
        bt_session_close(&w->session);
    }
    if(w->key_event) ghostty_key_event_free(w->key_event);
    if(w->key_encoder) ghostty_key_encoder_free(w->key_encoder);
    if(w->mouse_event) ghostty_mouse_event_free(w->mouse_event);
    if(w->mouse_encoder) ghostty_mouse_encoder_free(w->mouse_encoder);
    bt_renderer_free(w->renderer);
    if(w->surface && !w->embedded) bt_surface_free(w->surface);
    w->key_event=NULL; w->key_encoder=NULL; w->mouse_event=NULL; w->mouse_encoder=NULL;
    w->renderer=NULL; w->window=NULL; w->surface=NULL;
}

void bt_window_close(BtWindow *w) { close_window(w,NULL); }
void bt_window_close_deferred(BtWindow *w, BtSession *deferred) {
    *deferred=(BtSession){.master=-1,.control=-1,.status=-1};
    close_window(w,deferred);
}
