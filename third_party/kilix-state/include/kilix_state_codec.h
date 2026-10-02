#ifndef KILIX_STATE_CODEC_H
#define KILIX_STATE_CODEC_H

/*
 * Bounded, endian-stable payload codecs and version migration dispatch.
 *
 * The storage layer in kilix_state.h owns durable files and CRC records.
 * This header owns only game-defined payload bytes. It performs no allocation
 * or I/O and never assumes that native integer layout is portable.
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum kilixstate_codec_result {
    KILIXSTATE_CODEC_OK = 0,
    KILIXSTATE_CODEC_INVALID_ARGUMENT = 1,
    KILIXSTATE_CODEC_NO_SPACE = 2,
    KILIXSTATE_CODEC_TRUNCATED = 3,
    KILIXSTATE_CODEC_NONCANONICAL = 4,
    KILIXSTATE_CODEC_UNKNOWN_VERSION = 5,
    KILIXSTATE_CODEC_DUPLICATE_VERSION = 6,
    KILIXSTATE_CODEC_SIZE_MISMATCH = 7,
    KILIXSTATE_CODEC_DECODE_FAILED = 8
} kilixstate_codec_result;

typedef struct kilixstate_writer {
    uint8_t *bytes;
    size_t capacity;
    size_t offset;
    kilixstate_codec_result result;
} kilixstate_writer;

typedef struct kilixstate_reader {
    const uint8_t *bytes;
    size_t size;
    size_t offset;
    kilixstate_codec_result result;
} kilixstate_reader;

/* Scalar operations consume either the complete value or no bytes. */

void kilixstate_writer_init(kilixstate_writer *writer, void *bytes,
                            size_t capacity);
bool kilixstate_write_u8(kilixstate_writer *writer, uint8_t value);
bool kilixstate_write_u16(kilixstate_writer *writer, uint16_t value);
bool kilixstate_write_u32(kilixstate_writer *writer, uint32_t value);
bool kilixstate_write_u64(kilixstate_writer *writer, uint64_t value);
bool kilixstate_write_i32(kilixstate_writer *writer, int32_t value);
bool kilixstate_write_bool(kilixstate_writer *writer, bool value);
bool kilixstate_write_bytes(kilixstate_writer *writer, const void *bytes,
                            size_t byte_count);
bool kilixstate_write_zeroes(kilixstate_writer *writer, size_t byte_count);
size_t kilixstate_writer_size(const kilixstate_writer *writer);
size_t kilixstate_writer_remaining(const kilixstate_writer *writer);
kilixstate_codec_result
kilixstate_writer_result(const kilixstate_writer *writer);

void kilixstate_reader_init(kilixstate_reader *reader, const void *bytes,
                            size_t size);
bool kilixstate_read_u8(kilixstate_reader *reader, uint8_t *value);
bool kilixstate_read_u16(kilixstate_reader *reader, uint16_t *value);
bool kilixstate_read_u32(kilixstate_reader *reader, uint32_t *value);
bool kilixstate_read_u64(kilixstate_reader *reader, uint64_t *value);
bool kilixstate_read_i32(kilixstate_reader *reader, int32_t *value);
bool kilixstate_read_bool(kilixstate_reader *reader, bool *value);
bool kilixstate_read_bytes(kilixstate_reader *reader, void *bytes,
                           size_t byte_count);
bool kilixstate_skip(kilixstate_reader *reader, size_t byte_count);
bool kilixstate_reader_require_finished(kilixstate_reader *reader);
bool kilixstate_reader_require_zero_tail(kilixstate_reader *reader);
size_t kilixstate_reader_remaining(const kilixstate_reader *reader);
kilixstate_codec_result
kilixstate_reader_result(const kilixstate_reader *reader);

typedef bool (*kilixstate_migration_decode_fn)(kilixstate_reader *reader,
                                               void *context);

typedef struct kilixstate_migration {
    uint32_t version;
    /* Zero accepts any payload size; otherwise the size must match exactly. */
    size_t payload_size;
    /*
     * Dispatch consumes the leading little-endian version before calling
     * decode. When true, every byte left by decode must be zero.
     */
    bool require_zero_tail;
    kilixstate_migration_decode_fn decode;
} kilixstate_migration;

/*
 * Select a unique migration by the leading little-endian u32 version.
 * Decoders should build temporary game state and publish it only on success.
 */
kilixstate_codec_result kilixstate_migrate(
    const void *payload, size_t payload_size,
    const kilixstate_migration *migrations, size_t migration_count,
    void *context, uint32_t *decoded_version);

const char *kilixstate_codec_result_name(kilixstate_codec_result result);

#ifdef __cplusplus
}
#endif

#endif
