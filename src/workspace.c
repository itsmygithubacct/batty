/* SPDX-License-Identifier: MIT */
#define _POSIX_C_SOURCE 200809L
#include "workspace.h"
#include "presentation.h"
#include "remote.h"
#include "control.h"
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

typedef struct {
    uint64_t id;
    int tab,font_size;
    bool synchronized,disconnected;
    char *name,*session_dir;
    BtRect bounds;
    BtWindow view;
    BtPaneTelemetry telemetry;
    uint64_t telemetry_updated;
    char title[256];
} Pane;
typedef struct {
    uint64_t id,active,zoom,previous;
    BtLayout layout,arranged[2];
    uint64_t order[BT_LAYOUT_PANES];
    unsigned order_count,mode;
    char title[256];
} Tab;
enum { BINDINGS=64, ACTIONS=64, DEFERRED_EVENTS=256, CHROME_HITS=BT_LAYOUT_PANES*11+BT_WORKSPACE_TABS+1 };
#define WORKSPACE_IMAGE_BUDGET (128u * 1024u * 1024u)
#define PROCESS_IMAGE_BUDGET (256u * 1024u * 1024u)
typedef struct { BtRect rect; uint64_t pane; const char *action; } ChromeHit;
typedef struct { SDL_Keycode key,prefix; unsigned modifiers,prefix_modifiers; char action[48]; } Binding;
typedef struct { const char *label,*action; unsigned child; SDL_Keycode key; } MenuEntry;
struct BtWorkspace {
    struct BtWorkspace *next;
    pid_t owner;
    BtControl *control,*scoped_controls[8];
    unsigned scoped_count,scoped_next;
    uint64_t id,capture,previous_tab,prefix_pane,resize_pane;
    uint64_t drag_pane,drag_target;
    BtDirection drag_direction;
    int drag_x,drag_y;
    bool drag_active;
    BtLayout resize_saved;
    uint64_t choices[BT_LAYOUT_PANES];
    unsigned choice_count,choice_index,choice_top;
    bool choose_pages,choose_all,choice_search;
    unsigned choice_scope;
    char choice_query[128];
    unsigned matches[BT_LAYOUT_PANES],match_count;
    uint64_t choice_confirm,choice_send_pane;
    char choice_send_text[1024],choice_notice[128];
    uint64_t preview_pane,preview_bytes,preview_revision,preview_refresh;
    uint64_t rename_pane;
    char rename_text[256];
    bool rename_page;
    SDL_Keycode prefix;
    unsigned prefix_modifiers;
    unsigned buttons;
    BtSurface *surface;
    size_t image_budget;
    Pane panes[BT_LAYOUT_PANES];
    BtSession detaching[BT_LAYOUT_PANES];
    unsigned detach_next;
    Tab tabs[BT_WORKSPACE_TABS];
    int active_tab,font_size;
    char *font;
    bool dirty,closed,focused,failed;
    BtWorkspaceFilter filter;
    void *filter_data;
    SDL_Event deferred[DEFERRED_EVENTS];
    unsigned deferred_start,deferred_count;
    Binding bindings[BINDINGS];
    BtWorkspaceAction actions[ACTIONS];
    unsigned action_start,action_count;
    uint64_t reload_next,reload_pending;
    struct { uint64_t ticket; unsigned status; } reload_results[16];
    bool consumed[SDL_NUM_SCANCODES],suppress_text;
    BtRenderer *labels;
    BtSession label_session;
    int bar_height,button_width;
    bool chrome,bottom_bar,start_badge;
    unsigned menu_depth,menu_kind[4],menu_index[4],menu_top[4];
    uint64_t menu_pane;
    int menu_x,menu_y;
    unsigned pump_next;
    BtWorkspacePumpStats pump_stats;
    unsigned app_count;
    char app_labels[256][256],app_actions[256][32];
    MenuEntry app_items[256];
    unsigned pane_buttons;
    bool settings_open,settings_edge_locked;
    unsigned settings_index,settings_top,settings_result;
    unsigned settings_recording[5];
    unsigned chrome_buttons,hit_count;
    ChromeHit hits[CHROME_HITS];
    char message[256];
    uint64_t confirm_pane;
    char confirm_action[48];
    char error[256];
};
static uint64_t next_id=1;
static BtWorkspace *workspaces;
static size_t process_image_budget=PROCESS_IMAGE_BUDGET;
static Pane *hit(BtWorkspace *, int x, int y);
static uint64_t identity(void) { return next_id++; }
static int fail(BtWorkspace *w, const char *message) {
    snprintf(w->error,sizeof(w->error),"%s",message); return -1;
}
static bool editing_event(const SDL_Event *e) {
    if(e->type==SDL_TEXTEDITING) return true;
#if SDL_VERSION_ATLEAST(2,0,22)
    if(e->type==SDL_TEXTEDITING_EXT) return true;
#endif
    return false;
}
/* Keep the retained frame and pane identity when its owner connection fails.
 * Rendering and other workspace errors still propagate to the host. */
static void connection_state(BtWorkspace *w, Pane *p) {
    if(!p->disconnected && bt_remote_disconnected(&p->view.session)) {
        p->disconnected=true; p->view.error[0]=0;
        (void)bt_window_release_keys(&p->view);
        (void)bt_window_release_pointer(&p->view);
        if(w->capture==p->id) { w->capture=0; w->buttons=0; }
        w->dirty=true;
    }
}
static int input_failure(BtWorkspace *w, Pane *p) {
    connection_state(w,p);
    if(p->disconnected) { p->view.error[0]=0; return 0; }
    return fail(w,p->view.error[0]?p->view.error:p->view.session.error);
}
static int enqueue(BtWorkspace *w, const char *name, uint64_t pane_id) {
    if(w->action_count==ACTIONS) return fail(w,"Workspace action queue is full");
    BtWorkspaceAction *a=&w->actions[(w->action_start+w->action_count++)%ACTIONS];
    a->pane=pane_id; memcpy(a->name,name,strlen(name)+1); return 0;
}
int bt_workspace_reload_request(BtWorkspace *w, uint64_t *ticket) {
    if(w->reload_pending) { *ticket=w->reload_pending; return 0; }
    if(w->reload_next==UINT64_MAX) return fail(w,"Settings reload ticket limit reached");
    uint64_t next=w->reload_next+1;
    if(enqueue(w,"remote-reload-settings",next)) return -1;
    w->reload_pending=w->reload_next=next; *ticket=next; return 0;
}
int bt_workspace_reload_complete(BtWorkspace *w, uint64_t ticket, unsigned status) {
    if(!ticket || ticket!=w->reload_pending || status>255) return fail(w,"No matching settings reload request");
    w->reload_results[ticket%16].ticket=ticket;
    w->reload_results[ticket%16].status=status;
    w->reload_pending=0; return 0;
}
int bt_workspace_reload_result(BtWorkspace *w, uint64_t ticket, bool *done, unsigned *status) {
    if(!ticket || ticket>w->reload_next) return fail(w,"Unknown settings reload ticket");
    if(ticket==w->reload_pending) { *done=false; *status=0; return 0; }
    if(w->reload_results[ticket%16].ticket!=ticket) return fail(w,"Expired settings reload ticket");
    *done=true; *status=w->reload_results[ticket%16].status; return 0;
}
static unsigned modifiers(SDL_Keymod mod) {
    /* AltGr text must not match Ctrl+Alt or unmodified host shortcuts. */
    if(mod&KMOD_MODE) return 16u;
    return ((mod&KMOD_CTRL)?1u:0u) | ((mod&KMOD_ALT)?2u:0u) |
        ((mod&KMOD_SHIFT)?4u:0u) | ((mod&KMOD_GUI)?8u:0u);
}
static int chord_key(BtWorkspace *w, const char *chord, SDL_Keycode *out, unsigned *mask) {
    if(!chord || !*chord) return fail(w,"Missing key chord");
    unsigned mod=0;
    for(;;) {
        unsigned bit=0; size_t length=0;
        if(!strncmp(chord,"Ctrl+",5)) { bit=1; length=5; }
        else if(!strncmp(chord,"Alt+",4)) { bit=2; length=4; }
        else if(!strncmp(chord,"Shift+",6)) { bit=4; length=6; }
        else if(!strncmp(chord,"Super+",6)) { bit=8; length=6; }
        else break;
        if(mod&bit) return fail(w,"Repeated chord modifier");
        mod|=bit; chord+=length;
    }
    SDL_Keycode key=SDL_GetKeyFromName(chord);
    if(key==SDLK_UNKNOWN) return fail(w,"Unknown key name");
    *out=key; *mask=mod; return 0;
}
int bt_workspace_bind_sequence(BtWorkspace *w, const char *prefix, const char *chord, const char *action) {
    if(!action || strlen(action)>=sizeof(w->actions[0].name)) return fail(w,"Invalid action name");
    for(const char *p=action;*p;++p)
        if(!((*p>='a' && *p<='z') || (*p>='0' && *p<='9') || *p=='-' || *p=='_'))
            return fail(w,"Action names use lowercase letters, digits, hyphens and underscores");
    SDL_Keycode key,first=0; unsigned mod,first_mod=0;
    if(chord_key(w,chord,&key,&mod) || (prefix && chord_key(w,prefix,&first,&first_mod))) return -1;
    Binding *slot=NULL;
    for(unsigned i=0;i<BINDINGS;++i) {
        Binding *b=&w->bindings[i];
        if(*action && b->key && ((!first && b->prefix==key && b->prefix_modifiers==mod) ||
           (first && !b->prefix && b->key==first && b->modifiers==first_mod)))
            return fail(w,"A prefix cannot also be a direct binding");
        if(b->key==key && b->modifiers==mod && b->prefix==first && b->prefix_modifiers==first_mod) { slot=b; break; }
        if(!b->key && !slot) slot=b;
    }
    if(!slot) return fail(w,"Workspace binding limit reached");
    *slot=(Binding){.key=*action?key:0,.modifiers=mod,.prefix=first,.prefix_modifiers=first_mod};
    snprintf(slot->action,sizeof(slot->action),"%s",action);
    w->prefix=0; w->dirty=true; return 0;
}
int bt_workspace_bind(BtWorkspace *w, const char *chord, const char *action) {
    return bt_workspace_bind_sequence(w,NULL,chord,action);
}
static int workspace_event(BtWorkspace *w, const SDL_Event *e);
static int deferred_events(BtWorkspace *w) {
    while(w->deferred_count && !w->action_count) {
        SDL_Event event=w->deferred[w->deferred_start];
        w->deferred_start=(w->deferred_start+1)%DEFERRED_EVENTS; --w->deferred_count;
        int result=workspace_event(w,&event);
#if SDL_VERSION_ATLEAST(2,0,22)
        if(event.type==SDL_TEXTEDITING_EXT) SDL_free(event.editExt.text);
#endif
        if(result) { w->failed=true; return -1; }
    }
    return 0;
}
int bt_workspace_action(BtWorkspace *w, BtWorkspaceAction *action) {
    if(deferred_events(w)) { *action=(BtWorkspaceAction){0}; return 0; }
    if(!w->action_count) { *action=(BtWorkspaceAction){0}; return 0; }
    *action=w->actions[w->action_start];
    w->action_start=(w->action_start+1)%ACTIONS; --w->action_count; return 1;
}
static int release_keys(BtWorkspace *w);
static int resize_finish(BtWorkspace *w, bool cancel);
static int resize_key(BtWorkspace *w, SDL_Keycode key);
static int chooser_event(BtWorkspace *w, const SDL_Event *e);
static int shortcut(BtWorkspace *w, const SDL_Event *e) {
    if(e->type==SDL_WINDOWEVENT && e->window.event==SDL_WINDOWEVENT_FOCUS_LOST) {
        if(resize_finish(w,true)) return -1;
        memset(w->consumed,0,sizeof(w->consumed)); w->suppress_text=false;
        w->prefix=0; w->dirty=true;
    }
    if(e->type==SDL_TEXTINPUT || editing_event(e)) return w->suppress_text || w->prefix || w->resize_pane;
    if(e->type!=SDL_KEYDOWN && e->type!=SDL_KEYUP) return 0;
    SDL_Scancode scan=e->key.keysym.scancode;
    if(scan<0 || scan>=SDL_NUM_SCANCODES) return 0;
    if(e->type==SDL_KEYUP) {
        bool consumed=w->consumed[scan]; w->consumed[scan]=false; return consumed;
    }
    if(w->resize_pane) {
        w->consumed[scan]=true; w->suppress_text=true;
        return resize_key(w,e->key.keysym.sym)?-1:1;
    }
    if(e->key.repeat && w->consumed[scan]) { w->suppress_text=true; return 1; }
    w->suppress_text=false;
    if(w->prefix) {
        w->consumed[scan]=true; w->suppress_text=true;
        if(e->key.repeat) return 1;
        SDL_Keycode key=e->key.keysym.sym;
        if(key==SDLK_LSHIFT || key==SDLK_RSHIFT || key==SDLK_LCTRL || key==SDLK_RCTRL ||
           key==SDLK_LALT || key==SDLK_RALT || key==SDLK_LGUI || key==SDLK_RGUI) return 1;
        SDL_Keycode prefix=w->prefix; w->prefix=0; w->dirty=true;
        if(key==SDLK_ESCAPE) return 1;
        for(unsigned i=0;i<BINDINGS;++i) {
            Binding *b=&w->bindings[i];
            if(b->key==key && b->modifiers==modifiers(e->key.keysym.mod) && b->prefix==prefix &&
               b->prefix_modifiers==w->prefix_modifiers)
                return enqueue(w,b->action,w->prefix_pane)?-1:1;
        }
        return 1; /* Unknown leader keys cancel without reaching a child. */
    }
    for(unsigned i=0;i<BINDINGS;++i) {
        Binding *b=&w->bindings[i];
        SDL_Keycode key=b->prefix?b->prefix:b->key;
        unsigned mod=b->prefix?b->prefix_modifiers:b->modifiers;
        if(!b->key || key!=e->key.keysym.sym || mod!=modifiers(e->key.keysym.mod)) continue;
        w->consumed[scan]=true; w->suppress_text=true;
        if(e->key.repeat) return 1;
        if(b->prefix) {
            if(release_keys(w)) return -1;
            w->prefix=b->prefix; w->prefix_modifiers=b->prefix_modifiers;
            w->prefix_pane=bt_workspace_active(w); w->dirty=true; return 1;
        }
        return enqueue(w,b->action,bt_workspace_active(w))?-1:1;
    }
    return 0;
}
const char *bt_workspace_error(BtWorkspace *w) { return w->error; }
BtSurface *bt_workspace_surface(BtWorkspace *w) { return w->surface; }
bool bt_workspace_closed(BtWorkspace *w) { return w->closed; }
static Pane *pane(BtWorkspace *w, uint64_t id) {
    if(id) for(unsigned i=0;i<BT_LAYOUT_PANES;++i) if(w->panes[i].id==id) return &w->panes[i];
    return NULL;
}
uint64_t bt_workspace_active(BtWorkspace *w) {
    return w->active_tab>=0?w->tabs[w->active_tab].active:0;
}
static bool visible(BtWorkspace *w, const Pane *p) {
    if(!p->id || p->tab!=w->active_tab) return false;
    Tab *t=&w->tabs[p->tab];
    return t->zoom?t->zoom==p->id:t->mode==1?t->active==p->id:true;
}
static BtLayout *display_layout(Tab *t) {
    if(t->mode<2) return &t->layout;
    uint64_t ids[BT_LAYOUT_PANES]; unsigned count=bt_layout_order(&t->layout,ids);
    if(count!=t->order_count || memcmp(ids,t->order,count*sizeof(*ids))) {
        for(unsigned i=0;i<2;++i) bt_layout_arrange(&t->arranged[i],ids,count,i==1);
        memcpy(t->order,ids,count*sizeof(*ids)); t->order_count=count;
    }
    return &t->arranged[t->mode-2];
}
static void placed(void *data, uint64_t id, BtRect bounds) {
    Pane *p=pane(data,id);
    if(p) p->bounds=bounds;
}
static BtRect area(BtWorkspace *w) {
    BtRect r={0}; SDL_GL_GetDrawableSize(bt_surface_window(w->surface),&r.width,&r.height); return r;
}
static BtRect content_area(BtWorkspace *w) {
    BtRect r=area(w);
    if(w->chrome) { if(!w->bottom_bar) r.y=w->bar_height; r.height-=w->bar_height; }
    return r;
}
static unsigned pane_button_count(const BtWorkspace *w) {
    unsigned count=0;
    for(unsigned i=0;i<9;++i) if(w->pane_buttons&(1u<<i)) ++count;
    return count;
}
static int geometry(BtWorkspace *w) {
    if(w->chrome) {
        int cw,ch;
        if(bt_renderer_metrics(w->labels,&cw,&ch)) return fail(w,bt_renderer_error(w->labels));
        w->label_session.cell_width=cw; w->label_session.cell_height=ch;
        w->bar_height=ch+16; w->button_width=2*cw+16;
    }
    if(w->active_tab<0) return 0;
    Tab *t=&w->tabs[w->active_tab];
    BtLayout *layout=display_layout(t);
    uint64_t single=t->zoom?t->zoom:t->mode==1?t->active:0;
    BtRect r=content_area(w);
    if(r.width<1 || r.height<1) return 0; /* Minimized surface. */
    int mw=17,mh=17;
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i) {
        Pane *p=&w->panes[i];
        if(!p->id || p->tab!=w->active_tab) continue;
        int cw,ch;
        if(bt_renderer_metrics(p->view.renderer,&cw,&ch)) return fail(w,bt_renderer_error(p->view.renderer));
        if(cw+16>mw) mw=cw+16;
        if(ch+16>mh) mh=ch+16;
    }
    if(w->chrome) {
        mh+=w->bar_height;
        int chrome_min=(int)(pane_button_count(w)+1)*w->button_width;
        if(mw<chrome_min) mw=chrome_min;
    }
    int minw=mw,minh=mh;
    if(!single && bt_layout_minimum(layout,mw,mh,&minw,&minh)) return fail(w,"Invalid layout minimum");
    SDL_Window *window=bt_surface_window(w->surface);
    int lw,lh; SDL_GetWindowSize(window,&lw,&lh);
    int logical_w=(int)(((int64_t)minw*lw+r.width-1)/r.width);
    int screen_height=area(w).height;
    int logical_h=(int)(((int64_t)(minh+(w->chrome?w->bar_height:0))*lh+screen_height-1)/screen_height);
    SDL_SetWindowMinimumSize(window,logical_w>120?logical_w:120,logical_h>80?logical_h:80);
    if(r.width<minw || r.height<minh) {
        SDL_SetWindowSize(window,lw>logical_w?lw:logical_w,lh>logical_h?lh:logical_h);
        r=content_area(w);
        if(r.width<minw || r.height<minh) return 0; /* Wait for the host resize event. */
    }
    if(single) pane(w,single)->bounds=r;
    else if(bt_layout_place(layout,r,mw,mh,placed,w)) return fail(w,"Window is too small for its panes");
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i) {
        Pane *p=&w->panes[i];
        if(!visible(w,p)) continue;
        BtRect b=p->bounds;
        if(w->chrome) { b.y+=w->bar_height; b.height-=w->bar_height; }
        if(bt_window_set_region(&p->view,(SDL_Rect){b.x,b.y,b.width,b.height}) && input_failure(w,p)) return -1;
        connection_state(w,p);
    }
    w->dirty=true; return 0;
}
static int release_keys(BtWorkspace *w) {
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i)
        if(w->panes[i].id && bt_window_release_keys(&w->panes[i].view) && input_failure(w,&w->panes[i])) return -1;
    return 0;
}
static int focused(BtWorkspace *w, Pane *p, bool value) {
    if(!value && release_keys(w)) return -1;
    if(p && bt_window_focus(&p->view,value) && input_failure(w,p)) return -1;
    return 0;
}
int bt_workspace_font(BtWorkspace *w, uint64_t id, int delta) {
    Pane *p=pane(w,id);
    if(!p || delta<-90 || delta>90 || p->font_size+delta<6 || p->font_size+delta>96)
        return fail(w,"Invalid pane font size");
    BtRenderer *r=bt_renderer_new_shared(bt_surface_window(w->surface),bt_surface_context(w->surface),
        w->font,p->font_size+delta,w->error,sizeof(w->error));
    if(!r) return -1;
    bt_renderer_free(p->view.renderer); p->view.renderer=r; p->font_size+=delta;
    p->view.force_draw=true; return geometry(w);
}
int bt_workspace_font_all(BtWorkspace *w, int size) {
    if(size<6 || size>96) return fail(w,"Invalid workspace font size");
    BtRenderer *prepared[BT_LAYOUT_PANES]={0};
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i) {
        if(!w->panes[i].id || w->panes[i].font_size==size) continue;
        prepared[i]=bt_renderer_new_shared(bt_surface_window(w->surface),
            bt_surface_context(w->surface),w->font,size,w->error,sizeof(w->error));
        if(!prepared[i]) {
            for(unsigned j=0;j<BT_LAYOUT_PANES;++j) bt_renderer_free(prepared[j]);
            return -1;
        }
    }
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i) if(prepared[i]) {
        Pane *p=&w->panes[i];
        bt_renderer_free(p->view.renderer); p->view.renderer=prepared[i];
        p->font_size=size; p->view.force_draw=true;
    }
    w->font_size=size;
    return geometry(w);
}
int bt_workspace_chrome_buttons(BtWorkspace *w, unsigned mask) {
    if(mask>511) return fail(w,"Pane button mask must be between 0 and 511");
    w->pane_buttons=mask;
    return geometry(w);
}
int bt_workspace_chrome_edge(BtWorkspace *w, bool bottom) {
    if(resize_finish(w,true)) return -1;
    w->choice_count=0; w->rename_pane=0; w->bottom_bar=bottom;
    return geometry(w);
}
int bt_workspace_chrome(BtWorkspace *w, bool enabled) {
    if(!enabled) { w->message[0]=0; w->confirm_pane=0; w->menu_depth=0; w->settings_open=false; w->choice_count=0; w->rename_pane=0;
        w->drag_pane=w->drag_target=0; w->drag_active=false; }
    if(enabled && !w->labels) {
        w->labels=bt_renderer_new_shared(bt_surface_window(w->surface),bt_surface_context(w->surface),
            "monospace",14,w->error,sizeof(w->error));
        if(!w->labels) return -1;
        if(ghostty_terminal_new(NULL,&w->label_session.terminal,1000,1)!=GHOSTTY_SUCCESS) {
            bt_renderer_free(w->labels); w->labels=NULL; return fail(w,"Could not create chrome labels");
        }
    }
    if(enabled) {
        int cw,ch;
        if(bt_renderer_metrics(w->labels,&cw,&ch)) return fail(w,bt_renderer_error(w->labels));
        w->label_session.cell_width=cw; w->label_session.cell_height=ch;
        w->bar_height=ch+16; w->button_width=2*cw+16;
    }
    w->chrome=enabled; w->chrome_buttons=0; w->hit_count=0; return geometry(w);
}
/* Titles are untrusted terminal output. Only printable UTF-8 enters the label
 * terminal; malformed sequences and all control codepoints are discarded. */
