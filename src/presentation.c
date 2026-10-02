/* SPDX-License-Identifier: MIT */
#define _GNU_SOURCE
#include "presentation.h"
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>

#define MAX_CELLS 1000000u
#define IMAGE_MAP_SIZE 32768u
#define WIRE_HEADER_BYTES 860u
#define WIRE_CELL_BYTES 31u
#define WIRE_IMAGE_BYTES 25u
#define WIRE_PLACEMENT_BYTES 64u

typedef struct { size_t references; uint8_t bytes[]; } PixelOwner;

struct BtPresenter {
    GhosttyTerminal terminal;
    GhosttyRenderState state;
    GhosttyRenderStateRowIterator rows;
    GhosttyRenderStateRowCells cells;
    GhosttyKittyGraphicsPlacementIterator placements;
    GhosttyRenderStateCursor cursor;
    uint64_t epoch, image_generation;
    unsigned cw, ch;
    GhosttyTerminalScreen screen;
    char *title;
    size_t title_length;
    bool captured;
    BtPresentationImage *cached_images;
    size_t cached_count;
    char error[192];
};

static int bad(int code) { errno=code; return -1; }
static int presenter_error(BtPresenter *p, const char *text, int code) {
    if(p) snprintf(p->error,sizeof(p->error),"%s",text);
    return bad(code);
}
const char *bt_presenter_error(const BtPresenter *p) { return p?p->error:"No presenter"; }
BtPresenter *bt_presenter_new(GhosttyTerminal terminal) {
    if(!terminal) { errno=EINVAL; return NULL; }
    BtPresenter *p=calloc(1,sizeof(*p));
    if(!p) return NULL;
    p->terminal=terminal;
    if(ghostty_render_state_new(NULL,&p->state)!=GHOSTTY_SUCCESS ||
       ghostty_render_state_row_iterator_new(NULL,&p->rows)!=GHOSTTY_SUCCESS ||
       ghostty_render_state_row_cells_new(NULL,&p->cells)!=GHOSTTY_SUCCESS ||
       ghostty_kitty_graphics_placement_iterator_new(NULL,&p->placements)!=GHOSTTY_SUCCESS) {
        bt_presenter_free(p); errno=ENOMEM; return NULL;
    }
    return p;
}
static void release_image(BtPresentationImage *im) {
    PixelOwner *owner=im->pixel_owner;
    if(owner) { if(!--owner->references) free(owner); }
    else free(im->pixels);
}
static void clear_cached_images(BtPresenter *p) {
    for(size_t i=0;i<p->cached_count;++i) release_image(&p->cached_images[i]);
    free(p->cached_images); p->cached_images=NULL; p->cached_count=0;
}
static const BtPresentationImage *cached_image(BtPresenter *p, uint32_t id) {
    size_t low=0,high=p->cached_count;
    while(low<high) {
        size_t mid=low+(high-low)/2;
        if(p->cached_images[mid].id<id) low=mid+1; else high=mid;
    }
    return low<p->cached_count && p->cached_images[low].id==id?&p->cached_images[low]:NULL;
}
static int image_order(const void *a, const void *b) {
    const BtPresentationImage *x=a,*y=b;
    return x->id==y->id?0:x->id<y->id?-1:1;
}
static int cache_images(BtPresenter *p, const BtPresentation *f) {
    size_t count=f->borrowed_pixels?0:f->image_count;
    BtPresentationImage *next=count?malloc(count*sizeof(*next)):NULL;
    if(count && !next) return -1;
    if(count) {
        memcpy(next,f->images,count*sizeof(*next));
        qsort(next,count,sizeof(*next),image_order);
        for(size_t i=0;i<count;++i) ++((PixelOwner *)next[i].pixel_owner)->references;
    }
    clear_cached_images(p); p->cached_images=next; p->cached_count=count;
    return 0;
}
void bt_presenter_free(BtPresenter *p) {
    if(!p) return;
    if(p->state) ghostty_render_state_free(p->state);
    if(p->rows) ghostty_render_state_row_iterator_free(p->rows);
    if(p->cells) ghostty_render_state_row_cells_free(p->cells);
    if(p->placements) ghostty_kitty_graphics_placement_iterator_free(p->placements);
    clear_cached_images(p); free(p->title); free(p);
}
void bt_presentation_free(BtPresentation *f) {
    if(!f) return;
    if(!f->borrowed_pixels)
        for(size_t i=0;i<f->image_count;++i) release_image(&f->images[i]);
    if(f->mapping) munmap(f->mapping,f->mapping_length);
    free(f->images); free(f->placements); free(f->cells); free(f->codepoints);
    free(f->title); free(f);
}
static bool add_size(size_t *total, size_t n) {
    if(n>BT_PRESENTATION_MAX_BYTES-*total) return false;
    *total+=n; return true;
}
static bool color_valid(GhosttyStyleColor c) {
    return c.tag>=GHOSTTY_STYLE_COLOR_NONE && c.tag<=GHOSTTY_STYLE_COLOR_RGB;
}
/* Also bounds the decoded allocation footprint, independent of wire size. */
static int validate(const BtPresentation *f) {
    if(!f || !f->epoch || !f->revision || !f->cols || f->cols>1000 ||
       !f->rows || f->rows>1000 || !f->cell_width || f->cell_width>512 ||
       !f->cell_height || f->cell_height>512 || (unsigned)f->screen>GHOSTTY_TERMINAL_SCREEN_ALTERNATE ||
       f->cell_count!=(size_t)f->cols*f->rows || f->cell_count>MAX_CELLS ||
       f->codepoint_count>BT_PRESENTATION_MAX_CODEPOINTS ||
       f->image_count>BT_PRESENTATION_MAX_PLACEMENTS ||
       f->placement_count>BT_PRESENTATION_MAX_PLACEMENTS ||
       f->title_length>BT_PRESENTATION_MAX_TITLE || !f->title || !f->cells ||
       (f->codepoint_count && !f->codepoints) || (f->image_count && !f->images) ||
       (f->placement_count && !f->placements)) return bad(EINVAL);
    if((unsigned)f->cursor.visual_style>GHOSTTY_RENDER_STATE_CURSOR_VISUAL_STYLE_BLOCK_HOLLOW ||
       (f->cursor.viewport_has_value &&
        (f->cursor.viewport_x>=f->cols || f->cursor.viewport_y>=f->rows))) return bad(EINVAL);
    size_t memory=sizeof(*f), image_bytes=0;
    if(!add_size(&memory,f->cell_count*sizeof(*f->cells)) ||
       !add_size(&memory,f->codepoint_count*sizeof(*f->codepoints)) ||
       !add_size(&memory,f->image_count*sizeof(*f->images)) ||
       !add_size(&memory,f->placement_count*sizeof(*f->placements)) ||
       !add_size(&memory,f->title_length+1)) return bad(EFBIG);
    size_t offset=0, row_codepoints=0;
    for(size_t i=0;i<f->cell_count;++i) {
        const BtPresentationCell *c=f->cells+i;
        if(i%f->cols==0) row_codepoints=0;
        if(c->offset!=offset || c->length>f->codepoint_count-offset ||
           (unsigned)c->wide>GHOSTTY_CELL_WIDE_SPACER_HEAD ||
           c->style.underline<GHOSTTY_SGR_UNDERLINE_NONE || c->style.underline>GHOSTTY_SGR_UNDERLINE_DASHED ||
           !color_valid(c->style.fg_color) || !color_valid(c->style.bg_color) ||
           !color_valid(c->style.underline_color)) return bad(EINVAL);
        offset+=c->length;
        row_codepoints+=c->length?c->length:1;
        if(row_codepoints>1024u*1024u) return bad(EFBIG);
    }
    if(offset!=f->codepoint_count) return bad(EINVAL);
    for(size_t i=0;i<f->codepoint_count;++i) {
        uint32_t cp=f->codepoints[i];
        if(cp>0x10ffff || (cp>=0xd800 && cp<=0xdfff)) return bad(EINVAL);
    }
    for(size_t i=0;i<f->image_count;++i) {
        const BtPresentationImage *im=f->images+i;
        uint64_t length=(uint64_t)im->width*im->height*im->channels;
        if(!im->id || !im->generation || !im->width || im->width>10000 ||
           !im->height || im->height>10000 || im->channels<1 || im->channels>4 ||
           length!=im->length || !im->pixels) return bad(EINVAL);
        if(im->length>BT_PRESENTATION_MAX_IMAGE_BYTES-image_bytes ||
           !add_size(&memory,im->length)) return bad(EFBIG);
        image_bytes+=im->length;
    }
    /* Cache identities are unique within an epoch. Reject conflicting asset
     * records, rather than let a frontend reuse another image's texture. */
    if(f->image_count>1) {
        uint32_t *map=calloc(IMAGE_MAP_SIZE,sizeof(*map));
        if(!map) return -1;
        for(unsigned by_generation=0;by_generation<2;++by_generation) {
            memset(map,0,IMAGE_MAP_SIZE*sizeof(*map));
            for(size_t i=0;i<f->image_count;++i) {
                uint64_t key=by_generation?f->images[i].generation:f->images[i].id;
                size_t slot=(uint32_t)((key^(key>>32))*2654435761u)&(IMAGE_MAP_SIZE-1);
                while(map[slot]) {
                    const BtPresentationImage *other=&f->images[map[slot]-1];
                    if(key==(by_generation?other->generation:other->id)) { free(map); return bad(EINVAL); }
                    slot=(slot+1)&(IMAGE_MAP_SIZE-1);
                }
                map[slot]=(uint32_t)i+1;
            }
        }
        free(map);
    }
    for(size_t i=0;i<f->placement_count;++i) {
        const BtPresentationPlacement *p=f->placements+i;
        const GhosttyKittyGraphicsPlacementRenderInfo *g=&p->geometry;
        if(p->image_index>=f->image_count) return bad(EINVAL);
        const BtPresentationImage *im=f->images+p->image_index;
        if(p->image_id!=im->id || !g->viewport_visible || !g->pixel_width || !g->pixel_height ||
           !g->source_width || !g->source_height || g->source_x>=im->width || g->source_y>=im->height ||
           g->source_width>im->width-g->source_x || g->source_height>im->height-g->source_y)
            return bad(EINVAL);
        if(i) {
            const BtPresentationPlacement *previous=p-1;
            if(previous->z>p->z || (previous->z==p->z &&
               (previous->image_id>p->image_id || (previous->image_id==p->image_id && previous->placement_id>p->placement_id)))) return bad(EINVAL);
        }
    }
    return 0;
}
static bool same_cursor(const GhosttyRenderStateCursor *a, const GhosttyRenderStateCursor *b) {
    return a->viewport_has_value==b->viewport_has_value && a->visible==b->visible &&
        a->blinking==b->blinking && a->password_input==b->password_input && a->visual_style==b->visual_style &&
        (!a->viewport_has_value || (a->viewport_x==b->viewport_x && a->viewport_y==b->viewport_y && a->wide_tail==b->wide_tail));
}
static int placement_order(const void *a, const void *b) {
    const BtPresentationPlacement *x=a,*y=b;
    if(x->z!=y->z) return x->z<y->z?-1:1;
    if(x->image_id!=y->image_id) return x->image_id<y->image_id?-1:1;
    return x->placement_id==y->placement_id?0:x->placement_id<y->placement_id?-1:1;
}
static int grow(void **ptr, size_t *capacity, size_t need, size_t unit, size_t limit, size_t *memory) {
    if(need>limit) return bad(EFBIG);
    if(need<=*capacity) return 0;
    size_t next=*capacity?*capacity:16;
    while(next<need) next=next>limit/2?limit:next*2;
    size_t growth=(next-*capacity)*unit;
    if(growth>BT_PRESENTATION_MAX_BYTES-*memory) return bad(EFBIG);
    void *replacement=realloc(*ptr,next*unit);
    if(!replacement) return -1;
    *ptr=replacement; *capacity=next; *memory+=growth; return 0;
}
static int capture_images(BtPresenter *p, BtPresentation *f, GhosttyKittyGraphics storage, size_t *memory) {
    if(!storage) return 0;
    if(ghostty_kitty_graphics_get(storage,GHOSTTY_KITTY_GRAPHICS_DATA_PLACEMENT_ITERATOR,&p->placements)!=GHOSTTY_SUCCESS)
        return bad(EIO);
    uint32_t *map=calloc(IMAGE_MAP_SIZE,sizeof(*map));
    if(!map) return -1;
    size_t imcap=0, pcap=0, payload=0;
    GhosttyKittyGraphicsVirtualPlacementIterator virtual_iter=NULL;
    bool has_virtual=false;
    int result=-1;
    for(unsigned pass=0;pass<2;++pass) {
        if(pass && !has_virtual) break;
        if(pass) {
            GhosttyResult opened=ghostty_kitty_graphics_virtual_placement_iterator_new(
                p->terminal,NULL,f->cell_width,f->cell_height,&virtual_iter);
            if(opened!=GHOSTTY_SUCCESS) { errno=opened==GHOSTTY_OUT_OF_MEMORY?ENOMEM:EIO; goto done; }
        }
        while(true) {
            BtPresentationPlacement entry={.geometry=GHOSTTY_INIT_SIZED(GhosttyKittyGraphicsPlacementRenderInfo)};
            if(pass) {
                GhosttyKittyGraphicsVirtualPlacementInfo info={
                    .size=sizeof(info),
                    .geometry=GHOSTTY_INIT_SIZED(GhosttyKittyGraphicsPlacementRenderInfo)};
                if(!ghostty_kitty_graphics_virtual_placement_next(virtual_iter,&info)) break;
                entry.image_id=info.image_id; entry.placement_id=info.placement_id;
                entry.z=info.z; entry.x_offset=info.x_offset; entry.y_offset=info.y_offset;
                entry.geometry=info.geometry;
            } else {
                if(!ghostty_kitty_graphics_placement_next(p->placements)) break;
                bool is_virtual=false;
#define PLACEMENT(key,output) do { if(ghostty_kitty_graphics_placement_get(p->placements,key,output)!=GHOSTTY_SUCCESS) { errno=EIO; goto done; } } while(0)
                PLACEMENT(GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_IS_VIRTUAL,&is_virtual);
                if(is_virtual) { has_virtual=true; continue; }
                PLACEMENT(GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_IMAGE_ID,&entry.image_id);
                PLACEMENT(GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_PLACEMENT_ID,&entry.placement_id);
                PLACEMENT(GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_Z,&entry.z);
                PLACEMENT(GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_X_OFFSET,&entry.x_offset);
                PLACEMENT(GHOSTTY_KITTY_GRAPHICS_PLACEMENT_DATA_Y_OFFSET,&entry.y_offset);
#undef PLACEMENT
            }
            GhosttyKittyGraphicsImage image=ghostty_kitty_graphics_image(storage,entry.image_id);
            if(!image || (!pass && ghostty_kitty_graphics_placement_render_info(p->placements,image,p->terminal,&entry.geometry)!=GHOSTTY_SUCCESS) ||
               !entry.geometry.viewport_visible || !entry.geometry.source_width || !entry.geometry.source_height ||
               !entry.geometry.pixel_width || !entry.geometry.pixel_height) continue;
            size_t slot=((uint32_t)(entry.image_id*2654435761u))&(IMAGE_MAP_SIZE-1);
            while(map[slot] && f->images[map[slot]-1].id!=entry.image_id) slot=(slot+1)&(IMAGE_MAP_SIZE-1);
            if(!map[slot]) {
                BtPresentationImage im={.id=entry.image_id};
                GhosttyKittyImageFormat format;
                const uint8_t *pixels=NULL;
                GhosttyResult available=ghostty_kitty_graphics_image_get(image,GHOSTTY_KITTY_IMAGE_DATA_DATA_PTR,&pixels);
                if(available==GHOSTTY_NO_VALUE) continue;
                if(available!=GHOSTTY_SUCCESS || !pixels) { errno=EIO; goto done; }
#define IMAGE(key,output) do { if(ghostty_kitty_graphics_image_get(image,key,output)!=GHOSTTY_SUCCESS) { errno=EIO; goto done; } } while(0)
                IMAGE(GHOSTTY_KITTY_IMAGE_DATA_GENERATION,&im.generation);
                IMAGE(GHOSTTY_KITTY_IMAGE_DATA_WIDTH,&im.width);
                IMAGE(GHOSTTY_KITTY_IMAGE_DATA_HEIGHT,&im.height);
                IMAGE(GHOSTTY_KITTY_IMAGE_DATA_FORMAT,&format);
                IMAGE(GHOSTTY_KITTY_IMAGE_DATA_DATA_LEN,&im.length);
#undef IMAGE
                switch(format) {
                    case GHOSTTY_KITTY_IMAGE_FORMAT_GRAY: im.channels=1; break;
                    case GHOSTTY_KITTY_IMAGE_FORMAT_GRAY_ALPHA: im.channels=2; break;
                    case GHOSTTY_KITTY_IMAGE_FORMAT_RGB: im.channels=3; break;
                    case GHOSTTY_KITTY_IMAGE_FORMAT_RGBA: im.channels=4; break;
                    default: continue; /* Same unsupported-format behavior as the local renderer. */
                }
                if(!im.width || !im.height || im.width>10000 || im.height>10000 ||
                   (uint64_t)im.width*im.height*im.channels!=im.length) { errno=EPROTO; goto done; }
                if(im.length>BT_PRESENTATION_MAX_IMAGE_BYTES-payload) { errno=EFBIG; goto done; }
                if(grow((void **)&f->images,&imcap,f->image_count+1,sizeof(*f->images),BT_PRESENTATION_MAX_PLACEMENTS,memory)) goto done;
                if(!add_size(memory,im.length)) { errno=EFBIG; goto done; }
                if(f->borrowed_pixels) im.pixels=(uint8_t *)pixels;
                else {
                    const BtPresentationImage *old=p->epoch==f->epoch?cached_image(p,im.id):NULL;
                    if(old && old->generation==im.generation && old->width==im.width && old->height==im.height &&
                       old->channels==im.channels && old->length==im.length) {
                        PixelOwner *owner=old->pixel_owner;
                        if(owner->references>=SIZE_MAX-1) { errno=EOVERFLOW; goto done; }
                        ++owner->references; im.pixel_owner=owner; im.pixels=owner->bytes;
                    } else {
                        PixelOwner *owner=malloc(sizeof(*owner)+im.length);
                        if(!owner) goto done;
                        owner->references=1; memcpy(owner->bytes,pixels,im.length);
                        im.pixel_owner=owner; im.pixels=owner->bytes;
                    }
                }
                payload+=im.length;
                f->images[f->image_count++]=im;
                map[slot]=(uint32_t)f->image_count;
            }
            entry.image_index=map[slot]-1;
            if(grow((void **)&f->placements,&pcap,f->placement_count+1,sizeof(*f->placements),BT_PRESENTATION_MAX_PLACEMENTS,memory)) goto done;
            f->placements[f->placement_count++]=entry;
        }
    }
    if(f->placement_count>1) qsort(f->placements,f->placement_count,sizeof(*f->placements),placement_order);
    result=0;
done:
    ghostty_kitty_graphics_virtual_placement_iterator_free(virtual_iter);
    free(map); return result;
}
static int capture(BtPresenter *p, uint64_t epoch, uint64_t revision,
                   unsigned cw, unsigned ch, bool force, bool borrowed, BtPresentation **out) {
    if(out) *out=NULL;
    if(!p || !out || !epoch || !revision || !cw || !ch || cw>512 || ch>512) return bad(EINVAL);
    p->error[0]=0;
    if(ghostty_render_state_update(p->state,p->terminal)!=GHOSTTY_SUCCESS)
        return presenter_error(p,"Could not update terminal presentation",EIO);
    GhosttyRenderStateDirty dirty;
    GhosttyRenderStateCursor cursor=GHOSTTY_INIT_SIZED(GhosttyRenderStateCursor);
    GhosttyTerminalScreen screen;
    GhosttyString title={0};
    if(ghostty_render_state_get(p->state,GHOSTTY_RENDER_STATE_DATA_DIRTY,&dirty)!=GHOSTTY_SUCCESS ||
       ghostty_render_state_get(p->state,GHOSTTY_RENDER_STATE_DATA_CURSOR,&cursor)!=GHOSTTY_SUCCESS ||
       ghostty_terminal_get(p->terminal,GHOSTTY_TERMINAL_DATA_ACTIVE_SCREEN,&screen)!=GHOSTTY_SUCCESS)
        return presenter_error(p,"Could not read terminal presentation",EIO);
    (void)ghostty_terminal_get(p->terminal,GHOSTTY_TERMINAL_DATA_TITLE,&title);
    if(title.len>BT_PRESENTATION_MAX_TITLE) return presenter_error(p,"Terminal title exceeds presentation limit",EFBIG);
    if(title.len && !title.ptr) return presenter_error(p,"Invalid terminal title",EPROTO);
    GhosttyKittyGraphics storage=NULL;
    uint64_t generation=0;
    if(ghostty_terminal_get(p->terminal,GHOSTTY_TERMINAL_DATA_KITTY_GRAPHICS,&storage)==GHOSTTY_SUCCESS)
        (void)ghostty_kitty_graphics_get(storage,GHOSTTY_KITTY_GRAPHICS_DATA_GENERATION,&generation);
    bool title_changed=p->title_length!=title.len || (title.len && (!p->title || memcmp(p->title,title.ptr,title.len)));
    if(!force && p->captured && p->epoch==epoch && p->cw==cw && p->ch==ch && p->screen==screen &&
       generation==p->image_generation && dirty==GHOSTTY_RENDER_STATE_DIRTY_FALSE &&
       same_cursor(&p->cursor,&cursor) && !title_changed) return 1;
    BtPresentation *f=calloc(1,sizeof(*f));
    if(!f) return presenter_error(p,"Could not allocate presentation",ENOMEM);
    f->borrowed_pixels=borrowed;
    f->epoch=epoch; f->revision=revision; f->cell_width=cw; f->cell_height=ch; f->screen=screen;
    f->cursor=cursor;
    if(!f->cursor.viewport_has_value) { f->cursor.viewport_x=f->cursor.viewport_y=0; f->cursor.wide_tail=false; }
    f->colors=(GhosttyRenderStateColors)GHOSTTY_INIT_SIZED(GhosttyRenderStateColors);
    uint16_t cols=0, rows=0;
    int code=EIO;
    if(ghostty_render_state_get(p->state,GHOSTTY_RENDER_STATE_DATA_COLS,&cols)!=GHOSTTY_SUCCESS ||
       ghostty_render_state_get(p->state,GHOSTTY_RENDER_STATE_DATA_ROWS,&rows)!=GHOSTTY_SUCCESS ||
       ghostty_render_state_get(p->state,GHOSTTY_RENDER_STATE_DATA_COLORS,&f->colors)!=GHOSTTY_SUCCESS) goto failed;
    if(!cols || !rows || cols>1000 || rows>1000) { code=EFBIG; goto failed; }
    f->cols=cols; f->rows=rows; f->cell_count=(size_t)cols*rows;
    size_t memory=sizeof(*f);
    if(!add_size(&memory,f->cell_count*sizeof(*f->cells)) || !add_size(&memory,title.len+1)) { code=EFBIG; goto failed; }
    f->cells=calloc(f->cell_count,sizeof(*f->cells));
    f->title=malloc(title.len+1); f->title_length=title.len;
    if(!f->cells || !f->title) { code=ENOMEM; goto failed; }
    if(title.len) memcpy(f->title,title.ptr,title.len);
    f->title[title.len]=0;
    if(!f->colors.cursor_has_value) f->colors.cursor=(GhosttyColorRgb){0};
    if(ghostty_render_state_get(p->state,GHOSTTY_RENDER_STATE_DATA_ROW_ITERATOR,&p->rows)!=GHOSTTY_SUCCESS) goto failed;
    size_t cell=0, cp_capacity=0;
    while(ghostty_render_state_row_iterator_next(p->rows)) {
        if(ghostty_render_state_row_get(p->rows,GHOSTTY_RENDER_STATE_ROW_DATA_CELLS,&p->cells)!=GHOSTTY_SUCCESS) goto failed;
        while(ghostty_render_state_row_cells_next(p->cells)) {
            if(cell>=f->cell_count) goto failed;
            BtPresentationCell *c=f->cells+cell++;
            c->style=(GhosttyStyle)GHOSTTY_INIT_SIZED(GhosttyStyle);
            c->foreground=f->colors.foreground; c->background=f->colors.background;
            GhosttyCell raw;
            if(ghostty_render_state_row_cells_get(p->cells,GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_RAW,&raw)!=GHOSTTY_SUCCESS ||
               ghostty_cell_get(raw,GHOSTTY_CELL_DATA_WIDE,&c->wide)!=GHOSTTY_SUCCESS ||
               ghostty_render_state_row_cells_get(p->cells,GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_STYLE,&c->style)!=GHOSTTY_SUCCESS ||
               ghostty_render_state_row_cells_get(p->cells,GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_GRAPHEMES_LEN,&c->length)!=GHOSTTY_SUCCESS) goto failed;
            (void)ghostty_render_state_row_cells_get(p->cells,GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_FG_COLOR,&c->foreground);
            c->explicit_background=ghostty_render_state_row_cells_get(p->cells,GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_BG_COLOR,&c->background)==GHOSTTY_SUCCESS;
            (void)ghostty_render_state_row_cells_get(p->cells,GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_SELECTED,&c->selected);
            c->offset=(uint32_t)f->codepoint_count;
            if(c->length>BT_PRESENTATION_MAX_CODEPOINTS-f->codepoint_count) { code=EFBIG; goto failed; }
            if(grow((void **)&f->codepoints,&cp_capacity,f->codepoint_count+c->length,sizeof(*f->codepoints),BT_PRESENTATION_MAX_CODEPOINTS,&memory)) { code=errno; goto failed; }
            if(c->length && ghostty_render_state_row_cells_get(p->cells,GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_GRAPHEMES_BUF,f->codepoints+f->codepoint_count)!=GHOSTTY_SUCCESS) goto failed;
            f->codepoint_count+=c->length;
        }
    }
    if(cell!=f->cell_count) goto failed;
    if(capture_images(p,f,storage,&memory) || validate(f)) { code=errno; goto failed; }
    char *saved_title=malloc(title.len+1);
    if(!saved_title) { code=ENOMEM; goto failed; }
    if(cache_images(p,f)) { code=errno; free(saved_title); goto failed; }
    memcpy(saved_title,f->title,title.len+1);
    free(p->title); p->title=saved_title; p->title_length=title.len;
    p->cursor=cursor; p->epoch=epoch; p->cw=cw; p->ch=ch; p->screen=screen;
    p->image_generation=generation; p->captured=true;
    (void)ghostty_render_state_clean(p->state);
    *out=f; return 0;
failed:
    bt_presentation_free(f);
    return presenter_error(p,"Could not capture a complete bounded presentation",code);
}

