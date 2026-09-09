/* SPDX-License-Identifier: MIT */
#include "sixel.h"

#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void require(bool condition, const char *message) {
    if (!condition) {
        fprintf(stderr, "FAIL: %s\n", message);
        exit(1);
    }
}

static BtSixelImage decode(const char *body, unsigned p1, unsigned p2, size_t chunk) {
    const uint8_t background[] = { 19, 23, 30 };
    BtSixel *s = bt_sixel_new(p1, p2, background);
    require(s != NULL, "create decoder");
    size_t length = strlen(body);
    for (size_t pos = 0; pos < length;) {
        size_t size = length - pos < chunk ? length - pos : chunk;
        if (bt_sixel_feed(s, body + pos, size) < 0) {
            fprintf(stderr, "Sixel error: %s\n", bt_sixel_error(s));
            require(false, "feed valid Sixel");
        }
        pos += size;
    }
    BtSixelImage image;
    require(bt_sixel_finish(s, &image) == 0, "finish valid Sixel");
    bt_sixel_free(s);
    return image;
}

static bool pixel(BtSixelImage image, unsigned x, unsigned y,
                  unsigned red, unsigned green, unsigned blue, unsigned alpha) {
    require(x < image.width && y < image.height, "pixel bounds");
    const uint8_t *p = image.pixels + ((size_t)y * image.width + x) * 4;
    return p[0] == red && p[1] == green && p[2] == blue && p[3] == alpha;
}

static void colors_and_chunks(void) {
    const char *body = "\"1;1;3;7#1;2;100;0;0!3~$#2;2;0;100;0?A-#3;2;0;0;100@";
    BtSixelImage reference = decode(body, 0, 1, strlen(body));
    require(reference.width == 3 && reference.height == 7, "raster extent and partial final band");
    for (unsigned y = 0; y < 6; ++y)
        for (unsigned x = 0; x < 3; ++x)
            require(x == 1 && y == 1 ? pixel(reference, x, y, 0, 255, 0, 255)
                                    : pixel(reference, x, y, 255, 0, 0, 255),
                    "RLE paints rows and carriage return preserves previous color planes");
    require(pixel(reference, 0, 6, 0, 0, 255, 255), "newline advances six pixels");
    require(pixel(reference, 1, 6, 0, 0, 0, 0), "transparent padding");
    require(pixel(reference, 2, 6, 0, 0, 0, 0), "transparent padding at row end");
    for (size_t chunk = 1; chunk <= strlen(body); ++chunk) {
        BtSixelImage other = decode(body, 0, 1, chunk);
        require(other.width == reference.width && other.height == reference.height,
                "dimensions independent of chunk boundaries");
        require(memcmp(other.pixels, reference.pixels,
                       (size_t)reference.width * reference.height * 4) == 0,
                "pixels independent of chunk boundaries");
        free(other.pixels);
    }
    free(reference.pixels);
    BtSixelImage opaque = decode(body, 0, 0, 1);
    require(pixel(opaque, 1, 6, 19, 23, 30, 255), "opaque terminal background");
    free(opaque.pixels);

    BtSixelImage growth = decode("\"1;1;2;2#1;2;100;0;0!3~", 0, 1, 3);
    require(growth.width == 3 && growth.height == 6, "raster dimensions are minimum extents");
    require(pixel(growth, 2, 5, 255, 0, 0, 255), "paint beyond declared raster survives");
    free(growth.pixels);
    puts("PASS Sixel RGB, RLE, overprinting, transparency and stream splits");
}

static void palettes_and_hls(void) {
    BtSixelImage image = decode("#1;1;120;50;100@#2;1;240;50;100@#3;1;0;50;100@", 7, 1, 1);
    require(image.width == 3 && image.height == 6, "HLS dimensions");
    require(pixel(image, 0, 0, 255, 0, 0, 255), "DEC HLS 120 is red");
    require(pixel(image, 1, 0, 0, 255, 0, 255), "DEC HLS 240 is green");
    require(pixel(image, 2, 0, 0, 0, 255, 255), "DEC HLS 0 is blue");
    free(image.pixels);
    image = decode("#1;2;999;50;0@#1;2;0;0;100@", 7, 1, 2);
    require(pixel(image, 0, 0, 255, 128, 0, 255), "RGB range clamp and rounding");
    require(pixel(image, 1, 0, 0, 0, 255, 255), "color redefinition applies to later pixels");
    free(image.pixels);

    BtSixel *s = bt_sixel_new(7, 1, NULL);
    require(s != NULL, "palette decoder allocation");
    const char *definition = "#200;2;0;100;100";
    require(bt_sixel_feed(s, definition, strlen(definition)) == 0, "palette definition feed");
    require(bt_sixel_finish(s, &image) == 0 && image.pixels == NULL, "palette-only finish");
    BtSixelPalette palette;
    bt_sixel_get_palette(s, &palette);
    bt_sixel_free(s);
    s = bt_sixel_new(7, 1, NULL);
    require(s && bt_sixel_set_palette(s, &palette) == 0, "import shared palette");
    require(bt_sixel_feed(s, "#200~", 5) == 0, "use imported palette");
    require(bt_sixel_finish(s, &image) == 0, "finish shared palette image");
    require(pixel(image, 0, 5, 0, 255, 255, 255), "shared register carries RGB values");
    free(image.pixels);
    bt_sixel_free(s);
    image = decode("#200~", 7, 1, 1);
    require(pixel(image, 0, 5, 0, 0, 0, 255), "new decoder has private register defaults");
    free(image.pixels);
    puts("PASS Sixel HLS, palette redefinition and optional shared registers");
}

