/* SPDX-License-Identifier: MIT */
#ifndef BATTY_TRANSCRIPT_FILTER_H
#define BATTY_TRANSCRIPT_FILTER_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef int (*BtTranscriptSink)(void *, const void *, size_t);
/* A bounded filter for raw PTY output, not a terminal parser or input recorder.
 * Elide seven- and eight-bit Kitty APC graphics and Sixel DCS strings; keep other bytes.
 * DCS headers over 64 bytes are elided conservatively until their terminator.
 * UTF-8 bytes and other terminal controls pass unchanged. Sink failure is
 * terminal: do not retry a partially delivered stream. No heap allocation. */
typedef struct {
    BtTranscriptSink sink;
    void *userdata;
    uint64_t input_bytes,output_bytes,elided_bytes,graphics_sequences;
    unsigned state,header_length,utf8_continuations;
    uint8_t utf8_min,utf8_max;
    uint8_t header[64];
    uint64_t pending;
    bool keep,failed,finished;
} BtTranscriptFilter;
void bt_transcript_filter_init(BtTranscriptFilter *, bool keep, BtTranscriptSink, void *);
int bt_transcript_filter_write(BtTranscriptFilter *, const void *, size_t);
/* Flush held prefixes; mark truncated graphics explicitly. Idempotent. */
int bt_transcript_filter_finish(BtTranscriptFilter *);
#endif
