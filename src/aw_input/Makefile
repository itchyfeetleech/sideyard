# Build the aw-input virtual-device helper into the repo build tree.
CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra
WAYLAND_SCANNER ?= wayland-scanner
WLR_PROTO ?= /usr/share/wlr-protocols/unstable/wlr-virtual-pointer-unstable-v1.xml
BUILD ?= ../../build/aw_input
BIN = $(BUILD)/aw-input

PKG_CFLAGS := $(shell pkg-config --cflags wayland-client xkbcommon)
PKG_LIBS := $(shell pkg-config --libs wayland-client xkbcommon)

GEN_H = $(BUILD)/virtual-keyboard-unstable-v1-client-protocol.h \
	$(BUILD)/wlr-virtual-pointer-unstable-v1-client-protocol.h
GEN_C = $(BUILD)/virtual-keyboard-unstable-v1-protocol.c \
	$(BUILD)/wlr-virtual-pointer-unstable-v1-protocol.c

all: $(BIN)

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/%-client-protocol.h: %.xml | $(BUILD)
	$(WAYLAND_SCANNER) client-header $< $@

$(BUILD)/%-protocol.c: %.xml | $(BUILD)
	$(WAYLAND_SCANNER) private-code $< $@

$(BUILD)/wlr-virtual-pointer-unstable-v1-client-protocol.h: $(WLR_PROTO) | $(BUILD)
	$(WAYLAND_SCANNER) client-header $< $@

$(BUILD)/wlr-virtual-pointer-unstable-v1-protocol.c: $(WLR_PROTO) | $(BUILD)
	$(WAYLAND_SCANNER) private-code $< $@

$(BIN): aw_input.c $(GEN_H) $(GEN_C)
	$(CC) $(CFLAGS) $(PKG_CFLAGS) -I$(BUILD) -o $@ aw_input.c $(GEN_C) $(PKG_LIBS)

$(BUILD)/test-input-helper: ../../tests/test_input_helper.c aw_input.c $(GEN_H) $(GEN_C)
	$(CC) $(CFLAGS) $(PKG_CFLAGS) -I$(BUILD) -o $@ ../../tests/test_input_helper.c $(GEN_C) $(PKG_LIBS)

check: $(BUILD)/test-input-helper
	$(BUILD)/test-input-helper

clean:
	rm -rf $(BUILD)

.PHONY: all check clean