int bt_presenter_capture(BtPresenter *p, uint64_t epoch, uint64_t revision,
                         unsigned cw, unsigned ch, bool force, BtPresentation **out) {
    return capture(p,epoch,revision,cw,ch,force,false,out);
}
int bt_presenter_capture_file(BtPresenter *p, uint64_t epoch, uint64_t revision,
                              unsigned cw, unsigned ch, bool force, BtPresentationFile *out) {
    if(!out) return bad(EINVAL);
    *out=(BtPresentationFile){.fd=-1};
    BtPresentation *f=NULL;
    int result=capture(p,epoch,revision,cw,ch,force,true,&f);
    if(result) return result;
    size_t length=0;
    int fd=bt_presentation_pack_fd(f,&length),error=errno;
    if(fd>=0) *out=(BtPresentationFile){fd,length,f->epoch,f->revision,f->cols,f->rows,cw,ch};
    else p->captured=false;
    bt_presentation_free(f);
    if(fd<0) return presenter_error(p,"Could not publish captured presentation",error);
    return 0;
}

/* Version 1 is self-contained. Version 2 retains complete semantic metadata
 * and uses image references/rectangles against an exact base revision. Sizes
 * and enum encodings belong to these codecs, not the C ABI. Exact consumption
 * and canonical reserved fields reject truncated/unknown publications. */
