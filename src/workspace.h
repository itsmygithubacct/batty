/* SPDX-License-Identifier: MIT */
#ifndef BATTY_WORKSPACE_H
#define BATTY_WORKSPACE_H
#include "layout.h"
#include "window.h"

enum { BT_WORKSPACE_TABS=16 };
typedef struct BtWorkspace BtWorkspace;
typedef struct {
    const char *helper,*service,*session_dir,*session_name;
    char *const *argv;
    char *const *env;
    bool attach,observe;
    uint64_t expected_epoch; /* Nonzero requires attach to this specific owner. */
} BtPaneLaunch;
typedef struct {
    uint64_t id,tab,window;
    bool active,tab_active,visible,synchronized,disconnected;
    const char *connection_error; /* Retained owner failure; empty while connected. */
    BtRect bounds;
    const char *session_name,*session_dir,*page_title,*title;
    uint64_t session_epoch; /* Zero for local panes. */
    unsigned page_index; /* One-based visible page order. */
    unsigned layout_mode; /* splits, stack, tall, grid */
    BtWindow *view; /* Borrowed; invalidated when the pane closes. */
} BtPaneInfo;
typedef void (*BtPaneVisit)(void *, const BtPaneInfo *);
/* Return true to consume an event as a host action before terminal input. */
typedef bool (*BtWorkspaceFilter)(void *, const SDL_Event *);
typedef struct { uint64_t pane; char name[48]; } BtWorkspaceAction;
/* Chords use SDL key names with Ctrl+, Alt+, Shift+ or Super+ prefixes.
 * Empty action removes a binding. Actions are identifiers, never executable code. */
int bt_workspace_bind(BtWorkspace *, const char *chord, const char *action);
int bt_workspace_bind_sequence(BtWorkspace *, const char *prefix, const char *chord, const char *action);
/* Returns 1 for an action, 0 when empty. Pane is captured at key press time. */
int bt_workspace_action(BtWorkspace *, BtWorkspaceAction *);
/* One coalesced external settings reload; completion is retained for polling. */
int bt_workspace_reload_request(BtWorkspace *, uint64_t *ticket);
int bt_workspace_reload_complete(BtWorkspace *, uint64_t ticket, unsigned status);
int bt_workspace_reload_result(BtWorkspace *, uint64_t ticket, bool *done, unsigned *status);
const char *bt_workspace_page_title(BtWorkspace *, uint64_t pane);
int bt_workspace_rename(BtWorkspace *, uint64_t pane, const char *title);
int bt_workspace_rename_prompt(BtWorkspace *, uint64_t pane);
const char *bt_workspace_pane_title(BtWorkspace *, uint64_t pane);
int bt_workspace_pane_rename(BtWorkspace *, uint64_t pane, const char *title);
int bt_workspace_pane_rename_prompt(BtWorkspace *, uint64_t pane);
int bt_workspace_pane_copy_title(BtWorkspace *, uint64_t pane);
int bt_workspace_pane_clear(BtWorkspace *, uint64_t pane);
int bt_workspace_start_badge(BtWorkspace *, bool enabled);
int bt_workspace_menu(BtWorkspace *);
int bt_workspace_app_menu(BtWorkspace *, const char *name);
int bt_workspace_settings_result(BtWorkspace *, unsigned result);
/* Shared recording/retention choices displayed by the settings overlay.
 * Changing them is a host action; existing owners keep their launch policy. */
int bt_workspace_settings_recording(BtWorkspace *, const char *enabled,
                                    const char *size, const char *graphics,
                                    const char *recent, const char *archive);
/* Bounded printable UTF-8 message; empty string dismisses. PTYs keep pumping. */
int bt_workspace_message(BtWorkspace *, const char *);
const char *bt_workspace_message_text(BtWorkspace *);
/* Modal confirmation: Y queues the named host action for pane; other input
 * stays out of PTYs. Escape, N, Return or a click cancels. */
