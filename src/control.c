/* SPDX-License-Identifier: MIT */
#define _GNU_SOURCE
#include "control.h"
#include "remote.h"
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/random.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

enum { CLIENTS=16, PACKET=131072, HEADER=16, EVENTS=256 };
enum { CHANGE_LAYOUT=1, CHANGE_TITLE=2, CHANGE_OUTPUT=4, CHANGE_STATUS=8 };
typedef struct {
    uint64_t id,tab,output;
    BtRect bounds;
    unsigned cols,rows,cell_width,cell_height,layout_mode,page_index;
    pid_t pid;
    int status;
    unsigned recording;
    char transcript_id[49];
    bool active,tab_active,visible,synchronized,exited,disconnected,resizing;
    char title[1024],page_title[256];
} Snapshot;
typedef struct { uint64_t sequence,pane; unsigned kind,changes; } Change;
enum { WAIT_EVENTS=1, WAIT_TEXT=2 };
typedef struct {
    int fd;
    uint64_t deadline,after,epoch,pane,text_ticket;
    unsigned waiting;
} ControlClient;
struct BtControl {
    BtWorkspace *workspace;
    int listener,directory;
    ControlClient clients[CLIENTS];
    Snapshot previous[BT_LAYOUT_PANES];
    Change changes[EVENTS];
    uint64_t sequence,epoch;
    uint64_t scope_pane;
    unsigned retained;
    char name[108],helper[PATH_MAX],service[PATH_MAX],root[PATH_MAX];
    char terminate_prefix[25];
    char **env;
    dev_t device; ino_t inode;
    bool read_only,bound,host_actions;
    unsigned cursor;
};
static uint64_t read64(const unsigned char *p) {
    uint64_t n=0; for(unsigned i=0;i<8;++i) n|=(uint64_t)p[i]<<(i*8); return n;
}
static uint32_t read32(const unsigned char *p) {
    uint32_t n=0; for(unsigned i=0;i<4;++i) n|=(uint32_t)p[i]<<(i*8); return n;
}
static void quoted(FILE *out, const char *s) {
    fputc('"',out);
    if(s) for(const unsigned char *p=(const unsigned char *)s;*p;++p) {
        if(*p=='"' || *p=='\\') { fputc('\\',out); fputc(*p,out); }
        else if(*p<32) fprintf(out,"\\u%04x",*p);
        else fputc(*p,out);
    }
    fputc('"',out);
}
typedef struct { FILE *out; BtWorkspace *workspace; uint64_t id; BtWindow *view;
    const char *session_name,*session_dir; bool first,details; } Listing;
