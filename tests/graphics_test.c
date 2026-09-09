/* SPDX-License-Identifier: MIT */
/* Native image protocol and framebuffer checks. Run from the project root. */
#define _GNU_SOURCE
#include "window.h"
#include <limits.h>
#include <png.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

extern char **environ;
static BtWindow window;
static unsigned cell_width, cell_height;
static struct { unsigned width, height; uint8_t *pixels; } frame;
static const uint8_t background[3]={19,23,30};
enum { PAD=8 };

static void require(bool ok, const char *message) {
    if(ok) return;
    fprintf(stderr,"FAIL graphics: %s\nwindow: %s\nsession: %s\nrenderer: %s\n",
            message,window.error,window.session.error,
            window.renderer?bt_renderer_error(window.renderer):"not created");
    free(frame.pixels);
    bt_window_close(&window);
    exit(1);
}
static void checked(GhosttyResult result, const char *message) {
    require(result==GHOSTTY_SUCCESS,message);
}
static void feed(const void *bytes,size_t size,size_t fragment) {
    const uint8_t *p=bytes;
    if(!fragment) fragment=size?size:1;
    while(size) {
        size_t n=size<fragment?size:fragment;
        bt_session_feed(&window.session,p,n);
        p+=n; size-=n;
    }
}
static void text(const char *bytes) { feed(bytes,strlen(bytes),0); }
static void control(const char *format,...) {
    char bytes[512];
    va_list args; va_start(args,format);
    int length=vsnprintf(bytes,sizeof(bytes),format,args);
    va_end(args);
    require(length>=0 && (size_t)length<sizeof(bytes),"control fixture length");
    feed(bytes,(size_t)length,0);
}
static void at(unsigned col,unsigned row) { control("\033[%u;%uH",row+1,col+1); }
static unsigned px(unsigned col) { return PAD+col*cell_width; }
static unsigned py(unsigned row) { return PAD+row*cell_height; }

static char *base64(const uint8_t *bytes,size_t length) {
    static const char alphabet[]="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    require(length<(SIZE_MAX-3)/4*3,"base64 fixture bound");
    char *out=malloc((length+2)/3*4+1);
    require(out!=NULL,"allocate base64 fixture");
    size_t used=0;
    for(size_t i=0;i<length;i+=3) {
        unsigned value=(unsigned)bytes[i]<<16;
        if(i+1<length) value|=(unsigned)bytes[i+1]<<8;
        if(i+2<length) value|=bytes[i+2];
        out[used++]=alphabet[(value>>18)&63]; out[used++]=alphabet[(value>>12)&63];
        out[used++]=i+1<length?alphabet[(value>>6)&63]:'=';
        out[used++]=i+2<length?alphabet[value&63]:'=';
    }
    out[used]=0;
    return out;
}
static void encoded_command(const char *header,const char *encoded,size_t length,size_t fragment) {
    size_t capacity=strlen(header)+length+16;
    char *command=malloc(capacity);
    require(command!=NULL,"allocate protocol fixture");
    int n=snprintf(command,capacity,"\033_G%s;",header);
    require(n>0 && (size_t)n<capacity,"format protocol fixture");
    memcpy(command+n,encoded,length);
    memcpy(command+n+length,"\033\\",2);
    feed(command,(size_t)n+length+2,fragment);
    free(command);
}
static void image_command(const char *header,const uint8_t *pixels,size_t length,size_t fragment) {
    char *encoded=base64(pixels,length);
    encoded_command(header,encoded,strlen(encoded),fragment);
    free(encoded);
}
static void solid(uint32_t id,unsigned width,unsigned height,unsigned format,
                  uint8_t red,uint8_t green,uint8_t blue,uint8_t alpha,const char *options) {
    unsigned bpp=format==24?3:4;
    size_t size=(size_t)width*height*bpp;
    uint8_t *pixels=malloc(size);
    require(pixels!=NULL,"allocate solid image fixture");
    for(size_t i=0;i<size;i+=bpp) {
        pixels[i]=red; pixels[i+1]=green; pixels[i+2]=blue;
        if(bpp==4) pixels[i+3]=alpha;
    }
    char header[256];
    int n=snprintf(header,sizeof(header),"a=T,f=%u,s=%u,v=%u,i=%u,C=1,q=2%s",
                   format,width,height,id,options);
    require(n>0 && (size_t)n<sizeof(header),"solid image header");
    image_command(header,pixels,size,0);
    free(pixels);
}

