/* SPDX-License-Identifier: MIT */
#ifndef BATTY_CONTROL_H
#define BATTY_CONTROL_H
#include "workspace.h"
typedef struct BtControl BtControl;
/* Private, same-UID Unix seqpacket endpoint. Never replaces an existing path.
 * Environment is copied for future pane launches. */
BtControl *bt_control_new(BtWorkspace *, const char *path, bool read_only,
                         const char *helper, const char *service, const char *root,
                         const char *terminate_prefix, uint64_t scope_pane, bool host_actions,
                         char *const env[], char *error, size_t capacity);
void bt_control_poll(BtControl *);
void bt_control_free(BtControl *);
#endif