int bt_workspace_confirm(BtWorkspace *, uint64_t pane, const char *action, const char *text);
int bt_workspace_settings(BtWorkspace *, bool edge_locked);
int bt_workspace_choose(BtWorkspace *, bool pages);
int bt_workspace_pane_center(BtWorkspace *);
typedef struct { char activity[16], process[65], agent[41], task[161]; } BtPaneTelemetry;
/* The host can feed bounded, independently sampled process/agent status into
 * the native Pane Center. Values expire after 3.5 seconds without refresh. */
int bt_workspace_telemetry(BtWorkspace *, uint64_t pane, const char *activity,
                           const char *process, const char *agent, const char *task);
bool bt_workspace_telemetry_get(BtWorkspace *, uint64_t pane, BtPaneTelemetry *);
/* Returns the currently displayed pane IDs, or false when Pane Center is closed. */
bool bt_workspace_center_visible(BtWorkspace *, uint64_t ids[BT_LAYOUT_PANES], unsigned *count);
int bt_workspace_chrome_buttons(BtWorkspace *, unsigned mask);
int bt_workspace_chrome_edge(BtWorkspace *, bool bottom);
int bt_workspace_chrome(BtWorkspace *, bool enabled);
int bt_workspace_font(BtWorkspace *, uint64_t pane, int delta);
int bt_workspace_font_all(BtWorkspace *, int size);
BtWorkspace *bt_workspace_new(const char *title, int width, int height, const char *font,
                               int font_size, char *error, size_t capacity);
void bt_workspace_free(BtWorkspace *);
BtSurface *bt_workspace_surface(BtWorkspace *);
const char *bt_workspace_error(BtWorkspace *);
/* target=0 creates a new tab; otherwise split that pane in the given direction.
 * IDs are stable for the lifetime of the frontend, across layout edits. */
int bt_workspace_add(BtWorkspace *, uint64_t target, BtDirection, const BtPaneLaunch *, uint64_t *id);
int bt_workspace_close(BtWorkspace *, uint64_t id);
int bt_workspace_focus(BtWorkspace *, uint64_t id);
void bt_workspace_raise(BtWorkspace *);
int bt_workspace_neighbor(BtWorkspace *, uint64_t id, BtDirection, uint64_t *neighbor);
int bt_workspace_move(BtWorkspace *, uint64_t id, BtDirection);
/* Place an existing pane on a target pane's edge, preserving its PTY owner. */
int bt_workspace_relocate(BtWorkspace *, uint64_t pane, uint64_t target, BtDirection);
int bt_workspace_resize(BtWorkspace *, uint64_t id, bool horizontal, int delta);
int bt_workspace_layout(BtWorkspace *, uint64_t pane, int mode); /* -1 cycles */
int bt_workspace_reset_sizes(BtWorkspace *, uint64_t pane);
int bt_workspace_resize_mode(BtWorkspace *, uint64_t pane);
int bt_workspace_zoom(BtWorkspace *, uint64_t id);
int bt_workspace_synchronize(BtWorkspace *, uint64_t id, bool enabled);
int bt_workspace_cycle_tab(BtWorkspace *, int delta);
/* Owned value snapshot, independent of the source workspace. This is an API
 * structure, not a disk/wire format. Pane IDs are remapped on application.
 * All current panes must participate exactly once; malformed snapshots and
 * mappings are rejected before changing layout or input state. */
typedef struct {
    BtLayout splits,tall,grid;
    uint64_t active,zoom,previous;
    unsigned mode;
    char title[256];
} BtWorkspacePageLayout;
typedef struct {
    unsigned version,page_count,pane_count,active_page;
    int previous_page; /* -1 when no previous page remains. */
    BtWorkspacePageLayout pages[BT_WORKSPACE_TABS];
    struct { uint64_t id; int font_size; bool synchronized; char title[256]; } panes[BT_LAYOUT_PANES];
} BtWorkspaceLayout;
typedef struct { uint64_t saved,current; } BtPaneMapping;
int bt_workspace_layout_capture(BtWorkspace *, BtWorkspaceLayout *);
/* Font is borrowed until workspace disposal; dimensions are logical SDL pixels. */
typedef struct {
    const char *font;
    int width,height,font_size;
    bool chrome,bottom_bar,start_badge;
    unsigned pane_buttons;
} BtWorkspaceAppearance;
BtWorkspaceAppearance bt_workspace_appearance(BtWorkspace *);
int bt_workspace_layout_validate(const BtWorkspaceLayout *);
/* Keep a nonempty subset of panes, collapse their removed split siblings,
 * drop empty pages and repair focus. Failure leaves the snapshot unchanged. */