static void label_text(GhosttyTerminal terminal, const char *text) {
    const unsigned char *p=(const unsigned char *)text;
    size_t left=strlen(text);
    while(left) {
        unsigned n=1; uint32_t c=*p,minimum=0;
        if(c>=0xc2 && c<=0xdf) { n=2; c&=31; minimum=0x80; }
        else if(c>=0xe0 && c<=0xef) { n=3; c&=15; minimum=0x800; }
        else if(c>=0xf0 && c<=0xf4) { n=4; c&=7; minimum=0x10000; }
        else if(c>=0x80) { ++p; --left; continue; }
        bool valid=n<=left;
        for(unsigned i=1;valid && i<n;++i) {
            if((p[i]&0xc0)!=0x80) valid=false;
            else c=(c<<6)|(p[i]&63);
        }
        if(!valid) { ++p; --left; continue; }
        if(c>=minimum && c>=32 && !(c>=127 && c<=159) && !(c>=0xd800 && c<=0xdfff) && c<=0x10ffff)
            ghostty_terminal_vt_write(terminal,p,n);
        p+=n; left-=n;
    }
}
static int label(BtWorkspace *w, BtRect r, const char *text, bool selected) {
    if(r.width<1 || r.height<1) return 0;
    GhosttyTerminal terminal=w->label_session.terminal;
    GhosttyColorRgb bg=selected?(GhosttyColorRgb){40,79,121}:(GhosttyColorRgb){32,40,53};
    GhosttyColorRgb fg={232,238,247};
    ghostty_terminal_set(terminal,GHOSTTY_TERMINAL_OPT_COLOR_BACKGROUND,&bg);
    ghostty_terminal_set(terminal,GHOSTTY_TERMINAL_OPT_COLOR_FOREGROUND,&fg);
    const char reset[]="\033[2J\033[H\033[?25l\033[?7l";
    ghostty_terminal_vt_write(terminal,(const uint8_t *)reset,sizeof(reset)-1);
    label_text(terminal,text);
    if(bt_renderer_draw_region(w->labels,&w->label_session,(SDL_Rect){r.x,r.y,r.width,r.height},false))
        return fail(w,bt_renderer_error(w->labels));
    return 0;
}
static bool printable_title(const char *text) {
    const unsigned char *p=(const unsigned char *)text;
    size_t left=strlen(text);
    if(left>255) return false;
    while(left) {
        unsigned n=1; uint32_t c=*p,min=0;
        if(c>=0xc2 && c<=0xdf) { n=2; c&=31; min=0x80; }
        else if(c>=0xe0 && c<=0xef) { n=3; c&=15; min=0x800; }
        else if(c>=0xf0 && c<=0xf4) { n=4; c&=7; min=0x10000; }
        else if(c>=0x80) return false;
        if(n>left) return false;
        for(unsigned i=1;i<n;++i) {
            if((p[i]&0xc0)!=0x80) return false;
            c=(c<<6)|(p[i]&63);
        }
        if(c<min || c<32 || (c>=127 && c<=159) || (c>=0xd800 && c<=0xdfff) || c>0x10ffff) return false;
        p+=n; left-=n;
    }
    return true;
}
static bool layout_has(const BtLayout *layout, uint64_t id) {
    if(!id) return false;
    for(unsigned i=0;i<BT_LAYOUT_NODES;++i)
        if(layout->nodes[i].used && layout->nodes[i].pane==id) return true;
    return false;
}
int bt_workspace_layout_capture(BtWorkspace *w, BtWorkspaceLayout *out) {
    if(!out) return fail(w,"Missing workspace layout output");
    memset(out,0,sizeof(*out)); out->version=2; out->previous_page=-1;
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i) {
        Pane *p=&w->panes[i]; if(!p->id) continue;
        unsigned at=out->pane_count++;
        out->panes[at].id=p->id; out->panes[at].font_size=p->font_size;
        out->panes[at].synchronized=p->synchronized;
        memcpy(out->panes[at].title,p->title,sizeof(p->title));
    }
    for(int i=0;i<BT_WORKSPACE_TABS;++i) {
        if(!w->tabs[i].id) continue;
        unsigned at=out->page_count++;
        if(i==w->active_tab) out->active_page=at;
        if(w->tabs[i].id==w->previous_tab) out->previous_page=(int)at;
        /* Refresh derived trees in a copy: capture must not change the source. */
        Tab copy=w->tabs[i]; copy.mode=2; (void)display_layout(&copy);
        BtWorkspacePageLayout *page=&out->pages[at];
        page->splits=copy.layout; page->tall=copy.arranged[0]; page->grid=copy.arranged[1];
        page->mode=w->tabs[i].mode; page->active=copy.active; page->zoom=copy.zoom;
        page->previous=layout_has(&copy.layout,copy.previous)?copy.previous:0;
        memcpy(page->title,copy.title,sizeof(page->title));
    }
    return 0;
}
BtWorkspaceAppearance bt_workspace_appearance(BtWorkspace *w) {
    BtWorkspaceAppearance out={.font=w->font,.font_size=w->font_size,.chrome=w->chrome,
        .bottom_bar=w->bottom_bar,.start_badge=w->start_badge,.pane_buttons=w->pane_buttons};
    SDL_GetWindowSize(bt_surface_window(w->surface),&out.width,&out.height);
    return out;
}
int bt_workspace_layout_validate(const BtWorkspaceLayout *state) {
    if(!state || (state->version!=1 && state->version!=2) || !state->page_count || state->page_count>BT_WORKSPACE_TABS ||
       !state->pane_count || state->pane_count>BT_LAYOUT_PANES || state->active_page>=state->page_count ||
       state->previous_page < -1 || state->previous_page>=(int)state->page_count) goto invalid;
    bool seen[BT_LAYOUT_PANES]={0};
    for(unsigned i=0;i<state->pane_count;++i) {
        if(!state->panes[i].id || state->panes[i].font_size<6 || state->panes[i].font_size>96 ||
           !memchr(state->panes[i].title,0,sizeof(state->panes[i].title)) ||
           !printable_title(state->panes[i].title) ||
           (state->version==1 && state->panes[i].title[0])) goto invalid;
        for(unsigned j=0;j<i;++j) if(state->panes[i].id==state->panes[j].id) goto invalid;
    }
    for(unsigned i=0;i<state->page_count;++i) {
        const BtWorkspacePageLayout *page=&state->pages[i];
        if(page->mode>3 || !memchr(page->title,0,sizeof(page->title)) || !printable_title(page->title) ||
           bt_layout_validate(&page->splits) || bt_layout_validate(&page->tall) || bt_layout_validate(&page->grid) ||
           !page->splits.count || !layout_has(&page->splits,page->active) ||
           (page->zoom && page->zoom!=page->active) ||
           (page->previous && !layout_has(&page->splits,page->previous))) goto invalid;
        uint64_t ids[BT_LAYOUT_PANES],tall[BT_LAYOUT_PANES],grid[BT_LAYOUT_PANES];
        unsigned n=bt_layout_order(&page->splits,ids);
        if(bt_layout_order(&page->tall,tall)!=n || bt_layout_order(&page->grid,grid)!=n ||
           memcmp(ids,tall,n*sizeof(*ids)) || memcmp(ids,grid,n*sizeof(*ids))) goto invalid;
        for(unsigned j=0;j<n;++j) {
            unsigned at=0;
            while(at<state->pane_count && state->panes[at].id!=ids[j]) ++at;
            if(at==state->pane_count || seen[at]) goto invalid;
            seen[at]=true;
        }
    }
    for(unsigned i=0;i<state->pane_count;++i) if(!seen[i]) goto invalid;
    return 0;
invalid:
    errno=EINVAL; return -1;
}
int bt_workspace_layout_prune(BtWorkspaceLayout *state, const uint64_t *ids, unsigned count) {
    if(bt_workspace_layout_validate(state) || !ids || !count || count>state->pane_count) {
        errno=EINVAL; return -1;
    }
    bool kept[BT_LAYOUT_PANES]={0};
    for(unsigned i=0;i<count;++i) {
        unsigned at=0;
        while(at<state->pane_count && state->panes[at].id!=ids[i]) ++at;
        if(at==state->pane_count || kept[at]) { errno=EINVAL; return -1; }
        kept[at]=true;
    }
    BtWorkspaceLayout *result=malloc(sizeof(*result));
    if(!result) return -1;
    *result=*state;
    for(unsigned i=0;i<state->pane_count;++i) if(!kept[i]) {
        uint64_t id=state->panes[i].id;
        for(unsigned page=0;page<result->page_count;++page) {
            BtWorkspacePageLayout *p=&result->pages[page];
            if(!layout_has(&p->splits,id)) continue;
            (void)bt_layout_remove(&p->splits,id);
            (void)bt_layout_remove(&p->tall,id);
            (void)bt_layout_remove(&p->grid,id);
            uint64_t order[BT_LAYOUT_PANES];
            if(bt_layout_order(&p->splits,order)) {
                if(p->active==id) p->active=order[0];
                if(p->zoom==id) p->zoom=0;
                if(p->previous==id) p->previous=0;
            }
        }
    }
    result->pane_count=0; result->page_count=0; result->previous_page=-1;
    unsigned active=UINT_MAX;
    for(unsigned i=0;i<state->pane_count;++i)
        if(kept[i]) result->panes[result->pane_count++]=state->panes[i];
    for(unsigned i=0;i<state->page_count;++i) {
        if(!result->pages[i].splits.count) continue;
        if(i==state->active_page) active=result->page_count;
        if((int)i==state->previous_page) result->previous_page=(int)result->page_count;
        result->pages[result->page_count++]=result->pages[i];
    }
    result->active_page=active==UINT_MAX?0:active;
    int rc=bt_workspace_layout_validate(result);
    if(!rc) *state=*result;
    free(result); return rc;
}
/* Explicit byte fields keep saved layouts independent of C ABI and host
 * endianness. Bounds are checked before every access, including truncated IDs. */
