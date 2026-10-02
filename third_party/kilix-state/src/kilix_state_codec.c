#include "kilix_state_codec.h"

#include <string.h>

static bool writer_reserve(kilixstate_writer *writer, size_t byte_count)
{
    if (!writer || writer->result != KILIXSTATE_CODEC_OK) return false;
    if (writer->offset > writer->capacity ||
        byte_count > writer->capacity - writer->offset) {
        writer->result = KILIXSTATE_CODEC_NO_SPACE;
        return false;
    }
    return true;
}

void kilixstate_writer_init(kilixstate_writer *writer, void *bytes,
                            size_t capacity)
{
    if (!writer) return;
    writer->bytes = bytes;
    writer->capacity = bytes || capacity == 0u ? capacity : 0u;
    writer->offset = 0u;
    writer->result = bytes || capacity == 0u ?
                     KILIXSTATE_CODEC_OK :
                     KILIXSTATE_CODEC_INVALID_ARGUMENT;
}

bool kilixstate_write_u8(kilixstate_writer *writer, uint8_t value)
{
    if (!writer_reserve(writer, 1u)) return false;
    writer->bytes[writer->offset++] = value;
    return true;
}

bool kilixstate_write_u16(kilixstate_writer *writer, uint16_t value)
{
    if (!writer_reserve(writer, 2u)) return false;
    writer->bytes[writer->offset++] = (uint8_t)(value & UINT16_C(0xff));
    writer->bytes[writer->offset++] =
        (uint8_t)((value >> 8) & UINT16_C(0xff));
    return true;
}

bool kilixstate_write_u32(kilixstate_writer *writer, uint32_t value)
{
    if (!writer_reserve(writer, 4u)) return false;
    writer->bytes[writer->offset++] = (uint8_t)(value & UINT32_C(0xff));
    writer->bytes[writer->offset++] =
        (uint8_t)((value >> 8) & UINT32_C(0xff));
    writer->bytes[writer->offset++] =
        (uint8_t)((value >> 16) & UINT32_C(0xff));
    writer->bytes[writer->offset++] =
        (uint8_t)((value >> 24) & UINT32_C(0xff));
    return true;
}

bool kilixstate_write_u64(kilixstate_writer *writer, uint64_t value)
{
    size_t offset;

    if (!writer_reserve(writer, 8u)) return false;
    offset = writer->offset;
    writer->bytes[offset] = (uint8_t)(value & UINT64_C(0xff));
    writer->bytes[offset + 1u] =
        (uint8_t)((value >> 8) & UINT64_C(0xff));
    writer->bytes[offset + 2u] =
        (uint8_t)((value >> 16) & UINT64_C(0xff));
    writer->bytes[offset + 3u] =
        (uint8_t)((value >> 24) & UINT64_C(0xff));
    writer->bytes[offset + 4u] =
        (uint8_t)((value >> 32) & UINT64_C(0xff));
    writer->bytes[offset + 5u] =
        (uint8_t)((value >> 40) & UINT64_C(0xff));
    writer->bytes[offset + 6u] =
        (uint8_t)((value >> 48) & UINT64_C(0xff));
    writer->bytes[offset + 7u] =
        (uint8_t)((value >> 56) & UINT64_C(0xff));
    writer->offset += 8u;
    return true;
}

bool kilixstate_write_i32(kilixstate_writer *writer, int32_t value)
{
    uint32_t encoded;
    (void)memcpy(&encoded, &value, sizeof encoded);
    return kilixstate_write_u32(writer, encoded);
}

bool kilixstate_write_bool(kilixstate_writer *writer, bool value)
{
    return kilixstate_write_u8(writer, value ? UINT8_C(1) : UINT8_C(0));
}

bool kilixstate_write_bytes(kilixstate_writer *writer, const void *bytes,
                            size_t byte_count)
{
    if (byte_count != 0u && !bytes) {
        if (writer && writer->result == KILIXSTATE_CODEC_OK)
            writer->result = KILIXSTATE_CODEC_INVALID_ARGUMENT;
        return false;
    }
    if (!writer_reserve(writer, byte_count)) return false;
    if (byte_count != 0u)
        (void)memcpy(writer->bytes + writer->offset, bytes, byte_count);
    writer->offset += byte_count;
    return true;
}

bool kilixstate_write_zeroes(kilixstate_writer *writer, size_t byte_count)
{
    if (!writer_reserve(writer, byte_count)) return false;
    if (byte_count != 0u)
        (void)memset(writer->bytes + writer->offset, 0, byte_count);
    writer->offset += byte_count;
    return true;
}

size_t kilixstate_writer_size(const kilixstate_writer *writer)
{
    return writer ? writer->offset : 0u;
}

