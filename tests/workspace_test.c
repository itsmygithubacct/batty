/* SPDX-License-Identifier: MIT */
#define _GNU_SOURCE
#include "workspace.h"
#include "remote.h"
#include "remote_internal.h"
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <termios.h>
#include <unistd.h>

extern char **environ;
static BtWorkspace *workspace;
static BtPaneInfo snapshot[BT_LAYOUT_PANES];
static unsigned count;
static void require(bool ok, const char *message) {
    if(ok) return;
    fprintf(stderr,"FAIL workspace: %s (%s)\n",message,workspace?bt_workspace_error(workspace):"not created");
    bt_workspace_free(workspace); exit(1);
}
static int child(const char *name) {
    struct termios term;
    if(tcgetattr(0,&term)) return 30;
    cfmakeraw(&term); if(tcsetattr(0,TCSANOW,&term)) return 31;
    setvbuf(stdout,NULL,_IONBF,0);
    printf("\033[?25lREADY:%s\r\n",name);
    unsigned char keys[256]; size_t used=0;
    unsigned char c;
    while(read(0,&c,1)==1) {
        if(c=='q') return 7;
        if(c=='g') {
            printf("\033[10;1H\033P0;1q\"1;1;64;36#1;2;100;0;0#1!64~-!64~-!64~-!64~-!64~-!64~\033\\"
                   "GRAPHICS_READY\r\n");
            continue;
        }
        if(c=='f' && !strcmp(name,"flood")) {
            char bytes[16384]; memset(bytes,'x',sizeof(bytes));
            while(write(1,bytes,sizeof(bytes))>0) {}
            return 0;
        }
        if(c=='~') { used=0; printf("\033[>15uKBD_READY\r\n"); continue; }
        if(c=='!') {
            printf("KEYS:");
            for(size_t i=0;i<used;++i) printf("%02x",keys[i]);
            printf("\r\n"); used=0; continue;
        }
        if(used<sizeof(keys)) keys[used++]=c;
        printf("GOT:%02x\r\n",c);
    }
    return 32;
}
static void collect(void *data, const BtPaneInfo *p) {
    (void)data;
    require(count<BT_LAYOUT_PANES,"snapshot bound"); snapshot[count++]=*p;
}
static BtPaneInfo info(uint64_t id) {
    count=0; bt_workspace_visit(workspace,collect,NULL);
    for(unsigned i=0;i<count;++i) if(snapshot[i].id==id) return snapshot[i];
    require(false,"pane appears in snapshot"); return (BtPaneInfo){0};
}
static bool contains(uint64_t id, const char *needle) {
    BtPaneInfo p=info(id); size_t length;
    char *text=bt_session_text(&p.view->session,false,&length);
    bool found=text && strstr(text,needle); free(text); return found;
}
static void until(uint64_t id, const char *needle) {
    uint64_t deadline=bt_millis()+5000;
    while(!contains(id,needle) && bt_millis()<deadline)
        require(!bt_workspace_pump(workspace,5),"pump workspace");
    require(contains(id,needle),needle);
}
static unsigned red_pixels(const char *path, bool right_half) {
    FILE *file=fopen(path,"rb"); require(file!=NULL,"open Pane Center graphics capture");
    char magic[3]={0}; int width=0,height=0,maximum=0;
    require(fscanf(file,"%2s %d %d %d%*c",magic,&width,&height,&maximum)==4 &&
            !strcmp(magic,"P6") && width>0 && height>0 && maximum==255,
            "read Pane Center graphics dimensions");
    size_t length=(size_t)width*height*3;
    unsigned char *pixels=malloc(length); require(pixels!=NULL,"allocate Pane Center graphics pixels");
    require(fread(pixels,1,length,file)==length && !fclose(file),"read Pane Center graphics pixels");
    unsigned red=0;
    for(int y=0;y<height;++y) for(int x=right_half?width/2:0;x<width;++x) {
        size_t at=((size_t)y*width+x)*3;
        if(pixels[at]>220 && pixels[at+1]<40 && pixels[at+2]<40) ++red;
    }
    free(pixels); return red;
}
static void key(char c) {
    uint32_t window=SDL_GetWindowID(bt_surface_window(bt_workspace_surface(workspace)));
    SDL_Event e={.type=SDL_KEYDOWN}; e.key.windowID=window;
    e.key.keysym=(SDL_Keysym){.sym=c,.scancode=SDL_SCANCODE_A+c-'a'};
    require(SDL_PushEvent(&e)==1,"queue workspace key");
    e=(SDL_Event){.type=SDL_TEXTINPUT}; e.text.windowID=window; e.text.text[0]=c;
    require(SDL_PushEvent(&e)==1,"queue workspace text");
}
static void chord(SDL_Scancode scan, SDL_Keycode keycode, SDL_Keymod modifiers, const char *text) {
    SDL_Event event={.type=SDL_KEYDOWN};
    event.key.keysym=(SDL_Keysym){.scancode=scan,.sym=keycode,.mod=modifiers};
    require(!bt_workspace_event(workspace,&event),"dispatch chord press");
    if(text) {
        SDL_Event input={.type=SDL_TEXTINPUT}; snprintf(input.text.text,sizeof(input.text.text),"%s",text);
        require(!bt_workspace_event(workspace,&input),"dispatch chord text");
    }
    event.type=SDL_KEYUP; require(!bt_workspace_event(workspace,&event),"dispatch chord release");
}
static void leader(void) { chord(SDL_SCANCODE_B,SDLK_b,KMOD_CTRL|KMOD_SHIFT,"B"); }
static void fairness(const char *executable, const char *helper) {
    char error[256];
    workspace=bt_workspace_new("Workspace scheduling",900,650,"monospace",16,error,sizeof(error));
    require(workspace!=NULL,"create scheduling workspace");
    char *args[]={(char *)executable,"--child","flood",NULL};
    BtPaneLaunch launch={.helper=helper,.argv=args,.env=environ};
    uint64_t busy[16],before[16],interactive;
    for(unsigned i=0;i<16;++i) {
        require(!bt_workspace_add(workspace,i==1?busy[0]:0,BT_RIGHT,&launch,&busy[i]),"add continuous output pane");
        until(busy[i],"READY:flood");
    }
    args[2]="interactive";
    require(!bt_workspace_add(workspace,0,BT_RIGHT,&launch,&interactive),"add interactive page after busy panes");
    until(interactive,"READY:interactive");
    for(unsigned i=0;i<16;++i) {
        BtSession *session=&info(busy[i]).view->session;
        before[i]=session->bytes_read;
        require(!bt_session_send(session,"f",1),"start continuous output");
    }
    BtWorkspacePumpStats initial=bt_workspace_pump_stats(workspace),stats=initial;
    key('k');
    uint64_t deadline=bt_millis()+5000;
    bool progressed=false;
    while(bt_millis()<deadline) {
        require(!bt_workspace_pump(workspace,0),"pump busy workspace in bounded rounds");
        stats=bt_workspace_pump_stats(workspace);
        require(stats.last_panes<=16,"workspace yields after at most sixteen pane pumps");
        progressed=contains(interactive,"GOT:6b");
        for(unsigned i=0;i<16;++i)
            if(info(busy[i]).view->session.bytes_read<before[i]+4096) progressed=false;
        if(progressed && stats.budget_yields>initial.budget_yields) break;
    }
    require(progressed,"all continuous producers and interactive input make progress");
    require(stats.budget_yields>initial.budget_yields,"busy workspace exercises round-robin budget yields");
    printf("PASS workspace fairness: 16 continuous producers, interactive page, %llu budget yields, max I/O pass %llu ms\n",
           (unsigned long long)(stats.budget_yields-initial.budget_yields),(unsigned long long)stats.max_io_ms);
    bt_workspace_free(workspace); workspace=NULL;
}
static char checkpoint_root[PATH_MAX];
static pid_t stopped_checkpoint_owner;
static void checkpoint_cleanup(void) {
    if(stopped_checkpoint_owner>0) {
        (void)kill(stopped_checkpoint_owner,SIGCONT);
        stopped_checkpoint_owner=0;
    }
    if(!*checkpoint_root) return;
    for(unsigned i=0;i<4;++i) {
        char name[32],error[256]; snprintf(name,sizeof(name),"restore%u",i);
        (void)bt_remote_terminate(checkpoint_root,name,error,sizeof(error));
    }
    (void)rmdir(checkpoint_root); checkpoint_root[0]=0;
}
static uint64_t original_id(const BtPaneMapping *map, uint64_t id) {
    if(!id) return 0;
    for(unsigned i=0;i<4;++i) if(map[i].current==id) return map[i].saved;
    return id;
}
static void same_tree(const BtLayout *a, const BtLayout *b, const BtPaneMapping *map) {
    require(a->root==b->root && a->count==b->count,"restored tree root and count");
    for(unsigned i=0;i<BT_LAYOUT_NODES;++i) {
        const BtLayoutNode *x=&a->nodes[i],*y=&b->nodes[i];
        require(x->used==y->used,"restored node occupancy");
        if(!x->used) continue;
        require(x->pane==original_id(map,y->pane) && x->parent==y->parent && x->first==y->first &&
            x->second==y->second && x->ratio==y->ratio && x->horizontal==y->horizontal,
            "restored exact tree topology and ratios");
    }
}
static void same_checkpoint(const BtWorkspaceLayout *a, const BtWorkspaceLayout *b, const BtPaneMapping *map) {
    require(a->version==b->version && a->page_count==b->page_count && a->pane_count==b->pane_count &&
        a->active_page==b->active_page && a->previous_page==b->previous_page,"restored page ordering and focus");
    for(unsigned i=0;i<a->page_count;++i) {
        const BtWorkspacePageLayout *x=&a->pages[i],*y=&b->pages[i];
        require(x->active==original_id(map,y->active) && x->zoom==original_id(map,y->zoom) &&
            x->previous==original_id(map,y->previous) && x->mode==y->mode && !strcmp(x->title,y->title),
            "restored page selection, zoom, history, mode and title");
        same_tree(&x->splits,&y->splits,map); same_tree(&x->tall,&y->tall,map); same_tree(&x->grid,&y->grid,map);
    }
    for(unsigned i=0;i<a->pane_count;++i) {
        unsigned j=0;
        while(j<b->pane_count && a->panes[i].id!=original_id(map,b->panes[j].id)) ++j;
        require(j<b->pane_count && a->panes[i].font_size==b->panes[j].font_size &&
            a->panes[i].synchronized==b->panes[j].synchronized &&
            !strcmp(a->panes[i].title,b->panes[j].title),
            "restored per-pane font, synchronized input and title override");
    }
}
static BtWorkspaceLayout *codec_roundtrip(const BtWorkspaceLayout *saved, const BtPaneMapping *map) {
    uint8_t *bytes=NULL; size_t length=0;
    require(!bt_workspace_layout_pack(saved,&bytes,&length) && length<=BT_WORKSPACE_LAYOUT_MAX_BYTES,
        "pack portable workspace layout");
    require(length>8 && !memcmp(bytes,"BWL2",4) && bytes[4]==saved->page_count && bytes[5]==saved->pane_count,
        "layout wire magic and bounded counts");
    for(unsigned i=0;i<8;++i) require(bytes[8+i]==(uint8_t)(saved->panes[0].id>>(8*i)),"layout IDs are little endian");
    BtWorkspaceLayout *decoded=NULL;
    for(size_t cut=0;cut<length;++cut) {
        decoded=(BtWorkspaceLayout *)(uintptr_t)1;
        require(bt_workspace_layout_unpack(bytes,cut,&decoded)==-1 && decoded==NULL,"reject every truncated layout");
    }
    uint8_t *changed=malloc(length+1); require(changed!=NULL,"allocate malformed wire fixture");
    memcpy(changed,bytes,length); changed[length]=0;
    require(bt_workspace_layout_unpack(changed,length+1,&decoded)==-1 && !decoded,"reject trailing layout bytes");
    memcpy(changed,"BWL3",4);
    require(bt_workspace_layout_unpack(changed,length,&decoded)==-1 && !decoded,"reject unknown layout format");
    memcpy(changed,bytes,length); changed[17]=2;
    require(bt_workspace_layout_unpack(changed,length,&decoded)==-1 && !decoded,"reject noncanonical sync flag");
    /* Every accepted byte mutation must roundtrip as the exact canonical wire
     * value, not be truncated, normalized silently or interpreted as commands. */
    for(size_t at=0;at<length;++at) {
        memcpy(changed,bytes,length); changed[at]^=0x80;
        if(bt_workspace_layout_unpack(changed,length,&decoded)) { require(!decoded,"failed decode clears output"); continue; }
        uint8_t *again=NULL; size_t n=0;
        require(!bt_workspace_layout_validate(decoded) && !bt_workspace_layout_pack(decoded,&again,&n) &&
            n==length && !memcmp(again,changed,n),"accepted wire mutations remain canonical valid layouts");
        free(again); free(decoded); decoded=NULL;
    }
    free(changed);
    require(!bt_workspace_layout_unpack(bytes,length,&decoded),"decode complete layout without a workspace");
    same_checkpoint(saved,decoded,map);
    memset(bytes,0,length); free(bytes); /* decoded strings/nodes own their storage */
    BtWorkspaceLayout *legacy=malloc(sizeof(*legacy)); require(legacy!=NULL,"allocate legacy layout");
    *legacy=*saved; legacy->version=1;
    for(unsigned i=0;i<legacy->pane_count;++i) legacy->panes[i].title[0]=0;
    uint8_t *old=NULL; size_t old_length=0;
    require(!bt_workspace_layout_pack(legacy,&old,&old_length) && !memcmp(old,"BWL1",4),
            "pack old BWL1 layout without pane title overrides");
    BtWorkspaceLayout *old_decoded=NULL;
    require(!bt_workspace_layout_unpack(old,old_length,&old_decoded) && old_decoded->version==1,
            "read existing BWL1 checkpoints");
    same_checkpoint(legacy,old_decoded,map);
    free(old_decoded); free(old); free(legacy);
    return decoded;
}
static void codec_limits(void) {
    BtWorkspaceLayout *state=calloc(1,sizeof(*state)); require(state!=NULL,"allocate maximum layout fixture");
    for(unsigned pages=1;pages<=BT_WORKSPACE_TABS;pages+=BT_WORKSPACE_TABS-1) {
        memset(state,0,sizeof(*state)); state->version=2; state->pane_count=BT_LAYOUT_PANES;
        state->page_count=pages; state->previous_page=-1;
        for(unsigned i=0;i<BT_LAYOUT_PANES;++i) {
            state->panes[i].id=i+1; state->panes[i].font_size=96; state->panes[i].synchronized=true;
            memset(state->panes[i].title,'P',255);
        }
        for(unsigned page=0;page<pages;++page) {
            BtWorkspacePageLayout *p=&state->pages[page]; unsigned n=BT_LAYOUT_PANES/pages;
            memset(p->title,'T',255); p->active=page*n+1;
            bt_layout_init(&p->splits);
            uint64_t ids[BT_LAYOUT_PANES];
            for(unsigned i=0;i<n;++i) {
                ids[i]=page*n+i+1;
                require(!bt_layout_split(&p->splits,i?ids[i-1]:0,ids[i],BT_RIGHT),"construct maximum/deep layout");
            }
            require(!bt_layout_arrange(&p->tall,ids,n,false) && !bt_layout_arrange(&p->grid,ids,n,true),"construct maximum derived layouts");
        }
        uint8_t *bytes=NULL; size_t n=0; BtWorkspaceLayout *decoded=NULL;
        require(!bt_workspace_layout_pack(state,&bytes,&n) && n<=BT_WORKSPACE_LAYOUT_MAX_BYTES &&
            !bt_workspace_layout_unpack(bytes,n,&decoded),"maximum pane/page/title layouts fit the portable bound");
        free(decoded); free(bytes);
    }
    free(state);
}
static void checkpoints(const char *executable, const char *helper) {
    char service[PATH_MAX],error[256];
    require(realpath("build/batty-state",service)!=NULL,"checkpoint service");
    const char *tmp=getenv("TMPDIR");
    if(!tmp || !*tmp) tmp="/tmp";
    require(snprintf(checkpoint_root,sizeof(checkpoint_root),"%s/bt-layout-XXXXXX",tmp)<
            (int)sizeof(checkpoint_root),"bounded checkpoint root");
    require(mkdtemp(checkpoint_root)!=NULL && !atexit(checkpoint_cleanup),"checkpoint private root");
    BtWorkspaceLayout *saved=calloc(1,sizeof(*saved)),*actual=calloc(1,sizeof(*actual)),*bad=calloc(1,sizeof(*bad));
    require(saved && actual && bad,"allocate checkpoint values");
    workspace=bt_workspace_new("Checkpoint",1200,850,"monospace",16,error,sizeof(error));
    require(workspace!=NULL,"checkpoint source window");
    require(!bt_workspace_chrome_buttons(workspace,0) && !bt_workspace_chrome(workspace,true),"checkpoint source chrome");
    BtPaneMapping map[4]; pid_t children[4]; uint64_t epochs[4];
    for(unsigned i=0;i<4;++i) {
        char name[32]; snprintf(name,sizeof(name),"restore%u",i);
        char *args[]={(char *)executable,"--child",name,NULL};
        BtPaneLaunch launch={.helper=helper,.service=service,.session_dir=checkpoint_root,
            .session_name=name,.argv=args,.env=environ};
        require(!bt_workspace_add(workspace,i==1 || i==2?map[0].saved:0,i==1?BT_RIGHT:BT_DOWN,&launch,&map[i].saved),
            "create persistent checkpoint pane");
        until(map[i].saved,name); children[i]=info(map[i].saved).view->session.child;
        BtPaneInfo descriptor=info(map[i].saved);
        epochs[i]=descriptor.session_epoch;
        require(epochs[i] && !strcmp(descriptor.session_dir,checkpoint_root) && !strcmp(descriptor.session_name,name),
            "owned session descriptor includes root, name and epoch");
        map[i].current=map[i].saved;
    }
    require(!bt_workspace_rename(workspace,map[0].saved,"Build λ") &&
        !bt_workspace_rename(workspace,map[3].saved,"Logs"),"checkpoint page titles");
    require(!bt_workspace_pane_rename(workspace,map[1].saved,"Build pane λ"),"checkpoint pane title override");
    require(!bt_workspace_font(workspace,map[1].saved,3) &&
        !bt_workspace_synchronize(workspace,map[0].saved,true),"checkpoint pane preferences");
    require(!bt_workspace_focus(workspace,map[0].saved) &&
        !bt_workspace_resize(workspace,map[0].saved,true,900) &&
        !bt_workspace_resize(workspace,map[0].saved,false,-700),"checkpoint custom splits");
    require(!bt_workspace_layout(workspace,map[0].saved,2) &&
        !bt_workspace_resize(workspace,map[0].saved,true,1300),"checkpoint custom tall layout");
    require(!bt_workspace_layout(workspace,map[0].saved,3) &&
        !bt_workspace_resize(workspace,map[0].saved,true,-1100),"checkpoint custom grid layout");
    require(!bt_workspace_focus(workspace,map[2].saved) && !bt_workspace_zoom(workspace,map[2].saved),"checkpoint selected and zoomed pane");
    require(!bt_workspace_layout_capture(workspace,saved),"capture independent workspace layout");
    BtWorkspaceLayout *decoded=codec_roundtrip(saved,map);
    free(saved); saved=decoded;
    codec_limits();
    *bad=*saved;
    uint64_t surviving[]={map[1].saved,map[3].saved};
    require(!bt_workspace_layout_prune(bad,surviving,2) && bad->pane_count==2 && bad->page_count==2 &&
            bad->pages[0].active==map[1].saved && !bad->pages[0].zoom &&
            !strcmp(bad->panes[0].title,"Build pane λ"),
            "prune closed split panes without losing page/title state or leaving stale zoom");
    *actual=*bad;
    uint64_t duplicate[]={map[1].saved,map[1].saved},unknown[]={UINT64_MAX};
    require(bt_workspace_layout_prune(bad,duplicate,2)==-1 && !memcmp(bad,actual,sizeof(*bad)) &&
            bt_workspace_layout_prune(bad,unknown,1)==-1 && !memcmp(bad,actual,sizeof(*bad)) &&
            bt_workspace_layout_prune(bad,surviving,0)==-1 && !memcmp(bad,actual,sizeof(*bad)),
            "invalid pruning preserves the original checkpoint");
    *bad=*saved;
    require(!bt_workspace_layout_prune(bad,&map[3].saved,1) && bad->page_count==1 && bad->active_page==0 &&
            bad->previous_page==0 && bad->pages[0].active==map[3].saved && !strcmp(bad->pages[0].title,"Logs"),
            "prune an entire active page and repair page focus");
    *bad=*saved;
    require(!bt_workspace_layout_prune(bad,&map[1].saved,1) && bad->page_count==1 && bad->active_page==0 &&
            bad->previous_page==-1 && bad->pages[0].active==map[1].saved && !bad->pages[0].previous,
            "prune the previous page and previous pane without leaving stale focus references");
    /* Validation failure must preserve a live source, including a modal prompt. */
    require(!bt_workspace_message(workspace,"Keep this message"),"checkpoint validation sentinel");
    for(unsigned fault=0;fault<13;++fault) {
        *bad=*saved;
        BtPaneMapping invalid_map[4]; memcpy(invalid_map,map,sizeof(map));
        BtLayout *tree=&bad->pages[0].splits;
        switch(fault) {
            case 0: bad->version=99; break;
            case 1: tree->nodes[tree->root].first=tree->root; break;
            case 2: tree->nodes[tree->root].first=BT_LAYOUT_NODES; break;
            case 3: tree->nodes[tree->root].ratio=0; break;
            case 4: tree->count=BT_LAYOUT_PANES+1; break;
            case 5: invalid_map[1].current=invalid_map[0].current; break;
            case 6: invalid_map[1].saved=invalid_map[0].saved; break;
            case 7: memset(bad->pages[0].title,'x',sizeof(bad->pages[0].title)); break;
            case 8: bad->pages[0].active=UINT64_MAX; break;
            case 9: bad->panes[0].font_size=5; break;
            case 10: bad->pages[1]=bad->pages[0]; break;
            case 11: tree->nodes[BT_LAYOUT_NODES-1]=(BtLayoutNode){.used=true,.pane=999,.first=-1,.second=-1}; break;
            case 12: memset(bad->panes[1].title,'x',sizeof(bad->panes[1].title)); break;
        }
        require(bt_workspace_layout_apply(workspace,bad,invalid_map,4)==-1,"reject malformed checkpoint");
        require(!strcmp(bt_workspace_message_text(workspace),"Keep this message"),"rejected checkpoint preserves interaction");
        require(!bt_workspace_layout_capture(workspace,actual),"capture after checkpoint rejection");
        same_checkpoint(saved,actual,map);
    }
    int owner_socket=bt_wire_connect(checkpoint_root,"restore0");
    require(owner_socket>=0,"connect checkpoint owner for shutdown probe");
    struct ucred owner; socklen_t owner_size=sizeof(owner);
    require(!getsockopt(owner_socket,SOL_SOCKET,SO_PEERCRED,&owner,&owner_size) &&
            owner.uid==getuid() && owner.pid>0,"identify checkpoint owner");
    close(owner_socket);
    stopped_checkpoint_owner=owner.pid;
    require(!kill(stopped_checkpoint_owner,SIGSTOP),"pause checkpoint owner before frontend shutdown");
    char owner_status[64]; snprintf(owner_status,sizeof(owner_status),"/proc/%ld/status",(long)stopped_checkpoint_owner);
    bool paused=false; uint64_t pause_deadline=bt_millis()+1000;
    while(!paused && bt_millis()<pause_deadline) {
        FILE *status=fopen(owner_status,"r");
        if(status) {
            char state[1024]; size_t n=fread(state,1,sizeof(state)-1,status); state[n]=0;
            paused=strstr(state,"\nState:\tT")!=NULL; fclose(status);
        }
        if(!paused) (void)poll(NULL,0,1);
    }
    require(paused,"checkpoint owner is stopped before input queues");
    require(!bt_workspace_focus(workspace,map[0].saved),"select paused owner for Pane Center preview");
    require(!bt_workspace_pane_center(workspace),"open Pane Center over paused owner");
    uint64_t preview_started=bt_millis();
    require(!bt_workspace_draw(workspace,false) && !bt_workspace_pump(workspace,0),
            "Pane Center keeps pumping with a paused preview owner");
    require(bt_millis()-preview_started<750,"Pane Center preview never waits for a paused owner");
    require(!bt_session_send(&info(map[0].saved).view->session,"p",1),
            "queue input while checkpoint owner is paused");
    uint64_t closing=bt_millis();
    bt_workspace_free(workspace); workspace=NULL;
    uint64_t close_ms=bt_millis()-closing;
    require(!kill(stopped_checkpoint_owner,SIGCONT),"resume checkpoint owner after frontend shutdown");
    stopped_checkpoint_owner=0;
    require(close_ms<1000,"frontend shutdown does not wait for a paused owner");
    workspace=bt_workspace_new("Restored",1200,850,"monospace",16,error,sizeof(error));
    require(workspace!=NULL,"new checkpoint destination window");
    require(!bt_workspace_chrome_buttons(workspace,0) && !bt_workspace_chrome(workspace,true),"checkpoint destination chrome");
    for(unsigned i=0;i<4;++i) {
        char name[32]; snprintf(name,sizeof(name),"restore%u",i);
        BtPaneLaunch launch={.session_dir=checkpoint_root,.session_name=name,.attach=true,.expected_epoch=epochs[i]};
        require(!bt_workspace_add(workspace,0,BT_RIGHT,&launch,&map[i].current),"reattach surviving checkpoint process");
        require(map[i].current!=map[i].saved && info(map[i].current).view->session.child==children[i],
            "fresh pane identity retains original child process");
        until(map[i].current,name);
    }
    require(!bt_workspace_layout_apply(workspace,saved,map,4),"apply checkpoint to freshly attached views");
    require(!bt_workspace_layout_capture(workspace,actual),"capture restored workspace");
    same_checkpoint(saved,actual,map);
    require(!bt_workspace_draw(workspace,true),"render restored zoomed page");
    require(!bt_workspace_zoom(workspace,map[2].current),"unzoom restored page");
    for(int mode=0;mode<4;++mode) {
        require(!bt_workspace_layout(workspace,map[0].current,mode) && !bt_workspace_draw(workspace,true),
            "render each retained page arrangement");
    }
    require(!bt_workspace_focus(workspace,map[3].current) && !bt_workspace_draw(workspace,true),"render restored second page");
    bt_workspace_free(workspace); workspace=NULL;
    require(!bt_remote_terminate(checkpoint_root,"restore0",error,sizeof(error)),"stop original checkpoint owner");
    char *replacement_args[]={(char *)executable,"--child","replacement",NULL};
    uint64_t replacement_epoch=0;
    require(!bt_remote_create_owned(service,helper,checkpoint_root,"restore0",replacement_args,environ,
        80,24,8,16,error,sizeof(error),&replacement_epoch),"create replacement under saved name");
    require(replacement_epoch && replacement_epoch!=epochs[0],"replacement has a new owner epoch");
    workspace=bt_workspace_new("Reject stale restore",900,650,"monospace",16,error,sizeof(error));
    require(workspace!=NULL,"stale restore destination");
    BtPaneLaunch stale={.session_dir=checkpoint_root,.session_name="restore0",.attach=true,.expected_epoch=epochs[0]};
    uint64_t rejected=123;
    require(bt_workspace_add(workspace,0,BT_RIGHT,&stale,&rejected)==-1 && !rejected,
        "reject reused session name without publishing a pane");
    count=0; bt_workspace_visit(workspace,collect,NULL); require(count==0,"stale restore leaves destination empty");
    BtSession observer;
    require(!bt_remote_attach_epoch(&observer,checkpoint_root,"restore0",true,replacement_epoch),"observe replacement owner");
    require(observer.cols==80 && observer.rows==24 && observer.bytes_written==0,
        "rejected restore does not resize or send input to replacement");
    bt_session_close(&observer);
    stale.expected_epoch=replacement_epoch;
    require(!bt_workspace_add(workspace,0,BT_RIGHT,&stale,&rejected),"replacement still accepts its correct controller");
    until(rejected,"replacement");
    bt_workspace_free(workspace); workspace=NULL;
    checkpoint_cleanup(); free(saved); free(actual); free(bad);
    puts("PASS workspace checkpoint: exact mapped layouts, titles, fonts, focus and sync survive frontend replacement; malformed state rejected");
}
static void process_image_budget_test(const char *executable, const char *helper) {
    char error[256];
    char *args[]={(char *)executable,"--child","image-budget",NULL};
    BtPaneLaunch launch={.helper=helper,.argv=args,.env=environ};
    workspace=bt_workspace_new("First image window",900,650,"monospace",16,error,sizeof(error));
    require(workspace!=NULL,"create first image workspace");
    BtWorkspace *first_workspace=workspace;
    uint64_t first;
    require(!bt_workspace_add(workspace,0,BT_RIGHT,&launch,&first),"add first image pane");
    until(first,"READY:image-budget");
    require(!bt_session_send(&info(first).view->session,"g",1),"upload first image");
    until(first,"GRAPHICS_READY");
    require(!bt_workspace_draw(workspace,false),"draw first image workspace");
    BtImageStats first_stats=bt_renderer_image_stats(info(first).view->renderer);
    BtRenderer *first_renderer=info(first).view->renderer;
    size_t first_bytes=first_stats.texture_bytes+first_stats.shadow_bytes;
    require(first_bytes>0,"first workspace image cache populated");

    workspace=bt_workspace_new("Second image window",900,650,"monospace",16,error,sizeof(error));
    require(workspace!=NULL,"create second image workspace");
    BtWorkspace *second_workspace=workspace;
    uint64_t second;
    require(!bt_workspace_add(workspace,0,BT_RIGHT,&launch,&second),"add second image pane");
    until(second,"READY:image-budget");
    require(!bt_session_send(&info(second).view->session,"g",1),"upload second image");
    until(second,"GRAPHICS_READY");
    require(!bt_workspace_draw(workspace,false),"draw second image workspace");
    BtImageStats second_stats=bt_renderer_image_stats(info(second).view->renderer);
    BtRenderer *second_renderer=info(second).view->renderer;
    size_t second_bytes=second_stats.texture_bytes+second_stats.shadow_bytes;
    require(second_bytes>0 && bt_workspace_process_image_cache_bytes()==first_bytes+second_bytes,
            "process accounting includes both windows' textures and shadows");

    SDL_Event event={.type=SDL_WINDOWEVENT};
    event.window.event=SDL_WINDOWEVENT_FOCUS_LOST;
    require(!bt_workspace_event(first_workspace,&event),"unfocus first image window");
    event.window.event=SDL_WINDOWEVENT_FOCUS_GAINED;
    require(!bt_workspace_event(second_workspace,&event),"focus second image window");
    size_t budget=first_bytes>second_bytes?first_bytes:second_bytes;
    require(!bt_workspace_process_image_budget(second_workspace,budget),"set shared image budget");
    require(!bt_renderer_image_stats(first_renderer).texture_bytes &&
            bt_renderer_image_stats(second_renderer).texture_bytes==second_stats.texture_bytes &&
            bt_workspace_process_image_cache_bytes()<=budget,
            "process budget evicts the unfocused window first");

    event.window.event=SDL_WINDOWEVENT_FOCUS_LOST;
    require(!bt_workspace_event(second_workspace,&event),"unfocus second image window");
    event.window.event=SDL_WINDOWEVENT_FOCUS_GAINED;
    require(!bt_workspace_event(first_workspace,&event),"focus first image window");
    require(!bt_workspace_draw(first_workspace,false),"redraw evicted first window from retained image");
    require(bt_renderer_image_stats(first_renderer).texture_bytes==first_stats.texture_bytes &&
            !bt_renderer_image_stats(second_renderer).texture_bytes &&
            bt_workspace_process_image_cache_bytes()<=budget,
            "redrawing first window recreates its image and evicts the other window");
    require(!bt_surface_capture(bt_workspace_surface(first_workspace),"build/workspace-global-first.ppm") &&
            red_pixels("build/workspace-global-first.ppm",false)>100,
            "evicted first window restores its red image on the framebuffer");
    event.window.event=SDL_WINDOWEVENT_FOCUS_LOST;
    require(!bt_workspace_event(first_workspace,&event),"unfocus first image window again");
    event.window.event=SDL_WINDOWEVENT_FOCUS_GAINED;
    require(!bt_workspace_event(second_workspace,&event),"refocus second image window");
    require(!bt_workspace_draw(second_workspace,false) &&
            bt_renderer_image_stats(second_renderer).texture_bytes==second_stats.texture_bytes &&
            bt_workspace_process_image_cache_bytes()<=budget,
            "redrawing second window recreates its retained image within the process budget");
    require(!bt_surface_capture(bt_workspace_surface(second_workspace),"build/workspace-global-second.ppm") &&
            red_pixels("build/workspace-global-second.ppm",false)>100,
            "evicted second window restores its red image on the framebuffer");
    require(!bt_workspace_process_image_budget(first_workspace,256u*1024u*1024u),
            "restore default process image budget");
    bt_workspace_free(second_workspace);
    bt_workspace_free(first_workspace); workspace=NULL;
    require(!bt_workspace_process_image_cache_bytes(),"closed windows release aggregate image cache");
    puts("PASS process-wide image cache bounds two GL windows and restores evicted images");
}
int main(int argc, char **argv) {
    if(argc==3 && !strcmp(argv[1],"--child")) return child(argv[2]);
    char executable[PATH_MAX],helper[PATH_MAX],error[256];
    require(realpath(argv[0],executable)!=NULL,"test executable");
    require(realpath("build/batty-session",helper)!=NULL,"session helper");
    workspace=bt_workspace_new("Batty workspace",900,650,"monospace",16,error,sizeof(error));
    require(workspace!=NULL,"create workspace");
    char *args[]={executable,"--child","pane",NULL};
    BtPaneLaunch launch={.helper=helper,.argv=args,.env=environ};
    uint64_t first,second,third,fourth;
    require(!bt_workspace_add(workspace,0,BT_RIGHT,&launch,&first),"first tab");
    require(!bt_workspace_add(workspace,first,BT_RIGHT,&launch,&second),"right pane");
    require(!bt_workspace_add(workspace,first,BT_DOWN,&launch,&third),"lower pane");
    until(first,"READY:pane"); until(second,"READY:pane"); until(third,"READY:pane");
    char *missing[]={"/nonexistent/batty-command",NULL};
    BtPaneLaunch invalid=launch; invalid.argv=missing;
    uint64_t rejected=0;
    require(bt_workspace_add(workspace,0,BT_RIGHT,&invalid,&rejected)==-1 && !rejected,"failed exec does not publish a pane");
    (void)info(first); require(count==3,"failed exec preserves existing panes and tabs");
    BtPaneInfo a=info(first),b=info(second),c=info(third);
    require(a.tab==b.tab && b.tab==c.tab && a.bounds.x<b.bounds.x && a.bounds.y<c.bounds.y,"native split tree geometry");
    int old_height=a.bounds.height;
    require(!bt_workspace_resize(workspace,first,false,1200),"resize nested split");
    require(info(first).bounds.height>old_height,"pane height increased");
    uint64_t neighbor=0;
    require(!bt_workspace_neighbor(workspace,first,BT_DOWN,&neighbor) && neighbor==third,"lower neighbor follows layout");
    require(!bt_workspace_neighbor(workspace,third,BT_RIGHT,&neighbor) && neighbor==second,"right neighbor overlaps source");

    SDL_Window *window=bt_surface_window(bt_workspace_surface(workspace));
    int lw,lh,pw,ph; SDL_GetWindowSize(window,&lw,&lh); SDL_GL_GetDrawableSize(window,&pw,&ph);
    b=info(second);
    SDL_Event mouse={.type=SDL_MOUSEBUTTONDOWN}; mouse.button.windowID=SDL_GetWindowID(window);
    mouse.button.button=SDL_BUTTON_LEFT;
    mouse.button.x=(b.bounds.x+20)*lw/pw; mouse.button.y=(b.bounds.y+20)*lh/ph;
    require(SDL_PushEvent(&mouse)==1,"click second pane");
    mouse.type=SDL_MOUSEBUTTONUP; require(SDL_PushEvent(&mouse)==1,"release second pane");
    require(!bt_workspace_pump(workspace,0),"dispatch pointer focus");
    require(bt_workspace_active(workspace)==second,"click changes pane focus");
    key('a'); until(second,"GOT:61");
    require(!contains(first,"GOT:61") && !contains(third,"GOT:61"),"typing reaches focused pane only");
    require(!bt_workspace_synchronize(workspace,first,true) && !bt_workspace_synchronize(workspace,second,true),"select synchronized peers");
    require(!bt_workspace_focus(workspace,first),"focus synchronized pane");
    key('b'); until(first,"GOT:62"); until(second,"GOT:62");
    require(!contains(third,"GOT:62"),"synchronization excludes unselected panes");

    require(!bt_workspace_synchronize(workspace,first,false) && !bt_workspace_synchronize(workspace,first,true),
            "changing synchronization releases old input state");
    a=info(first); b=info(second);
    require(!bt_session_send(&a.view->session,"~",1) && !bt_session_send(&b.view->session,"~",1),"enable application keyboard protocol");
    until(first,"KBD_READY"); until(second,"KBD_READY");
    key('d'); until(first,"GOT:75"); until(second,"GOT:75");
    require(!bt_workspace_focus(workspace,third),"focus away from synchronized application panes");
    require(!bt_session_send(&a.view->session,"!",1) && !bt_session_send(&b.view->session,"!",1),"inspect synchronized key lifecycle");
    until(first,"KEYS:1b5b313030751b5b3130303b313a3375");
    until(second,"KEYS:1b5b313030751b5b3130303b313a3375");
    SDL_Event special={.type=SDL_KEYDOWN}; special.key.keysym.scancode=SDL_SCANCODE_PRINTSCREEN;
    special.key.keysym.sym=SDLK_PRINTSCREEN;
    require(!bt_window_event(a.view,&special),"send Print Screen key press to Kitty keyboard-mode app");
    require(!bt_session_send(&a.view->session,"!",1),"inspect Print Screen key press encoding");
    until(first,"KEYS:1b5b353733363175");
    special.type=SDL_KEYUP;
    require(!bt_window_event(a.view,&special),"send Print Screen key release to Kitty keyboard-mode app");
    require(!bt_session_send(&a.view->session,"!",1),"inspect Print Screen key release encoding");
    until(first,"KEYS:1b5b35373336313b313a3375");
    uint64_t composing_written=a.view->session.bytes_written;
    SDL_Event compose={.type=SDL_KEYDOWN};
    compose.key.keysym=(SDL_Keysym){.scancode=SDL_SCANCODE_A,.sym=SDLK_a};
    require(!bt_window_event(a.view,&compose),"begin key that starts IME composition");
    compose=(SDL_Event){.type=SDL_TEXTEDITING};
    snprintf(compose.edit.text,sizeof(compose.edit.text),"λ");
    require(!bt_window_event(a.view,&compose),"start IME composition in Kitty keyboard mode");
    compose=(SDL_Event){.type=SDL_KEYDOWN};
    compose.key.keysym=(SDL_Keysym){.scancode=SDL_SCANCODE_LEFT,.sym=SDLK_LEFT};
    require(!bt_window_event(a.view,&compose),"IME navigation key press");
    compose.type=SDL_KEYUP;
    require(!bt_window_event(a.view,&compose) && a.view->session.bytes_written==composing_written,
            "IME navigation stays out of Kitty keyboard stream");
    compose=(SDL_Event){.type=SDL_TEXTINPUT};
    snprintf(compose.text.text,sizeof(compose.text.text),"λx");
    require(!bt_window_event(a.view,&compose),"commit multi-codepoint IME text");
    compose=(SDL_Event){.type=SDL_KEYUP};
    compose.key.keysym=(SDL_Keysym){.scancode=SDL_SCANCODE_A,.sym=SDLK_a};
    require(!bt_window_event(a.view,&compose),"release composition starter key");
    require(!bt_session_send(&a.view->session,"!",1),"inspect IME text in Kitty keyboard mode");
    until(first,"KEYS:cebb78");
    compose=(SDL_Event){.type=SDL_KEYDOWN};
    compose.key.keysym=(SDL_Keysym){.scancode=SDL_SCANCODE_A,.sym=SDLK_a};
    require(!bt_window_event(a.view,&compose),"begin key before direct multi-codepoint commit");
    compose=(SDL_Event){.type=SDL_TEXTINPUT};
    snprintf(compose.text.text,sizeof(compose.text.text),"λy");
    require(!bt_window_event(a.view,&compose),"deliver direct multi-codepoint text without preedit");
    compose=(SDL_Event){.type=SDL_KEYUP};
    compose.key.keysym=(SDL_Keysym){.scancode=SDL_SCANCODE_A,.sym=SDLK_a};
    require(!bt_window_event(a.view,&compose),"release direct-commit starter key");
    require(!bt_session_send(&a.view->session,"!",1),"inspect direct text in Kitty keyboard mode");
    until(first,"KEYS:cebb79");
    compose=(SDL_Event){.type=SDL_KEYDOWN};
    compose.key.keysym=(SDL_Keysym){.scancode=SDL_SCANCODE_A,.sym=SDLK_a};
    require(!bt_window_event(a.view,&compose),"begin key before direct single-codepoint IME commit");
    compose=(SDL_Event){.type=SDL_TEXTINPUT};
    snprintf(compose.text.text,sizeof(compose.text.text),"Ж");
    require(!bt_window_event(a.view,&compose),"deliver direct single-codepoint text without preedit");
    compose=(SDL_Event){.type=SDL_KEYUP};
    compose.key.keysym=(SDL_Keysym){.scancode=SDL_SCANCODE_A,.sym=SDLK_a};
    require(!bt_window_event(a.view,&compose),"release direct single-commit starter key");
    require(!bt_session_send(&a.view->session,"!",1),"inspect direct single text in Kitty keyboard mode");
    until(first,"KEYS:d096");
    bt_session_feed(&a.view->session,"\033[<u",4); bt_session_feed(&b.view->session,"\033[<u",4);
    require(!bt_workspace_focus(workspace,first),"restore focused synchronized pane");

    require(!bt_workspace_navigate(workspace,BT_NEXT_PANE,0) && bt_workspace_active(workspace)==second,"next pane navigation");
    require(!bt_workspace_navigate(workspace,BT_LAST_PANE,0) && bt_workspace_active(workspace)==first,"last pane history");
    require(!bt_workspace_navigate(workspace,BT_LAST_PANE,0) && bt_workspace_active(workspace)==second,"last pane toggles");
    require(!bt_workspace_navigate(workspace,BT_PREVIOUS_PANE,0) && bt_workspace_active(workspace)==first,"previous pane navigation");
    a=info(first); b=info(second);
    require(!bt_workspace_navigate(workspace,BT_SWAP_NEXT,0) && info(first).bounds.x==b.bounds.x,"swap forward retains pane identity");
    require(!bt_workspace_navigate(workspace,BT_SWAP_NEXT,0) && info(first).bounds.x==a.bounds.x,"swap back restores layout");
    a=info(first);
    uint64_t resize_bytes=a.view->session.bytes_written, peer_bytes=info(second).view->session.bytes_written;
    require(!bt_workspace_resize_mode(workspace,first),"begin interactive resize");
    chord(SDL_SCANCODE_RIGHT,SDLK_RIGHT,KMOD_NONE,NULL);
    require(info(first).bounds.width>a.bounds.width,"resize arrows grow focused pane");
    int grown_width=info(first).bounds.width;
    SDL_Event repeated={.type=SDL_KEYDOWN};
    repeated.key.keysym=(SDL_Keysym){.sym=SDLK_RIGHT,.scancode=SDL_SCANCODE_RIGHT}; repeated.key.repeat=1;
    require(!bt_workspace_event(workspace,&repeated) && info(first).bounds.width>grown_width,"held resize arrows repeat");
    chord(SDL_SCANCODE_J,SDLK_j,KMOD_NONE,"j");
    chord(SDL_SCANCODE_ESCAPE,SDLK_ESCAPE,KMOD_NONE,NULL);
    require(info(first).bounds.width==a.bounds.width,"Escape restores original split ratios");
    require(a.view->session.bytes_written==resize_bytes && info(second).view->session.bytes_written==peer_bytes,
            "resize mode consumes keys and text for focused and synchronized panes");
    require(!bt_workspace_resize_mode(workspace,first),"restart interactive resize");
    chord(SDL_SCANCODE_RIGHT,SDLK_RIGHT,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_RETURN,SDLK_RETURN,KMOD_NONE,NULL);
    require(info(first).bounds.width>a.bounds.width,"Enter retains resized layout");
    require(!bt_workspace_resize(workspace,first,true,-250),"restore accepted resize");
    require(!bt_workspace_resize_mode(workspace,first),"resize before focus change");
    chord(SDL_SCANCODE_RIGHT,SDLK_RIGHT,KMOD_NONE,NULL);
    require(!bt_workspace_focus(workspace,second),"focus change ends resize");
    require(info(first).bounds.width==a.bounds.width,"focus cancellation restores layout");
    require(!bt_workspace_focus(workspace,first),"restore resize source focus");
    require(!bt_workspace_resize_mode(workspace,first),"begin resize before reset");
    chord(SDL_SCANCODE_RIGHT,SDLK_RIGHT,KMOD_NONE,NULL);
    require(!bt_workspace_reset_sizes(workspace,first),"reset ends resize transaction");
    BtPaneInfo reset_first=info(first),reset_second=info(second);
    require(abs(reset_first.bounds.width-reset_second.bounds.width)<=1,"reset balances horizontal split");
    require(abs(reset_first.bounds.height-info(third).bounds.height)<=1,"reset balances nested vertical split");
    chord(SDL_SCANCODE_ESCAPE,SDLK_ESCAPE,KMOD_NONE,NULL);
    require(info(first).bounds.width==reset_first.bounds.width,"cancel cannot restore pre-reset ratios");
    require(!bt_workspace_resize(workspace,first,false,1200),"restore earlier vertical adjustment");
    require(!bt_workspace_bind_sequence(workspace,"Ctrl+Shift+B","O","next-pane"),"bind leader sequence");
    require(bt_workspace_bind(workspace,"Ctrl+Shift+B","ambiguous")==-1,"reject direct action on prefix");
    require(!bt_workspace_bind_sequence(workspace,"Ctrl+Shift+B","Shift+5","split-right"),"bind shifted leader suffix");
    BtWorkspaceAction sequence;
    leader(); chord(SDL_SCANCODE_O,SDLK_o,KMOD_NONE,"o");
    require(bt_workspace_action(workspace,&sequence)==1 && sequence.pane==first && !strcmp(sequence.name,"next-pane"),"leader queues one action with source pane");
    require(!bt_workspace_action(workspace,&sequence),"leader has no extra actions");
    leader(); chord(SDL_SCANCODE_5,SDLK_5,KMOD_SHIFT,"%");
    require(bt_workspace_action(workspace,&sequence)==1 && !strcmp(sequence.name,"split-right"),"leader handles shifted suffix");
    leader(); chord(SDL_SCANCODE_J,SDLK_j,KMOD_NONE,"j");
    require(!bt_workspace_action(workspace,&sequence),"unknown suffix cancels leader");
    leader(); chord(SDL_SCANCODE_ESCAPE,SDLK_ESCAPE,KMOD_NONE,NULL);
    require(!bt_workspace_action(workspace,&sequence),"Escape cancels leader");
    chord(SDL_SCANCODE_V,SDLK_v,KMOD_NONE,"v"); until(first,"GOT:76");
    require(!contains(first,"GOT:6f") && !contains(second,"GOT:6f") && !contains(first,"GOT:6a") &&
            !contains(first,"GOT:25") && !contains(second,"GOT:25"),"leader prefix/suffix text never reaches synchronized children");
    leader(); require(!bt_workspace_focus(workspace,third),"focus change cancels pending leader");
    chord(SDL_SCANCODE_O,SDLK_o,KMOD_NONE,"o"); until(third,"GOT:6f");
    require(!bt_workspace_action(workspace,&sequence),"cancelled leader does not target another pane");
    chord(SDL_SCANCODE_B,SDLK_b,KMOD_CTRL,NULL); until(third,"GOT:02");
    require(!bt_workspace_focus(workspace,first),"restore leader source pane");

    require(!bt_workspace_bind(workspace,"Ctrl+Alt+Q","new-page"),"bind host chord that overlaps an AltGr layout");
    chord(SDL_SCANCODE_Q,SDLK_q,KMOD_RALT|KMOD_LCTRL|KMOD_MODE,"@");
    until(first,"GOT:40"); until(second,"GOT:40");
    require(!bt_workspace_action(workspace,&sequence),"AltGr text bypasses host Ctrl+Alt shortcuts and synchronizes");
    require(!bt_workspace_bind(workspace,"Ctrl+Alt+Q",""),"unbind AltGr overlap fixture");

    require(!bt_workspace_bind(workspace,"Ctrl+Shift+T","new-page"),"bind host shortcut");
    require(bt_workspace_bind(workspace,"Ctrl+Ctrl+T","invalid")==-1,"reject ambiguous chord");
    require(bt_workspace_bind(workspace,"Ctrl+T","shell;code")==-1,"reject executable action text");
    SDL_Event shortcut={.type=SDL_KEYDOWN};
    shortcut.key.keysym=(SDL_Keysym){.sym=SDLK_t,.scancode=SDL_SCANCODE_T,.mod=KMOD_RCTRL|KMOD_LSHIFT|KMOD_CAPS};
    require(!bt_workspace_event(workspace,&shortcut),"consume shortcut with either modifier side and caps lock");
    shortcut.key.repeat=1;
    require(!bt_workspace_event(workspace,&shortcut),"consume shortcut repeat without another action");
    SDL_Event text={.type=SDL_TEXTINPUT}; strcpy(text.text.text,"T");
    require(!bt_workspace_event(workspace,&text),"consume shortcut text");
    shortcut.type=SDL_KEYUP;
    require(!bt_workspace_event(workspace,&shortcut),"consume shortcut release");
    require(!bt_workspace_focus(workspace,third),"change focus before draining action");
#if SDL_VERSION_ATLEAST(2,0,22)
    uint64_t preedit_written=info(third).view->session.bytes_written;
    SDL_Event preedit={.type=SDL_TEXTEDITING_EXT};
    preedit.editExt.text=strdup("Deferred IME composition λ");
    require(preedit.editExt.text!=NULL,"allocate deferred composition fixture");
    preedit.editExt.start=9; preedit.editExt.length=3;
    require(!bt_workspace_event(workspace,&preedit),"queue extended composition behind host action");
    free(preedit.editExt.text);
#endif
    BtWorkspaceAction action;
    require(bt_workspace_action(workspace,&action)==1 && action.pane==first && !strcmp(action.name,"new-page"),
            "action retains original pane and name");
    require(!bt_workspace_action(workspace,&action),"repeats do not enqueue duplicate actions");
#if SDL_VERSION_ATLEAST(2,0,22)
    require(!bt_workspace_draw(workspace,false) &&
            info(third).view->session.bytes_written==preedit_written &&
            !contains(third,"Deferred IME composition"),
            "deferred extended composition survives event ownership and stays out of PTY");
    preedit=(SDL_Event){.type=SDL_TEXTEDITING};
    require(!bt_workspace_event(workspace,&preedit),"cancel deferred composition");
#endif
    key('e'); until(third,"GOT:65");
    require(!contains(first,"GOT:54") && !contains(second,"GOT:54") && !contains(first,"GOT:14"),
            "host shortcut never reaches terminal or synchronized peers");
    require(!bt_workspace_bind(workspace,"Ctrl+Shift+T",""),"unbind shortcut");

    require(!bt_workspace_focus(workspace,first),"focus layout source");
    BtRect saved_first=info(first).bounds,saved_second=info(second).bounds,saved_third=info(third).bounds;
    require(!bt_workspace_layout(workspace,first,-1),"cycle splits to stack");
    require(info(first).layout_mode==1 && info(first).visible && !info(second).visible && !info(third).visible,"stack presents only active pane");
    require(!bt_workspace_reset_sizes(workspace,first),"stack reset preserves saved custom proportions");
    require(bt_workspace_resize(workspace,first,true,100)==-1,"stack rejects invisible split resize");
    require(!bt_workspace_focus(workspace,second) && info(second).visible && !info(first).visible,"stack follows focus");
    require(!bt_workspace_layout(workspace,second,-1),"cycle stack to tall");
    require(info(first).layout_mode==2 && info(first).visible && info(third).visible,"tall exposes every pane");
    require(!bt_workspace_resize(workspace,first,true,700),"resize automatic arrangement");
    require(!bt_workspace_layout(workspace,first,-1),"cycle tall to grid");
    require(info(first).layout_mode==3 && info(first).visible && info(second).visible,"grid exposes panes");
    uint64_t temporary;
    require(!bt_workspace_add(workspace,first,BT_DOWN,&launch,&temporary),"add pane while using grid");
    require(info(temporary).layout_mode==3 && info(temporary).visible,"new pane participates in grid");
    require(!bt_workspace_close(workspace,temporary),"close grid pane");
    require(!bt_workspace_layout(workspace,first,-1),"cycle grid to saved splits");
    a=info(first); b=info(second); c=info(third);
    require(!memcmp(&saved_first,&a.bounds,sizeof(saved_first)) && !memcmp(&saved_second,&b.bounds,sizeof(saved_second)) &&
            !memcmp(&saved_third,&c.bounds,sizeof(saved_third)),"cycle and automatic edits preserve custom split geometry");
    require(!bt_workspace_add(workspace,0,BT_RIGHT,&launch,&fourth),"second tab");
    until(fourth,"READY:pane");
    require(info(fourth).visible && !info(first).visible && info(fourth).tab!=info(first).tab,"tab visibility and identity");
    require(info(first).page_index==1 && info(third).page_index==1 && info(fourth).page_index==2,
            "pane metadata follows displayed page order");
    require(!bt_workspace_navigate(workspace,BT_LAST_PAGE,0) && info(first).visible,"last page history");
    require(!bt_workspace_navigate(workspace,BT_LAST_PAGE,0) && bt_workspace_active(workspace)==fourth,"last page toggles");
    require(!bt_workspace_navigate(workspace,BT_PAGE_NUMBER,1) && info(first).visible,"select page by display number");
    require(!bt_workspace_navigate(workspace,BT_PAGE_NUMBER,2) && bt_workspace_active(workspace)==fourth,"select second page");
    require(bt_workspace_navigate(workspace,BT_PAGE_NUMBER,3)==-1,"reject absent page number");
    key('c'); until(fourth,"GOT:63");
    require(!contains(first,"GOT:63"),"typing excludes hidden tabs");
    a=info(first);
    require(!bt_session_send(&a.view->session,"d",1),"send to hidden process");
    until(first,"GOT:64");
    require(!bt_workspace_cycle_tab(workspace,-1) && info(first).visible,"return to previous tab");
    require(!bt_workspace_zoom(workspace,first),"maximize pane");
    a=info(first);
    require(a.visible && !info(second).visible && !info(third).visible && a.bounds.width==pw && a.bounds.height==ph,
            "maximized pane fills OS window");
    require(!bt_workspace_zoom(workspace,first),"restore split layout");
    require(info(second).visible && info(third).visible && info(first).bounds.height>old_height,"restore preserves split ratio");
    require(!bt_workspace_move(workspace,first,BT_RIGHT),"move pane right");
    require(info(first).bounds.x>info(second).bounds.x,"move preserves pane identities");
    require(!bt_workspace_close(workspace,second),"close nested sibling");
    require(info(third).bounds.height==ph,"close expands surviving sibling");

    SDL_SetWindowSize(window,1100,700);
    for(unsigned i=0;i<8;++i) require(!bt_workspace_pump(workspace,5),"resize workspace");
    a=info(first); c=info(third);
    SDL_GL_GetDrawableSize(window,&pw,&ph);
    require(a.bounds.height==ph && c.bounds.height==ph && a.bounds.width+c.bounds.width==pw,"OS resize covers window");
    require(!bt_workspace_draw(workspace,false),"draw workspace for capture");
    require(!bt_surface_capture(bt_workspace_surface(workspace),"build/workspace-test.ppm"),"capture workspace");
    uint64_t frames=bt_renderer_frames(a.view->renderer);
    for(unsigned i=0;i<4;++i) require(!bt_workspace_pump(workspace,0),"idle workspace");
    require(bt_renderer_frames(a.view->renderer)==frames,"idle workspace skips unchanged frames");

    require(!bt_workspace_chrome(workspace,true),"enable clickable chrome");
    require(!bt_workspace_focus(workspace,first),"focus chooser source");
    require(!bt_workspace_start_badge(workspace,true),"enable Start badge");
    require(!bt_workspace_menu(workspace),"open Start menu");
    chord(SDL_SCANCODE_A,SDLK_a,KMOD_NONE,"a");
    chord(SDL_SCANCODE_S,SDLK_s,KMOD_NONE,"s");
    require(!bt_workspace_draw(workspace,false),"draw nested Start submenus");
    require(!bt_surface_capture(bt_workspace_surface(workspace),"build/workspace-menu.ppm"),"capture Start submenus");
    chord(SDL_SCANCODE_R,SDLK_r,KMOD_NONE,"r");
    require(bt_workspace_action(workspace,&action)==1 && !strcmp(action.name,"split-right") && action.pane==first,
            "Start submenu mnemonic queues the pane action");
    require(!bt_workspace_menu(workspace),"open Start menu for voice stop");
    chord(SDL_SCANCODE_V,SDLK_v,KMOD_NONE,"v");
    require(bt_workspace_action(workspace,&action)==1 && !strcmp(action.name,"stop-voice"),
            "Start menu queues voice stop");
    require(!bt_workspace_menu(workspace),"open Start menu for pane voice");
    chord(SDL_SCANCODE_A,SDLK_a,KMOD_NONE,"a");
    chord(SDL_SCANCODE_A,SDLK_a,KMOD_NONE,"a");
    require(bt_workspace_action(workspace,&action)==1 && !strcmp(action.name,"read-aloud") && action.pane==first,
            "pane menu queues read-aloud for the selected pane");
    require(!bt_workspace_menu(workspace),"reopen Start menu");
    chord(SDL_SCANCODE_DOWN,SDLK_DOWN,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_RIGHT,SDLK_RIGHT,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_LEFT,SDLK_LEFT,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_ESCAPE,SDLK_ESCAPE,KMOD_NONE,NULL);
    require(!bt_workspace_action(workspace,&action),"menu navigation and cancellation do not execute actions");
    require(!bt_workspace_start_badge(workspace,false),"restore default badge policy");
    require(!bt_workspace_settings(workspace,false),"open native settings");
    require(bt_workspace_settings_result(workspace,4)==-1,"reject unknown settings result");
    require(!bt_workspace_settings_result(workspace,2),"display save failure");
    require(!bt_workspace_draw(workspace,false),"draw settings error");
    require(!bt_surface_capture(bt_workspace_surface(workspace),"build/workspace-settings-error.ppm"),"capture settings error feedback");
    require(!bt_workspace_settings_result(workspace,0),"clear settings failure");
    require(!bt_workspace_draw(workspace,false),"draw settings overlay");
    require(!bt_surface_capture(bt_workspace_surface(workspace),"build/workspace-settings.ppm"),"capture settings overlay");
    chord(SDL_SCANCODE_RETURN,SDLK_RETURN,KMOD_NONE,NULL);
    require(bt_workspace_action(workspace,&action)==1 && !strcmp(action.name,"setting-edge-bottom"),"settings queues explicit edge choice");
    chord(SDL_SCANCODE_DOWN,SDLK_DOWN,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_SPACE,SDLK_SPACE,KMOD_NONE," ");
    require(bt_workspace_action(workspace,&action)==1 && !strcmp(action.name,"setting-button-0-off"),"settings queues explicit button choice");
    require(!bt_workspace_chrome_buttons(workspace,510),"apply settings button choice");
    chord(SDL_SCANCODE_SPACE,SDLK_SPACE,KMOD_NONE," ");
    require(bt_workspace_action(workspace,&action)==1 && !strcmp(action.name,"setting-button-0-on"),"settings reflects applied native state");
    require(bt_workspace_settings_recording(workspace,"on","8M","elide","5G","broken")==-1,
            "reject invalid shared recording policy");
    require(!bt_workspace_settings_recording(workspace,"on","8M","elide","5G","1G"),
            "display validated shared recording policy");
    chord(SDL_SCANCODE_END,SDLK_END,KMOD_NONE,NULL);
    require(!bt_workspace_draw(workspace,false),"draw scrolled recording settings");
    chord(SDL_SCANCODE_RETURN,SDLK_RETURN,KMOD_NONE,NULL);
    require(bt_workspace_action(workspace,&action)==1 &&
            !strcmp(action.name,"setting-transcript_archive_total-5G"),
            "settings cycles archive retention for new windows");
    chord(SDL_SCANCODE_UP,SDLK_UP,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_RETURN,SDLK_RETURN,KMOD_NONE,NULL);
    require(bt_workspace_action(workspace,&action)==1 && !strcmp(action.name,"setting-transcript_total-10G"),
            "settings cycles recent retention");
    chord(SDL_SCANCODE_UP,SDLK_UP,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_RETURN,SDLK_RETURN,KMOD_NONE,NULL);
    require(bt_workspace_action(workspace,&action)==1 && !strcmp(action.name,"setting-transcript_graphics-keep"),
            "settings cycles transcript graphics policy");
    chord(SDL_SCANCODE_UP,SDLK_UP,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_RETURN,SDLK_RETURN,KMOD_NONE,NULL);
    require(bt_workspace_action(workspace,&action)==1 && !strcmp(action.name,"setting-transcript_size-32M"),
            "settings cycles transcript size");
    chord(SDL_SCANCODE_UP,SDLK_UP,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_RETURN,SDLK_RETURN,KMOD_NONE,NULL);
    require(bt_workspace_action(workspace,&action)==1 && !strcmp(action.name,"setting-transcript-off"),
            "settings toggles recording for new windows");
    chord(SDL_SCANCODE_ESCAPE,SDLK_ESCAPE,KMOD_NONE,NULL);
    require(!bt_workspace_chrome_buttons(workspace,511),"restore native settings state");
    require(!bt_workspace_settings(workspace,true),"open settings with edge override");
    chord(SDL_SCANCODE_RETURN,SDLK_RETURN,KMOD_NONE,NULL);
    require(!bt_workspace_action(workspace,&action),"configuration override prevents edge action");
    chord(SDL_SCANCODE_ESCAPE,SDLK_ESCAPE,KMOD_NONE,NULL);
    require(bt_workspace_message(workspace,"bad\033message")==-1,"reject message control sequences");
    require(!bt_workspace_message(workspace,"Action failed: new-page"),"open recoverable action feedback");
    require(!bt_workspace_draw(workspace,false),"draw recoverable failure without stopping applications");
    require(!bt_surface_capture(bt_workspace_surface(workspace),"build/workspace-message.ppm"),"capture action failure message");
    require(!bt_workspace_pump(workspace,5),"drain pre-dialog key releases");
    uint64_t message_written=info(first).view->session.bytes_written;
    chord(SDL_SCANCODE_Z,SDLK_z,KMOD_NONE,"z");
    require(!bt_workspace_pump(workspace,5),"pump while failure message is visible");
    require(info(first).view->session.bytes_written==message_written && !bt_workspace_action(workspace,&action),
            "message consumes application input and host actions");
    require(!strcmp(bt_workspace_message_text(workspace),"Action failed: new-page"),"ordinary typing does not dismiss failure");
    chord(SDL_SCANCODE_RETURN,SDLK_RETURN,KMOD_NONE,NULL);
    require(!*bt_workspace_message_text(workspace),"Enter dismisses native failure feedback");
    require(!bt_workspace_message(workspace,"Retry the action"),"replace failure message");
    chord(SDL_SCANCODE_ESCAPE,SDLK_ESCAPE,KMOD_NONE,NULL);
    require(!*bt_workspace_message_text(workspace),"Escape dismisses native failure feedback");
    require(!bt_workspace_message(workspace,"Click to dismiss"),"open pointer-dismissed feedback");
    SDL_Event dismiss={.type=SDL_MOUSEBUTTONDOWN}; dismiss.button.button=SDL_BUTTON_LEFT;
    require(!bt_workspace_event(workspace,&dismiss),"dismiss message by clicking");
    dismiss.type=SDL_MOUSEBUTTONUP;
    require(!bt_workspace_event(workspace,&dismiss) && !*bt_workspace_message_text(workspace),
            "dismissal consumes the matching pointer release");
    require(!bt_workspace_action(workspace,&action),"message dismissal does not activate underlying chrome");
    require(bt_workspace_confirm(workspace,UINT64_MAX,"close-confirmed","Close?")==-1,
            "confirmation requires a live pane");
    require(bt_workspace_confirm(workspace,first,"bad action","Close?")==-1,
            "confirmation requires an action identifier");
    message_written=info(first).view->session.bytes_written;
    require(!bt_workspace_confirm(workspace,first,"close-confirmed","Terminate this running pane?"),
            "show native destructive close confirmation");
    chord(SDL_SCANCODE_Z,SDLK_z,KMOD_NONE,"z");
    require(!bt_workspace_action(workspace,&action) && *bt_workspace_message_text(workspace),
            "unrelated keys cannot confirm or reach a pane");
    chord(SDL_SCANCODE_N,SDLK_n,KMOD_NONE,"n");
    require(!*bt_workspace_message_text(workspace) && !bt_workspace_action(workspace,&action),
            "N cancels destructive close");
    require(!bt_workspace_confirm(workspace,first,"close-confirmed","Terminate this running pane?"),
            "reopen close confirmation");
    chord(SDL_SCANCODE_Y,SDLK_y,KMOD_NONE,"y");
    require(bt_workspace_action(workspace,&action)==1 && action.pane==first &&
            !strcmp(action.name,"close-confirmed"),"Y queues exactly one confirmed pane action");
    require(!bt_workspace_action(workspace,&action) && !*bt_workspace_message_text(workspace),
            "confirmation closes without sending typed text or extra actions");
    require(!bt_workspace_pump(workspace,5) && info(first).view->session.bytes_written==message_written,
            "confirmation input never reaches the child");
    require(!bt_workspace_confirm(workspace,first,"close-page-confirmed","Terminate this page?"),
            "show page close confirmation");
    chord(SDL_SCANCODE_ESCAPE,SDLK_ESCAPE,KMOD_NONE,NULL);
    require(!bt_workspace_action(workspace,&action),"Escape cancels page close");
    require(!bt_workspace_rename(workspace,first,"Work café"),"rename page with UTF-8");
    require(!strcmp(info(third).page_title,"Work café") && !*info(fourth).page_title,"page title belongs to page, not pane");
    require(bt_workspace_rename(workspace,first,"bad\033title")==-1 &&
            bt_workspace_rename(workspace,first,"bad\xff")==-1,"reject control and malformed title bytes");
    char long_title[257]; memset(long_title,'x',sizeof(long_title)-1); long_title[256]=0;
    require(bt_workspace_rename(workspace,first,long_title)==-1,"bound title storage");
    require(!bt_workspace_rename_prompt(workspace,first),"open rename prompt");
    chord(SDL_SCANCODE_U,SDLK_u,KMOD_CTRL,NULL);
    chord(SDL_SCANCODE_E,SDLK_e,KMOD_NONE,"é");
    chord(SDL_SCANCODE_BACKSPACE,SDLK_BACKSPACE,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_A,SDLK_a,KMOD_NONE,"Alpha");
    chord(SDL_SCANCODE_RETURN,SDLK_RETURN,KMOD_NONE,NULL);
    require(!strcmp(info(first).page_title,"Alpha"),"rename prompt deletes whole UTF-8 codepoint and commits");
    require(!bt_workspace_rename_prompt(workspace,first),"open rename to cancel");
    chord(SDL_SCANCODE_U,SDLK_u,KMOD_CTRL,NULL);
    chord(SDL_SCANCODE_ESCAPE,SDLK_ESCAPE,KMOD_NONE,NULL);
    require(!strcmp(info(first).page_title,"Alpha"),"cancel preserves page title");
    require(!bt_workspace_rename_prompt(workspace,first),"rename before controller resize");
    require(!bt_workspace_resize_mode(workspace,first),"controller replaces rename with resize");
    int modal_width=info(first).bounds.width;
    chord(SDL_SCANCODE_RIGHT,SDLK_RIGHT,KMOD_NONE,NULL);
    require(info(first).bounds.width>modal_width,"resize keys bypass dismissed rename prompt");
    chord(SDL_SCANCODE_ESCAPE,SDLK_ESCAPE,KMOD_NONE,NULL);
    require(!strcmp(info(first).page_title,"Alpha"),"mode transition preserves committed title");
    require(!bt_workspace_choose(workspace,false),"chooser before controller resize");
    require(!bt_workspace_resize_mode(workspace,first),"controller replaces chooser with resize");
    chord(SDL_SCANCODE_RIGHT,SDLK_RIGHT,KMOD_NONE,NULL);
    require(info(first).bounds.width>modal_width,"resize keys bypass dismissed chooser");
    chord(SDL_SCANCODE_ESCAPE,SDLK_ESCAPE,KMOD_NONE,NULL);
    require(!bt_workspace_rename(workspace,first,""),"clear custom page title");
    uint64_t chooser_bytes=info(first).view->session.bytes_written;
    require(!bt_workspace_choose(workspace,false),"open current-page pane chooser");
    require(!bt_workspace_draw(workspace,false),"draw pane chooser overlay");
    require(!bt_surface_capture(bt_workspace_surface(workspace),"build/workspace-chooser.ppm"),"capture chooser overlay");
    chord(SDL_SCANCODE_DOWN,SDLK_DOWN,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_RETURN,SDLK_RETURN,KMOD_NONE,NULL);
    require(bt_workspace_active(workspace)==third,"pane chooser selects next pane");
    require(info(first).view->session.bytes_written==chooser_bytes,"chooser keys never reach source application");
    const char *first_title="\033]2;Build café\007",*third_title="\033]2;Logs\007";
    bt_session_feed(&info(first).view->session,first_title,strlen(first_title));
    bt_session_feed(&info(third).view->session,third_title,strlen(third_title));
    require(!bt_workspace_choose(workspace,false),"open filtered pane chooser");
    chord(SDL_SCANCODE_A,SDLK_a,KMOD_NONE,"zzzzz");
    chord(SDL_SCANCODE_RETURN,SDLK_RETURN,KMOD_NONE,NULL);
    require(bt_workspace_active(workspace)==third,"no-match Enter leaves focus unchanged");
    require(!bt_workspace_draw(workspace,false),"draw no-match chooser");
    require(!bt_surface_capture(bt_workspace_surface(workspace),"build/workspace-search.ppm"),"capture no-match search");
    chord(SDL_SCANCODE_U,SDLK_u,KMOD_CTRL,NULL);
    chord(SDL_SCANCODE_A,SDLK_a,KMOD_NONE,"café");
    chord(SDL_SCANCODE_RETURN,SDLK_RETURN,KMOD_NONE,NULL);
    require(bt_workspace_active(workspace)==first,"Unicode title query selects matching pane after no-match edit");
    require(!bt_workspace_choose(workspace,false),"open ASCII case-insensitive query");
    chord(SDL_SCANCODE_A,SDLK_a,KMOD_NONE,"LoGs");
    chord(SDL_SCANCODE_RETURN,SDLK_RETURN,KMOD_NONE,NULL);
    require(bt_workspace_active(workspace)==third,"ASCII title query ignores case");
    require(!bt_workspace_rename(workspace,fourth,"Deploy"),"name page for search");
    require(!bt_workspace_choose(workspace,true),"search custom page name");
    chord(SDL_SCANCODE_A,SDLK_a,KMOD_NONE,"ploy");
    chord(SDL_SCANCODE_RETURN,SDLK_RETURN,KMOD_NONE,NULL);
    require(bt_workspace_active(workspace)==fourth,"page query searches custom name");
    require(!bt_workspace_focus(workspace,third),"restore pane after search");
    require(!bt_workspace_choose(workspace,true),"open page chooser");
    chord(SDL_SCANCODE_2,SDLK_2,KMOD_NONE,"2");
    require(bt_workspace_active(workspace)==fourth,"page chooser selects numbered page");
    require(!bt_workspace_choose(workspace,true),"reopen page chooser");
    chord(SDL_SCANCODE_UP,SDLK_UP,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_ESCAPE,SDLK_ESCAPE,KMOD_NONE,NULL);
    require(bt_workspace_active(workspace)==fourth,"cancel preserves active page");
    require(!bt_workspace_focus(workspace,first),"restore chooser source page");
    require(!bt_workspace_choose(workspace,false),"open mouse chooser");
    int chooser_height=info(first).bounds.y;
    SDL_Event choose_mouse={.type=SDL_MOUSEBUTTONDOWN}; choose_mouse.button.button=SDL_BUTTON_LEFT;
    SDL_GetWindowSize(window,&lw,&lh); SDL_GL_GetDrawableSize(window,&pw,&ph);
    choose_mouse.button.x=lw/2;
    choose_mouse.button.y=((ph-3*chooser_height)/2+2*chooser_height+5)*lh/ph;
    require(!bt_workspace_event(workspace,&choose_mouse),"click second chooser row");
    choose_mouse.type=SDL_MOUSEBUTTONUP;
    require(!bt_workspace_event(workspace,&choose_mouse),"consume chooser click release");
    require(bt_workspace_active(workspace)==third,"mouse chooser focuses selected pane");
    require(!bt_session_send(&info(first).view->session,"g",1),"upload Sixel to preview source");
    until(first,"GRAPHICS_READY");
    BtSession *preview_owner=&info(first).view->session;
    unsigned preview_cols=preview_owner->cols,preview_rows=preview_owner->rows;
    int preview_cw=preview_owner->cell_width,preview_ch=preview_owner->cell_height;
    require(!bt_workspace_focus(workspace,fourth),"hide Sixel source on another page");
    require(!bt_session_send(&info(fourth).view->session,"g",1),"upload Sixel to active pane");
    until(fourth,"GRAPHICS_READY");
    size_t hidden_cache=bt_renderer_image_stats(info(first).view->renderer).texture_bytes;
    size_t active_cache=bt_renderer_image_stats(info(fourth).view->renderer).texture_bytes;
    require(hidden_cache && active_cache,"two pane image caches are populated");
    size_t shared_budget=hidden_cache>active_cache?hidden_cache:active_cache;
    require(!bt_workspace_image_budget(workspace,shared_budget),"set workspace-wide image cache budget");
    require(bt_workspace_image_cache_bytes(workspace)<=shared_budget &&
            !bt_renderer_image_stats(info(first).view->renderer).texture_bytes &&
            bt_renderer_image_stats(info(fourth).view->renderer).texture_bytes==active_cache,
            "shared budget evicts hidden pane before active pane");
    require(!bt_workspace_pane_center(workspace),"open Pane Center for hidden graphical pane");
    chord(SDL_SCANCODE_HOME,SDLK_HOME,KMOD_NONE,NULL);
    uint64_t center_ids[BT_LAYOUT_PANES]; unsigned center_count=0;
    require(bt_workspace_center_visible(workspace,center_ids,&center_count) && center_count>=2 &&
            center_ids[0]==first,"Pane Center exposes only its displayed pane IDs");
    require(!bt_workspace_telemetry(workspace,first,"working","codex","codex 12345678",
                                    "Build Pane Center task display"),
            "accept bounded Pane Center agent telemetry");
    BtPaneTelemetry telemetry;
    require(bt_workspace_telemetry_get(workspace,first,&telemetry) &&
            !strcmp(telemetry.activity,"working") && !strcmp(telemetry.process,"codex") &&
            !strcmp(telemetry.task,"Build Pane Center task display"),
            "Pane Center retains validated live telemetry");
    require(!bt_workspace_draw(workspace,false),"draw selected hidden graphical pane preview");
    require(!bt_surface_capture(bt_workspace_surface(workspace),"build/workspace-pane-center-graphics.ppm"),
            "capture hidden pane graphical preview");
    require(red_pixels("build/workspace-pane-center-graphics.ppm",true)>100,
            "Pane Center preview contains the hidden pane's red Sixel pixels");
    require(bt_workspace_image_cache_bytes(workspace)<=shared_budget,
            "graphical preview keeps the workspace cache bounded");
    require(preview_owner->cols==preview_cols && preview_owner->rows==preview_rows &&
            preview_owner->cell_width==preview_cw && preview_owner->cell_height==preview_ch,
            "scaled Pane Center preview leaves the hidden PTY geometry unchanged");
    require(!bt_workspace_image_budget(workspace,128u*1024u*1024u),"restore workspace image budget");
    chord(SDL_SCANCODE_ESCAPE,SDLK_ESCAPE,KMOD_NONE,NULL);
    require(!bt_workspace_focus(workspace,third),"restore pane after graphical preview");
    require(!bt_workspace_choose(workspace,true),"open chooser before focus loss");
    SDL_Event lost={.type=SDL_WINDOWEVENT}; lost.window.event=SDL_WINDOWEVENT_FOCUS_LOST;
    require(!bt_workspace_event(workspace,&lost),"focus loss cancels chooser");
    lost.window.event=SDL_WINDOWEVENT_FOCUS_GAINED;
    require(!bt_workspace_event(workspace,&lost),"regain workspace focus");
    chord(SDL_SCANCODE_V,SDLK_v,KMOD_NONE,"v"); until(third,"GOT:76");
    require(!bt_workspace_focus(workspace,first),"restore source after mouse chooser");
    require(!bt_workspace_pane_center(workspace),"open all-page Pane Center");
    require(!bt_workspace_draw(workspace,false),"draw all-page Pane Center");
    require(!bt_surface_capture(bt_workspace_surface(workspace),"build/workspace-pane-center.ppm"),
            "capture Pane Center tree and selected pane details");
    chord(SDL_SCANCODE_END,SDLK_END,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_RETURN,SDLK_RETURN,KMOD_NONE,NULL);
    require(bt_workspace_active(workspace)==fourth,"Pane Center reaches another page");
    require(!bt_workspace_focus(workspace,first),"restore source before scoped Pane Center");
    require(!bt_workspace_pane_center(workspace),"open scoped Pane Center");
    chord(SDL_SCANCODE_TAB,SDLK_TAB,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_END,SDLK_END,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_RETURN,SDLK_RETURN,KMOD_NONE,NULL);
    require(bt_workspace_active(workspace)==third,"Pane Center current-page scope excludes other pages");
    require(!bt_workspace_focus(workspace,first),"restore source before other-page scope");
    require(!bt_workspace_pane_center(workspace),"open other-page Pane Center");
    chord(SDL_SCANCODE_TAB,SDLK_TAB,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_TAB,SDLK_TAB,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_RETURN,SDLK_RETURN,KMOD_NONE,NULL);
    require(bt_workspace_active(workspace)==fourth,"Pane Center other-page scope selects a remote page");
    require(!bt_workspace_focus(workspace,first),"restore source after Pane Center");
    require(!bt_workspace_pane_center(workspace),"open Pane Center for selected-page rename");
    chord(SDL_SCANCODE_END,SDLK_END,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_F2,SDLK_F2,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_U,SDLK_u,KMOD_CTRL,NULL);
    chord(SDL_SCANCODE_O,SDLK_o,KMOD_NONE,"Ops");
    chord(SDL_SCANCODE_RETURN,SDLK_RETURN,KMOD_NONE,NULL);
    require(!strcmp(info(fourth).page_title,"Ops") && bt_workspace_active(workspace)==first,
            "Pane Center renames another page without moving focus");
    require(!bt_workspace_rename(workspace,fourth,"Deploy"),"restore page name after Pane Center rename");
    require(!bt_workspace_pane_center(workspace),"open Pane Center for another pane's actions");
    chord(SDL_SCANCODE_END,SDLK_END,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_A,SDLK_a,KMOD_NONE,"a");
    chord(SDL_SCANCODE_R,SDLK_r,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_U,SDLK_u,KMOD_CTRL,NULL);
    chord(SDL_SCANCODE_C,SDLK_c,KMOD_NONE,"Center pane λ");
    chord(SDL_SCANCODE_RETURN,SDLK_RETURN,KMOD_NONE,NULL);
    require(!strcmp(bt_workspace_pane_title(workspace,fourth),"Center pane λ") &&
            bt_workspace_active(workspace)==first,
            "Pane Center actions rename a selected pane without changing focus");
    require(!bt_workspace_pane_center(workspace),"reopen Pane Center for selected title copy");
    chord(SDL_SCANCODE_END,SDLK_END,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_A,SDLK_a,KMOD_NONE,"a");
    chord(SDL_SCANCODE_C,SDLK_c,KMOD_NONE,NULL);
    char *center_copy=SDL_GetClipboardText();
    require(center_copy && !strcmp(center_copy,"Center pane λ"),"Pane Center actions copy the selected pane title");
    SDL_free(center_copy);
    require(!bt_workspace_pane_center(workspace),"reopen Pane Center for selected title reset");
    chord(SDL_SCANCODE_END,SDLK_END,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_A,SDLK_a,KMOD_NONE,"a");
    chord(SDL_SCANCODE_E,SDLK_e,KMOD_NONE,NULL);
    require(strcmp(bt_workspace_pane_title(workspace,fourth),"Center pane λ") &&
            bt_workspace_active(workspace)==first,
            "Pane Center actions reset only the selected pane title");
    require(!bt_workspace_pane_center(workspace),"open Pane Center for a row context action");
    require(!bt_workspace_draw(workspace,false),"draw Pane Center before row context action");
    SDL_GetWindowSize(window,&lw,&lh); SDL_GL_GetDrawableSize(window,&pw,&ph);
    SDL_Event row_menu={.type=SDL_MOUSEBUTTONDOWN};
    row_menu.button.button=SDL_BUTTON_RIGHT; row_menu.button.x=lw/2;
    row_menu.button.y=((ph-12*chooser_height)/2+chooser_height+5)*lh/ph;
    require(!bt_workspace_event(workspace,&row_menu),"right-click first Pane Center row");
    row_menu.type=SDL_MOUSEBUTTONUP;
    require(!bt_workspace_event(workspace,&row_menu),"release Pane Center row context click");
    chord(SDL_SCANCODE_C,SDLK_c,KMOD_NONE,NULL);
    center_copy=SDL_GetClipboardText();
    require(center_copy && !strcmp(center_copy,bt_workspace_pane_title(workspace,first)),
            "Pane Center row context menu targets its own pane");
    SDL_free(center_copy);
    uint64_t source_written=info(first).view->session.bytes_written;
    require(!bt_workspace_pane_center(workspace),"open Pane Center to message another page");
    chord(SDL_SCANCODE_END,SDLK_END,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_S,SDLK_s,KMOD_NONE,"s");
    chord(SDL_SCANCODE_M,SDLK_m,KMOD_NONE,"m");
    chord(SDL_SCANCODE_RETURN,SDLK_RETURN,KMOD_NONE,NULL);
    until(fourth,"GOT:6d"); until(fourth,"GOT:0d");
    require(info(first).view->session.bytes_written==source_written && bt_workspace_active(workspace)==first,
            "Pane Center message and submit reach only the selected pane");
    chord(SDL_SCANCODE_X,SDLK_x,KMOD_NONE,"x");
    require(info(fourth).view!=NULL,"Pane Center x only arms close confirmation");
    chord(SDL_SCANCODE_N,SDLK_n,KMOD_NONE,"n");
    require(info(fourth).view!=NULL && bt_workspace_active(workspace)==first,
            "any non-y confirmation cancels without closing or focusing");
    chord(SDL_SCANCODE_ESCAPE,SDLK_ESCAPE,KMOD_NONE,NULL);
    a=info(first); c=info(third);
    require(a.bounds.y>0 && a.view->region.y>a.bounds.y && a.view->region.h<a.bounds.height,
            "page strip and pane header reserve space outside PTY viewport");
    unsigned old_cell=a.view->session.cell_height;
    require(!bt_workspace_font(workspace,first,3),"increase one pane font");
    require(a.view->session.cell_height>old_cell && c.view->session.cell_height==old_cell,
            "font controls resize only the selected PTY grid");
    require(!bt_workspace_font(workspace,first,-3),"restore pane font");
    require(bt_workspace_font(workspace,first,90)==-1,"reject out-of-range font");
    SDL_GetWindowSize(window,&lw,&lh); SDL_GL_GetDrawableSize(window,&pw,&ph);
    a=info(first);
    const char mouse_mode[]="\033[?1000h\033[?1006h";
    bt_session_feed(&a.view->session,mouse_mode,sizeof(mouse_mode)-1);
    uint64_t written=a.view->session.bytes_written;
    mouse=(SDL_Event){.type=SDL_MOUSEBUTTONDOWN}; mouse.button.button=SDL_BUTTON_LEFT;
    mouse.button.x=(a.bounds.x+a.bounds.width-5)*lw/pw;
    mouse.button.y=(a.bounds.y+5)*lh/ph;
    require(!bt_workspace_event(workspace,&mouse),"click pane close control");
    mouse.type=SDL_MOUSEBUTTONUP; mouse.button.y=lh-5;
    require(!bt_workspace_event(workspace,&mouse),"consume header release over terminal");
    require(bt_workspace_action(workspace,&action)==1 && action.pane==first && !strcmp(action.name,"close"),
            "close button queues action for its pane");
    require(a.view->session.bytes_written==written,"chrome click and release never reach mouse-aware process");
    mouse.type=SDL_MOUSEBUTTONDOWN; mouse.button.x=205*lw/pw; mouse.button.y=5*lh/ph;
    require(!bt_workspace_event(workspace,&mouse),"click second page");
    mouse.type=SDL_MOUSEBUTTONUP; require(!bt_workspace_event(workspace,&mouse),"release page click");
    require(bt_workspace_action(workspace,&action)==1 && action.pane==fourth && !strcmp(action.name,"focus"),
            "page control targets that page's active pane");
    mouse.type=SDL_MOUSEBUTTONDOWN; mouse.button.x=405*lw/pw;
    require(!bt_workspace_event(workspace,&mouse),"click new page");
    mouse.type=SDL_MOUSEBUTTONUP; require(!bt_workspace_event(workspace,&mouse),"release new page");
    require(bt_workspace_action(workspace,&action)==1 && !strcmp(action.name,"new-page"),"new page action");
    require(!bt_workspace_draw(workspace,false),"render chrome");
    require(!bt_surface_capture(bt_workspace_surface(workspace),"build/workspace-chrome.ppm"),"capture clickable chrome");
    require(!bt_workspace_chrome_buttons(workspace,0),"hide every pane button");
    require(bt_workspace_chrome_buttons(workspace,512)==-1,"reject invalid pane button mask");
    a=info(first);
    mouse.type=SDL_MOUSEBUTTONDOWN; mouse.button.x=(a.bounds.x+a.bounds.width-5)*lw/pw;
    mouse.button.y=(a.bounds.y+5)*lh/ph;
    require(!bt_workspace_event(workspace,&mouse),"click former close-button area");
    mouse.type=SDL_MOUSEBUTTONUP; require(!bt_workspace_event(workspace,&mouse),"release title click");
    require(bt_workspace_action(workspace,&action)==1 && !strcmp(action.name,"focus"),"hidden controls become title area, not stale actions");
    require(bt_workspace_action(workspace,&action)==0,"release opens pane title menu after focus action");
    chord(SDL_SCANCODE_C,SDLK_c,KMOD_NONE,NULL);
    char *copied=SDL_GetClipboardText();
    require(copied && !strcmp(copied,bt_workspace_pane_title(workspace,first)),"pane title menu copies displayed title");
    SDL_free(copied);
    mouse.type=SDL_MOUSEBUTTONDOWN; require(!bt_workspace_event(workspace,&mouse),"reopen pane title menu");
    require(bt_workspace_action(workspace,&action)==1 && action.pane==first,"title focus precedes menu");
    mouse.type=SDL_MOUSEBUTTONUP; require(!bt_workspace_event(workspace,&mouse),"release title to open rename menu");
    chord(SDL_SCANCODE_R,SDLK_r,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_U,SDLK_u,KMOD_CTRL,NULL);
    chord(SDL_SCANCODE_D,SDLK_d,KMOD_NONE,"Desk pane");
    chord(SDL_SCANCODE_RETURN,SDLK_RETURN,KMOD_NONE,NULL);
    require(!strcmp(bt_workspace_pane_title(workspace,first),"Desk pane"),"pane menu renames only displayed pane title");
    mouse.type=SDL_MOUSEBUTTONDOWN; require(!bt_workspace_event(workspace,&mouse),"open title reset menu");
    require(bt_workspace_action(workspace,&action)==1 && action.pane==first,"title reset focus action");
    mouse.type=SDL_MOUSEBUTTONUP; require(!bt_workspace_event(workspace,&mouse),"release title for reset");
    chord(SDL_SCANCODE_E,SDLK_e,KMOD_NONE,NULL);
    require(strcmp(bt_workspace_pane_title(workspace,first),"Desk pane"),"pane menu resets custom title");
    pid_t clear_pid=info(first).view->session.child;
    mouse.type=SDL_MOUSEBUTTONDOWN; require(!bt_workspace_event(workspace,&mouse),"open clear menu");
    require(bt_workspace_action(workspace,&action)==1 && action.pane==first,"clear menu focus action");
    mouse.type=SDL_MOUSEBUTTONUP; require(!bt_workspace_event(workspace,&mouse),"release title for clear");
    chord(SDL_SCANCODE_L,SDLK_l,KMOD_NONE,NULL);
    require(!contains(first,"READY:pane") && info(first).view->session.child==clear_pid,
            "pane clear resets authoritative parser without restarting its PTY");
    require(!bt_workspace_chrome_buttons(workspace,1u<<8),"show only close button");
    mouse.type=SDL_MOUSEBUTTONDOWN; require(!bt_workspace_event(workspace,&mouse),"click sole visible close button");
    mouse.type=SDL_MOUSEBUTTONUP; require(!bt_workspace_event(workspace,&mouse),"release sole button");
    require(bt_workspace_action(workspace,&action)==1 && !strcmp(action.name,"close"),"visible button packs at right edge");
    require(!bt_workspace_chrome_buttons(workspace,511),"restore all pane buttons");
    int old_bar=info(first).bounds.y;
    require(!bt_workspace_chrome_edge(workspace,true),"move page strip to bottom");
    a=info(first); c=info(third);
    require(a.bounds.y==0 && a.bounds.height==ph-old_bar && a.view->region.y==old_bar,
            "bottom strip reserves space below pane headers and terminal views");
    mouse.type=SDL_MOUSEBUTTONDOWN; mouse.button.x=205*lw/pw; mouse.button.y=lh-5;
    require(!bt_workspace_event(workspace,&mouse),"click bottom page strip");
    mouse.type=SDL_MOUSEBUTTONUP; require(!bt_workspace_event(workspace,&mouse),"release bottom page click");
    require(bt_workspace_action(workspace,&action)==1 && action.pane==fourth && !strcmp(action.name,"focus"),
            "bottom strip click targets correct page");
    require(!bt_workspace_draw(workspace,false),"draw bottom strip");
    require(!bt_surface_capture(bt_workspace_surface(workspace),"build/workspace-bottom.ppm"),"capture bottom strip");
    require(!bt_workspace_chrome_edge(workspace,false) && info(first).bounds.y==old_bar,"restore top page strip");
    require(bt_workspace_relocate(workspace,first,first,BT_LEFT)==-1 &&
            bt_workspace_relocate(workspace,first,UINT64_MAX,BT_RIGHT)==-1,
            "pane relocation rejects self and missing targets");
    pid_t third_pid=info(third).view->session.child;
    require(!bt_workspace_relocate(workspace,third,fourth,BT_RIGHT),"move live pane to another page");
    require(info(third).tab==info(fourth).tab && info(third).view->session.child==third_pid &&
            info(third).bounds.x>info(fourth).bounds.x,
            "cross-page relocation preserves PTY owner and places pane on target edge");
    require(!bt_workspace_relocate(workspace,third,first,BT_DOWN),"return live pane to original page");
    require(info(third).tab==info(first).tab && info(third).view->session.child==third_pid,
            "returning pane preserves owner and source page");
    require(!bt_workspace_chrome_buttons(workspace,0),"make full pane titles draggable");
    a=info(first); b=info(third);
    SDL_GetWindowSize(window,&lw,&lh); SDL_GL_GetDrawableSize(window,&pw,&ph);
    uint64_t drag_written_a=a.view->session.bytes_written,drag_written_b=b.view->session.bytes_written;
    mouse=(SDL_Event){.type=SDL_MOUSEBUTTONDOWN}; mouse.button.button=SDL_BUTTON_LEFT;
    mouse.button.x=(a.bounds.x+12)*lw/pw; mouse.button.y=(a.bounds.y+6)*lh/ph;
    require(!bt_workspace_event(workspace,&mouse),"press source pane title for drag");
    require(bt_workspace_action(workspace,&action)==1 && !strcmp(action.name,"focus") && action.pane==first,
            "title press retains click-to-focus action before dragging");
    SDL_Event motion={.type=SDL_MOUSEMOTION};
    motion.motion.x=(b.bounds.x+b.bounds.width-8)*lw/pw;
    motion.motion.y=(b.bounds.y+b.bounds.height/2)*lh/ph;
    require(!bt_workspace_event(workspace,&motion),"drag title to target right quadrant");
    mouse.type=SDL_MOUSEBUTTONUP; mouse.button.x=motion.motion.x; mouse.button.y=motion.motion.y;
    require(!bt_workspace_event(workspace,&mouse),"drop title onto target edge");
    require(info(first).bounds.x>info(third).bounds.x &&
            info(first).view->session.bytes_written==drag_written_a &&
            info(third).view->session.bytes_written==drag_written_b,
            "header drag reorders panes without sending pointer input to either PTY");
    require(!bt_workspace_relocate(workspace,first,third,BT_LEFT),"restore pane order after drag");
    a=info(first); pid_t first_pid=a.view->session.child;
    mouse=(SDL_Event){.type=SDL_MOUSEBUTTONDOWN}; mouse.button.button=SDL_BUTTON_LEFT;
    mouse.button.x=(a.bounds.x+12)*lw/pw; mouse.button.y=(a.bounds.y+6)*lh/ph;
    require(!bt_workspace_event(workspace,&mouse),"press pane title for cross-page drag");
    require(bt_workspace_action(workspace,&action)==1 && !strcmp(action.name,"focus") && action.pane==first,
            "cross-page drag handles title focus before motion");
    motion=(SDL_Event){.type=SDL_MOUSEMOTION}; motion.motion.x=205*lw/pw; motion.motion.y=5*lh/ph;
    require(!bt_workspace_event(workspace,&motion) && bt_workspace_active(workspace)==fourth,
            "drag over another page's tab reveals its pane targets");
    b=info(fourth);
    SDL_GetWindowSize(window,&lw,&lh); SDL_GL_GetDrawableSize(window,&pw,&ph);
    motion.motion.x=(b.bounds.x+b.bounds.width-8)*lw/pw;
    motion.motion.y=(b.bounds.y+b.bounds.height/2)*lh/ph;
    require(!bt_workspace_event(workspace,&motion),"drag onto pane in revealed page");
    mouse.type=SDL_MOUSEBUTTONUP; mouse.button.x=motion.motion.x; mouse.button.y=motion.motion.y;
    require(!bt_workspace_event(workspace,&mouse),"drop pane into another page");
    require(info(first).tab==info(fourth).tab && info(first).bounds.x>info(fourth).bounds.x &&
            info(first).view->session.child==first_pid,
            "cross-page header drag preserves the PTY and target-side placement");
    require(!bt_workspace_relocate(workspace,first,third,BT_LEFT),"restore source page after cross-page drag");
    require(!bt_workspace_chrome_buttons(workspace,511),"restore controls after header drag");
    require(!bt_workspace_chrome(workspace,false),"disable chrome");
    require(info(first).bounds.y==0 && info(first).view->region.y==0,"disabling chrome restores terminal area");

    require(!bt_session_send(&a.view->session,"q",1),"finish one child");
    uint64_t deadline=bt_millis()+5000;
    while(!a.view->session.done && bt_millis()<deadline) require(!bt_workspace_pump(workspace,5),"wait for child status");
    require(a.view->session.done && a.view->session.exit_status==7,"independent pane exit status");
    require(!info(third).view->session.done && !info(fourth).view->session.done,"other tabs and panes continue running");
    require(!bt_workspace_close(workspace,first),"close finished pane");
    require(!bt_workspace_focus(workspace,fourth),"focus surviving second page before Pane Center close");
    require(!bt_workspace_chrome(workspace,true),"enable chrome for final Pane Center action");
    require(!bt_workspace_pane_center(workspace),"open Pane Center to close another page's pane");
    chord(SDL_SCANCODE_HOME,SDLK_HOME,KMOD_NONE,NULL);
    chord(SDL_SCANCODE_X,SDLK_x,KMOD_NONE,"x");
    chord(SDL_SCANCODE_Y,SDLK_y,KMOD_NONE,"y");
    count=0; bt_workspace_visit(workspace,collect,NULL);
    require(count==1 && snapshot[0].id==fourth,"confirmed Pane Center close removes exactly selected pane");
    chord(SDL_SCANCODE_ESCAPE,SDLK_ESCAPE,KMOD_NONE,NULL);
    require(bt_workspace_active(workspace)==fourth && info(fourth).visible && info(fourth).page_index==1,
            "remaining tab becomes the first displayed page");
    uint64_t recycled;
    require(!bt_workspace_add(workspace,0,BT_RIGHT,&launch,&recycled),"reuse a freed page slot");
    require(info(recycled).page_index==1 && info(fourth).page_index==2,
            "page order follows the reused slot, not pane allocation order");
    require(!bt_workspace_close(workspace,recycled) && info(fourth).page_index==1,
            "closing reused page restores remaining page number");
    uint64_t extra;
    require(!bt_workspace_add(workspace,fourth,BT_RIGHT,&launch,&extra),"split final page before page closure");
    require(!bt_workspace_close_tab(workspace,fourth) && bt_workspace_closed(workspace),"close page closes every pane");
    bt_workspace_free(workspace); workspace=NULL;
    process_image_budget_test(executable,helper);
    fairness(executable,helper);
    checkpoints(executable,helper);
    puts("PASS native tabs, splits, focus, synchronized input, hidden sessions, zoom, move, resize and independent exit status");
    return 0;
}