typedef struct { uint8_t *data; size_t at,length; bool failed; } LayoutBytes;
static void layout_put(LayoutBytes *b, uint64_t value, unsigned width) {
    if(b->at>b->length || width>b->length-b->at) { b->failed=true; return; }
    for(unsigned i=0;i<width;++i) b->data[b->at++]=(uint8_t)(value>>(8*i));
}
static uint64_t layout_get(LayoutBytes *b, unsigned width) {
    if(b->at>b->length || width>b->length-b->at) { b->failed=true; return 0; }
    uint64_t value=0;
    for(unsigned i=0;i<width;++i) value|=(uint64_t)b->data[b->at++]<<(8*i);
    return value;
}
static void layout_pack_tree(LayoutBytes *b, const BtLayout *tree) {
    unsigned used=0;
    for(unsigned i=0;i<BT_LAYOUT_NODES;++i) if(tree->nodes[i].used) ++used;
    layout_put(b,tree->root+1,1); layout_put(b,tree->count,1); layout_put(b,used,1);
    for(unsigned i=0;i<BT_LAYOUT_NODES;++i) {
        const BtLayoutNode *n=&tree->nodes[i]; if(!n->used) continue;
        layout_put(b,i,1); layout_put(b,n->horizontal,1); layout_put(b,n->pane,8);
        layout_put(b,n->parent+1,1); layout_put(b,n->first+1,1); layout_put(b,n->second+1,1);
        layout_put(b,n->ratio,4);
    }
}
static void layout_unpack_tree(LayoutBytes *b, BtLayout *tree) {
    bt_layout_init(tree);
    tree->root=(int)layout_get(b,1)-1; tree->count=(unsigned)layout_get(b,1);
    unsigned used=(unsigned)layout_get(b,1);
    if(used>BT_LAYOUT_NODES) { b->failed=true; return; }
    int previous=-1;
    for(unsigned i=0;i<used && !b->failed;++i) {
        unsigned at=(unsigned)layout_get(b,1),horizontal=(unsigned)layout_get(b,1);
        if(at>=BT_LAYOUT_NODES || (int)at<=previous || horizontal>1) { b->failed=true; return; }
        previous=(int)at;
        BtLayoutNode *n=&tree->nodes[at]; n->used=true; n->horizontal=horizontal!=0;
        n->pane=layout_get(b,8); n->parent=(int)layout_get(b,1)-1;
        n->first=(int)layout_get(b,1)-1; n->second=(int)layout_get(b,1)-1;
        n->ratio=(unsigned)layout_get(b,4);
    }
}
int bt_workspace_layout_pack(const BtWorkspaceLayout *state, uint8_t **out, size_t *length) {
    if(out) *out=NULL;
    if(length) *length=0;
    if(!out || !length || bt_workspace_layout_validate(state)) { errno=EINVAL; return -1; }
    uint8_t *data=malloc(BT_WORKSPACE_LAYOUT_MAX_BYTES);
    if(!data) return -1;
    LayoutBytes b={.data=data,.length=BT_WORKSPACE_LAYOUT_MAX_BYTES};
    layout_put(&b,state->version==2?0x324c5742:0x314c5742,4); /* BWL2 or BWL1 */
    layout_put(&b,state->page_count,1); layout_put(&b,state->pane_count,1);
    layout_put(&b,state->active_page,1); layout_put(&b,state->previous_page+1,1);
    for(unsigned i=0;i<state->pane_count;++i) {
        layout_put(&b,state->panes[i].id,8); layout_put(&b,state->panes[i].font_size,1);
        layout_put(&b,state->panes[i].synchronized,1);
    }
    if(state->version==2) for(unsigned i=0;i<state->pane_count;++i) {
        size_t n=strlen(state->panes[i].title); layout_put(&b,n,1);
        for(size_t j=0;j<n;++j) layout_put(&b,(unsigned char)state->panes[i].title[j],1);
    }
    for(unsigned i=0;i<state->page_count;++i) {
        const BtWorkspacePageLayout *page=&state->pages[i];
        layout_put(&b,page->mode,1); layout_put(&b,page->active,8);
        layout_put(&b,page->zoom,8); layout_put(&b,page->previous,8);
        size_t n=strlen(page->title); layout_put(&b,n,1);
        for(size_t j=0;j<n;++j) layout_put(&b,(unsigned char)page->title[j],1);
        layout_pack_tree(&b,&page->splits); layout_pack_tree(&b,&page->tall); layout_pack_tree(&b,&page->grid);
    }
    if(b.failed) { free(data); errno=E2BIG; return -1; }
    *out=data; *length=b.at; return 0;
}
int bt_workspace_layout_unpack(const void *data, size_t length, BtWorkspaceLayout **out) {
    if(out) *out=NULL;
    if(!out || !data || length<8 || length>BT_WORKSPACE_LAYOUT_MAX_BYTES) { errno=EINVAL; return -1; }
    BtWorkspaceLayout *state=calloc(1,sizeof(*state));
    if(!state) return -1;
    LayoutBytes b={.data=(uint8_t *)data,.length=length};
    uint64_t magic=layout_get(&b,4);
    if(magic!=0x314c5742 && magic!=0x324c5742) goto invalid;
    state->version=magic==0x324c5742?2:1;
    state->page_count=(unsigned)layout_get(&b,1); state->pane_count=(unsigned)layout_get(&b,1);
    state->active_page=(unsigned)layout_get(&b,1); state->previous_page=(int)layout_get(&b,1)-1;
    if(state->page_count>BT_WORKSPACE_TABS || state->pane_count>BT_LAYOUT_PANES) goto invalid;
    for(unsigned i=0;i<state->pane_count;++i) {
        state->panes[i].id=layout_get(&b,8); state->panes[i].font_size=(int)layout_get(&b,1);
        unsigned sync=(unsigned)layout_get(&b,1); if(sync>1) goto invalid;
        state->panes[i].synchronized=sync!=0;
    }
    if(state->version==2) for(unsigned i=0;i<state->pane_count && !b.failed;++i) {
        unsigned n=(unsigned)layout_get(&b,1);
        for(unsigned j=0;j<n && !b.failed;++j) {
            state->panes[i].title[j]=(char)layout_get(&b,1);
            if(!state->panes[i].title[j]) goto invalid;
        }
    }
    for(unsigned i=0;i<state->page_count && !b.failed;++i) {
        BtWorkspacePageLayout *page=&state->pages[i];
        page->mode=(unsigned)layout_get(&b,1); page->active=layout_get(&b,8);
        page->zoom=layout_get(&b,8); page->previous=layout_get(&b,8);
        unsigned n=(unsigned)layout_get(&b,1);
        for(unsigned j=0;j<n && !b.failed;++j) {
            page->title[j]=(char)layout_get(&b,1);
            if(!page->title[j]) goto invalid;
        }
        layout_unpack_tree(&b,&page->splits); layout_unpack_tree(&b,&page->tall); layout_unpack_tree(&b,&page->grid);
    }
    if(b.failed || b.at!=length || bt_workspace_layout_validate(state)) goto invalid;
    *out=state; return 0;
invalid:
    free(state); errno=EINVAL; return -1;
}
static uint64_t mapped_id(const BtPaneMapping *map, unsigned count, uint64_t id) {
    for(unsigned i=0;i<count;++i) if(map[i].saved==id) return map[i].current;
    return 0;
}
static void remap_layout(BtLayout *layout, const BtPaneMapping *map, unsigned count) {
    for(unsigned i=0;i<BT_LAYOUT_NODES;++i)
        if(layout->nodes[i].used && layout->nodes[i].pane)
            layout->nodes[i].pane=mapped_id(map,count,layout->nodes[i].pane);
}
int bt_workspace_layout_apply(BtWorkspace *w, const BtWorkspaceLayout *state,
                               const BtPaneMapping *map, unsigned count) {
    if(bt_workspace_layout_validate(state) || count!=state->pane_count || !map)
        return fail(w,"Invalid workspace layout or mapping header");
    unsigned current_count=0;
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i) if(w->panes[i].id) ++current_count;
    if(count!=current_count) return fail(w,"Layout must include every current pane");
    for(unsigned i=0;i<count;++i) {
        if(!state->panes[i].id || state->panes[i].font_size<6 || state->panes[i].font_size>96 ||
           !map[i].saved || !pane(w,map[i].current)) return fail(w,"Invalid layout pane or mapping");
        for(unsigned j=0;j<i;++j)
            if(state->panes[j].id==state->panes[i].id || map[j].saved==map[i].saved ||
               map[j].current==map[i].current) return fail(w,"Duplicate layout pane or mapping");
        if(!mapped_id(map,count,state->panes[i].id)) return fail(w,"Missing saved pane mapping");
    }
    Tab *tabs=calloc(BT_WORKSPACE_TABS,sizeof(*tabs));
    if(!tabs) return fail(w,"Could not allocate restored layout");
    int membership[BT_LAYOUT_PANES];
    for(unsigned i=0;i<count;++i) membership[i]=-1;
    for(unsigned i=0;i<state->page_count;++i) {
        const BtWorkspacePageLayout *page=&state->pages[i];
        uint64_t ids[BT_LAYOUT_PANES];
        unsigned n=bt_layout_order(&page->splits,ids);
        for(unsigned j=0;j<n;++j) {
            unsigned at=0;
            while(at<count && state->panes[at].id!=ids[j]) ++at;
            if(at==count || membership[at]!=-1) goto invalid;
            membership[at]=(int)i;
        }
        Tab *t=&tabs[i];
        t->layout=page->splits; t->arranged[0]=page->tall; t->arranged[1]=page->grid;
        remap_layout(&t->layout,map,count); remap_layout(&t->arranged[0],map,count); remap_layout(&t->arranged[1],map,count);
        t->order_count=bt_layout_order(&t->layout,t->order);
        t->active=mapped_id(map,count,page->active); t->zoom=mapped_id(map,count,page->zoom);
        t->previous=mapped_id(map,count,page->previous); t->mode=page->mode;
        memcpy(t->title,page->title,sizeof(t->title));
    }
    for(unsigned i=0;i<count;++i) if(membership[i]<0) goto invalid;
    /* Allocate every changed font before replacing any renderer. A resource
     * failure leaves the caller's layout and font choices intact. */
    BtRenderer *renderers[BT_LAYOUT_PANES]={0};
    for(unsigned i=0;i<count;++i) {
        Pane *p=pane(w,mapped_id(map,count,state->panes[i].id));
        if(p->font_size==state->panes[i].font_size) continue;
        renderers[i]=bt_renderer_new_shared(bt_surface_window(w->surface),bt_surface_context(w->surface),
            w->font,state->panes[i].font_size,w->error,sizeof(w->error));
        if(!renderers[i]) goto operational_failure;
    }
    if(focused(w,pane(w,bt_workspace_active(w)),false)) goto operational_failure;
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i)
        if(w->panes[i].id && bt_window_release_pointer(&w->panes[i].view) && input_failure(w,&w->panes[i]))
            goto operational_failure;
    for(unsigned i=0;i<state->page_count;++i) tabs[i].id=identity();
    memcpy(w->tabs,tabs,sizeof(w->tabs));
    w->active_tab=(int)state->active_page;
    w->previous_tab=state->previous_page<0?0:tabs[state->previous_page].id;
    for(unsigned i=0;i<count;++i) {
        Pane *p=pane(w,mapped_id(map,count,state->panes[i].id));
        p->tab=membership[i]; p->synchronized=state->panes[i].synchronized;
        memcpy(p->title,state->panes[i].title,sizeof(p->title));
        if(renderers[i]) { bt_renderer_free(p->view.renderer); p->view.renderer=renderers[i]; }
        p->font_size=state->panes[i].font_size; p->view.force_draw=true;
    }
    free(tabs);
    w->resize_pane=0; w->capture=0; w->buttons=0; w->prefix=0; w->menu_depth=0;
    w->settings_open=false; w->rename_pane=0; w->choice_count=0; w->message[0]=0;
    w->dirty=true; w->closed=false;
    if(geometry(w)) return -1;
    return w->focused?focused(w,pane(w,bt_workspace_active(w)),true):0;
operational_failure:
    for(unsigned i=0;i<count;++i) bt_renderer_free(renderers[i]);
    free(tabs); return -1;
invalid:
    free(tabs); return fail(w,"Invalid workspace page topology");
}
const char *bt_workspace_page_title(BtWorkspace *w, uint64_t id) {
    Pane *p=pane(w,id);
    return p?w->tabs[p->tab].title:NULL;
}
static const char *pane_title(Pane *p) {
    if(*p->title) return p->title;
    const char *title=bt_session_title(&p->view.session);
    return title?title:"";
}
const char *bt_workspace_pane_title(BtWorkspace *w, uint64_t id) {
    Pane *p=pane(w,id); return p?pane_title(p):NULL;
}
int bt_workspace_pane_rename(BtWorkspace *w, uint64_t id, const char *text) {
    Pane *p=pane(w,id);
    if(!p) return fail(w,"Pane does not exist");
    if(!text || !printable_title(text)) return fail(w,"Pane title must be printable UTF-8, at most 255 bytes");
    memmove(p->title,text,strlen(text)+1);
    w->rename_pane=0; w->dirty=true; return 0;
}
int bt_workspace_pane_copy_title(BtWorkspace *w, uint64_t id) {
    const char *title=bt_workspace_pane_title(w,id);
    if(!title) return fail(w,"Pane does not exist");
    return SDL_SetClipboardText(title)?fail(w,SDL_GetError()):0;
}
int bt_workspace_pane_clear(BtWorkspace *w, uint64_t id) {
    Pane *p=pane(w,id);
    if(!p) return fail(w,"Pane does not exist");
    if(release_keys(w) || (bt_window_release_pointer(&p->view) && input_failure(w,p))) return -1;
    if(bt_session_reset(&p->view.session)) return fail(w,p->view.session.error);
    p->view.force_draw=true; w->dirty=true; return 0;
}
int bt_workspace_rename(BtWorkspace *w, uint64_t id, const char *text) {
    Pane *p=pane(w,id);
    if(!p) return fail(w,"Pane does not exist");
    if(!text || !printable_title(text)) return fail(w,"Page title must be printable UTF-8, at most 255 bytes");
    memmove(w->tabs[p->tab].title,text,strlen(text)+1);
    w->rename_pane=0; w->dirty=true; return 0;
}
static int rename_prompt(BtWorkspace *w, uint64_t id, bool page) {
    if(!w->chrome) return fail(w,"Rename prompt requires workspace chrome");
    if(!pane(w,id)) return fail(w,"Pane does not exist");
    if(resize_finish(w,true) || release_keys(w)) return -1;
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i)
        if(w->panes[i].id && bt_window_release_pointer(&w->panes[i].view) && input_failure(w,&w->panes[i])) return -1;
    w->menu_depth=0; w->settings_open=false;
    w->capture=0; w->buttons=0; w->choice_count=0; w->prefix=0;
    snprintf(w->rename_text,sizeof(w->rename_text),"%s",page?bt_workspace_page_title(w,id):bt_workspace_pane_title(w,id));
    w->rename_pane=id; w->rename_page=page; w->dirty=true; return 0;
}
int bt_workspace_rename_prompt(BtWorkspace *w, uint64_t id) { return rename_prompt(w,id,true); }
int bt_workspace_pane_rename_prompt(BtWorkspace *w, uint64_t id) { return rename_prompt(w,id,false); }
static int rename_draw(BtWorkspace *w) {
    if(!w->rename_pane) return 0;
    BtRect screen=area(w);
    int width=screen.width>640?640:screen.width;
    BtRect r={(screen.width-width)/2,screen.height/2-w->bar_height,width,w->bar_height};
    if(label(w,r,w->rename_page?"Page name: Enter saves, Esc cancels, Ctrl+U clears":
             "Pane title: Enter saves, Esc cancels, Ctrl+U clears",true)) return -1;
    r.y+=w->bar_height;
    size_t length=strlen(w->rename_text);
    int cells=(r.width-16)/(int)w->label_session.cell_width-2;
    size_t room=cells>0?(size_t)cells:1;
    size_t start=length>room?length-room:0;
    while(start<length && ((unsigned char)w->rename_text[start]&0xc0)==0x80) ++start;
    char text[260]; snprintf(text,sizeof(text),"%s%s_",start?"<":"",w->rename_text+start);
    return label(w,r,text,false);
}
static int rename_event(BtWorkspace *w, const SDL_Event *e) {
    if(!w->rename_pane) return 0;
    if(e->type==SDL_WINDOWEVENT && e->window.event==SDL_WINDOWEVENT_FOCUS_LOST) {
        w->rename_pane=0; w->dirty=true; return 0;
    }
    if(e->type==SDL_KEYDOWN) {
        SDL_Scancode scan=e->key.keysym.scancode;
        if(scan>=0 && scan<SDL_NUM_SCANCODES) w->consumed[scan]=true;
        w->suppress_text=true; w->dirty=true;
        SDL_Keycode key=e->key.keysym.sym;
        if(key==SDLK_ESCAPE) w->rename_pane=0;
        else if(key==SDLK_RETURN || key==SDLK_KP_ENTER)
            return (w->rename_page?bt_workspace_rename(w,w->rename_pane,w->rename_text):
                    bt_workspace_pane_rename(w,w->rename_pane,w->rename_text))?-1:1;
        else if(key==SDLK_BACKSPACE) {
            size_t n=strlen(w->rename_text);
            if(n) { --n; while(n && ((unsigned char)w->rename_text[n]&0xc0)==0x80) --n; w->rename_text[n]=0; }
        } else if(key==SDLK_u && (e->key.keysym.mod&KMOD_CTRL) && !(e->key.keysym.mod&KMOD_MODE)) w->rename_text[0]=0;
        else w->suppress_text=false;
        return 1;
    }
    if(e->type==SDL_KEYUP) {
        SDL_Scancode scan=e->key.keysym.scancode;
        if(scan>=0 && scan<SDL_NUM_SCANCODES) w->consumed[scan]=false;
        return 1;
    }
    if(e->type==SDL_TEXTINPUT) {
        size_t n=strlen(w->rename_text),more=strlen(e->text.text);
        if(!w->suppress_text && n+more<sizeof(w->rename_text) && printable_title(e->text.text)) {
            memcpy(w->rename_text+n,e->text.text,more+1); w->dirty=true;
        }
        return 1;
    }
    if(editing_event(e)) return 1;
    if(e->type==SDL_MOUSEBUTTONDOWN) {
        if(e->button.button>=1 && e->button.button<=32) w->chrome_buttons|=1u<<(e->button.button-1);
        w->rename_pane=0; w->dirty=true; return 1;
    }
    return e->type==SDL_MOUSEBUTTONUP || e->type==SDL_MOUSEMOTION || e->type==SDL_MOUSEWHEEL;
}
static const char *choice_title(BtWorkspace *w, uint64_t id) {
    Pane *p=pane(w,id);
    if(!p) return "";
    if(w->choose_pages && *w->tabs[p->tab].title) return w->tabs[p->tab].title;
    const char *title=pane_title(p);
    return title && *title?title:"Terminal";
}
static unsigned char folded(unsigned char c) { return c>='A' && c<='Z'?c+('a'-'A'):c; }
static bool query_match(const char *text, const char *query) {
    if(!*query) return true;
    for(;*text;++text) {
        const unsigned char *a=(const unsigned char *)text,*b=(const unsigned char *)query;
        while(*a && *b && folded(*a)==folded(*b)) { ++a; ++b; }
        if(!*b) return true;
    }
    return false;
}
static void chooser_filter(BtWorkspace *w, bool reset) {
    uint64_t selected=w->match_count?w->choices[w->matches[w->choice_index]]:0;
    w->match_count=0;
    for(unsigned i=0;i<w->choice_count;++i) {
        Pane *selected=pane(w,w->choices[i]);
        if(!selected || (w->choose_all && ((w->choice_scope==1 && selected->tab!=w->active_tab) ||
             (w->choice_scope==2 && selected->tab==w->active_tab)))) continue;
        char id[32]; snprintf(id,sizeof(id),"%llu",(unsigned long long)w->choices[i]);
        const char *page=w->tabs[selected->tab].title;
        if(query_match(choice_title(w,w->choices[i]),w->choice_query) || query_match(id,w->choice_query) ||
           (w->choose_all && *page && query_match(page,w->choice_query)))
            w->matches[w->match_count++]=i;
    }
    w->choice_index=0;
    if(!reset) for(unsigned i=0;i<w->match_count;++i)
        if(w->choices[w->matches[i]]==selected) w->choice_index=i;
    if(reset) w->choice_top=0;
}
static void chooser_preview_clear(BtWorkspace *w) {
    w->preview_pane=0; w->preview_bytes=0; w->preview_revision=0; w->preview_refresh=0;
}
/* Draw only the highlighted pane, at most every 100 ms during output floods.
 * Its retained presentation is rendered locally; no owner RPC is needed. */
