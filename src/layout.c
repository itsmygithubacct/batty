/* SPDX-License-Identifier: MIT */
#include "layout.h"
#include <errno.h>
#include <limits.h>
#include <string.h>

static int failed(int error) { errno=error; return -1; }
void bt_layout_init(BtLayout *layout) {
    memset(layout,0,sizeof(*layout)); layout->root=-1;
}
static int validate_node(const BtLayout *l, int at, int parent, bool *seen,
                          uint64_t *ids, unsigned *count) {
    if(at<0 || at>=BT_LAYOUT_NODES || seen[at]) return -1;
    const BtLayoutNode *n=&l->nodes[at];
    if(!n->used || n->parent!=parent) return -1;
    seen[at]=true;
    if(n->pane) {
        if(n->first!=-1 || n->second!=-1 || *count==BT_LAYOUT_PANES) return -1;
        for(unsigned i=0;i<*count;++i) if(ids[i]==n->pane) return -1;
        ids[(*count)++]=n->pane; return 0;
    }
    if(n->ratio<1 || n->ratio>9999) return -1;
    return validate_node(l,n->first,at,seen,ids,count) ||
        validate_node(l,n->second,at,seen,ids,count)?-1:0;
}
int bt_layout_validate(const BtLayout *l) {
    if(!l || l->count>BT_LAYOUT_PANES) return failed(EINVAL);
    bool seen[BT_LAYOUT_NODES]={0};
    uint64_t ids[BT_LAYOUT_PANES]; unsigned count=0;
    if(l->count ? validate_node(l,l->root,-1,seen,ids,&count) : l->root!=-1)
        return failed(EINVAL);
    if(count!=l->count) return failed(EINVAL);
    for(unsigned i=0;i<BT_LAYOUT_NODES;++i)
        if(l->nodes[i].used!=seen[i]) return failed(EINVAL);
    return 0;
}
static int find(const BtLayout *layout, uint64_t id) {
    for(int i=0;i<BT_LAYOUT_NODES;++i)
        if(layout->nodes[i].used && layout->nodes[i].pane==id) return i;
    return -1;
}
static BtLayoutNode leaf(uint64_t pane, int parent) {
    return (BtLayoutNode){.used=true,.pane=pane,.parent=parent,.first=-1,.second=-1};
}
int bt_layout_split(BtLayout *l, uint64_t target, uint64_t pane, BtDirection direction) {
    if(!pane || direction<BT_LEFT || direction>BT_DOWN || find(l,pane)>=0) return failed(EINVAL);
    if(l->count==BT_LAYOUT_PANES) return failed(ENOSPC);
    if(!l->count) {
        if(target) return failed(ENOENT);
        l->nodes[0]=leaf(pane,-1); l->root=0; l->count=1; return 0;
    }
    int at=find(l,target), first=-1,second=-1;
    if(!target || at<0) return failed(ENOENT);
    for(int i=0;i<BT_LAYOUT_NODES;++i) if(!l->nodes[i].used) {
        if(first<0) first=i;
        else { second=i; break; }
    }
    if(second<0) return failed(ENOSPC);
    bool before=direction==BT_LEFT || direction==BT_UP;
    l->nodes[first]=leaf(before?pane:target,at);
    l->nodes[second]=leaf(before?target:pane,at);
    l->nodes[at]=(BtLayoutNode){.used=true,.parent=l->nodes[at].parent,
        .first=first,.second=second,.ratio=5000,.horizontal=direction==BT_LEFT || direction==BT_RIGHT};
    ++l->count; return 0;
}
int bt_layout_remove(BtLayout *l, uint64_t pane) {
    int at=pane?find(l,pane):-1;
    if(at<0) return failed(ENOENT);
    int parent=l->nodes[at].parent;
    if(parent<0) { bt_layout_init(l); return 0; }
    BtLayoutNode *p=&l->nodes[parent];
    int sibling=p->first==at?p->second:p->first;
    int grand=p->parent;
    *p=l->nodes[sibling]; p->parent=grand;
    if(!p->pane) { l->nodes[p->first].parent=parent; l->nodes[p->second].parent=parent; }
    memset(&l->nodes[sibling],0,sizeof(l->nodes[sibling]));
    memset(&l->nodes[at],0,sizeof(l->nodes[at]));
    --l->count; return 0;
}
int bt_layout_swap(BtLayout *l, uint64_t first, uint64_t second) {
    int a=first?find(l,first):-1,b=second?find(l,second):-1;
    if(a<0 || b<0) return failed(ENOENT);
    l->nodes[a].pane=second; l->nodes[b].pane=first; return 0;
}
int bt_layout_adjust(BtLayout *l, uint64_t pane, bool horizontal, int delta) {
    int at=pane?find(l,pane):-1;
    if(at<0) return failed(ENOENT);
    while(l->nodes[at].parent>=0) {
        int parent=l->nodes[at].parent;
        BtLayoutNode *p=&l->nodes[parent];
        if(p->horizontal==horizontal) {
            int64_t ratio=(int64_t)p->ratio+(p->first==at?(int64_t)delta:-(int64_t)delta);
            p->ratio=(unsigned)(ratio<1?1:ratio>9999?9999:ratio);
            return 0;
        }
        at=parent;
    }
    return failed(ENOENT);
}
void bt_layout_reset(BtLayout *l) {
    for(unsigned i=0;i<BT_LAYOUT_NODES;++i)
        if(l->nodes[i].used && !l->nodes[i].pane) l->nodes[i].ratio=5000;
}
static void order(const BtLayout *l, int at, uint64_t *ids, unsigned *count) {
    if(at<0) return;
    const BtLayoutNode *n=&l->nodes[at];
    if(n->pane) ids[(*count)++]=n->pane;
    else { order(l,n->first,ids,count); order(l,n->second,ids,count); }
}
unsigned bt_layout_order(const BtLayout *l, uint64_t ids[BT_LAYOUT_PANES]) {
    unsigned count=0; order(l,l->root,ids,&count); return count;
}
static int arranged_nodes(BtLayout *l, const uint64_t *ids, const unsigned *rows,
                          unsigned columns, int parent, unsigned *used) {
    int at=(int)(*used)++;
    unsigned first=rows[0];
    if(columns==1 && first==1) { l->nodes[at]=leaf(*ids,parent); return at; }
    bool horizontal=columns>1;
    unsigned remaining=first-1;
    l->nodes[at]=(BtLayoutNode){.used=true,.parent=parent,.horizontal=horizontal,
        .ratio=10000/(horizontal?columns:first)};
    l->nodes[at].first=arranged_nodes(l,ids,horizontal?rows:&(unsigned){1},1,at,used);
    l->nodes[at].second=arranged_nodes(l,ids+(horizontal?first:1),
        horizontal?rows+1:&remaining,horizontal?columns-1:1,at,used);
    return at;
}
int bt_layout_arrange(BtLayout *l, const uint64_t *ids, unsigned count, bool grid) {
    if(count>BT_LAYOUT_PANES || (count && !ids)) return failed(EINVAL);
    for(unsigned i=0;i<count;++i) {
        if(!ids[i]) return failed(EINVAL);
        for(unsigned j=0;j<i;++j) if(ids[i]==ids[j]) return failed(EINVAL);
    }
    BtLayout result; bt_layout_init(&result);
    if(count) {
        unsigned columns=count==1?1:2,rows[BT_LAYOUT_PANES]={0},used=0;
        if(grid) {
            if(count>5) { columns=3; while(columns*columns<count) ++columns; }
            for(unsigned i=0;i<columns;++i) rows[i]=count/columns;
            rows[columns-1]+=count%columns;
        } else { rows[0]=1; if(count>1) rows[1]=count-1; }
        result.root=arranged_nodes(&result,ids,rows,columns,-1,&used);
        result.count=count;
    }
    *l=result; return 0;
}
static void minimum(const BtLayout *l, int at, int *width, int *height) {
    const BtLayoutNode *n=&l->nodes[at];
    if(n->pane) { *width=*height=1; return; }
    int aw,ah,bw,bh;
    minimum(l,n->first,&aw,&ah); minimum(l,n->second,&bw,&bh);
    *width=n->horizontal?aw+bw:(aw>bw?aw:bw);
    *height=n->horizontal?(ah>bh?ah:bh):ah+bh;
}
int bt_layout_minimum(const BtLayout *l, int cw, int ch, int *width, int *height) {
    *width=*height=0;
    if(cw<1 || ch<1 || cw>INT_MAX/BT_LAYOUT_PANES || ch>INT_MAX/BT_LAYOUT_PANES)
        return failed(EINVAL);
    if(l->root<0) return 0;
    minimum(l,l->root,width,height);
    *width*=cw; *height*=ch;
    return 0;
}
static void place(const BtLayout *l, int at, BtRect r, int cw, int ch, BtLayoutVisit visit, void *data) {
    const BtLayoutNode *n=&l->nodes[at];
    if(n->pane) { visit(data,n->pane,r); return; }
    int aw,ah,bw,bh;
    minimum(l,n->first,&aw,&ah); minimum(l,n->second,&bw,&bh);
    int span=n->horizontal?r.width:r.height;
    int first_min=n->horizontal?aw*cw:ah*ch, second_min=n->horizontal?bw*cw:bh*ch;
    int cut=(int)((int64_t)span*n->ratio/10000);
    if(cut<first_min) cut=first_min;
    if(cut>span-second_min) cut=span-second_min;
    BtRect a=r,b=r;
    if(n->horizontal) { a.width=cut; b.x+=cut; b.width-=cut; }
    else { a.height=cut; b.y+=cut; b.height-=cut; }
    place(l,n->first,a,cw,ch,visit,data); place(l,n->second,b,cw,ch,visit,data);
}
int bt_layout_place(const BtLayout *l, BtRect r, int cw, int ch, BtLayoutVisit visit, void *data) {
    if(!visit || cw<1 || ch<1 || cw>INT_MAX/BT_LAYOUT_PANES || ch>INT_MAX/BT_LAYOUT_PANES ||
       r.x<0 || r.y<0 || r.width<0 || r.height<0 || r.x>INT_MAX-r.width || r.y>INT_MAX-r.height)
        return failed(EINVAL);
    if(l->root<0) return 0;
    int width,height;
    if(bt_layout_minimum(l,cw,ch,&width,&height)) return -1;
    if(r.width<width || r.height<height) return failed(ENOSPC);
    place(l,l->root,r,cw,ch,visit,data); return 0;
}
