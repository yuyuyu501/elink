#ifndef ELINKPAD_PROTOCOL_H
#define ELINKPAD_PROTOCOL_H
#include <stdint.h>
#include <stddef.h>
#include <string.h>

#define EP_VERSION 1u
#define EP_TIMEOUT_MS 500u
/* Private device type, buffered IO, read/write access. */
#define EP_QUERY   0x83376000u
#define EP_CLAIM   0x8337e004u
#define EP_UPDATE  0x8337e008u
#define EP_POLL    0x8337e00cu
#define EP_RELEASE 0x8337e010u
#define EP_CONTROL_GUID_INIT {0x654cd75a,0x921d,0x434f,{0x94,0xbb,0x80,0x7d,0xaa,0x86,0xb0,0x76}}
#define EP_XUSB_GUID_INIT {0xec87f1e3,0xc13b,0x4100,{0xb5,0xf7,0x8b,0x84,0xd5,0x42,0x60,0xcb}}
#define EP_HARDWARE_ID L"Elink\\VirtualPadV1"

#pragma pack(push, 1)
typedef struct { uint32_t version, size; } EP_HEADER;
typedef struct {
    uint16_t buttons;
    uint8_t lt, rt;
    int16_t lx, ly, rx, ry;
} EP_PAD;
typedef struct { EP_HEADER header; uint64_t sequence; EP_PAD pad; } EP_INPUT;
typedef struct { EP_HEADER header; uint32_t serial; uint16_t left, right; } EP_FEEDBACK;
typedef struct { EP_HEADER header; uint32_t max_pads, timeout_ms; } EP_INFO;
#pragma pack(pop)

typedef struct {
    uintptr_t owner;
    uint64_t sequence, last_update;
    uint32_t packet;
    EP_PAD pad;
    EP_FEEDBACK feedback;
} EP_STATE;

static int ep_header(const void *data, size_t length, size_t expected) {
    EP_HEADER header;
    if (!data || length != expected || length < sizeof(header)) return 0;
    memcpy(&header, data, sizeof(header));
    return header.version == EP_VERSION && header.size == expected;
}
static void ep_neutral(EP_STATE *s) {
    EP_PAD zero = {0};
    if (memcmp(&zero, &s->pad, sizeof(zero))) { s->pad = zero; s->packet++; }
    if (s->feedback.left || s->feedback.right) {
        s->feedback.left = s->feedback.right = 0;
        s->feedback.serial++;
    }
}
static int ep_claim(EP_STATE *s, uintptr_t owner) {
    if (!owner || s->owner) return 0;
    ep_neutral(s);
    s->owner = owner; s->sequence = 0; s->last_update = 0;
    s->feedback.header.version = EP_VERSION;
    s->feedback.header.size = sizeof(EP_FEEDBACK);
    return 1;
}
static void ep_release(EP_STATE *s, uintptr_t owner) {
    if (owner && s->owner == owner) { ep_neutral(s); s->owner = 0; s->sequence = 0; }
}
static int ep_update(EP_STATE *s, uintptr_t owner, const void *data, size_t length, uint64_t now) {
    EP_INPUT input;
    if (!owner || owner != s->owner || !ep_header(data, length, sizeof(input))) return 0;
    memcpy(&input, data, sizeof(input));
    if (input.sequence <= s->sequence || (input.pad.buttons & 0x0800)) return 0;
    if (memcmp(&input.pad, &s->pad, sizeof(s->pad))) { s->pad = input.pad; s->packet++; }
    s->sequence = input.sequence; s->last_update = now;
    return 1;
}
static void ep_expire(EP_STATE *s, uint64_t now) {
    if (s->owner && now - s->last_update >= EP_TIMEOUT_MS) ep_neutral(s);
}
/* XUSB layout reference: HIDMaestro companion.c, MIT, pinned in README. */
static void ep_xusb_state(const EP_STATE *s, uint8_t out[29], int waiting) {
    memset(out, 0, 29); out[0] = 3; out[1] = 1;
    out[2] = waiting ? 3 : 1;
    if (waiting) out[10] = 0x14;
    memcpy(out + 5, &s->packet, 4);
    memcpy(out + 11, &s->pad, 12);
}
/* SET_STATE: slot, LED, large motor, small motor, flags (1 LED, 2 motors). */
static int ep_rumble(EP_STATE *s, const uint8_t *data, size_t length) {
    uint16_t left, right;
    if (!data || length != 5 || data[0] || !data[4] || (data[4] & ~3u)) return 0;
    if (!(data[4] & 2u) || !s->owner) return 1;
    left = (uint16_t)(data[2] * 257u); right = (uint16_t)(data[3] * 257u);
    if (left != s->feedback.left || right != s->feedback.right) {
        s->feedback.left = left; s->feedback.right = right; s->feedback.serial++;
    }
    return 1;
}
#endif
