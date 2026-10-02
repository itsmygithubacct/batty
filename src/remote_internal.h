/* SPDX-License-Identifier: MIT */
#ifndef BATTY_REMOTE_INTERNAL_H
#define BATTY_REMOTE_INTERNAL_H
#include "remote.h"
#include <stddef.h>
#include <sys/stat.h>

#define BT_WIRE_MAGIC 0x42545953u
#define BT_WIRE_VERSION 1u
#define BT_WIRE_CHUNK 16384u
enum { BT_WIRE_HELLO=1, BT_WIRE_FRAME, BT_WIRE_SEND, BT_WIRE_INTENT,
       BT_WIRE_RESIZE, BT_WIRE_TEXT, BT_WIRE_STATUS, BT_WIRE_STOP, BT_WIRE_READY, BT_WIRE_CLIPBOARD,
       BT_WIRE_RESET, BT_WIRE_RECOVERY };
enum { BT_STOP_RECOVERY_FAILED=1 }; /* STOP reply flags; owner remains alive. */
enum { BT_STATE_EXITED=1, BT_STATE_EOF=2, BT_STATE_DONE=4,
       BT_STATE_CLIPBOARD_PENDING=8, BT_STATE_CLIPBOARD_SUPPORTED=16,
       BT_STATE_DELTA_SUPPORTED=32, BT_STATE_DELTA_FRAME=64,
       BT_STATE_RECORD_SHIFT=7, BT_STATE_RECORD_MASK=7<<BT_STATE_RECORD_SHIFT,
       BT_STATE_UPSTREAM_DISCONNECTED=1<<10 };
/* Fixed-width local protocol. Both processes run on this Linux host. Frame
 * bodies use a separate endian-stable semantic codec. No pointers are sent. */
typedef struct {
    uint64_t request, revision, epoch, bytes_read, bytes_written, pending, blob_size;
    uint32_t magic, version, type, flags, error, cols, rows, cw, ch, child;
    uint32_t exit_status, state, controllers, observers, length, reserved;
    uint8_t data[BT_WIRE_CHUNK];
} BtPacket;
#define BT_WIRE_HEADER offsetof(BtPacket, data)
_Static_assert(BT_WIRE_HEADER == 120, "local wire header layout");

int bt_wire_root(const char *path, bool create);
bool bt_wire_name(const char *name);
int bt_wire_address(int root, const char *name, char *out, size_t capacity);
int bt_wire_connect(const char *root, const char *name);
int bt_wire_peer(int fd);
int bt_wire_send(int fd, const BtPacket *, int passed_fd);
/* 1=packet, 0=EOF, -1=errno. Always closes unexpected received descriptors. */
int bt_wire_receive(int fd, BtPacket *, int *passed_fd);
int bt_wire_wait(int fd, short events, uint64_t deadline);
int bt_wire_blob(const void *, size_t);
void *bt_wire_map(int fd, size_t length, size_t maximum);
void bt_wire_packet(BtPacket *, uint32_t type);
int bt_wire_error(char *, size_t, const char *, int error);
#endif
