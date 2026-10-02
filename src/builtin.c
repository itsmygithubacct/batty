/* SPDX-License-Identifier: MIT */
#include <config.h>
#include "loadables.h"
#include "window.h"
#include "workspace.h"
#include "remote.h"
#include <ctype.h>
#include <dlfcn.h>
#include <errno.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

enum { HANDLE_LIMIT=16 };
static struct { uint64_t id; BtWindow *window; } handles[HANDLE_LIMIT];
static struct { uint64_t id; BtWorkspace *workspace; } workspaces[HANDLE_LIMIT];
static uint64_t next_id=1;
static pid_t controller;

static int number(const char *text, int minimum, int maximum, int *value) {
    char *end;
    errno=0;
    long result=strtol(text,&end,10);
    if(errno || !*text || *end || result<minimum || result>maximum) return -1;
    *value=(int)result; return 0;
}
static bool identifier(const char *name) {
    if(!name || !(isalpha((unsigned char)*name)||*name=='_')) return false;
    for(++name;*name;++name) if(!(isalnum((unsigned char)*name)||*name=='_')) return false;
    return true;
}
static bool handle_variable(const char *name) {
    for(int depth=0;depth<64;++depth) {
        if(!identifier(name)) break;
        SHELL_VAR *v=find_variable_noref(name);
        if(v && (readonly_p(v) || noassign_p(v) || array_p(v) || assoc_p(v) ||
                 integer_p(v) || uppercase_p(v) || lowercase_p(v) || capcase_p(v) ||
                 v->dynamic_value || v->assign_func)) break;
        if(v && nameref_p(v)) { name=nameref_cell(v); continue; }
        return true;
    }
    builtin_error("handle variable must be a writable text scalar");
    return false;
}
static int session_root(const char *option, char *path, size_t capacity) {
    const char *value=option?option:get_string_value("BATTY_SESSION_DIR");
    int n;
    if(value && *value) n=snprintf(path,capacity,"%s",value);
    else {
        const char *runtime=get_string_value("XDG_RUNTIME_DIR");
        if(runtime && *runtime) n=snprintf(path,capacity,"%s/batty",runtime);
        else n=snprintf(path,capacity,"/tmp/batty-%lu",(unsigned long)geteuid());
    }
    if(n<0 || (size_t)n>=capacity || path[0]!='/') {
        builtin_error("session directory must be an absolute path of supported length"); return -1;
    }
    return 0;
}
static int module_program(const char *program, char *path, size_t capacity) {
    Dl_info info;
    char module[PATH_MAX];
    if(!dladdr((void *)module_program,&info) || !realpath(info.dli_fname,module)) {
        builtin_error("cannot locate native session programs"); return -1;
    }
    char *slash=strrchr(module,'/');
    if(!slash) return -1;
    *slash=0;
    int n=snprintf(path,capacity,"%s/%s",module,program);
    if(n<0 || (size_t)n>=capacity) { builtin_error("module path too long"); return -1; }
    return 0;
}
static int bind(const char *name, const char *value) {
    if(!identifier(name)) { builtin_error("expected a Bash variable name"); return 2; }
    SHELL_VAR *var=builtin_bind_variable((char *)name,(char *)value,0);
    if(!var || readonly_p(var) || noassign_p(var)) return 2;
    return 0;
}
static int output(WORD_LIST *args, const char *text) {
    if(!args) { puts(text); return 0; }
    if(!strcmp(args->word->word,"-V") && args->next && !args->next->next)
        return bind(args->next->word->word,text);
    builtin_error("expected -V VARIABLE"); return 2;
}
static int lookup(const char *name) {
    if(strncmp(name,"batty-",6)) return -1;
    char *end; errno=0;
    uint64_t id=strtoull(name+6,&end,10);
    if(errno || !name[6] || *end) return -1;
    for(int i=0;i<HANDLE_LIMIT;++i) if(handles[i].window && handles[i].id==id) return i;
    return -1;
}
static void dispose(int slot) {
    bt_window_close(handles[slot].window);
    free(handles[slot].window); handles[slot].window=NULL;
}
static int create(WORD_LIST *args) {
    int cols=100,rows=30,size=16;
    bool headless=false;
    const char *font="monospace",*variable=NULL,*name=NULL,*root_option=NULL;
    while(args) {
        const char *arg=args->word->word;
        if(!strcmp(arg,"--")) { args=args->next; break; }
        if(!strcmp(arg,"--headless")) { headless=true; args=args->next; continue; }
        if(!args->next) goto usage;
        const char *value=args->next->word->word;
        if(!strcmp(arg,"-h")) variable=value;
        else if(!strcmp(arg,"-W")) { if(number(value,1,1000,&cols)) goto usage; }
        else if(!strcmp(arg,"-H")) { if(number(value,1,1000,&rows)) goto usage; }
        else if(!strcmp(arg,"--font-size")) { if(number(value,6,96,&size)) goto usage; }
        else if(!strcmp(arg,"--font")) font=value;
        else if(!strcmp(arg,"--session")) name=value;
        else if(!strcmp(arg,"--session-dir")) root_option=value;
        else goto usage;
        args=args->next->next;
    }
    if(!args || !identifier(variable) || (root_option && !name)) goto usage;
    if(!handle_variable(variable)) return 2;
    int slot=0;
    while(slot<HANDLE_LIMIT && handles[slot].window) ++slot;
    if(slot==HANDLE_LIMIT) { builtin_error("terminal handle limit reached"); return 2; }
    size_t count=0;
    for(WORD_LIST *p=args;p;p=p->next) ++count;
    char **argv=calloc(count+1,sizeof(*argv));
    BtWindow *w=calloc(1,sizeof(*w));
    if(!argv || !w) { free(argv); free(w); builtin_error("out of memory"); return 2; }
    size_t index=0;
    for(WORD_LIST *p=args;p;p=p->next) argv[index++]=p->word->word;
    char helper[PATH_MAX],service[PATH_MAX],root[PATH_MAX];
    if(module_program("batty-session",helper,sizeof(helper)) ||
       (name && (module_program("batty-state",service,sizeof(service)) ||
                 session_root(root_option,root,sizeof(root))))) { free(argv); free(w); return 2; }
    maybe_make_export_env();
    int result=name?bt_window_open_persistent(w,service,helper,root,name,argv,export_env,cols,rows,font,size,headless):
        bt_window_open(w,helper,argv,export_env,cols,rows,font,size,headless);
    free(argv);
    if(result) { builtin_error("%s",w->error); bt_window_close(w); free(w); return 2; }
    uint64_t id=next_id++;
    char handle[40]; snprintf(handle,sizeof(handle),"batty-%llu",(unsigned long long)id);
    if(bind(variable,handle)) { bt_window_close(w); free(w); return 2; }
    handles[slot].window=w; handles[slot].id=id;
    return 0;
usage:
    builtin_error("new -h VARIABLE [--session NAME] [--session-dir ROOT] [-W COLS] [-H ROWS] [--font FAMILY] [--font-size PX] [--headless] -- COMMAND [ARG ...]");
    return 2;
}
static int attach(WORD_LIST *args) {
    const char *variable=NULL,*name=NULL,*root_option=NULL,*font="monospace";
    int size=16;
    bool headless=false,observe=false;
    while(args) {
        const char *arg=args->word->word;
        if(!strcmp(arg,"--")) { args=args->next; break; }
        if(!strcmp(arg,"--headless")) { headless=true; args=args->next; continue; }
        if(!strcmp(arg,"--observe")) { observe=true; args=args->next; continue; }
        if(!strcmp(arg,"-h") || !strcmp(arg,"--session-dir") ||
           !strcmp(arg,"--font") || !strcmp(arg,"--font-size")) {
            if(!args->next) goto usage;
            const char *value=args->next->word->word;
            if(!strcmp(arg,"-h")) variable=value;
            else if(!strcmp(arg,"--session-dir")) root_option=value;
            else if(!strcmp(arg,"--font")) font=value;
            else if(number(value,6,96,&size)) goto usage;
            args=args->next->next; continue;
        }
        break;
    }
    if(!args || args->next || !identifier(variable)) goto usage;
    name=args->word->word;
    if(!handle_variable(variable)) return 2;
    int slot=0;
    while(slot<HANDLE_LIMIT && handles[slot].window) ++slot;
    if(slot==HANDLE_LIMIT) { builtin_error("terminal handle limit reached"); return 2; }
    char root[PATH_MAX];
    if(session_root(root_option,root,sizeof(root))) return 2;
    BtWindow *w=calloc(1,sizeof(*w));
    if(!w) { builtin_error("out of memory"); return 2; }
    if(bt_window_attach(w,root,name,font,size,headless,observe)) {
        builtin_error("%s",w->error); bt_window_close(w); free(w); return 2;
    }
    uint64_t id=next_id++;
    char handle[40]; snprintf(handle,sizeof(handle),"batty-%llu",(unsigned long long)id);
    if(bind(variable,handle)) { bt_window_close(w); free(w); return 2; }
    handles[slot].window=w; handles[slot].id=id;
    return 0;
usage:
    builtin_error("attach -h VARIABLE [--session-dir ROOT] [--headless] [--observe] [--font FAMILY] [--font-size PX] NAME");
    return 2;
}
static int admin(const char *verb, WORD_LIST *args) {
    const char *root_option=NULL;
    if(args && !strcmp(args->word->word,"--session-dir")) {
        if(!args->next) return 2;
        root_option=args->next->word->word; args=args->next->next;
    }
    if(args && !strcmp(args->word->word,"--")) args=args->next;
    bool listing=!strcmp(verb,"list");
    if((listing && args) || (!listing && (!args || args->next))) {
        builtin_error("%s [--session-dir ROOT]%s",verb,listing?"":" NAME"); return 2;
    }
    char root[PATH_MAX],error[256];
    if(session_root(root_option,root,sizeof(root))) return 2;
    if(listing) {
        char *text=NULL;
        if(bt_remote_list(root,&text,error,sizeof(error))) { builtin_error("%s",error); return 2; }
        bool ok=!text || fputs(text,stdout)>=0;
        free(text); return ok?0:2;
    }
    if(bt_remote_terminate(root,args->word->word,error,sizeof(error))) { builtin_error("%s",error); return 2; }
    return 0;
}
static int pane_number(const char *text, uint64_t *value) {
    if(!text || !*text) return -1;
    for(const char *p=text;*p;++p) if(*p<'0' || *p>'9') return -1;
    errno=0; char *end;
    *value=strtoull(text,&end,10);
    return errno || *end ? -1 : 0;
}
static int session_epoch(const char *text, uint64_t *value) {
    if(!text || !*text || strlen(text)>16) return -1;
    for(const char *p=text;*p;++p)
        if(!((*p>='0' && *p<='9') || (*p>='a' && *p<='f') || (*p>='A' && *p<='F'))) return -1;
    errno=0; char *end; *value=strtoull(text,&end,16);
    return errno || *end || !*value ? -1 : 0;
}
static int direction(const char *text, BtDirection *value) {
    const char *names[]={"left","right","up","down"};
    for(int i=0;i<4;++i) if(!strcmp(text,names[i])) { *value=(BtDirection)i; return 0; }
    return -1;
}
typedef struct { uint64_t id; BtWindow *view; const char *session_name,*session_dir; } PaneLookup;
static void pane_lookup(void *data, const BtPaneInfo *info) {
    PaneLookup *lookup=data;
    if(info->id==lookup->id) {
        lookup->view=info->view;
        if(info->view->session.remote && !bt_remote_observer(&info->view->session)) {
            lookup->session_name=info->session_name;
            lookup->session_dir=info->session_dir;
        }
    }
}
typedef struct { char text[BT_LAYOUT_PANES*256]; size_t used; } PaneList;
static void pane_list(void *data, const BtPaneInfo *info) {
    PaneList *list=data;
    int n=snprintf(list->text+list->used,sizeof(list->text)-list->used,
        "%llu %llu %llu %d %d %d %d %d %d %d\n",
        (unsigned long long)info->id,(unsigned long long)info->tab,(unsigned long long)info->window,
        info->active,info->visible,info->synchronized,info->bounds.x,info->bounds.y,
        info->bounds.width,info->bounds.height);
    if(n>0 && (size_t)n<sizeof(list->text)-list->used) list->used+=(size_t)n;
}
static int workspace_create(WORD_LIST *args) {
    const char *variable=NULL,*font="monospace",*title="Batty";
    int width=1000,height=700,size=16;
    for(;args;args=args->next->next) {
        if(!args->next) goto usage;
        const char *key=args->word->word,*value=args->next->word->word;
        if(!strcmp(key,"-h")) variable=value;
        else if(!strcmp(key,"--font")) font=value;
        else if(!strcmp(key,"--title")) title=value;
        else if(!strcmp(key,"--font-size")) { if(number(value,6,96,&size)) goto usage; }
        else if(!strcmp(key,"--width")) { if(number(value,120,16384,&width)) goto usage; }
        else if(!strcmp(key,"--height")) { if(number(value,80,16384,&height)) goto usage; }
        else goto usage;
    }
    if(!variable || !handle_variable(variable)) return 2;
    int slot=0;
    while(slot<HANDLE_LIMIT && workspaces[slot].workspace) ++slot;
    if(slot==HANDLE_LIMIT) { builtin_error("workspace handle limit reached"); return 2; }
    char error[256];
    BtWorkspace *w=bt_workspace_new(title,width,height,font,size,error,sizeof(error));
    if(!w) { builtin_error("%s",error); return 2; }
    uint64_t id=next_id++;
    char handle[48]; snprintf(handle,sizeof(handle),"workspace-%llu",(unsigned long long)id);
    if(bind(variable,handle)) { bt_workspace_free(w); return 2; }
    workspaces[slot].workspace=w; workspaces[slot].id=id; return 0;
usage:
    builtin_error("workspace new -h VARIABLE [--title TEXT] [--width PX] [--height PX] [--font FAMILY] [--font-size PX]");
    return 2;
}
static int workspace_add(BtWorkspace *w, WORD_LIST *args) {
    const char *variable=NULL,*name=NULL,*root_option=NULL;
    uint64_t target=0,epoch=0;
    BtDirection dir=BT_RIGHT;
    bool attach=false,observe=false;
    while(args) {
        const char *key=args->word->word;
        if(!strcmp(key,"--")) { args=args->next; break; }
        if(!strcmp(key,"--attach")) { attach=true; args=args->next; continue; }
        if(!strcmp(key,"--observe")) { attach=true; observe=true; args=args->next; continue; }
        if(!args->next) goto usage;
        const char *value=args->next->word->word;
        if(!strcmp(key,"-V")) variable=value;
        else if(!strcmp(key,"--session")) name=value;
        else if(!strcmp(key,"--session-dir")) root_option=value;
        else if(!strcmp(key,"--epoch")) { if(session_epoch(value,&epoch)) goto usage; }
        else if(!strcmp(key,"--target")) { if(pane_number(value,&target) || !target) goto usage; }
        else if(!strcmp(key,"--direction")) { if(direction(value,&dir)) goto usage; }
        else goto usage;
        args=args->next->next;
    }
    if(!variable || (attach?(!name || args):!args) || (root_option && !name) || (epoch && !attach)) goto usage;
    if(!handle_variable(variable)) return 2;
    size_t count=0;
    for(WORD_LIST *p=args;p;p=p->next) ++count;
    char **argv=calloc(count+1,sizeof(*argv));
    if(!argv) { builtin_error("out of memory"); return 2; }
    size_t index=0;
    for(WORD_LIST *p=args;p;p=p->next) argv[index++]=p->word->word;
    char helper[PATH_MAX],service[PATH_MAX],root[PATH_MAX];
    if(module_program("batty-session",helper,sizeof(helper)) ||
       module_program("batty-state",service,sizeof(service)) ||
       (name && session_root(root_option,root,sizeof(root)))) { free(argv); return 2; }
    maybe_make_export_env();
    BtPaneLaunch launch={.helper=helper,.service=service,.session_dir=name?root:NULL,
        .session_name=name,.argv=argv,.env=export_env,.attach=attach,.observe=observe,.expected_epoch=epoch};
    uint64_t id=0;
    int rc=bt_workspace_add(w,target,dir,&launch,&id);
    free(argv);
    if(rc) { builtin_error("%s",bt_workspace_error(w)); return 2; }
    char text[32]; snprintf(text,sizeof(text),"%llu",(unsigned long long)id);
    return bind(variable,text);
usage:
    builtin_error("workspace add H -V PANE [--target PANE --direction left|right|up|down] [--session NAME] [--session-dir ROOT] [--attach|--observe] [--epoch HEX] [-- COMMAND ARG ...]");
    return 2;
}
static int workspace_layout_bytes(BtWorkspace *w, WORD_LIST *args, bool apply) {
    if(!args) { builtin_error("layout-check|layout-apply requires BWL1 or BWL2 hex bytes"); return 2; }
    const char *hex=args->word->word; size_t n=strlen(hex);
    if(!n || n%2 || n>2*BT_WORKSPACE_LAYOUT_MAX_BYTES) { builtin_error("Invalid layout byte length"); return 2; }
    uint8_t *bytes=malloc(n/2);
    if(!bytes) { builtin_error("out of memory"); return 2; }
    for(size_t i=0;i<n;++i) {
        unsigned char c=(unsigned char)hex[i];
        unsigned value=c>='0' && c<='9'?c-'0':c>='a' && c<='f'?c-'a'+10:c>='A' && c<='F'?c-'A'+10:16;
        if(value==16) { free(bytes); builtin_error("Invalid layout hex"); return 2; }
        if(!(i%2)) bytes[i/2]=(uint8_t)(value<<4); else bytes[i/2]|=(uint8_t)value;
    }
    BtWorkspaceLayout *state=NULL;
    int rc=bt_workspace_layout_unpack(bytes,n/2,&state); free(bytes);
    if(rc) { builtin_error("Invalid workspace layout"); return 2; }
    args=args->next;
    if(!apply) {
        char ids[BT_LAYOUT_PANES*22]; size_t used=0;
        for(unsigned i=0;i<state->pane_count;++i)
            used+=(size_t)snprintf(ids+used,sizeof(ids)-used,"%s%llu",i?" ":"",(unsigned long long)state->panes[i].id);
        rc=output(args,ids); free(state); return rc;
    }
    BtPaneMapping map[BT_LAYOUT_PANES]; unsigned count=0;
    while(args && count<BT_LAYOUT_PANES) {
        if(!args->next || pane_number(args->word->word,&map[count].saved) ||
           pane_number(args->next->word->word,&map[count].current)) break;
        ++count; args=args->next->next;
    }
    if(args || count!=state->pane_count) { free(state); builtin_error("Supply one saved/current pane pair per layout pane"); return 2; }
    rc=bt_workspace_layout_apply(w,state,map,count); free(state);
    if(rc) { builtin_error("%s",bt_workspace_error(w)); return 2; }
    return 0;
}
static int workspace_builtin(WORD_LIST *args) {
    if(!args) goto usage;
    const char *verb=args->word->word; args=args->next;
    if(!strcmp(verb,"new")) return workspace_create(args);
    if(!args) goto usage;
    const char *handle=args->word->word;
    uint64_t id=0;
    if(strncmp(handle,"workspace-",10) || pane_number(handle+10,&id)) goto invalid;
    int slot=0;
    while(slot<HANDLE_LIMIT && (!workspaces[slot].workspace || workspaces[slot].id!=id)) ++slot;
    if(slot==HANDLE_LIMIT) goto invalid;
    BtWorkspace *w=workspaces[slot].workspace;
    args=args->next;
    if(!strcmp(verb,"add")) return workspace_add(w,args);
    if(!strcmp(verb,"layout-check")) return workspace_layout_bytes(w,args,false);
    if(!strcmp(verb,"layout-apply")) return workspace_layout_bytes(w,args,true);
    if(!strcmp(verb,"window-size")) {
        int width,height;
        if(!args || !args->next || args->next->next || number(args->word->word,120,16384,&width) ||
           number(args->next->word->word,80,16384,&height)) goto usage;
        SDL_SetWindowSize(bt_surface_window(bt_workspace_surface(w)),width,height);
        return 0;
    }
    if(!strcmp(verb,"listen")) {
        if(!args) goto usage;
        const char *path=args->word->word,*terminate_prefix=NULL;
        uint64_t scope_pane=0;
        bool read_only=false,host_actions=false;
        for(args=args->next;args;args=args->next) {
            if(!strcmp(args->word->word,"--read-only") && !read_only) read_only=true;
            else if(!strcmp(args->word->word,"--host-actions") && !host_actions) host_actions=true;
            else if(!strcmp(args->word->word,"--terminate-prefix") && !terminate_prefix && args->next) {
                terminate_prefix=args->next->word->word; args=args->next;
            } else if(!strcmp(args->word->word,"--pane") && !scope_pane && args->next) {
                if(pane_number(args->next->word->word,&scope_pane) || !scope_pane) goto usage;
                args=args->next;
            } else goto usage;
        }
        if(((read_only || scope_pane) && terminate_prefix) ||
           (host_actions && (read_only || scope_pane))) goto usage;
        char helper[PATH_MAX],service[PATH_MAX],root[PATH_MAX];
        if(module_program("batty-session",helper,sizeof(helper)) ||
           module_program("batty-state",service,sizeof(service)) || session_root(NULL,root,sizeof(root))) return 2;
        maybe_make_export_env();
        if(scope_pane?bt_workspace_listen_scope(w,path,read_only,helper,service,root,scope_pane,export_env):
           bt_workspace_listen_policy(w,path,read_only,helper,service,root,terminate_prefix,host_actions,export_env)) goto failed;
        return 0;
    }
    if(!strcmp(verb,"app-menu")) {
        if(args && args->next) goto usage;
        if(bt_workspace_app_menu(w,args?args->word->word:NULL)) goto failed;
        return 0;
    }
    if(!strcmp(verb,"menu")) {
        if(args) goto usage;
        if(bt_workspace_menu(w)) goto failed;
        return 0;
    }
    if(!strcmp(verb,"start-badge")) {
        int enabled;
        if(!args || args->next || number(args->word->word,0,1,&enabled)) goto usage;
        if(bt_workspace_start_badge(w,enabled)) goto failed;
        return 0;
    }
    if(!strcmp(verb,"message")) {
        if(args && args->next && !args->next->next && !strcmp(args->word->word,"-V"))
            return output(args,bt_workspace_message_text(w));
        if(!args || args->next) goto usage;
        if(bt_workspace_message(w,args->word->word)) goto failed;
        return 0;
    }
    if(!strcmp(verb,"settings-result")) {
        if(!args || args->next) goto usage;
        const char *names[]={"clear","saved","save-failed","reload-failed"};
        unsigned result=0;
        while(result<4 && strcmp(args->word->word,names[result])) ++result;
        if(result==4) goto usage;
        if(bt_workspace_settings_result(w,result)) goto failed;
        return 0;
    }
    if(!strcmp(verb,"settings-recording")) {
        const char *values[5];
        for(unsigned i=0;i<5;++i) {
            if(!args) goto usage;
            values[i]=args->word->word; args=args->next;
        }
        if(args) goto usage;
        if(bt_workspace_settings_recording(w,values[0],values[1],values[2],values[3],values[4])) goto failed;
        return 0;
    }
    if(!strcmp(verb,"settings")) {
        if(args && (args->next || strcmp(args->word->word,"--edge-locked"))) goto usage;
        if(bt_workspace_settings(w,args!=NULL)) goto failed;
        return 0;
    }
    if(!strcmp(verb,"choose")) {
        if(!args || args->next || (strcmp(args->word->word,"panes") && strcmp(args->word->word,"pages") &&
                                   strcmp(args->word->word,"all"))) goto usage;
        if(!strcmp(args->word->word,"all")?bt_workspace_pane_center(w):
           bt_workspace_choose(w,!strcmp(args->word->word,"pages"))) goto failed;
        return 0;
    }
    if(!strcmp(verb,"chrome-buttons")) {
        int mask;
        if(!args || args->next || number(args->word->word,0,511,&mask)) goto usage;
        if(bt_workspace_chrome_buttons(w,(unsigned)mask)) goto failed;
        return 0;
    }
    if(!strcmp(verb,"chrome-edge")) {
        if(!args || args->next || (strcmp(args->word->word,"top") && strcmp(args->word->word,"bottom"))) goto usage;
        if(bt_workspace_chrome_edge(w,!strcmp(args->word->word,"bottom"))) goto failed;
        return 0;
    }
    if(!strcmp(verb,"chrome")) {
        int enabled;
        if(!args || args->next || number(args->word->word,0,1,&enabled)) goto usage;
        if(bt_workspace_chrome(w,enabled)) goto failed;
        return 0;
    }
    if(!strcmp(verb,"bind")) {
        if(!args || !args->next || (args->next->next && args->next->next->next)) goto usage;
        int rc=args->next->next?
            bt_workspace_bind_sequence(w,args->word->word,args->next->word->word,args->next->next->word->word):
            bt_workspace_bind(w,args->word->word,args->next->word->word);
        if(rc) goto failed;
        return 0;
    }
    if(!strcmp(verb,"navigate")) {
        if(!args) goto usage;
        const char *name=args->word->word;
        const char *names[]={"next-pane","previous-pane","last-pane","last-page","page","swap-next","swap-previous"};
        unsigned operation=0;
        while(operation<sizeof(names)/sizeof(names[0]) && strcmp(name,names[operation])) ++operation;
        if(operation==sizeof(names)/sizeof(names[0])) goto usage;
        int page=0;
        if(operation==BT_PAGE_NUMBER) {
            if(!args->next || args->next->next || number(args->next->word->word,1,BT_WORKSPACE_TABS,&page)) goto usage;
        } else if(args->next) goto usage;
        if(bt_workspace_navigate(w,(BtNavigation)operation,(unsigned)page)) goto failed;
        return 0;
    }
    if(!strcmp(verb,"close-page")) {
        uint64_t pane;
        if(!args || args->next || pane_number(args->word->word,&pane)) goto usage;
        if(bt_workspace_close_tab(w,pane)) goto failed;
        return 0;
    }
    if(!strcmp(verb,"action")) {
        if(!args || strcmp(args->word->word,"-V") || !args->next || args->next->next ||
           !handle_variable(args->next->word->word)) goto usage;
        BtWorkspaceAction action;
        int ready=bt_workspace_action(w,&action);
        char text[80]="";
        if(ready) snprintf(text,sizeof(text),"%s %llu",action.name,(unsigned long long)action.pane);
        if(bind(args->next->word->word,text)) return 2;
        return ready?0:1;
    }
    if(!strcmp(verb,"reload-complete")) {
        uint64_t ticket; int status;
        if(!args || !args->next || args->next->next ||
           pane_number(args->word->word,&ticket) || !ticket ||
           number(args->next->word->word,0,255,&status)) goto usage;
        if(bt_workspace_reload_complete(w,ticket,(unsigned)status)) goto failed;
        return 0;
    }
    if(!strcmp(verb,"close")) {
        if(args) goto usage;
        bt_workspace_free(w); workspaces[slot].workspace=NULL; return 0;
    }
    if(!strcmp(verb,"active")) {
        char text[32]; snprintf(text,sizeof(text),"%llu",(unsigned long long)bt_workspace_active(w));
        return output(args,text);
    }
    if(!strcmp(verb,"panes")) {
        PaneList list={0}; bt_workspace_visit(w,pane_list,&list);
        if(list.used) list.text[list.used-1]=0;
        return output(args,list.text);
    }
    if(!strcmp(verb,"pump-stats")) {
        BtWorkspacePumpStats stats=bt_workspace_pump_stats(w);
        char text[256];
        snprintf(text,sizeof(text),"passes=%llu yields=%llu panes=%u io_ms=%llu max_io_ms=%llu pending_detaches=%u completed_detaches=%llu failed_detaches=%llu",
                 (unsigned long long)stats.passes,(unsigned long long)stats.budget_yields,stats.last_panes,
                 (unsigned long long)stats.last_io_ms,(unsigned long long)stats.max_io_ms,stats.pending_detaches,
                 (unsigned long long)stats.completed_detaches,(unsigned long long)stats.failed_detaches);
        return output(args,text);
    }
    int rc=0;
    if(!strcmp(verb,"pump")) {
        int timeout=16;
        if(args && (strcmp(args->word->word,"-t") || !args->next || args->next->next ||
                    number(args->next->word->word,0,100,&timeout))) goto usage;
        if(bt_workspace_pump(w,timeout)) goto failed;
        return bt_workspace_closed(w)?1:0;
    }
    if(!strcmp(verb,"capture")) {
        if(!args || args->next) goto usage;
        if(bt_workspace_draw(w,false)) goto failed;
        BtSurface *surface=bt_workspace_surface(w);
        if(bt_surface_capture(surface,args->word->word)) {
            builtin_error("%s",bt_surface_error(surface)); return 2;
        }
        return 0;
    }
    if(!strcmp(verb,"tab")) {
        int delta;
        if(!args || args->next || number(args->word->word,-1,1,&delta) || !delta) goto usage;
        rc=bt_workspace_cycle_tab(w,delta);
    } else {
        if(!args || pane_number(args->word->word,&id) || !id) goto usage;
        args=args->next;
        PaneLookup lookup={.id=id}; bt_workspace_visit(w,pane_lookup,&lookup);
        BtWindow *view=lookup.view;
        if(!view) { builtin_error("pane does not exist in this workspace"); return 2; }
        if(!strcmp(verb,"session-name")) return output(args,lookup.session_name?lookup.session_name:"");
        if(!strcmp(verb,"session-dir")) return output(args,lookup.session_dir?lookup.session_dir:"");
        if(!strcmp(verb,"confirm")) {
            if(!args || !args->next || args->next->next) goto usage;
            if(bt_workspace_confirm(w,id,args->word->word,args->next->word->word)) goto failed;
            return 0;
        }
        if(!strcmp(verb,"status")) {
            char text[32];
            if(view->session.exited) snprintf(text,sizeof(text),"%d",view->session.exit_status);
            else snprintf(text,sizeof(text),"running");
            return output(args,text);
        }
        if(!strcmp(verb,"page-title")) return output(args,bt_workspace_page_title(w,id));
        if(!strcmp(verb,"rename")) {
            if(!args || args->next) goto usage;
            if(bt_workspace_rename(w,id,args->word->word)) goto failed;
            return 0;
        }
        if(!strcmp(verb,"pane-rename")) {
            if(!args || args->next) goto usage;
            if(bt_workspace_pane_rename(w,id,args->word->word)) goto failed;
            return 0;
        }
        if(!strcmp(verb,"title")) {
            const char *title=bt_workspace_pane_title(w,id);
            return output(args,title?title:"");
        }
        if(!strcmp(verb,"dump")) {
            if(args) goto usage;
            size_t length=0; char *text=bt_session_text(&view->session,false,&length);
            if(!text) { builtin_error("could not format terminal"); return 2; }
            bool ok=fwrite(text,1,length,stdout)==length;
            free(text); return ok?0:2;
        }
        if(!strcmp(verb,"send") || !strcmp(verb,"paste")) {
            if(!args || args->next) goto usage;
            if(view->session.remote && bt_remote_observer(&view->session)) {
                builtin_error("observer panes cannot send input"); return 2;
            }
            const char *text=args->word->word;
            rc=!strcmp(verb,"paste")?bt_window_paste(view,text,strlen(text)):
                bt_session_send(&view->session,text,strlen(text));
            if(rc) { builtin_error("%s",view->error[0]?view->error:view->session.error); return 2; }
            return 0;
        }
        if(!strcmp(verb,"layout")) {
            if(!args || args->next) goto usage;
            const char *names[]={"next","splits","stack","tall","grid"};
            int mode=0;
            while(mode<5 && strcmp(args->word->word,names[mode])) ++mode;
            if(mode==5) goto usage;
            rc=bt_workspace_layout(w,id,mode-1);
        } else if(!strcmp(verb,"font")) {
            int delta;
            if(!args || args->next || number(args->word->word,-90,90,&delta)) goto usage;
            rc=bt_workspace_font(w,id,delta);
        } else if(!strcmp(verb,"move") || !strcmp(verb,"neighbor")) {
            BtDirection dir;
            if(!args || direction(args->word->word,&dir)) goto usage;
            args=args->next;
            if(!strcmp(verb,"neighbor")) {
                uint64_t neighbor;
                if(bt_workspace_neighbor(w,id,dir,&neighbor)) goto failed;
                char text[32]; snprintf(text,sizeof(text),"%llu",(unsigned long long)neighbor);
                return output(args,text);
            }
            if(args) goto usage;
            rc=bt_workspace_move(w,id,dir);
        } else if(!strcmp(verb,"relocate")) {
            uint64_t target; BtDirection dir;
            if(!args || !args->next || args->next->next ||
               pane_number(args->word->word,&target) || !target ||
               direction(args->next->word->word,&dir)) goto usage;
            rc=bt_workspace_relocate(w,id,target,dir);
        } else if(!strcmp(verb,"resize")) {
            if(!args || !args->next || args->next->next) goto usage;
            bool horizontal=!strcmp(args->word->word,"horizontal");
            int delta;
            if((!horizontal && strcmp(args->word->word,"vertical")) ||
               number(args->next->word->word,-9998,9998,&delta)) goto usage;
            rc=bt_workspace_resize(w,id,horizontal,delta);
        } else if(!strcmp(verb,"sync")) {
            int enabled;
            if(!args || args->next || number(args->word->word,0,1,&enabled)) goto usage;
            rc=bt_workspace_synchronize(w,id,enabled);
        } else {
            if(args) goto usage;
            if(!strcmp(verb,"focus")) rc=bt_workspace_focus(w,id);
            else if(!strcmp(verb,"reset-sizes")) rc=bt_workspace_reset_sizes(w,id);
            else if(!strcmp(verb,"rename-prompt")) rc=bt_workspace_rename_prompt(w,id);
            else if(!strcmp(verb,"pane-rename-prompt")) rc=bt_workspace_pane_rename_prompt(w,id);
            else if(!strcmp(verb,"pane-reset-title")) rc=bt_workspace_pane_rename(w,id,"");
            else if(!strcmp(verb,"pane-copy-title")) rc=bt_workspace_pane_copy_title(w,id);
            else if(!strcmp(verb,"pane-clear")) rc=bt_workspace_pane_clear(w,id);
            else if(!strcmp(verb,"resize-mode")) rc=bt_workspace_resize_mode(w,id);
            else if(!strcmp(verb,"zoom")) rc=bt_workspace_zoom(w,id);
            else if(!strcmp(verb,"remove")) rc=bt_workspace_close(w,id);
            else goto usage;
        }
    }
    if(!rc) return 0;
failed:
    builtin_error("%s",bt_workspace_error(w)); return 2;
invalid:
    builtin_error("invalid or closed workspace handle"); return 2;
usage:
    builtin_error("workspace new|add|close|active|panes|pump|capture|tab|status|title|dump|send|paste|neighbor|move|resize|sync|focus|zoom|remove: see help batty");
    return 2;
}
int batty_builtin(WORD_LIST *args) {
    if(!args) { builtin_usage(); return 2; }
    const char *verb=args->word->word; args=args->next;
    if(!strcmp(verb,"list") || !strcmp(verb,"terminate")) return admin(verb,args);
    if(getpid()!=controller) { builtin_error("run batty in its owning Bash process; subshell handles are invalid"); return 2; }
    if(!strcmp(verb,"workspace")) return workspace_builtin(args);
    if(!strcmp(verb,"new")) return create(args);
    if(!strcmp(verb,"attach")) return attach(args);
    if(!args) { builtin_error("%s needs a terminal handle",verb); return 2; }
    int slot=lookup(args->word->word);
    if(slot<0) { builtin_error("invalid or closed terminal handle"); return 2; }
    BtWindow *w=handles[slot].window;
    args=args->next;
    if(!strcmp(verb,"close")) {
        if(args) { builtin_error("close takes only a handle"); return 2; }
        dispose(slot); return 0;
    }
    if(!strcmp(verb,"pump")) {
        int timeout=16;
        const char *variable=NULL;
        for(;args;args=args->next->next) {
            if(!args->next) { builtin_error("pump H [-t MILLISECONDS] [-V EVENT]"); return 2; }
            if(!strcmp(args->word->word,"-t")) { if(number(args->next->word->word,0,100,&timeout)) return 2; }
            else if(!strcmp(args->word->word,"-V")) variable=args->next->word->word;
            else { builtin_error("unknown pump option"); return 2; }
        }
        if(variable && !identifier(variable)) return 2;
        unsigned old_cols=w->session.cols, old_rows=w->session.rows;
        if(bt_window_pump(w,timeout)) { builtin_error("%s",w->error); return 2; }
        const char *event=w->close_requested?"close":w->session.done?"exit":
            old_cols!=w->session.cols || old_rows!=w->session.rows?"resize":w->session.title_changed?"title":"tick";
        if(variable && bind(variable,event)) return 2;
        w->session.title_changed=false;
        return w->close_requested || w->session.done ? 1 : 0;
    }
    if(!strcmp(verb,"send") || !strcmp(verb,"paste")) {
        if(w->session.remote && bt_remote_observer(&w->session)) { builtin_error("observer handles cannot send input"); return 2; }
        if(!args || args->next) { builtin_error("%s H TEXT",verb); return 2; }
        const char *text=args->word->word;
        int rc=!strcmp(verb,"paste")?bt_window_paste(w,text,strlen(text)):bt_session_send(&w->session,text,strlen(text));
        if(rc) { builtin_error("%s",w->error[0]?w->error:w->session.error); return 2; }
        return 0;
    }
    if(!strcmp(verb,"resize")) {
        if(w->session.remote && bt_remote_observer(&w->session)) { builtin_error("observer handles cannot resize a session"); return 2; }
        int cols=0,rows=0;
        while(args && args->next) {
            if(!strcmp(args->word->word,"-W")) { if(number(args->next->word->word,1,1000,&cols)) return 2; }
            else if(!strcmp(args->word->word,"-H")) { if(number(args->next->word->word,1,1000,&rows)) return 2; }
            else return 2;
            args=args->next->next;
        }
        if(args || !cols || !rows) { builtin_error("resize H -W COLS -H ROWS"); return 2; }
        if(bt_session_resize(&w->session,cols,rows,w->session.cell_width,w->session.cell_height)) {
            builtin_error("%s",w->session.error); return 2;
        }
        if(w->window) {
            int ww,wh,pw,ph;
            SDL_GetWindowSize(w->window,&ww,&wh); SDL_GL_GetDrawableSize(w->window,&pw,&ph);
            SDL_SetWindowSize(w->window,(cols*w->session.cell_width+16)*ww/pw,(rows*w->session.cell_height+16)*wh/ph);
        }
        w->force_draw=true; return 0;
    }
    if(!strcmp(verb,"dump")) {
        if(args) return 2;
        size_t length=0; char *text=bt_session_text(&w->session,false,&length);
        if(!text) { builtin_error("could not format terminal"); return 2; }
        bool ok=fwrite(text,1,length,stdout)==length;
        free(text); return ok?0:2;
    }
    if(!strcmp(verb,"capture")) {
        if(!w->renderer || !args || args->next) { builtin_error("capture H PATH requires a desktop window"); return 2; }
        if(bt_renderer_capture(w->renderer,args->word->word)) { builtin_error("%s",bt_renderer_error(w->renderer)); return 2; }
        return 0;
    }
    if(!strcmp(verb,"graphics")) {
        char graphics[2048];
        if(!w->renderer) { builtin_error("graphics H requires a desktop window"); return 2; }
        if(bt_renderer_graphics(w->renderer,graphics,sizeof(graphics))) {
            builtin_error("%s",bt_renderer_error(w->renderer)); return 2;
        }
        return output(args,graphics);
    }
    char text[256];
    if(!strcmp(verb,"status")) {
        if(w->session.exited) snprintf(text,sizeof(text),"%d",w->session.exit_status);
        else snprintf(text,sizeof(text),"%s",w->close_requested?"0":"running");
    } else if(!strcmp(verb,"info")) {
        snprintf(text,sizeof(text),"pid=%d cols=%u rows=%u pending=%zu read=%llu written=%llu persistent=%d observe=%d",
            (int)w->session.child,w->session.cols,w->session.rows,w->session.pending_end-w->session.pending_start,
            (unsigned long long)w->session.bytes_read,(unsigned long long)w->session.bytes_written,
            w->session.remote!=NULL,w->session.remote && bt_remote_observer(&w->session));
    } else if(!strcmp(verb,"title")) {
        const char *title=bt_session_title(&w->session);
        snprintf(text,sizeof(text),"%s",title?title:"");
    } else { builtin_error("unknown operation: %s",verb); return 2; }
    return output(args,text);
}
int batty_builtin_load(char *name) {
    (void)name;
    const char *version=get_string_value("BASH_VERSION");
    if(!version || strncmp(version,"5.3.",4)) { builtin_error("this build requires GNU Bash 5.3"); return 0; }
    controller=getpid();
    return 1;
}
void batty_builtin_unload(char *name) {
    (void)name;
    if(getpid()!=controller) return;
    for(int i=0;i<HANDLE_LIMIT;++i) if(handles[i].window) dispose(i);
    for(int i=0;i<HANDLE_LIMIT;++i) if(workspaces[i].workspace) {
        bt_workspace_free(workspaces[i].workspace); workspaces[i].workspace=NULL;
    }
}
__attribute__((destructor)) static void finish(void) { batty_builtin_unload(NULL); }
char *batty_doc[]={
    "Bash-controlled native terminal using pinned libghostty-vt.",
    "new -h VARIABLE [--session NAME] [--session-dir ROOT] [-W COLS] [-H ROWS] [--font FAMILY] [--font-size PX] [--headless] -- COMMAND [ARG ...]",
    "attach -h VARIABLE [--session-dir ROOT] [--headless] [--observe] [--font FAMILY] [--font-size PX] NAME",
    "list [--session-dir ROOT]; terminate [--session-dir ROOT] NAME",
    "pump H [-t MS] [-V EVENT]: 0 running, 1 close/exit, 2 error; maximum wait 100 ms.",
    "send H TEXT; paste H TEXT; resize H -W COLS -H ROWS; dump H; capture H PATH.ppm",
    "status|info|title|graphics H [-V VARIABLE]; close H",
    "workspace new -h VARIABLE [--title TEXT] [--width PX] [--height PX] [--font FAMILY] [--font-size PX]",
    "workspace add H -V PANE [--target PANE --direction left|right|up|down] [--session NAME] [--session-dir ROOT] [--attach|--observe] [--epoch HEX] [-- COMMAND ARG ...]",
    "workspace active|panes H [-V VARIABLE]; panes columns: pane tab window active visible sync x y width height.",
    "workspace bind H [PREFIX] CHORD ACTION; action H -V VARIABLE: NAME PANE, status 1 when empty.",
    "workspace navigate H next-pane|previous-pane|last-pane|last-page|swap-next|swap-previous|page N; close-page H PANE.",
    "workspace chrome H 0|1; font H PANE DELTA adjusts the pane's font size.",
    "workspace listen H PATH [--read-only] [--pane ID | --terminate-prefix PREFIX] [--host-actions]: private same-user external control endpoint.",
    "workspace reload-complete H TICKET STATUS finishes an accepted host reload (0 succeeds).",
    "workspace pump H [-t MS]: 0 open, 1 closed, 2 error; pane exits are reported by status.",
    "workspace status|title H PANE [-V VARIABLE]; dump H PANE; send|paste H PANE TEXT",
    "workspace focus|zoom|remove H PANE; sync H PANE 0|1; tab H -1|1",
    "workspace neighbor H PANE left|right|up|down [-V VARIABLE]; move H PANE left|right|up|down",
    "workspace relocate H PANE TARGET left|right|up|down moves an existing pane onto a target edge",
    "workspace reset-sizes H PANE restores half shares in that page without changing topology",
    "workspace rename H PANE TITLE; page-title H PANE [-V VAR]; rename-prompt H PANE",
    "workspace pane-rename H PANE TITLE; pane-rename-prompt|pane-reset-title|pane-copy-title|pane-clear H PANE",
    "workspace layout H PANE next|splits|stack|tall|grid changes the page arrangement",
    "workspace window-size H WIDTH HEIGHT sets logical window dimensions",
    "workspace layout-check H HEX [-V VARIABLE]; layout-apply H HEX SAVED CURRENT [SAVED CURRENT ...]",
    "workspace chrome-buttons H MASK sets nine pane button visibility bits (0-511)",
    "workspace chrome-edge H top|bottom places the page strip",
    "workspace settings-result H clear|saved|save-failed|reload-failed updates overlay feedback",
    "workspace settings-recording H ENABLED SIZE GRAPHICS RECENT ARCHIVE updates shared-policy display",
    "workspace message H TEXT opens an error message; empty clears; -V VARIABLE reads it",
    "workspace menu H toggles Start; start-badge H 0|1 shows the page-strip button",
    "workspace settings H [--edge-locked] opens presentation and recording settings",
    "workspace choose H panes|pages opens a keyboard/mouse chooser (requires chrome)",
    "workspace resize-mode H PANE enters interactive resize; Enter keeps, Escape cancels",
    "workspace resize H PANE horizontal|vertical DELTA (basis points); capture H PATH.ppm; close H",
    "Handles belong to the Bash process that loaded this builtin.",
    "Closing a named session window detaches; terminate explicitly stops it.",NULL};
struct builtin batty_struct={"batty",batty_builtin,BUILTIN_ENABLED,batty_doc,
    "batty new|attach|list|terminate|pump|send|paste|resize|dump|capture|status|info|title|graphics|close ARGS",0};
