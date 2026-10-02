/* SPDX-License-Identifier: MIT */
/* Native cell -> shaped glyph run -> cached atlas -> batched GLES quads. */
#define _POSIX_C_SOURCE 200809L
#include "render.h"
#include "image_renderer.h"
#include "presentation.h"
#include <ghostty/vt/unicode.h>
#include <GLES3/gl3.h>
#include <fontconfig/fontconfig.h>
#include <ft2build.h>
#include FT_FREETYPE_H
#include <hb-ft.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define ATLAS_SIZE 2048
#define GLYPH_SLOTS 8192
#define FONT_LIMIT 64
#define VERTEX_LIMIT 24576
#define PAD 8

typedef struct {
    FT_Face face;
    hb_font_t *hb;
    char *path;
    int index;
    float scale;
} Font;
typedef struct {
    bool used, colored;
    int font, left, top, width, height, x, y;
    uint32_t glyph;
} Glyph;
typedef struct { float x, y, u, v, r, g, b, a; } Vertex;
typedef struct {
    uint32_t offset, length;
    GhosttyStyle style;
    GhosttyColorRgb fg, bg, underline;
    GhosttyCellWide wide;
    int font;
} Cell;
struct BtRenderer {
    SDL_Window *window;
    SDL_GLContext context;
    bool owns_context, composite, scale_region;
    SDL_Rect region;
    int origin_x, origin_y, viewport_width, viewport_height;
    FT_Library ft;
    FcConfig *fc;
    FcFontSet *sets[4];
    Font fonts[FONT_LIMIT];
    int font_count, requested_size, pixel_size, cw, ch, ascent;
    GLuint program, texture, vao, vbo;
    GLint viewport_uniform;
    Glyph glyphs[GLYPH_SLOTS];
    int atlas_x, atlas_y, atlas_row_height;
    Vertex vertices[VERTEX_LIMIT];
    size_t vertex_count;
    hb_buffer_t *shape;
    BtPresenter *presenter;
    GhosttyTerminal local_terminal;
    BtPresentation *local_frame;
    uint64_t local_epoch, local_revision, frame_epoch, frame_revision;
    bool had_frame, last_remote;
    BtImageRenderer *images;
    Cell *cells;
    size_t cell_capacity;
    uint32_t *codepoints;
    size_t cp_capacity, cp_used;
    int width, height;
    uint64_t blink_tick;
    uint64_t frames;
    bool animated;
    bool capture_mode, last_focused;
    BtSession *last_session;
    char preedit[1025];
    unsigned preedit_start, preedit_length;
    char error[256];
};
static int error(BtRenderer *r, const char *message) {
    snprintf(r->error, sizeof(r->error), "%s", message);
    return -1;
}
const char *bt_renderer_error(BtRenderer *r) { return r->error; }
uint64_t bt_renderer_frames(BtRenderer *r) { return r->frames; }
bool bt_renderer_due(BtRenderer *r) { return !r->had_frame || (r->animated && r->blink_tick!=bt_millis()/600); }
void bt_renderer_preedit(BtRenderer *r, const char *utf8, int start, int length) {
    size_t n=utf8?strnlen(utf8,sizeof(r->preedit)):0;
    if(n>=sizeof(r->preedit)) {
        n=sizeof(r->preedit)-1;
        while(n && ((unsigned char)utf8[n]&0xc0)==0x80) --n;
    }
    if(n) memcpy(r->preedit,utf8,n);
    r->preedit[n]=0;
    r->preedit_start=start>0?(unsigned)(start>1024?1024:start):0;
    r->preedit_length=length>0?(unsigned)(length>1024?1024:length):0;
}
int bt_renderer_graphics(BtRenderer *r, char *out, size_t capacity) {
    if(SDL_GL_MakeCurrent(r->window,r->context)) return error(r,SDL_GetError());
    const GLubyte *vendor=glGetString(GL_VENDOR), *renderer=glGetString(GL_RENDERER);
    const GLubyte *version=glGetString(GL_VERSION), *shading=glGetString(GL_SHADING_LANGUAGE_VERSION);
    if(!vendor || !renderer || !version || !shading) return error(r,"Could not query graphics context");
    int length=snprintf(out,capacity,"GL_VENDOR: %s\nGL_RENDERER: %s\nGL_VERSION: %s\nGLSL: %s",
                        vendor,renderer,version,shading);
    return length<0 || (size_t)length>=capacity ? error(r,"Graphics information exceeds buffer") : 0;
}
static void flush(BtRenderer *r) {
    if (!r->vertex_count) return;
    glBufferData(GL_ARRAY_BUFFER, (GLsizeiptr)(r->vertex_count * sizeof(Vertex)), r->vertices, GL_STREAM_DRAW);
    glDrawArrays(GL_TRIANGLES, 0, (GLsizei)r->vertex_count);
    r->vertex_count = 0;
}
static void atlas_reset(BtRenderer *r) {
    /* Finish commands that reference old atlas coordinates before reusing them. */
    flush(r);
    memset(r->glyphs, 0, sizeof(r->glyphs));
    r->atlas_x = 2; r->atlas_y = 0; r->atlas_row_height = 2;
    glBindTexture(GL_TEXTURE_2D, r->texture);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, ATLAS_SIZE, ATLAS_SIZE, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
    uint8_t white[4] = {255,255,255,255};
    glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, white);
}
static void quad(BtRenderer *r, float x, float y, float w, float h,
                 float u, float v, float uw, float vh, GhosttyColorRgb color) {
    if (w <= 0 || h <= 0) return;
    if (r->vertex_count + 6 > VERTEX_LIMIT) flush(r);
    float red=color.r/255.f, green=color.g/255.f, blue=color.b/255.f;
    Vertex vertices[] = {
        {x,y,u,v,red,green,blue,1}, {x+w,y,u+uw,v,red,green,blue,1}, {x,y+h,u,v+vh,red,green,blue,1},
        {x,y+h,u,v+vh,red,green,blue,1}, {x+w,y,u+uw,v,red,green,blue,1}, {x+w,y+h,u+uw,v+vh,red,green,blue,1}};
    memcpy(r->vertices+r->vertex_count, vertices, sizeof(vertices));
    r->vertex_count += 6;
}
static void rect(BtRenderer *r, float x, float y, float w, float h, GhosttyColorRgb color) {
    quad(r,x,y,w,h,.5f/ATLAS_SIZE,.5f/ATLAS_SIZE,0,0,color);
}
static void underline(BtRenderer *r, unsigned x, unsigned y, int style, GhosttyColorRgb color) {
    unsigned stroke=r->ch>=32?(unsigned)r->ch/16:1;
    float left=PAD+x*r->cw, bottom=PAD+(y+1)*r->ch;
    if(style==GHOSTTY_SGR_UNDERLINE_SINGLE || style==GHOSTTY_SGR_UNDERLINE_DOUBLE) {
        rect(r,left,bottom-2*stroke,r->cw,stroke,color);
        if(style==GHOSTTY_SGR_UNDERLINE_DOUBLE)
            rect(r,left,bottom-4*stroke,r->cw,stroke,color);
        return;
    }
    /* Phase follows terminal pixels, so adjacent cells form one continuous
     * pattern even across style runs, wide tails and cursor redraws. */
    for(unsigned pixel=0;pixel<(unsigned)r->cw;++pixel) {
        unsigned position=x*(unsigned)r->cw+pixel;
        if(style==GHOSTTY_SGR_UNDERLINE_DOTTED) {
            if(position%(4*stroke)<stroke)
                rect(r,left+pixel,bottom-2*stroke,1,stroke,color);
        } else if(style==GHOSTTY_SGR_UNDERLINE_DASHED) {
            if(position%(6*stroke)<4*stroke)
                rect(r,left+pixel,bottom-2*stroke,1,stroke,color);
        } else if(style==GHOSTTY_SGR_UNDERLINE_CURLY) {
            unsigned phase=position%(4*stroke);
            unsigned rise=phase<2*stroke?phase:4*stroke-phase;
            rect(r,left+pixel,bottom-4*stroke+rise,1,stroke,color);
        }
    }
}
static int set_size(BtRenderer *r, Font *font) {
    font->scale = 1;
    if (FT_Set_Pixel_Sizes(font->face, 0, r->pixel_size)) {
        if (!font->face->num_fixed_sizes) return -1;
        int best=0;
        for (int i=1;i<font->face->num_fixed_sizes;++i)
            if (abs(font->face->available_sizes[i].height-r->pixel_size) <
                abs(font->face->available_sizes[best].height-r->pixel_size)) best=i;
        if (FT_Select_Size(font->face,best)) return -1;
        font->scale=(float)r->pixel_size / font->face->available_sizes[best].height;
    }
    if (font->hb) hb_ft_font_changed(font->hb);
    return 0;
}
static int load_font(BtRenderer *r, FcPattern *pattern) {
    FcChar8 *file=NULL;
    int index=0;
    if (FcPatternGetString(pattern, FC_FILE, 0, &file) != FcResultMatch) return -1;
    FcPatternGetInteger(pattern, FC_INDEX, 0, &index);
    for (int i=0;i<r->font_count;++i)
        if (r->fonts[i].index==index && !strcmp(r->fonts[i].path,(char *)file)) return i;
    if (r->font_count == FONT_LIMIT) return 0;
    Font *f=&r->fonts[r->font_count];
    if (FT_New_Face(r->ft,(char *)file,index,&f->face)) return -1;
    if (set_size(r,f)) { FT_Done_Face(f->face); f->face=NULL; return -1; }
    f->path=strdup((char *)file);
    if (!f->path) { FT_Done_Face(f->face); f->face=NULL; return -1; }
    f->index=index;
    f->hb=hb_ft_font_create_referenced(f->face);
    return r->font_count++;
}
static bool ignorable(uint32_t cp) {
    return cp==0x200d || cp==0x200c || (cp>=0xfe00 && cp<=0xfe0f) || (cp>=0xe0100 && cp<=0xe01ef);
}
static int choose_font(BtRenderer *r, Cell *cell) {
    int variant=(cell->style.bold?1:0) | (cell->style.italic?2:0);
    FcFontSet *set=r->sets[variant];
    for (int i=0;i<set->nfont;++i) {
        FcCharSet *chars=NULL;
        if (FcPatternGetCharSet(set->fonts[i],FC_CHARSET,0,&chars)!=FcResultMatch) continue;
        bool covers=true;
        for (uint32_t j=0;j<cell->length;++j) {
            uint32_t cp=r->codepoints[cell->offset+j];
            if (!ignorable(cp) && !FcCharSetHasChar(chars,cp)) { covers=false; break; }
        }
        if (covers) { int font=load_font(r,set->fonts[i]); if (font>=0) return font; }
    }
    return 0;
}
static Glyph *glyph(BtRenderer *r, int font, uint32_t id) {
    size_t slot=((uint64_t)id*2654435761u+(unsigned)font*40503u)%GLYPH_SLOTS;
    size_t searched=0;
    while (r->glyphs[slot].used) {
        if (r->glyphs[slot].font==font && r->glyphs[slot].glyph==id) return &r->glyphs[slot];
        slot=(slot+1)%GLYPH_SLOTS;
        if (++searched==GLYPH_SLOTS) { atlas_reset(r); return glyph(r,font,id); }
    }
    FT_Face face=r->fonts[font].face;
    if (FT_Load_Glyph(face,id,FT_LOAD_DEFAULT|FT_LOAD_COLOR) ||
        (face->glyph->format!=FT_GLYPH_FORMAT_BITMAP && FT_Render_Glyph(face->glyph,FT_RENDER_MODE_NORMAL)))
        return NULL;
    FT_Bitmap *bm=&face->glyph->bitmap;
    int w=(int)bm->width, h=(int)bm->rows;
    if(w && h && bm->pixel_mode!=FT_PIXEL_MODE_BGRA && bm->pixel_mode!=FT_PIXEL_MODE_GRAY && bm->pixel_mode!=FT_PIXEL_MODE_MONO) {
        error(r,"Unsupported font bitmap format"); return NULL;
    }
    if (w+2>=ATLAS_SIZE || h+2>=ATLAS_SIZE) { error(r,"Font glyph exceeds atlas limit"); return NULL; }
    if (r->atlas_x+w+2>=ATLAS_SIZE) { r->atlas_x=0; r->atlas_y+=r->atlas_row_height; r->atlas_row_height=0; }
    if (r->atlas_y+h+2>=ATLAS_SIZE) { atlas_reset(r); return glyph(r,font,id); }
    Glyph *g=&r->glyphs[slot];
    *g=(Glyph){.used=true,.font=font,.glyph=id,.left=face->glyph->bitmap_left,.top=face->glyph->bitmap_top,
        .width=w,.height=h,.x=r->atlas_x+1,.y=r->atlas_y+1,.colored=bm->pixel_mode==FT_PIXEL_MODE_BGRA};
    if (w && h) {
        /* Clear the surrounding texels too: linear filtering samples the gutter. */
        uint8_t *pixels=calloc((size_t)(w+2)*(h+2),4);
        if (!pixels) { error(r,"Could not allocate glyph upload"); return NULL; }
        for (int y=0;y<h;++y) {
            const uint8_t *row=bm->buffer+(bm->pitch<0 ? h-1-y : y)*abs(bm->pitch);
            for (int x=0;x<w;++x) {
                uint8_t *p=pixels+((size_t)(y+1)*(w+2)+x+1)*4;
                if (g->colored) { p[0]=row[x*4+2]; p[1]=row[x*4+1]; p[2]=row[x*4]; p[3]=row[x*4+3]; }
                else {
                    uint8_t alpha=bm->pixel_mode==FT_PIXEL_MODE_MONO ? ((row[x/8] & (0x80>>(x%8)))?255:0) : row[x];
                    p[0]=p[1]=p[2]=p[3]=alpha;
                }
            }
        }
        glTexSubImage2D(GL_TEXTURE_2D,0,g->x-1,g->y-1,w+2,h+2,GL_RGBA,GL_UNSIGNED_BYTE,pixels);
        free(pixels);
    }
    r->atlas_x+=w+2;
    if (h+2>r->atlas_row_height) r->atlas_row_height=h+2;
    return g;
}
static GLuint shader(BtRenderer *r, GLenum kind, const char *source) {
    GLuint id=glCreateShader(kind);
    glShaderSource(id,1,&source,NULL); glCompileShader(id);
    GLint compiled=0; glGetShaderiv(id,GL_COMPILE_STATUS,&compiled);
    if (!compiled) { glGetShaderInfoLog(id,sizeof(r->error),NULL,r->error); glDeleteShader(id); return 0; }
    return id;
}
int bt_renderer_metrics(BtRenderer *r, int *cw, int *ch) {
    int logical_w, logical_h;
    SDL_GetWindowSize(r->window,&logical_w,&logical_h);
    SDL_GL_GetDrawableSize(r->window,&r->width,&r->height);
    int pixels=(int)lround((double)r->requested_size*r->height/(logical_h?logical_h:1));
    if (pixels<1 || pixels>384) return error(r,"Unsupported window scale");
    if (pixels!=r->pixel_size) {
        r->pixel_size=pixels;
        for (int i=0;i<r->font_count;++i) if (set_size(r,&r->fonts[i])) return error(r,"Could not resize font");
        if (r->texture) { SDL_GL_MakeCurrent(r->window,r->context); atlas_reset(r); }
    }
    Font *font=&r->fonts[0];
    if (FT_Load_Char(font->face,'M',FT_LOAD_DEFAULT)) return error(r,"Could not measure font");
    r->cw=(int)ceilf(font->face->glyph->advance.x/64.f*font->scale);
    r->ch=(int)ceilf(font->face->size->metrics.height/64.f*font->scale)+2;
    r->ascent=(int)ceilf(font->face->size->metrics.ascender/64.f*font->scale)+1;
    if (r->cw<1 || r->ch<1) return error(r,"Invalid font cell dimensions");
    *cw=r->cw; *ch=r->ch;
    return 0;
}
BtRenderer *bt_renderer_new_shared(SDL_Window *window, SDL_GLContext context, const char *family, int size, char *out_error, size_t error_size) {
    BtRenderer *r=calloc(1,sizeof(*r));
    if (!r) { snprintf(out_error,error_size,"Could not allocate renderer"); return NULL; }
    r->window=window; r->requested_size=size; r->pixel_size=size;
    r->owns_context=context==NULL;
    r->context=context?context:SDL_GL_CreateContext(window);
    if (!r->context) { error(r,SDL_GetError()); goto fail; }
    if(SDL_GL_MakeCurrent(window,r->context)) { error(r,SDL_GetError()); goto fail; }
    SDL_GL_SetSwapInterval(0); /* The controller sets cadence; idle windows do not redraw. */
    if (FT_Init_FreeType(&r->ft)) { error(r,"Could not initialize FreeType"); goto fail; }
    r->fc=FcInitLoadConfigAndFonts();
    if (!r->fc) { error(r,"Could not initialize Fontconfig"); goto fail; }
    for (int i=0;i<4;++i) {
        FcPattern *p=FcPatternCreate();
        FcPatternAddString(p,FC_FAMILY,(const FcChar8 *)family);
        FcPatternAddInteger(p,FC_WEIGHT,(i&1)?FC_WEIGHT_BOLD:FC_WEIGHT_REGULAR);
        FcPatternAddInteger(p,FC_SLANT,(i&2)?FC_SLANT_ITALIC:FC_SLANT_ROMAN);
        FcPatternAddDouble(p,FC_PIXEL_SIZE,size);
        FcConfigSubstitute(r->fc,p,FcMatchPattern); FcDefaultSubstitute(p);
        FcResult result;
        /* Keep style variants even where their character coverage is identical. */
        r->sets[i]=FcFontSort(r->fc,p,FcFalse,NULL,&result);
        FcPatternDestroy(p);
        if (!r->sets[i] || !r->sets[i]->nfont) { error(r,"No usable font found"); goto fail; }
    }
    if (load_font(r,r->sets[0]->fonts[0])<0) { error(r,"Could not load primary font"); goto fail; }
    int cw,ch;
    if (bt_renderer_metrics(r,&cw,&ch)) goto fail;
    const char *vertex="#version 300 es\nprecision highp float;\nlayout(location=0) in vec2 position;\n"
        "layout(location=1) in vec2 uv; layout(location=2) in vec4 color;\n"
        "uniform vec2 viewport; out vec2 tex; out vec4 tint;\n"
        "void main(){gl_Position=vec4(position.x/viewport.x*2.-1.,1.-position.y/viewport.y*2.,0.,1.);tex=uv;tint=color;}";
    const char *fragment="#version 300 es\nprecision mediump float;\nin vec2 tex; in vec4 tint;\n"
        "uniform sampler2D atlas; out vec4 result; void main(){result=texture(atlas,tex)*tint;}";
    GLuint vs=shader(r,GL_VERTEX_SHADER,vertex), fs=shader(r,GL_FRAGMENT_SHADER,fragment);
    if (!vs || !fs) { if(vs)glDeleteShader(vs); if(fs)glDeleteShader(fs); goto fail; }
    r->program=glCreateProgram(); glAttachShader(r->program,vs); glAttachShader(r->program,fs); glLinkProgram(r->program);
    glDeleteShader(vs); glDeleteShader(fs);
    GLint linked=0; glGetProgramiv(r->program,GL_LINK_STATUS,&linked);
    if (!linked) { glGetProgramInfoLog(r->program,sizeof(r->error),NULL,r->error); goto fail; }
    r->viewport_uniform=glGetUniformLocation(r->program,"viewport");
    glGenVertexArrays(1,&r->vao); glBindVertexArray(r->vao);
    glGenBuffers(1,&r->vbo); glBindBuffer(GL_ARRAY_BUFFER,r->vbo);
    glVertexAttribPointer(0,2,GL_FLOAT,GL_FALSE,sizeof(Vertex),(void *)0);
    glVertexAttribPointer(1,2,GL_FLOAT,GL_FALSE,sizeof(Vertex),(void *)(2*sizeof(float)));
    glVertexAttribPointer(2,4,GL_FLOAT,GL_FALSE,sizeof(Vertex),(void *)(4*sizeof(float)));
    for (int i=0;i<3;++i) glEnableVertexAttribArray(i);
    glGenTextures(1,&r->texture); glBindTexture(GL_TEXTURE_2D,r->texture);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MIN_FILTER,GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MAG_FILTER,GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_WRAP_S,GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_WRAP_T,GL_CLAMP_TO_EDGE);
    atlas_reset(r);
    r->shape=hb_buffer_create();
    r->images=bt_images_new(r->error,sizeof(r->error));
    if(!r->images) goto fail;
    return r;