static void chooser_preview_update(BtWorkspace *w) {
    if(!w->choose_all || !w->choice_count || !w->match_count) {
        if(w->preview_pane) chooser_preview_clear(w);
        return;
    }
    uint64_t id=w->choices[w->matches[w->choice_index]];
    if(w->preview_pane!=id) {
        chooser_preview_clear(w);
        w->preview_pane=id; w->preview_bytes=UINT64_MAX; w->preview_revision=UINT64_MAX;
        w->dirty=true;
    }
    Pane *p=pane(w,id);
    if(!p) return;
    BtSession *s=&p->view.session;
    uint64_t now=bt_millis();
    uint64_t revision=s->presentation?s->presentation->revision:0;
    if(w->preview_bytes==s->bytes_read && w->preview_revision==revision &&
       !bt_renderer_due(p->view.renderer)) return;
    if(w->preview_refresh && now-w->preview_refresh<100) return;
    w->preview_refresh=now; w->preview_bytes=s->bytes_read; w->preview_revision=revision;
    w->dirty=true;
}
static int choose(BtWorkspace *w, bool pages, bool all) {
    if(!w->chrome) return fail(w,"Chooser requires workspace chrome");
    if(resize_finish(w,true) || release_keys(w)) return -1;
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i)
        if(w->panes[i].id && bt_window_release_pointer(&w->panes[i].view) && input_failure(w,&w->panes[i])) return -1;
    w->capture=0; w->buttons=0; w->prefix=0;
    w->menu_depth=0; w->settings_open=false; w->rename_pane=0;
    chooser_preview_clear(w);
    w->choice_count=0; w->choice_index=0; w->choice_top=0;
    w->choice_confirm=0; w->choice_send_pane=0; w->choice_send_text[0]=0; w->choice_notice[0]=0;
    w->choose_pages=pages; w->choose_all=all; w->choice_scope=0;
    w->choice_query[0]=0; w->choice_search=false; w->match_count=0;
    if(pages) {
        for(unsigned i=0;i<BT_WORKSPACE_TABS;++i) if(w->tabs[i].id)
            w->choices[w->choice_count++]=w->tabs[i].active;
    } else if(all) {
        for(unsigned tab=0;tab<BT_WORKSPACE_TABS;++tab) if(w->tabs[tab].id)
            for(unsigned i=0;i<BT_LAYOUT_PANES;++i)
                if(w->panes[i].id && w->panes[i].tab==(int)tab)
                    w->choices[w->choice_count++]=w->panes[i].id;
    } else {
        for(unsigned i=0;i<BT_LAYOUT_PANES;++i)
            if(w->panes[i].id && w->panes[i].tab==w->active_tab)
                w->choices[w->choice_count++]=w->panes[i].id;
    }
    chooser_filter(w,true);
    for(unsigned i=0;i<w->match_count;++i)
        if(w->choices[w->matches[i]]==bt_workspace_active(w)) w->choice_index=i;
    w->dirty=true; return 0;
}
int bt_workspace_choose(BtWorkspace *w, bool pages) { return choose(w,pages,false); }
int bt_workspace_pane_center(BtWorkspace *w) { return choose(w,false,true); }
static bool chooser_details(BtWorkspace *w) {
    return w->choose_all && area(w).height>=5*w->bar_height;
}
static BtRect chooser_rect(BtWorkspace *w, unsigned *rows) {
    BtRect r=area(w);
    int room=r.height/w->bar_height-(chooser_details(w)?5:2);
    *rows=room>0?(unsigned)room:1;
    unsigned available=w->match_count?w->match_count:1;
    if(w->choose_all && available<8) available=8;
    if(*rows>available) *rows=available;
    if(w->choice_index<w->choice_top) w->choice_top=w->choice_index;
    if(w->choice_index>=w->choice_top+*rows) w->choice_top=w->choice_index-*rows+1;
    int maximum=w->choose_all?1100:640;
    int width=r.width>maximum?maximum:r.width;
    int height=((int)*rows+1+(chooser_details(w)?3:0))*w->bar_height;
    return (BtRect){(r.width-width)/2,r.height>height?(r.height-height)/2:0,width,height};
}
bool bt_workspace_center_visible(BtWorkspace *w, uint64_t ids[BT_LAYOUT_PANES], unsigned *count) {
    *count=0;
    if(!w->choose_all || !w->choice_count) return false;
    chooser_filter(w,false);
    unsigned rows; (void)chooser_rect(w,&rows);
    for(unsigned n=0;n<rows && w->choice_top+n<w->match_count;++n)
        ids[(*count)++]=w->choices[w->matches[w->choice_top+n]];
    return true;
}
static bool valid_activity(const char *activity) {
    static const char *const values[]={"shell","running","remote","agent","working","idle",
                                       "waiting","exited","disconnected","unknown"};
    for(unsigned i=0;i<sizeof(values)/sizeof(*values);++i)
        if(!strcmp(activity,values[i])) return true;
    return false;
}
int bt_workspace_telemetry(BtWorkspace *w, uint64_t id, const char *activity,
                           const char *process, const char *agent, const char *task) {
    Pane *p=pane(w,id);
    if(!p) return fail(w,"Pane does not exist");
    if(!activity || !process || !agent || !task || !valid_activity(activity) ||
       strlen(process)>=sizeof(p->telemetry.process) ||
       strlen(agent)>=sizeof(p->telemetry.agent) ||
       strlen(task)>=sizeof(p->telemetry.task) ||
       !printable_title(process) || !printable_title(agent) || !printable_title(task))
        return fail(w,"Invalid pane telemetry");
    BtPaneTelemetry next={0};
    snprintf(next.activity,sizeof(next.activity),"%s",activity);
    snprintf(next.process,sizeof(next.process),"%s",process);
    snprintf(next.agent,sizeof(next.agent),"%s",agent);
    snprintf(next.task,sizeof(next.task),"%s",task);
    if(memcmp(&next,&p->telemetry,sizeof(next))) w->dirty=true;
    p->telemetry=next; p->telemetry_updated=bt_millis();
    return 0;
}
bool bt_workspace_telemetry_get(BtWorkspace *w, uint64_t id, BtPaneTelemetry *out) {
    Pane *p=pane(w,id);
    memset(out,0,sizeof(*out));
    if(!p || !p->telemetry_updated || bt_millis()-p->telemetry_updated>=3500) return false;
    *out=p->telemetry;
    return true;
}
static int chooser_preview_draw(BtWorkspace *w, BtRect box, unsigned rows) {
    if(!w->choose_all || !w->match_count || !chooser_details(w) || box.width<640 || rows<2) return 0;
    int left=box.width*46/100;
    BtRect row={box.x+left,box.y+w->bar_height,box.width-left,w->bar_height};
    char title[80];
    snprintf(title,sizeof(title),"Live view — pane %llu",
             (unsigned long long)w->choices[w->matches[w->choice_index]]);
    if(label(w,row,title,true)) return -1;
    for(unsigned n=1;n<rows;++n) {
        row.y+=w->bar_height;
        if(label(w,row,"",false)) return -1;
    }
    Pane *p=pane(w,w->choices[w->matches[w->choice_index]]);
    if(!p) return 0;
    BtSession *s=&p->view.session;
    const BtPresentation *frame=s->presentation;
    uint64_t natural_width=(uint64_t)(frame?frame->cols:s->cols)*(frame?frame->cell_width:s->cell_width)+16;
    uint64_t natural_height=(uint64_t)(frame?frame->rows:s->rows)*(frame?frame->cell_height:s->cell_height)+16;
    int available_width=box.width-left-8,available_height=(int)(rows-1)*w->bar_height-8;
    if(!natural_width || !natural_height || available_width<1 || available_height<1) return 0;
    int width=available_width;
    int height=(int)((uint64_t)width*natural_height/natural_width);
    if(height>available_height) {
        height=available_height;
        width=(int)((uint64_t)height*natural_width/natural_height);
    }
    if(width<1 || height<1) return 0;
    SDL_Rect preview={box.x+left+(box.width-left-width)/2,
        box.y+2*w->bar_height+((int)(rows-1)*w->bar_height-height)/2,width,height};
    if(bt_renderer_draw_region_scaled(p->view.renderer,s,preview,false))
        return fail(w,bt_renderer_error(p->view.renderer));
    return 0;
}
static int chooser_draw(BtWorkspace *w) {
    if(!w->choice_count) return 0;
    chooser_filter(w,false);
    chooser_preview_update(w);
    unsigned rows; BtRect r=chooser_rect(w,&rows),box=r;
    r.height=w->bar_height;
    char heading[192];
    if(w->choose_all) {
        const char *scopes[]={"all","this page","other pages"};
        if(w->choice_confirm)
            snprintf(heading,sizeof(heading),"Close pane %llu? Y confirms; any other key cancels",
                     (unsigned long long)w->choice_confirm);
        else if(w->choice_send_pane) {
            if(chooser_details(w))
                snprintf(heading,sizeof(heading),"Send to pane %llu: Enter submits, Esc cancels",
                         (unsigned long long)w->choice_send_pane);
            else {
                size_t length=strlen(w->choice_send_text),start=length>90?length-90:0;
                while(start<length && ((unsigned char)w->choice_send_text[start]&0xc0)==0x80) ++start;
                snprintf(heading,sizeof(heading),"Send pane %llu: %s_",
                         (unsigned long long)w->choice_send_pane,w->choice_send_text+start);
            }
        }
        else if(w->choice_notice[0]) snprintf(heading,sizeof(heading),"Pane Center: %s",w->choice_notice);
        else if(w->choice_search) snprintf(heading,sizeof(heading),"Pane Center [%s] / %s_",scopes[w->choice_scope],w->choice_query);
        else snprintf(heading,sizeof(heading),"Pane Center [%s]: Tab scope, / filter, a actions, F2 page, s send, x close, Enter",scopes[w->choice_scope]);
    } else if(w->choice_search) snprintf(heading,sizeof(heading),"%s / %s_",w->choose_pages?"Pages":"Panes",w->choice_query);
    else snprintf(heading,sizeof(heading),"%s: type to filter, Enter, Esc",w->choose_pages?"Pages":"Panes");
    if(label(w,r,heading,true)) return -1;
    if(!w->match_count) { r.y+=w->bar_height; return label(w,r,"No matches — Backspace or Ctrl+U to edit",false); }
    unsigned listed=w->match_count>w->choice_top?w->match_count-w->choice_top:0;
    if(listed>rows) listed=rows;
    for(unsigned n=0;n<rows;++n) {
        r.y+=w->bar_height;
        if(n>=listed) {
            BtRect blank=r;
            if(w->choose_all && chooser_details(w) && box.width>=640 && rows>=2) blank.width=box.width*46/100;
            if(label(w,blank,"",false)) return -1;
            continue;
        }
        unsigned i=w->choice_top+n;
        uint64_t id=w->choices[w->matches[i]];
        const char *title=choice_title(w,id);
        char text[256];
        if(w->choose_all) {
            Pane *selected=pane(w,id);
            BtPaneTelemetry telemetry;
            (void)bt_workspace_telemetry_get(w,id,&telemetry);
            unsigned page=0;
            for(int tab=0;tab<=selected->tab;++tab) page+=w->tabs[tab].id!=0;
            const char *page_title=w->tabs[selected->tab].title;
            char page_label[49]; size_t page_length=strlen(page_title);
            if(page_length>=sizeof(page_label)) {
                page_length=sizeof(page_label)-1;
                while(page_length && ((unsigned char)page_title[page_length]&0xc0)==0x80) --page_length;
            }
            memcpy(page_label,page_title,page_length); page_label[page_length]=0;
            snprintf(text,sizeof(text),"%u  %s%s%sPage %u%s%s > %s  [pane %llu]",i+1,
                     *telemetry.activity?"[":"",telemetry.activity,*telemetry.activity?"]  ":"",
                     page,*page_label?" ":"",page_label,title && *title?title:"Terminal",(unsigned long long)id);
        } else snprintf(text,sizeof(text),"%u  %s  [pane %llu]",i+1,title && *title?title:"Terminal",(unsigned long long)id);
        BtRect row=r;
        if(w->choose_all && chooser_details(w) && box.width>=640 && rows>=2) row.width=box.width*46/100;
        if(label(w,row,text,i==w->choice_index)) return -1;
    }
    if(chooser_preview_draw(w,box,rows)) return -1;
    if(chooser_details(w)) {
        Pane *selected=pane(w,w->choices[w->matches[w->choice_index]]);
        BtSession *session=&selected->view.session;
        const char *states[]={"unknown","off","recording","finishing","complete","failed"};
        unsigned status=bt_session_recording(session);
        BtPaneTelemetry telemetry;
        (void)bt_workspace_telemetry_get(w,selected->id,&telemetry);
        char details[256];
        snprintf(details,sizeof(details),"Pane %llu   PID %ld   %s%s%s%s%s   recording %s%s",
                 (unsigned long long)selected->id,(long)session->child,
                 *telemetry.process?telemetry.process:"process unknown",
                 *telemetry.activity?" ":"",telemetry.activity,
                 *telemetry.agent?"  ":"",telemetry.agent,
                 states[status<=BT_RECORD_FAILED?status:BT_RECORD_UNKNOWN],
                 selected->disconnected?"   disconnected":"");
        r.y+=w->bar_height;
        if(label(w,r,details,false)) return -1;
        snprintf(details,sizeof(details),"Task: %.160s",*telemetry.task?telemetry.task:"unavailable");
        r.y+=w->bar_height;
        if(label(w,r,details,false)) return -1;
        if(w->choice_send_pane) {
            size_t length=strlen(w->choice_send_text),start=length>190?length-190:0;
            while(start<length && ((unsigned char)w->choice_send_text[start]&0xc0)==0x80) ++start;
            snprintf(details,sizeof(details),"Message: %s%s_",start?"<":"",w->choice_send_text+start);
        } else {
            char cwd[PATH_MAX];
            char proc[64]; snprintf(proc,sizeof(proc),"/proc/%ld/cwd",(long)session->child);
            ssize_t n=!selected->disconnected && !session->exited && session->child>0?
                      readlink(proc,cwd,sizeof(cwd)-1):-1;
            if(n>=0) {
                size_t shown=(size_t)n>240?240:(size_t)n;
                while(shown && ((unsigned char)cwd[shown]&0xc0)==0x80) --shown;
                cwd[shown]=0;
                if(!printable_title(cwd)) n=-1;
            }
            snprintf(details,sizeof(details),"Directory: %.240s",n>=0?cwd:"unavailable");
        }
        r.y+=w->bar_height;
        if(label(w,r,details,false)) return -1;
    }
    return 0;
}
static int chooser_accept(BtWorkspace *w) {
    if(!w->match_count) return 0;
    uint64_t id=w->choices[w->matches[w->choice_index]];
    w->choice_count=0; w->choice_confirm=0; w->choice_send_pane=0;
    chooser_preview_clear(w); w->dirty=true;
    return pane(w,id)?bt_workspace_focus(w,id):0;
}
static int chooser_modal_event(BtWorkspace *w, const SDL_Event *e) {
    if(!w->choice_confirm && !w->choice_send_pane) return 0;
    if(e->type==SDL_KEYDOWN) {
        SDL_Scancode scan=e->key.keysym.scancode;
        if(scan>=0 && scan<SDL_NUM_SCANCODES) w->consumed[scan]=true;
        w->suppress_text=true; w->dirty=true;
        SDL_Keycode key=e->key.keysym.sym;
        if(w->choice_confirm) {
            if(!e->key.repeat) {
                uint64_t id=w->choice_confirm; w->choice_confirm=0;
                if(key==SDLK_y && pane(w,id)) {
                    chooser_preview_clear(w);
                    if(bt_workspace_close(w,id)) {
                        snprintf(w->message,sizeof(w->message),"Could not close pane %llu: %.170s",
                                 (unsigned long long)id,w->error);
                        w->dirty=true; return 1;
                    }
                    return w->closed?1:bt_workspace_pane_center(w)?-1:1;
                }
            }
            return 1;
        }
        if(key==SDLK_ESCAPE) {
            w->choice_send_pane=0; w->choice_send_text[0]=0;
        } else if(key==SDLK_RETURN || key==SDLK_KP_ENTER) {
            uint64_t id=w->choice_send_pane;
            Pane *target=pane(w,id);
            size_t n=strlen(w->choice_send_text);
            char bytes[sizeof(w->choice_send_text)];
            memcpy(bytes,w->choice_send_text,n); bytes[n++]='\r';
            if(!target) snprintf(w->choice_notice,sizeof(w->choice_notice),"Pane %llu no longer exists",
                                 (unsigned long long)id);
            else if(bt_session_send(&target->view.session,bytes,n))
                snprintf(w->choice_notice,sizeof(w->choice_notice),"Send to pane %llu refused: %.70s",
                         (unsigned long long)id,target->view.session.error);
            else snprintf(w->choice_notice,sizeof(w->choice_notice),"Queued %zu bytes for pane %llu",
                          n,(unsigned long long)id);
            w->choice_send_pane=0; w->choice_send_text[0]=0;
        } else if(key==SDLK_BACKSPACE) {
            size_t n=strlen(w->choice_send_text);
            if(n) { --n; while(n && ((unsigned char)w->choice_send_text[n]&0xc0)==0x80) --n; w->choice_send_text[n]=0; }
        } else if(key==SDLK_u && (e->key.keysym.mod&KMOD_CTRL) && !(e->key.keysym.mod&KMOD_MODE)) w->choice_send_text[0]=0;
        else w->suppress_text=false;
        return 1;
    }
    if(e->type==SDL_KEYUP) {
        SDL_Scancode scan=e->key.keysym.scancode;
        if(scan>=0 && scan<SDL_NUM_SCANCODES) w->consumed[scan]=false;
        return 1;
    }
    if(e->type==SDL_TEXTINPUT) {
        if(w->choice_send_pane && !w->suppress_text) {
            size_t n=strlen(w->choice_send_text),more=strlen(e->text.text);
            if(n+more<sizeof(w->choice_send_text)-1 && printable_title(e->text.text)) {
                memcpy(w->choice_send_text+n,e->text.text,more+1); w->dirty=true;
            }
        }
        return 1;
    }
    if(e->type==SDL_MOUSEBUTTONDOWN) {
        w->choice_confirm=0; w->choice_send_pane=0; w->choice_send_text[0]=0; w->dirty=true;
        return 1;
    }
    return editing_event(e) || e->type==SDL_MOUSEBUTTONUP ||
           e->type==SDL_MOUSEMOTION || e->type==SDL_MOUSEWHEEL;
}
static int pane_menu(BtWorkspace *w, uint64_t id, int mouse_x, int mouse_y);
static int chooser_event(BtWorkspace *w, const SDL_Event *e) {
    if(!w->choice_count) return 0;
    chooser_filter(w,false);
    if(e->type==SDL_WINDOWEVENT && e->window.event==SDL_WINDOWEVENT_FOCUS_LOST) {
        w->choice_count=0; w->choice_confirm=0; w->choice_send_pane=0;
        chooser_preview_clear(w); w->dirty=true; return 0;
    }
    int modal=chooser_modal_event(w,e);
    if(modal) return modal;
    if(e->type==SDL_KEYDOWN) {
        SDL_Scancode scan=e->key.keysym.scancode;
        if(scan>=0 && scan<SDL_NUM_SCANCODES) w->consumed[scan]=true;
        w->suppress_text=true; w->dirty=true;
        w->choice_notice[0]=0;
        switch(e->key.keysym.sym) {
            case SDLK_ESCAPE: w->choice_count=0; chooser_preview_clear(w); break;
            case SDLK_RETURN: case SDLK_KP_ENTER: return chooser_accept(w)?-1:1;
            case SDLK_UP: if(w->match_count) w->choice_index=(w->choice_index+w->match_count-1)%w->match_count; break;
            case SDLK_DOWN: if(w->match_count) w->choice_index=(w->choice_index+1)%w->match_count; break;
            case SDLK_HOME: w->choice_index=0; break;
            case SDLK_END: w->choice_index=w->match_count?w->match_count-1:0; break;
            case SDLK_TAB:
                if(w->choose_all) {
                    w->choice_scope=(w->choice_scope+1)%3;
                    chooser_filter(w,true);
                }
                break;
            case SDLK_F2:
                if(w->choose_all && !w->choice_search && w->match_count) {
                    uint64_t id=w->choices[w->matches[w->choice_index]];
                    w->choice_count=0; chooser_preview_clear(w);
                    return bt_workspace_rename_prompt(w,id)?-1:1;
                }
                break;
            case SDLK_a:
                if(w->choose_all && !w->choice_search && w->match_count) {
                    uint64_t id=w->choices[w->matches[w->choice_index]];
                    chooser_preview_clear(w);
                    int lw,lh; SDL_GetWindowSize(bt_surface_window(w->surface),&lw,&lh);
                    return pane_menu(w,id,lw/2,lh/2)?-1:1;
                }
                w->suppress_text=false; break;
            case SDLK_x:
                if(w->choose_all && !w->choice_search && w->match_count) {
                    w->choice_confirm=w->choices[w->matches[w->choice_index]];
                    return 1;
                }
                w->suppress_text=false; break;
            case SDLK_s:
                if(w->choose_all && !w->choice_search && w->match_count) {
                    w->choice_send_pane=w->choices[w->matches[w->choice_index]];
                    w->choice_send_text[0]=0; w->choice_notice[0]=0;
                    return 1;
                }
                w->suppress_text=false; break;
            case SDLK_BACKSPACE: {
                size_t n=strlen(w->choice_query);
                if(n) { --n; while(n && ((unsigned char)w->choice_query[n]&0xc0)==0x80) --n; w->choice_query[n]=0; }
                chooser_filter(w,true); break;
            }
            default:
                if(e->key.keysym.sym==SDLK_u && (e->key.keysym.mod&KMOD_CTRL) && !(e->key.keysym.mod&KMOD_MODE)) {
                    w->choice_query[0]=0; chooser_filter(w,true); break;
                }
                if(e->key.keysym.sym==SDLK_SLASH && !w->choice_search) { w->choice_search=true; break; }
                if(!w->choice_search && e->key.keysym.sym>=SDLK_1 && e->key.keysym.sym<=SDLK_9 &&
                   (unsigned)(e->key.keysym.sym-SDLK_1)<w->match_count) {
                    w->choice_index=e->key.keysym.sym-SDLK_1; return chooser_accept(w)?-1:1;
                }
                w->suppress_text=false;
        }
        return 1;
    }
    if(e->type==SDL_KEYUP) {
        SDL_Scancode scan=e->key.keysym.scancode;
        if(scan>=0 && scan<SDL_NUM_SCANCODES) w->consumed[scan]=false;
        return 1;
    }
    if(e->type==SDL_TEXTINPUT) {
        size_t n=strlen(w->choice_query),more=strlen(e->text.text);
        if(!w->suppress_text && n+more<sizeof(w->choice_query) && printable_title(e->text.text)) {
            memcpy(w->choice_query+n,e->text.text,more+1); w->choice_search=true;
            chooser_filter(w,true); w->dirty=true;
        }
        return 1;
    }
    if(editing_event(e)) return 1;
    if(e->type==SDL_MOUSEWHEEL) {
        if(e->wheel.y>0 && w->choice_index) --w->choice_index;
        if(e->wheel.y<0 && w->choice_index+1<w->match_count) ++w->choice_index;
        w->dirty=true; return 1;
    }
    if(e->type==SDL_MOUSEBUTTONDOWN) {
        if(e->button.button>=1 && e->button.button<=32) w->chrome_buttons|=1u<<(e->button.button-1);
        int lw,lh; SDL_GetWindowSize(bt_surface_window(w->surface),&lw,&lh);
        BtRect screen=area(w); unsigned rows; BtRect r=chooser_rect(w,&rows);
        int x=(int)((int64_t)e->button.x*screen.width/(lw?lw:1));
        int y=(int)((int64_t)e->button.y*screen.height/(lh?lh:1));
        if(x<r.x || x>=r.x+r.width || y<r.y || y>=r.y+r.height) {
            w->choice_count=0; chooser_preview_clear(w); w->dirty=true;
        } else if(w->match_count && e->button.button==SDL_BUTTON_LEFT && y>=r.y+w->bar_height &&
                  y<r.y+((int)(w->match_count-w->choice_top<rows?w->match_count-w->choice_top:rows)+1)*w->bar_height) {
            w->choice_index=w->choice_top+(unsigned)((y-r.y)/w->bar_height-1);
            return chooser_accept(w)?-1:1;
        } else if(w->choose_all && w->match_count && e->button.button==SDL_BUTTON_RIGHT &&
                  y>=r.y+w->bar_height &&
                  y<r.y+((int)(w->match_count-w->choice_top<rows?w->match_count-w->choice_top:rows)+1)*w->bar_height) {
            w->choice_index=w->choice_top+(unsigned)((y-r.y)/w->bar_height-1);
            uint64_t id=w->choices[w->matches[w->choice_index]];
            chooser_preview_clear(w);
            return pane_menu(w,id,e->button.x,e->button.y)?-1:1;
        }
        return 1;
    }
    if(e->type==SDL_MOUSEBUTTONUP) {
        if(e->button.button>=1 && e->button.button<=32) w->chrome_buttons&=~(1u<<(e->button.button-1));
        return 1;
    }
    return e->type==SDL_MOUSEMOTION;
}
int bt_workspace_settings(BtWorkspace *w, bool edge_locked) {
    if(!w->chrome) return fail(w,"Settings require workspace chrome");
    if(resize_finish(w,true) || release_keys(w)) return -1;
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i)
        if(w->panes[i].id && bt_window_release_pointer(&w->panes[i].view) && input_failure(w,&w->panes[i])) return -1;
    w->capture=0; w->buttons=0; w->choice_count=0; w->rename_pane=0; w->prefix=0;
    w->menu_depth=0; w->settings_result=0; w->settings_open=true; w->settings_edge_locked=edge_locked;
    w->settings_index=0; w->settings_top=0; w->dirty=true; return 0;
}
int bt_workspace_settings_result(BtWorkspace *w, unsigned result) {
    if(result>3) return fail(w,"Invalid settings result");
    w->settings_result=result; w->dirty=true; return 0;
}
static const char *const recording_choices[5][7]={
    {"off","on"}, {"2M","8M","32M","128M"}, {"elide","keep"},
    {"1G","5G","10G","20G","50G","100G"},
    {"off","1G","5G","10G","20G","50G","100G"}};
