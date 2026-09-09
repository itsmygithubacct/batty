/* SPDX-License-Identifier: MIT */
#include <config.h>
#include "loadables.h"
#include "window.h"
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
int batty_builtin(WORD_LIST *args) {
    if(!args) { builtin_usage(); return 2; }
    const char *verb=args->word->word; args=args->next;
    if(!strcmp(verb,"list") || !strcmp(verb,"terminate")) return admin(verb,args);
    if(getpid()!=controller) { builtin_error("run batty in its owning Bash process; subshell handles are invalid"); return 2; }
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
    "Handles belong to the Bash process that loaded this builtin.",
    "Closing a named session window detaches; terminate explicitly stops it.",NULL};
struct builtin batty_struct={"batty",batty_builtin,BUILTIN_ENABLED,batty_doc,
    "batty new|attach|list|terminate|pump|send|paste|resize|dump|capture|status|info|title|graphics|close ARGS",0};