typedef struct { uint8_t *data; size_t used, limit, buffered; bool failed, streaming; int fd,error; } Writer;
#define WRITE_BUFFER_BYTES 65536u
typedef struct { const uint8_t *data; size_t used, length; bool failed; } Reader;
static void stream_bytes(Writer *w, const void *data, size_t n) {
    size_t done=0;
    while(done<n && !w->failed) {
        ssize_t written=write(w->fd,(const uint8_t *)data+done,n-done);
        if(written<0 && errno==EINTR) continue;
        if(written<=0) { w->failed=true; w->error=written<0?errno:EIO; return; }
        done+=(size_t)written;
    }
}
static void flush(Writer *w) {
    stream_bytes(w,w->data,w->buffered); w->buffered=0;
}
static void bytes(Writer *w, const void *data, size_t n) {
    if(w->failed || n>w->limit-w->used) { w->failed=true; return; }
    if(w->streaming) {
        if(n>WRITE_BUFFER_BYTES-w->buffered) flush(w);
        if(n>=WRITE_BUFFER_BYTES) stream_bytes(w,data,n);
        else if(n && !w->failed) { memcpy(w->data+w->buffered,data,n); w->buffered+=n; }
    } else if(w->data && n) memcpy(w->data+w->used,data,n);
    w->used+=n;
}
static void u8(Writer *w, unsigned v) { uint8_t b=(uint8_t)v; bytes(w,&b,1); }
static void u16(Writer *w, unsigned v) { u8(w,v); u8(w,v>>8); }
static void u32(Writer *w, uint32_t v) { for(unsigned i=0;i<4;++i) u8(w,v>>(8*i)); }
static void u64(Writer *w, uint64_t v) { for(unsigned i=0;i<8;++i) u8(w,(unsigned)(v>>(8*i))); }
static void rgb(Writer *w, GhosttyColorRgb c) { u8(w,c.r); u8(w,c.g); u8(w,c.b); }
static void style_color(Writer *w, GhosttyStyleColor c) {
    u8(w,c.tag);
    if(c.tag==GHOSTTY_STYLE_COLOR_RGB) rgb(w,c.value.rgb);
    else { u8(w,c.tag==GHOSTTY_STYLE_COLOR_PALETTE?c.value.palette:0); u16(w,0); }
}
static unsigned style_bits(const GhosttyStyle *s) {
    return s->bold | s->italic<<1 | s->faint<<2 | s->blink<<3 | s->inverse<<4 |
        s->invisible<<5 | s->strikethrough<<6 | s->overline<<7;
}
typedef struct { unsigned kind, x, y, width, height; } ImageDelta;
/* Sorted shallow indexes avoid quadratic lookup when a frame has many images. */
static int index_base(const BtPresentation *base, BtPresentation *index) {
    if(validate(base)) return -1;
    *index=*base; index->images=NULL;
    if(!base->image_count) return 0;
    index->images=malloc(base->image_count*sizeof(*index->images));
    if(!index->images) return -1;
    memcpy(index->images,base->images,base->image_count*sizeof(*index->images));
    qsort(index->images,index->image_count,sizeof(*index->images),image_order);
    return 0;
}
static const BtPresentationImage *base_image(const BtPresentation *base, uint32_t id) {
    if(!base || !base->image_count) return NULL;
    BtPresentationImage key={.id=id};
    return bsearch(&key,base->images,base->image_count,sizeof(*base->images),image_order);
}
static bool same_shape(const BtPresentationImage *a, const BtPresentationImage *b) {
    return a && b && a->width==b->width && a->height==b->height && a->channels==b->channels;
}
static ImageDelta image_delta(const BtPresentationImage *im, const BtPresentationImage *base) {
    ImageDelta d={0};
    if(!same_shape(im,base)) return d;
    if(im->generation==base->generation) { d.kind=1; return d; }
    unsigned x0=im->width,y0=im->height,x1=0,y1=0,c=im->channels;
    size_t stride=(size_t)im->width*c;
    for(unsigned y=0;y<im->height;++y) {
        size_t row=(size_t)y*stride;
        if(!memcmp(im->pixels+row,base->pixels+row,stride)) continue;
        if(y<y0) y0=y;
        y1=y+1;
        for(unsigned x=0;x<im->width;++x) if(memcmp(im->pixels+row+(size_t)x*c,base->pixels+row+(size_t)x*c,c)) {
            if(x<x0) x0=x;
            if(x+1>x1) x1=x+1;
        }
    }
    if(!y1) { d.kind=1; return d; }
    if((uint64_t)(x1-x0)*(y1-y0)*c+16>=im->length) return d;
    return (ImageDelta){.kind=2,.x=x0,.y=y0,.width=x1-x0,.height=y1-y0};
}
static void encode(Writer *w, const BtPresentation *f, uint32_t total, const BtPresentation *base, const ImageDelta *deltas) {
    bytes(w,"BTPRES01",8); u32(w,base?2:1); u32(w,total); u64(w,f->epoch); u64(w,f->revision);
    u32(w,f->cols); u32(w,f->rows); u32(w,f->cell_width); u32(w,f->cell_height); u32(w,f->screen);
    u32(w,(uint32_t)f->cell_count); u32(w,(uint32_t)f->codepoint_count); u32(w,(uint32_t)f->image_count);
    u32(w,(uint32_t)f->placement_count); u32(w,(uint32_t)f->title_length); u32(w,base?2:1);
    if(base) u64(w,base->revision);
    rgb(w,f->colors.background); rgb(w,f->colors.foreground);
    rgb(w,f->colors.cursor_has_value?f->colors.cursor:(GhosttyColorRgb){0}); u8(w,f->colors.cursor_has_value);
    for(unsigned i=0;i<256;++i) rgb(w,f->colors.palette[i]);
    const GhosttyRenderStateCursor *c=&f->cursor;
    u8(w,c->viewport_has_value | (c->viewport_has_value && c->wide_tail)<<1 | c->visible<<2 | c->blinking<<3 | c->password_input<<4);
    u8(w,c->visual_style); u16(w,c->viewport_has_value?c->viewport_x:0); u16(w,c->viewport_has_value?c->viewport_y:0);
    bytes(w,f->title,f->title_length);
    for(size_t i=0;i<f->cell_count;++i) {
        const BtPresentationCell *cell=f->cells+i;
        u32(w,cell->offset); u32(w,cell->length); u8(w,cell->wide); u8(w,cell->selected); u8(w,cell->explicit_background);
        u8(w,style_bits(&cell->style)); u8(w,cell->style.underline);
        style_color(w,cell->style.fg_color); style_color(w,cell->style.bg_color); style_color(w,cell->style.underline_color);
        rgb(w,cell->foreground); rgb(w,cell->background);
    }
    for(size_t i=0;i<f->codepoint_count;++i) u32(w,f->codepoints[i]);
    for(size_t i=0;i<f->image_count;++i) {
        const BtPresentationImage *im=f->images+i;
        u32(w,im->id); u64(w,im->generation); u32(w,im->width); u32(w,im->height); u8(w,im->channels);
        u32(w,(uint32_t)im->length);
        const ImageDelta *d=base?&deltas[i]:NULL;
        if(d) u8(w,d->kind);
        if(!d || !d->kind) bytes(w,im->pixels,im->length);
        else if(d->kind==2) {
            u32(w,d->x); u32(w,d->y); u32(w,d->width); u32(w,d->height);
            for(unsigned y=0;y<d->height;++y)
                bytes(w,im->pixels+((size_t)(d->y+y)*im->width+d->x)*im->channels,(size_t)d->width*im->channels);
        }
    }
    for(size_t i=0;i<f->placement_count;++i) {
        const BtPresentationPlacement *p=f->placements+i;
        const GhosttyKittyGraphicsPlacementRenderInfo *g=&p->geometry;
        u32(w,p->image_index); u32(w,p->image_id); u32(w,p->placement_id); u32(w,p->x_offset); u32(w,p->y_offset); u32(w,(uint32_t)p->z);
        u32(w,g->pixel_width); u32(w,g->pixel_height); u32(w,g->grid_cols); u32(w,g->grid_rows);
        u32(w,(uint32_t)g->viewport_col); u32(w,(uint32_t)g->viewport_row);
        u32(w,g->source_x); u32(w,g->source_y); u32(w,g->source_width); u32(w,g->source_height);
    }
}
int bt_presentation_pack(const BtPresentation *f, uint8_t **out, size_t *length) {
    if(out) *out=NULL;
    if(length) *length=0;
    if(!out || !length) return bad(EINVAL);
    if(validate(f)) return -1;
    Writer measure={.limit=BT_PRESENTATION_MAX_BYTES};
    encode(&measure,f,0,NULL,NULL);
    if(measure.failed) return bad(EFBIG);
    uint8_t *data=malloc(measure.used);
    if(!data) return -1;
    Writer w={.data=data,.limit=measure.used}; encode(&w,f,(uint32_t)measure.used,NULL,NULL);
    if(w.failed || w.used!=measure.used) { free(data); return bad(EIO); }
    *out=data; *length=w.used; return 0;
}
static int pack_fd(const BtPresentation *f, const BtPresentation *base, const ImageDelta *deltas, size_t *length) {
    if(length) *length=0;
    if(!length) return bad(EINVAL);
    if(validate(f)) return -1;
    Writer measure={.limit=BT_PRESENTATION_MAX_BYTES};
    encode(&measure,f,0,base,deltas);
    if(measure.failed) return bad(EFBIG);
    int fd=memfd_create("batty-frame",MFD_CLOEXEC|MFD_ALLOW_SEALING);
    if(fd<0) return -1;
    /* Small semantic fields are buffered; large image payloads go directly
     * from their owned storage to the file. write reports backing exhaustion
     * as an ordinary publication error without mapped-write SIGBUS risk. */
    uint8_t buffer[WRITE_BUFFER_BYTES];
    Writer w={.data=buffer,.limit=measure.used,.streaming=true,.fd=fd};
    encode(&w,f,(uint32_t)measure.used,base,deltas); flush(&w);
    int error=(w.failed || w.used!=measure.used)?(w.error?w.error:EIO):0;
    if(!error && fcntl(fd,F_ADD_SEALS,F_SEAL_WRITE|F_SEAL_GROW|F_SEAL_SHRINK|F_SEAL_SEAL)<0) error=errno;
    if(error) { close(fd); return bad(error); }
    *length=w.used;
    return fd;
}
int bt_presentation_pack_fd(const BtPresentation *f, size_t *length) {
    return pack_fd(f,NULL,NULL,length);
}
int bt_presentation_pack_delta_fd(const BtPresentation *f, const BtPresentation *base, size_t *length) {
    if(length) *length=0;
    if(!length) return bad(EINVAL);
    if(validate(f)) return -1;
    BtPresentation index;
    if(index_base(base,&index)) return -1;
    if(f->epoch!=base->epoch || f->revision<=base->revision) { free(index.images); return bad(EINVAL); }
    ImageDelta *deltas=f->image_count?calloc(f->image_count,sizeof(*deltas)):NULL;
    if(f->image_count && !deltas) { free(index.images); return -1; }
    for(size_t i=0;i<f->image_count;++i) deltas[i]=image_delta(&f->images[i],base_image(&index,f->images[i].id));
    int fd=pack_fd(f,&index,deltas,length),error=errno;
    free(deltas); free(index.images); errno=error; return fd;
}
static void take(Reader *r, void *out, size_t n) {
    if(r->failed || n>r->length-r->used) { r->failed=true; return; }
    if(n) memcpy(out,r->data+r->used,n);
    r->used+=n;
}
static unsigned r8(Reader *r) { uint8_t b=0; take(r,&b,1); return b; }
static unsigned r16(Reader *r) { unsigned a=r8(r), b=r8(r); return a | b<<8; }
static uint32_t r32(Reader *r) { uint32_t n=0; for(unsigned i=0;i<4;++i) n|=(uint32_t)r8(r)<<(8*i); return n; }
static uint64_t r64(Reader *r) { uint64_t n=0; for(unsigned i=0;i<8;++i) n|=(uint64_t)r8(r)<<(8*i); return n; }
static GhosttyColorRgb rrgb(Reader *r) {
    GhosttyColorRgb c; c.r=(uint8_t)r8(r); c.g=(uint8_t)r8(r); c.b=(uint8_t)r8(r); return c;
}
static bool rbool(Reader *r) { unsigned b=r8(r); if(b>1) r->failed=true; return b!=0; }
static GhosttyStyleColor rcolor(Reader *r) {
    GhosttyStyleColor c={0}; c.tag=(GhosttyStyleColorTag)r8(r);
    GhosttyColorRgb data=rrgb(r);
    if(c.tag==GHOSTTY_STYLE_COLOR_RGB) c.value.rgb=data;
    else if(c.tag==GHOSTTY_STYLE_COLOR_PALETTE) { c.value.palette=data.r; if(data.g || data.b) r->failed=true; }
    else if(c.tag!=GHOSTTY_STYLE_COLOR_NONE || data.r || data.g || data.b) r->failed=true;
    return c;
}
static int32_t signed32(uint32_t n) {
    return n<=INT32_MAX?(int32_t)n:-(int32_t)(UINT32_MAX-n)-1;
}
static int unpack(const void *data, size_t length, BtPresentation **out, bool borrowed, const BtPresentation *base) {
    if(out) *out=NULL;
    if(!out || !data) return bad(EINVAL);
    if(length<WIRE_HEADER_BYTES || length>BT_PRESENTATION_MAX_BYTES) return bad(EPROTO);
    Reader r={.data=data,.length=length}; char magic[8]; take(&r,magic,8);
    if(memcmp(magic,"BTPRES01",8) || r32(&r)!=(base?2u:1u) || r32(&r)!=length) return bad(EPROTO);
    BtPresentation *f=calloc(1,sizeof(*f));
    if(!f) return -1;
    f->borrowed_pixels=borrowed;
    int code=EPROTO;
    f->epoch=r64(&r); f->revision=r64(&r); f->cols=r32(&r); f->rows=r32(&r);
    f->cell_width=r32(&r); f->cell_height=r32(&r); f->screen=(GhosttyTerminalScreen)r32(&r);
    f->cell_count=r32(&r); f->codepoint_count=r32(&r);
    size_t image_count=r32(&r), placement_count=r32(&r); f->title_length=r32(&r);
    unsigned kind=r32(&r);
    uint64_t base_revision=base?r64(&r):0;
    if(kind!=(base?2u:1u) || (base && (base_revision!=base->revision || f->epoch!=base->epoch || f->revision<=base_revision)) || !f->epoch || !f->revision || !f->cols || f->cols>1000 ||
       !f->rows || f->rows>1000 || !f->cell_width || f->cell_width>512 ||
       !f->cell_height || f->cell_height>512 || (unsigned)f->screen>GHOSTTY_TERMINAL_SCREEN_ALTERNATE ||
       f->cell_count!=(size_t)f->cols*f->rows || f->codepoint_count>BT_PRESENTATION_MAX_CODEPOINTS ||
       image_count>BT_PRESENTATION_MAX_PLACEMENTS || placement_count>BT_PRESENTATION_MAX_PLACEMENTS ||
       f->title_length>BT_PRESENTATION_MAX_TITLE) goto failed;
    size_t minimum=WIRE_HEADER_BYTES+(base?8:0), memory=sizeof(*f);
    if(!add_size(&minimum,f->title_length) || !add_size(&minimum,f->cell_count*WIRE_CELL_BYTES) ||
       !add_size(&minimum,f->codepoint_count*4) || !add_size(&minimum,image_count*(WIRE_IMAGE_BYTES+(base?1:0))) ||
       !add_size(&minimum,placement_count*WIRE_PLACEMENT_BYTES) || minimum>length ||
       !add_size(&memory,f->title_length+1) || !add_size(&memory,f->cell_count*sizeof(*f->cells)) ||
       !add_size(&memory,f->codepoint_count*sizeof(*f->codepoints)) ||
       !add_size(&memory,image_count*sizeof(*f->images)) ||
       !add_size(&memory,placement_count*sizeof(*f->placements))) goto failed;
    f->colors=(GhosttyRenderStateColors)GHOSTTY_INIT_SIZED(GhosttyRenderStateColors);
    f->colors.background=rrgb(&r); f->colors.foreground=rrgb(&r); f->colors.cursor=rrgb(&r);
    f->colors.cursor_has_value=rbool(&r);
    if(!f->colors.cursor_has_value && (f->colors.cursor.r || f->colors.cursor.g || f->colors.cursor.b)) goto failed;
    for(unsigned i=0;i<256;++i) f->colors.palette[i]=rrgb(&r);
    f->cursor=(GhosttyRenderStateCursor)GHOSTTY_INIT_SIZED(GhosttyRenderStateCursor);
    unsigned flags=r8(&r);
    if(flags&~31u) goto failed;
    f->cursor.viewport_has_value=(flags&1)!=0; f->cursor.wide_tail=(flags&2)!=0;
    f->cursor.visible=(flags&4)!=0; f->cursor.blinking=(flags&8)!=0; f->cursor.password_input=(flags&16)!=0;
    f->cursor.visual_style=(GhosttyRenderStateCursorVisualStyle)r8(&r);
    f->cursor.viewport_x=(uint16_t)r16(&r); f->cursor.viewport_y=(uint16_t)r16(&r);
    if(r.failed || (!f->cursor.viewport_has_value && (f->cursor.viewport_x || f->cursor.viewport_y || f->cursor.wide_tail))) goto failed;
    f->title=malloc(f->title_length+1); f->cells=calloc(f->cell_count,sizeof(*f->cells));
    if(f->codepoint_count) f->codepoints=malloc(f->codepoint_count*sizeof(*f->codepoints));
    if(image_count) f->images=calloc(image_count,sizeof(*f->images));
    if(placement_count) f->placements=calloc(placement_count,sizeof(*f->placements));
    if(!f->title || !f->cells || (f->codepoint_count && !f->codepoints) ||
       (image_count && !f->images) || (placement_count && !f->placements)) { code=ENOMEM; goto failed; }
    take(&r,f->title,f->title_length); f->title[f->title_length]=0;
    for(size_t i=0;i<f->cell_count;++i) {
        BtPresentationCell *c=f->cells+i;
        c->offset=r32(&r); c->length=r32(&r); c->wide=(GhosttyCellWide)r8(&r);
        c->selected=rbool(&r); c->explicit_background=rbool(&r);
        unsigned bits=r8(&r);
        c->style=(GhosttyStyle)GHOSTTY_INIT_SIZED(GhosttyStyle);
        c->style.bold=bits&1; c->style.italic=(bits&2)!=0; c->style.faint=(bits&4)!=0;
        c->style.blink=(bits&8)!=0; c->style.inverse=(bits&16)!=0; c->style.invisible=(bits&32)!=0;
        c->style.strikethrough=(bits&64)!=0; c->style.overline=(bits&128)!=0;
        c->style.underline=(int)r8(&r); c->style.fg_color=rcolor(&r);
        c->style.bg_color=rcolor(&r); c->style.underline_color=rcolor(&r);
        c->foreground=rrgb(&r); c->background=rrgb(&r);
    }
    for(size_t i=0;i<f->codepoint_count;++i) f->codepoints[i]=r32(&r);
    size_t payload=0;
    for(size_t i=0;i<image_count;++i) {
        BtPresentationImage *im=f->images+i;
        im->id=r32(&r); im->generation=r64(&r); im->width=r32(&r); im->height=r32(&r);
        im->channels=r8(&r); im->length=r32(&r);
        unsigned mode=base?r8(&r):0;
        const BtPresentationImage *old=mode?base_image(base,im->id):NULL;
        if(r.failed || !im->id || !im->generation || !im->width || im->width>10000 || !im->height || im->height>10000 ||
           im->channels<1 || im->channels>4 || (uint64_t)im->width*im->height*im->channels!=im->length ||
           im->length>BT_PRESENTATION_MAX_IMAGE_BYTES-payload || mode>2 ||
           (mode && !same_shape(im,old)) || !add_size(&memory,im->length)) goto failed;
        unsigned x=0,y=0,w=im->width,h=im->height;
        if(mode==2) {
            x=r32(&r); y=r32(&r); w=r32(&r); h=r32(&r);
            if(!w || !h || x>=im->width || y>=im->height || w>im->width-x || h>im->height-y) goto failed;
        }
        size_t encoded=mode==1?0:(size_t)w*h*im->channels;
        if(r.failed || encoded>r.length-r.used) goto failed;
        if(base) {
            PixelOwner *owner=mode==1?old->pixel_owner:NULL;
            if(owner) {
                if(owner->references>=SIZE_MAX-1) { code=EOVERFLOW; goto failed; }
                ++owner->references;
            } else {
                owner=malloc(sizeof(*owner)+im->length);
                if(!owner) { code=ENOMEM; goto failed; }
                owner->references=1;
                if(mode) memcpy(owner->bytes,old->pixels,im->length);
            }
            im->pixel_owner=owner; im->pixels=owner->bytes;
            if(mode!=1) for(unsigned row=0;row<h;++row)
                take(&r,im->pixels+((size_t)(y+row)*im->width+x)*im->channels,(size_t)w*im->channels);
        } else if(borrowed) {
            im->pixels=(uint8_t *)r.data+r.used;
            r.used+=im->length;
        } else {
            im->pixels=malloc(im->length);
            if(!im->pixels) { code=ENOMEM; goto failed; }
            take(&r,im->pixels,im->length);
        }
        ++f->image_count; payload+=im->length;
    }
    for(size_t i=0;i<placement_count;++i) {
        BtPresentationPlacement *p=f->placements+i;
        GhosttyKittyGraphicsPlacementRenderInfo *g=&p->geometry;
        *g=(GhosttyKittyGraphicsPlacementRenderInfo)GHOSTTY_INIT_SIZED(GhosttyKittyGraphicsPlacementRenderInfo);
        p->image_index=r32(&r); p->image_id=r32(&r); p->placement_id=r32(&r);
        p->x_offset=r32(&r); p->y_offset=r32(&r); p->z=signed32(r32(&r));
        g->pixel_width=r32(&r); g->pixel_height=r32(&r); g->grid_cols=r32(&r); g->grid_rows=r32(&r);
        g->viewport_col=signed32(r32(&r)); g->viewport_row=signed32(r32(&r));
        g->source_x=r32(&r); g->source_y=r32(&r); g->source_width=r32(&r); g->source_height=r32(&r);
        g->viewport_visible=true; ++f->placement_count;
        if(i && placement_order(p-1,p)>0) goto failed;
    }
    if(r.failed || r.used!=r.length) goto failed;
    if(validate(f)) { if(errno==ENOMEM) code=ENOMEM; goto failed; }
    *out=f; return 0;
failed:
    bt_presentation_free(f); return bad(code);
}

int bt_presentation_unpack(const void *data, size_t length, BtPresentation **out) {
    return unpack(data,length,out,false,NULL);
}
int bt_presentation_unpack_mapping(void *data, size_t length, BtPresentation **out) {
    if(data==MAP_FAILED || !data || !length) {
        if(out) *out=NULL;
        return bad(EINVAL);
    }
    if(unpack(data,length,out,true,NULL)) {
        int error=errno;
        munmap(data,length);
        return bad(error);
    }
    (*out)->mapping=data;
    (*out)->mapping_length=length;
    return 0;
}

int bt_presentation_unpack_delta_mapping(void *data, size_t length, const BtPresentation *base, BtPresentation **out) {
    if(out) *out=NULL;
    if(data==MAP_FAILED || !data || !length) return bad(EINVAL);
    BtPresentation index;
    int rc=index_base(base,&index);
    if(!rc) {
        rc=unpack(data,length,out,false,&index);
        int error=errno; free(index.images); errno=error;
    }
    int error=errno; munmap(data,length); errno=error; return rc;
}
