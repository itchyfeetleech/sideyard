/* aw-input: persistent virtual-pointer/virtual-keyboard helper.
 *
 * Owns both virtual devices for the session lifetime and executes bounded
 * input commands received as lines on stdin, one ack line per command on
 * stdout. Coordinates are inner-output layout pixels.
 *
 * Commands (fields space separated, one line each):
 *   M <x> <y>            absolute pointer motion
 *   B <code> <0|1>       pointer button release/press (linux/input-event-codes)
 *   S <dx> <dy>          scroll in discrete steps (x, y)
 *   K <code> <0|1>       keyboard key release/press (evdev code, us map)
 *   T <utf8-text>        type text; rest of line is the payload (no newlines)
 *   C                    cancel: release all pressed keys/buttons
 *   Q                    quit cleanly
 * Replies: READY (once, after devices exist), OK, or ERR <message>.
 *
 * Unicode typing generates a throwaway XKB keymap mapping printable-key codes to
 * the requested codepoints, following the approach of wtype (MIT licensed,
 * https://github.com/atx/wtype), then restores the default map.
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/input-event-codes.h>
#include <poll.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#include <wayland-client.h>
#include <xkbcommon/xkbcommon.h>

#include "virtual-keyboard-unstable-v1-client-protocol.h"
#include "wlr-virtual-pointer-unstable-v1-client-protocol.h"

#define MAX_LINE 4096
#define MAX_TYPE_BYTES 1024
#define MAX_TYPE_CHARS 256
#define KEY_BASE 8 /* xkb keycode = evdev code + 8 */

static struct wl_display *display;
static struct wl_seat *seat;
static struct zwp_virtual_keyboard_manager_v1 *kbd_mgr;
static struct zwlr_virtual_pointer_manager_v1 *ptr_mgr;
static struct zwp_virtual_keyboard_v1 *kbd;
static struct zwlr_virtual_pointer_v1 *ptr;
static uint32_t ptr_mgr_version;
static bool dead;
static uint32_t extent_w, extent_h;

static struct xkb_context *xkb_ctx;
static struct xkb_keymap *default_map;
static struct xkb_state *key_state;
static char *default_map_str;

/* Tracked pressed state for release on cancel. */
static bool keys_down[KEY_MAX + 1];
static uint32_t btns_down[16];
static size_t n_btns_down;

static uint32_t now_ms(void) {
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint32_t)(ts.tv_sec * 1000 + ts.tv_nsec / 1000000);
}

static void send_keymap(struct xkb_keymap *map) {
	char *allocated = xkb_keymap_get_as_string(map, XKB_KEYMAP_USE_ORIGINAL_FORMAT);
	const char *str = allocated ? allocated : default_map_str;
	size_t len = strlen(str) + 1;
	int fd = memfd_create("aw-keymap", MFD_CLOEXEC);
	if (fd < 0) {
		free(allocated);
		return;
	}
	if (write(fd, str, len) != (ssize_t)len) {
		close(fd);
		free(allocated);
		return;
	}
	zwp_virtual_keyboard_v1_keymap(kbd, XKB_KEYMAP_FORMAT_TEXT_V1, fd, (uint32_t)len);
	close(fd);
	free(allocated);
}

static void registry_global(void *data, struct wl_registry *reg, uint32_t name,
		const char *iface, uint32_t version) {
	(void)data;
	if (strcmp(iface, wl_seat_interface.name) == 0) {
		seat = wl_registry_bind(reg, name, &wl_seat_interface, 1);
	} else if (strcmp(iface, zwp_virtual_keyboard_manager_v1_interface.name) == 0) {
		kbd_mgr = wl_registry_bind(reg, name,
				&zwp_virtual_keyboard_manager_v1_interface, 1);
	} else if (strcmp(iface, zwlr_virtual_pointer_manager_v1_interface.name) == 0) {
		ptr_mgr_version = version < 2 ? version : 2;
		ptr_mgr = wl_registry_bind(reg, name,
				&zwlr_virtual_pointer_manager_v1_interface, ptr_mgr_version);
	}
}

static void registry_remove(void *data, struct wl_registry *reg, uint32_t name) {
	(void)data; (void)reg; (void)name;
}

static const struct wl_registry_listener registry_listener = {
	.global = registry_global,
	.global_remove = registry_remove,
};

