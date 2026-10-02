/* SPDX-License-Identifier: MIT */
/* Frame Sixel DCS streams, decode them, and reuse the terminal's image storage.
 * All injected VT writes happen outside Ghostty callbacks. Other VT data keeps
 * its original byte order and is forwarded in batches. */
#include "graphics.h"
#include "sixel.h"
#include <png.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define IMAGE_BYTES (64u * 1024u * 1024u)
enum State { GROUND, ESCAPE, CSI, DCS_HEADER, STRING, STRING_ESCAPE, SIXEL, SIXEL_ESCAPE };
struct BtGraphics {
    GhosttyTerminal terminal;
    BtGraphicsReply reply;
    void *reply_userdata;
    enum State state;
    unsigned cols, rows, cw, ch, utf8;
    uint32_t next_sixel_id;
    uint8_t header[256];
    size_t used;
    bool osc, page_mode, scroll_right, private_palette, palette_valid;
    BtSixel *sixel;
    BtSixelPalette palette;
};
static unsigned users;

static bool decode_png(void *userdata, const GhosttyAllocator *allocator,
                       const uint8_t *data, size_t length, GhosttySysImage *out) {
    (void)userdata;
    if(length>IMAGE_BYTES) return false;
    png_image image={.version=PNG_IMAGE_VERSION};
    if(!png_image_begin_read_from_memory(&image,data,length)) { png_image_free(&image); return false; }
    uint64_t bytes=(uint64_t)image.width*image.height*4;
    if(!image.width || !image.height || image.width>10000 || image.height>10000 || bytes>IMAGE_BYTES) {
        png_image_free(&image); return false;
    }
    image.format=PNG_FORMAT_RGBA;
    uint8_t *pixels=ghostty_alloc(allocator,(size_t)bytes);
    if(!pixels) { png_image_free(&image); return false; }
    bool ok=png_image_finish_read(&image,NULL,pixels,0,NULL)!=0;
    if(ok) *out=(GhosttySysImage){image.width,image.height,pixels,(size_t)bytes};
    else ghostty_free(allocator,pixels,(size_t)bytes);
    png_image_free(&image);
    return ok;
}
static bool attributes(GhosttyTerminal terminal, void *userdata, GhosttyDeviceAttributes *out) {
    (void)terminal; (void)userdata;
    memset(out,0,sizeof(*out));
    out->primary.conformance_level=GHOSTTY_DA_CONFORMANCE_VT220;
    out->primary.features[0]=GHOSTTY_DA_FEATURE_SIXEL;
    out->primary.features[1]=GHOSTTY_DA_FEATURE_ANSI_COLOR;
    out->primary.num_features=2;
    out->secondary.device_type=GHOSTTY_DA_DEVICE_TYPE_VT220;
    out->secondary.firmware_version=1;
    return true;
}
BtGraphics *bt_graphics_new(GhosttyTerminal terminal) {
    BtGraphics *g=calloc(1,sizeof(*g));
    if(!g) return NULL;
    g->terminal=terminal;
    g->cols=80; g->rows=24; g->cw=8; g->ch=16;
    g->next_sixel_id=UINT32_MAX;
    uint64_t limit=IMAGE_BYTES;
    bool shared_memory=true;
    const char *local_files=getenv("BATTY_KITTY_LOCAL_FILES");
    bool file_medium=local_files && !strcmp(local_files,"1");
    const char *temp_dir=getenv("TMPDIR");
    if(!temp_dir || !*temp_dir) temp_dir="/tmp";
    GhosttyString temporary={(const uint8_t *)temp_dir,strlen(temp_dir)};
    if(ghostty_terminal_set(terminal,GHOSTTY_TERMINAL_OPT_KITTY_IMAGE_STORAGE_LIMIT,&limit)!=GHOSTTY_SUCCESS ||
       ghostty_terminal_set(terminal,GHOSTTY_TERMINAL_OPT_KITTY_IMAGE_MEDIUM_SHARED_MEM,&shared_memory)!=GHOSTTY_SUCCESS ||
       (file_medium &&
        (ghostty_terminal_set(terminal,GHOSTTY_TERMINAL_OPT_KITTY_IMAGE_MEDIUM_FILE,&file_medium)!=GHOSTTY_SUCCESS ||
         ghostty_terminal_set(terminal,GHOSTTY_TERMINAL_OPT_KITTY_IMAGE_MEDIUM_TEMP_FILE,&temporary)!=GHOSTTY_SUCCESS)) ||
       ghostty_terminal_set(terminal,GHOSTTY_TERMINAL_OPT_DEVICE_ATTRIBUTES,(const void *)attributes)!=GHOSTTY_SUCCESS ||
       (!users && ghostty_sys_set(GHOSTTY_SYS_OPT_DECODE_PNG,(const void *)decode_png)!=GHOSTTY_SUCCESS)) {
        free(g); return NULL;
    }
    ++users;
    return g;
}
void bt_graphics_free(BtGraphics *g) {
    if(!g) return;
    bt_sixel_free(g->sixel);
    free(g);
    if(users && !--users) ghostty_sys_set(GHOSTTY_SYS_OPT_DECODE_PNG,NULL);
}
void bt_graphics_resize(BtGraphics *g, unsigned cols, unsigned rows, unsigned cw, unsigned ch) {
    if(g) { g->cols=cols; g->rows=rows; g->cw=cw; g->ch=ch; }
}
void bt_graphics_set_reply(BtGraphics *g, BtGraphicsReply reply, void *userdata) {
    if(g) { g->reply=reply; g->reply_userdata=userdata; }
}
static void vt(BtGraphics *g, const void *data, size_t length) {
    if(length) ghostty_terminal_vt_write(g->terminal,data,length);
}
static void literal(BtGraphics *g, const char *s) { vt(g,s,strlen(s)); }
static void transmit(BtGraphics *g, const BtSixelImage *image) {
    /* At the pinned API, deleting the nonexistent ID zero cancels an unfinished
     * Kitty transmission. Stored IDs are nonzero; existing images survive. */
    literal(g,"\033_Ga=d,d=i,i=0,q=2\033\\");
    char placement[64]="";
    uint32_t page_image_id=0;
    if(g->page_mode) {
        /* An explicit ID lets us anchor the completed image at the page origin
         * without changing the cursor or introducing a scrolling parent. */
        GhosttyKittyGraphics storage;
        if(ghostty_terminal_get(g->terminal,GHOSTTY_TERMINAL_DATA_KITTY_GRAPHICS,&storage)!=GHOSTTY_SUCCESS) return;
        while(!g->next_sixel_id || ghostty_kitty_graphics_image(storage,g->next_sixel_id)) --g->next_sixel_id;
        page_image_id=g->next_sixel_id--;
        snprintf(placement,sizeof(placement),",i=%u,p=1",page_image_id);
    }
    const char alphabet[]="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    size_t size=(size_t)image->width*image->height*4, offset=0;
    while(offset<size) {
        size_t count=size-offset; if(count>3072) count=3072;
        bool more=offset+count<size;
        char header[256], encoded[4096];
        int n=offset ? snprintf(header,sizeof(header),"\033_Gm=%d;",more) :
            snprintf(header,sizeof(header),"\033_Ga=T,f=32,t=d,q=2,C=1,z=-1,s=%u,v=%u,m=%d%s;",
                     image->width,image->height,more,placement);
        vt(g,header,(size_t)n);
        size_t out=0;
        for(size_t i=0;i<count;i+=3) {
            unsigned a=image->pixels[offset+i], b=i+1<count?image->pixels[offset+i+1]:0;
            unsigned c=i+2<count?image->pixels[offset+i+2]:0;
            encoded[out++]=alphabet[a>>2]; encoded[out++]=alphabet[((a&3)<<4)|(b>>4)];
            encoded[out++]=i+1<count?alphabet[((b&15)<<2)|(c>>6)]:'=';
            encoded[out++]=i+2<count?alphabet[c&63]:'=';
        }
        vt(g,encoded,out); literal(g,"\033\\"); offset+=count;
    }
    if(page_image_id && ghostty_kitty_graphics_placement_set_position(g->terminal,page_image_id,1,0,0)!=GHOSTTY_SUCCESS) {
        char command[64];
        int n=snprintf(command,sizeof(command),"\033_Ga=d,d=I,i=%u,q=2\033\\",page_image_id);
        vt(g,command,(size_t)n);
    }
}
static void finish_sixel(BtGraphics *g, bool cancelled) {
    BtSixelImage image={0};
    if(!cancelled && g->sixel && !bt_sixel_finish(g->sixel,&image)) {
        if(!g->private_palette) {
            bt_sixel_get_palette(g->sixel,&g->palette); g->palette_valid=true;
        }
        if(image.width && image.height) {
            transmit(g,&image);
            if(!g->page_mode) {
                unsigned lines=(image.height+g->ch-1)/g->ch;
                for(unsigned i=1;i<lines;++i) literal(g,"\033D");
                if(g->scroll_right) {
                    char seq[32]; int n=snprintf(seq,sizeof(seq),"\033[%uC",(image.width+g->cw-1)/g->cw);
                    vt(g,seq,(size_t)n);
                }
            }
        }
        free(image.pixels);
    }
    bt_sixel_free(g->sixel); g->sixel=NULL; g->state=GROUND; g->utf8=0;
}
static void start_sixel(BtGraphics *g) {
    unsigned params[3]={0}; size_t index=0;
    for(size_t i=2;i+1<g->used;++i) {
        uint8_t c=g->header[i];
        if(c==';') { if(++index>=3) break; }
        else if(c>='0' && c<='9') {
            if(params[index]<100000) params[index]=params[index]*10+(c-'0');
        } else { g->state=STRING; vt(g,g->header,g->used); return; }
    }
    GhosttyColorRgb bg={19,23,30};
    ghostty_terminal_get(g->terminal,GHOSTTY_TERMINAL_DATA_COLOR_BACKGROUND,&bg);
    uint8_t background[]={bg.r,bg.g,bg.b};
    g->sixel=bt_sixel_new(params[0],params[1],background);
    if(g->sixel && !g->private_palette && g->palette_valid)
        (void)bt_sixel_set_palette(g->sixel,&g->palette);
    g->state=SIXEL;
}
static bool csi_modes(BtGraphics *g) {
    if(g->used<4 || g->header[2]!='?') return false;
    uint8_t final=g->header[g->used-1];
    if(final=='p' && g->used>=6 && g->header[g->used-2]=='$') {
        unsigned mode=0;
        for(size_t i=3;i+2<g->used;++i) {
            uint8_t c=g->header[i];
            if(c<'0' || c>'9' || mode>100000) return false;
            mode=mode*10+c-'0';
        }
        bool set;
        if(mode==80) set=g->page_mode;
        else if(mode==8452) set=g->scroll_right;
        else if(mode==1070) set=g->private_palette;
        else return false;
        char response[32];
        int n=snprintf(response,sizeof(response),"\033[?%u;%u$y",mode,set?1:2);
        if(g->reply) g->reply(g->reply_userdata,(const uint8_t *)response,(size_t)n);
        return true;
    }
    if(final!='h' && final!='l') return false;
    for(size_t i=3;i+1<g->used;++i)
        if((g->header[i]<'0' || g->header[i]>'9') && g->header[i]!=';') return false;
    unsigned value=0;
    for(size_t i=3;i<g->used;++i) {
        uint8_t c=g->header[i];
        if(c>='0' && c<='9') { if(value<100000) value=value*10+c-'0'; }
        else if(c==';' || i==g->used-1) {
            if(value==80) g->page_mode=final=='h';
            if(value==8452) g->scroll_right=final=='h';
            if(value==1070) g->private_palette=final=='h';
            value=0;
        }
    }
    return false;
}
void bt_graphics_feed(BtGraphics *g, const void *input, size_t length) {
    const uint8_t *bytes=input;
    size_t i=0;
    while(i<length) {
        uint8_t c=bytes[i];
        if(g->state==GROUND) {
            size_t start=i;
            while(i<length) {
                c=bytes[i];
                if(g->utf8 && (c&0xc0)==0x80) { --g->utf8; ++i; continue; }
                g->utf8=0;
                if(c==27 || c==0x90 || c==0x9b || c==0x9d || c==0x9e || c==0x9f) break;
                if(c>=0xc2 && c<=0xdf) g->utf8=1;
                else if(c>=0xe0 && c<=0xef) g->utf8=2;
                else if(c>=0xf0 && c<=0xf4) g->utf8=3;
                ++i;
            }
            vt(g,bytes+start,i-start);
            if(i==length) break;
            c=bytes[i++]; g->used=0; g->header[g->used++]=27;
            if(c==27) { g->state=ESCAPE; continue; }
            uint8_t mapped=c==0x90?'P':c==0x9b?'[':c==0x9d?']':c==0x9e?'^':'_';
            g->header[g->used++]=mapped;
            if(mapped=='P') g->state=DCS_HEADER;
            else if(mapped=='[') g->state=CSI;
            else { vt(g,g->header,g->used); g->osc=mapped==']'; g->state=STRING; }
        } else if(g->state==ESCAPE) {
            ++i;
            if(c==27) continue;
            if(c<0x20 || c==0x7f) {
                vt(g,&c,1);
                if(c==0x18 || c==0x1a) g->state=GROUND;
                continue;
            }
            g->header[g->used++]=c;
            if(c=='P') g->state=DCS_HEADER;
            else if(c=='[') g->state=CSI;
            else {
                vt(g,g->header,g->used);
                if(c==']' || c=='_' || c=='^') { g->osc=c==']'; g->state=STRING; }
                else { g->state=GROUND; if(c=='c') { g->page_mode=g->scroll_right=g->private_palette=g->palette_valid=false; } }
            }
        } else if(g->state==CSI || g->state==DCS_HEADER) {
            if(c==27 || c==0x18 || c==0x1a) {
                vt(g,g->header,g->used); g->state=GROUND; continue;
            }
            ++i;
            if(c<0x20 || c==0x7f) { vt(g,&c,1); continue; }
            if(g->used==sizeof(g->header)) {
                vt(g,g->header,g->used); vt(g,&c,1);
                g->state=g->state==DCS_HEADER?STRING:GROUND; g->osc=false; continue;
            }
            g->header[g->used++]=c;
            if(c>=0x40 && c<=0x7e) {
                if(g->state==DCS_HEADER) {
                    g->osc=false;
                    if(c=='q') start_sixel(g);
                    else { vt(g,g->header,g->used); g->state=STRING; }
                } else { if(!csi_modes(g)) vt(g,g->header,g->used); g->state=GROUND; }
            }
        } else if(g->state==SIXEL) {
            size_t start=i;
            while(i<length && bytes[i]!=27 && bytes[i]!=0x9c && bytes[i]!=0x18 && bytes[i]!=0x1a) ++i;
            if(g->sixel && bt_sixel_feed(g->sixel,bytes+start,i-start)) {
                bt_sixel_free(g->sixel); g->sixel=NULL;
            }
            if(i==length) break;
            c=bytes[i++];
            if(c==27) g->state=SIXEL_ESCAPE;
            else finish_sixel(g,c!=0x9c);
        } else if(g->state==SIXEL_ESCAPE) {
            if(c=='\\') { ++i; finish_sixel(g,false); }
            else { finish_sixel(g,true); g->state=ESCAPE; g->used=1; g->header[0]=27; }
        } else if(g->state==STRING) {
            size_t start=i;
            while(i<length) {
                c=bytes[i];
                if(g->utf8 && (c&0xc0)==0x80) { --g->utf8; ++i; continue; }
                g->utf8=0;
                if(c==27 || c==0x18 || c==0x1a || c==0x9c || (g->osc && c==7)) break;
                if(c>=0xc2 && c<=0xdf) g->utf8=1;
                else if(c>=0xe0 && c<=0xef) g->utf8=2;
                else if(c>=0xf0 && c<=0xf4) g->utf8=3;
                ++i;
            }
            vt(g,bytes+start,i-start);
            if(i==length) break;
            c=bytes[i++];
            if(c==0x9c) { literal(g,"\033\\"); g->state=GROUND; continue; }
            if(c!=27) vt(g,&c,1);
            g->state=c==27?STRING_ESCAPE:GROUND;
        } else if(g->state==STRING_ESCAPE) {
            if(c=='\\') { literal(g,"\033\\"); ++i; g->state=GROUND; }
            else {
                /* The held ESC terminates the preceding string even when the
                 * next sequence is handled here (mode query or Sixel). Finish
                 * it in Ghostty before buffering that next escape sequence. */
                literal(g,"\033\\");
                g->state=ESCAPE; g->used=1; g->header[0]=27;
            }
        }
    }
}
