/* SPDX-License-Identifier: MIT */
#define _GNU_SOURCE
#include "recovery.h"
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

enum { HEADER=48, ARGS_LIMIT=65536 };
static uint32_t get32(const uint8_t *p) {
    return (uint32_t)p[0]|(uint32_t)p[1]<<8|(uint32_t)p[2]<<16|(uint32_t)p[3]<<24;
}
static void put32(uint8_t *p, uint32_t n) {
    for(unsigned i=0;i<4;++i) p[i]=(uint8_t)(n>>(8*i));
}
int bt_recovery_capture(BtSession *s, const BtPresentation *frame, char *const argv[],
                        uint8_t **out, size_t *length) {
    *out=NULL; *length=0;
    if(s->recovery_argv) argv=s->recovery_argv;
    GhosttyFormatter formatter=NULL;
    GhosttyFormatterTerminalOptions options=GHOSTTY_INIT_SIZED(GhosttyFormatterTerminalOptions);
    options.emit=GHOSTTY_FORMATTER_FORMAT_VT;
    options.unwrap=true;
    options.extra.size=sizeof(options.extra); options.extra.palette=true;
    options.extra.screen.size=sizeof(options.extra.screen);
    if(ghostty_formatter_terminal_new(NULL,&formatter,s->terminal,options)!=GHOSTTY_SUCCESS) {
        errno=ENOMEM; return -1;
    }
    size_t vt_size=0, frame_size=0, args_size=0;
    GhosttyResult result=ghostty_formatter_format_buf(formatter,NULL,0,&vt_size);
    uint8_t *encoded=NULL,*data=NULL;
    if(result!=GHOSTTY_OUT_OF_SPACE && result!=GHOSTTY_SUCCESS) goto failed;
    if(bt_presentation_pack(frame,&encoded,&frame_size)) goto failed;
    for(unsigned i=0;argv[i];++i) {
        size_t n=strlen(argv[i])+1;
        if(n>ARGS_LIMIT-args_size) { errno=E2BIG; goto failed; }
        args_size+=n;
    }
    char proc[64],cwd[4096];
    snprintf(proc,sizeof(proc),"/proc/%d/cwd",(int)s->child);
    ssize_t n=readlink(proc,cwd,sizeof(cwd)-1);
    if(n<0) { if(!getcwd(cwd,sizeof(cwd))) cwd[0]=0; }
    else cwd[n]=0;
    size_t cwd_size=strlen(cwd);
    uint64_t total=HEADER+(uint64_t)vt_size+frame_size+args_size+cwd_size;
    if(total>BT_PRESENTATION_MAX_BYTES) { errno=E2BIG; goto failed; }
    data=calloc(1,(size_t)total);
    if(!data) goto failed;
    memcpy(data,"BTRCV001",8);
    const uint32_t fields[]={1,(uint32_t)total,(uint32_t)vt_size,(uint32_t)frame_size,
        (uint32_t)args_size,(uint32_t)cwd_size,frame->cols,frame->rows,frame->cell_width,frame->cell_height};
    for(unsigned i=0;i<10;++i) put32(data+8+i*4,fields[i]);
    if(ghostty_formatter_format_buf(formatter,data+HEADER,vt_size,&vt_size)!=GHOSTTY_SUCCESS) goto failed;
    size_t at=HEADER+vt_size;
    memcpy(data+at,encoded,frame_size); at+=frame_size;
    for(unsigned i=0;argv[i];++i) { size_t count=strlen(argv[i])+1; memcpy(data+at,argv[i],count); at+=count; }
    memcpy(data+at,cwd,cwd_size);
    free(encoded); ghostty_formatter_free(formatter);
    *out=data; *length=(size_t)total; return 0;
failed:
    free(encoded); free(data); ghostty_formatter_free(formatter);
    if(!errno) errno=ENOMEM;
    return -1;
}
static void feed(BtSession *s, const char *text) { bt_session_feed(s,text,strlen(text)); }
static void base64(BtSession *s, const uint8_t *pixels, size_t length) {
    static const char alphabet[]="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    char chunk[4096];
    for(size_t at=0;at<length;) {
        size_t used=0;
        while(at<length && used<sizeof(chunk)) {
            size_t remaining=length-at;
            uint32_t n=(uint32_t)pixels[at]<<16;
            if(remaining>1) n|=(uint32_t)pixels[at+1]<<8;
            if(remaining>2) n|=pixels[at+2];
            chunk[used++]=alphabet[n>>18]; chunk[used++]=alphabet[n>>12&63];
            chunk[used++]=remaining>1?alphabet[n>>6&63]:'=';
            chunk[used++]=remaining>2?alphabet[n&63]:'=';
            at+=remaining<3?remaining:3;
        }
        bt_session_feed(s,chunk,used);
        if(at<length) feed(s,"\033\\\033_Gm=1;");
    }
}
/* Reconstruct the visible static frame at its resolved pixel size. Each
 * placement gets independent pixels, so crops, offsets and scaling survive
 * without depending on the old process's image IDs or animation timers. */