/* Decode one UTF-8 sequence; returns bytes consumed, 0 on invalid. */
static size_t utf8_decode(const char *s, size_t n, uint32_t *cp) {
	if (n == 0) {
		return 0;
	}
	unsigned char c = (unsigned char)s[0];
	if (c < 0x80) {
		*cp = c;
		return 1;
	}
	size_t want;
	uint32_t v;
	if ((c & 0xE0) == 0xC0) {
		want = 2; v = c & 0x1F;
	} else if ((c & 0xF0) == 0xE0) {
		want = 3; v = c & 0x0F;
	} else if ((c & 0xF8) == 0xF0) {
		want = 4; v = c & 0x07;
	} else {
		return 0;
	}
	if (n < want) {
		return 0;
	}
	for (size_t i = 1; i < want; i++) {
		unsigned char d = (unsigned char)s[i];
		if ((d & 0xC0) != 0x80) {
			return 0;
		}
		v = (v << 6) | (d & 0x3F);
	}
	*cp = v;
	return want;
}

/* Evdev codes whose usual meaning is a plain printable key (digits, letters,
 * punctuation). Compositors match keybinds against the user's own layout, so
 * typing on codes that mean F9, Print, volume or brightness there would be
 * swallowed and could fire those binds. Prefer ordinary printable positions. */
static const uint32_t TYPE_CODES[] = {
	2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13,          /* 1..0 - =   */
	16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27,  /* q..p [ ]   */
	30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41,  /* a..l ; ' ` */
	43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53,      /* \ z..m , . / */
};
#define N_TYPE_CODES (sizeof(TYPE_CODES) / sizeof(TYPE_CODES[0]))

/* Build a keymap putting each distinct codepoint on one TYPE_CODES entry (wtype approach). */
static struct xkb_keymap *build_type_map(const uint32_t *cps, size_t n) {
	size_t cap = 4096 + n * 96;
	char *buf = malloc(cap);
	if (!buf) {
		return NULL;
	}
	size_t off = 0;
	off += (size_t)snprintf(buf + off, cap - off,
			"xkb_keymap {\n"
			"  xkb_keycodes \"aw\" {\n"
			"    minimum = 8;\n"
			"    maximum = 255;\n");
	for (size_t i = 0; i < n; i++) {
		off += (size_t)snprintf(buf + off, cap - off,
				"    <K%02zu> = %u;\n", i, TYPE_CODES[i] + KEY_BASE);
	}
	off += (size_t)snprintf(buf + off, cap - off,
			"  };\n"
			"  xkb_types \"aw\" { include \"complete\" };\n"
			"  xkb_compat \"aw\" { include \"complete\" };\n"
			"  xkb_symbols \"aw\" {\n");
	for (size_t i = 0; i < n; i++) {
		off += (size_t)snprintf(buf + off, cap - off,
				"    key <K%02zu> { [ U%04X ] };\n", i, cps[i]);
	}
	off += (size_t)snprintf(buf + off, cap - off, "  };\n};\n");
	struct xkb_keymap *map = xkb_keymap_new_from_string(xkb_ctx, buf,
			XKB_KEYMAP_FORMAT_TEXT_V1, XKB_KEYMAP_COMPILE_NO_FLAGS);
	free(buf);
	return map;
}

static void send_modifiers(void) {
	zwp_virtual_keyboard_v1_modifiers(kbd,
		xkb_state_serialize_mods(key_state, XKB_STATE_MODS_DEPRESSED),
		xkb_state_serialize_mods(key_state, XKB_STATE_MODS_LATCHED),
		xkb_state_serialize_mods(key_state, XKB_STATE_MODS_LOCKED),
		xkb_state_serialize_layout(key_state, XKB_STATE_LAYOUT_EFFECTIVE));
}

static void release_all(void) {
	uint32_t t = now_ms();
	for (int i = 0; i <= KEY_MAX; i++) {
		if (keys_down[i]) {
			zwp_virtual_keyboard_v1_key(kbd, t, (uint32_t)i, 0);
			xkb_state_update_key(key_state, (xkb_keycode_t)i + KEY_BASE, XKB_KEY_UP);
			keys_down[i] = false;
		}
	}
	xkb_state_update_mask(key_state, 0, 0, 0, 0, 0, 0);
	send_modifiers();
	for (size_t i = 0; i < n_btns_down; i++) {
		zwlr_virtual_pointer_v1_button(ptr, t, btns_down[i], 0);
	}
	if (n_btns_down > 0) {
		zwlr_virtual_pointer_v1_frame(ptr);
		n_btns_down = 0;
	}
	wl_display_flush(display);
}

