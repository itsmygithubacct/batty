/* SPDX-License-Identifier: MIT */
#ifndef BATTY_PRESENTATION_H
#define BATTY_PRESENTATION_H
#include <ghostty/vt.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define BT_PRESENTATION_MAX_BYTES (128u * 1024u * 1024u)
#define BT_PRESENTATION_MAX_IMAGE_BYTES (64u * 1024u * 1024u)
#define BT_PRESENTATION_MAX_PLACEMENTS 16384u
#define BT_PRESENTATION_MAX_CODEPOINTS (4u * 1024u * 1024u)
#define BT_PRESENTATION_MAX_TITLE 65536u

typedef struct {
    uint32_t offset, length;
    GhosttyStyle style;
    GhosttyColorRgb foreground, background;
    GhosttyCellWide wide;
    bool selected, explicit_background;
} BtPresentationCell;

typedef struct {
    uint32_t id, width, height;
    uint64_t generation;
    unsigned channels; /* 1=gray, 2=gray+alpha, 3=RGB, 4=RGBA; straight alpha. */
    size_t length;
    uint8_t *pixels;
} BtPresentationImage;

typedef struct {
    uint32_t image_index, image_id, placement_id, x_offset, y_offset;
    int32_t z;
    GhosttyKittyGraphicsPlacementRenderInfo geometry;
} BtPresentationPlacement;

/* A complete immutable viewport once published. Every array/string is owned.
 * Ghostty structs below contain semantic values only; the wire codec encodes
 * fields explicitly and never copies their ABI layout or any native handles. */
typedef struct BtPresentation {
    uint64_t epoch, revision;
    unsigned cols, rows, cell_width, cell_height;
    GhosttyTerminalScreen screen;
    GhosttyRenderStateColors colors;
    GhosttyRenderStateCursor cursor;
    char *title;
    size_t title_length, cell_count, codepoint_count, image_count, placement_count;
    BtPresentationCell *cells; /* Row-major, exactly cols * rows entries. */
    uint32_t *codepoints;
    BtPresentationImage *images;
    BtPresentationPlacement *placements; /* Full replacement list, sorted by z. */
} BtPresentation;

typedef struct BtPresenter BtPresenter;
/* Borrows terminal. This must be its only render-state consumer. The caller
 * excludes terminal mutation for the entire capture call. */
BtPresenter *bt_presenter_new(GhosttyTerminal terminal);
void bt_presenter_free(BtPresenter *);
/* 0=new owned complete frame, 1=unchanged, -1=errno. *out is NULL on 1/-1.
 * epoch/revision must be nonzero. revision is supplied by the state owner.
 * Cell metrics must match those already applied with terminal_resize. */
int bt_presenter_capture(BtPresenter *, uint64_t epoch, uint64_t revision,
                         unsigned cell_width, unsigned cell_height, bool force,
                         BtPresentation **out);
const char *bt_presenter_error(const BtPresenter *);
void bt_presentation_free(BtPresentation *);
/* Versioned, little-endian, bounded complete-frame codec. 0=success/-1=errno.
 * Pack allocates *out (free it); unpack owns all decoded arrays. Both clear
 * output parameters on failure and reject invalid frames before publication. */
int bt_presentation_pack(const BtPresentation *, uint8_t **out, size_t *length);
int bt_presentation_unpack(const void *, size_t length, BtPresentation **out);
#endif