static void visit(void *data, const BtPaneInfo *p) {
    Listing *l=data;
    if(l->id && l->id!=p->id) return;
    l->view=p->view;
    l->session_name=p->session_name; l->session_dir=p->session_dir;
    if(!l->out) return;
    if(!l->first) fputc(',',l->out);
    l->first=false;
    BtSession *s=&p->view->session;
    fprintf(l->out,"{\"id\":%llu,\"tab\":%llu,\"page_index\":%u,\"window\":%llu,\"active\":%s,\"visible\":%s,\"synchronized\":%s,"
        "\"x\":%d,\"y\":%d,\"width\":%d,\"height\":%d,\"cols\":%u,\"rows\":%u,\"pid\":%d,\"exit_status\":",
        (unsigned long long)p->id,(unsigned long long)p->tab,p->page_index,(unsigned long long)p->window,
        p->active?"true":"false",p->visible?"true":"false",p->synchronized?"true":"false",
        p->bounds.x,p->bounds.y,p->bounds.width,p->bounds.height,s->cols,s->rows,(int)s->child);
    if(s->exited) fprintf(l->out,"%d",s->exit_status); else fputs("null",l->out);
    const char *recordings[]={"unknown","disabled","active","finishing","complete","failed"};
    fprintf(l->out,",\"recording\":\"%s\"",recordings[bt_session_recording(s)]);
    fputs(",\"transcript_id\":",l->out);
    const char *transcript=bt_session_transcript_id(s);
    if(transcript) quoted(l->out,transcript); else fputs("null",l->out);
    fputs(",\"transcript_dir\":",l->out);
    const char *transcript_dir=bt_session_transcript_directory(s);
    if(transcript_dir) quoted(l->out,transcript_dir); else fputs("null",l->out);
    fprintf(l->out,",\"tab_active\":%s",p->tab_active?"true":"false");
    fprintf(l->out,",\"cell_width\":%u,\"cell_height\":%u",s->cell_width,s->cell_height);
    fprintf(l->out,",\"persistent\":%s,\"observe\":%s",s->remote?"true":"false",bt_remote_observer(s)?"true":"false");
    fprintf(l->out,",\"disconnected\":%s,\"resize_pending\":%s",p->disconnected?"true":"false",
            bt_remote_resize_pending(s)?"true":"false");
    fputs(",\"connection_error\":",l->out); quoted(l->out,p->connection_error);
    BtRemoteFrameStats transport=bt_remote_frame_stats(s);
    fprintf(l->out,",\"frame_transport\":{\"full_frames\":%llu,\"delta_frames\":%llu,\"unchanged_polls\":%llu,\"full_bytes\":%llu,\"delta_bytes\":%llu}",
            (unsigned long long)transport.full_frames,(unsigned long long)transport.delta_frames,
            (unsigned long long)transport.unchanged_polls,(unsigned long long)transport.full_bytes,
            (unsigned long long)transport.delta_bytes);
    fputs(",\"session\":",l->out); quoted(l->out,p->session_name);
    fputs(",\"session_dir\":",l->out); quoted(l->out,p->session_dir);
    fprintf(l->out,",\"session_epoch\":\"%016llx\"",(unsigned long long)p->session_epoch);
    const char *layouts[]={"splits","stack","tall","grid"};
    fputs(",\"layout\":",l->out); quoted(l->out,layouts[p->layout_mode]);
    fputs(",\"page_title\":",l->out); quoted(l->out,p->page_title);
    if(l->details) {
        fputs(",\"title\":",l->out); quoted(l->out,p->title);
        BtPaneTelemetry telemetry;
        (void)bt_workspace_telemetry_get(l->workspace,p->id,&telemetry);
        fputs(",\"overlay_activity\":",l->out); quoted(l->out,telemetry.activity);
        fputs(",\"overlay_process\":",l->out); quoted(l->out,telemetry.process);
        fputs(",\"overlay_agent\":",l->out); quoted(l->out,telemetry.agent);
        fputs(",\"overlay_task\":",l->out); quoted(l->out,telemetry.task);
    }
    fputc('}',l->out);
}
static bool generated_name(const BtControl *c, const char *name) {
    size_t prefix=strlen(c->terminate_prefix);
    if(!prefix || !name || strlen(name)!=prefix+24 || strncmp(name,c->terminate_prefix,prefix)) return false;
    for(const char *p=name+prefix;*p;++p) if((*p<'0' || *p>'9') && (*p<'a' || *p>'f')) return false;
    return true;
}
static void close_client(BtControl *c, ControlClient *client) {
    if(client->text_ticket) {
        Listing lookup={.id=client->pane}; bt_workspace_visit(c->workspace,visit,&lookup);
        if(lookup.view) bt_remote_text_cancel(&lookup.view->session,client->text_ticket);
    }
    if(client->fd>=0) close(client->fd);
    *client=(ControlClient){.fd=-1};
}
static void emit(BtControl *c, uint64_t pane, unsigned kind, unsigned changes) {
    ++c->sequence;
    c->changes[c->sequence%EVENTS]=(Change){c->sequence,pane,kind,changes};
    if(c->retained<EVENTS) ++c->retained;
}
typedef struct { Snapshot panes[BT_LAYOUT_PANES]; unsigned count; uint64_t scope_pane; } Samples;
static void snapshot(void *data, const BtPaneInfo *p) {
    Samples *samples=data;
    if(samples->scope_pane && samples->scope_pane!=p->id) return;
    Snapshot *v=&samples->panes[samples->count++];
    BtSession *s=&p->view->session;
    *v=(Snapshot){.id=p->id,.tab=p->tab,.output=s->bytes_read,.bounds=p->bounds,
        .cols=s->cols,.rows=s->rows,.layout_mode=p->layout_mode,.page_index=p->page_index,.cell_width=s->cell_width,.cell_height=s->cell_height,
        .pid=s->child,.status=s->exit_status,.recording=bt_session_recording(s),.exited=s->exited,.disconnected=p->disconnected,.resizing=bt_remote_resize_pending(s),
        .active=p->active,.tab_active=p->tab_active,.visible=p->visible,.synchronized=p->synchronized};
    snprintf(v->page_title,sizeof(v->page_title),"%s",p->page_title);
    snprintf(v->title,sizeof(v->title),"%s",p->title);
    const char *transcript=bt_session_transcript_id(s);
    if(transcript) snprintf(v->transcript_id,sizeof(v->transcript_id),"%s",transcript);
}
static void sample(BtControl *c) {
    Samples now={.scope_pane=c->scope_pane}; bt_workspace_visit(c->workspace,snapshot,&now);
    for(unsigned i=0;i<now.count;++i) {
        Snapshot *n=&now.panes[i],*old=NULL;
        for(unsigned j=0;j<BT_LAYOUT_PANES;++j) if(c->previous[j].id==n->id) { old=&c->previous[j]; break; }
        if(!old) { emit(c,n->id,1,0); continue; }
        unsigned changes=0;
        if(n->resizing!=old->resizing || n->layout_mode!=old->layout_mode || n->page_index!=old->page_index || n->tab!=old->tab || memcmp(&n->bounds,&old->bounds,sizeof(n->bounds)) ||
           n->cols!=old->cols || n->rows!=old->rows || n->active!=old->active ||
           n->cell_width!=old->cell_width || n->cell_height!=old->cell_height ||
           n->tab_active!=old->tab_active || n->visible!=old->visible || n->synchronized!=old->synchronized)
            changes|=CHANGE_LAYOUT;
        if(strcmp(n->title,old->title) || strcmp(n->page_title,old->page_title)) changes|=CHANGE_TITLE;
        if(n->output!=old->output) changes|=CHANGE_OUTPUT;
        if(n->pid!=old->pid || n->status!=old->status || n->recording!=old->recording ||
           strcmp(n->transcript_id,old->transcript_id) || n->exited!=old->exited || n->disconnected!=old->disconnected) changes|=CHANGE_STATUS;
        if(changes) emit(c,n->id,2,changes);
    }
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i) if(c->previous[i].id) {
        bool found=false;
        for(unsigned j=0;j<now.count;++j) if(now.panes[j].id==c->previous[i].id) { found=true; break; }
        if(!found) emit(c,c->previous[i].id,3,0);
    }
    memcpy(c->previous,now.panes,sizeof(c->previous));
}
/* One request per connection: BTC1, opcode, three zero bytes, pane u64 LE,
 * followed by opcode-specific bytes. Response: BTC1, status u32 LE, body.
 * Status zero is success; other statuses contain a UTF-8 diagnostic. */