static GhosttyKittyGraphics storage(void) {
    GhosttyKittyGraphics graphics=NULL;
    checked(ghostty_terminal_get(window.session.terminal,GHOSTTY_TERMINAL_DATA_KITTY_GRAPHICS,
                                &graphics),"get active image storage");
    require(graphics!=NULL,"image storage available");
    return graphics;
}
static bool image_exists(uint32_t id) { return ghostty_kitty_graphics_image(storage(),id)!=NULL; }
typedef struct {
    uint32_t image_id,offset_x,offset_y;
    int32_t z;
    GhosttyKittyGraphicsPlacementRenderInfo geometry;
} Placement;
static unsigned placements(Placement *out,uint32_t wanted) {
    GhosttyKittyGraphics graphics=storage();
    GhosttyKittyGraphicsPlacementIterator iterator=NULL;
    checked(ghostty_kitty_graphics_placement_iterator_new(NULL,&iterator),"allocate placement iterator");
    checked(ghostty_kitty_graphics_get(graphics,GHOSTTY_KITTY_GRAPHICS_DATA_PLACEMENT_ITERATOR,
                                     &iterator),"populate placement iterator");
    unsigned count=0;
    while(ghostty_kitty_graphics_placement_next(iterator)) {
        Placement p={.geometry=GHOSTTY_INIT_SIZED(GhosttyKittyGraphicsPlacementRenderInfo)};
        checked(ghostty_kitty_graphics_placement_get(iterator,GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_IMAGE_ID,
                                                  &p.image_id),"placement image ID");
        if(out && (!wanted || p.image_id==wanted)) {
            GhosttyKittyGraphicsImage image=ghostty_kitty_graphics_image(graphics,p.image_id);
            require(image!=NULL,"placement has an image");
            checked(ghostty_kitty_graphics_placement_render_info(iterator,image,window.session.terminal,
                                                                &p.geometry),"placement geometry");
            checked(ghostty_kitty_graphics_placement_get(iterator,GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_Z,
                                                        &p.z),"placement z");
            checked(ghostty_kitty_graphics_placement_get(iterator,GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_X_OFFSET,
                                                        &p.offset_x),"placement x offset");
            checked(ghostty_kitty_graphics_placement_get(iterator,GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_Y_OFFSET,
                                                        &p.offset_y),"placement y offset");
            *out=p;
        }
        ++count;
    }
    ghostty_kitty_graphics_placement_iterator_free(iterator);
    return count;
}
static Placement placement(uint32_t id) {
    Placement p={0};
    placements(&p,id);
    require(p.image_id!=0 && (!id || p.image_id==id),"find expected placement");
    return p;
}
static Placement placement_with_size(unsigned width,unsigned height) {
    GhosttyKittyGraphics graphics=storage();
    GhosttyKittyGraphicsPlacementIterator iterator=NULL;
    checked(ghostty_kitty_graphics_placement_iterator_new(NULL,&iterator),"allocate size lookup iterator");
    checked(ghostty_kitty_graphics_get(graphics,GHOSTTY_KITTY_GRAPHICS_DATA_PLACEMENT_ITERATOR,
                                     &iterator),"populate size lookup iterator");
    uint32_t found=0;
    while(ghostty_kitty_graphics_placement_next(iterator)) {
        uint32_t id=0,w=0,h=0;
        checked(ghostty_kitty_graphics_placement_get(iterator,GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_IMAGE_ID,
                                                   &id),"size lookup image ID");
        GhosttyKittyGraphicsImage image=ghostty_kitty_graphics_image(graphics,id);
        checked(ghostty_kitty_graphics_image_get(image,GHOSTTY_KITTY_IMAGE_DATA_WIDTH,&w),"size lookup width");
        checked(ghostty_kitty_graphics_image_get(image,GHOSTTY_KITTY_IMAGE_DATA_HEIGHT,&h),"size lookup height");
        if(w==width && h==height) {
            require(!found,"image size identifies one visible fixture");
            found=id;
        }
    }
    ghostty_kitty_graphics_placement_iterator_free(iterator);
    require(found!=0,"find image by native size");
    return placement(found);
}
static void expect_cursor(unsigned x,unsigned y,bool pending_wrap,const char *message) {
    uint16_t actual_x=0,actual_y=0;
    bool actual_wrap=false;
    checked(ghostty_terminal_get(window.session.terminal,GHOSTTY_TERMINAL_DATA_CURSOR_X,&actual_x),
            "read cursor column");
    checked(ghostty_terminal_get(window.session.terminal,GHOSTTY_TERMINAL_DATA_CURSOR_Y,&actual_y),
            "read cursor row");
    checked(ghostty_terminal_get(window.session.terminal,GHOSTTY_TERMINAL_DATA_CURSOR_PENDING_WRAP,&actual_wrap),
            "read pending wrap");
    if(actual_x!=x || actual_y!=y || actual_wrap!=pending_wrap) {
        fprintf(stderr,"cursor: expected %u,%u wrap=%d; got %u,%u wrap=%d\n",
                x,y,pending_wrap,actual_x,actual_y,actual_wrap);
        require(false,message);
    }
}
static void blank(void) {
    text("\033c\033[?25l\033[2J\033[H");
    require(!bt_renderer_draw(window.renderer,&window.session,true,true),"draw blank screen");
    require(placements(NULL,0)==0,"reset clears image placements");
}
static void capture(void) {
    require(!bt_renderer_draw(window.renderer,&window.session,false,true),"draw graphics frame");
    require(!bt_renderer_capture(window.renderer,"build/graphics-test.ppm"),"capture graphics framebuffer");
    FILE *file=fopen("build/graphics-test.ppm","rb");
    require(file!=NULL,"open framebuffer capture");
    char magic[3]={0}; unsigned maximum;
    require(fscanf(file,"%2s%u%u%u",magic,&frame.width,&frame.height,&maximum)==4 &&
            !strcmp(magic,"P6") && maximum==255 && fgetc(file)=='\n',"parse framebuffer header");
    require(frame.width>0 && frame.width<=8192 && frame.height>0 && frame.height<=8192,
            "framebuffer dimension bound");
    size_t size=(size_t)frame.width*frame.height*3;
    uint8_t *pixels=realloc(frame.pixels,size);
    require(pixels!=NULL,"allocate framebuffer pixels"); frame.pixels=pixels;
    require(fread(frame.pixels,1,size,file)==size && fgetc(file)==EOF,"read complete framebuffer");
    require(!fclose(file),"close framebuffer capture");
}
static bool near_pixel(unsigned x,unsigned y,unsigned red,unsigned green,unsigned blue,unsigned tolerance) {
    require(x<frame.width && y<frame.height,"pixel coordinates inside framebuffer");
    const uint8_t *p=frame.pixels+((size_t)y*frame.width+x)*3;
    return abs((int)p[0]-(int)red)<=(int)tolerance &&
           abs((int)p[1]-(int)green)<=(int)tolerance &&
           abs((int)p[2]-(int)blue)<=(int)tolerance;
}
static void pixel(unsigned x,unsigned y,unsigned red,unsigned green,unsigned blue,unsigned tolerance,
                  const char *message) {
    if(!near_pixel(x,y,red,green,blue,tolerance)) {
        const uint8_t *p=frame.pixels+((size_t)y*frame.width+x)*3;
        fprintf(stderr,"pixel (%u,%u): expected %u,%u,%u +/- %u; got %u,%u,%u\n",
                x,y,red,green,blue,tolerance,p[0],p[1],p[2]);
        require(false,message);
    }
}
static void background_pixel(unsigned x,unsigned y,const char *message) {
    pixel(x,y,background[0],background[1],background[2],1,message);
}
static unsigned white_pixels(unsigned x,unsigned y,unsigned width,unsigned height) {
    unsigned count=0;
    for(unsigned yy=y;yy<y+height;++yy) for(unsigned xx=x;xx<x+width;++xx)
        if(near_pixel(xx,yy,255,255,255,40)) ++count;
    return count;
}