int bt_workspace_layout_prune(BtWorkspaceLayout *, const uint64_t *, unsigned count);
/* BWL2 adds pane title overrides to BWL1. Both are portable little-endian,
 * without struct bytes/padding, pointers, commands or session data. Outputs
 * are malloc-owned; failures leave outputs NULL/zero. Decode validates the
 * complete value independently of any live workspace or attachments. */
enum { BT_WORKSPACE_LAYOUT_MAX_BYTES=32768 };
int bt_workspace_layout_pack(const BtWorkspaceLayout *, uint8_t **, size_t *);
int bt_workspace_layout_unpack(const void *, size_t, BtWorkspaceLayout **);
int bt_workspace_layout_apply(BtWorkspace *, const BtWorkspaceLayout *,
                               const BtPaneMapping *, unsigned count);
typedef enum { BT_NEXT_PANE, BT_PREVIOUS_PANE, BT_LAST_PANE, BT_LAST_PAGE,
               BT_PAGE_NUMBER, BT_SWAP_NEXT, BT_SWAP_PREVIOUS } BtNavigation;
int bt_workspace_navigate(BtWorkspace *, BtNavigation, unsigned number);
int bt_workspace_close_tab(BtWorkspace *, uint64_t pane);
uint64_t bt_workspace_active(BtWorkspace *);
void bt_workspace_visit(BtWorkspace *, BtPaneVisit, void *);
void bt_workspace_filter(BtWorkspace *, BtWorkspaceFilter, void *);
/* The host can use this entry point with its own event loop. */
int bt_workspace_event(BtWorkspace *, const SDL_Event *);
int bt_workspace_draw(BtWorkspace *, bool present);
/* Bounded aggregate GPU texture budget across the pane renderers in one
 * workspace. Eviction preserves frames and never changes PTY geometry. */
int bt_workspace_image_budget(BtWorkspace *, size_t bytes);
size_t bt_workspace_image_cache_bytes(BtWorkspace *);
/* Process-wide cap across all workspace textures and their CPU pixel shadows.
 * A retained presentation recreates an evicted image on the next draw. */
int bt_workspace_process_image_budget(BtWorkspace *, size_t bytes);
size_t bt_workspace_process_image_cache_bytes(void);
typedef struct {
    uint64_t passes, budget_yields, last_io_ms, max_io_ms;
    unsigned last_panes,pending_detaches;
    uint64_t completed_detaches,failed_detaches;
} BtWorkspacePumpStats;
BtWorkspacePumpStats bt_workspace_pump_stats(BtWorkspace *);
int bt_workspace_pump(BtWorkspace *, int timeout_ms);
bool bt_workspace_closed(BtWorkspace *);
int bt_workspace_listen(BtWorkspace *, const char *path, bool read_only,
                        const char *helper, const char *service, const char *root, char *const env[]);
/* Optional generated-session prefix: control close terminates matching owners
 * whose names append 24 lowercase hex digits. NULL keeps detach semantics. */
int bt_workspace_listen_policy(BtWorkspace *, const char *path, bool read_only,
                               const char *helper, const char *service, const char *root,
                               const char *terminate_prefix, bool host_actions, char *const env[]);
/* A second endpoint may be restricted to one current pane. Its read-only mode
 * permits metadata/text/events; writable mode additionally permits send/paste. */
int bt_workspace_listen_scope(BtWorkspace *, const char *path, bool read_only,
                              const char *helper, const char *service, const char *root,
                              uint64_t pane, char *const env[]);
#endif