static const unsigned recording_choice_count[5]={2,4,2,6,7};
static const char *const recording_keys[5]={
    "transcript","transcript_size","transcript_graphics","transcript_total","transcript_archive_total"};
int bt_workspace_settings_recording(BtWorkspace *w, const char *enabled,
                                    const char *size, const char *graphics,
                                    const char *recent, const char *archive) {
    const char *values[5]={enabled,size,graphics,recent,archive};
    unsigned selected[5];
    for(unsigned i=0;i<5;++i) {
        if(!values[i]) return fail(w,"Invalid recording setting");
        unsigned n=0;
        while(n<recording_choice_count[i] && strcmp(values[i],recording_choices[i][n])) ++n;
        if(n==recording_choice_count[i]) return fail(w,"Invalid recording setting");
        selected[i]=n;
    }
    memcpy(w->settings_recording,selected,sizeof(selected)); w->dirty=true;
    return 0;
}
int bt_workspace_message(BtWorkspace *w, const char *text) {
    if(!text || !printable_title(text)) return fail(w,"Message must be printable UTF-8 up to 255 bytes");
    if(*text) {
        if(bt_workspace_settings(w,false)) return -1;
        w->settings_open=false;
    }
    w->confirm_pane=0;
    snprintf(w->message,sizeof(w->message),"%s",text); w->dirty=true; return 0;
}
const char *bt_workspace_message_text(BtWorkspace *w) { return w->message; }
int bt_workspace_confirm(BtWorkspace *w, uint64_t id, const char *action, const char *text) {
    if(!pane(w,id)) return fail(w,"Confirmation pane does not exist");
    if(!action || !*action || strlen(action)>=sizeof(w->confirm_action))
        return fail(w,"Invalid confirmation action");
    for(const char *p=action;*p;++p)
        if(!((*p>='a' && *p<='z') || (*p>='0' && *p<='9') || *p=='-' || *p=='_'))
            return fail(w,"Invalid confirmation action");
    if(!text || !*text || bt_workspace_message(w,text)) return fail(w,"Invalid confirmation message");
    w->confirm_pane=id;
    snprintf(w->confirm_action,sizeof(w->confirm_action),"%s",action);
    return 0;
}
static int message_draw(BtWorkspace *w) {
    if(!w->message[0]) return 0;
    BtRect screen=area(w);
    int width=screen.width>720?720:screen.width;
    int height=2*w->bar_height;
    BtRect r={(screen.width-width)/2,screen.height>height?(screen.height-height)/2:0,width,w->bar_height};
    if(label(w,r,w->message,true)) return -1;
    r.y+=w->bar_height;
    return label(w,r,w->confirm_pane?"Y confirms; N / Esc / click cancels":"Enter / Esc / click to dismiss",false);
}
static int message_event(BtWorkspace *w, const SDL_Event *e) {
    if(!w->message[0]) return 0;
    if(e->type==SDL_KEYDOWN) {
        SDL_Scancode scan=e->key.keysym.scancode;
        if(scan>=0 && scan<SDL_NUM_SCANCODES) w->consumed[scan]=true;
        w->suppress_text=true;
        if(!e->key.repeat && w->confirm_pane && e->key.keysym.sym==SDLK_y) {
            if(pane(w,w->confirm_pane) && enqueue(w,w->confirm_action,w->confirm_pane)) return -1;
            w->confirm_pane=0; w->message[0]=0; w->dirty=true;
        } else if(!e->key.repeat && (e->key.keysym.sym==SDLK_ESCAPE || e->key.keysym.sym==SDLK_RETURN ||
                   e->key.keysym.sym==SDLK_KP_ENTER ||
                   (w->confirm_pane && e->key.keysym.sym==SDLK_n))) {
            w->confirm_pane=0; w->message[0]=0; w->dirty=true;
        }
        return 1;
    }
    if(e->type==SDL_KEYUP) {
        SDL_Scancode scan=e->key.keysym.scancode;
        if(scan>=0 && scan<SDL_NUM_SCANCODES) w->consumed[scan]=false;
        return 1;
    }
    if(e->type==SDL_MOUSEBUTTONDOWN) {
        if(e->button.button>=1 && e->button.button<=32) w->chrome_buttons|=1u<<(e->button.button-1);
        w->confirm_pane=0; w->message[0]=0; w->dirty=true; return 1;
    }
    if(e->type==SDL_MOUSEBUTTONUP) {
        if(e->button.button>=1 && e->button.button<=32) w->chrome_buttons&=~(1u<<(e->button.button-1));
        return 1;
    }
    return e->type==SDL_TEXTINPUT || editing_event(e) ||
           e->type==SDL_MOUSEMOTION || e->type==SDL_MOUSEWHEEL;
}
static BtRect settings_rect(BtWorkspace *w, unsigned *rows) {
    BtRect screen=area(w);
    int available=screen.height/w->bar_height-2;
    *rows=available<1?1:available>10?10:(unsigned)available;
    if(w->settings_index<w->settings_top) w->settings_top=w->settings_index;
    if(w->settings_index>=w->settings_top+*rows) w->settings_top=w->settings_index-*rows+1;
    int width=screen.width>640?640:screen.width;
    int height=((int)*rows+1)*w->bar_height;
    return (BtRect){(screen.width-width)/2,screen.height>height?(screen.height-height)/2:0,width,height};
}
static int settings_draw(BtWorkspace *w) {
    if(!w->settings_open) return 0;
    const char *names[]={"Synchronize input","Decrease font","Increase font","Split left","Split up","Split down","Split right","Maximize","Close pane"};
    const char *recording_names[]={"Recording (new windows)","Transcript size","Transcript graphics",
                                   "Recent retention","Archive retention"};
    unsigned rows; BtRect r=settings_rect(w,&rows); r.height=w->bar_height;
    const char *status[]={"Settings: Enter toggles, Esc closes", "Settings saved — Esc closes",
        "Not saved — check settings file access and retry", "Saved; reload failed — close and press Ctrl+Shift+F5"};
    if(label(w,r,status[w->settings_result],true)) return -1;
    for(unsigned i=0;i<rows;++i) {
        unsigned item=w->settings_top+i;
        char text[160];
        if(!item) snprintf(text,sizeof(text),"Page strip: %s%s",w->bottom_bar?"bottom":"top",w->settings_edge_locked?" (configuration override)":"");
        else if(item<10) snprintf(text,sizeof(text),"[%c] %s button",w->pane_buttons&(1u<<(item-1))?'x':' ',names[item-1]);
        else {
            unsigned setting=item-10;
            snprintf(text,sizeof(text),"%s: %s",recording_names[setting],
                     recording_choices[setting][w->settings_recording[setting]]);
        }
        r.y+=w->bar_height;
        if(label(w,r,text,item==w->settings_index)) return -1;
    }
    return 0;
}
static int settings_toggle(BtWorkspace *w) {
    char action[48];
    if(!w->settings_index) {
        if(w->settings_edge_locked) return 0;
        snprintf(action,sizeof(action),"setting-edge-%s",w->bottom_bar?"top":"bottom");
    } else if(w->settings_index<10) {
        unsigned bit=w->settings_index-1;
        snprintf(action,sizeof(action),"setting-button-%u-%s",bit,w->pane_buttons&(1u<<bit)?"off":"on");
    } else {
        unsigned setting=w->settings_index-10;
        unsigned next=(w->settings_recording[setting]+1)%recording_choice_count[setting];
        int n=snprintf(action,sizeof(action),"setting-%s-%s",recording_keys[setting],recording_choices[setting][next]);
        if(n<0 || (size_t)n>=sizeof(action)) return fail(w,"Recording setting action is too long");
    }
    return enqueue(w,action,bt_workspace_active(w));
}
static int settings_event(BtWorkspace *w, const SDL_Event *e) {
    if(!w->settings_open) return 0;
    if(e->type==SDL_WINDOWEVENT && e->window.event==SDL_WINDOWEVENT_FOCUS_LOST) {
        w->menu_depth=0; w->settings_open=false; w->dirty=true; return 0;
    }
    if(e->type==SDL_KEYDOWN) {
        SDL_Scancode scan=e->key.keysym.scancode;
        if(scan>=0 && scan<SDL_NUM_SCANCODES) w->consumed[scan]=true;
        w->suppress_text=true; w->dirty=true;
        switch(e->key.keysym.sym) {
            case SDLK_ESCAPE: w->menu_depth=0; w->settings_open=false; break;
            case SDLK_UP: w->settings_index=(w->settings_index+14)%15; break;
            case SDLK_DOWN: w->settings_index=(w->settings_index+1)%15; break;
            case SDLK_HOME: w->settings_index=0; break;
            case SDLK_END: w->settings_index=14; break;
            case SDLK_RETURN: case SDLK_KP_ENTER: case SDLK_SPACE:
                if(!e->key.repeat && settings_toggle(w)) return -1;
                break;
        }
        return 1;
    }
    if(e->type==SDL_KEYUP) {
        SDL_Scancode scan=e->key.keysym.scancode;
        if(scan>=0 && scan<SDL_NUM_SCANCODES) w->consumed[scan]=false;
        return 1;
    }
    if(e->type==SDL_TEXTINPUT || editing_event(e)) return 1;
    if(e->type==SDL_MOUSEWHEEL) {
        if(e->wheel.y>0 && w->settings_index) --w->settings_index;
        if(e->wheel.y<0 && w->settings_index<14) ++w->settings_index;
        w->dirty=true; return 1;
    }
    if(e->type==SDL_MOUSEBUTTONDOWN) {
        if(e->button.button>=1 && e->button.button<=32) w->chrome_buttons|=1u<<(e->button.button-1);
        int lw,lh; SDL_GetWindowSize(bt_surface_window(w->surface),&lw,&lh);
        BtRect screen=area(w); unsigned rows; BtRect r=settings_rect(w,&rows);
        int x=(int)((int64_t)e->button.x*screen.width/(lw?lw:1));
        int y=(int)((int64_t)e->button.y*screen.height/(lh?lh:1));
        if(x<r.x || x>=r.x+r.width || y<r.y || y>=r.y+r.height) { w->menu_depth=0; w->settings_open=false; }
        else if(e->button.button==SDL_BUTTON_LEFT && y>=r.y+w->bar_height) {
            w->settings_index=w->settings_top+(unsigned)((y-r.y)/w->bar_height-1);
            if(settings_toggle(w)) return -1;
        }
        w->dirty=true; return 1;
    }
    if(e->type==SDL_MOUSEBUTTONUP) {
        if(e->button.button>=1 && e->button.button<=32) w->chrome_buttons&=~(1u<<(e->button.button-1));
        return 1;
    }
    return e->type==SDL_MOUSEMOTION;
}
static const MenuEntry start_entries[]={
    {"New page", "new-page",0,SDLK_n}, {"Pages",NULL,1,SDLK_p},
    {"Panes",NULL,2,SDLK_a}, {"Settings","settings",0,SDLK_s},
    {"Reload settings","reload-settings",0,SDLK_r}, {"Applications",NULL,5,SDLK_g},
    {"Stop voice","stop-voice",0,SDLK_v}};
static const MenuEntry page_entries[]={
    {"New page","new-page",0,SDLK_n}, {"Choose page","choose-pages",0,SDLK_c},
    {"Rename page","rename-page",0,SDLK_r}, {"Next page","next-page",0,SDLK_f},
    {"Previous page","previous-page",0,SDLK_b}, {"Close page","close-page",0,SDLK_x}};
static const MenuEntry pane_entries[]={
    {"Split",NULL,3,SDLK_s}, {"Focus",NULL,4,SDLK_f}, {"Choose pane","choose-panes",0,SDLK_c},
    {"Cycle layout","next-layout",0,SDLK_l}, {"Resize","resize-mode",0,SDLK_r},
    {"Maximize / restore","zoom",0,SDLK_z}, {"Synchronize input","sync",0,SDLK_y},
    {"Read aloud","read-aloud",0,SDLK_a}, {"Dictate","dictate-pane",0,SDLK_d},
    {"Close pane","close",0,SDLK_x}};
static const MenuEntry split_entries[]={
    {"Left","split-left",0,SDLK_l}, {"Up","split-up",0,SDLK_u},
    {"Down","split-down",0,SDLK_d}, {"Right","split-right",0,SDLK_r}};
static const MenuEntry focus_entries[]={
    {"Left","focus-left",0,SDLK_l}, {"Up","focus-up",0,SDLK_u},
    {"Down","focus-down",0,SDLK_d}, {"Right","focus-right",0,SDLK_r},
    {"Next pane","next-pane",0,SDLK_o}, {"Last pane","last-pane",0,SDLK_b}};
static const MenuEntry title_entries[]={
    {"Rename title","pane-rename",0,SDLK_r}, {"Copy title","copy-title",0,SDLK_c},
    {"Reset title","reset-title",0,SDLK_e}, {"Clear terminal","clear-pane",0,SDLK_l},
    {"Split right","split-right",0,SDLK_s}, {"Split down","split-down",0,SDLK_d},
    {"Read aloud","read-aloud",0,SDLK_a}, {"Dictate","dictate-pane",0,SDLK_i},
    {"Close pane","close",0,SDLK_o}};
static const struct { const char *name; const MenuEntry *items; unsigned count; } menus[]={
    {"Start",start_entries,7},{"Pages",page_entries,6},{"Panes",pane_entries,10},
    {"Split",split_entries,4},{"Focus",focus_entries,6},
    {"Applications",NULL,0},{"Pane actions",title_entries,9}};
