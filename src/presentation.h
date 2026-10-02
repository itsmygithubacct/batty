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
    void *pixel_owner; /* Private immutable shared-storage ownership, or NULL. */
} BtPresentationImage;

typedef struct {
    uint32_t image_index, image_id, placement_id, x_offset, y_offset;
    int32_t z;
    GhosttyKittyGraphicsPlacementRenderInfo geometry;
} BtPresentationPlacement;

/* A complete immutable viewport once published. Arrays/strings are owned;
 * mapped presentations retain read-only image pixels in their owned mapping.
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
    /* Private ownership bookkeeping; callers must not modify these fields. */
    bool borrowed_pixels;
    void *mapping;
    size_t mapping_length;
} BtPresentation;

typedef struct {
    int fd;
    size_t length;
    uint64_t epoch, revision;
    unsigned cols, rows, cell_width, cell_height;
} BtPresentationFile;
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
/* Capture and serialize synchronously while terminal mutation is excluded.
 * Image pixels are borrowed only within this call, never returned to callers.
 * 0=owned sealed file, 1=unchanged, -1=errno; fd=-1 on 1/-1. A publication
 * failure forces the next capture to retry even without new terminal damage. */
int bt_presenter_capture_file(BtPresenter *, uint64_t epoch, uint64_t revision,
                              unsigned cell_width, unsigned cell_height, bool force,
                              BtPresentationFile *out);
const char *bt_presenter_error(const BtPresenter *);
void bt_presentation_free(BtPresentation *);
/* Versioned, little-endian, bounded complete-frame codec. 0=success/-1=errno.
 * Pack allocates *out (free it); unpack owns all decoded arrays. Both clear
 * output parameters on failure and reject invalid frames before publication. */
int bt_presentation_pack(const BtPresentation *, uint8_t **out, size_t *length);
/* Encode directly into a sealed, close-on-exec memory file. Returns an owned
 * fd, or -1/errno with *length=0. No intermediate serialized heap buffer. */
int bt_presentation_pack_fd(const BtPresentation *, size_t *length);
/* Incremental image codec: complete text/placement metadata, image references
 * and changed rectangles against one exact earlier revision in the same epoch.
 * Input frames remain immutable. Full-frame version 1 stays independent.
 * Delta decoding owns/shares individual images, never retains a base frame or
 * its mapping. The first reference to mapped pixels copies that image once. */
int bt_presentation_pack_delta_fd(const BtPresentation *, const BtPresentation *base, size_t *length);
/* Consumes the sealed input mapping on every path, including wrong-base errors.
 * Reconstructed pixels outlive both the mapping and the supplied base. */
int bt_presentation_unpack_delta_mapping(void *, size_t length, const BtPresentation *base,
                                         BtPresentation **out);
int bt_presentation_unpack(const void *, size_t length, BtPresentation **out);
/* Consumes an mmap mapping on success and failure. Caller must first validate
 * its exact size and immutable backing (including shrink/write seals). Image
 * bytes remain read-only in that mapping until presentation_free. */
int bt_presentation_unpack_mapping(void *, size_t length, BtPresentation **out);
#endif
