/* Exercise real Unicode keymap generation with a fake Wayland receiver. */
#define _GNU_SOURCE
#include <assert.h>
#include <wayland-client.h>
#include <xkbcommon/xkbcommon.h>
#include "virtual-keyboard-unstable-v1-client-protocol.h"

static void receive_map(struct zwp_virtual_keyboard_v1 *, uint32_t, int32_t, uint32_t);
static void receive_key(struct zwp_virtual_keyboard_v1 *, uint32_t, uint32_t, uint32_t);
static void receive_modifiers(struct zwp_virtual_keyboard_v1 *, uint32_t, uint32_t, uint32_t, uint32_t);
static int sync_display(struct wl_display *unused) { (void)unused; return 0; }

#define zwp_virtual_keyboard_v1_keymap receive_map
#define zwp_virtual_keyboard_v1_key receive_key
#define zwp_virtual_keyboard_v1_modifiers receive_modifiers
#define wl_display_roundtrip sync_display
#define wl_display_flush sync_display
#define main helper_main
#include "../src/aw_input/aw_input.c"
#undef main

static struct xkb_state *received_state;
static uint32_t expected[240];
static size_t received, maps;
static bool pressed;

static void receive_map(struct zwp_virtual_keyboard_v1 *unused, uint32_t format, int32_t fd, uint32_t size) {
	(void)unused;
	assert(format == XKB_KEYMAP_FORMAT_TEXT_V1);
	char *text = malloc(size);
	assert(text && pread(fd, text, size, 0) == (ssize_t)size);
	struct xkb_keymap *map = xkb_keymap_new_from_string(xkb_ctx, text,
			XKB_KEYMAP_FORMAT_TEXT_V1, XKB_KEYMAP_COMPILE_NO_FLAGS);
	assert(map);
	if (received_state) xkb_state_unref(received_state);
	received_state = xkb_state_new(map);
	assert(received_state);
	xkb_keymap_unref(map);
	free(text);
	maps++;
}

static void receive_key(struct zwp_virtual_keyboard_v1 *unused, uint32_t time, uint32_t code, uint32_t down) {
	(void)unused; (void)time;
	/* Ordinary letter/digit/punctuation positions, never function/media keys. */
	assert((code >= 2 && code <= 13) || (code >= 16 && code <= 27) ||
			(code >= 30 && code <= 41) || (code >= 43 && code <= 53));
	assert(down != pressed);
	pressed = down;
	if (down) {
		assert(received < sizeof(expected) / sizeof(expected[0]));
		assert(xkb_state_key_get_utf32(received_state, code + KEY_BASE) == expected[received++]);
	}
}

static void receive_modifiers(struct zwp_virtual_keyboard_v1 *unused,
		uint32_t depressed, uint32_t latched, uint32_t locked, uint32_t group) {
	(void)unused;
	xkb_state_update_mask(received_state, depressed, latched, locked, 0, 0, group);
}

int main(void) {
	xkb_ctx = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
	struct xkb_rule_names names = { .rules = "evdev", .model = "pc105", .layout = "us" };
	default_map = xkb_keymap_new_from_names(xkb_ctx, &names, XKB_KEYMAP_COMPILE_NO_FLAGS);
	assert(default_map);
	key_state = xkb_state_new(default_map);
	default_map_str = xkb_keymap_get_as_string(default_map, XKB_KEYMAP_USE_ORIGINAL_FORMAT);
	char line[1026] = "T ";
	size_t offset = 2;
	for (size_t i = 0; i < sizeof(expected) / sizeof(expected[0]); i++) {
		uint32_t cp = expected[i] = i % 3 ? 0x4E00 + i : 0x03A9;
		if (cp < 0x800) {
			line[offset++] = 0xC0 | (cp >> 6);
		} else {
			line[offset++] = 0xE0 | (cp >> 12);
			line[offset++] = 0x80 | ((cp >> 6) & 0x3F);
		}
		line[offset++] = 0x80 | (cp & 0x3F);
	}
	line[offset] = '\0';
	handle_line(line);
	assert(received == sizeof(expected) / sizeof(expected[0]) && !pressed);
	assert(maps > 2);  /* Multiple typing rounds followed by the default map. */
	assert(xkb_state_key_get_utf32(received_state, KEY_A + KEY_BASE) == 'a');
	xkb_state_unref(received_state);
	xkb_state_unref(key_state);
	xkb_keymap_unref(default_map);
	xkb_context_unref(xkb_ctx);
	free(default_map_str);
	puts("PASS: Unicode order, repeated characters, printable keycodes and default-map restoration");
	return 0;
}
