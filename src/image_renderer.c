/* SPDX-License-Identifier: MIT */
/* Epoch/generation-keyed textures from immutable owned presentation frames. */
#include "image_renderer.h"
#include <GLES3/gl3.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define PAD 8
#define TEXTURES 128
#define GPU_BYTES (64u * 1024u * 1024u)
typedef struct {
    GLuint texture;
    uint64_t generation, used;
    size_t bytes, length;
    uint32_t id, width, height;
    unsigned channels;
    uint8_t *shadow;
} Texture;
struct BtImageRenderer {
    GLuint program, vao, vbo;
    GLint viewport, source_bounds, max_texture;
    Texture textures[TEXTURES];
    size_t bytes;
    BtImageStats stats;
    uint64_t epoch, clock;
    const BtPresentation *frame;
    unsigned cw, ch;
    char error[256];
};
static int fail(BtImageRenderer *r, const char *message) {
    snprintf(r->error,sizeof(r->error),"%s",message); return -1;
}
const char *bt_images_error(BtImageRenderer *r) { return r->error; }
static GLuint shader(BtImageRenderer *r, GLenum kind, const char *text) {
    GLuint id=glCreateShader(kind);
    glShaderSource(id,1,&text,NULL); glCompileShader(id);
    GLint ok; glGetShaderiv(id,GL_COMPILE_STATUS,&ok);
    if(!ok) { glGetShaderInfoLog(id,sizeof(r->error),NULL,r->error); glDeleteShader(id); return 0; }
    return id;
}
BtImageRenderer *bt_images_new(char *error, size_t size) {
    BtImageRenderer *r=calloc(1,sizeof(*r));
    if(!r) { snprintf(error,size,"Could not allocate image renderer"); return NULL; }
    const char *vs="#version 300 es\nprecision highp float;\n"
        "layout(location=0) in vec2 position; layout(location=1) in vec2 uv;"
        "uniform vec2 viewport; out vec2 tex;"
        "void main(){gl_Position=vec4(position.x/viewport.x*2.-1.,1.-position.y/viewport.y*2.,0.,1.);tex=uv;}";
    const char *fs="#version 300 es\nprecision highp float;\n"
        "in vec2 tex; uniform sampler2D image; uniform vec4 source_bounds; out vec4 result;"
        "void main(){result=texture(image,clamp(tex,source_bounds.xy,source_bounds.zw));}";
    GLuint vertex=shader(r,GL_VERTEX_SHADER,vs), fragment=shader(r,GL_FRAGMENT_SHADER,fs);
    if(!vertex || !fragment) {
        if(vertex) glDeleteShader(vertex);
        if(fragment) glDeleteShader(fragment);
        goto failed;
    }
    r->program=glCreateProgram(); glAttachShader(r->program,vertex); glAttachShader(r->program,fragment);
    glLinkProgram(r->program); glDeleteShader(vertex); glDeleteShader(fragment);
    GLint ok; glGetProgramiv(r->program,GL_LINK_STATUS,&ok);
    if(!ok) { glGetProgramInfoLog(r->program,sizeof(r->error),NULL,r->error); goto failed; }
    r->viewport=glGetUniformLocation(r->program,"viewport");
    r->source_bounds=glGetUniformLocation(r->program,"source_bounds");
    glGenVertexArrays(1,&r->vao); glBindVertexArray(r->vao);
    glGenBuffers(1,&r->vbo); glBindBuffer(GL_ARRAY_BUFFER,r->vbo);
    glVertexAttribPointer(0,2,GL_FLOAT,GL_FALSE,4*sizeof(float),(void *)0);
    glVertexAttribPointer(1,2,GL_FLOAT,GL_FALSE,4*sizeof(float),(void *)(2*sizeof(float)));
    glEnableVertexAttribArray(0); glEnableVertexAttribArray(1);
    glGetIntegerv(GL_MAX_TEXTURE_SIZE,&r->max_texture);
    return r;
failed:
    snprintf(error,size,"%s",r->error); bt_images_free(r); return NULL;
}
static void evict(BtImageRenderer *r, unsigned i) {
    Texture *t=&r->textures[i];
    if(t->texture) glDeleteTextures(1,&t->texture);
    r->bytes-=t->bytes; r->stats.shadow_bytes-=t->length;
    free(t->shadow); memset(t,0,sizeof(*t));
}
void bt_images_reset(BtImageRenderer *r) {
    if(!r) return;
    for(unsigned i=0;i<TEXTURES;++i) evict(r,i);
    r->epoch=0; r->clock=0; r->frame=NULL;
}
int bt_images_prepare(BtImageRenderer *r, const BtPresentation *f) {
    if(!f || !f->epoch || !f->cell_width || !f->cell_height)
        return fail(r,"Invalid image presentation");
    if(r->epoch!=f->epoch) bt_images_reset(r);
    r->epoch=f->epoch; r->frame=f; r->cw=f->cell_width; r->ch=f->cell_height;
    return 0;
}
static unsigned oldest(BtImageRenderer *r) {
    unsigned slot=0;
    for(unsigned i=0;i<TEXTURES;++i) {
        if(!r->textures[i].texture) return i;
        if(r->textures[i].used<r->textures[slot].used) slot=i;
    }
    return slot;
}
BtImageStats bt_images_stats(const BtImageRenderer *r) {
    BtImageStats stats=r->stats; stats.texture_bytes=r->bytes; return stats;
}
/* Keep straight-alpha shadows; compare before premultiplication so alpha-only
 * edits and grayscale images follow the same damage calculation. */
