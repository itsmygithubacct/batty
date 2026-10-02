/* SPDX-License-Identifier: MIT */
#ifndef BATTY_RECOVERY_H
#define BATTY_RECOVERY_H
#include "session.h"
#include "presentation.h"
/* Private, bounded archive: styled text including scrollback, a complete
 * static graphics frame, and the original launch arguments and current cwd. */
int bt_recovery_capture(BtSession *, const BtPresentation *, char *const argv[],
                        uint8_t **out, size_t *length);
/* Load into the parser before a new PTY exists. Never replay into shell input. */
int bt_recovery_load(BtSession *, const char *path);
/* Persist deliberate closure before terminating an owner. An unset directory
 * needs no marker. Failure leaves the owner responsible for staying alive. */
int bt_recovery_forget(const char *directory, uint64_t epoch);
#endif