static void reply(int fd, unsigned status, const void *body, size_t length) {
    unsigned char header[8]={'B','T','C','1',(unsigned char)status,0,0,0};
    struct iovec iov[]={{header,sizeof(header)},{(void *)body,length}};
    struct msghdr msg={.msg_iov=iov,.msg_iovlen=2};
    (void)sendmsg(fd,&msg,MSG_DONTWAIT|MSG_NOSIGNAL);
}
/* Return false only when the caller should wait for a change or heartbeat. */
static bool events(BtControl *c, int fd, uint64_t after, uint64_t epoch, bool heartbeat) {
    bool reset=epoch!=c->epoch || after>c->sequence || after<c->sequence-c->retained;
    if(!reset && after==c->sequence && !heartbeat) return false;
    char *body=NULL; size_t used=0; FILE *out=open_memstream(&body,&used);
    if(!out) { reply(fd,1,"Could not allocate event response",33); return true; }
    fprintf(out,"{\"version\":1,\"epoch\":\"%016llx\",\"cursor\":%llu,\"reset\":%s,\"events\":[",
        (unsigned long long)c->epoch,(unsigned long long)c->sequence,reset?"true":"false");
    if(!reset) for(uint64_t seq=after+1;seq<=c->sequence;++seq) {
        Change *event=&c->changes[seq%EVENTS];
        if(seq!=after+1) fputc(',',out);
        fprintf(out,"{\"sequence\":%llu,\"pane\":%llu,\"kind\":\"%s\",\"changes\":%u}",
            (unsigned long long)seq,(unsigned long long)event->pane,
            event->kind==1?"added":event->kind==3?"removed":"changed",event->changes);
    }
    fputc(']',out);
    if(reset) {
        fputs(",\"panes\":[",out);
        Listing listing={.out=out,.id=c->scope_pane,.first=true}; bt_workspace_visit(c->workspace,visit,&listing);
        fputc(']',out);
    }
    fputc('}',out);
    int failed=fclose(out);
    if(!failed && used<=PACKET-8) reply(fd,0,body,used);
    else reply(fd,1,"Event response exceeds control limit",36);
    free(body); return true;
}
static unsigned request(BtControl *c, ControlClient *client, unsigned char *packet, size_t length) {
    int fd=client->fd;
    const char *error="Malformed control request";
    char terminate_error[256];
    if(length<HEADER || memcmp(packet,"BTC1",4) || packet[5] || packet[6] || packet[7]) goto rejected;
    unsigned op=packet[4]; uint64_t id=read64(packet+8);
    unsigned char *payload=packet+HEADER; size_t size=length-HEADER;
    if(op<1 || op>26) { error="Unknown operation"; goto rejected; }
    bool reading=op==1 || op==2 || op==6 || op==9 || op==15 || op==17 || op==20 || op==24;
    if(c->read_only && !reading) { error="Endpoint is read-only"; goto rejected; }
    if(c->scope_pane) {
        bool allowed=op==1 || op==2 || op==6 || op==9 || op==15 ||
                     (!c->read_only && (op==4 || op==5));
        if(!allowed) { error="Operation outside pane scope"; goto rejected; }
        if(op!=1 && op!=2 && op!=15 && id!=c->scope_pane) {
            error="Pane outside endpoint scope"; goto rejected;
        }
    }
    if(op!=4 && op!=5 && op!=10 && op!=11 && op!=12 && op!=13 && op!=14 && op!=15 && op!=16 && op!=19 && op!=21 && op!=22 && op!=25 && op!=26 && size) goto rejected;
    if((op==1 || op==2 || op==17 || op==18 || op==20 || op==21 || op==23 || op==25 || op==26) && id) goto rejected;
    if(op==24 && !id) goto rejected;
    Listing lookup={.id=id};
    if(id) bt_workspace_visit(c->workspace,visit,&lookup);
    if(op!=1 && op!=2 && op!=10 && op!=14 && op!=15 && op!=17 && op!=18 && op!=20 && op!=21 && op!=23 && op!=24 && op!=25 && op!=26 && !lookup.view) { error="Pane does not exist"; goto rejected; }
    if(op==15) {
        if(size!=8) goto rejected;
        return !events(c,fd,id,read64(payload),false);
    }
    if(op==1) {
        if(c->scope_pane) {
            char body[256];
            int n=snprintf(body,sizeof(body),
                "{\"version\":1,\"read_only\":%s,\"scope_pane\":%llu,\"operations\":%s}",
                c->read_only?"true":"false",(unsigned long long)c->scope_pane,
                c->read_only?"[\"ping\",\"list\",\"dump\",\"info\",\"events\"]":
                             "[\"ping\",\"list\",\"send\",\"paste\",\"dump\",\"info\",\"events\"]");
            reply(fd,0,body,(size_t)n); return false;
        }
        const char *caps=c->read_only?
            "{\"version\":1,\"read_only\":true,\"operations\":[\"ping\",\"list\",\"dump\",\"info\",\"events\",\"checkpoint\",\"pane-center-state\"]}":
            "{\"version\":1,\"read_only\":false,\"operations\":[\"ping\",\"list\",\"focus\",\"send\",\"paste\",\"dump\",\"close\",\"zoom\",\"info\",\"launch\",\"resize\",\"sync\",\"move\",\"session\",\"events\",\"rename\",\"checkpoint\",\"pane-center\",\"telemetry\",\"pane-center-state\",\"layout-apply\",\"pane-rename\",\"message\",\"font-size\"]}";
        BtWorkspacePumpStats stats=bt_workspace_pump_stats(c->workspace);
        char body[1024];
        int n=snprintf(body,sizeof(body),"%.*s%s,\"pending_detaches\":%u,\"failed_detaches\":%llu}",
            (int)strlen(caps)-1,caps,c->host_actions?",\"host_actions\":[\"reload-settings\"]":"",
            stats.pending_detaches,(unsigned long long)stats.failed_detaches);
        reply(fd,0,body,(size_t)n); return false;
    }
    if(op==23 || op==24) {
        if(!c->host_actions) { error="Endpoint has no host actions"; goto rejected; }
        uint64_t ticket=id; bool done=false; unsigned status=0;
        int rc=op==23?bt_workspace_reload_request(c->workspace,&ticket):
                      bt_workspace_reload_result(c->workspace,ticket,&done,&status);
        if(rc) { error=bt_workspace_error(c->workspace); goto rejected; }
        char body[100]; int n=snprintf(body,sizeof(body),
            "{\"ticket\":%llu,\"done\":%s,\"status\":%u}",
            (unsigned long long)ticket,done?"true":"false",status);
        reply(fd,0,body,(size_t)n); return false;
    }
    if(op==25) {
        if(size>255 || memchr(payload,0,size)) goto rejected;
        char message[256]; memcpy(message,payload,size); message[size]=0;
        if(bt_workspace_message(c->workspace,message)) { error=bt_workspace_error(c->workspace); goto rejected; }
        reply(fd,0,"{}",2); return false;
    }
    if(op==26) {
        if(size!=1 || payload[0]<6 || payload[0]>96) goto rejected;
        if(bt_workspace_font_all(c->workspace,payload[0])) { error=bt_workspace_error(c->workspace); goto rejected; }
        sample(c); reply(fd,0,"{}",2); return false;
    }
    if(op==17) {
        /* No event dispatch or PTY pumping between these reads: layout IDs,
         * owner descriptors and appearance belong to the same host state. */
        BtWorkspaceLayout *state=malloc(sizeof(*state));
        uint8_t *bytes=NULL; size_t n=0;
        if(!state) { error="Could not allocate checkpoint"; goto rejected; }
        int rc=bt_workspace_layout_capture(c->workspace,state);
        if(!rc) rc=bt_workspace_layout_pack(state,&bytes,&n);
        free(state);
        if(rc) { free(bytes); error="Could not capture a nonempty workspace layout"; goto rejected; }
        char *body=NULL; size_t used=0; FILE *out=open_memstream(&body,&used);
        if(!out) { free(bytes); error="Could not allocate checkpoint response"; goto rejected; }
        fputs("{\"version\":1,\"layout_format\":\"BWL2\",\"layout_hex\":\"",out);
        static const char hex[]="0123456789abcdef";
        for(size_t i=0;i<n;++i) { fputc(hex[bytes[i]>>4],out); fputc(hex[bytes[i]&15],out); }
        free(bytes);
        BtWorkspaceAppearance a=bt_workspace_appearance(c->workspace);
        fprintf(out,"\",\"appearance\":{\"width\":%d,\"height\":%d,\"font_size\":%d,\"font\":",a.width,a.height,a.font_size);
        quoted(out,a.font);
        fprintf(out,",\"chrome\":%s,\"bottom_bar\":%s,\"start_badge\":%s,\"pane_buttons\":%u},\"panes\":[",
            a.chrome?"true":"false",a.bottom_bar?"true":"false",a.start_badge?"true":"false",a.pane_buttons);
        Listing listing={.out=out,.workspace=c->workspace,.first=true,.details=true};
        bt_workspace_visit(c->workspace,visit,&listing); fputs("]}",out);
        int failed=fclose(out);
        if(!failed && used<=PACKET-8) reply(fd,0,body,used);
        else { const char *message="Checkpoint exceeds control limit"; reply(fd,1,message,strlen(message)); }
        free(body); return false;
    }
    if(op==18) {
        if(bt_workspace_pane_center(c->workspace)) { error=bt_workspace_error(c->workspace); goto rejected; }
        static const char body[]="{\"open\":true}";
        reply(fd,0,body,sizeof(body)-1); return false;
    }
    if(op==20) {
        uint64_t ids[BT_LAYOUT_PANES]; unsigned count=0;
        bool open=bt_workspace_center_visible(c->workspace,ids,&count);
        char body[8192]; size_t used=0;
        used+=(size_t)snprintf(body+used,sizeof(body)-used,"{\"open\":%s,\"ids\":[",open?"true":"false");
        for(unsigned i=0;i<count;++i)
            used+=(size_t)snprintf(body+used,sizeof(body)-used,"%s%llu",i?",":"",(unsigned long long)ids[i]);
        used+=(size_t)snprintf(body+used,sizeof(body)-used,"]}");
        reply(fd,0,body,used); return false;
    }
    if(op==19) {
        if(size<2 || size>285) goto rejected;
        unsigned char *first=memchr(payload,0,size);
        if(!first) goto rejected;
        unsigned char *second=memchr(first+1,0,size-(size_t)(first+1-payload));
        if(!second) goto rejected;
        unsigned char *third=memchr(second+1,0,size-(size_t)(second+1-payload));
        if(third && memchr(third+1,0,size-(size_t)(third+1-payload))) goto rejected;
        size_t a=(size_t)(first-payload),p=(size_t)(second-first-1);
        size_t g=third?(size_t)(third-second-1):size-(size_t)(second+1-payload);
        size_t t=third?size-(size_t)(third+1-payload):0;
        if(!a || a>=16 || p>=65 || g>=41 || t>=161) goto rejected;
        char activity[16],process[65],agent[41],task[161];
        memcpy(activity,payload,a); activity[a]=0;
        memcpy(process,first+1,p); process[p]=0;
        memcpy(agent,second+1,g); agent[g]=0;
        if(t) memcpy(task,third+1,t);
        task[t]=0;
        if(bt_workspace_telemetry(c->workspace,id,activity,process,agent,task)) {
            error=bt_workspace_error(c->workspace); goto rejected;
        }
        reply(fd,0,"{}",2); return false;
    }
    if(op==21) {
        if(size<6) goto rejected;
        uint32_t bytes=read32(payload);
        unsigned count=(unsigned)payload[4]|(unsigned)payload[5]<<8;
        if(!bytes || bytes>BT_WORKSPACE_LAYOUT_MAX_BYTES || !count || count>BT_LAYOUT_PANES ||
           size!=6+(size_t)bytes+(size_t)count*16) goto rejected;
        BtWorkspaceLayout *state=NULL;
        if(bt_workspace_layout_unpack(payload+6,bytes,&state)) {
            error="Invalid workspace layout"; goto rejected;
        }
        BtPaneMapping mapping[BT_LAYOUT_PANES];
        const unsigned char *pairs=payload+6+bytes;
        for(unsigned i=0;i<count;++i) {
            mapping[i].saved=read64(pairs+i*16);
            mapping[i].current=read64(pairs+i*16+8);
        }
        int rc=bt_workspace_layout_apply(c->workspace,state,mapping,count);
        free(state);
        if(rc) { error=bt_workspace_error(c->workspace); goto rejected; }
        sample(c); reply(fd,0,"{}",2); return false;
    }
    if(op==2 || op==9) {
        char *body=NULL; size_t used=0; FILE *out=open_memstream(&body,&used);
        if(!out) { error="Could not allocate pane listing"; goto rejected; }
        fputs("{\"version\":1,\"panes\":[",out);
        Listing listing={.out=out,.workspace=c->workspace,.id=op==2 && c->scope_pane?c->scope_pane:id,
                         .first=true,.details=op==9};
        bt_workspace_visit(c->workspace,visit,&listing); fputs("]}",out);
        int failed=fclose(out);
        if(!failed && used<=PACKET-8) reply(fd,0,body,used);
        else reply(fd,1,"Pane listing exceeds control limit",34);
        free(body); return false;
    }
    if(op==6) {
        if(lookup.view->session.remote) {
            if(bt_remote_text_start(&lookup.view->session,false,PACKET-8,&client->text_ticket)) {
                error="Could not queue pane text"; goto rejected;
            }
            client->pane=id; client->deadline=bt_millis()+3000;
            return WAIT_TEXT;
        }
        size_t used=0; char *body=bt_session_text(&lookup.view->session,false,&used);
        if(!body) { error="Could not read pane text"; goto rejected; }
        if(used<=PACKET-8) reply(fd,0,body,used);
        else reply(fd,1,"Pane text exceeds control limit",31);
        free(body); return false;
    }
    int rc=0;
    if(op==3) {
        rc=bt_workspace_focus(c->workspace,id);
        if(!rc) bt_workspace_raise(c->workspace);
    }
    else if(op==7) {
        char name[49]="";
        if(lookup.view->session.remote && !bt_remote_observer(&lookup.view->session) &&
           lookup.session_dir && !strcmp(lookup.session_dir,c->root) && generated_name(c,lookup.session_name))
            snprintf(name,sizeof(name),"%s",lookup.session_name);
        /* A failed durable closure must retain the pane and its attachment;
         * otherwise the next autosave silently drops the still-live owner. */
        if(name[0] && bt_remote_terminate(c->root,name,terminate_error,sizeof(terminate_error))) {
            error=terminate_error; goto rejected;
        }
        rc=bt_workspace_close(c->workspace,id);
    }
    else if(op==8) rc=bt_workspace_zoom(c->workspace,id);
    else if(op==16 || op==22) {
        if(size>255 || memchr(payload,0,size)) goto rejected;
        char title[256]; memcpy(title,payload,size); title[size]=0;
        rc=op==16?bt_workspace_rename(c->workspace,id,title):bt_workspace_pane_rename(c->workspace,id,title);
    }
    else if(op==4 || op==5) {
        if(bt_remote_observer(&lookup.view->session)) { error="Observer pane cannot receive input"; goto rejected; }
        rc=op==4?bt_session_send(&lookup.view->session,payload,size):bt_window_paste(lookup.view,(const char *)payload,size);
        if(rc) { error="Could not deliver pane input"; goto rejected; }
    } else if(op==10 || op==14) {
        if(size<3 || payload[0]>BT_DOWN || payload[size-1]) goto rejected;
        char *argv[129]; unsigned count=0; size_t at=1;
        const char *name=NULL; unsigned mode=0;
        if(op==14) {
            if(size<4 || payload[1]>2) goto rejected;
            mode=payload[1]; name=(const char *)payload+2;
            size_t n=strlen(name);
            if(!n || n>48) goto rejected;
            at=3+n;
        }
        while(at<size && count<128) { argv[count++]=(char *)payload+at; at+=strlen((char *)payload+at)+1; }
        if(at!=size || (mode?count!=0:(!count || !*argv[0]))) goto rejected;
        argv[count]=NULL;
        BtPaneLaunch launch={.helper=c->helper,.service=c->service,.session_dir=c->root,
            .session_name=name,.attach=mode!=0,.observe=mode==2,.argv=argv,.env=c->env}; uint64_t created;
        if(bt_workspace_add(c->workspace,id,(BtDirection)payload[0],&launch,&created)) { error=bt_workspace_error(c->workspace); goto rejected; }
        char body[64]; int n=snprintf(body,sizeof(body),"{\"id\":%llu}",(unsigned long long)created);
        sample(c); reply(fd,0,body,(size_t)n); return false;
    } else if(op==11) {
        if(size!=5 || payload[0]>1) goto rejected;
        uint32_t raw=0; for(unsigned i=0;i<4;++i) raw|=(uint32_t)payload[i+1]<<(8*i);
        int64_t delta=raw<=INT32_MAX?(int64_t)raw:(int64_t)raw-4294967296LL;
        if(delta<-9998 || delta>9998) goto rejected;
        rc=bt_workspace_resize(c->workspace,id,payload[0]!=0,(int)delta);
    } else if(op==12) {
        if(size!=1 || payload[0]>1) goto rejected;
        rc=bt_workspace_synchronize(c->workspace,id,payload[0]!=0);
    } else if(op==13) {
        if(size!=1 || payload[0]>BT_DOWN) goto rejected;
        rc=bt_workspace_move(c->workspace,id,(BtDirection)payload[0]);
    }
    if(rc) { error=bt_workspace_error(c->workspace); goto rejected; }
    sample(c); reply(fd,0,"{}",2); return false;
rejected:
    reply(fd,1,error,strlen(error));
    return false;
}
BtControl *bt_control_new(BtWorkspace *workspace, const char *path, bool read_only,
                         const char *helper, const char *service, const char *root,
                         const char *terminate_prefix, uint64_t scope_pane, bool host_actions,
                         char *const env[], char *error, size_t capacity) {
    BtControl *c=calloc(1,sizeof(*c));
    if(!c) { snprintf(error,capacity,"Could not allocate control endpoint"); return NULL; }
    c->listener=c->directory=-1;
    for(unsigned i=0;i<CLIENTS;++i) c->clients[i].fd=-1;
    c->workspace=workspace; c->read_only=read_only; c->scope_pane=scope_pane;
    c->host_actions=host_actions;
    if(getrandom(&c->epoch,sizeof(c->epoch),0)!=(ssize_t)sizeof(c->epoch)) goto failed;
    if(!c->epoch) c->epoch=1;
    struct sockaddr_un address={.sun_family=AF_UNIX};
    if(!path || path[0]!='/' || strlen(path)>=sizeof(address.sun_path) || !helper || strlen(helper)>=sizeof(c->helper) ||
       !service || strlen(service)>=sizeof(c->service) || !root || root[0]!='/' || strlen(root)>=sizeof(c->root) ||
       (terminate_prefix && (!*terminate_prefix || strlen(terminate_prefix)>=sizeof(c->terminate_prefix)))) {
        errno=EINVAL; goto failed;
    }
    if(terminate_prefix) {
        for(const char *p=terminate_prefix;*p;++p)
            if(!((*p>='a' && *p<='z') || (*p>='0' && *p<='9') || *p=='-' || *p=='_')) {
                errno=EINVAL; goto failed;
            }
        strcpy(c->terminate_prefix,terminate_prefix);
    }
    strcpy(address.sun_path,path); strcpy(c->helper,helper);
    strcpy(c->service,service); strcpy(c->root,root);
    char parent[108]; strcpy(parent,path); char *slash=strrchr(parent,'/');
    if(!slash || !slash[1]) { errno=EINVAL; goto failed; }
    strcpy(c->name,slash+1); *slash=0;
    if(!*parent) { errno=EPERM; goto failed; }
    c->directory=open(parent,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
    struct stat st;
    if(c->directory<0 || fstat(c->directory,&st)) goto failed;
    if(st.st_uid!=geteuid() || (st.st_mode&077)!=0) { errno=EPERM; goto failed; }
    size_t count=0; if(env) while(env[count]) ++count;
    c->env=calloc(count+1,sizeof(*c->env)); if(!c->env) goto failed;
    for(size_t i=0;i<count;++i) if(!(c->env[i]=strdup(env[i]))) goto failed;
    c->listener=socket(AF_UNIX,SOCK_SEQPACKET|SOCK_NONBLOCK|SOCK_CLOEXEC,0);
    if(c->listener<0 || bind(c->listener,(struct sockaddr *)&address,sizeof(address))) goto failed;
    if(fstatat(c->directory,c->name,&st,AT_SYMLINK_NOFOLLOW)) goto failed;
    c->bound=true; c->device=st.st_dev; c->inode=st.st_ino;
    if(fchmodat(c->directory,c->name,0600,0) || listen(c->listener,CLIENTS)) goto failed;
    return c;
failed:
    snprintf(error,capacity,"Could not create private control endpoint: %s",strerror(errno));
    bt_control_free(c); return NULL;
}
static bool text_reply(BtControl *c, ControlClient *client) {
    Listing lookup={.id=client->pane}; bt_workspace_visit(c->workspace,visit,&lookup);
    const char *error="Pane closed before text reply";
    if(lookup.view) {
        char *text=NULL; size_t length=0;
        int rc=bt_remote_text_take(&lookup.view->session,client->text_ticket,&text,&length);
        if(rc==1) {
            if(bt_millis()<client->deadline) return false;
            error="Pane text request timed out";
        } else {
            client->text_ticket=0;
            if(!rc) { reply(client->fd,0,text,length); free(text); return true; }
            error=errno==E2BIG?"Pane text exceeds control limit":"Could not read pane text";
        }
    }
    reply(client->fd,1,error,strlen(error)); return true;
}
void bt_control_poll(BtControl *c) {
    if(!c) return;
    sample(c);
    for(unsigned i=0;i<CLIENTS;++i) if(c->clients[i].fd<0) {
        int fd=accept4(c->listener,NULL,NULL,SOCK_NONBLOCK|SOCK_CLOEXEC);
        if(fd<0) break;
        struct ucred peer; socklen_t length=sizeof(peer);
        if(getsockopt(fd,SOL_SOCKET,SO_PEERCRED,&peer,&length) || peer.uid!=geteuid()) { close(fd); continue; }
        c->clients[i]=(ControlClient){.fd=fd,.deadline=bt_millis()+1000};
    }
    unsigned processed=0;
    for(unsigned offset=0;offset<CLIENTS;++offset) {
        unsigned i=(c->cursor+offset)%CLIENTS;
        ControlClient *client=&c->clients[i];
        int fd=client->fd; if(fd<0) continue;
        struct pollfd peer={.fd=fd};
        bool closed=poll(&peer,1,0)>0 && (peer.revents&(POLLHUP|POLLERR|POLLNVAL));
        if(closed) { /* Cancellation below releases any pending text delivery. */ }
        else if(client->waiting==WAIT_TEXT) {
            if(!text_reply(c,client)) continue;
        } else if(client->waiting==WAIT_EVENTS) {
            if(!events(c,fd,client->after,client->epoch,bt_millis()>=client->deadline)) continue;
        } else {
            unsigned char packet[PACKET];
            ssize_t n=recv(fd,packet,sizeof(packet),MSG_DONTWAIT|MSG_TRUNC);
            if(n<0 && (errno==EAGAIN || errno==EWOULDBLOCK) && bt_millis()<c->clients[i].deadline) continue;
            if(n>0 && (size_t)n<=sizeof(packet)) {
                unsigned waiting=request(c,client,packet,(size_t)n);
                if(waiting) {
                    client->waiting=waiting;
                    if(waiting==WAIT_EVENTS) {
                        client->after=read64(packet+8); client->epoch=read64(packet+HEADER); client->deadline=bt_millis()+1000;
                    }
                    if(++processed==4) { c->cursor=(i+1)%CLIENTS; break; }
                    continue;
                }
            } else if(n>0) reply(fd,1,"Request exceeds control limit",29);
        }
        close_client(c,client);
        if(++processed==4) { c->cursor=(i+1)%CLIENTS; break; }
    }
}
void bt_control_free(BtControl *c) {
    if(!c) return;
    if(c->listener>=0) close(c->listener);
    for(unsigned i=0;i<CLIENTS;++i) close_client(c,&c->clients[i]);
    struct stat st;
    if(c->bound && !fstatat(c->directory,c->name,&st,AT_SYMLINK_NOFOLLOW) &&
       st.st_dev==c->device && st.st_ino==c->inode) unlinkat(c->directory,c->name,0);
    if(c->directory>=0) close(c->directory);
    if(c->env) { for(char **p=c->env;*p;++p) free(*p); free(c->env); }
    free(c);
}