static const MenuEntry empty_apps[]={ {"No installed applications",NULL,0,0} };
static unsigned menu_count(BtWorkspace *w, unsigned kind) {
    return kind==5?(w->app_count?w->app_count:1):menus[kind].count;
}
static const MenuEntry *menu_items(BtWorkspace *w, unsigned kind) {
    return kind==5?(w->app_count?w->app_items:empty_apps):menus[kind].items;
}
int bt_workspace_app_menu(BtWorkspace *w, const char *name) {
    if(!name) { w->app_count=0; w->menu_depth=0; w->dirty=true; return 0; }
    if(w->app_count==256 || !*name || !printable_title(name)) return fail(w,"Invalid application menu label or menu full");
    unsigned i=w->app_count++;
    strcpy(w->app_labels[i],name);
    snprintf(w->app_actions[i],sizeof(w->app_actions[i]),"application-%u",i);
    w->app_items[i]=(MenuEntry){w->app_labels[i],w->app_actions[i],0,0};
    w->menu_depth=0; w->dirty=true; return 0;
}
int bt_workspace_start_badge(BtWorkspace *w, bool enabled) {
    w->start_badge=enabled; w->dirty=true; return 0;
}
int bt_workspace_menu(BtWorkspace *w) {
    if(w->menu_depth) { w->menu_depth=0; w->dirty=true; return 0; }
    if(!w->chrome) return fail(w,"Start menu requires workspace chrome");
    if(resize_finish(w,true) || release_keys(w)) return -1;
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i)
        if(w->panes[i].id && bt_window_release_pointer(&w->panes[i].view) && input_failure(w,&w->panes[i])) return -1;
    w->capture=0; w->buttons=0; w->choice_count=0; w->rename_pane=0;
    w->menu_depth=0; w->settings_open=false; w->prefix=0;
    w->menu_depth=1; w->menu_kind[0]=w->menu_index[0]=w->menu_top[0]=0;
    w->dirty=true; return 0;
}
static int pane_menu(BtWorkspace *w, uint64_t id, int mouse_x, int mouse_y) {
    if(!pane(w,id)) return fail(w,"Pane does not exist");
    if(resize_finish(w,true) || release_keys(w)) return -1;
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i)
        if(w->panes[i].id && bt_window_release_pointer(&w->panes[i].view) && input_failure(w,&w->panes[i])) return -1;
    int lw,lh; SDL_GetWindowSize(bt_surface_window(w->surface),&lw,&lh);
    BtRect screen=area(w);
    w->menu_x=(int)((int64_t)mouse_x*screen.width/(lw?lw:1));
    w->menu_y=(int)((int64_t)mouse_y*screen.height/(lh?lh:1));
    w->menu_pane=id; w->menu_depth=1;
    w->menu_kind[0]=6; w->menu_index[0]=w->menu_top[0]=0;
    w->settings_open=false; w->rename_pane=0; w->choice_count=0;
    w->capture=0; w->buttons=0; w->prefix=0; w->dirty=true;
    return 0;
}
static void menu_rects(BtWorkspace *w, BtRect rects[4], unsigned rows[4]) {
    BtRect screen=area(w); int width=screen.width>240?240:screen.width;
    int room=screen.height/w->bar_height-2; if(room<1) room=1;
    for(unsigned d=0;d<w->menu_depth;++d) {
        unsigned count=menu_count(w,w->menu_kind[d]);
        rows[d]=count<(unsigned)room?count:(unsigned)room;
        if(w->menu_index[d]<w->menu_top[d]) w->menu_top[d]=w->menu_index[d];
        if(w->menu_index[d]>=w->menu_top[d]+rows[d]) w->menu_top[d]=w->menu_index[d]-rows[d]+1;
        int height=((int)rows[d]+1)*w->bar_height;
        int x=d?rects[d-1].x+width:w->menu_kind[0]==6?w->menu_x:0;
        if(x+width>screen.width) x=d?rects[d-1].x-width:w->menu_kind[0]==6?screen.width-width:0;
        if(x<0) x=0;
        int y=d?rects[d-1].y+(int)(w->menu_index[d-1]-w->menu_top[d-1]+1)*w->bar_height:
            w->menu_kind[0]==6?w->menu_y:w->bottom_bar?screen.height-w->bar_height-height:w->bar_height;
        if(y+height>screen.height) y=screen.height-height;
        if(y<0) y=0;
        rects[d]=(BtRect){x,y,width,height};
    }
}
static int menu_draw(BtWorkspace *w) {
    if(!w->menu_depth) return 0;
    BtRect rects[4]; unsigned rows[4]; menu_rects(w,rects,rows);
    for(unsigned d=0;d<w->menu_depth;++d) {
        unsigned kind=w->menu_kind[d]; BtRect r=rects[d]; r.height=w->bar_height;
        if(label(w,r,kind==5?"Applications":menus[kind].name,true)) return -1;
        for(unsigned row=0;row<rows[d];++row) {
            unsigned index=w->menu_top[d]+row; const MenuEntry *item=&menu_items(w,kind)[index];
            char text[272]; snprintf(text,sizeof(text),"%c  %s%s",item->key?(char)item->key:' ',item->label,item->child?" >":"");
            r.y+=w->bar_height;
            if(label(w,r,text,index==w->menu_index[d])) return -1;
        }
    }
    return 0;
}
static int menu_activate(BtWorkspace *w, unsigned d, bool open_only) {
    unsigned kind=w->menu_kind[d];
    const MenuEntry *item=&menu_items(w,kind)[w->menu_index[d]];
    w->menu_depth=d+1; w->dirty=true;
    if(item->child) {
        w->menu_kind[d+1]=item->child; w->menu_index[d+1]=w->menu_top[d+1]=0; ++w->menu_depth;
        return 0;
    }
    if(open_only || !item->action) return 0;
    uint64_t id=kind==6?w->menu_pane:bt_workspace_active(w);
    w->menu_depth=0;
    if(kind==6) {
        if(!pane(w,id)) return fail(w,"Pane menu target closed");
        if(!strcmp(item->action,"pane-rename")) return bt_workspace_pane_rename_prompt(w,id);
        if(!strcmp(item->action,"copy-title")) return bt_workspace_pane_copy_title(w,id);
        if(!strcmp(item->action,"reset-title")) return bt_workspace_pane_rename(w,id,"");
        if(!strcmp(item->action,"clear-pane")) return bt_workspace_pane_clear(w,id);
    }
    return enqueue(w,item->action,id);
}
static int menu_event(BtWorkspace *w, const SDL_Event *e) {
    if(!w->menu_depth) return 0;
    if(e->type==SDL_WINDOWEVENT && e->window.event==SDL_WINDOWEVENT_FOCUS_LOST) {
        w->menu_depth=0; w->dirty=true; return 0;
    }
    unsigned d=w->menu_depth-1;
    if(e->type==SDL_KEYDOWN) {
        SDL_Scancode scan=e->key.keysym.scancode;
        if(scan>=0 && scan<SDL_NUM_SCANCODES) w->consumed[scan]=true;
        w->suppress_text=true; w->dirty=true;
        SDL_Keycode key=e->key.keysym.sym; unsigned count=menu_count(w,w->menu_kind[d]);
        if(key==SDLK_ESCAPE || (key==SDLK_m && modifiers(e->key.keysym.mod)==3)) w->menu_depth=0;
        else if(key==SDLK_LEFT) { if(d) --w->menu_depth; }
        else if(key==SDLK_UP) w->menu_index[d]=(w->menu_index[d]+count-1)%count;
        else if(key==SDLK_DOWN) w->menu_index[d]=(w->menu_index[d]+1)%count;
        else if(key==SDLK_HOME) w->menu_index[d]=0;
        else if(key==SDLK_END) w->menu_index[d]=count-1;
        else if(!e->key.repeat) {
            if(key==SDLK_RETURN || key==SDLK_SPACE || key==SDLK_RIGHT) {
                if(menu_activate(w,d,key==SDLK_RIGHT)) return -1;
            } else for(unsigned i=0;i<count;++i) if(menu_items(w,w->menu_kind[d])[i].key && menu_items(w,w->menu_kind[d])[i].key==key) {
                w->menu_index[d]=i; if(menu_activate(w,d,false)) return -1; break;
            }
        }
        return 1;
    }
    if(e->type==SDL_KEYUP) {
        SDL_Scancode scan=e->key.keysym.scancode;
        if(scan>=0 && scan<SDL_NUM_SCANCODES) w->consumed[scan]=false;
        return 1;
    }
    if(e->type==SDL_TEXTINPUT || editing_event(e)) return 1;
    if(e->type==SDL_MOUSEWHEEL) {
        unsigned count=menu_count(w,w->menu_kind[d]);
        if(e->wheel.y>0 && w->menu_index[d]) --w->menu_index[d];
        if(e->wheel.y<0 && w->menu_index[d]+1<count) ++w->menu_index[d];
        w->dirty=true; return 1;
    }
    if(e->type==SDL_MOUSEBUTTONUP) {
        if(e->button.button>=1 && e->button.button<=32) w->chrome_buttons&=~(1u<<(e->button.button-1));
        return 1;
    }
    if(e->type==SDL_MOUSEMOTION || e->type==SDL_MOUSEBUTTONDOWN) {
        bool click=e->type==SDL_MOUSEBUTTONDOWN;
        if(click && e->button.button>=1 && e->button.button<=32) w->chrome_buttons|=1u<<(e->button.button-1);
        int lw,lh; SDL_GetWindowSize(bt_surface_window(w->surface),&lw,&lh);
        BtRect screen=area(w),rects[4]; unsigned rows[4]; menu_rects(w,rects,rows);
        int x=click?e->button.x:e->motion.x,y=click?e->button.y:e->motion.y;
        x=(int)((int64_t)x*screen.width/(lw?lw:1)); y=(int)((int64_t)y*screen.height/(lh?lh:1));
        for(int depth=(int)w->menu_depth-1;depth>=0;--depth) {
            BtRect r=rects[depth];
            if(x<r.x || x>=r.x+r.width || y<r.y || y>=r.y+r.height) continue;
            if(y>=r.y+w->bar_height) {
                unsigned index=w->menu_top[depth]+(unsigned)((y-r.y)/w->bar_height-1);
                if(click || index!=w->menu_index[depth]) {
                    w->menu_index[depth]=index;
                    if(menu_activate(w,(unsigned)depth,!click || e->button.button!=SDL_BUTTON_LEFT)) return -1;
                }
            }
            return 1;
        }
        if(click) { w->menu_depth=0; w->dirty=true; }
        return 1;
    }
    return 0;
}
static int control(BtWorkspace *w, BtRect r, const char *text, const char *action,
                   uint64_t id, bool selected, bool paint) {
    if(r.width<1 || r.height<1) return 0;
    if(w->hit_count==CHROME_HITS) return fail(w,"Chrome control limit reached");
    w->hits[w->hit_count++]=(ChromeHit){r,id,action};
    return paint?label(w,r,text,selected):0;
}
static int chrome(BtWorkspace *w, bool paint) {
    w->hit_count=0;
    if(!w->chrome) return 0;
    BtRect screen=area(w);
    int h=w->bar_height,bw=w->button_width,tabs=0;
    for(int i=0;i<BT_WORKSPACE_TABS;++i) if(w->tabs[i].id) ++tabs;
    int bar_y=w->bottom_bar?screen.height-h:0;
    int start_width=w->start_badge?2*bw:0;
    int tabw=tabs?(screen.width-bw-start_width)/tabs:0;
    if(tabw>200) tabw=200;
    if(paint && label(w,(BtRect){0,bar_y,screen.width,h},"",false)) return -1;
    int x=start_width,number=0;
    if(w->start_badge && control(w,(BtRect){0,bar_y,start_width,h},"Start","start-menu",bt_workspace_active(w),false,paint)) return -1;
    for(int i=0;i<BT_WORKSPACE_TABS;++i) if(w->tabs[i].id) {
        Pane *p=pane(w,w->tabs[i].active);
        const char *title=p?pane_title(p):NULL;
        if(*w->tabs[i].title) title=w->tabs[i].title;
        char caption[120]; snprintf(caption,sizeof(caption),"%d  %.105s",++number,w->resize_pane && i==w->active_tab?"Resize: arrows, Enter/Esc":w->prefix && i==w->active_tab?"Leader":title && *title?title:"Page");
        if(control(w,(BtRect){x,bar_y,tabw,h},caption,"focus",w->tabs[i].active,i==w->active_tab,paint)) return -1;
        x+=tabw;
    }
    if(control(w,(BtRect){x,bar_y,bw,h},"+","new-page",bt_workspace_active(w),false,paint)) return -1;
    const char *names[]={"sync","font-smaller","font-larger","split-left","split-up","split-down","split-right","zoom","close"};
    const char *labels[]={"S","−","+","←","↑","↓","→","□","×"};
    unsigned buttons=pane_button_count(w);
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i) {
        Pane *p=&w->panes[i]; if(!visible(w,p)) continue;
        BtRect r=p->bounds; r.height=h;
        int button=r.width/(int)(buttons+1); if(button>bw) button=bw;
        int title_width=r.width-(int)buttons*button;
        r.width=title_width;
        const char *title=pane_title(p);
        char disconnected[320];
        if(bt_remote_disconnected(&p->view.session)) {
            snprintf(disconnected,sizeof(disconnected),"Disconnected: %s",bt_remote_failure(&p->view.session));
            title=disconnected;
        }
        const char *directions[]={"left","right","up","down"};
        char drop_label[64];
        if(w->drag_active && w->drag_target==p->id) {
            snprintf(drop_label,sizeof(drop_label),"Drop pane %s",directions[w->drag_direction]);
            title=drop_label;
        }
        char fallback[48]; snprintf(fallback,sizeof(fallback),"Pane %llu",(unsigned long long)p->id);
        if(control(w,r,title && *title?title:fallback,"pane-title",p->id,
                   bt_workspace_active(w)==p->id || w->drag_target==p->id,paint)) return -1;
        unsigned position=0;
        for(unsigned j=0;j<9;++j) {
            if(!(w->pane_buttons&(1u<<j))) continue;
            r=(BtRect){p->bounds.x+title_width+(int)position++*button,p->bounds.y,button,h};
            if(control(w,r,labels[j],names[j],p->id,j==0?p->synchronized:bt_workspace_active(w)==p->id,paint)) return -1;
        }
    }
    return 0;
}
static bool drag_edge(BtWorkspace *w, int mouse_x, int mouse_y, uint64_t *target, BtDirection *direction) {
    Pane *p=hit(w,mouse_x,mouse_y);
    if(!p || p->id==w->drag_pane) return false;
    int lw,lh; SDL_GetWindowSize(bt_surface_window(w->surface),&lw,&lh);
    BtRect screen=area(w),r=p->bounds;
    int x=(int)((int64_t)mouse_x*screen.width/(lw?lw:1))-r.x;
    int y=(int)((int64_t)mouse_y*screen.height/(lh?lh:1))-r.y;
    int64_t horizontal=(int64_t)(2*x-r.width)*r.height;
    int64_t vertical=(int64_t)(2*y-r.height)*r.width;
    *direction=llabs(horizontal)>=llabs(vertical)?
        (horizontal<0?BT_LEFT:BT_RIGHT):(vertical<0?BT_UP:BT_DOWN);
    *target=p->id;
    return true;
}
static int drag_page_hover(BtWorkspace *w, int mouse_x, int mouse_y) {
    if(chrome(w,false)) return -1;
    int lw,lh; SDL_GetWindowSize(bt_surface_window(w->surface),&lw,&lh);
    BtRect screen=area(w);
    int x=(int)((int64_t)mouse_x*screen.width/(lw?lw:1));
    int y=(int)((int64_t)mouse_y*screen.height/(lh?lh:1));
    for(unsigned i=0;i<w->hit_count;++i) {
        ChromeHit *h=&w->hits[i]; BtRect r=h->rect;
        if(strcmp(h->action,"focus") || x<r.x || y<r.y || x>=r.x+r.width || y>=r.y+r.height) continue;
        if(h->pane!=bt_workspace_active(w)) return bt_workspace_focus(w,h->pane);
        break;
    }
    return 0;
}
static int chrome_event(BtWorkspace *w, const SDL_Event *e) {
    if(!w->chrome) return 0;
    if(e->type==SDL_WINDOWEVENT && e->window.event==SDL_WINDOWEVENT_FOCUS_LOST) {
        w->chrome_buttons=0; w->drag_pane=w->drag_target=0; w->drag_active=false;
    }
    if(e->type==SDL_MOUSEMOTION && w->drag_pane) {
        if(llabs((long long)e->motion.x-w->drag_x)>=8 ||
           llabs((long long)e->motion.y-w->drag_y)>=8) w->drag_active=true;
        uint64_t target=0; BtDirection direction=BT_LEFT;
        if(w->drag_active) {
            if(drag_page_hover(w,e->motion.x,e->motion.y)) return -1;
            (void)drag_edge(w,e->motion.x,e->motion.y,&target,&direction);
        }
        if(target!=w->drag_target || (target && direction!=w->drag_direction)) w->dirty=true;
        w->drag_target=target; w->drag_direction=direction;
        return 1;
    }
    if(e->type==SDL_MOUSEMOTION && w->chrome_buttons) return 1;
    bool button=e->type==SDL_MOUSEBUTTONDOWN || e->type==SDL_MOUSEBUTTONUP;
    if(!button && e->type!=SDL_MOUSEMOTION && e->type!=SDL_MOUSEWHEEL) return 0;
    if(button && (e->button.button<1 || e->button.button>32)) return 0;
    unsigned bit=button?1u<<(e->button.button-1):0;
    if(e->type==SDL_MOUSEBUTTONUP && e->button.button==SDL_BUTTON_LEFT && w->drag_pane) {
        uint64_t source=w->drag_pane,target=0; BtDirection direction=BT_LEFT;
        bool dragged=w->drag_active;
        bool dropped=dragged && drag_edge(w,e->button.x,e->button.y,&target,&direction);
        w->drag_pane=w->drag_target=0; w->drag_active=false;
        w->chrome_buttons&=~bit; w->dirty=true;
        if(!dragged && pane_menu(w,source,e->button.x,e->button.y)) return -1;
        if(dropped && bt_workspace_relocate(w,source,target,direction)) {
            snprintf(w->message,sizeof(w->message),"Could not move pane: %.210s",w->error);
            w->dirty=true;
        }
        return 1;
    }
    if(e->type==SDL_MOUSEBUTTONUP && (w->chrome_buttons&bit)) { w->chrome_buttons&=~bit; return 1; }
    if(w->capture) return 0; /* Keep terminal drags paired with their release. */
    if(chrome(w,false)) return -1;
    int lw,lh; SDL_GetWindowSize(bt_surface_window(w->surface),&lw,&lh);
    BtRect screen=area(w);
    int x,y;
    if(button) { x=e->button.x; y=e->button.y; }
    else if(e->type==SDL_MOUSEMOTION) { x=e->motion.x; y=e->motion.y; }
    else SDL_GetMouseState(&x,&y);
    x=(int)((int64_t)x*screen.width/(lw?lw:1));
    y=(int)((int64_t)y*screen.height/(lh?lh:1));
    for(unsigned i=0;i<w->hit_count;++i) {
        ChromeHit *hit=&w->hits[i]; BtRect r=hit->rect;
        if(x<r.x || y<r.y || x>=r.x+r.width || y>=r.y+r.height) continue;
        if(e->type==SDL_MOUSEBUTTONDOWN) {
            w->chrome_buttons|=bit;
            if(e->button.button==SDL_BUTTON_LEFT && !strcmp(hit->action,"pane-title")) {
                w->drag_pane=hit->pane; w->drag_target=0; w->drag_active=false;
                w->drag_x=e->button.x; w->drag_y=e->button.y;
                if(enqueue(w,"focus",hit->pane)) return -1;
            } else if(e->button.button==SDL_BUTTON_LEFT && enqueue(w,hit->action,hit->pane)) return -1;
        }
        return 1;
    }
    if(w->bottom_bar?y>=screen.height-w->bar_height:y<w->bar_height) {
        if(e->type==SDL_MOUSEBUTTONDOWN) w->chrome_buttons|=bit;
        return 1;
    }
    return 0;
}
static void surface_event(void *data, const SDL_Event *e) {
    BtWorkspace *w=data;
    if(bt_workspace_event(w,e)) w->failed=true;
}
BtWorkspace *bt_workspace_new(const char *title, int width, int height, const char *font,
                               int font_size, char *error, size_t capacity) {
    if(!title || !font || width<1 || height<1 || width>16384 || height>16384 || font_size<6 || font_size>96) {
        snprintf(error,capacity,"Invalid workspace geometry or font"); return NULL;
    }
    BtWorkspace *w=calloc(1,sizeof(*w));
    if(!w) { snprintf(error,capacity,"Could not allocate workspace"); return NULL; }
    w->id=identity(); w->pane_buttons=511; w->active_tab=-1; w->font_size=font_size; w->font=strdup(font);
    w->settings_recording[0]=1; w->settings_recording[1]=1;
    w->settings_recording[3]=1; w->settings_recording[4]=1;
    w->image_budget=WORKSPACE_IMAGE_BUDGET;
    if(!w->font) { snprintf(error,capacity,"Could not allocate workspace font"); free(w); return NULL; }
    w->surface=bt_surface_new(title,width,height,error,capacity);
    if(!w->surface) { free(w->font); free(w); return NULL; }
    bt_surface_handler(w->surface,surface_event,w); w->dirty=true;
    w->focused=(SDL_GetWindowFlags(bt_surface_window(w->surface))&SDL_WINDOW_INPUT_FOCUS)!=0;
    w->owner=getpid(); w->next=workspaces; workspaces=w;
    return w;
}
typedef struct { uint64_t id; BtRect rect; } FindRect;
static void find_rect(void *data, uint64_t id, BtRect r) {
    FindRect *f=data; if(id==f->id) f->rect=r;
}
int bt_workspace_add(BtWorkspace *w, uint64_t target, BtDirection direction, const BtPaneLaunch *launch, uint64_t *out) {
    *out=0;
    if(resize_finish(w,true)) return -1;
    Pane *p=NULL,*other=pane(w,target);
    if(target && !other) return fail(w,"Split target does not exist");
    if(!launch || (!launch->attach && (!launch->argv || !launch->argv[0] || !launch->helper)) ||
       ((launch->attach || launch->session_name) && (!launch->session_dir || !launch->session_name)) ||
       (launch->session_name && !launch->attach && !launch->service) ||
       ((launch->observe || launch->expected_epoch) && !launch->attach)) return fail(w,"Incomplete pane launch");
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i) if(!w->panes[i].id) { p=&w->panes[i]; break; }
    if(!p) return fail(w,"Workspace pane limit reached");
    int tab=other?other->tab:-1;
    if(!other) for(int i=0;i<BT_WORKSPACE_TABS;++i) if(!w->tabs[i].id) { tab=i; break; }
    if(tab<0) return fail(w,"Workspace tab limit reached");
    BtLayout layout;
    if(other) layout=w->tabs[tab].layout;
    else bt_layout_init(&layout);
    uint64_t id=identity();
    if(bt_layout_split(&layout,target,id,direction)) return fail(w,"Could not split pane layout");
    FindRect f={.id=id};
    if(bt_layout_place(&layout,content_area(w),17,17,find_rect,&f)) return fail(w,"Window is too small to split");
    memset(p,0,sizeof(*p));
    if(launch->session_name) {
        p->name=strdup(launch->session_name); p->session_dir=strdup(launch->session_dir);
        if(!p->name || !p->session_dir) {
            free(p->name); free(p->session_dir); memset(p,0,sizeof(*p));
            return fail(w,"Could not allocate session descriptor");
        }
    }
    SDL_Rect r={f.rect.x,f.rect.y,f.rect.width,f.rect.height};
    int rc;
    if(launch->attach) rc=bt_window_attach_view_epoch(&p->view,w->surface,r,launch->session_dir,
        launch->session_name,w->font,w->font_size,launch->observe,launch->expected_epoch);
    else if(launch->session_name) rc=bt_window_persistent_view(&p->view,w->surface,r,launch->service,
        launch->helper,launch->session_dir,launch->session_name,launch->argv,launch->env,w->font,w->font_size);
    else rc=bt_window_open_view(&p->view,w->surface,r,launch->helper,launch->argv,launch->env,w->font,w->font_size);
    if(rc) {
        fail(w,p->view.error); bt_window_close(&p->view); free(p->name); free(p->session_dir); memset(p,0,sizeof(*p)); return -1;
    }
    p->id=id; p->tab=tab; p->font_size=w->font_size; p->bounds=f.rect;
    if(!other) w->tabs[tab]=(Tab){.id=identity()};
    w->tabs[tab].layout=layout; w->tabs[tab].zoom=0;
    *out=id; w->closed=false;
    return bt_workspace_focus(w,id);
}
int bt_workspace_focus(BtWorkspace *w, uint64_t id) {
    Pane *p=pane(w,id),*old=pane(w,bt_workspace_active(w));
    if(!p) return fail(w,"Pane does not exist");
    if(old!=p) { w->menu_depth=0; w->settings_open=false; w->rename_pane=0; w->choice_count=0; w->dirty=true; }
    if(old!=p && resize_finish(w,true)) return -1;
    if(old!=p && w->focused && focused(w,old,false)) return -1;
    if(old!=p) {
        w->capture=0; w->buttons=0; w->prefix=0;
        if(old && old->tab!=p->tab) w->previous_tab=w->tabs[old->tab].id;
        if(w->tabs[p->tab].active && w->tabs[p->tab].active!=id)
            w->tabs[p->tab].previous=w->tabs[p->tab].active;
    }
    w->active_tab=p->tab; w->tabs[p->tab].active=id;
    if(w->tabs[p->tab].zoom) w->tabs[p->tab].zoom=id;
    if(geometry(w)) return -1;
    if(old!=p && w->focused && focused(w,p,true)) return -1;
    return 0;
}
void bt_workspace_raise(BtWorkspace *w) {
    SDL_Window *window=bt_surface_window(w->surface);
    if(window) SDL_RaiseWindow(window);
}
static void drain_detach(BtWorkspace *w, BtSession *s) {
    if(!s->remote) return;
    int rc=bt_remote_input_drain(s);
    if(rc==1) return;
    if(rc<0) ++w->pump_stats.failed_detaches;
    else ++w->pump_stats.completed_detaches;
    --w->pump_stats.pending_detaches;
    bt_session_close(s);
}
static void drain_detaches(BtWorkspace *w) {
    unsigned count=0;
    for(unsigned step=0;step<BT_LAYOUT_PANES;++step) {
        unsigned i=w->detach_next; w->detach_next=(i+1)%BT_LAYOUT_PANES;
        if(!w->detaching[i].remote) continue;
        drain_detach(w,&w->detaching[i]);
        if(++count==4) break;
    }
}
int bt_workspace_close(BtWorkspace *w, uint64_t id) {
    if(w->preview_pane==id) chooser_preview_clear(w);
    w->menu_depth=0; w->settings_open=false; w->rename_pane=0; w->choice_count=0; w->dirty=true;
    if(resize_finish(w,true)) return -1;
    Pane *p=pane(w,id);
    if(!p) return fail(w,"Pane does not exist");
    BtSession *deferred=NULL;
    if(p->view.session.remote && !bt_remote_observer(&p->view.session)) {
        for(unsigned i=0;i<BT_LAYOUT_PANES;++i) if(!w->detaching[i].remote) { deferred=&w->detaching[i]; break; }
        if(!deferred) return fail(w,"Pending detach limit reached");
    }
    Tab *t=&w->tabs[p->tab]; bool active=t->active==id,was_focused=bt_workspace_active(w)==id;
    if(was_focused && release_keys(w)) return -1;
    if(w->prefix_pane==id) w->prefix=0;
    if(bt_layout_remove(&t->layout,id)) return fail(w,"Pane is missing from its layout");
    if(w->capture==id) { w->capture=0; w->buttons=0; }
    if(t->zoom==id) t->zoom=0;
    if(active) t->active=0;
    if(deferred) {
        bt_window_close_deferred(&p->view,deferred);
        ++w->pump_stats.pending_detaches; drain_detach(w,deferred);
    } else bt_window_close(&p->view);
    free(p->name); free(p->session_dir); memset(p,0,sizeof(*p));
    if(!t->layout.count) {
        int tab=(int)(t-w->tabs); memset(t,0,sizeof(*t));
        if(w->active_tab==tab) {
            w->active_tab=-1;
            for(int i=0;i<BT_WORKSPACE_TABS;++i) if(w->tabs[i].id) { w->active_tab=i; break; }
        }
    } else if(active) {
        for(unsigned i=0;i<BT_LAYOUT_PANES;++i)
            if(w->panes[i].id && &w->tabs[w->panes[i].tab]==t) { t->active=w->panes[i].id; break; }
    }
    w->dirty=true;
    if(w->active_tab<0) { w->closed=true; return 0; }
    if(geometry(w)) return -1;
    return was_focused && w->focused?focused(w,pane(w,bt_workspace_active(w)),true):0;
}
int bt_workspace_neighbor(BtWorkspace *w, uint64_t id, BtDirection direction, uint64_t *out) {
    *out=0; Pane *p=pane(w,id);
    if(!p || direction<BT_LEFT || direction>BT_DOWN) return fail(w,"Invalid neighboring pane request");
    int64_t best=INT64_MAX;
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i) {
        Pane *q=&w->panes[i];
        if(!q->id || q==p || q->tab!=p->tab) continue;
        BtRect a=p->bounds,b=q->bounds;
        bool horizontal=direction==BT_LEFT || direction==BT_RIGHT;
        if(horizontal && (a.y+a.height<=b.y || b.y+b.height<=a.y)) continue;
        if(!horizontal && (a.x+a.width<=b.x || b.x+b.width<=a.x)) continue;
        if((direction==BT_LEFT && b.x+b.width>a.x) ||
           (direction==BT_RIGHT && b.x<a.x+a.width) ||
           (direction==BT_UP && b.y+b.height>a.y) ||
           (direction==BT_DOWN && b.y<a.y+a.height)) continue;
        int64_t dx=2*(int64_t)q->bounds.x+q->bounds.width-2*(int64_t)p->bounds.x-p->bounds.width;
        int64_t dy=2*(int64_t)q->bounds.y+q->bounds.height-2*(int64_t)p->bounds.y-p->bounds.height;
        if((direction==BT_LEFT && dx>=0) || (direction==BT_RIGHT && dx<=0) ||
           (direction==BT_UP && dy>=0) || (direction==BT_DOWN && dy<=0)) continue;
        int64_t distance=dx*dx+dy*dy;
        if(distance<best) { best=distance; *out=q->id; }
    }
    return *out?0:fail(w,"No pane in that direction");
}
int bt_workspace_move(BtWorkspace *w, uint64_t id, BtDirection direction) {
    if(resize_finish(w,true)) return -1;
    uint64_t other;
    if(bt_workspace_neighbor(w,id,direction,&other)) return -1;
    Pane *p=pane(w,id);
    if(bt_layout_swap(&w->tabs[p->tab].layout,id,other)) return fail(w,"Could not move pane");
    return geometry(w);
}
int bt_workspace_relocate(BtWorkspace *w, uint64_t id, uint64_t target, BtDirection direction) {
    Pane *moving=pane(w,id),*anchor=pane(w,target);
    if(!moving || !anchor || moving==anchor || direction<BT_LEFT || direction>BT_DOWN)
        return fail(w,"Invalid pane relocation");
    if(resize_finish(w,true)) return -1;
    int from=moving->tab,to=anchor->tab;
    Tab source=w->tabs[from],destination=w->tabs[to];
    if(bt_layout_remove(&source.layout,id)) return fail(w,"Could not remove relocating pane");
    if(from==to) {
        if(bt_layout_split(&source.layout,target,id,direction))
            return fail(w,"Could not place relocating pane");
        source.active=id; source.zoom=0;
    } else {
        if(bt_layout_split(&destination.layout,target,id,direction))
            return fail(w,"Destination page is full");
        if(source.layout.count) {
            uint64_t order[BT_LAYOUT_PANES];
            if(source.active==id) {
                (void)bt_layout_order(&source.layout,order);
                source.active=order[0];
            }
            if(source.previous==id) source.previous=0;
            if(source.zoom==id) source.zoom=0;
        }
        destination.previous=destination.active;
        destination.active=id; destination.zoom=0;
    }
    Pane *old=pane(w,bt_workspace_active(w));
    if(release_keys(w) || (w->focused && focused(w,old,false))) return -1;
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i)
        if(w->panes[i].id && bt_window_release_pointer(&w->panes[i].view) && input_failure(w,&w->panes[i])) return -1;
    w->capture=0; w->buttons=0; w->prefix=0; w->menu_depth=0;
    w->settings_open=false; w->rename_pane=0; w->choice_count=0;
    w->tabs[from]=source;
    if(from!=to) {
        if(!source.layout.count) {
            if(w->previous_tab==source.id) w->previous_tab=0;
            memset(&w->tabs[from],0,sizeof(w->tabs[from]));
        }
        else w->previous_tab=source.id;
        w->tabs[to]=destination;
        moving->tab=to;
    }
    w->active_tab=to;
    if(from==to) w->tabs[to].active=id;
    if(geometry(w)) return -1;
    return w->focused?focused(w,moving,true):0;
}
static int resize_finish(BtWorkspace *w, bool cancel) {
    if(!w->resize_pane) return 0;
    Pane *p=pane(w,w->resize_pane);
    w->resize_pane=0; w->dirty=true;
    if(cancel && p) *display_layout(&w->tabs[p->tab])=w->resize_saved;
    return geometry(w);
}
int bt_workspace_layout(BtWorkspace *w, uint64_t id, int mode) {
    Pane *p=pane(w,id);
    if(!p || mode<-1 || mode>3) return fail(w,"Invalid page layout");
    if(resize_finish(w,true) || release_keys(w)) return -1;
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i)
        if(w->panes[i].id && bt_window_release_pointer(&w->panes[i].view) && input_failure(w,&w->panes[i])) return -1;
    w->capture=0; w->buttons=0;
    w->menu_depth=0; w->settings_open=false; w->rename_pane=0; w->choice_count=0; w->prefix=0;
    Tab *t=&w->tabs[p->tab];
    t->mode=mode<0?(t->mode+1)%4:(unsigned)mode; t->zoom=0;
    return geometry(w);
}
int bt_workspace_reset_sizes(BtWorkspace *w, uint64_t id) {
    Pane *p=pane(w,id);
    if(!p) return fail(w,"Pane does not exist");
    if(resize_finish(w,true)) return -1;
    Tab *t=&w->tabs[p->tab];
    if(t->mode==1) return 0;
    BtLayout *layout=display_layout(t);
    if(t->mode>=2) bt_layout_arrange(layout,t->order,t->order_count,t->mode==3);
    else bt_layout_reset(layout);
    return geometry(w);
}
int bt_workspace_resize_mode(BtWorkspace *w, uint64_t id) {
    if(resize_finish(w,true) || bt_workspace_focus(w,id)) return -1;
    Pane *p=pane(w,id);
    Tab *t=&w->tabs[p->tab];
    if(t->layout.count<2 || t->zoom || t->mode==1) return fail(w,"Resize requires visible split panes");
    if(release_keys(w)) return -1;
    w->menu_depth=0; w->settings_open=false; w->rename_pane=0; w->choice_count=0;
    w->resize_saved=*display_layout(t); w->resize_pane=id; w->prefix=0; w->dirty=true;
    return 0;
}
static int resize_key(BtWorkspace *w, SDL_Keycode key) {
    if(key==SDLK_ESCAPE) return resize_finish(w,true);
    if(key==SDLK_RETURN || key==SDLK_KP_ENTER) return resize_finish(w,false);
    if(key!=SDLK_LEFT && key!=SDLK_RIGHT && key!=SDLK_UP && key!=SDLK_DOWN) return 0;
    Pane *p=pane(w,w->resize_pane);
    if(!p) return resize_finish(w,true);
    bool horizontal=key==SDLK_LEFT || key==SDLK_RIGHT;
    int delta=key==SDLK_RIGHT || key==SDLK_DOWN?250:-250;
    if(bt_layout_adjust(display_layout(&w->tabs[p->tab]),p->id,horizontal,delta)) return 0;
    return geometry(w);
}
int bt_workspace_resize(BtWorkspace *w, uint64_t id, bool horizontal, int delta) {
    if(resize_finish(w,true)) return -1;
    Pane *p=pane(w,id);
    if(!p) return fail(w,"Pane does not exist");
    if(w->tabs[p->tab].mode==1) return fail(w,"Stack has no visible split to resize");
    if(bt_layout_adjust(display_layout(&w->tabs[p->tab]),id,horizontal,delta)) return fail(w,"No split along that axis");
    return geometry(w);
}
int bt_workspace_zoom(BtWorkspace *w, uint64_t id) {
    if(resize_finish(w,true)) return -1;
    Pane *p=pane(w,id);
    if(!p) return fail(w,"Pane does not exist");
    if(bt_workspace_focus(w,id)) return -1;
    Tab *t=&w->tabs[p->tab]; t->zoom=t->zoom?0:id; return geometry(w);
}
int bt_workspace_synchronize(BtWorkspace *w, uint64_t id, bool enabled) {
    Pane *p=pane(w,id);
    if(!p) return fail(w,"Pane does not exist");
    if(p->synchronized!=enabled && release_keys(w)) return -1;
    p->synchronized=enabled; w->dirty=true; return 0;
}
int bt_workspace_cycle_tab(BtWorkspace *w, int delta) {
    if(w->active_tab<0 || !delta) return 0;
    for(int i=1;i<=BT_WORKSPACE_TABS;++i) {
        int tab=(w->active_tab+(delta>0?i:-i)+BT_WORKSPACE_TABS)%BT_WORKSPACE_TABS;
        if(w->tabs[tab].id) return bt_workspace_focus(w,w->tabs[tab].active);
    }
    return 0;
}
int bt_workspace_navigate(BtWorkspace *w, BtNavigation operation, unsigned number) {
    if(resize_finish(w,true)) return -1;
    Pane *active=pane(w,bt_workspace_active(w));
    if(!active) return fail(w,"No active pane");
    if(operation==BT_PAGE_NUMBER || operation==BT_LAST_PAGE) {
        unsigned count=0;
        for(unsigned i=0;i<BT_WORKSPACE_TABS;++i) if(w->tabs[i].id) {
            ++count;
            if((operation==BT_PAGE_NUMBER && count==number) ||
               (operation==BT_LAST_PAGE && w->tabs[i].id==w->previous_tab))
                return bt_workspace_focus(w,w->tabs[i].active);
        }
        return fail(w,"Page is unavailable");
    }
    if(operation==BT_LAST_PANE) {
        Pane *previous=pane(w,w->tabs[active->tab].previous);
        if(!previous || previous->tab!=active->tab) return fail(w,"Previous pane is unavailable");
        return bt_workspace_focus(w,previous->id);
    }
    if(operation!=BT_NEXT_PANE && operation!=BT_PREVIOUS_PANE && operation!=BT_SWAP_NEXT && operation!=BT_SWAP_PREVIOUS)
        return fail(w,"Invalid navigation operation");
    int index=(int)(active-w->panes);
    int direction=operation==BT_PREVIOUS_PANE || operation==BT_SWAP_PREVIOUS?-1:1;
    for(int i=1;i<=BT_LAYOUT_PANES;++i) {
        Pane *next=&w->panes[(index+direction*i+BT_LAYOUT_PANES)%BT_LAYOUT_PANES];
        if(!next->id || next->tab!=active->tab) continue;
        if(next==active) return 0;
        if(operation==BT_SWAP_NEXT || operation==BT_SWAP_PREVIOUS) {
            if(bt_layout_swap(&w->tabs[active->tab].layout,active->id,next->id)) return fail(w,"Could not swap panes");
            return geometry(w);
        }
        return bt_workspace_focus(w,next->id);
    }
    return 0;
}
int bt_workspace_close_tab(BtWorkspace *w, uint64_t id) {
    Pane *p=pane(w,id);
    if(!p) return fail(w,"Pane does not exist");
    int tab=p->tab;
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i)
        if(w->panes[i].id && w->panes[i].tab==tab && bt_workspace_close(w,w->panes[i].id)) return -1;
    return 0;
}
void bt_workspace_visit(BtWorkspace *w, BtPaneVisit visit, void *data) {
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i) {
        Pane *p=&w->panes[i]; if(!p->id) continue;
        unsigned page_index=0;
        for(int tab=0;tab<=p->tab;++tab) page_index+=w->tabs[tab].id!=0;
        BtPaneInfo info={.id=p->id,.tab=w->tabs[p->tab].id,.window=w->id,
            .active=bt_workspace_active(w)==p->id,.tab_active=w->tabs[p->tab].active==p->id,
            .visible=visible(w,p),.synchronized=p->synchronized,
            .disconnected=bt_remote_disconnected(&p->view.session),.connection_error=bt_remote_failure(&p->view.session),
            .bounds=p->bounds,.session_name=p->name,.session_dir=p->session_dir,.session_epoch=bt_remote_epoch(&p->view.session),.page_title=w->tabs[p->tab].title,.title=pane_title(p),.page_index=page_index,.layout_mode=w->tabs[p->tab].mode,.view=&p->view};
        visit(data,&info);
    }
}
void bt_workspace_filter(BtWorkspace *w, BtWorkspaceFilter filter, void *data) {
    w->filter=filter; w->filter_data=data;
}
static Pane *hit(BtWorkspace *w, int x, int y) {
    int lw,lh; SDL_GetWindowSize(bt_surface_window(w->surface),&lw,&lh);
    BtRect r=area(w);
    x=(int)((int64_t)x*r.width/(lw?lw:1)); y=(int)((int64_t)y*r.height/(lh?lh:1));
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i) {
        Pane *p=&w->panes[i]; BtRect b=p->bounds;
        if(visible(w,p) && x>=b.x && x<b.x+b.width && y>=b.y && y<b.y+b.height) return p;
    }
    return NULL;
}
static int workspace_event(BtWorkspace *w, const SDL_Event *e) {
    int message=message_event(w,e);
    if(message) return message<0?-1:0;
    int menu=menu_event(w,e);
    if(menu) return menu<0?-1:0;
    int settings=settings_event(w,e);
    if(settings) return settings<0?-1:0;
    int rename=rename_event(w,e);
    if(rename) return rename<0?-1:0;
    int choice=chooser_event(w,e);
    if(choice) return choice<0?-1:0;
    if(e->type==SDL_MOUSEBUTTONDOWN && resize_finish(w,true)) return -1;
    if(e->type==SDL_MOUSEBUTTONDOWN && w->prefix) { w->prefix=0; w->dirty=true; }
    int mouse=chrome_event(w,e);
    if(mouse) return mouse<0?-1:0;
    int action=shortcut(w,e);
    if(action) return action<0?-1:0;
    if(w->filter && w->filter(w->filter_data,e)) { w->dirty=true; return 0; }
    if(e->type==SDL_QUIT) { w->closed=true; return 0; }
    Pane *p=pane(w,bt_workspace_active(w));
    if(e->type==SDL_WINDOWEVENT) {
        switch(e->window.event) {
            case SDL_WINDOWEVENT_CLOSE: w->closed=true; break;
            case SDL_WINDOWEVENT_SIZE_CHANGED: case SDL_WINDOWEVENT_DISPLAY_CHANGED: return geometry(w);
            case SDL_WINDOWEVENT_FOCUS_GAINED: case SDL_WINDOWEVENT_FOCUS_LOST:
                w->focused=e->window.event==SDL_WINDOWEVENT_FOCUS_GAINED;
                if(!w->focused) { w->capture=0; w->buttons=0; }
                if(focused(w,p,w->focused)) return -1;
                break;
        }
        w->dirty=true; return 0;
    }
    if(e->type==SDL_MOUSEBUTTONDOWN || e->type==SDL_MOUSEBUTTONUP) {
        if(e->button.button<1 || e->button.button>32) return 0;
        p=pane(w,w->capture);
        if(!p) p=hit(w,e->button.x,e->button.y);
        if(p && e->type==SDL_MOUSEBUTTONDOWN) {
            if(bt_workspace_focus(w,p->id)) return -1;
            w->capture=p->id; w->buttons|=1u<<(e->button.button-1);
        } else if(e->type==SDL_MOUSEBUTTONUP) {
            w->buttons&=~(1u<<(e->button.button-1));
            if(!w->buttons) w->capture=0;
        }
    } else if(e->type==SDL_MOUSEMOTION) {
        p=pane(w,w->capture);
        if(!p) p=hit(w,e->motion.x,e->motion.y);
    } else if(e->type==SDL_MOUSEWHEEL) {
        int x,y; SDL_GetMouseState(&x,&y); p=hit(w,x,y);
    }
    if(!p) return 0;
    if(bt_window_event(&p->view,e) && input_failure(w,p)) return -1;
    bool keyboard=e->type==SDL_KEYDOWN || e->type==SDL_KEYUP || e->type==SDL_TEXTINPUT;
    bool copy=(e->type==SDL_KEYDOWN || e->type==SDL_KEYUP) && e->key.keysym.sym==SDLK_c &&
        !(e->key.keysym.mod&KMOD_MODE) && (e->key.keysym.mod&KMOD_CTRL) && (e->key.keysym.mod&KMOD_SHIFT);
    if(keyboard && !copy && p->synchronized) for(unsigned i=0;i<BT_LAYOUT_PANES;++i) {
        Pane *q=&w->panes[i];
        if(q->id && q!=p && q->tab==p->tab && q->synchronized && bt_window_event(&q->view,e) && input_failure(w,q))
            return -1;
    }
    w->dirty=true; return 0;
}
size_t bt_workspace_image_cache_bytes(BtWorkspace *w) {
    size_t total=0;
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i) {
        Pane *p=&w->panes[i];
        if(p->id && p->view.renderer) total+=bt_renderer_image_stats(p->view.renderer).texture_bytes;
    }
    return total;
}
size_t bt_workspace_process_image_cache_bytes(void) {
    size_t total=0;
    for(BtWorkspace *w=workspaces;w;w=w->next) if(w->owner==getpid())
        for(unsigned i=0;i<BT_LAYOUT_PANES;++i) {
            Pane *p=&w->panes[i];
            if(!p->id || !p->view.renderer) continue;
            BtImageStats stats=bt_renderer_image_stats(p->view.renderer);
            total+=stats.texture_bytes+stats.shadow_bytes;
        }
    return total;
}
static int process_image_cache_trim(BtWorkspace *report) {
    size_t total=bt_workspace_process_image_cache_bytes();
    SDL_Window *previous_window=SDL_GL_GetCurrentWindow();
    SDL_GLContext previous_context=SDL_GL_GetCurrentContext();
    bool switched=false;
    int result=0;
    while(total>process_image_budget) {
        Pane *victim=NULL;
        size_t largest=0;
        int priority=INT_MAX;
        for(BtWorkspace *w=workspaces;w;w=w->next) if(w->owner==getpid())
            for(unsigned i=0;i<BT_LAYOUT_PANES;++i) {
                Pane *p=&w->panes[i];
                if(!p->id || !p->view.renderer) continue;
                BtImageStats stats=bt_renderer_image_stats(p->view.renderer);
                size_t bytes=stats.texture_bytes+stats.shadow_bytes;
                if(!bytes) continue;
                int rank=!visible(w,p) && p->id!=w->preview_pane?0:
                         !w->focused?1:p->id!=bt_workspace_active(w)?2:3;
                if(rank<priority || (rank==priority && bytes>largest)) {
                    priority=rank; largest=bytes; victim=p;
                }
            }
        if(!victim) { result=fail(report,"Process image cache accounting failed"); break; }
        switched=true;
        if(bt_renderer_drop_image_cache(victim->view.renderer)) {
            result=fail(report,bt_renderer_error(victim->view.renderer)); break;
        }
        total-=largest;
    }
    if(switched && SDL_GL_MakeCurrent(previous_window,previous_context))
        result=fail(report,SDL_GetError());
    return result;
}
int bt_workspace_process_image_budget(BtWorkspace *w, size_t bytes) {
    if(!w || w->owner!=getpid() || !bytes || bytes>1024u*1024u*1024u)
        return w?fail(w,"Process image budget is out of range"):-1;
    process_image_budget=bytes;
    return process_image_cache_trim(w);
}
static int image_cache_trim(BtWorkspace *w) {
    size_t total=bt_workspace_image_cache_bytes(w);
    while(total>w->image_budget) {
        Pane *victim=NULL;
        size_t largest=0;
        int priority=INT_MAX;
        for(unsigned i=0;i<BT_LAYOUT_PANES;++i) {
            Pane *p=&w->panes[i];
            if(!p->id || !p->view.renderer) continue;
            size_t bytes=bt_renderer_image_stats(p->view.renderer).texture_bytes;
            if(!bytes) continue;
            int rank=!visible(w,p) && p->id!=w->preview_pane?0:
                     p->id!=bt_workspace_active(w)?1:2;
            if(rank<priority || (rank==priority && bytes>largest)) {
                priority=rank; largest=bytes; victim=p;
            }
        }
        if(!victim) return fail(w,"Workspace image cache accounting failed");
        if(bt_renderer_drop_image_cache(victim->view.renderer))
            return fail(w,bt_renderer_error(victim->view.renderer));
        total-=largest;
    }
    return 0;
}
int bt_workspace_image_budget(BtWorkspace *w, size_t bytes) {
    if(!bytes || bytes>512u*1024u*1024u) return fail(w,"Workspace image budget is out of range");
    w->image_budget=bytes;
    return image_cache_trim(w);
}
int bt_workspace_draw(BtWorkspace *w, bool present) {
    BtRect r=area(w); if(r.width<1 || r.height<1) return 0;
    if(bt_surface_clear(w->surface,19,23,30)) return fail(w,bt_surface_error(w->surface));
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i) {
        Pane *p=&w->panes[i];
        if(visible(w,p) && bt_window_draw_view(&p->view,w->focused && bt_workspace_active(w)==p->id))
            return fail(w,p->view.error);
    }
    if(chrome(w,true) || chooser_draw(w) || rename_draw(w) || settings_draw(w) || menu_draw(w) || message_draw(w)) return -1;
    if(image_cache_trim(w)) return -1;
    if(process_image_cache_trim(w)) return -1;
    if(present && bt_surface_present(w->surface)) return fail(w,bt_surface_error(w->surface));
    w->dirty=false; return 0;
}
int bt_workspace_event(BtWorkspace *w, const SDL_Event *e) {
    /* Host actions must run before later input, including another key delivered
     * in the same SDL batch. Keep bounded ordering across the Bash boundary. */
    if(w->action_count || w->deferred_count) {
        if(w->deferred_count==DEFERRED_EVENTS) return fail(w,"Workspace input queue is full");
        SDL_Event copy=*e;
#if SDL_VERSION_ATLEAST(2,0,22)
        if(copy.type==SDL_TEXTEDITING_EXT) {
            copy.editExt.text=SDL_strdup(e->editExt.text?e->editExt.text:"");
            if(!copy.editExt.text) return fail(w,"Could not queue extended preedit text");
        }
#endif
        w->deferred[(w->deferred_start+w->deferred_count++)%DEFERRED_EVENTS]=copy;
        return 0;
    }
    return workspace_event(w,e);
}
BtWorkspacePumpStats bt_workspace_pump_stats(BtWorkspace *w) { return w->pump_stats; }
int bt_workspace_pump(BtWorkspace *w, int timeout_ms) {
    if(timeout_ms<0 || timeout_ms>100) return fail(w,"Invalid workspace pump timeout");
    if(timeout_ms) SDL_Delay((unsigned)timeout_ms);
    if(deferred_events(w)) return -1;
    bt_surface_poll();
    if(w->failed) return -1;
    if(w->closed) return 0;
    drain_detaches(w);
    bt_control_poll(w->control);
    if(w->scoped_count) {
        bt_control_poll(w->scoped_controls[w->scoped_next]);
        w->scoped_next=(w->scoped_next+1)%w->scoped_count;
    }
    if(w->closed) return 0;
    uint64_t started=bt_millis();
    unsigned first=w->pump_next;
    ++w->pump_stats.passes; w->pump_stats.last_panes=0;
    for(unsigned step=0;step<BT_LAYOUT_PANES;++step) {
        unsigned i=(first+step)%BT_LAYOUT_PANES;
        w->pump_next=(i+1)%BT_LAYOUT_PANES;
        Pane *p=&w->panes[i]; if(!p->id) continue;
        ++w->pump_stats.last_panes;
        BtSession *s=&p->view.session;
        uint64_t bytes=s->bytes_read,revision=s->presentation?s->presentation->revision:0;
        uint64_t graphics_revision=s->graphics_revision;
        bool done=s->done;
        connection_state(w,p);
        if(!p->disconnected && bt_window_pump(&p->view,0) && input_failure(w,p)) return -1;
        if(w->chrome && s->title_changed) w->dirty=true;
        s->title_changed=false;
        if(visible(w,p) && (bytes!=s->bytes_read || done!=s->done ||
           graphics_revision!=s->graphics_revision || p->view.force_draw ||
           (s->presentation && revision!=s->presentation->revision) || bt_renderer_due(p->view.renderer))) w->dirty=true;
        if(step+1<BT_LAYOUT_PANES && (w->pump_stats.last_panes>=16 || bt_millis()-started>=8)) {
            ++w->pump_stats.budget_yields;
            break;
        }
    }
    w->pump_stats.last_io_ms=bt_millis()-started;
    if(w->pump_stats.last_io_ms>w->pump_stats.max_io_ms) w->pump_stats.max_io_ms=w->pump_stats.last_io_ms;
    if(w->choose_all) for(unsigned i=0;i<BT_LAYOUT_PANES;++i) {
        Pane *p=&w->panes[i];
        if(p->id && p->telemetry_updated && bt_millis()-p->telemetry_updated>=3500) {
            memset(&p->telemetry,0,sizeof(p->telemetry)); p->telemetry_updated=0;
            w->dirty=true;
        }
    }
    if(w->choice_count) chooser_preview_update(w);
    else if(w->preview_pane) chooser_preview_clear(w);
    return w->dirty?bt_workspace_draw(w,true):0;
}
void bt_workspace_free(BtWorkspace *w) {
    if(!w) return;
    BtWorkspace **link=&workspaces;
    while(*link && *link!=w) link=&(*link)->next;
    if(*link) *link=w->next;
    chooser_preview_clear(w);
    while(w->deferred_count) {
        SDL_Event *event=&w->deferred[w->deferred_start];
#if SDL_VERSION_ATLEAST(2,0,22)
        if(event->type==SDL_TEXTEDITING_EXT) SDL_free(event->editExt.text);
#endif
        w->deferred_start=(w->deferred_start+1)%DEFERRED_EVENTS;
        --w->deferred_count;
    }
    bt_control_free(w->control);
    for(unsigned i=0;i<w->scoped_count;++i) bt_control_free(w->scoped_controls[i]);
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i) if(w->panes[i].id) {
        /* A closing host cannot wait once per pane for owners that are slow or
         * stopped. Already-sent socket packets remain ordered at the owner;
         * unsent frontend queue entries end with this view. */
        BtSession detached={.master=-1,.control=-1,.status=-1};
        bt_window_close_deferred(&w->panes[i].view,&detached);
        if(detached.remote) bt_session_close(&detached);
        free(w->panes[i].name); free(w->panes[i].session_dir);
    }
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i) if(w->detaching[i].remote) {
        bt_session_close(&w->detaching[i]);
    }
    bt_renderer_free(w->labels);
    if(w->label_session.terminal) ghostty_terminal_free(w->label_session.terminal);
    bt_surface_free(w->surface); free(w->font); free(w);
}
int bt_workspace_listen(BtWorkspace *w, const char *path, bool read_only,
                        const char *helper, const char *service, const char *root, char *const env[]) {
    return bt_workspace_listen_policy(w,path,read_only,helper,service,root,NULL,false,env);
}
int bt_workspace_listen_policy(BtWorkspace *w, const char *path, bool read_only,
                               const char *helper, const char *service, const char *root,
                               const char *terminate_prefix, bool host_actions, char *const env[]) {
    if(w->control) return fail(w,"Workspace already has a control endpoint");
    w->control=bt_control_new(w,path,read_only,helper,service,root,terminate_prefix,0,host_actions,env,w->error,sizeof(w->error));
    return w->control?0:-1;
}
int bt_workspace_listen_scope(BtWorkspace *w, const char *path, bool read_only,
                              const char *helper, const char *service, const char *root,
                              uint64_t id, char *const env[]) {
    if(!id || !pane(w,id)) return fail(w,"Scoped control pane does not exist");
    if(w->scoped_count==8) return fail(w,"Scoped control endpoint limit reached");
    BtControl *control=bt_control_new(w,path,read_only,helper,service,root,NULL,id,false,env,w->error,sizeof(w->error));
    if(!control) return -1;
    w->scoped_controls[w->scoped_count++]=control;
    return 0;
}