static void test_pixels(void) {
    blank(); at(1,1);
    solid(101,8,8,24,240,20,30,255,"");
    at(4,1); solid(102,8,8,32,0,0,255,128,"");
    capture();
    pixel(px(1)+3,py(1)+3,240,20,30,1,"native RGB pixels");
    background_pixel(px(1)+8,py(1)+3,"native image width is not rounded to cell size");
    pixel(px(4)+3,py(1)+3,9,11,143,2,"straight RGBA alpha blends over terminal background");
    Placement p=placement(101);
    require(p.geometry.pixel_width==8 && p.geometry.pixel_height==8,"native RGB dimensions");

    uint8_t rgba[8*8*4];
    for(size_t i=0;i<sizeof(rgba);i+=4) {
        rgba[i]=0; rgba[i+1]=220; rgba[i+2]=80; rgba[i+3]=128;
    }
    png_image image={0}; image.version=PNG_IMAGE_VERSION;
    image.width=8; image.height=8; image.format=PNG_FORMAT_RGBA;
    png_alloc_size_t length=0;
    require(png_image_write_to_memory(&image,NULL,&length,0,rgba,0,NULL)!=0,"size PNG fixture");
    uint8_t *png=malloc(length); require(png!=NULL,"allocate PNG fixture");
    require(png_image_write_to_memory(&image,png,&length,0,rgba,0,NULL)!=0,"encode PNG fixture");
    png_image_free(&image);
    at(7,1);
    image_command("a=T,f=100,i=103,C=1,q=2",png,length,1);
    free(png);
    require(image_exists(103),"PNG accepted after byte-at-a-time parsing");
    capture();
    pixel(px(7)+3,py(1)+3,9,121,55,2,"decoded PNG preserves color and alpha");
    puts("PASS graphics RGB, native pixel sizing, RGBA alpha and actual PNG decoding");
}

static void test_filtering_and_offset_shrink(void) {
    /* Transparent pixels can contain arbitrary RGB. Filtering must not leak
     * their blue into the visible edge of an opaque red pixel. */
    blank(); at(2,2);
    static const uint8_t edge[]={255,0,0,255, 0,0,255,0};
    image_command("a=T,f=32,s=2,v=1,i=151,C=1,q=2,c=12,r=2",edge,sizeof(edge),0);
    capture();
    unsigned width=12*cell_width;
    double alpha=0.5-1.0/width; /* Bilinear alpha at the center-right pixel. */
    unsigned red=(unsigned)(255*alpha+background[0]*(1-alpha)+0.5);
    unsigned green=(unsigned)(background[1]*(1-alpha)+0.5);
    unsigned blue=(unsigned)(background[2]*(1-alpha)+0.5);
    pixel(px(2)+width/2,py(2)+cell_height,red,green,blue,2,
          "scaled RGBA edge retains red hue without transparent-blue halo");
    pixel(px(2)+1,py(2)+cell_height,255,0,0,1,"opaque end of filtered RGBA image");
    background_pixel(px(2)+width-2,py(2)+cell_height,"transparent end of filtered RGBA image");

    /* Sampling the crop boundary must not blend adjacent, excluded texels. */
    blank(); at(2,2);
    static const uint8_t stripe[]={255,0,0,255, 0,255,0,255, 0,0,255,255};
    image_command("a=T,f=32,s=3,v=1,i=152,C=1,q=2,x=1,y=0,w=1,h=1,c=12,r=3",
                  stripe,sizeof(stripe),0);
    capture();
    unsigned height=3*cell_height;
    pixel(px(2),py(2)+height/2,0,255,0,1,"one-pixel crop left edge excludes red neighbor");
    pixel(px(2)+width-1,py(2)+height/2,0,255,0,1,"one-pixel crop right edge excludes blue neighbor");
    pixel(px(2)+width/2,py(2),0,255,0,1,"one-pixel crop top edge remains green");
    pixel(px(2)+width/2,py(2)+height-1,0,255,0,1,"one-pixel crop bottom edge remains green");

    /* Simulate a font/DPI shrink while preserving the placement's original
     * stored offsets. Restore real metrics before rendering the framebuffer. */
    blank();
    unsigned cols=window.session.cols,rows=window.session.rows;
    require(!bt_session_resize(&window.session,cols,rows,cell_width*2,cell_height*2),
            "grow cell metrics for persistent-offset fixture");
    at(2,2);
    char options[128];
    int n=snprintf(options,sizeof(options),",c=3,r=2,X=%u,Y=%u",cell_width*2-1,cell_height*2-1);
    require(n>0 && (size_t)n<sizeof(options),"format persistent-offset fixture");
    solid(153,2,2,32,220,40,160,255,options);
    require(!bt_session_resize(&window.session,cols,rows,cell_width,cell_height),
            "restore smaller cell metrics");
    Placement p=placement(153);
    require(p.offset_x==cell_width*2-1 && p.offset_y==cell_height*2-1,
            "offset fixture retains original stored values across cell shrink");
    require(p.geometry.pixel_width==cell_width*2+1 && p.geometry.pixel_height==cell_height+1,
            "core placement size uses offsets clamped to current cell bounds");
    capture();
    pixel(px(2)+cell_width,py(2)+cell_height,220,40,160,1,
          "renderer clamps stored X and Y offsets after cell metrics shrink");
    background_pixel(px(2)+cell_width-2,py(2)+cell_height,
                     "shrunken horizontal offset preserves pixel before image");
    background_pixel(px(2)+cell_width,py(2)+cell_height-2,
                     "shrunken vertical offset preserves pixel before image");
    puts("PASS graphics premultiplied filtering, crop-edge isolation and persistent offset shrink");
}