static void handle_line(char *line) {
	char op = line[0];
	if (op == 'M') {
		int x, y;
		if (sscanf(line + 1, "%d %d", &x, &y) != 2 || x < 0 || y < 0) {
			printf("ERR bad move\n");
			return;
		}
		zwlr_virtual_pointer_v1_motion_absolute(ptr, now_ms(),
				(uint32_t)x, (uint32_t)y, extent_w, extent_h);
		zwlr_virtual_pointer_v1_frame(ptr);
		wl_display_flush(display);
		/* Ack only once the server consumed the request; quitting
		 * (or a keymap restore) ahead of the server drops input. */
		wl_display_roundtrip(display);
		printf("OK\n");
	} else if (op == 'B') {
		int code, press;
		if (sscanf(line + 1, "%d %d", &code, &press) != 2 ||
				code < 0 || code > 0x2FF || (press != 0 && press != 1)) {
			printf("ERR bad button\n");
			return;
		}
		zwlr_virtual_pointer_v1_button(ptr, now_ms(), (uint32_t)code,
				(uint32_t)press);
		zwlr_virtual_pointer_v1_frame(ptr);
		wl_display_flush(display);
		wl_display_roundtrip(display);
		if (press) {
			if (n_btns_down < 16) {
				btns_down[n_btns_down++] = (uint32_t)code;
			}
		} else {
			for (size_t i = 0; i < n_btns_down; i++) {
				if (btns_down[i] == (uint32_t)code) {
					btns_down[i] = btns_down[--n_btns_down];
					break;
				}
			}
		}
		printf("OK\n");
	} else if (op == 'S') {
		int dx, dy;
		if (sscanf(line + 1, "%d %d", &dx, &dy) != 2) {
			printf("ERR bad scroll\n");
			return;
		}
		uint32_t t = now_ms();
		/* wl_pointer.axis: 0 = vertical, 1 = horizontal. */
		if (dy) {
			zwlr_virtual_pointer_v1_axis(ptr, t, 0,
					wl_fixed_from_int(dy * 15));
			zwlr_virtual_pointer_v1_axis_discrete(ptr, t, 0,
					wl_fixed_from_int(dy * 15), dy);
		}
		if (dx) {
			zwlr_virtual_pointer_v1_axis(ptr, t, 1,
					wl_fixed_from_int(dx * 15));
			zwlr_virtual_pointer_v1_axis_discrete(ptr, t, 1,
					wl_fixed_from_int(dx * 15), dx);
		}
		zwlr_virtual_pointer_v1_frame(ptr);
		wl_display_flush(display);
		wl_display_roundtrip(display);
		printf("OK\n");
	} else if (op == 'K') {
		int code, press;
		if (sscanf(line + 1, "%d %d", &code, &press) != 2 ||
				code < 0 || code > KEY_MAX || (press != 0 && press != 1)) {
			printf("ERR bad key\n");
			return;
		}
		zwp_virtual_keyboard_v1_key(kbd, now_ms(), (uint32_t)code,
				(uint32_t)press);
		keys_down[code] = press == 1;
		xkb_state_update_key(key_state, (xkb_keycode_t)code + KEY_BASE,
			press ? XKB_KEY_DOWN : XKB_KEY_UP);
		send_modifiers();
		wl_display_flush(display);
		wl_display_roundtrip(display);
		printf("OK\n");
	} else if (op == 'T') {
		char *text = line + 1;
		if (*text == ' ') {
			text++;
		}
		size_t len = strlen(text);
		if (len == 0 || len > MAX_TYPE_BYTES) {
			printf("ERR bad text\n");
			return;
		}
		uint32_t cps[MAX_TYPE_CHARS];
		size_t n = 0, pos = 0;
		while (pos < len && n < MAX_TYPE_CHARS) {
			uint32_t cp;
			size_t used = utf8_decode(text + pos, len - pos, &cp);
			if (used == 0 || cp < 0x20 || cp == 0x7F || cp > 0x10FFFF) {
				printf("ERR bad utf8\n");
				return;
			}
			cps[n++] = cp;
			pos += used;
		}
		if (pos != len) {
			printf("ERR text too long\n");
			return;
		}
		/* Type in rounds of at most N_TYPE_CODES distinct codepoints. */
		for (size_t start = 0; start < n;) {
			uint32_t distinct[N_TYPE_CODES];
			size_t slot[MAX_TYPE_CHARS];
			size_t nd = 0, end = start;
			for (; end < n; end++) {
				size_t k = 0;
				while (k < nd && distinct[k] != cps[end]) {
					k++;
				}
				if (k == nd) {
					if (nd == N_TYPE_CODES) {
						break;
					}
					distinct[nd++] = cps[end];
				}
				slot[end] = k;
			}
			struct xkb_keymap *map = build_type_map(distinct, nd);
			if (!map) {
				send_keymap(default_map);
				wl_display_roundtrip(display);
				printf("ERR keymap failed\n");
				return;
			}
			send_keymap(map);
			zwp_virtual_keyboard_v1_modifiers(kbd, 0, 0, 0, 0);
			wl_display_roundtrip(display);
			for (size_t i = start; i < end; i++) {
				uint32_t t = now_ms();
				zwp_virtual_keyboard_v1_key(kbd, t, TYPE_CODES[slot[i]], 1);
				zwp_virtual_keyboard_v1_key(kbd, t, TYPE_CODES[slot[i]], 0);
			}
			wl_display_flush(display);
			/* Keys must be dispatched under the type map before restoring
			 * the default map; quitting ahead of the server drops them. */
			wl_display_roundtrip(display);
			xkb_keymap_unref(map);
			start = end;
		}
		send_keymap(default_map);
		send_modifiers();
		wl_display_flush(display);
		wl_display_roundtrip(display);
		printf("OK\n");
	} else if (op == 'C') {
		release_all();
		wl_display_roundtrip(display);
		printf("OK\n");
	} else if (op == 'Q') {
		printf("OK\n");
		fflush(stdout);
		dead = true;
	} else {
		printf("ERR unknown command\n");
	}
	fflush(stdout);
}

