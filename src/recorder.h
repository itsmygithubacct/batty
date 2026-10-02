/* SPDX-License-Identifier: MIT */
#ifndef BATTY_RECORDER_H
#define BATTY_RECORDER_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>

typedef struct {
    int fd,error;
    pid_t worker;
    bool ending,complete;
    uint64_t bytes;
    char name[49];
    char *directory;
} BtRecorder;
/* NULL with errno=0 means disabled; NULL with errno set means allocation failed.
 * Other errors are retained on the recorder and belong to recording alone. */
BtRecorder *bt_recorder_open(const char *helper, char *const env[], pid_t child, char *const argv[]);
void bt_recorder_write(BtRecorder *, const void *, size_t);
void bt_recorder_pump(BtRecorder *, bool eof);
void bt_recorder_close(BtRecorder *);
#endif