static void test_zlib_upload(void) {
    blank(); at(2,2);
    /* A zlib stream containing the RGB pixels red/green then blue/yellow.
     * Keeping the wire fixture fixed avoids another test-only link dependency. */
    static const uint8_t compressed[]={
        0x78,0x9c,0xfb,0xcf,0xc0,0xc0,0xf0,0x1f,0x84,
        0xff,0xff,0x67,0x00,0x00,0x1c,0xef,0x04,0xfc
    };
    char *encoded=base64(compressed,sizeof(compressed));
    size_t length=strlen(encoded),split=12;
    encoded_command("a=T,f=24,s=2,v=2,o=z,i=211,C=1,q=2,c=6,r=4,m=1",encoded,split,1);
    require(!image_exists(211),"partial zlib Kitty stream stays undisplayed");
    encoded_command("m=0,q=2",encoded+split,length-split,2);
    free(encoded);
    require(image_exists(211),"complete zlib Kitty stream accepted");
    GhosttyKittyGraphicsImage image=ghostty_kitty_graphics_image(storage(),211);
    GhosttyKittyImageCompression compression;
    size_t data_length=0;
    checked(ghostty_kitty_graphics_image_get(image,GHOSTTY_KITTY_IMAGE_DATA_COMPRESSION,&compression),
            "read stored image compression");
    checked(ghostty_kitty_graphics_image_get(image,GHOSTTY_KITTY_IMAGE_DATA_DATA_LEN,&data_length),
            "read decompressed image length");
    require(compression==GHOSTTY_KITTY_IMAGE_COMPRESSION_NONE && data_length==12,
            "core stores decompressed RGB data");
    capture();
    unsigned width=6*cell_width,height=4*cell_height;
    pixel(px(2)+width/8,py(2)+height/8,255,0,0,1,"zlib image red quadrant reaches GPU");
    pixel(px(2)+width*7/8,py(2)+height/8,0,255,0,1,"zlib image green quadrant reaches GPU");
    pixel(px(2)+width/8,py(2)+height*7/8,0,0,255,1,"zlib image blue quadrant reaches GPU");
    pixel(px(2)+width*7/8,py(2)+height*7/8,255,255,0,1,"zlib image yellow quadrant reaches GPU");
    at(10,2);
    image_command("a=T,f=24,s=2,v=2,o=z,i=212,C=1,q=2",compressed,sizeof(compressed)-5,1);
    require(!image_exists(212),"truncated zlib image rejected");
    puts("PASS graphics chunked zlib Kitty upload, decompressed metadata and framebuffer quadrants");
}

static void test_relative_placements(void) {
    blank(); at(5,5);
    solid(801,2,2,32,230,30,40,255,",p=7,c=2,r=2");
    at(30,12);
    static const uint8_t green[]={0,220,60,255};
    image_command("a=T,f=32,s=1,v=1,i=802,p=8,P=801,Q=7,H=3,V=-2,c=3,r=2,C=0,q=2,z=1",
                  green,sizeof(green),1);
    expect_cursor(30,12,false,"relative placement does not move the cursor even with C=0");
    Placement p=placement(802);
    require(p.geometry.viewport_visible && p.geometry.viewport_col==8 && p.geometry.viewport_row==3,
            "explicit child resolves signed offsets from named parent");
    solid(803,2,2,32,20,70,230,255,",p=9,P=802,Q=8,H=-10,V=-4,c=4,r=3,z=2");
    p=placement(803);
    require(p.geometry.viewport_visible && p.geometry.viewport_col==-2 && p.geometry.viewport_row==-1,
            "relative chain resolves negative viewport coordinates");
    capture();
    pixel(px(5)+2,py(5)+2,230,30,40,1,"explicit parent placement pixels");
    pixel(px(8)+2,py(3)+2,0,220,60,1,"explicit relative child pixels");
    pixel(PAD+2,PAD+2,20,70,230,1,"relative grandchild clipped into viewport");
    background_pixel(PAD-1,PAD+2,"relative grandchild cannot paint left padding");
    background_pixel(PAD+2,PAD-1,"relative grandchild cannot paint top padding");

    at(7,7);
    text("\033_Ga=p,i=801,p=7,c=2,r=2,C=1,q=2\033\\");
    p=placement(802);
    require(p.geometry.viewport_col==10 && p.geometry.viewport_row==5,
            "moving a named parent repositions existing child");
    p=placement(803);
    require(p.geometry.viewport_col==0 && p.geometry.viewport_row==1,
            "moving a named parent repositions entire relative chain");
    capture();
    pixel(px(10)+2,py(5)+2,0,220,60,1,"moved relative child pixels");
    pixel(PAD+2,py(1)+2,20,70,230,1,"moved relative grandchild pixels");
    background_pixel(px(8)+2,py(3)+2,"previous child location repainted after parent move");
    text("\033_Ga=d,d=I,i=801,p=7,q=2\033\\");
    require(placements(NULL,0)==0,"deleting parent removes orphan relative placement chain");
    capture();
    background_pixel(px(10)+2,py(5)+2,"orphan child pixels removed after parent deletion");
    background_pixel(PAD+2,py(1)+2,"orphan grandchild pixels removed after parent deletion");
    puts("PASS graphics explicit relative placements, signed chains, parent movement and orphan deletion");
}