int main(void) {
	if (!getenv("WAYLAND_DISPLAY")) {
		fprintf(stderr, "aw-input: WAYLAND_DISPLAY is not set\n");
		return 1;
	}
	const char *ew = getenv("AW_EXTENT_W"), *eh = getenv("AW_EXTENT_H");
	if (!ew || !eh || (extent_w = (uint32_t)atoi(ew)) == 0 ||
			(extent_h = (uint32_t)atoi(eh)) == 0) {
		fprintf(stderr, "aw-input: AW_EXTENT_W/H must name the layout size\n");
		return 1;
	}
	display = wl_display_connect(NULL);
	if (!display) {
		fprintf(stderr, "aw-input: cannot connect to Wayland display\n");
		return 1;
	}
	struct wl_registry *reg = wl_display_get_registry(display);
	wl_registry_add_listener(reg, &registry_listener, NULL);
	wl_display_roundtrip(display);
	if (!seat || !kbd_mgr || !ptr_mgr) {
		fprintf(stderr, "aw-input: seat/keyboard/pointer protocol missing\n");
		return 1;
	}
	if (ptr_mgr_version < 2) {
		fprintf(stderr, "aw-input: virtual-pointer v2 required\n");
		return 1;
	}
	kbd = zwp_virtual_keyboard_manager_v1_create_virtual_keyboard(kbd_mgr, seat);
	ptr = zwlr_virtual_pointer_manager_v1_create_virtual_pointer(ptr_mgr, seat);

	xkb_ctx = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
	struct xkb_rule_names names = {
		.rules = "evdev", .model = "pc105", .layout = "us",
		.variant = NULL, .options = NULL,
	};
	default_map = xkb_keymap_new_from_names(xkb_ctx, &names,
			XKB_KEYMAP_COMPILE_NO_FLAGS);
	if (!default_map) {
		fprintf(stderr, "aw-input: cannot compile default keymap\n");
		return 1;
	}
	key_state = xkb_state_new(default_map);
	if (!key_state) {
		fprintf(stderr, "aw-input: cannot create keyboard state\n");
		return 1;
	}
	default_map_str = xkb_keymap_get_as_string(default_map,
			XKB_KEYMAP_USE_ORIGINAL_FORMAT);
	send_keymap(default_map);
	wl_display_roundtrip(display);
	if (wl_display_get_error(display)) {
		fprintf(stderr, "aw-input: compositor rejected the devices\n");
		return 1;
	}

	printf("READY\n");
	fflush(stdout);

	char *line = NULL;
	size_t cap = 0;
	struct pollfd pfd = { .fd = STDIN_FILENO, .events = POLLIN };
	while (!dead) {
		wl_display_dispatch_pending(display);
		if (wl_display_get_error(display)) {
			fprintf(stderr, "aw-input: lost compositor connection\n");
			return 1;
		}
		if (poll(&pfd, 1, 100) > 0) {
			ssize_t n = getline(&line, &cap, stdin);
			if (n < 0) {
				break; /* stdin closed: release and exit */
			}
			if (n > MAX_LINE) {
				printf("ERR line too long\n");
				fflush(stdout);
				continue;
			}
			while (n > 0 && (line[n - 1] == '\n' || line[n - 1] == '\r')) {
				line[--n] = '\0';
			}
			if (n > 0) {
				handle_line(line);
			}
		}
	}
	release_all();
	wl_display_roundtrip(display);
	free(line);
	return 0;
}