size_t kilixstate_writer_remaining(const kilixstate_writer *writer)
{
    return writer && writer->offset <= writer->capacity ?
           writer->capacity - writer->offset : 0u;
}

kilixstate_codec_result
kilixstate_writer_result(const kilixstate_writer *writer)
{
    return writer ? writer->result : KILIXSTATE_CODEC_INVALID_ARGUMENT;
}

static bool reader_reserve(kilixstate_reader *reader, size_t byte_count)
{
    if (!reader || reader->result != KILIXSTATE_CODEC_OK) return false;
    if (reader->offset > reader->size ||
        byte_count > reader->size - reader->offset) {
        reader->result = KILIXSTATE_CODEC_TRUNCATED;
        return false;
    }
    return true;
}

void kilixstate_reader_init(kilixstate_reader *reader, const void *bytes,
                            size_t size)
{
    if (!reader) return;
    reader->bytes = bytes;
    reader->size = bytes || size == 0u ? size : 0u;
    reader->offset = 0u;
    reader->result = bytes || size == 0u ?
                     KILIXSTATE_CODEC_OK :
                     KILIXSTATE_CODEC_INVALID_ARGUMENT;
}

bool kilixstate_read_u8(kilixstate_reader *reader, uint8_t *value)
{
    if (!value) {
        if (reader && reader->result == KILIXSTATE_CODEC_OK)
            reader->result = KILIXSTATE_CODEC_INVALID_ARGUMENT;
        return false;
    }
    if (!reader_reserve(reader, 1u)) return false;
    *value = reader->bytes[reader->offset++];
    return true;
}

bool kilixstate_read_u16(kilixstate_reader *reader, uint16_t *value)
{
    if (!value) {
        if (reader && reader->result == KILIXSTATE_CODEC_OK)
            reader->result = KILIXSTATE_CODEC_INVALID_ARGUMENT;
        return false;
    }
    if (!reader_reserve(reader, 2u)) return false;
    *value = (uint16_t)reader->bytes[reader->offset] |
             (uint16_t)((uint16_t)reader->bytes[reader->offset + 1u] << 8);
    reader->offset += 2u;
    return true;
}

bool kilixstate_read_u32(kilixstate_reader *reader, uint32_t *value)
{
    if (!value) {
        if (reader && reader->result == KILIXSTATE_CODEC_OK)
            reader->result = KILIXSTATE_CODEC_INVALID_ARGUMENT;
        return false;
    }
    if (!reader_reserve(reader, 4u)) return false;
    *value = (uint32_t)reader->bytes[reader->offset] |
             (uint32_t)reader->bytes[reader->offset + 1u] << 8 |
             (uint32_t)reader->bytes[reader->offset + 2u] << 16 |
             (uint32_t)reader->bytes[reader->offset + 3u] << 24;
    reader->offset += 4u;
    return true;
}

bool kilixstate_read_u64(kilixstate_reader *reader, uint64_t *value)
{
    size_t offset;

    if (!value) {
        if (reader && reader->result == KILIXSTATE_CODEC_OK)
            reader->result = KILIXSTATE_CODEC_INVALID_ARGUMENT;
        return false;
    }
    if (!reader_reserve(reader, 8u)) return false;
    offset = reader->offset;
    *value = (uint64_t)reader->bytes[offset] |
             (uint64_t)reader->bytes[offset + 1u] << 8 |
             (uint64_t)reader->bytes[offset + 2u] << 16 |
             (uint64_t)reader->bytes[offset + 3u] << 24 |
             (uint64_t)reader->bytes[offset + 4u] << 32 |
             (uint64_t)reader->bytes[offset + 5u] << 40 |
             (uint64_t)reader->bytes[offset + 6u] << 48 |
             (uint64_t)reader->bytes[offset + 7u] << 56;
    reader->offset += 8u;
    return true;
}

bool kilixstate_read_i32(kilixstate_reader *reader, int32_t *value)
{
    uint32_t encoded;
    if (!value) {
        if (reader && reader->result == KILIXSTATE_CODEC_OK)
            reader->result = KILIXSTATE_CODEC_INVALID_ARGUMENT;
        return false;
    }
    if (!kilixstate_read_u32(reader, &encoded)) return false;
    (void)memcpy(value, &encoded, sizeof *value);
    return true;
}

bool kilixstate_read_bool(kilixstate_reader *reader, bool *value)
{
    uint8_t encoded;
    if (!value) {
        if (reader && reader->result == KILIXSTATE_CODEC_OK)
            reader->result = KILIXSTATE_CODEC_INVALID_ARGUMENT;
        return false;
    }
    if (!kilixstate_read_u8(reader, &encoded)) return false;
    if (encoded > UINT8_C(1)) {
        reader->result = KILIXSTATE_CODEC_NONCANONICAL;
        return false;
    }
    *value = encoded != 0u;
    return true;
}