static void test_erased_backgrounds(void) {
    /* EL/ED can represent blank backgrounds as compact cell content, with a
     * default style. Their resolved BG_COLOR still makes them opaque. */
    blank(); at(3,3); text("\033[48;2;230;180;20m\033[2K\033[0m"); at(3,3);
    solid(851,2,2,32,0,40,230,255,",c=4,r=2,z=-1073741825");
    capture();
    pixel(px(3)+2,py(3)+2,230,180,20,1,"erase-line background covers below-background image");
    pixel(PAD+2,py(3)+2,230,180,20,1,"erase-line background drawn outside image area");
    pixel(px(3)+2,py(4)+2,0,40,230,1,"default row still reveals below-background image");
    blank(); text("\033[48;2;25;60;90m\033[2J\033[0m"); at(3,3);
    solid(852,2,2,32,230,30,50,255,",c=4,r=2,z=-1073741825");
    capture();
    pixel(px(3)+2,py(3)+2,25,60,90,1,"erase-display background covers below-background image");
    pixel(PAD+2,PAD+2,25,60,90,1,"erase-display background drawn on blank cells");
    puts("PASS graphics compact erased-cell backgrounds preserve opacity above image layer");
}

static void test_empty_crops(void) {
    blank(); at(2,2);
    solid(861,2,2,32,255,0,0,255,",x=9,y=0,w=1,h=1,c=4,r=2");
    Placement p=placement(861);
    require(p.geometry.source_width==0 && p.geometry.pixel_width>0,
            "empty crop fixture retains requested destination size");
    at(8,2);
    solid(862,2,2,32,0,255,0,255,",x=0,y=9,w=1,h=1,c=4,r=2");
    p=placement(862);
    require(p.geometry.source_height==0 && p.geometry.pixel_height>0,
            "empty vertical crop fixture retains requested destination size");
    capture();
    background_pixel(px(2)+cell_width,py(2)+cell_height,
                     "empty horizontal crop does not smear edge texel into destination");
    background_pixel(px(8)+cell_width,py(2)+cell_height,
                     "empty vertical crop does not smear edge texel into destination");
    puts("PASS graphics empty source intersections draw no pixels");
}

static void test_chunked(void) {
    blank(); at(2,2);
    uint8_t rgba[8*8*4];
    for(size_t i=0;i<sizeof(rgba);i+=4) {
        rgba[i]=30; rgba[i+1]=220; rgba[i+2]=70; rgba[i+3]=255;
    }
    char *encoded=base64(rgba,sizeof(rgba));
    size_t length=strlen(encoded),split=length/8*4;
    encoded_command("a=T,f=32,s=8,v=8,i=201,C=1,q=2,m=1",encoded,split,1);
    require(!image_exists(201),"incomplete Kitty image is not displayed");
    encoded_command("m=0,q=2",encoded+split,length-split,2);
    free(encoded);
    require(image_exists(201),"Kitty final chunk completes image");
    capture(); pixel(px(2)+3,py(2)+3,30,220,70,1,"chunked image framebuffer pixels");
    puts("PASS graphics chunked Kitty transfer across arbitrary read boundaries");
}

static void expect_text_clean(GhosttyRenderState state) {
    checked(ghostty_render_state_update(state,window.session.terminal),"update independent text snapshot");
    GhosttyRenderStateDirty dirty;
    checked(ghostty_render_state_get(state,GHOSTTY_RENDER_STATE_DATA_DIRTY,&dirty),"read text dirty flag");
    require(dirty==GHOSTTY_RENDER_STATE_DIRTY_FALSE,"image-only command keeps text snapshot clean");
}
static void expect_redraw(uint64_t before) {
    require(!bt_renderer_draw(window.renderer,&window.session,false,true),"draw image-only change");
    require(bt_renderer_frames(window.renderer)>before,"image-only mutation schedules an unforced frame");
}
static void test_redraw(void) {
    blank(); at(2,2);
    require(!bt_renderer_draw(window.renderer,&window.session,true,true),"clean positioned text frame");
    GhosttyRenderState state=NULL;
    checked(ghostty_render_state_new(NULL,&state),"independent text snapshot");
    checked(ghostty_render_state_update(state,window.session.terminal),"initialize independent text snapshot");
    checked(ghostty_render_state_clean(state),"clean independent text snapshot");
    uint64_t before=bt_renderer_frames(window.renderer);
    solid(301,8,8,32,230,20,30,255,"");
    expect_text_clean(state); expect_redraw(before); capture();
    pixel(px(2)+3,py(2)+3,230,20,30,1,"first image-only transmission appears");
    before=bt_renderer_frames(window.renderer);
    solid(301,8,8,32,20,50,230,255,"");
    expect_text_clean(state); expect_redraw(before); capture();
    pixel(px(2)+3,py(2)+3,20,50,230,1,"same-size retransmission invalidates GPU texture");
    before=bt_renderer_frames(window.renderer);
    text("\033_Ga=d,d=I,i=301,q=2\033\\");
    expect_text_clean(state); expect_redraw(before); capture();
    background_pixel(px(2)+3,py(2)+3,"image-only deletion repaints the former image");
    ghostty_render_state_free(state);
    puts("PASS graphics image-only redraw, same-sized texture replacement and deletion");
}