static uint8_t *stage(const uint8_t *pixels, unsigned width, unsigned channels,
                      unsigned x, unsigned y, unsigned w, unsigned h) {
    size_t stride=(size_t)w*channels;
    uint8_t *data=malloc(stride*h);
    if(!data) return NULL;
    for(unsigned row=0;row<h;++row) {
        uint8_t *out=data+row*stride;
        memcpy(out,pixels+((size_t)(y+row)*width+x)*channels,stride);
        if(channels==2 || channels==4) for(size_t i=0;i<stride;i+=channels) {
            unsigned alpha=out[i+channels-1];
            for(unsigned c=0;c+1<channels;++c) out[i+c]=(uint8_t)((out[i+c]*alpha+127)/255);
        }
    }
    return data;
}
static GLuint update(BtImageRenderer *r, Texture *t, const BtPresentationImage *im, GLenum format) {
    unsigned x0=im->width,y0=im->height,x1=0,y1=0,c=im->channels;
    size_t stride=(size_t)im->width*c;
    for(unsigned y=0;y<im->height;++y) {
        size_t row=(size_t)y*stride;
        if(!memcmp(t->shadow+row,im->pixels+row,stride)) continue;
        if(y<y0) y0=y;
        y1=y+1;
        for(unsigned x=0;x<im->width;++x) if(memcmp(t->shadow+row+(size_t)x*c,im->pixels+row+(size_t)x*c,c)) {
            if(x<x0) x0=x;
            if(x+1>x1) x1=x+1;
        }
    }
    if(y1) {
        unsigned w=x1-x0,h=y1-y0;
        uint8_t *data=stage(im->pixels,im->width,c,x0,y0,w,h);
        if(!data) { fail(r,"Could not stage image damage"); return 0; }
        glBindTexture(GL_TEXTURE_2D,t->texture);
        glPixelStorei(GL_UNPACK_ALIGNMENT,1);
        glTexSubImage2D(GL_TEXTURE_2D,0,x0,y0,w,h,format,GL_UNSIGNED_BYTE,data);
        free(data);
        if(glGetError()!=GL_NO_ERROR) { fail(r,"Could not upload image damage"); return 0; }
        for(unsigned y=y0;y<y1;++y) {
            size_t offset=(size_t)y*stride+(size_t)x0*c;
            memcpy(t->shadow+offset,im->pixels+offset,(size_t)w*c);
        }
        ++r->stats.region_uploads; r->stats.uploaded_bytes+=(uint64_t)w*h*c;
    } else ++r->stats.unchanged_updates;
    t->generation=im->generation; t->used=++r->clock;
    return t->texture;
}
static GLuint texture(BtImageRenderer *r, const BtPresentationImage *image, uint32_t *width, uint32_t *height) {
    uint64_t stamp=image->generation;
    *width=image->width; *height=image->height;
    for(unsigned i=0;i<TEXTURES;++i) if(r->textures[i].texture && r->textures[i].generation==stamp && r->textures[i].id==image->id) {
        r->textures[i].used=++r->clock; return r->textures[i].texture;
    }
    uint64_t bytes=(uint64_t)*width * *height * 4;
    if(!*width || !*height || *width>(unsigned)r->max_texture || *height>(unsigned)r->max_texture || bytes>GPU_BYTES) return 0;
    const uint8_t *pixels=image->pixels; size_t length=image->length;
    GLenum external,internal; unsigned channels=image->channels;
    switch(channels) {
        case 4: external=GL_RGBA; internal=GL_RGBA8; break;
        case 3: external=GL_RGB; internal=GL_RGB8; break;
        case 2: external=GL_RG; internal=GL_RG8; break;
        case 1: external=GL_RED; internal=GL_R8; break;
        default: return 0;
    }
    if((uint64_t)*width * *height * channels!=length) return 0;
    for(unsigned i=0;i<TEXTURES;++i) {
        Texture *t=&r->textures[i];
        if(!t->texture || t->id!=image->id) continue;
        if(t->width==*width && t->height==*height && t->channels==channels)
            return update(r,t,image,external);
        evict(r,i);
        break;
    }
    while(r->bytes+bytes>GPU_BYTES) {
        unsigned slot=0;
        for(unsigned i=1;i<TEXTURES;++i)
            if(r->textures[i].texture && (!r->textures[slot].texture || r->textures[i].used<r->textures[slot].used)) slot=i;
        evict(r,slot);
    }
    unsigned slot=oldest(r); evict(r,slot);
    Texture *t=&r->textures[slot];
    glGenTextures(1,&t->texture); glBindTexture(GL_TEXTURE_2D,t->texture);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MIN_FILTER,GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MAG_FILTER,GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_WRAP_S,GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_WRAP_T,GL_CLAMP_TO_EDGE);
    if(channels<3) {
        glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_SWIZZLE_G,GL_RED);
        glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_SWIZZLE_B,GL_RED);
        glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_SWIZZLE_A,channels==2?GL_GREEN:GL_ONE);
    }
    uint8_t *data=stage(pixels,*width,channels,0,0,*width,*height);
    uint8_t *shadow=malloc(length);
    if(!data || !shadow) {
        free(data); free(shadow); evict(r,slot);
        fail(r,"Could not stage image pixels"); return 0;
    }
    memcpy(shadow,pixels,length);
    glPixelStorei(GL_UNPACK_ALIGNMENT,1);
    glTexImage2D(GL_TEXTURE_2D,0,(GLint)internal,*width,*height,0,external,GL_UNSIGNED_BYTE,data);
    free(data);
    if(glGetError()!=GL_NO_ERROR) {
        free(shadow); evict(r,slot); fail(r,"Could not upload image pixels"); return 0;
    }
    t->shadow=shadow; t->length=length; t->id=image->id;
    t->width=*width; t->height=*height; t->channels=channels;
    t->generation=stamp; t->used=++r->clock; t->bytes=(size_t)bytes; r->bytes+=t->bytes;
    ++r->stats.full_uploads; r->stats.uploaded_bytes+=length; r->stats.shadow_bytes+=length;
    return t->texture;
}
int bt_images_draw(BtImageRenderer *r, int layer, int width, int height, unsigned cols, unsigned rows,
                  int origin_x, int origin_y, int viewport_width, int viewport_height) {
    if(!r->frame) return 0;
    glUseProgram(r->program); glUniform2f(r->viewport,width,height);
    glBindVertexArray(r->vao); glBindBuffer(GL_ARRAY_BUFFER,r->vbo);
    glActiveTexture(GL_TEXTURE0); glEnable(GL_BLEND); glBlendFunc(GL_ONE,GL_ONE_MINUS_SRC_ALPHA);
    int grid_width=(int)(cols*r->cw),grid_height=(int)(rows*r->ch);
    if(grid_width>width-PAD) grid_width=width-PAD;
    if(grid_height>height-PAD) grid_height=height-PAD;
    if(grid_width<=0 || grid_height<=0) return 0;
    glEnable(GL_SCISSOR_TEST);
    int clip_x=origin_x+(int)((int64_t)PAD*viewport_width/width);
    int clip_y=origin_y+(int)((int64_t)(height-PAD-grid_height)*viewport_height/height);
    int clip_right=origin_x+(int)(((int64_t)(PAD+grid_width)*viewport_width+width-1)/width);
    int clip_top=origin_y+(int)(((int64_t)(height-PAD)*viewport_height+height-1)/height);
    if(clip_right>origin_x+viewport_width) clip_right=origin_x+viewport_width;
    if(clip_top>origin_y+viewport_height) clip_top=origin_y+viewport_height;
    if(clip_right<=clip_x || clip_top<=clip_y) return 0;
    glScissor(clip_x,clip_y,clip_right-clip_x,clip_top-clip_y);
    for(size_t i=0;i<r->frame->placement_count;++i) {
        const BtPresentationPlacement *p=&r->frame->placements[i];
        int actual=p->z<INT32_MIN/2?0:p->z<0?1:2;
        if(actual!=layer) continue;
        if(p->image_index>=r->frame->image_count) { glDisable(GL_SCISSOR_TEST); return fail(r,"Invalid image reference"); }
        uint32_t iw=0,ih=0; GLuint id=texture(r,&r->frame->images[p->image_index],&iw,&ih);
        if(!id) continue;
        const GhosttyKittyGraphicsPlacementRenderInfo *g=&p->geometry;
        float x=PAD+(float)g->viewport_col*r->cw+(p->x_offset<r->cw?p->x_offset:r->cw-1);
        float y=PAD+(float)g->viewport_row*r->ch+(p->y_offset<r->ch?p->y_offset:r->ch-1);
        float right=x+g->pixel_width,bottom=y+g->pixel_height;
        float u=(float)g->source_x/iw,v=(float)g->source_y/ih;
        float ur=(float)(g->source_x+g->source_width)/iw,vb=(float)(g->source_y+g->source_height)/ih;
        float vertices[]={x,y,u,v, right,y,ur,v, x,bottom,u,vb,
                          x,bottom,u,vb, right,y,ur,v, right,bottom,ur,vb};
        glUniform4f(r->source_bounds,((float)g->source_x+.5f)/iw,((float)g->source_y+.5f)/ih,
                    ((float)g->source_x+g->source_width-.5f)/iw,((float)g->source_y+g->source_height-.5f)/ih);
        glBindTexture(GL_TEXTURE_2D,id);
        glBufferData(GL_ARRAY_BUFFER,sizeof(vertices),vertices,GL_STREAM_DRAW);
        glDrawArrays(GL_TRIANGLES,0,6);
    }
    glScissor(origin_x,origin_y,viewport_width,viewport_height);
    if(glGetError()!=GL_NO_ERROR) return fail(r,"OpenGL image rendering failed");
    return r->error[0]?-1:0;
}
void bt_images_free(BtImageRenderer *r) {
    if(!r) return;
    for(unsigned i=0;i<TEXTURES;++i) evict(r,i);
    if(r->program) glDeleteProgram(r->program);
    if(r->vao) glDeleteVertexArrays(1,&r->vao);
    if(r->vbo) glDeleteBuffers(1,&r->vbo);
    free(r);
}
