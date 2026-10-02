/* SPDX-License-Identifier: MIT */
#ifndef BATTY_LAYOUT_H
#define BATTY_LAYOUT_H
#include <stdbool.h>
#include <stdint.h>

enum { BT_LAYOUT_PANES=64, BT_LAYOUT_NODES=2*BT_LAYOUT_PANES-1 };
typedef enum { BT_LEFT, BT_RIGHT, BT_UP, BT_DOWN } BtDirection;
typedef struct { int x,y,width,height; } BtRect;
typedef struct {
    uint64_t pane;
    int parent,first,second;
    unsigned ratio; /* first child's share, in ten-thousandths */
    bool used,horizontal;
} BtLayoutNode;
typedef struct {
    BtLayoutNode nodes[BT_LAYOUT_NODES];
    int root;
    unsigned count;
} BtLayout;
typedef void (*BtLayoutVisit)(void *, uint64_t, BtRect);
void bt_layout_init(BtLayout *);
/* Validate every node, edge, parent, ratio and unique leaf ID before traversing
 * a layout supplied by a caller. Reject cycles, shared or unreachable nodes. */
int bt_layout_validate(const BtLayout *);
/* IDs must be nonzero and unique. First insertion uses target=0; subsequent
 * insertions split target. Failed edits leave the tree unchanged. */
int bt_layout_split(BtLayout *, uint64_t target, uint64_t pane, BtDirection);
int bt_layout_remove(BtLayout *, uint64_t pane);
int bt_layout_swap(BtLayout *, uint64_t, uint64_t);
/* Adjust the nearest matching split, increasing this pane's share. */
int bt_layout_adjust(BtLayout *, uint64_t pane, bool horizontal, int delta);
/* Restore every split to half shares without changing topology or IDs. */
void bt_layout_reset(BtLayout *);
unsigned bt_layout_order(const BtLayout *, uint64_t ids[BT_LAYOUT_PANES]);
/* Build tall (master plus right column) or grid geometry atomically. */
int bt_layout_arrange(BtLayout *, const uint64_t *, unsigned count, bool grid);
int bt_layout_minimum(const BtLayout *, int cell_width, int cell_height, int *, int *);
/* Require the complete minimum geometry before calling any visitor. */
int bt_layout_place(const BtLayout *, BtRect, int minimum_width, int minimum_height,
                    BtLayoutVisit, void *);
#endif