static void test_crop_and_layers(void) {
    blank();
    uint8_t rgba[8*8*4];
    for(unsigned y=0;y<8;++y) for(unsigned x=0;x<8;++x) {
        uint8_t *p=rgba+(y*8+x)*4;
        p[0]=x<4?255:0; p[1]=x>=4?255:0; p[2]=y>=4?255:0; p[3]=255;
    }
    at(2,2);
    image_command("a=T,f=32,s=8,v=8,i=401,C=1,q=2,x=4,y=0,w=4,h=4,c=3,r=2,X=2,Y=3",
                  rgba,sizeof(rgba),0);
    Placement p=placement(401);
    require(p.geometry.source_x==4 && p.geometry.source_y==0 &&
            p.geometry.source_width==4 && p.geometry.source_height==4,"resolved source crop");
    require(p.geometry.pixel_width==3*cell_width-2 && p.geometry.pixel_height==2*cell_height-3,
            "scaled placement subtracts subcell offsets");
    capture();
    pixel(px(2)+cell_width,py(2)+cell_height,0,255,0,1,"cropped image shows selected source quadrant");
    background_pixel(px(2)+1,py(2)+4,"image respects horizontal subcell offset");
    background_pixel(px(2)+4,py(2)+2,"image respects vertical subcell offset");

    blank(); at(3,3); text("\033[48;2;230;180;20m \033[0m"); at(3,3);
    solid(402,2,2,32,0,40,230,255,",c=4,r=2,z=-1073741825");
    capture();
    pixel(px(3)+2,py(3)+2,230,180,20,1,"lowest layer stays behind explicit cell background");
    pixel(px(4)+2,py(3)+2,0,40,230,1,"lowest layer remains visible through default background cells");

    blank(); at(3,3); text("\033[38;2;255;255;255;48;2;230;180;20mX\033[0m"); at(3,3);
    solid(403,2,2,32,0,40,230,255,",c=4,r=2,z=-1");
    capture();
    pixel(px(3)+1,py(3)+1,0,40,230,1,"middle image layer covers explicit cell background");
    require(white_pixels(px(3),py(3),cell_width,cell_height)>2,"middle layer stays underneath text glyphs");
    at(3,3); solid(404,2,2,32,230,30,50,255,",c=4,r=2,z=0");
    capture();
    require(white_pixels(px(3),py(3),cell_width,cell_height)==0,"top image layer covers text glyphs");
    pixel(px(3)+cell_width/2,py(3)+cell_height/2,230,30,50,1,"top image layer framebuffer pixels");

    blank(); at(3,3);
    solid(490,2,2,32,0,220,50,255,",c=4,r=2,z=2");
    solid(410,2,2,32,230,0,0,255,",c=4,r=2,z=2");
    capture();
    pixel(px(4),py(3)+4,0,220,50,1,"equal-z images sort by image ID regardless of insertion order");
    solid(411,2,2,32,0,50,230,255,",c=4,r=2,z=3");
    capture(); pixel(px(4),py(3)+4,0,50,230,1,"larger signed z sorts above larger image ID");
    puts("PASS graphics source crop, subcell offsets, all three text layers and image z sorting");
}

static void test_screens_and_geometry(void) {
    blank(); at(1,1); solid(501,8,8,32,230,10,30,255,""); capture();
    text("\033[?1049h\033[?25l");
    require(!image_exists(501),"alternate screen has separate image storage"); capture();
    background_pixel(px(1)+3,py(1)+3,"primary image hidden in alternate screen");
    at(1,1); solid(501,8,8,32,20,210,60,255,""); capture();
    pixel(px(1)+3,py(1)+3,20,210,60,1,"alternate screen can reuse image ID");
    text("\033[?1049l\033[?25l"); capture();
    pixel(px(1)+3,py(1)+3,230,10,30,1,"primary texture restored after alternate ID reuse");

    blank(); at(2,1); solid(502,2,2,32,230,30,60,255,",c=3,r=4");
    unsigned initial_rows=window.session.rows;
    control("\033[%u;1H\n\n\n",initial_rows);
    Placement p=placement(502);
    require(p.geometry.viewport_visible && p.geometry.viewport_row==-2,"partially scrolled placement has negative row");
    capture();
    pixel(px(2)+3,PAD+2,230,30,60,1,"partially scrolled image remains visible");
    background_pixel(px(2)+3,PAD-1,"scrolled image clipped before top padding");
    background_pixel(px(2)+3,py(2)+2,"scrolled image bottom is clipped at correct row");
    GhosttyTerminalScrollViewport scroll={.tag=GHOSTTY_SCROLL_VIEWPORT_DELTA,.value.delta=-3};
    ghostty_terminal_scroll_viewport(window.session.terminal,scroll);
    p=placement(502); require(p.geometry.viewport_row==1,"scrollback restores original image location");
    capture(); pixel(px(2)+3,py(1)+3,230,30,60,1,"history image pixels follow viewport");
    scroll.tag=GHOSTTY_SCROLL_VIEWPORT_BOTTOM;
    ghostty_terminal_scroll_viewport(window.session.terminal,scroll);

    blank(); at(2,2); solid(503,2,2,32,30,190,230,255,",c=3,r=2");
    unsigned old_cols=window.session.cols;
    SDL_SetWindowSize(window.window,(int)((old_cols+4)*cell_width+2*PAD),
                      (int)((window.session.rows+2)*cell_height+2*PAD));
    require(!bt_window_resize(&window),"resize image window");
    cell_width=window.session.cell_width; cell_height=window.session.cell_height;
    require(window.session.cols>old_cols,"window resize reaches terminal geometry");
    p=placement(503);
    require(p.geometry.viewport_visible && p.geometry.pixel_width==3*cell_width &&
            p.geometry.pixel_height==2*cell_height,"image placement geometry after resize");
    capture(); pixel(px(2)+3,py(2)+3,30,190,230,1,"image remains visible after window resize");
    puts("PASS graphics main/alternate storage, negative scroll clipping, history and window resize");
}

