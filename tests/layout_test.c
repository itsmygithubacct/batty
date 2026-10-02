/* SPDX-License-Identifier: MIT */
#include "layout.h"
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static struct { uint64_t id; BtRect rect; } placed[BT_LAYOUT_PANES];
static unsigned count;
static void require(bool ok, const char *message) {
    if(!ok) { fprintf(stderr,"FAIL layout: %s\n",message); exit(1); }
}
static void visit(void *data, uint64_t id, BtRect r) {
    (void)data;
    require(count<BT_LAYOUT_PANES,"bounded visit count");
    placed[count].id=id; placed[count].rect=r; ++count;
}
static BtRect rectangle(uint64_t id) {
    for(unsigned i=0;i<count;++i) if(placed[i].id==id) return placed[i].rect;
    require(false,"requested pane was placed"); return (BtRect){0};
}
static void check(const BtLayout *l) {
    BtRect area={13,29,1280,960}; count=0;
    require(!bt_layout_place(l,area,9,11,visit,NULL),"place complete tree");
    require(count==l->count,"each pane appears once");
    int64_t total=0;
    for(unsigned i=0;i<count;++i) {
        BtRect a=placed[i].rect;
        require(a.x>=area.x && a.y>=area.y && a.x+a.width<=area.x+area.width &&
                a.y+a.height<=area.y+area.height,"pane stays within window");
        require(a.width>=9 && a.height>=11,"minimum pane dimensions");
        total+=(int64_t)a.width*a.height;
        for(unsigned j=0;j<i;++j) {
            BtRect b=placed[j].rect;
            require(placed[i].id!=placed[j].id,"pane identity is unique");
            require(a.x+a.width<=b.x || b.x+b.width<=a.x ||
                    a.y+a.height<=b.y || b.y+b.height<=a.y,"panes do not overlap");
        }
    }
    require(!count || total==(int64_t)area.width*area.height,"panes exactly cover window");
}
static uint32_t random_state=0x92f72341;
static uint32_t next_random(void) {
    random_state^=random_state<<13; random_state^=random_state>>17; random_state^=random_state<<5;
    return random_state;
}
int main(void) {
    BtLayout l; bt_layout_init(&l);
    require(!bt_layout_split(&l,0,1,BT_RIGHT),"initial pane");
    require(!bt_layout_split(&l,1,2,BT_LEFT),"split left");
    require(!bt_layout_split(&l,1,3,BT_DOWN),"split down");
    check(&l);
    require(rectangle(2).x<rectangle(1).x && rectangle(1).y<rectangle(3).y,"requested split directions");
    BtRect original=rectangle(1);
    require(!bt_layout_adjust(&l,1,false,1200),"grow vertically"); check(&l);
    require(rectangle(1).height>original.height,"resize grows targeted pane");
    BtLayout resized=l;
    bt_layout_reset(&l); check(&l);
    require(rectangle(1).height==original.height,"reset restores half shares");
    for(unsigned i=0;i<BT_LAYOUT_NODES;++i) resized.nodes[i].ratio=l.nodes[i].ratio;
    require(!memcmp(&resized,&l,sizeof(l)),"reset preserves identities and topology");
    require(!bt_layout_swap(&l,1,2),"swap distant panes"); check(&l);
    require(rectangle(1).x<rectangle(2).x,"swap preserves requested identities");
    require(!bt_layout_remove(&l,2),"remove nested pane"); check(&l);
    require(rectangle(3).height==960,"sibling occupies removed split");
    BtLayout before=l;
    require(bt_layout_split(&l,99,4,BT_UP)==-1 && errno==ENOENT,"reject missing target");
    require(!memcmp(&before,&l,sizeof(l)),"failed split preserves layout");
    require(bt_layout_split(&l,1,3,BT_RIGHT)==-1 && errno==EINVAL,"reject duplicate identity");
    count=0;
    require(bt_layout_place(&l,(BtRect){0,0,1,1},9,11,visit,NULL)==-1 && errno==ENOSPC && !count,
            "insufficient geometry calls no visitor");
    require(bt_layout_place(&l,(BtRect){INT_MAX,0,10,10},1,1,visit,NULL)==-1 && errno==EINVAL,
            "reject coordinate overflow");

    uint64_t next_id=4;
    for(unsigned step=0;step<4000;++step) {
        check(&l);
        if(!count) {
            require(!bt_layout_split(&l,0,next_id++,BT_RIGHT),"recreate empty layout"); continue;
        }
        uint64_t id=placed[next_random()%count].id;
        switch(next_random()%4) {
            case 0:
                if(count<BT_LAYOUT_PANES)
                    require(!bt_layout_split(&l,id,next_id++,(BtDirection)(next_random()%4)),"random split");
                break;
            case 1: require(!bt_layout_remove(&l,id),"random close"); break;
            case 2: require(!bt_layout_swap(&l,id,placed[next_random()%count].id),"random move"); break;
            case 3: {
                int rc=bt_layout_adjust(&l,id,next_random()%2,(int)(next_random()%20001)-10000);
                require(!rc || errno==ENOENT,"random resize or missing split axis"); break;
            }
        }
    }
    while(l.count) { check(&l); require(!bt_layout_remove(&l,placed[0].id),"close all panes"); }
    check(&l);
    for(unsigned i=1;i<=BT_LAYOUT_PANES;++i)
        require(!bt_layout_split(&l,i==1?0:i-1,i,BT_RIGHT),"fill bounded layout");
    check(&l); before=l;
    require(bt_layout_split(&l,1,1000,BT_DOWN)==-1 && errno==ENOSPC && !memcmp(&before,&l,sizeof(l)),
            "capacity failure is atomic");
    uint64_t ids[BT_LAYOUT_PANES],ordered[BT_LAYOUT_PANES];
    for(unsigned i=0;i<BT_LAYOUT_PANES;++i) ids[i]=i+1;
    for(unsigned n=1;n<=BT_LAYOUT_PANES;++n) for(unsigned grid=0;grid<2;++grid) {
        require(!bt_layout_arrange(&l,ids,n,grid),"generate automatic arrangement"); check(&l);
        require(bt_layout_order(&l,ordered)==n && !memcmp(ids,ordered,n*sizeof(*ids)),"arrangement preserves complete pane order");
    }
    require(!bt_layout_arrange(&l,ids,4,false),"four-pane tall"); check(&l);
    require(rectangle(1).height==960 && rectangle(1).width==640 &&
            rectangle(2).height>=319 && rectangle(2).height<=321 && rectangle(2).x>rectangle(1).x,
            "tall has full-height master and three right-hand rows");
    require(!bt_layout_arrange(&l,ids,4,true),"four-pane grid"); check(&l);
    require(rectangle(1).height==480 && rectangle(1).width==640 &&
            rectangle(3).x>rectangle(1).x && rectangle(2).y>rectangle(1).y,
            "four-pane grid forms two equal columns and rows");
    before=l; ids[1]=ids[0];
    require(bt_layout_arrange(&l,ids,2,true)==-1 && !memcmp(&before,&l,sizeof(l)),"invalid arrangement is atomic");
    puts("PASS directional splits, resizing, swaps, close collapse and 4000 layout edits without overlap or lost area");
    return 0;
}