fail:
    snprintf(out_error,error_size,"%s",r->error);
    bt_renderer_free(r);
    return NULL;
}
BtRenderer *bt_renderer_new(SDL_Window *window, const char *family, int size, char *error, size_t capacity) {
    return bt_renderer_new_shared(window,NULL,family,size,error,capacity);
}

static bool equal_color(GhosttyColorRgb a, GhosttyColorRgb b) { return a.r==b.r && a.g==b.g && a.b==b.b; }
static bool same_run(Cell *a, Cell *b) {
    return a->font==b->font && equal_color(a->fg,b->fg) && a->style.invisible==b->style.invisible &&
        a->style.blink==b->style.blink && a->style.bold==b->style.bold && a->style.italic==b->style.italic;
}
static int read_row(BtRenderer *r, const BtPresentation *f, unsigned y) {
    unsigned cols=f->cols;
    GhosttyRenderStateColors colors=f->colors;
    if (cols>r->cell_capacity) {
        Cell *cells=realloc(r->cells,cols*sizeof(Cell));
        if (!cells) return error(r,"Could not allocate cell row");
        r->cells=cells; r->cell_capacity=cols;
    }
    r->cp_used=0;
    for(unsigned x=0;x<cols;++x) {
        const BtPresentationCell *source=&f->cells[(size_t)y*cols+x];
        Cell *c=&r->cells[x];
        memset(c,0,sizeof(*c));
        c->style=source->style; c->wide=source->wide; c->length=source->length;
        bool placeholder=c->length && f->codepoints[source->offset]==0x10eeeeu;
        if(placeholder) { c->length=1; c->style.invisible=true; }
        c->fg=source->foreground; c->bg=source->background;
        if (c->style.bold && c->style.fg_color.tag==GHOSTTY_STYLE_COLOR_PALETTE && c->style.fg_color.value.palette<8)
            c->fg=colors.palette[c->style.fg_color.value.palette+8];
        if (c->style.inverse) { GhosttyColorRgb tmp=c->fg; c->fg=c->bg; c->bg=tmp; }
        if (c->style.faint) { c->fg.r/=2; c->fg.g/=2; c->fg.b/=2; }
        if (source->selected) { c->bg=(GhosttyColorRgb){59,85,119}; c->fg=(GhosttyColorRgb){255,255,255}; }
        c->underline=c->fg;
        if(c->style.underline_color.tag==GHOSTTY_STYLE_COLOR_RGB) c->underline=c->style.underline_color.value.rgb;
        else if(c->style.underline_color.tag==GHOSTTY_STYLE_COLOR_PALETTE) c->underline=colors.palette[c->style.underline_color.value.palette];
        c->offset=(uint32_t)r->cp_used;
        size_t need=r->cp_used+(c->length?c->length:1);
        if (need>1024u*1024u) return error(r,"Row grapheme limit exceeded");
        if (need>r->cp_capacity) {
            size_t cap=r->cp_capacity?r->cp_capacity:4096;
            while(cap<need) cap*=2;
            uint32_t *points=realloc(r->codepoints,cap*sizeof(*points));
            if (!points) return error(r,"Could not allocate grapheme buffer");
            r->codepoints=points; r->cp_capacity=cap;
        }
        if(placeholder) r->codepoints[c->offset]=' ';
        else if (c->length) memcpy(r->codepoints+c->offset,f->codepoints+source->offset,c->length*sizeof(uint32_t));
        else { c->length=1; r->codepoints[c->offset]=' '; }
        r->cp_used+=c->length;
        c->font=choose_font(r,c);
    }
    return 0;
}
static void draw_run_clipped(BtRenderer *r, unsigned begin, unsigned end, unsigned y, bool blink_visible,
                             const SDL_Rect *clip, const GhosttyColorRgb *foreground) {
    Cell *first=&r->cells[begin];
    if (first->style.blink) r->animated=true;
    if (first->style.invisible || (first->style.blink && !blink_visible)) return;
    hb_buffer_clear_contents(r->shape);
    hb_buffer_set_content_type(r->shape,HB_BUFFER_CONTENT_TYPE_UNICODE);
    for (unsigned x=begin;x<end;++x) {
        Cell *c=&r->cells[x];
        if (c->wide==GHOSTTY_CELL_WIDE_SPACER_TAIL || c->wide==GHOSTTY_CELL_WIDE_SPACER_HEAD) continue;
        for (uint32_t j=0;j<c->length;++j) hb_buffer_add(r->shape,r->codepoints[c->offset+j],x);
    }
    hb_buffer_guess_segment_properties(r->shape);
    /* Terminal cells retain logical left-to-right grid order. Contextual shaping
     * works within runs; paragraph bidirectional layout is not implemented. */
    hb_buffer_set_direction(r->shape,HB_DIRECTION_LTR);
    hb_shape(r->fonts[first->font].hb,r->shape,NULL,0);
    unsigned count;
    hb_glyph_info_t *info=hb_buffer_get_glyph_infos(r->shape,&count);
    hb_glyph_position_t *positions=hb_buffer_get_glyph_positions(r->shape,NULL);
    float scale=r->fonts[first->font].scale;
    for (unsigned i=0;i<count;) {
        unsigned j=i+1;
        while (j<count && info[j].cluster==info[i].cluster) ++j;
        unsigned cell=info[i].cluster;
        unsigned next=j<count ? info[j].cluster : end;
        if (next<=cell) next=cell+1;
        float advance=0;
        for (unsigned k=i;k<j;++k) advance+=positions[k].x_advance/64.f*scale;
        float span=(next-cell)*r->cw;
        float sx=advance>span ? span/advance : 1;
        float pen=0;
        for (unsigned k=i;k<j;++k) {
            Glyph *g=glyph(r,first->font,info[k].codepoint);
            if (g && g->width && g->height) {
                float px=PAD+cell*r->cw+(pen+(positions[k].x_offset/64.f+g->left)*scale)*sx;
                float py=PAD+y*r->ch+r->ascent-(positions[k].y_offset/64.f+g->top)*scale;
                float w=g->width*scale*sx, h=g->height*scale;
                float left=fmaxf(px,PAD+cell*r->cw), right=fminf(px+w,PAD+next*r->cw);
                float top=fmaxf(py,PAD+y*r->ch), bottom=fminf(py+h,PAD+(y+1)*r->ch);
                if(clip) {
                    left=fmaxf(left,clip->x); right=fminf(right,clip->x+clip->w);
                    top=fmaxf(top,clip->y); bottom=fminf(bottom,clip->y+clip->h);
                }
                if (right>left && bottom>top) {
                    float u=(g->x+(left-px)/w*g->width)/ATLAS_SIZE;
                    float v=(g->y+(top-py)/h*g->height)/ATLAS_SIZE;
                    quad(r,left,top,right-left,bottom-top,u,v,
                        (right-left)/w*g->width/ATLAS_SIZE,(bottom-top)/h*g->height/ATLAS_SIZE,
                        g->colored?(GhosttyColorRgb){255,255,255}:foreground?*foreground:first->fg);
                }
            }
            pen+=positions[k].x_advance/64.f*scale;
        }
        i=j;
    }
}
static void draw_run(BtRenderer *r, unsigned begin, unsigned end, unsigned y, bool blink_visible) {
    draw_run_clipped(r,begin,end,y,blink_visible,NULL,NULL);
}
static void decorations(BtRenderer *r, unsigned x, unsigned y, Cell *c, bool blink_visible,
                         const GhosttyColorRgb *foreground) {
    if(c->style.invisible || (c->style.blink && !blink_visible)) return;
    GhosttyColorRgb color=foreground?*foreground:c->fg;
    if(c->style.underline) underline(r,x,y,c->style.underline,foreground?*foreground:c->underline);
    if(c->style.strikethrough) rect(r,PAD+x*r->cw,PAD+y*r->ch+r->ascent*.65f,r->cw,1,color);
    if(c->style.overline) rect(r,PAD+x*r->cw,PAD+y*r->ch,r->cw,1,color);
}
static void text_state(BtRenderer *r) {
    glUseProgram(r->program); glUniform2f(r->viewport_uniform,r->width,r->height);
    glBindVertexArray(r->vao); glBindBuffer(GL_ARRAY_BUFFER,r->vbo);
    glActiveTexture(GL_TEXTURE0); glBindTexture(GL_TEXTURE_2D,r->texture);
    glEnable(GL_BLEND); glBlendFunc(GL_ONE,GL_ONE_MINUS_SRC_ALPHA);
}
static int image_pass(BtRenderer *r, const BtPresentation *f, int layer) {
    flush(r);
    if(bt_images_draw(r->images,layer,r->width,r->height,f->cols,f->rows,r->origin_x,r->origin_y,
                      r->viewport_width,r->viewport_height))
        return error(r,bt_images_error(r->images));
    text_state(r); return 0;
}
static void backgrounds(BtRenderer *r, const BtPresentation *f) {
    for(unsigned y=0;y<f->rows;++y) for(unsigned x=0;x<f->cols;++x) {
        const BtPresentationCell *c=&f->cells[(size_t)y*f->cols+x];
        /* Default backgrounds reveal the protocol's below-background layer. */
        if(c->explicit_background || c->style.inverse || c->selected) {
            GhosttyColorRgb bg=c->style.inverse?c->foreground:c->background;
            if(c->style.inverse && c->style.bold && c->style.fg_color.tag==GHOSTTY_STYLE_COLOR_PALETTE && c->style.fg_color.value.palette<8)
                bg=f->colors.palette[c->style.fg_color.value.palette+8];
            if(c->selected) bg=(GhosttyColorRgb){59,85,119};
            rect(r,PAD+x*r->cw,PAD+y*r->ch,r->cw,r->ch,bg);
        }
    }
}
typedef struct { unsigned offset,length,width; } PreeditCluster;
static unsigned preedit_decode(const char *text, uint32_t out[1024]) {
    unsigned count=0; size_t at=0,bytes=strlen(text);
    const unsigned char *p=(const unsigned char *)text;
    while(at<bytes && count<1024) {
        uint32_t cp=p[at]; unsigned width=1;
        if(p[at]>=0xc2 && p[at]<=0xdf) { cp=p[at]&31u; width=2; }
        else if(p[at]>=0xe0 && p[at]<=0xef) { cp=p[at]&15u; width=3; }
        else if(p[at]>=0xf0 && p[at]<=0xf4) { cp=p[at]&7u; width=4; }
        bool valid=width==1?p[at]<0x80:true;
        for(unsigned i=1;i<width && valid;++i) {
            if(at+i>=bytes || (p[at+i]&0xc0)!=0x80) valid=false;
            else cp=(cp<<6)|(p[at+i]&63u);
        }
        if(valid && ((width==2 && cp<0x80) || (width==3 && cp<0x800) ||
                     (width==4 && cp<0x10000) || (cp>=0xd800 && cp<=0xdfff) || cp>0x10ffff))
            valid=false;
        if(!valid) { cp=0xfffd; width=1; }
        if(cp<32 || (cp>=0x7f && cp<0xa0)) cp=0xfffd;
        out[count++]=cp; at+=width;
    }
    return count;
}
static int draw_preedit(BtRenderer *r, const BtPresentation *f) {
    if(!r->preedit[0] || !f->cursor.viewport_has_value || !f->cols || !f->rows) return 0;
    uint32_t points[1024]; PreeditCluster clusters[1024];
    unsigned count=preedit_decode(r->preedit,points), used=0,total=0,caret=0;
    unsigned start=r->preedit_start<count?r->preedit_start:count;
    for(unsigned i=0;i<count;) {
        uint8_t width=0;
        unsigned length=(unsigned)ghostty_unicode_grapheme_width(points+i,count-i,&width);
        if(!length) break;
        if(!width) width=1; /* A leading combining mark still needs a visible cell. */
        clusters[used++]=(PreeditCluster){i,length,width};
        if(i<start) caret+=width;
        total+=width; i+=length;
    }
    if(!used) return 0;
    unsigned first=0,skipped=0;
    while(total>f->cols && first+1<used && caret-skipped>=f->cols) {
        skipped+=clusters[first++].width;
    }
    unsigned visible=0,last=first;
    while(last<used && visible+clusters[last].width<=f->cols) visible+=clusters[last++].width;
    if(!visible) return 0;
    unsigned cursor_x=f->cursor.viewport_x<f->cols?f->cursor.viewport_x:f->cols-1;
    unsigned x0=total<=f->cols && cursor_x+visible>f->cols?f->cols-visible:cursor_x;
    if(total>f->cols) x0=0;
    unsigned y=f->cursor.viewport_y<f->rows?f->cursor.viewport_y:f->rows-1;
    GhosttyColorRgb foreground={245,247,252}, background={35,43,60};
    GhosttyColorRgb selected={65,85,124}, underline={119,192,255};
    rect(r,PAD+x0*r->cw,PAD+y*r->ch,visible*r->cw,r->ch,background);
    for(unsigned i=first,x=x0;i<last;++i) {
        PreeditCluster cluster=clusters[i];
        bool highlight=r->preedit_length && cluster.offset<start+r->preedit_length &&
                       cluster.offset+cluster.length>start;
        if(highlight) rect(r,PAD+x*r->cw,PAD+y*r->ch,cluster.width*r->cw,r->ch,selected);
        if(cluster.length>r->cp_capacity) {
            uint32_t *expanded=realloc(r->codepoints,cluster.length*sizeof(*expanded));
            if(!expanded) return error(r,"Could not allocate preedit text");
            r->codepoints=expanded; r->cp_capacity=cluster.length;
        }
        memcpy(r->codepoints,points+cluster.offset,cluster.length*sizeof(*points));
        Cell *cell=&r->cells[x]; memset(cell,0,sizeof(*cell));
        cell->length=cluster.length; cell->fg=foreground;
        cell->font=choose_font(r,cell);
        if(cluster.width==2) { memset(&r->cells[x+1],0,sizeof(*r->cells)); r->cells[x+1].wide=GHOSTTY_CELL_WIDE_SPACER_TAIL; }
        draw_run(r,x,x+cluster.width,y,true);
        rect(r,PAD+x*r->cw,PAD+(y+1)*r->ch-2,cluster.width*r->cw,2,underline);
        x+=cluster.width;
    }
    unsigned caret_x=caret>=skipped?caret-skipped:0;
    if(caret_x>=visible) caret_x=visible-1;
    rect(r,PAD+(x0+caret_x)*r->cw,PAD+y*r->ch,2,r->ch,underline);
    return 0;
}
static int draw_cursor(BtRenderer *r, const BtPresentation *f, bool focused, bool blink_visible) {
    unsigned col=f->cursor.viewport_x, row=f->cursor.viewport_y;
    if(f->cursor.wide_tail && col) --col;
    const BtPresentationCell *cell=&f->cells[(size_t)row*f->cols+col];
    unsigned span=cell->wide==GHOSTTY_CELL_WIDE_WIDE && col+1<f->cols?2:1;
    SDL_Rect box={PAD+(int)col*r->cw,PAD+(int)row*r->ch,(int)span*r->cw,r->ch};
    GhosttyColorRgb color=f->colors.cursor_has_value?f->colors.cursor:f->colors.foreground;
    if(focused && f->cursor.visual_style==GHOSTTY_RENDER_STATE_CURSOR_VISUAL_STYLE_BAR)
        rect(r,box.x,box.y,2,box.h,color);
    else if(focused && f->cursor.visual_style==GHOSTTY_RENDER_STATE_CURSOR_VISUAL_STYLE_UNDERLINE)
        rect(r,box.x,box.y+box.h-2,box.w,2,color);
    else if(focused && f->cursor.visual_style==GHOSTTY_RENDER_STATE_CURSOR_VISUAL_STYLE_BLOCK) {
        rect(r,box.x,box.y,box.w,box.h,color);
        if(read_row(r,f,row)) return -1;
        /* Shape the original run, then clip its glyphs to the cursor. Splitting
         * the run at the cursor would change ligatures and contextual shaping. */
        GhosttyColorRgb foreground=f->colors.background;
        if(equal_color(foreground,color)) foreground=f->colors.foreground;
        if(equal_color(foreground,color))
            foreground=(GhosttyColorRgb){255-color.r,255-color.g,255-color.b};
        for(unsigned begin=0;begin<f->cols;) {
            unsigned end=begin+1;
            while(end<f->cols && (r->cells[end].wide==GHOSTTY_CELL_WIDE_SPACER_TAIL ||
                                  same_run(&r->cells[begin],&r->cells[end]))) ++end;
            if(begin<col+span && end>col)
                draw_run_clipped(r,begin,end,row,blink_visible,&box,&foreground);
            begin=end;
        }
        for(unsigned x=col;x<col+span;++x)
            decorations(r,x,row,&r->cells[x],blink_visible,&foreground);
    } else {
        rect(r,box.x,box.y,box.w,1,color); rect(r,box.x,box.y+box.h-1,box.w,1,color);
        rect(r,box.x,box.y,1,box.h,color); rect(r,box.x+box.w-1,box.y,1,box.h,color);
    }
    return 0;
}
static int draw_frame(BtRenderer *r, const BtPresentation *f, bool focused, bool blink_visible) {
    SDL_GL_GetDrawableSize(r->window,&r->width,&r->height);
    int surface_height=r->height;
    r->origin_x=0; r->origin_y=0; r->viewport_width=r->width; r->viewport_height=r->height;
    if(r->composite) {
        r->origin_x=r->region.x;
        r->origin_y=surface_height-r->region.y-r->region.h;
        r->viewport_width=r->region.w; r->viewport_height=r->region.h;
        if(r->scale_region) {
            r->width=(int)((uint64_t)f->cols*f->cell_width+2*PAD);
            r->height=(int)((uint64_t)f->rows*f->cell_height+2*PAD);
        } else { r->width=r->region.w; r->height=r->region.h; }
    }
    glViewport(r->origin_x,r->origin_y,r->viewport_width,r->viewport_height);
    glEnable(GL_SCISSOR_TEST);
    glScissor(r->origin_x,r->origin_y,r->viewport_width,r->viewport_height);
    text_state(r);
    GhosttyRenderStateColors colors=f->colors;
    glClearColor(colors.background.r/255.f,colors.background.g/255.f,colors.background.b/255.f,1);
    glClear(GL_COLOR_BUFFER_BIT);
    if(bt_images_prepare(r->images,f)) return error(r,bt_images_error(r->images));
    if(image_pass(r,f,0)) return -1;
    backgrounds(r,f);
    if(image_pass(r,f,1)) return -1;
    for(unsigned y=0;y<f->rows;++y) {
        if(read_row(r,f,y)) return -1;
        for(unsigned begin=0;begin<f->cols;) {
            unsigned end=begin+1;
            while(end<f->cols && (r->cells[end].wide==GHOSTTY_CELL_WIDE_SPACER_TAIL || same_run(&r->cells[begin],&r->cells[end]))) ++end;
            draw_run(r,begin,end,y,blink_visible);
            begin=end;
        }
        for(unsigned x=0;x<f->cols;++x)
            decorations(r,x,y,&r->cells[x],blink_visible,NULL);
    }
    if(image_pass(r,f,2)) return -1;
    if(focused && draw_preedit(r,f)) return -1;
    GhosttyRenderStateCursor cursor=f->cursor;
    r->animated|=cursor.blinking && cursor.visible && focused;
    if(!(focused && r->preedit[0]) && cursor.visible && cursor.viewport_has_value &&
       (!cursor.blinking || blink_visible || !focused)) {
        if(draw_cursor(r,f,focused,blink_visible)) return -1;
    }
    flush(r);
    glDisable(GL_SCISSOR_TEST);
    if(glGetError()!=GL_NO_ERROR) return error(r,"OpenGL rendering failed");
    if(!r->capture_mode && !r->composite) SDL_GL_SwapWindow(r->window);
    if(!r->error[0]) ++r->frames;
    return r->error[0]?-1:0;
}
int bt_renderer_draw(BtRenderer *r, BtSession *s, bool force, bool focused) {
    if(!s) return error(r,"Missing terminal session");
    bool focus_changed=r->last_focused!=focused;
    r->last_session=s; r->last_focused=focused;
    if(SDL_GL_MakeCurrent(r->window,r->context)) return error(r,SDL_GetError());
    bool remote=s->presentation!=NULL;
    const BtPresentation *f=s->presentation;
    if(!remote) {
        if(r->local_terminal!=s->terminal || !r->presenter) {
            bt_presenter_free(r->presenter); r->presenter=NULL;
            bt_presentation_free(r->local_frame); r->local_frame=NULL;
            r->local_terminal=s->terminal;
            r->presenter=bt_presenter_new(s->terminal);
            if(!r->presenter) return error(r,"Could not create terminal presenter");
            if(!++r->local_epoch) ++r->local_epoch;
            r->local_revision=0;
            force=true;
        }
        BtPresentation *next=NULL;
        if(!++r->local_revision) ++r->local_revision;
        int captured=bt_presenter_capture(r->presenter,r->local_epoch,r->local_revision,
                                         s->cell_width,s->cell_height,force || !r->local_frame,&next);
        if(captured<0) return error(r,bt_presenter_error(r->presenter));
        if(!captured) { bt_presentation_free(r->local_frame); r->local_frame=next; }
        f=r->local_frame;
    }
    if(!f) return error(r,"Missing presentation frame");
    bool epoch_changed=!r->had_frame || r->last_remote!=remote || r->frame_epoch!=f->epoch;
    if(epoch_changed) { atlas_reset(r); bt_images_reset(r->images); }
    uint64_t tick=bt_millis()/600;
    if(!force && !focus_changed && !epoch_changed && r->frame_revision==f->revision &&
       (!r->animated || r->blink_tick==tick)) return 0;
    r->blink_tick=tick; r->animated=false;
    /* Geometry belongs to the owner. Keep local font metrics available for a
     * controller's resize proposal, while drawing observers on the same grid. */
    int local_cw=r->cw, local_ch=r->ch;
    r->cw=(int)f->cell_width; r->ch=(int)f->cell_height;
    int result=draw_frame(r,f,focused,(tick%2)==0);
    r->cw=local_cw; r->ch=local_ch;
    if(!result) {
        r->had_frame=true; r->last_remote=remote;
        r->frame_epoch=f->epoch; r->frame_revision=f->revision;
    }
    return result;
}
static int draw_region(BtRenderer *r, BtSession *s, SDL_Rect region, bool focused, bool scaled) {
    int width,height; SDL_GL_GetDrawableSize(r->window,&width,&height);
    if(region.x<0 || region.y<0 || region.w<1 || region.h<1 ||
       region.w>width || region.h>height || region.x>width-region.w || region.y>height-region.h)
        return error(r,"Terminal view is outside its window surface");
    r->region=region; r->composite=true; r->scale_region=scaled;
    int result=bt_renderer_draw(r,s,true,focused);
    r->composite=false; r->scale_region=false;
    return result;
}
int bt_renderer_draw_region(BtRenderer *r, BtSession *s, SDL_Rect region, bool focused) {
    return draw_region(r,s,region,focused,false);
}
int bt_renderer_draw_region_scaled(BtRenderer *r, BtSession *s, SDL_Rect region, bool focused) {
    return draw_region(r,s,region,focused,true);
}
int bt_renderer_capture(BtRenderer *r, const char *path) {
    if(!r->last_session) return error(r,"Render a frame before capturing it");
    /* Capture a freshly drawn back buffer; contents after a swap are undefined. */
    r->capture_mode=true;
    int rendered=bt_renderer_draw(r,r->last_session,true,r->last_focused);
    r->capture_mode=false;
    if(rendered) return -1;
    SDL_GL_MakeCurrent(r->window,r->context);
    SDL_GL_GetDrawableSize(r->window,&r->width,&r->height);
    size_t size=(size_t)r->width*r->height*4;
    uint8_t *pixels=malloc(size);
    if(!pixels) return error(r,"Could not allocate screenshot");
    glReadPixels(0,0,r->width,r->height,GL_RGBA,GL_UNSIGNED_BYTE,pixels);
    SDL_GL_SwapWindow(r->window);
    FILE *f=fopen(path,"wb");
    if(!f) { free(pixels); return error(r,"Could not open screenshot"); }
    bool ok=fprintf(f,"P6\n%d %d\n255\n",r->width,r->height)>0;
    for(int y=r->height-1;y>=0 && ok;--y) {
        uint8_t *row=pixels+(size_t)y*r->width*4;
        /* Compact RGBA in place, then write one PPM row at a time. */
        for(int x=0;x<r->width;++x) {
            row[x*3]=row[x*4]; row[x*3+1]=row[x*4+1]; row[x*3+2]=row[x*4+2];
        }
        size_t bytes=(size_t)r->width*3;
        ok=fwrite(row,1,bytes,f)==bytes;
    }
    free(pixels);
    if(fclose(f)) ok=false;
    return ok?0:error(r,"Could not write screenshot");
}
void bt_renderer_free(BtRenderer *r) {
    if(!r) return;
    bt_presenter_free(r->presenter);
    bt_presentation_free(r->local_frame);
    if(r->shape) hb_buffer_destroy(r->shape);
    for(int i=0;i<r->font_count;++i) {
        hb_font_destroy(r->fonts[i].hb); FT_Done_Face(r->fonts[i].face); free(r->fonts[i].path);
    }
    for(int i=0;i<4;++i) if(r->sets[i]) FcFontSetDestroy(r->sets[i]);
    if(r->fc) FcConfigDestroy(r->fc);
    if(r->ft) FT_Done_FreeType(r->ft);
    if(r->context) {
        SDL_GL_MakeCurrent(r->window,r->context);
        bt_images_free(r->images);
        if(r->program) glDeleteProgram(r->program);
        if(r->texture) glDeleteTextures(1,&r->texture);
        if(r->vbo) glDeleteBuffers(1,&r->vbo);
        if(r->vao) glDeleteVertexArrays(1,&r->vao);
        if(r->owns_context) SDL_GL_DeleteContext(r->context);
    }
    free(r->cells); free(r->codepoints); free(r);
}

BtImageStats bt_renderer_image_stats(BtRenderer *r) { return bt_images_stats(r->images); }
int bt_renderer_drop_image_cache(BtRenderer *r) {
    if(SDL_GL_MakeCurrent(r->window,r->context)) return error(r,SDL_GetError());
    bt_images_reset(r->images);
    return glGetError()==GL_NO_ERROR?0:error(r,"Could not release image cache");
}