static bool screen_contains(const char *needle) {
    size_t length=0; char *value=bt_session_text(&window.session,false,&length);
    bool found=value && strstr(value,needle)!=NULL;
    free(value); return found;
}
static void test_sixel(void) {
    const char *red="\033P0;1q\"1;1;8;6#1;2;100;0;0#1!8~\033\\";
    const char *green="\033P0;1q\"1;1;8;6#1;2;0;100;0#1!8~\033\\";
    blank(); at(2,2); feed(red,strlen(red),1);
    require(placements(NULL,0)==1,"Sixel becomes a native image placement");
    Placement p=placement(0);
    require(p.geometry.pixel_width==8 && p.geometry.pixel_height==6,"Sixel native raster dimensions");
    capture(); pixel(px(2)+3,py(2)+3,255,0,0,1,"Sixel framebuffer pixels after byte-at-a-time input");
    text("\033[2J"); require(placements(NULL,0)==0,"ED2 clears Sixel placement");

    blank(); at(1,1); solid(601,8,8,32,230,40,50,255,""); at(4,4);
    encoded_command("a=T,f=32,s=2,v=1,i=602,C=1,q=2,m=1","/wAA/w==",8,1);
    feed(green,strlen(green),2);
    require(image_exists(601) && !image_exists(602) && placements(NULL,0)==2,
            "Sixel cancels pending Kitty transfer while preserving previous images");
    capture(); pixel(px(4)+3,py(4)+3,0,255,0,1,"Sixel after pending Kitty transfer renders");
    at(7,4);
    encoded_command("a=T,f=999,s=2,v=1,i=603,C=1,q=2,m=1","/wAA/w==",8,3);
    feed(red,strlen(red),3);
    require(!image_exists(603) && placements(NULL,0)==3,"rejected Kitty first chunk cannot suppress later Sixel");
    capture(); pixel(px(7)+3,py(4)+3,255,0,0,1,"Sixel after rejected Kitty transfer renders");

    blank();
    static const char cancel[]="\033P0;1q#1;2;100;0;0!8~\030AFTER_CANCEL";
    feed(cancel,sizeof(cancel)-1,1);
    require(placements(NULL,0)==0 && screen_contains("AFTER_CANCEL"),"CAN aborts Sixel and text parsing recovers");
    static const char substitute[]="\033P0;1q#1;2;100;0;0!8~\032AFTER_SUB";
    feed(substitute,sizeof(substitute)-1,2);
    require(placements(NULL,0)==0 && screen_contains("AFTER_SUB"),"SUB aborts Sixel and text parsing recovers");
    text("\033P0;1q\"1;1;999999999;999999999#1!999999999~\033\\AFTER_LIMIT");
    require(placements(NULL,0)==0 && screen_contains("AFTER_LIMIT"),"oversized Sixel rejected with text recovery");
    image_command("a=T,f=100,i=604,C=1,q=2",(const uint8_t *)"not PNG",7,1);
    text("AFTER_PNG");
    require(!image_exists(604) && screen_contains("AFTER_PNG"),"invalid PNG rejected with text recovery");
    blank();
    const char *unicode="UTF8_ĐÜ_END";
    feed(unicode,strlen(unicode),1);
    require(screen_contains(unicode),"UTF-8 continuation bytes must not become C1 DCS or ST controls");
    text("\033P$qm\033\\AFTER_OTHER_DCS");
    require(placements(NULL,0)==0 && screen_contains("AFTER_OTHER_DCS"),
            "non-Sixel DCS passes through without consuming following text");
    puts("PASS graphics Sixel pixels, read boundaries, mixed transfers, cancellation and malformed-input recovery");
}