static void aspect_and_empty(void) {
    BtSixelImage image = decode("#1;2;100;0;0@", 0, 1, 2);
    require(image.width == 1 && image.height == 12, "default P1 gives 2:1 aspect");
    require(pixel(image, 0, 0, 255, 0, 0, 255) && pixel(image, 0, 1, 255, 0, 0, 255),
            "aspect expands painted rows");
    require(pixel(image, 0, 2, 0, 0, 0, 0), "aspect leaves next bit transparent");
    free(image.pixels);
    image = decode("\"3;2;1;1#1;2;100;0;0@", 7, 1, 1);
    require(image.width == 1 && image.height == 2, "raster aspect rounds to nearest integer");
    free(image.pixels);
    image = decode("\"1;3;1;1#1;2;100;0;0@", 0, 1, 1);
    require(image.width == 1 && image.height == 1, "raster aspect never collapses pixels");
    free(image.pixels);
    image = decode("\"1;1;2;3", 7, 0, 1);
    require(image.width == 2 && image.height == 3, "unpainted declared raster retained");
    require(pixel(image, 1, 2, 19, 23, 30, 255), "unpainted raster background");
    free(image.pixels);
    image = decode("", 7, 1, 1);
    require(!image.width && !image.height && !image.pixels, "empty payload makes no image");
    image = decode("!0@", 7, 1, 1);
    require(image.width == 1 && image.height == 6, "zero repeat defaults to one");
    free(image.pixels);
    image = decode("#1;2;100;0;0!\n3\r@", 7, 1, 1);
    require(image.width == 3 && pixel(image, 2, 0, 255, 0, 0, 255), "C0 controls do not split parameters");
    free(image.pixels);
    puts("PASS Sixel raster aspect, backgrounds and empty payloads");
}

static void invalid(const char *body) {
    BtSixel *s = bt_sixel_new(7, 1, NULL);
    require(s != NULL, "invalid case allocation");
    BtSixelImage image = { 0 };
    int result = bt_sixel_feed(s, body, strlen(body));
    if (result == 0) result = bt_sixel_finish(s, &image);
    require(result < 0, "invalid Sixel rejected");
    require(bt_sixel_error(s)[0] != 0, "invalid Sixel has diagnostic");
    require(bt_sixel_feed(s, "~", 1) < 0, "decoder error is sticky");
    require(image.pixels == NULL, "invalid Sixel transfers no image");
    bt_sixel_free(s);
}

static void limits_and_lifetime(void) {
    invalid("!42949672960~");
    invalid("!4097~");
    invalid("\"1;1;4097;1~");
    invalid("\"2;1;1;4096~");
    invalid("\"4294967295;1;1;1~");
    invalid("#256~");
    invalid("#1;2;0;0;0;0~");
    invalid("!1;2~");
    char newline_bomb[700];
    memset(newline_bomb, '-', sizeof(newline_bomb) - 1);
    newline_bomb[sizeof(newline_bomb) - 1] = 0;
    invalid(newline_bomb);

    BtSixel *s = bt_sixel_new(7, 1, NULL);
    require(s != NULL, "input bound allocation");
    require(bt_sixel_feed(s, "x", (size_t)BT_SIXEL_MAX_INPUT + 1) < 0,
            "encoded length rejected before reading supplied pointer");
    bt_sixel_free(s);
    s = bt_sixel_new(7, 1, NULL);
    require(s != NULL, "expansion bound allocation");
    bool rejected = false;
    for (unsigned i = 0; i < 3000; ++i)
        if (bt_sixel_feed(s, "!4096~$", 7) < 0) { rejected = true; break; }
    require(rejected, "repeated overprinting has a total expansion bound");
    bt_sixel_free(s);
    s = bt_sixel_new(7, 1, NULL);
    require(s && bt_sixel_feed(s, "~", 1) == 0, "lifetime fixture feed");
    BtSixelImage image;
    require(bt_sixel_finish(s, &image) == 0, "lifetime fixture finish");
    require(bt_sixel_feed(s, "~", 1) < 0, "feed after finish rejected");
    bt_sixel_free(s);
    require(image.width == 1 && image.pixels != NULL, "finished image survives decoder destruction");
    free(image.pixels);
    puts("PASS Sixel size/expansion limits and ownership");
}

int main(void) {
    colors_and_chunks();
    palettes_and_hls();
    aspect_and_empty();
    limits_and_lifetime();
    return 0;
}