static int graphics(BtSession *s, const BtPresentation *frame) {
    for(size_t i=0;i<frame->placement_count;++i) {
        const BtPresentationPlacement *p=&frame->placements[i];
        const GhosttyKittyGraphicsPlacementRenderInfo *g=&p->geometry;
        const BtPresentationImage *image=&frame->images[p->image_index];
        int64_t x=(int64_t)g->viewport_col*frame->cell_width+p->x_offset;
        int64_t y=(int64_t)g->viewport_row*frame->cell_height+p->y_offset;
        uint32_t left=x<0?(uint32_t)-x:0,top=y<0?(uint32_t)-y:0;
        if(left>=g->pixel_width || top>=g->pixel_height) continue;
        unsigned width=g->pixel_width-left,height=g->pixel_height-top;
        uint64_t length=(uint64_t)width*height*4;
        if(length>BT_PRESENTATION_MAX_IMAGE_BYTES) { errno=E2BIG; return -1; }
        uint8_t *pixels=malloc((size_t)length);
        if(!pixels) return -1;
        for(unsigned row=0;row<height;++row) for(unsigned col=0;col<width;++col) {
            unsigned sx=g->source_x+(uint64_t)(col+left)*g->source_width/g->pixel_width;
            unsigned sy=g->source_y+(uint64_t)(row+top)*g->source_height/g->pixel_height;
            const uint8_t *source=image->pixels+((size_t)sy*image->width+sx)*image->channels;
            uint8_t *target=pixels+((size_t)row*width+col)*4;
            target[0]=source[0]; target[1]=image->channels<3?source[0]:source[1];
            target[2]=image->channels<3?source[0]:source[2];
            target[3]=image->channels==2?source[1]:image->channels==4?source[3]:255;
        }
        if(x<0) x=0;
        if(y<0) y=0;
        char command[256];
        /* Chunk framing marks every nonfinal transmission m=1. */
        snprintf(command,sizeof(command),"\033[%llu;%lluH\033_Ga=T,f=32,s=%u,v=%u,i=%u,p=%u,z=%d,X=%u,Y=%u,C=1,q=2,m=1;",
            (unsigned long long)(y/frame->cell_height+1),(unsigned long long)(x/frame->cell_width+1),
            width,height,UINT32_MAX-(unsigned)i,1,p->z,(unsigned)(x%frame->cell_width),(unsigned)(y%frame->cell_height));
        feed(s,command); base64(s,pixels,(size_t)length); free(pixels);
        feed(s,"\033\\\033_Gm=0,q=2;\033\\");
    }
    return 0;
}
int bt_recovery_load(BtSession *s, const char *path) {
    int fd=open(path,O_RDONLY|O_CLOEXEC|O_NOFOLLOW|O_NONBLOCK);
    if(fd<0) return -1;
    struct stat info;
    uint8_t *data=NULL; BtPresentation *frame=NULL; int rc=-1;
    if(fstat(fd,&info)<0) goto done;
    if(!S_ISREG(info.st_mode) || info.st_uid!=geteuid() || (info.st_mode&077)!=0 ||
       info.st_size<HEADER || info.st_size>BT_PRESENTATION_MAX_BYTES) { errno=EINVAL; goto done; }
    data=malloc((size_t)info.st_size);
    if(!data) goto done;
    for(size_t at=0;at<(size_t)info.st_size;) {
        ssize_t n=read(fd,data+at,(size_t)info.st_size-at);
        if(n<0 && errno==EINTR) continue;
        if(n<=0) { if(!n) errno=EIO; goto done; }
        at+=(size_t)n;
    }
    uint32_t version=get32(data+8),total=get32(data+12),vt=get32(data+16),scene=get32(data+20);
    uint32_t args=get32(data+24),cwd=get32(data+28);
    if(memcmp(data,"BTRCV001",8) || version!=1 || total!=(size_t)info.st_size ||
       (uint64_t)HEADER+vt+scene+args+cwd!=total || args>ARGS_LIMIT || cwd>=4096 ||
       bt_presentation_unpack(data+HEADER+vt,scene,&frame)) { errno=EINVAL; goto done; }
    if(frame->cols!=get32(data+32) || frame->rows!=get32(data+36) ||
       frame->cell_width!=get32(data+40) || frame->cell_height!=get32(data+44)) { errno=EINVAL; goto done; }
    const uint8_t *arguments=data+HEADER+vt+scene;
    if(!args || !arguments[0] || arguments[args-1]) { errno=EINVAL; goto done; }
    size_t count=0;
    for(size_t i=0;i<args;++i) if(!arguments[i]) ++count;
    char *saved=malloc(args);
    char **launch=calloc(count+1,sizeof(*launch));
    if(!saved || !launch) { free(saved); free(launch); goto done; }
    memcpy(saved,arguments,args); launch[0]=saved;
    for(size_t i=0,index=1;i+1<args;++i) if(!saved[i]) launch[index++]=saved+i+1;
    free(s->recovery_arguments); free(s->recovery_argv);
    s->recovery_arguments=saved; s->recovery_argv=launch;
    unsigned cols=s->cols,rows=s->rows,cw=s->cell_width,ch=s->cell_height;
    if(bt_session_resize(s,frame->cols,frame->rows,frame->cell_width,frame->cell_height)) goto done;
    s->restoring=true;
    char colors[128];
    snprintf(colors,sizeof(colors),"\033]10;rgb:%02x/%02x/%02x\033\\\033]11;rgb:%02x/%02x/%02x\033\\",
        frame->colors.foreground.r,frame->colors.foreground.g,frame->colors.foreground.b,
        frame->colors.background.r,frame->colors.background.g,frame->colors.background.b);
    feed(s,colors);
    /* The formatter emits LF separators; terminal output needs CR as well. */
    for(size_t at=0,start=0;at<=vt;++at) {
        if(at==vt || data[HEADER+at]=='\n') {
            bt_session_feed(s,data+HEADER+start,at-start);
            if(at<vt) feed(s,"\r\n");
            start=at+1;
        }
    }
    /* Graphics placement moves the cursor. Remember the end of the replayed
     * text first, and leave the new prompt below any visible image as well.
     * Sparse output stays in view; a full screen scrolls by just one row. */
    uint16_t cursor_x=0,cursor_y=0;
    bool pending_wrap=false;
    ghostty_terminal_get(s->terminal,GHOSTTY_TERMINAL_DATA_CURSOR_X,&cursor_x);
    ghostty_terminal_get(s->terminal,GHOSTTY_TERMINAL_DATA_CURSOR_Y,&cursor_y);
    ghostty_terminal_get(s->terminal,GHOSTTY_TERMINAL_DATA_CURSOR_PENDING_WRAP,&pending_wrap);
    unsigned prompt_row=cursor_y+((cursor_x || pending_wrap)?1u:0u);
    for(size_t i=0;i<frame->placement_count;++i) {
        const BtPresentationPlacement *p=&frame->placements[i];
        int64_t bottom=(int64_t)p->geometry.viewport_row*frame->cell_height+p->y_offset+p->geometry.pixel_height;
        if(bottom>0) {
            uint64_t row=((uint64_t)bottom+frame->cell_height-1)/frame->cell_height;
            if(row>prompt_row) prompt_row=row>frame->rows?frame->rows:(unsigned)row;
        }
    }
    rc=graphics(s,frame);
    char prompt[64]; snprintf(prompt,sizeof(prompt),"\033[0m\033[?25h\033[%u;1H%s",
        prompt_row<frame->rows?prompt_row+1:frame->rows,prompt_row<frame->rows?"":"\r\n");
    feed(s,prompt); s->restoring=false;
    if(bt_session_resize(s,cols,rows,cw,ch)) rc=-1;
done:
    { int error=errno; free(data); bt_presentation_free(frame); close(fd); errno=error; }
    return rc;
}
void bt_recovery_forget(const char *directory, uint64_t epoch) {
    if(!directory || !*directory) return;
    int root=open(directory,O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW);
    if(root<0) return;
    struct stat info;
    if(!fstat(root,&info) && info.st_uid==geteuid() && !(info.st_mode&077)) {
        char name[40]; snprintf(name,sizeof(name),"closed-%016llx",(unsigned long long)epoch);
        int fd=openat(root,name,O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC|O_NOFOLLOW,0600);
        if(fd>=0) { (void)fsync(fd); close(fd); (void)fsync(root); }
    }
    close(root);
}