static void test_c1_strings(void) {
    static const char *const strings[]={
        "\033]0;C1_STRING\234", "\235" "0;C1_STRING\234",
        "\033P$qm\234", "\220$qm\234",
        "\033_ignored\234", "\237ignored\234"
    };
    static const char red[]="\2207;1q\"1;1;8;6#1;2;100;0;0!8~\234";
    for(size_t i=0;i<sizeof(strings)/sizeof(strings[0]);++i) {
        blank(); feed(strings[i],strlen(strings[i]),1);
        text("AFTER_C1_STRING");
        require(screen_contains("AFTER_C1_STRING"),"C1 ST terminates OSC, APC and non-Sixel DCS");
        at(2,2); feed(red,sizeof(red)-1,1);
        Placement p=placement_with_size(8,6);
        require(p.geometry.viewport_visible && p.geometry.viewport_col==2 && p.geometry.viewport_row==2,
                "C1 Sixel after a C1 string terminator has correct geometry");
        capture(); pixel(px(2)+3,py(2)+3,255,0,0,1,"C1 Sixel following another string renders");
    }

    blank();
    static const char title[]="TITLE_\302\234_INSIDE";
    static const char osc[]="\033]0;TITLE_\302\234_INSIDE\234AFTER_UTF8_TITLE";
    feed(osc,sizeof(osc)-1,1);
    GhosttyString actual={0};
    checked(ghostty_terminal_get(window.session.terminal,GHOSTTY_TERMINAL_DATA_TITLE,&actual),"read UTF-8 title");
    require(actual.len==sizeof(title)-1 && !memcmp(actual.ptr,title,sizeof(title)-1),
            "UTF-8 U+009C inside OSC is data rather than C1 ST");
    require(screen_contains("AFTER_UTF8_TITLE") && !screen_contains("INSIDE"),
            "only text following the actual C1 ST reaches the text grid");

    blank(); at(3,3);
    static const char apc[]="\237Ga=T,f=32,s=1,v=1,i=650,C=1,q=2;/wAA/w==\234AFTER_KITTY_C1";
    feed(apc,sizeof(apc)-1,1);
    require(image_exists(650) && screen_contains("AFTER_KITTY_C1"),
            "C1 APC and ST frame an actual Kitty image without swallowing later text");
    Placement p=placement(650);
    require(p.geometry.pixel_width==1 && p.geometry.pixel_height==1 &&
            p.geometry.viewport_col==3 && p.geometry.viewport_row==3,"C1 Kitty placement geometry");
    puts("PASS graphics C1 string termination, subsequent image framing and UTF-8 title boundaries");
}

static void test_sixel_page_mode(void) {
    static const char red[]="\033P7;1q\"1;1;8;6#1;2;100;0;0!8~\033\\";
    blank(); at(2,2); text("\0337");
    text("\033[5;10r\033[?6h\033[3;4H\033[?80h");
    expect_cursor(3,6,false,"origin-mode fixture is relative to the top margin");
    feed(red,sizeof(red)-1,1);
    Placement p=placement_with_size(8,6);
    require(p.geometry.viewport_visible && p.geometry.viewport_col==0 && p.geometry.viewport_row==0,
            "page-mode Sixel ignores scrolling margins and origin mode");
    expect_cursor(3,6,false,"page-mode Sixel preserves cursor inside scrolling margins");
    capture(); pixel(px(0)+3,py(0)+3,255,0,0,1,"page-mode image renders at page origin under margins");
    text("\033[1;1H");
    expect_cursor(0,4,false,"page-mode Sixel preserves DECOM after placement");
    text("\0338");
    expect_cursor(2,2,false,"page-mode Sixel preserves the application's earlier saved cursor");
    text("\033[1;1H");
    expect_cursor(0,0,false,"restored application cursor retains its saved origin mode");
    text("\033[10;4H\033D");
    p=placement_with_size(8,6);
    require(p.geometry.viewport_visible && p.geometry.viewport_col==0 && p.geometry.viewport_row==0,
            "scrolling rows below a page-mode image leaves its location unchanged");
    capture(); pixel(px(0)+3,py(0)+3,255,0,0,1,"page-mode image survives scrolling in a separate region");

    blank(); at(2,3); text("\0337");
    at(window.session.cols-1,6); text("X\033[?80h");
    expect_cursor(window.session.cols-1,6,true,"page-mode pending-wrap fixture");
    feed(red,sizeof(red)-1,1);
    p=placement_with_size(8,6);
    require(p.geometry.viewport_visible && p.geometry.viewport_col==0 && p.geometry.viewport_row==0,
            "page-mode image is absolute when the text cursor has a pending wrap");
    expect_cursor(window.session.cols-1,6,true,"page-mode image preserves pending wrap");
    capture(); pixel(px(0)+3,py(0)+3,255,0,0,1,"pending-wrap page-mode image renders at page origin");
    text("Y");
    expect_cursor(1,7,false,"text after page-mode graphics performs the pending soft wrap");
    text("\0338");
    expect_cursor(2,3,false,"page-mode wrap preservation does not overwrite the saved cursor");
    puts("PASS graphics page-mode Sixel origin, margins, cursor, pending wrap and saved cursor");
}

int main(void) {
    char helper[PATH_MAX];
    require(realpath("build/batty-session",helper)!=NULL,"session helper path");
    char *args[]={"/bin/cat",NULL};
    require(!bt_window_open(&window,helper,args,environ,64,20,"monospace",16,false),"open graphics test window");
    cell_width=window.session.cell_width; cell_height=window.session.cell_height;
    require(cell_width>=4 && cell_height>=8,"usable font cell metrics");
    test_pixels(); test_filtering_and_offset_shrink(); test_chunked(); test_redraw(); test_crop_and_layers();
    test_zlib_upload(); test_relative_placements(); test_erased_backgrounds(); test_empty_crops();
    test_screens_and_geometry(); test_sixel(); test_c1_strings(); test_sixel_page_mode();

    /* Retain a useful final artifact, with both protocols and alpha visible. */
    blank(); at(2,2); solid(701,8,8,24,30,170,240,255,",c=18,r=5");
    at(12,4); solid(702,8,8,32,240,80,50,160,",c=18,r=5");
    at(4,10);
    const char *sixel="\033P0;1q\"1;1;96;24#1;2;30;100;40#1!96~-!96~-!96~-!96~\033\\";
    feed(sixel,strlen(sixel),7);
    at(2,1); text("\033[1mBatty native Kitty and Sixel graphics\033[0m");
    at(2,12); text("RGB   RGBA alpha   PNG   crop   layering   scroll   resize");
    capture();
    free(frame.pixels); frame.pixels=NULL;
    bt_window_close(&window);
    puts("PASS graphics native image integration and GLES framebuffer assertions");
    return 0;
}