bool kilixstate_read_bytes(kilixstate_reader *reader, void *bytes,
                           size_t byte_count)
{
    if (byte_count != 0u && !bytes) {
        if (reader && reader->result == KILIXSTATE_CODEC_OK)
            reader->result = KILIXSTATE_CODEC_INVALID_ARGUMENT;
        return false;
    }
    if (!reader_reserve(reader, byte_count)) return false;
    if (byte_count != 0u)
        (void)memcpy(bytes, reader->bytes + reader->offset, byte_count);
    reader->offset += byte_count;
    return true;
}

bool kilixstate_skip(kilixstate_reader *reader, size_t byte_count)
{
    if (!reader_reserve(reader, byte_count)) return false;
    reader->offset += byte_count;
    return true;
}

bool kilixstate_reader_require_finished(kilixstate_reader *reader)
{
    if (!reader || reader->result != KILIXSTATE_CODEC_OK) return false;
    if (reader->offset != reader->size) {
        reader->result = KILIXSTATE_CODEC_NONCANONICAL;
        return false;
    }
    return true;
}

bool kilixstate_reader_require_zero_tail(kilixstate_reader *reader)
{
    size_t index;
    if (!reader || reader->result != KILIXSTATE_CODEC_OK) return false;
    for (index = reader->offset; index < reader->size; ++index) {
        if (reader->bytes[index] != 0u) {
            reader->result = KILIXSTATE_CODEC_NONCANONICAL;
            return false;
        }
    }
    reader->offset = reader->size;
    return true;
}

size_t kilixstate_reader_remaining(const kilixstate_reader *reader)
{
    return reader && reader->offset <= reader->size ?
           reader->size - reader->offset : 0u;
}

kilixstate_codec_result
kilixstate_reader_result(const kilixstate_reader *reader)
{
    return reader ? reader->result : KILIXSTATE_CODEC_INVALID_ARGUMENT;
}

kilixstate_codec_result kilixstate_migrate(
    const void *payload, size_t payload_size,
    const kilixstate_migration *migrations, size_t migration_count,
    void *context, uint32_t *decoded_version)
{
    kilixstate_reader reader;
    const kilixstate_migration *selected = NULL;
    uint32_t version;
    size_t index;
    size_t previous;

    if ((!payload && payload_size != 0u) ||
        (!migrations && migration_count != 0u))
        return KILIXSTATE_CODEC_INVALID_ARGUMENT;
    for (index = 0u; index < migration_count; ++index) {
        if (!migrations[index].decode)
            return KILIXSTATE_CODEC_INVALID_ARGUMENT;
        for (previous = 0u; previous < index; ++previous)
            if (migrations[previous].version == migrations[index].version)
                return KILIXSTATE_CODEC_DUPLICATE_VERSION;
    }
    kilixstate_reader_init(&reader, payload, payload_size);
    if (!kilixstate_read_u32(&reader, &version))
        return kilixstate_reader_result(&reader);
    for (index = 0u; index < migration_count; ++index)
        if (migrations[index].version == version) {
            selected = &migrations[index];
            break;
        }
    if (!selected) return KILIXSTATE_CODEC_UNKNOWN_VERSION;
    if (selected->payload_size != 0u &&
        selected->payload_size != payload_size)
        return KILIXSTATE_CODEC_SIZE_MISMATCH;
    if (!selected->decode(&reader, context)) {
        kilixstate_codec_result result = kilixstate_reader_result(&reader);
        return result == KILIXSTATE_CODEC_OK ?
               KILIXSTATE_CODEC_DECODE_FAILED : result;
    }
    if (selected->require_zero_tail &&
        !kilixstate_reader_require_zero_tail(&reader))
        return kilixstate_reader_result(&reader);
    if (decoded_version) *decoded_version = version;
    return KILIXSTATE_CODEC_OK;
}

const char *kilixstate_codec_result_name(kilixstate_codec_result result)
{
    switch (result) {
    case KILIXSTATE_CODEC_OK: return "ok";
    case KILIXSTATE_CODEC_INVALID_ARGUMENT: return "invalid argument";
    case KILIXSTATE_CODEC_NO_SPACE: return "output buffer too small";
    case KILIXSTATE_CODEC_TRUNCATED: return "truncated payload";
    case KILIXSTATE_CODEC_NONCANONICAL: return "noncanonical payload";
    case KILIXSTATE_CODEC_UNKNOWN_VERSION: return "unknown version";
    case KILIXSTATE_CODEC_DUPLICATE_VERSION: return "duplicate version";
    case KILIXSTATE_CODEC_SIZE_MISMATCH: return "payload size mismatch";
    case KILIXSTATE_CODEC_DECODE_FAILED: return "migration decode failed";
    default: return "unknown codec result";
    }
}
