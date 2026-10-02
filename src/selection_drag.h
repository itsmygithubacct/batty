/* SPDX-License-Identifier: MIT */
#ifndef BATTY_SELECTION_DRAG_H
#define BATTY_SELECTION_DRAG_H
#include <ghostty/vt.h>
#include <ghostty/vt/grid_ref_tracked.h>
#include <stdbool.h>
#include <stdint.h>

/* A tracked anchor stays on its original cell while the viewport moves.
 * Both local views and persistent owners use this state for pointer selection. */
typedef struct {
    GhosttyTrackedGridRef anchor;
    int x, y, direction;
    uint64_t next_tick;
    bool active, dragged;
} BtSelectionDrag;

static inline void bt_selection_drag_reset(BtSelectionDrag *drag) {
    ghostty_tracked_grid_ref_free(drag->anchor);
    *drag=(BtSelectionDrag){0};
}
static inline int bt_selection_drag_apply(BtSelectionDrag *drag, GhosttyTerminal terminal,
                                   unsigned cols, unsigned rows, unsigned cw, unsigned ch,
                                   int padding) {
    if(!drag->active || !terminal || !cols || !rows || !cw || !ch) return 0;
    int x=drag->x-padding, y=drag->y-padding;
    unsigned col=x<0?0:(unsigned)x/cw, row=y<0?0:(unsigned)y/ch;
    if(col>=cols) col=cols-1;
    if(row>=rows) row=rows-1;
    GhosttySelection selection=GHOSTTY_INIT_SIZED(GhosttySelection);
    selection.start=(GhosttyGridRef)GHOSTTY_INIT_SIZED(GhosttyGridRef);
    selection.end=(GhosttyGridRef)GHOSTTY_INIT_SIZED(GhosttyGridRef);
    GhosttyResult anchor=ghostty_tracked_grid_ref_snapshot(drag->anchor,&selection.start);
    if(anchor==GHOSTTY_NO_VALUE) { bt_selection_drag_reset(drag); return 0; }
    if(anchor!=GHOSTTY_SUCCESS) return -1;
    GhosttyPoint point={.tag=GHOSTTY_POINT_TAG_VIEWPORT,.value.coordinate={(uint16_t)col,(uint16_t)row}};
    if(ghostty_terminal_grid_ref(terminal,point,&selection.end)!=GHOSTTY_SUCCESS ||
       ghostty_terminal_set(terminal,GHOSTTY_TERMINAL_OPT_SELECTION,&selection)!=GHOSTTY_SUCCESS) return -1;
    return 1;
}
static inline int bt_selection_drag_event(BtSelectionDrag *drag, GhosttyTerminal terminal,
                                   unsigned cols, unsigned rows, unsigned cw, unsigned ch,
                                   int padding, int x, int y, bool press, bool release, uint64_t now) {
    if(!terminal || !cols || !rows || !cw || !ch) return 0;
    if(press) {
        bt_selection_drag_reset(drag);
        int px=x-padding, py=y-padding;
        unsigned col=px<0?0:(unsigned)px/cw, row=py<0?0:(unsigned)py/ch;
        if(col>=cols) col=cols-1;
        if(row>=rows) row=rows-1;
        GhosttyPoint point={.tag=GHOSTTY_POINT_TAG_VIEWPORT,.value.coordinate={(uint16_t)col,(uint16_t)row}};
        if(ghostty_terminal_grid_ref_track(terminal,point,&drag->anchor)!=GHOSTTY_SUCCESS) return -1;
        drag->active=true;
    }
    if(!drag->active) return 0;
    drag->x=x; drag->y=y;
    if(!press && !release) drag->dragged=true;
    int direction=drag->dragged && !release ?
        (y<padding?-1:y>=padding+(int)(rows*ch)?1:0):0;
    if(direction!=drag->direction) drag->next_tick=now+50;
    drag->direction=direction;
    int result=bt_selection_drag_apply(drag,terminal,cols,rows,cw,ch,padding);
    if(release) bt_selection_drag_reset(drag);
    return result;
}
static inline int bt_selection_drag_tick(BtSelectionDrag *drag, GhosttyTerminal terminal,
                                  unsigned cols, unsigned rows, unsigned cw, unsigned ch,
                                  int padding, uint64_t now) {
    if(!drag->active || !drag->direction || now<drag->next_tick) return 0;
    drag->next_tick=now+50;
    GhosttyTerminalScrollViewport scroll={.tag=GHOSTTY_SCROLL_VIEWPORT_DELTA,
                                          .value.delta=drag->direction};
    ghostty_terminal_scroll_viewport(terminal,scroll);
    return bt_selection_drag_apply(drag,terminal,cols,rows,cw,ch,padding);
}
#endif
